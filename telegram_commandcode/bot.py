"""
telegram-commandcode Bot — Main entry point.

Async Python Telegram bot using python-telegram-bot 20.x.
Bridges Telegram ↔ Command Code CLI with Hermes Agent architecture.

Usage:
    TELEGRAM_BOT_TOKEN=*** python -m telegram_commandcode.bot

Environment:
    TELEGRAM_BOT_TOKEN (required)  — Bot token from @BotFather
    TELEGRAM_ALLOWED_USERS         — Comma-separated user IDs (default: "any")
    COMMAND_CODE_CMD               — Path to cmd binary (default: "cmd")
    COMMAND_CODE_YOLO              — "false" to disable --yolo (default: true)
    COMMAND_CODE_MAX_TURNS         — Max turns per prompt (default: 20)
    OPENAI_API_KEY                 — For voice transcription (optional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from .gateway import BotGateway

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("telegram-commandcode")

# Quiet noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


# ── Bot Commands (Telegram menu) ─────────────────────────────────────────────

BOT_COMMANDS = [
    ("add_dir", "Add directory to workspace: /add-dir <path>"),
    ("agents", "Show agent configuration info"),
    ("background", "Run a task in background: /background <prompt>"),
    ("clear", "Clear conversation history (fresh start)"),
    ("compact", "Compact conversation history"),
    ("compact_mode", "Select compact mode: /compact-mode <mode>"),
    ("configure_models", "Choose model per task: /configure-models"),
    ("context", "Show context window usage"),
    ("courses", "Open Command Code courses in browser"),
    ("effort", "Set reasoning effort: /effort <low|medium|high|xhigh|max>"),
    ("exit", "Exit session (N/A remotely)"),
    ("feedback", "Submit feedback about Command Code"),
    ("fork", "Fork conversation into new session"),
    ("goal", "Set objective: /goal <text|clear|status>"),
    ("help", "Show available commands"),
    ("ide", "Connect IDE — local only"),
    ("info", "Show system information"),
    ("init", "Initialize AGENTS.md for this project"),
    ("learn_taste", "Learn taste from other agents"),
    ("login", "Authenticate with Command Code"),
    ("logout", "Remove stored authentication"),
    ("mcp", "Manage MCP server connections"),
    ("memory", "Manage memory: /memory or /memory <instruction>"),
    ("model", "List models or switch: /model <name>"),
    ("new", "Start a new conversation"),
    ("plan", "Enter plan mode or plan a task"),
    ("pr_comments", "Fetch PR comments for current branch"),
    ("provider", "Set AI provider: /provider <name>"),
    ("queue", "Queue prompt for next turn: /queue <prompt>"),
    ("reasoning", "Set reasoning effort (alias for /effort)"),
    ("reload", "Restart bot and resume session"),
    ("rename", "Rename current session: /rename <name>"),
    ("resume", "Resume a past conversation"),
    ("retry", "Re-run the last prompt"),
    ("review", "Review a pull request: /review or /review <pr>"),
    ("rewind", "Restore to previous checkpoint (TUI only)"),
    ("share", "Share conversation — N/A remotely"),
    ("skills", "Browse and manage agent skills"),
    ("start", "Start the bot"),
    ("status", "Show environment status (model, session, config)"),
    ("steer", "Give mid-session guidance: /steer <instruction>"),
    ("stop", "Stop the running agent"),
    ("taste", "Manage Taste learning"),
    ("terminal_setup", "VSCode keybindings — local only"),
    ("undo", "Back up N turns and re-prompt: /undo [N]"),
    ("unshare", "Stop sharing — N/A remotely"),
    ("update", "Update Command Code to latest version"),
    ("usage", "Show credits, plan, and usage metrics"),
    ("version", "Show Command Code version"),
    ("yolo", "Toggle YOLO mode on/off"),
    ("whoami", "Show your user info"),
]


# ── Singleton gateway ────────────────────────────────────────────────────────

# Created at module level so the same instance handles all updates.
_gateway = BotGateway()


async def _message_handler(update: Update, context) -> None:
    """PTB message handler — delegates to the BotGateway."""
    await _gateway.handle_message(update, context)


# ── Startup / Shutdown ──────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    """Called when the bot starts. Register commands, verify connection."""
    logger.info("🤖 telegram-commandcode v2 starting...")

    me = await app.bot.get_me()
    logger.info("   Bot: @%s (%s)", me.username, me.first_name)
    logger.info("   CMD: %s", os.getenv("COMMAND_CODE_CMD", "cmd"))
    logger.info("   Access: %s", os.getenv("TELEGRAM_ALLOWED_USERS", "any"))

    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("   %d commands registered", len(BOT_COMMANDS))
    except Exception as e:
        logger.warning("   ⚠️ Failed to register commands: %s", e)

    try:
        updates = await app.bot.get_updates()
        if updates:
            await app.update_queue.put(None)
            logger.info("   Cleared %d pending updates", len(updates))
    except Exception:
        pass

    logger.info("   Listening... (Ctrl+C to stop)")


async def _on_shutdown(app: Application) -> None:
    """Graceful shutdown — await pending tasks, kill all running processes."""
    logger.info("Shutting down gracefully...")

    # Wait for in-flight prompt executions to finish
    await _gateway.processor.wait_pending(timeout=10.0)

    from .executor import process_tracker
    await process_tracker.kill_all()
    logger.info("Goodbye.")


# ── Orphan cleanup ──────────────────────────────────────────────────────────

def _kill_orphaned_processes() -> None:
    """Kill leftover `cmd -p` processes from crashed instances (Linux only)."""
    if sys.platform != "linux":
        return
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "axo", "pid,args", "--no-headers"],
            capture_output=True, text=True, timeout=5,
        )
        killed = 0
        for line in result.stdout.strip().split("\n"):
            if "grep" in line or "sh -c" in line:
                continue
            if "cmd" in line and " -p " in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pid = int(parts[0])
                        os.kill(pid, signal.SIGTERM)
                        asyncio.get_event_loop().call_later(
                            2,
                            lambda p=pid: (
                                os.kill(p, signal.SIGKILL)
                                if True
                                else None
                            ),
                        )
                        killed += 1
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        if killed:
            logger.info("   Killed %d orphaned cmd process(es)", killed)
    except Exception:
        pass


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point for `telegram-commandcode` console script."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        for env_path in (
            Path(".env"),
            Path(__file__).resolve().parent.parent / ".env",
        ):
            try:
                for line in env_path.read_text().splitlines():
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except FileNotFoundError:
                continue
        if not token:
            print("❌ TELEGRAM_BOT_TOKEN is required.", file=sys.stderr)
            print(
                "   Set via env var, or create a .env file with: "
                "TELEGRAM_BOT_TOKEN=your_token_here",
                file=sys.stderr,
            )
            sys.exit(1)

    _kill_orphaned_processes()

    app: Application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Register all messages through the single gateway handler
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE,
            _message_handler,
        )
    )

    # Also register command handlers for Telegram's menu system
    for cmd_name, _desc in BOT_COMMANDS:
        app.add_handler(CommandHandler(cmd_name, _message_handler))

    logger.info("Starting bot with long polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
