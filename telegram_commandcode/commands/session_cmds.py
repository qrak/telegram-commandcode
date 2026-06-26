"""
SessionCommands — session lifecycle: clear, resume, undo, fork, compact,
rename, yolo, stop.  All Lane B (state engineering) — no LLM forwarding.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


class SessionCommands(BaseCommandHandler):
    """Handlers for: /clear, /new, /resume, /undo, /fork, /compact,
    /rename, /yolo, /stop."""

    COMMANDS = {
        "/clear": "handle_clear",
        "/new": "handle_clear",
        "/resume": "handle_resume",
        "/undo": "handle_undo",
        "/fork": "handle_fork",
        "/compact": "handle_compact",
        "/rename": "handle_rename",
        "/yolo": "handle_yolo",
        "/stop": "handle_stop",
    }

    # ── /clear, /new ───────────────────────────────────────────────────

    async def handle_clear(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        self.reset_state(chat_id)
        await self.send_md(
            update,
            "🧹 Session cleared\\. Model reset to default, plan mode off, "
            "goal/steer cleared\\. Next prompt starts fresh\\.",
        )
        return None

    # ── /resume ────────────────────────────────────────────────────────

    async def handle_resume(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        self.update_state(chat_id, active=True)
        await self.send_md(
            update,
            "🔄 Session resumed\\. Sending a continuation prompt\\.\\.\\.",
        )
        return (
            "Continue where we left off. Summarize what we were working on "
            "and ask what I'd like to do next."
        )

    # ── /undo ──────────────────────────────────────────────────────────

    async def handle_undo(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not state.last_prompt:
            await self.send_md(update, "🤷 No previous prompt to undo\\.")
            return None

        n = int(args) if (args and args.isdigit()) else 1
        self.update_state(chat_id, active=False, one_shot_plan=False)
        await self.send_md(
            update,
            f"↩️ Undoing last {n} turn(s)\\. Re\\-running your prompt with a fresh session state\\.\\.\\.",
        )
        return state.last_prompt

    # ── /fork ──────────────────────────────────────────────────────────

    async def handle_fork(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        name = args or f"fork_{int(datetime.now().timestamp())}"
        self.update_state(chat_id, active=False, session_name=name)
        await self.send_md(
            update,
            f"🌿 Session forked as *{escape_md2(name)}*\\.\n\n"
            "Session state has been reset\\. Your next prompt starts a fresh conversation\\.",
        )
        return None

    # ── /compact ───────────────────────────────────────────────────────

    async def handle_compact(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        self.reset_state(chat_id)
        await self._send_chunked(
            update,
            "🗜️ Session compacted\\. All state reset \\(model, plan mode, steer, goal\\)\\. "
            "Next prompt starts fresh\\.",
        )
        return None

    # ── /rename ────────────────────────────────────────────────────────

    async def handle_rename(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            if state.session_name:
                await self.send_md(
                    update,
                    f"📝 Current session name: {escape_md2(state.session_name)}",
                )
            else:
                await self.send_md(
                    update,
                    "📝 No session name set\\. Use `/rename <name>` to name this session\\.",
                )
            return None

        self.update_state(chat_id, session_name=args)
        await self.send_md(
            update,
            f"📝 Session renamed to: *{escape_md2(args)}*",
        )
        return None

    # ── /yolo ──────────────────────────────────────────────────────────

    async def handle_yolo(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        new_mode = not state.yolo
        self.update_state(chat_id, yolo=new_mode)

        status = "ON ✅" if new_mode else "OFF ❌"
        msg = (
            f"⚡ YOLO mode: *{status}*\n\n"
            + (
                "Next prompts will run with `\\-\\-yolo`\\. Use `/yolo` again to disable\\."
                if new_mode
                else "Next prompts will run without `\\-\\-yolo`\\. Use `/yolo` again to enable\\."
            )
        )
        await self._send_chunked(update, msg)
        return None

    # ── /stop ──────────────────────────────────────────────────────────

    async def handle_stop(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        from telegram_commandcode.executor import process_tracker

        chat_id = str(update.effective_chat.id)
        killed = await process_tracker.kill(chat_id)
        if killed:
            await self.send_md(update, "🛑 Execution stopped by user\\.")
        else:
            await self.send_md(update, "🤷 No active execution to stop\\.")
        return None
