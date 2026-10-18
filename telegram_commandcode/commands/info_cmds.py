"""
InfoCommands — status, help, version, usage, and context display commands.

All purely local (Lane A) — no LLM execution, no prompt forwarding.
The numbers shown here come from the last real run's ``--output-format json``
result (session id, token usage, duration), not from guesses.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from telegram import Update

from telegram_commandcode.executor import (
    DEFAULT_CMD_BIN,
    DEFAULT_MAX_TURNS,
    process_tracker,
)
from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

logger = logging.getLogger(__name__)

_TOKEN_KEYS = (
    ("input_tokens", "in"),
    ("output_tokens", "out"),
    ("cache_read_input_tokens", "cache read"),
    ("cache_creation_input_tokens", "cache write"),
    ("total_tokens", "total"),
)


def format_tokens(usage: dict) -> str:
    """Human-readable token counts from a usage dict (best effort)."""
    if not usage:
        return "—"
    parts = [
        f"{label} {int(value):,}"
        for key, label in _TOKEN_KEYS
        if isinstance(value := usage.get(key), (int, float))
        and not isinstance(value, bool)
    ]
    if parts:
        return " · ".join(parts)
    # Unknown shape: show the keys rather than guessing at semantics.
    return ", ".join(f"{key}={value}" for key, value in list(usage.items())[:6])


def format_duration(ms) -> str:
    """Milliseconds → '8.4s' / '1m 12s'."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest}s"


