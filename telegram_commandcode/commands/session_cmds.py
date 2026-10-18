"""
SessionCommands — session lifecycle: clear, resume, undo, fork, compact,
rename, yolo, stop.  All Lane B (state engineering) — no direct prompt
forwarding except for the continuation prompt /resume produces.

The hard part is that Command Code's own rewind/compaction primitives are
interactive-only. Everything here is built on what headless mode does support:

- ``--continue`` / ``--resume <id|name>`` / ``--fork-session`` / ``-n <name>``
- one extra summarisation run that turns the current transcript into a
  handoff brief, which then seeds the new session exactly once.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.executor import ExecOptions, process_tracker, run_cmd
from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

COMPACT_PROMPT = (
    "Write a compact handoff brief for this conversation so a fresh session "
    "can continue the work without the full transcript. Include: the goal, "
    "what has been done so far, files touched, decisions made, what works, "
    "what is unfinished, and the exact next step. Do not call tools and do "
    "not continue the work. Output only the brief."
)

# A summarisation run needs the transcript but must not edit anything, so it
# runs with --yolo off and a small turn budget.
HANDOFF_TIMEOUT = 180
HANDOFF_MAX_TURNS = 2
MAX_UNDO = 20


def undo_prompt(dropped: list[str]) -> str:
    """Build the rewind summarisation prompt for /undo."""
    listed = "\n".join(f"- {prompt[:200]}" for prompt in dropped)
    return (
        f"The user is rewinding this conversation by {len(dropped)} turn(s). "
        "Write a compact handoff brief describing the state of the work as it "
        "was BEFORE these last user requests:\n"
        f"{listed}\n\n"
        "Include the goal, decisions, files touched, what was finished, what "
        "was not, and the next step from that earlier point. Do not call "
        "tools and do not continue the work. Output only the brief."
    )


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
            "goal/steer cleared\\. The next prompt starts a brand\\-new session\\.",
        )
        return None

    # ── /resume ────────────────────────────────────────────────────────

    async def handle_resume(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        target = args.strip()

        if target.lower() in ("list", "ls"):
            known = []
            if state.session_name:
                known.append(f"`{escape_md2(state.session_name)}` (current name)")
            if state.session_id:
                known.append(f"`{escape_md2(state.session_id)}` (current id)")
            listing = "\n".join(f"  • {item}" for item in known) or "  _(none yet)_"
            await self._send_chunked(
                update,
                f"📚 *Known sessions for this chat*\n{listing}\n\n"
                "Use `/resume <name>` or `/resume <id>` to attach this chat to "
                "one\\. `cmd \\-\\-resume` on the host lists every session\\.",
            )
            return None

        if target:
            self.update_state(chat_id, resume_target=target, active=True)
            await self.send_md(
                update,
                f"🔄 Attaching to session `{escape_md2(target)}`\\. "
                "Your next message continues that conversation\\.",
            )
            return (
                "Continue where we left off. Summarize what we were working on "
                "and ask what I'd like to do next."
            )

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

        if args and not args.isdigit():
            await self.send_md(
                update,
                "Usage: `/undo [N]` — rewind the last N turns \\(default 1\\)\\.",
            )
            return None

        n = max(1, min(int(args), MAX_UNDO)) if args else 1

        if not state.active:
            await self.send_md(
                update,
                "🤷 Nothing to undo — there is no active session\\. "
                "Send a prompt first\\.",
            )
            return None

        if not state.history:
            await self.send_md(
                update,
                "🤷 No prompt history recorded yet, so there is nothing to rewind\\.",
            )
            return None

        if process_tracker.is_running(chat_id):
            await self.send_md(
                update,
                "⏳ A prompt is still running\\. Wait for it, or `/stop` it first\\.",
            )
            return None

        n = min(n, len(state.history))
        dropped = list(state.history[-n:])
        remaining = list(state.history[:-n])

        await self._send_chunked(
            update,
            f"↩️ Rewinding {n} turn(s)\\. Building a brief of the earlier state\\.\\.\\.",
        )

        brief = await self._handoff(chat_id, state, undo_prompt(dropped))
        if not brief:
            await self.send_md(
                update,
                "❌ Could not build the rewind brief \\(Command Code returned no "
                "output\\)\\. Nothing was changed — your session is intact\\.",
            )
            return None

        self.reset_state(chat_id)
        self.update_state(
            chat_id,
            seed_context=brief,
            history=remaining,
            last_prompt=remaining[-1] if remaining else None,
        )

        preview = escape_md2(brief[:2500])
        listed = "\n".join(
            f"  {i + 1}\\. {escape_md2(p[:80])}" for i, p in enumerate(dropped)
        )
        await self._send_chunked(
            update,
            f"↩️ *Rewound {n} turn(s)*\n\n"
            f"Dropped:\n{listed}\n\n"
            f"The next prompt starts a fresh session seeded with this brief:\n\n"
            f"{preview}",
        )
        return None

    # ── /fork ──────────────────────────────────────────────────────────

    async def handle_fork(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        name = (args.strip() or f"fork-{datetime.now():%Y%m%d-%H%M%S}")[:64]

        if not state.active:
            await self.send_md(
                update,
                "🌿 There is no active session to fork\\. "
                "Send a prompt first — the original stays intact either way\\.",
            )
            return None

        self.update_state(chat_id, fork_next=name)
        previous = (
            f"`{escape_md2(state.session_name)}`"
            if state.session_name
            else "the current one"
        )
        await self._send_chunked(
            update,
            f"🌿 *Fork pending:* `{escape_md2(name)}`\n\n"
            f"Your next prompt runs with `\\-\\-continue \\-\\-fork\\-session`, so it "
            f"continues in a new session named `{escape_md2(name)}` and leaves "
            f"{previous} untouched\\.\n\n"
            f"Use `/resume <name>` to switch back at any time\\.",
        )
        return None

    # ── /compact ───────────────────────────────────────────────────────

    async def handle_compact(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not state.active:
            await self.send_md(
                update,
                "🗜️ Conversation is already compact — there is no active session\\.",
            )
            return None

        if process_tracker.is_running(chat_id):
            await self.send_md(
                update,
                "⏳ A prompt is still running\\. Wait for it, or `/stop` it first\\.",
            )
            return None

        await self._send_chunked(
            update,
            "🗜️ Compacting the conversation into a handoff brief\\.\\.\\.",
        )

        brief = await self._handoff(chat_id, state, COMPACT_PROMPT)
        if not brief:
            await self.send_md(
                update,
                "❌ Could not compact \\(Command Code returned no output\\)\\. "
                "Nothing was changed — your session is intact\\.",
            )
            return None

        self.reset_state(chat_id)
        self.update_state(chat_id, seed_context=brief)

        await self._send_chunked(
            update,
            "🗜️ *Compacted*\\. The next prompt starts a new session seeded with "
            f"this brief:\n\n{escape_md2(brief[:3000])}",
        )
        return None

    # ── /rename ────────────────────────────────────────────────────────

    async def handle_rename(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        name = args.strip()

        if not name:
            if state.session_name:
                await self.send_md(
                    update,
                    f"📝 Current session name: `{escape_md2(state.session_name)}`\n\n"
                    f"_Use `/rename <name>` to change it\\._",
                )
            else:
                await self.send_md(
                    update,
                    "📝 No session name set\\. Use `/rename <name>` to name this "
                    "session\\. Named sessions can be resumed with "
                    "`/resume <name>`\\.",
                )
            return None

        name = name[:64]
        self.update_state(chat_id, session_name=name)
        await self.send_md(
            update,
            f"📝 Session renamed to `{escape_md2(name)}`\\.\n\n"
            f"Future runs pass `\\-n {escape_md2(name)}` to Command Code, so "
            f"`/resume {escape_md2(name)}` finds it again\\.",
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
        chat_id = str(update.effective_chat.id)
        stopped_foreground = await process_tracker.kill(chat_id)
        stopped_background = await process_tracker.kill_background(chat_id)

        if stopped_foreground or stopped_background:
            parts = []
            if stopped_foreground:
                parts.append("the running prompt")
            if stopped_background:
                parts.append(
                    f"{stopped_background} background task(s)"
                )
            await self.send_md(
                update,
                f"🛑 Stopped {' and '.join(parts)}\\.",
            )
        else:
            await self.send_md(update, "🤷 No active execution to stop\\.")
        return None

    # ── Shared: summarisation run ──────────────────────────────────────

    async def _handoff(self, chat_id: str, state, prompt: str) -> Optional[str]:
        """Ask Command Code for a handoff brief of the current session.

        Runs with the transcript continued but with writes disabled. The chat
        id is passed so the run occupies the foreground slot: a prompt that
        starts meanwhile cancels this run instead of racing it on the same
        transcript, and ``/stop`` can cancel it too.
        """
        opts = ExecOptions(
            model=state.model,
            effort=state.effort,
            continue_session=True,
            compact_mode=state.compact_mode,
            yolo=False,
            max_turns=HANDOFF_MAX_TURNS,
            timeout=HANDOFF_TIMEOUT,
        )
        try:
            result = await run_cmd(prompt, opts, chat_id=chat_id)
        except Exception:
            logger.exception("Handoff run failed")
            return None

        if not result.success or not result.output:
            logger.warning(
                "Handoff run produced nothing (exit %s, subtype %s): %s",
                result.exit_code, result.subtype, result.stderr[:500],
            )
            return None
        return result.output.strip()
