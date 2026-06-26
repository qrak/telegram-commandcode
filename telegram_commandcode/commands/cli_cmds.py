"""
CliCommands — CLI-subcommand forwarders and directory management.

Commands: /feedback, /learn-taste, /login, /logout, /mcp, /skills,
/taste, /add-dir.

All Lane A (local). No prompt forwarding — these map directly to
`cmd <subcommand>` calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

# CLI subcommand mapping: cc_slash → (subcommand_args, status_msg)
CLI_MAP: dict[str, tuple[list[str], str]] = {
    "/feedback": (["feedback"], "📝 Submitting feedback..."),
    "/learn-taste": (["learn-taste"], "🧠 Learning taste from repositories..."),
    "/login": (["login"], "🔑 Authenticating..."),
    "/logout": (["logout"], "👋 Logging out..."),
    "/mcp": (["mcp"], "🔌 Managing MCP servers..."),
    "/skills": (["skills"], "📦 Managing skills..."),
    "/taste": (["taste"], "🎨 Managing taste..."),
}


class CliCommands(BaseCommandHandler):
    """Handlers for: CLI-mapped commands + /add-dir."""

    COMMANDS = {
        "/feedback": "handle_cli_mapped",
        "/learn-taste": "handle_cli_mapped",
        "/login": "handle_cli_mapped",
        "/logout": "handle_cli_mapped",
        "/mcp": "handle_cli_mapped",
        "/skills": "handle_cli_mapped",
        "/taste": "handle_cli_mapped",
        "/add-dir": "handle_add_dir",
    }

    # ── CLI-mapped commands ────────────────────────────────────────────

    async def handle_cli_mapped(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        """Route /feedback, /login, /mcp, /taste etc. to `cmd <sub>`."""
        cc_slash = self._resolve_slash(update)
        if cc_slash not in CLI_MAP:
            return None

        cli_base, status_msg = CLI_MAP[cc_slash]

        # Special handling for /taste <name> → cmd taste pull <name>
        if cc_slash == "/taste" and args:
            cli_args = cli_base + ["pull"] + args.split()
        else:
            cli_args = cli_base + (args.split() if args else ["list"])

        await self.send_md(update, escape_md2(status_msg))
        await self.run_cli_and_reply(update, cli_args, timeout=30)
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
