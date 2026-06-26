"""
Command router — dispatches Telegram slash commands to handler methods.

Archtecture:
  - Each command category (session, config, prompt, info, CLI) is a
    subclass of BaseCommandHandler that registers COMMANDS = {/cmd: method}.
  - CommandRouter.__init__ instantiates all handlers and builds a flat
    dispatch table by introspecting every handler's COMMANDS dict.
  - handle_command() looks up cc_slash in the dispatch table and calls
    the bound method directly — no if/elif chain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import Update

from .base import BaseCommandHandler
from .session_cmds import SessionCommands
from .config_cmds import ConfigCommands
from .prompt_cmds import PromptCommands
from .info_cmds import InfoCommands
from .cli_cmds import CliCommands

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Map Telegram-safe names (no hyphens) to real CC slash commands
TG_TO_CC: dict[str, str] = {
    "add_dir": "/add-dir",
    "compact_mode": "/compact-mode",
    "compactmode": "/compact-mode",
    "configure_models": "/configure-models",
    "configuremodels": "/configure-models",
    "learn_taste": "/learn-taste",
    "learntaste": "/learn-taste",
    "pr_comments": "/pr-comments",
    "prcomments": "/pr-comments",
    "terminal_setup": "/terminal-setup",
    "terminalsetup": "/terminal-setup",
}

# TUI-only commands (cannot be executed remotely)
TUI_ONLY: set[str] = {"/ide", "/terminal-setup", "/rewind"}

# N/A remotely commands
NA_CMDS: set[str] = {"/exit", "/share", "/unshare"}

# Valid effort levels
VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class CommandRouter:
    """Unified slash-command dispatcher.

    Each handler module is instantiated once and its COMMANDS dict
    is merged into a flat `_dispatch` table.  handle_command() does
    a single dictionary lookup — no sprawling if/elif chain.
    """

    def __init__(self):
        self.handlers: list[BaseCommandHandler] = [
            InfoCommands(),
            SessionCommands(),
            ConfigCommands(),
            PromptCommands(),
            CliCommands(),
        ]
        self._dispatch: dict[str, str] = {}
        for h in self.handlers:
            if h.COMMANDS:
                self._dispatch.update(h.COMMANDS)
        logger.info("CommandRouter: %d slash commands registered", len(self._dispatch))

    async def handle_command(
        self,
        update: Update,
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> Optional[str]:
        """Route a slash command to its handler method.

        Returns:
            None  → command was fully handled (response sent, no further action)
            str   → use this string as the prompt for `cmd -p` execution
        """
        if not update.message or not update.message.text:
            return None

        text = update.message.text.strip()
        if not text.startswith("/"):
            return None

        chat_id = str(update.effective_chat.id)
        parts = text.split(maxsplit=1)
        raw_cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Convert TG-safe names to CC slash commands
        cc_slash = TG_TO_CC.get(raw_cmd.lstrip("/"), raw_cmd)

        # Handle TUI-only and N/A commands upfront
        if cc_slash in TUI_ONLY:
            handler = self.handlers[0]  # InfoCommands, always first
            return await handler._handle_tui_only(update, cc_slash)

        if cc_slash in NA_CMDS:
            handler = self.handlers[0]
            return await handler._handle_na(update, cc_slash)

        # Look up the handler method name
        method_name = self._dispatch.get(cc_slash)
        if method_name is None:
            # Unknown command — fall through to InfoCommands for help message
            handler = self.handlers[0]
            return await handler._handle_unknown(update, cc_slash, args)

        # Resolve which handler owns this method
        for handler in self.handlers:
            method = getattr(handler, method_name, None)
            if method is not None:
                return await method(update, context, args)

        # Should never happen if dispatch table is consistent
        logger.error("Dispatch %s → %s but no handler owns it", cc_slash, method_name)
        return None