class InfoCommands(BaseCommandHandler):
    """Handlers for: /help, /start, /status, /whoami, /context, /info,
    /version, /usage, /update, /agents, /courses, /reload, /session-file."""

    COMMANDS = {
        "/help": "handle_help",
        "/start": "handle_start",
        "/status": "handle_status",
        "/whoami": "handle_whoami",
        "/context": "handle_context",
        "/info": "handle_info",
        "/version": "handle_version",
        "/usage": "handle_usage",
        "/update": "handle_update",
        "/agents": "handle_agents",
        "/courses": "handle_courses",
        "/reload": "handle_reload",
        "/session-file": "handle_session_file",
    }

    # ── /help ──────────────────────────────────────────────────────────

    async def handle_help(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        help_text = (
            "🤖 *Command Code Bot — Help*\n\n"
            "*Session:*\n"
            "  `/resume` — Continue the last session\n"
            "  `/resume <name|id>` — Attach to a named session\n"
            "  `/rename <name>` — Name the current session\n"
            "  `/fork [name]` — Fork the next prompt into a new session\n"
            "  `/compact` — Summarize into a handoff brief \\(new session\\)\n"
            "  `/undo [N]` — Rewind the last N turns\n"
            "  `/clear` — Fresh session, no brief\n"
            "  `/session-file` — Show the Command Code session id\n"
            "  `/stop` — Kill the running prompt \\+ background tasks\n\n"
            "*Model & Config:*\n"
            "  `/model [name]` — List or switch models \\(`\\-m`\\)\n"
            "  `/effort <level>` — Reasoning effort \\(`\\-\\-effort`\\)\n"
            "  `/compact\\-mode <default|fast>` — Auto\\-compaction strategy\n"
            "  `/provider <name>` — Auth provider \\(config\\.json\\)\n"
            "  `/configure\\-models` — Current model setup\n\n"
            "*Workflow:*\n"
            "  `/goal <text>`, `/steer <text>` — Standing instructions\n"
            "  `/plan` — Plan mode; `/plan <task>` for one\\-shot\n"
            "  `/yolo` — Toggle permission bypass\n"
            "  `/queue <prompt>` — Queue a prompt for the next turn\n"
            "  `/background <prompt>` — Detached task, not in this session\n"
            "  `/retry` — Re\\-run the last prompt\n\n"
            "*Files & Memory:*\n"
            "  `/add\\-dir <path>`, `/init`, `/memory`, `/review`, `/pr\\-comments`\n\n"
            "*Environment:*\n"
            "  `/status`, `/context`, `/usage`, `/info`, `/version`, `/whoami`\n"
            "  `/login` — How to authenticate the host CLI\n"
            "  `/feedback <text>`, `/mcp`, `/skills`, `/taste`, `/learn\\-taste`\n"
            "  `/update` — Update Command Code \\(`/update check` to only check\\)\n\n"
            "_Any other message is sent to the AI as a prompt\\._"
        )
        await self._send_chunked(update, help_text)
        return None

    # ── /start ─────────────────────────────────────────────────────────

    async def handle_start(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        msg = (
            "🤖 *Command Code Bot*\n\n"
            "I connect Telegram to your Command Code CLI\\. "
            "All CC commands are available \\(type `/` to see them\\)\\.\n\n"
            "*Send any prompt* and I'll run `cmd \\-p` \\(headless mode\\)\\.\n\n"
            "_Key commands:_\n"
            "  `/goal <text>` \\- standing objective\n"
            "  `/steer <text>` \\- mid\\-session guidance\n"
            "  `/model <name>` \\- switch model\n"
            "  `/effort <level>` \\- reasoning effort\n"
            "  `/compact` \\- shrink context, keep the thread\n"
            "  `/undo [N]` \\- rewind the last N turns\n"
            "  `/fork` \\- branch off into a new session\n"
            "  `/resume <name>` \\- come back to it later\n"
            "  `/status` \\- environment overview"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /status ────────────────────────────────────────────────────────

    async def handle_status(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        cfg = self.read_cc_config()
        chat = update.effective_chat

        if chat:
            await chat.send_chat_action(action="typing")

        whoami = await self.run_cli(["whoami"], timeout=10)
        version = await self.run_cli(["--version"], timeout=10)

        model_name = state.model or cfg.get("model") or "default"
        provider = cfg.get("provider", "command-code")
        session_info = (
            "active (`/resume` to continue, `/clear` to reset)"
            if state.active
            else "none (send any prompt to start)"
        )
        running = "running" if process_tracker.is_running(chat_id) else "idle"
        bg = process_tracker.background_count(chat_id)

        msg = (
            f"╔══ *Command Code* ══\n"
            f"╟ Model: `{escape_md2(str(model_name))}`"
            f"{'' if state.model else ' (config default)'}\n"
            f"╟ Effort: `{escape_md2(state.effort or 'model default')}`\n"
            f"╟ Provider: `{escape_md2(str(provider))}`\n"
            f"╟ Binary: `{escape_md2(DEFAULT_CMD_BIN)}` v{escape_md2(version)}\n"
            f"╟ Auth: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ Session: {escape_md2(session_info)}\n"
            f"╟ Name: `{escape_md2(state.session_name or '—')}`\n"
            f"╟ Plan: {'ON' if state.plan_mode else 'off'} · "
            f"YOLO: {'on' if state.yolo else 'off'} · Turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ Compact: `{escape_md2(state.compact_mode or 'default')}` · "
            f"Process: {running} · Background: {bg}\n"
            f"╚══ `/model` `/effort` `/compact` `/undo` `/help`"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /whoami ────────────────────────────────────────────────────────

    async def handle_whoami(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        user_id = update.effective_user.id if update.effective_user else None
        username = (
            update.effective_user.username
            or update.effective_user.first_name
            or "unknown"
        )
        chat_type = update.effective_chat.type if update.effective_chat else "?"
        pid = "active" if process_tracker.is_running(chat_id) else "idle"

        await self.send_md(
            update,
            f"*User info*\n"
            f"  ID: `{user_id or 'unknown'}`\n"
            f"  Username: @{escape_md2(str(username))}\n"
            f"  Platform: Telegram\n"
            f"  Chat type: {escape_md2(str(chat_type))}\n"
            f"  PID: {pid}",
        )
        return None

    # ── /context ───────────────────────────────────────────────────────

    async def handle_context(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        cwd = os.getcwd()
        home = os.path.expanduser("~")

        lines = [
            "📊 *Session Context*",
            "",
            f"  Model: `{escape_md2(state.model or 'default')}`",
            f"  Effort: `{escape_md2(state.effort or 'model default')}`",
            f"  Session: {'active' if state.active else 'none'}",
            f"  Name: `{escape_md2(state.session_name or '—')}`",
            f"  Session id: `{escape_md2(state.session_id or '—')}`",
            f"  Working dir: `{escape_md2(cwd)}`",
            f"  Home: `{escape_md2(home)}`",
            f"  Plan mode: {'ON' if state.plan_mode else 'off'}",
            f"  Max turns: {DEFAULT_MAX_TURNS}",
            f"  YOLO: {'on' if state.yolo else 'off'}",
            f"  Compact mode: `{escape_md2(state.compact_mode or 'default')}`",
        ]
        if state.goal:
            lines.append(f"  Goal: {escape_md2(state.goal[:80])}")
        if state.steer:
            lines.append(f"  Steer: {escape_md2(state.steer[:80])}")
        if state.add_dirs:
            dirs = ", ".join(
                f"`{escape_md2(d)}`" for d in state.add_dirs[:3]
            )
            lines.append(f"  Added dirs: {dirs}")
        if state.seed_context:
            lines.append(
                f"  Pending brief: {len(state.seed_context):,} chars "
                "\\(injected with the next prompt\\)"
            )
        lines.append("")
        lines.append(
            f"  Last run: {format_tokens(state.last_usage)} · "
            f"{format_duration(state.last_duration_ms)}"
        )
        lines.append(f"  Total this chat: {format_tokens(state.total_usage)}")
        lines.append("")
        lines.append(
            "_`/context` in the TUI shows the per-section window breakdown; "
            "it needs the interactive client\\._"
        )
        await self._send_chunked(update, "\n".join(lines))
        return None

    # ── /info ──────────────────────────────────────────────────────────

    async def handle_info(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        await self.run_cli_and_reply(update, ["info"], timeout=15)
        return None

    # ── /version ───────────────────────────────────────────────────────

    async def handle_version(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        ver = await self.run_cli(["--version"], timeout=10)
        await self.send_md(update, f"📦 *Command Code* v{escape_md2(ver)}")
        return None

    # ── /usage ─────────────────────────────────────────────────────────

    async def handle_usage(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")

        whoami = await self.run_cli(["whoami"], timeout=10)
        version = await self.run_cli(["--version"], timeout=10)

        model_name = state.model or "default"
        await self._send_chunked(
            update,
            f"╔══ *Usage & Credits* ══\n"
            f"╟ User: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ Version: v{escape_md2(version)}\n"
            f"╟ Model: `{escape_md2(str(model_name))}`\n"
            f"╟ Max turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ Last run: {format_tokens(state.last_usage)} · "
            f"{format_duration(state.last_duration_ms)}\n"
            f"╟ Total this chat: {format_tokens(state.total_usage)}\n"
            f"╟ Session: `{escape_md2(state.session_id or '—')}`\n"
            f"╚══ _Credits and plan come from the TUI's `/usage`; token counts "
            "above are reported by the CLI for each run\\._",
        )
        return None

    # ── /session-file ──────────────────────────────────────────────────

    async def handle_session_file(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not state.session_id and not state.session_name:
            await self.send_md(
                update,
                "📄 *No session yet\\.*\n\n"
                "Send a prompt first — its id is captured from the CLI and "
                "shown here\\.",
            )
            return None

        await self.send_md(
            update,
            "📄 *Current session*\n\n"
            f"  id: `{escape_md2(state.session_id or '—')}`\n"
            f"  name: `{escape_md2(state.session_name or '—')}`\n"
            f"  resumed with: `/resume {escape_md2(state.session_name or state.session_id or '')}`\n\n"
            "_`cmd \\-\\-resume <id>` on the host opens the same transcript in "
            "the TUI\\._",
        )
        return None

    # ── /update ────────────────────────────────────────────────────────

    async def handle_update(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        if args.strip().lower() in ("check", "--check-only", "check-only"):
            await self.run_cli_and_reply(
                update, ["update", "--check-only"], timeout=60,
            )
            return None

        await self.send_md(update, "⬆️ Updating Command Code...")
        await self.run_cli_and_reply(update, ["update"], timeout=180)
        return None

    # ── /agents ────────────────────────────────────────────────────────

    async def handle_agents(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        from pathlib import Path

        agents_dir = Path.home() / ".commandcode" / "agents"
        if agents_dir.exists():
            await self.send_md(
                update,
                f"🤖 Agent configs stored at `{escape_md2(str(agents_dir))}`\\.\n\n"
                "Interactive agent management \\(TUI\\) is not available remotely\\. "
                "Describe what you want and I can help set it up via prompt\\.",
            )
        else:
            await self.send_md(
                update,
                "🤖 No agent configurations found\\.\n\n"
                "Interactive agent management \\(TUI\\) is not available remotely\\. "
                "Describe what you want and I can help set it up via prompt\\.",
            )
        return None

    # ── /courses ───────────────────────────────────────────────────────

    async def handle_courses(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        await self.send_md(
            update,
            "📚 *Command Code Courses*\n\n"
            "[Open courses in browser](https://commandcode\\.ai/courses)\n\n"
            "_Learn how to get the most out of Command Code\\._",
        )
        return None

    # ── /reload ────────────────────────────────────────────────────────

    async def handle_reload(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        await self.send_md(
            update,
            "🔄 Restarting bot\\. Session state is preserved\\.\n\n"
            "_A supervisor \\(systemd, Docker restart policy\\) is required to bring it back up\\._",
        )
        # Exit hard (so a supervisor restarts us) but take the agent processes
        # with us — os._exit() skips shutdown hooks, so kill them first.
        await process_tracker.kill_all()
        os._exit(0)

    # ── Fallback handlers used by CommandRouter ────────────────────────

    async def _handle_tui_only(
        self, update: Update, cc_slash: str,
    ) -> Optional[str]:
        await self.send_md(
            update,
            f"ℹ️ {escape_md2(cc_slash)} is a TUI\\-only command \\(interactive terminal mode\\) "
            "and cannot be executed remotely\\. Use it in a local `cmd` session\\.",
        )
        return None

    async def _handle_na(
        self, update: Update, cc_slash: str,
    ) -> Optional[str]:
        await self.send_md(
            update,
            f"ℹ️ {escape_md2(cc_slash)} is not applicable when using Command Code remotely via Telegram\\.",
        )
        return None

    async def _handle_unknown(
        self, update: Update, cc_slash: str, args: str,
    ) -> Optional[str]:
        if not args:
            await self.send_md(
                update,
                f"Unknown command: `{escape_md2(cc_slash)}`\\. Use /help to see available commands\\.",
            )
            return None
        return args  # Unknown command with args → treat as prompt
