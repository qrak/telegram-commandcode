"""
InfoCommands — status, help, version, usage, and context display commands.

All purely local (Lane A) — no LLM execution, no prompt forwarding.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.session import ChatSession
from telegram_commandcode.executor import DEFAULT_CMD_BIN, DEFAULT_MAX_TURNS
from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = __import__("logging").getLogger(__name__)


class InfoCommands(BaseCommandHandler):
    """Handlers for: /help, /start, /status, /whoami, /context, /info,
    /version, /usage, /update, /agents, /courses, /reload."""

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
    }

    # ── /help ──────────────────────────────────────────────────────────

    async def handle_help(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        help_text = (
            "🤖 *Command Code Bot — Help*\n\n"
            "*Session & State:*\n"
            "  `/context` — Show session state, model, working dir\n"
            "  `/status` — Environment overview \\+ auth status\n"
            "  `/whoami` — Your user ID and chat info\n"
            "  `/clear` — Reset session \\(fresh start\\)\n"
            "  `/compact` — Compact \\= reset session state\n"
            "  `/reload` — Restart the bot\n\n"
            "*Model & Config:*\n"
            "  `/model` — List models or switch: `/model <name>`\n"
            "  `/configure\\-models` — Show current model \\+ per\\-task overrides\n"
            "  `/provider` — Switch AI provider: `/provider <name>`\n"
            "  `/effort` — Set reasoning effort: `/effort <low|medium|high|xhigh|max>`\n\n"
            "*Workflow:*\n"
            "  `/goal <text>` — Set a standing objective\n"
            "  `/steer <text>` — Mid\\-session guidance\n"
            "  `/plan` — Toggle plan mode; `/plan <task>` for one\\-shot\n"
            "  `/yolo` — Toggle YOLO mode \\(auto\\-approve\\)\n"
            "  `/undo` — Undo last turn \\+ re\\-run prompt\n"
            "  `/retry` — Re\\-run last prompt\n"
            "  `/fork` — Name \\+ reset session\n"
            "  `/resume` — Continue a previous session\n"
            "  `/stop` — Kill running execution\n"
            "  `/background <p>` — Run a task in background\n"
            "  `/queue <p>` — Queue prompt for next turn\n\n"
            "*Files & Memory:*\n"
            "  `/add\\-dir <path>` — Add a directory to workspace\n"
            "  `/init` — Create or update AGENTS\\.md\n"
            "  `/memory` — List or modify memory files\n"
            "  `/rename` — Name the current session\n\n"
            "*GitHub & PRs:*\n"
            "  `/review` — Review a PR: `/review <# or url>`\n"
            "  `/pr\\-comments` — Fetch PR comments\n\n"
            "*CLI Commands:*\n"
            "  `/feedback` — Submit feedback\n"
            "  `/login` / `/logout` — Auth management\n"
            "  `/mcp` — MCP server management\n"
            "  `/skills` — Browse agent skills\n"
            "  `/taste` — List / install taste packages\n"
            "  `/update` — Update Command Code\n"
            "  `/version` — Show version\n"
            "  `/usage` — Show credits \\+ metrics\n"
            "  `/info` — System information\n"
            "  `/cmd <prompt>` — Run a one\\-shot prompt\n\n"
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
            "  `/goal <text>` \\- set a standing objective\n"
            "  `/steer <text>` \\- mid\\-session guidance\n"
            "  `/model <name>` \\- switch model\n"
            "  `/effort <level>` \\- set reasoning effort\n"
            "  `/background <p>` \\- run in background\n"
            "  `/queue <p>` \\- queue for next turn\n"
            "  `/status` \\- show environment\n"
            "  `/resume` \\- resume last session"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /status ────────────────────────────────────────────────────────

    async def handle_status(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        chat = update.effective_chat

        if chat:
            await chat.send_chat_action(action="typing")

        try:
            whoami = await self.run_cli(["whoami"], timeout=10)
            version = await self.run_cli(["--version"], timeout=10)
        except Exception:
            whoami = "unknown"
            version = "?"

        model_name = state.model
        if not model_name:
            try:
                models_out = await self.run_cli(["--list-models"], timeout=15)
                m = None
                for line in models_out.splitlines():
                    if "(default)" in line:
                        m = line.split()[0] if line.split() else None
                        break
                model_name = m or "unknown"
            except Exception:
                model_name = "unknown"

        session_info = (
            "Session: active (`/resume` to continue, `/clear` to reset)"
            if state.active
            else "Session: none (send any prompt to start)"
        )

        msg = (
            f"╔══ *Command Code* ══\n"
            f"╟ Model: `{escape_md2(model_name)}`{' (default)' if not state.model else ''}\n"
            f"╟ Binary: `{escape_md2(DEFAULT_CMD_BIN)}` v{escape_md2(version)}\n"
            f"╟ Auth: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ {escape_md2(session_info)}\n"
            f"╟ Plan: {'ON' if state.plan_mode else 'off'} · YOLO: {'on' if state.yolo else 'off'} · Turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ Goal: {escape_md2(state.goal[:60]) if state.goal else 'No goal set'}\n"
            f"╟ Steer: {escape_md2(state.steer[:60]) if state.steer else 'No steer set'}\n"
            f"╚══ Use `/model` to switch, `/goal` to set objective, `/steer` to guide, `/clear` to reset"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /whoami ────────────────────────────────────────────────────────

    async def handle_whoami(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        from telegram_commandcode.executor import process_tracker

        chat_id = str(update.effective_chat.id)
        user_id = update.effective_user.id if update.effective_user else None
        username = (
            update.effective_user.username
            or update.effective_user.first_name
            or "unknown"
        )
        chat_type = update.effective_chat.type if update.effective_chat else "?"
        pid = "active" if process_tracker.get(chat_id) else "idle"

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
            f"  Session: {'active' if state.active else 'none'}",
            f"  Working dir: `{escape_md2(cwd)}`",
            f"  Home: `{escape_md2(home)}`",
            f"  Plan mode: {'ON' if state.plan_mode else 'off'}",
            f"  Max turns: {DEFAULT_MAX_TURNS}",
            f"  YOLO: {'on' if state.yolo else 'off'}",
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
        lines.append("")
        lines.append(
            "_Context window specifics are managed server\u2010side by the API._"
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

        try:
            whoami = await self.run_cli(["whoami"], timeout=10)
            version = await self.run_cli(["--version"], timeout=10)
        except Exception:
            whoami = "unknown"
            version = "?"

        model_name = state.model or "default"
        await self.send_md(
            update,
            f"╔══ *Usage & Credits* ══\n"
            f"╟ User: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ Version: v{escape_md2(version)}\n"
            f"╟ Model: `{escape_md2(model_name)}`\n"
            f"╟ Max turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ YOLO: {'on' if state.yolo else 'off'}\n"
            f"╚══ _Detailed usage metrics require the TUI\\. Run `cmd` locally for full breakdown\\._",
        )
        return None

    # ── /update ────────────────────────────────────────────────────────

    async def handle_update(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        await self.send_md(update, "⬆️ Updating Command Code...")
        await self.run_cli_and_reply(update, ["update"], timeout=120)
        return None

    # ── /agents ────────────────────────────────────────────────────────

    async def handle_agents(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        agents_dir = __import__("pathlib").Path.home() / ".commandcode" / "agents"
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
            "🔄 Restarting bot... Session state will be preserved in config\\.",
        )
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
