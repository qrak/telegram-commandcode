"""
MessageSender — resilient message send/edit/reaction primitives.

Owns the retry logic, MarkdownV2 fallback, chunking, and error
classification that were previously inline in gateway.py.
"""

from __future__ import annotations

import asyncio
import logging
from tempfile import gettempdir
from typing import TYPE_CHECKING, Optional

from telegram_commandcode.formatter import strip_mdv2, escape_md2
from telegram_commandcode.chunking import truncate_message, chunk_escaped, maybe_file_fallback, find_file_paths

if TYPE_CHECKING:
    from .gateway import BotGateway

logger = logging.getLogger(__name__)

# File fallback threshold
FILE_FALLBACK_THRESHOLD = 15_000


class MessageSender:
    """Resilient message sending with auto-retry and parse fallback.

    All send/edit operations go through this class's methods so that
    error boundaries, flood-control waits, and MarkdownV2→plain retries
    are applied uniformly across the entire bot.
    """

    def __init__(self, gateway: "BotGateway"):
        self.gw = gateway

    # ── Public API ─────────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        bot=None,
        parse_mode: Optional[str] = "MarkdownV2",
        disable_notification: bool = False,
        **kwargs,
    ):
        """Send a message with full retry/chunk/fallback pipeline."""
        return await _send_message_safe(
            chat_id, text, parse_mode=parse_mode,
            bot=bot, disable_notification=disable_notification,
            **kwargs,
        )

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        bot=None,
        parse_mode: Optional[str] = "MarkdownV2",
        finalize: bool = False,
    ) -> bool:
        """Edit a message with full retry/fallback pipeline."""
        return await _edit_message_safe(
            chat_id, message_id, text,
            parse_mode=parse_mode, bot=bot, finalize=finalize,
        )

    async def set_reaction(
        self, chat_id: int, message_id: int, emoji: str, *, bot=None,
    ) -> None:
        """Set a reaction emoji (best-effort, never raises)."""
        if not bot:
            return
        try:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
                is_big=False,
            )
        except Exception:
            pass

    @staticmethod
    def classify_error(error: Exception) -> str:
        """Classify a Telegram error for retry decisions.

        Returns: 'parse', 'too_long', 'flood', 'network', 'connect',
        'timeout', 'bad_request', 'unknown'.
        """
        return classify_telegram_error(error)


# ═══════════════════════════════════════════════════════════════════════════
# Standalone functions (accessible without a Gateway instance for
# backward compatibility with bot.py startup code if needed)
# ═══════════════════════════════════════════════════════════════════════════

async def _send_message_safe(
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    *,
    bot=None,
    disable_notification: bool = False,
    **kwargs,
):
    """Send a message with MarkdownV2, auto-fallback to plain text.

    Auto-splits long messages via truncate_message. On parse failure,
    retries as plain text. Handles flood control, network retry, and
    re-chunk on too_long.
    """
    if not bot or not text or not text.strip():
        return None

    link_preview = kwargs.pop("link_preview_options", {"is_disabled": True})
    if disable_notification:
        kwargs.setdefault("disable_notification", True)

    # File fallback
    if len(text) > FILE_FALLBACK_THRESHOLD:
        fallback = maybe_file_fallback(text)
        if fallback:
            try:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=fallback.preview_message,
                    link_preview_options={"is_disabled": True},
                    **kwargs,
                )
                with open(fallback.file_path, "rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=f"cc_output_{int(asyncio.get_event_loop().time())}.txt",
                        caption="📄 Full output (too long for chat)",
                    )
                return last_msg
            except Exception as e:
                logger.warning("File fallback failed: %s", e)

    # Chunk using Hermes-patterned truncate_message
    chunks = truncate_message(text)
    escaped_chunks = chunk_escaped(chunks)

    last_msg = None
    for chunk in escaped_chunks:
        for attempt in range(3):
            try:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                    link_preview_options=link_preview,
                    **kwargs,
                )
                break
            except Exception as e:
                err_kind = classify_telegram_error(e)

                if err_kind == "parse" and parse_mode:
                    plain_chunk = strip_mdv2(chunk)
                    last_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=plain_chunk,
                        parse_mode=None,
                        link_preview_options=link_preview,
                        **kwargs,
                    )
                    break

                if err_kind == "too_long":
                    tighter = truncate_message(chunk, max_length=3072)
                    tighter_escaped = chunk_escaped(tighter)
                    for sub in tighter_escaped:
                        last_msg = await bot.send_message(
                            chat_id=chat_id,
                            text=sub,
                            parse_mode=None,
                            link_preview_options=link_preview,
                            **kwargs,
                        )
                    break

                retry_after = getattr(e, "retry_after", None)
                if err_kind == "flood" or retry_after is not None:
                    wait = float(retry_after) if retry_after else 1.0
                    if attempt < 2 and wait <= 10.0:
                        logger.warning(
                            "Telegram flood control, waiting %.1fs (attempt %d/3)",
                            wait, attempt + 1,
                        )
                        await asyncio.sleep(wait)
                        continue
                    raise

                if err_kind in ("network", "connect"):
                    if attempt < 2:
                        wait = 2 ** attempt
                        logger.warning(
                            "Network error on send (attempt %d/3), retrying in %ds: %s",
                            attempt + 1, wait, e,
                        )
                        await asyncio.sleep(wait)
                        continue
                raise

    return last_msg


