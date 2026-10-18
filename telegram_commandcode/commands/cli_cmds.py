"""
CliCommands — CLI-subcommand forwarders and directory management.

Commands: /feedback, /learn-taste, /login, /logout, /mcp, /skills,
/taste, /add-dir.

All Lane A (local). No prompt forwarding — these map directly to
`cmd <subcommand>` calls.

Some of these subcommands expect a terminal or a browser (login opens one),
so they answer with an instruction instead of a hanging subprocess.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_code_block, escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# cc_slash → (subcommand argv, status message, timeout seconds)
CLI_MAP: dict[str, tuple[list[str], str, int]] = {
    "/feedback": (["feedback"], "📝 Sending feedback...", 20),
    "/learn-taste": (["learn-taste"], "🧠 Learning taste from repositories...", 180),
    "/mcp": (["mcp", "list"], "🔌 Listing MCP servers...", 30),
    "/skills": (["skills", "list"], "📦 Listing skills...", 30),
    "/taste": (["taste", "list"], "🎨 Listing taste profiles...", 30),
}

LOGIN_HINT = (
    "🔑 *Login happens on the host machine*\\.\n\n"
    "`cmd login` opens a browser — or wants an API key pasted into the "
    "terminal — and the bot has no terminal to give it\\.\n\n"
    "*Option 1* — run this on the host:\n"
    "```\ncmd login\n```\n"
    "*Option 2* — give the bot process an API key:\n"
    "```\nCOMMAND_CODE_API_KEY=<your key>\n```\n"
    "and restart the bot\\.\n\n"
    "_`/status` shows whether the CLI is authenticated\\._"
)

LOGOUT_HINT = (
    "👋 *Logging out signs the host out too*\\.\n\n"
    "`cmd logout` removes the stored credentials that every Command Code "
    "process on this machine shares, including the bot's\\.\n\n"
    "Run `cmd login` again \\(on the host\\) to restore access\\."
)


class CliCommands(BaseCommandHandler):
    """Handlers for: CLI-mapped commands + /add-dir."""

    COMMANDS = {
        "/feedback": "handle_cli_mapped",
        "/learn-taste": "handle_cli_mapped",
        "/login": "handle_login",
        "/logout": "handle_logout",
        "/mcp": "handle_cli_mapped",
        "/skills": "handle_cli_mapped",
        "/taste": "handle_cli_mapped",
        "/add-dir": "handle_add_dir",
    }

    # ── CLI-mapped commands ────────────────────────────────────────────

    async def handle_cli_mapped(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        """Route /feedback, /mcp, /taste etc. to `cmd <sub> ...`."""
        cc_slash = self._resolve_slash(update)
        if cc_slash not in CLI_MAP:
            return None

        argv, status_msg, timeout = CLI_MAP[cc_slash]
        argv = list(argv)

        if args:
            if cc_slash == "/taste":
                # `/taste <name>` pulls a named profile
                argv = ["taste", "pull", *args.split()]
            elif cc_slash == "/feedback":
                # `cmd feedback "message"` takes the text as one argument
                argv = ["feedback", args]
            else:
                # Drop the default `list` subcommand and use the arguments.
                base = argv[:-1] if argv[-1] == "list" else argv
                argv = base + args.split()

        await self._send_chunked(update, escape_md2(status_msg))
        await self.run_cli_and_reply(
            update, argv, timeout=timeout,
            tty_hint=(
                f"⚠️ `cmd {escape_md2(' '.join(argv))}` needs an interactive "
                "terminal\\. Run it on the host instead\\."
            ),
        )
        return None

    @staticmethod
    def _resolve_slash(update: Update) -> str:
        """Extract the command-name part from the message text."""
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        raw_cmd = parts[0].lower()
        # Use the same TG_TO_CC resolution as the router
        from .router import TG_TO_CC
        return TG_TO_CC.get(raw_cmd.lstrip("/"), raw_cmd)

    # ── /login ─────────────────────────────────────────────────────────

    async def handle_login(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        status, _ = await self.run_cli_result(["status", "--json"], timeout=15)
        detail = ""
        if status and not status.startswith(("❌", "⚠️")):
            preview = status[:600]
            detail = (
                "\n\n*Current `cmd status --json`:*\n"
                f"```\n{escape_code_block(preview)}\n```"
            )
        await self._send_chunked(update, LOGIN_HINT + detail)
        return None

    # ── /logout ────────────────────────────────────────────────────────

    async def handle_logout(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        await self._send_chunked(update, LOGOUT_HINT)
        await self.run_cli_and_reply(update, ["logout"], timeout=30)
        return None

    # ── /add-dir ───────────────────────────────────────────────────────

    async def handle_add_dir(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            if not state.add_dirs:
                await self.send_md(
                    update,
                    "📂 No directories added yet\\.\n\n"
                    "Use `/add\\-dir <path>` to add a directory to the workspace context\\.\n"
                    "Use `/add\\-dir clear` to remove all\\.",
                )
            else:
                dirs = "\n".join(
                    f"  {i + 1}\\. `{escape_md2(d)}`"
                    for i, d in enumerate(state.add_dirs)
                )
                await self.send_md(
                    update,
                    f"📂 *Added directories:*\n{dirs}\n\n"
                    "Use `/add\\-dir clear` to remove all, or add more with `/add\\-dir <path>`\\.",
                )
            return None

        if args.lower() == "clear":
            self.update_state(chat_id, add_dirs=[])
            await self.send_md(update, "📂 All added directories cleared\\.")
            return None

        new_dirs = list(state.add_dirs) + [args]
        self.update_state(chat_id, add_dirs=new_dirs)
        await self.send_md(
            update,
            f"📂 Added directory: `{escape_md2(args)}`\n"
            f"Total: {len(new_dirs)}\\. Use `/add\\-dir clear` to remove all\\.",
        )
        return None
