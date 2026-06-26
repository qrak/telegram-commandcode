"""
BaseCommandHandler — shared state, formatting helpers, and CLI runner
used by all command handler subclasses.

Eliminates the previous module-level duplication of _send_chunked,
_run_cli, _read_cc_config, etc. across 970 lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from telegram import Update

from telegram_commandcode.session import ChatSession, session_store
from telegram_commandcode.executor import DEFAULT_CMD_BIN
from telegram_commandcode.formatter import escape_md2
from telegram_commandcode.chunking import (
    truncate_message,
    chunk_escaped,
    maybe_file_fallback,
)

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# File fallback threshold for chunked sends
FILE_FALLBACK_THRESHOLD = 15_000


class BaseCommandHandler:
    """Abstract base for all slash-command handler modules.

    Provides the shared machinery every command handler needs:
    - Session state access (via chat_id)
    - MarkdownV2-safe message sending (chunked, auto-fallback)
    - CLI subprocess runner for `cmd <subcommand>` calls
    - Command Code config file read/write

    Subclasses register themselves with a `COMMANDS` mapping from slash name
    to method, e.g.::

        class SessionCommands(BaseCommandHandler):
            COMMANDS = {
                "/clear": "handle_clear",
                "/resume": "handle_resume",
            }

            async def handle_clear(
                self, update: Update, context, args: str, state: ChatSession
            ) -> Optional[str]:
                ...
    """

    COMMANDS: dict[str, str] = {}  # /cmd → method_name, set by subclasses

    # ── Formatting helpers ──────────────────────────────────────────────

    async def send_md(self, update: Update, text: str) -> None:
        """Send MarkdownV2-formatted text via the chunked path."""
        await self._send_chunked(update, text)

    async def _send_chunked(
        self, update: Update, text: str, parse_mode: str = "MarkdownV2",
    ) -> None:
        """Send text, splitting into code-block-aware chunks with fallback."""
        chat = update.effective_chat
        if not chat:
            return

        # File fallback for very long outputs
        if len(text) > FILE_FALLBACK_THRESHOLD:
            fallback = maybe_file_fallback(text)
            if fallback:
                try:
                    await chat.send_message(
                        fallback.preview_message,
                        link_preview_options={"is_disabled": True},
                    )
                    with open(fallback.file_path, "rb") as f:
                        await chat.send_document(
                            document=f,
                            filename=f"cc_output_{int(datetime.now().timestamp())}.txt",
                            caption="📄 Full output",
                        )
                    return
                except Exception as exc:
                    logger.warning("File fallback failed: %s", exc)

        chunks = truncate_message(text)
        escaped = chunk_escaped(chunks)

        for chunk in escaped:
            try:
                await chat.send_message(
                    chunk,
                    parse_mode=parse_mode,
                    link_preview_options={"is_disabled": True},
                )
            except Exception as exc:
                if "can't parse entities" in str(exc).lower() and parse_mode:
                    await chat.send_message(
                        chunk,
                        link_preview_options={"is_disabled": True},
                    )
                else:
                    raise

    # ── CLI runner ──────────────────────────────────────────────────────

    async def run_cli(self, args: list[str], timeout: int = 30) -> str:
        """Run a Command Code CLI subcommand and return its output string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                DEFAULT_CMD_BIN, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            return out or err or f"(exit {proc.returncode})"
        except asyncio.TimeoutError:
            return "⚠️ Command timed out."
        except FileNotFoundError:
            return f"❌ Command '{DEFAULT_CMD_BIN}' not found."
        except Exception as exc:
            return f"❌ {exc}"

    async def run_cli_and_reply(
        self, update: Update, args: list[str],
        timeout: int = 30, wrap_code: bool = True,
    ) -> None:
        """Run a CLI command, send 'typing' indicator, and reply.

        This DRY pattern replaces 15+ repeated blocks of:
            await chat.send_chat_action("typing")
            output = await _run_cli(...)
            capped = output[:3800] + ... if len(output) > 3800 else output
            await _send_chunked(update, f"```\n{escape_md2(capped)}\n```")
        """
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        output = await self.run_cli(args, timeout=timeout)
        if wrap_code:
            capped = (
                output[:3800] + "\n...(truncated)"
                if len(output) > 3800
                else output
            )
            await self._send_chunked(update, f"```\n{escape_md2(capped)}\n```")
        else:
            await self._send_chunked(update, output)

    # ── Command Code config ─────────────────────────────────────────────

    @staticmethod
    def cc_config_path() -> Path:
        return Path.home() / ".commandcode" / "config.json"

    @classmethod
    def read_cc_config(cls) -> dict:
        try:
            return json.loads(cls.cc_config_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @classmethod
    def write_cc_config(cls, cfg: dict) -> None:
        cls.cc_config_path().parent.mkdir(parents=True, exist_ok=True)
        cls.cc_config_path().write_text(json.dumps(cfg, indent=2))

    # ── Session helpers ─────────────────────────────────────────────────

    @staticmethod
    def get_state(chat_id: str) -> ChatSession:
        return session_store.get(chat_id)

    @staticmethod
    def update_state(chat_id: str, **kwargs: Any) -> ChatSession:
        return session_store.update(chat_id, **kwargs)

    @staticmethod
    def reset_state(chat_id: str) -> None:
        session_store.reset(chat_id)