async def _edit_message_safe(
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    *,
    bot=None,
    finalize: bool = False,
) -> bool:
    """Edit a message with full retry/fallback pipeline.

    Returns True if the message was successfully edited.
    """
    if not bot or not text:
        return False

    for attempt in range(3):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                link_preview_options={"is_disabled": True},
            )
            return True
        except Exception as e:
            kind = classify_telegram_error(e)
            err_str = str(e).lower()

            # Parse failure → retry plain
            if kind == "parse" and parse_mode:
                plain = strip_mdv2(text)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=plain,
                        link_preview_options={"is_disabled": True},
                    )
                    return True
                except Exception:
                    pass
                return False

            # Too long → split or truncate
            if kind == "too_long":
                if finalize:
                    chunks = truncate_message(text, max_length=3500)
                    escaped = chunk_escaped(chunks)
                    for chunk in escaped:
                        await _send_message_safe(
                            chat_id, chunk, parse_mode=None, bot=bot,
                        )
                    return True
                if len(text) > 3500:
                    text = text[:3500] + "\n...(truncated)"
                    continue
                return False

            # Flood control
            retry_after = getattr(e, "retry_after", None)
            if kind == "flood" or retry_after is not None:
                wait = float(retry_after) if retry_after else 1.0
                if attempt < 2 and wait <= 5.0:
                    logger.warning(
                        "Edit flood control, waiting %.1fs (attempt %d/3)",
                        wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                return False

            # Can't be edited → send new
            if "message can't be edited" in err_str:
                await _send_message_safe(chat_id, text, parse_mode=None, bot=bot)
                return True

            # Not modified = ok
            if "not modified" in err_str:
                return True

            # Transient network
            if kind in ("network", "connect"):
                if attempt < 2:
                    wait = 2 ** attempt
                    logger.warning(
                        "Network error editing (attempt %d/3), retrying in %ds: %s",
                        attempt + 1, wait, e,
                    )
                    await asyncio.sleep(wait)
                    continue

            logger.debug("Edit failed for msg %d: %s", message_id, err_str)
            return False

    return False


def classify_telegram_error(error: Exception) -> str:
    """Classify a Telegram send/edit error for retry decisions."""
    err_str = str(error).lower()
    name = error.__class__.__name__.lower()

    if "parse" in err_str or "markdown" in err_str:
        return "parse"
    if "message_too_long" in err_str or "too long" in err_str:
        return "too_long"
    if "retry after" in err_str or hasattr(error, "retry_after"):
        return "flood"
    if "connect" in err_str or name == "connecterror":
        return "connect"
    if "network" in err_str or name == "networkerror":
        return "network"
    if "timed" in err_str or name == "timedout":
        return "timeout"
    if name == "badrequest" or "bad request" in err_str:
        return "bad_request"
    return "unknown"
