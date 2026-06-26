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

from .gateway import handle_message

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("telegram-commandcode")

# Quiet some noisy libraries
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
    ("whoami", "Show your user info"),
]


# ── Startup / Shutdown ──────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    """Called when the bot starts. Register commands, verify connection."""
    logger.info("🤖 telegram-commandcode v2 starting...")

    # Verify bot token
    me = await app.bot.get_me()
    logger.info("   Bot: @%s (%s)", me.username, me.first_name)
    logger.info("   CMD: %s", os.getenv("COMMAND_CODE_CMD", "cmd"))
    logger.info("   Access: %s", os.getenv("TELEGRAM_ALLOWED_USERS", "any"))

    # Register commands
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("   %d commands registered", len(BOT_COMMANDS))
    except Exception as e:
        logger.warning("   ⚠️ Failed to register commands: %s", e)

    # Clear pending updates (avoid processing old messages)
    try:
        updates = await app.bot.get_updates()
        if updates:
            await app.update_queue.put(None)  # Skip old
            logger.info("   Cleared %d pending updates", len(updates))
    except Exception:
        pass

    logger.info("   Listening... (Ctrl+C to stop)")


async def _on_shutdown(app: Application) -> None:
    """Graceful shutdown — kill all running processes."""
    logger.info("Shutting down gracefully...")
    from .executor import process_tracker
    await process_tracker.kill_all()
    logger.info("Goodbye.")


# ── Orphan cleanup ──────────────────────────────────────────────────────────

def _kill_orphaned_processes() -> None:
    """
    Kill any orphaned `cmd -p` processes from crashed bot instances.
    Uses `ps` + `kill` on Linux; no-op on other platforms.
    """
    if sys.platform != "linux":
        return
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "axo", "pid,args", "--no-headers"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        killed = 0
        for line in lines:
            if "grep" in line or "sh -c" in line:
                continue
            if "cmd" in line and " -p " in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pid = int(parts[0])
                        os.kill(pid, signal.SIGTERM)
                        # Best-effort SIGKILL after 2s
                        asyncio.get_event_loop().call_later(
                            2, lambda p=pid: os.kill(p, signal.SIGKILL) if True else None
                        )
                        killed += 1
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        if killed > 0:
            logger.info("   Killed %d orphaned cmd process(es)", killed)
    except Exception:
        pass  # ps/grep not available — skip


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point for `telegram-commandcode` console script."""
    # Validate token
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        # Try .env file
        for env_path in (Path(".env"), Path(__file__).resolve().parent.parent / ".env"):
            try:
                for line in env_path.read_text().splitlines():
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except FileNotFoundError:
                continue
        if not token:
            print("❌ TELEGRAM_BOT_TOKEN is required.", file=sys.stderr)
            print("   Set via env var, or create a .env file with: TELEGRAM_BOT_TOKEN=your_token_here", file=sys.stderr)
            sys.exit(1)

    # Cleanup orphans from previous crashed instances
    _kill_orphaned_processes()

    # Build PTB Application
    app: Application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Register handlers
    # Message handler for all non-command text (and commands are filtered inside)
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE,
        handle_message,
    ))

    # Also register command handlers for Telegram's menu system
    for cmd_name, _desc in BOT_COMMANDS:
        app.add_handler(CommandHandler(cmd_name, handle_message))

    # Start polling
    logger.info("Starting bot with long polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
