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

import logging
import os
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
    ("background", "Run a detached task: /background <prompt>"),
    ("clear", "Clear conversation history (fresh start)"),
    ("cmd", "Run a raw prompt or shell command: /cmd <text>"),
    ("compact", "Summarize context into a handoff brief"),
    ("compact_mode", "Auto-compact strategy: /compact-mode <default|fast>"),
    ("configure_models", "Show model, effort and compact-mode setup"),
    ("context", "Show session state and token usage"),
    ("courses", "Open Command Code courses in browser"),
    ("effort", "Set reasoning effort: /effort <low|medium|high|xhigh|max>"),
    ("feedback", "Send feedback: /feedback <text>"),
    ("fork", "Fork the next prompt into a new session: /fork [name]"),
    ("goal", "Set objective: /goal <text|clear|status>"),
    ("help", "Show available commands"),
    ("info", "Show system information"),
    ("init", "Initialize AGENTS.md for this project"),
    ("learn_taste", "Learn taste from other agents"),
    ("login", "Show how to authenticate the host CLI"),
    ("logout", "Remove stored authentication"),
    ("mcp", "List MCP server connections"),
    ("memory", "Manage memory: /memory or /memory <instruction>"),
    ("model", "List models or switch: /model <name>"),
    ("new", "Start a new conversation"),
    ("plan", "Enter plan mode or plan a task"),
    ("pr_comments", "Fetch PR comments for current branch"),
    ("provider", "Set auth provider: /provider <name>"),
    ("queue", "Queue prompt for next turn: /queue <prompt>"),
    ("reason", "Set reasoning level: /reason <level>"),
    ("reasoning", "Set reasoning effort (alias for /effort)"),
    ("reload", "Restart bot and resume session"),
    ("rename", "Name the session: /rename <name>"),
    ("resume", "Resume a session: /resume [name|id]"),
    ("retry", "Re-run the last prompt"),
    ("review", "Review a pull request: /review or /review <pr>"),
    ("session_file", "Show the current session id"),
    ("skills", "List installed agent skills"),
    ("start", "Start the bot"),
    ("status", "Show environment status (model, session, config)"),
    ("steer", "Give mid-session guidance: /steer <instruction>"),
    ("stop", "Stop the running prompt and background tasks"),
    ("taste", "Manage Taste learning"),
    ("undo", "Rewind the last N turns: /undo [N]"),
    ("update", "Update Command Code (/update check to only check)"),
    ("usage", "Show tokens used by this chat"),
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

    allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
    if not allowed:
        logger.warning(
            "   Access: LOCKED — no TELEGRAM_ALLOWED_USERS set. "
            "Send /start to the bot to learn your numeric id."
        )
    elif "any" in {part.strip() for part in allowed.split(",")}:
        logger.warning("   Access: OPEN — anyone who finds this bot can drive the agent.")
    else:
        logger.info("   Access: %s", allowed)

    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("   %d commands registered", len(BOT_COMMANDS))
    except Exception as e:
        logger.warning("   ⚠️ Failed to register commands: %s", e)

    logger.info("   Listening... (Ctrl+C to stop)")


async def _on_shutdown(app: Application) -> None:
    """Graceful shutdown — await pending tasks, kill all running processes."""
    logger.info("Shutting down gracefully...")

    # Wait for in-flight prompt executions to finish
    await _gateway.processor.wait_pending(timeout=10.0)

    from .executor import process_tracker
    await process_tracker.kill_all()
    logger.info("Goodbye.")


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point for `telegram-commandcode` console script."""
    # Windows consoles default to a legacy code page; log lines and error
    # messages contain emoji, which would otherwise print as \uXXXX escapes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

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
