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
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from telegram import Update

from telegram_commandcode.session import ChatSession, session_store
from telegram_commandcode.executor import DEFAULT_CMD_BIN
from telegram_commandcode.formatter import escape_code_block, strip_mdv2
from telegram_commandcode.chunking import (
    truncate_message,
    chunk_escaped,
    maybe_file_fallback,
)

if TYPE_CHECKING:
    pass

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
        if not chat or not text or not text.strip():
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
                finally:
                    with suppress(OSError):
                        fallback.file_path.unlink()

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
                    # Resending the escaped text would show the backslashes,
                    # so send the unescaped (plain) version instead.
                    await chat.send_message(
                        strip_mdv2(chunk),
                        link_preview_options={"is_disabled": True},
                    )
                else:
                    raise

    # ── CLI runner ──────────────────────────────────────────────────────

    async def run_cli_result(
        self, args: list[str], timeout: int = 30,
    ) -> tuple[str, bool]:
        """Run a Command Code subcommand and return ``(output, timed_out)``.

        The flag matters to callers that can do better than "timed out":
        several `cmd` subcommands (``login``, ``feedback``) expect a terminal
        or a browser, and simply wait forever when driven from here.
        """
        proc = None
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
            return out or err or f"(exit {proc.returncode})", False
        except asyncio.TimeoutError:
            # wait_for cancels the wait, not the child: kill it or it leaks.
            if proc is not None and proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(ProcessLookupError):
                    await proc.wait()
            return f"⚠️ Command timed out after {timeout}s.", True
        except FileNotFoundError:
            return f"❌ Command '{DEFAULT_CMD_BIN}' not found.", False
        except Exception as exc:
            return f"❌ {exc}", False

    async def run_cli(self, args: list[str], timeout: int = 30) -> str:
        """Run a Command Code CLI subcommand and return its output string."""
        output, _ = await self.run_cli_result(args, timeout=timeout)
        return output

    async def run_cli_and_reply(
        self, update: Update, args: list[str],
        timeout: int = 30, as_code_block: bool = True,
        tty_hint: Optional[str] = None,
    ) -> None:
        """Run a CLI command, send a 'typing' indicator, and reply.

        ``tty_hint`` replaces the raw timeout message for subcommands that
        need an interactive terminal (login, feedback, ...).
        """
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        output, timed_out = await self.run_cli_result(args, timeout=timeout)
        if timed_out and tty_hint:
            await self._send_chunked(update, tty_hint)
            return
        if as_code_block:
            capped = (
                output[:3800] + "\n...(truncated)"
                if len(output) > 3800
                else output
            )
            await self._send_chunked(update, f"```\n{escape_code_block(capped)}\n```")
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
