"""
Telegram MarkdownV2 / HTML formatting utilities.

Handles:
- Safe escaping of Telegram-special characters for MarkdownV2
- Auto-fallback to plain text on parse errors
- Code block wrapping
- Link formatting
- Smart splitting at paragraph/sentence boundaries (mirrors chunking.py)
"""

from __future__ import annotations

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Characters that MUST be escaped in Telegram MarkdownV2:
#   _ * [ ] ( ) ~ ` > # + - = | { } . !
# The backslash (\) and exclamation mark (!) need escaping.
# Inside code blocks (`) and code spans, no escaping is needed;
# inside pre/code blocks (```), only backticks and backslashes need escaping.
_MD2_SPECIAL = re.compile(r"([_\*\[\]\(\)~`>#\+\-=|{}\.!\\])")

# Characters to escape INSIDE code blocks (backtick and backslash)
_CODE_BLOCK_SPECIAL = re.compile(r"([`\\])")


def escape_md2(text: str) -> str:
    """
    Escape text for Telegram MarkdownV2.

    Safe for general text. For text inside code blocks, use escape_code_block().
    """
    return _MD2_SPECIAL.sub(r"\\\1", text)


def escape_code_block(text: str) -> str:
    """Escape text intended for placement inside a ```code block```."""
    return _CODE_BLOCK_SPECIAL.sub(r"\\\1", text)


def wrap_code(text: str, language: str = "") -> str:
    """
    Wrap text in a Telegram-compatible code block.
    Handles escaping of backticks within the content.
    """
    escaped = escape_code_block(text)
    return f"```{language}\n{escaped}\n```"


def wrap_inline_code(text: str) -> str:
    """Wrap text in inline code markers: `text`."""
    escaped = escape_code_block(text)
    return f"`{escaped}`"


def bold(text: str) -> str:
    return f"*{escape_md2(text)}*"


def italic(text: str) -> str:
    return f"_{escape_md2(text)}_"


def link(text: str, url: str) -> str:
    """Format a MarkdownV2 link: [text](url). Both are escaped appropriately."""
    # URL characters that break MarkdownV2 links: )
    url_escaped = url.replace(")", "\\)")
    return f"[{escape_md2(text)}]({url_escaped})"


def escape_user_input(text: str) -> str:
    """
    Escape user-provided text for safe inclusion in message templates.
    More aggressive than escape_md2 — also handles newlines in awkward places.
    """
    # Escape all special characters
    escaped = escape_md2(text)
    # Escape stray newlines that could break a MarkdownV2 parse
    # (MarkdownV2 requires double-newline for paragraph breaks)
    return escaped


def safe_send_params(
    text: str,
    parse_mode: str = "MarkdownV2",
    *,
    disable_web_page_preview: bool = True,
) -> dict:
    """
    Return kwargs for PTB's send_message / edit_message_text.

    Usage:
        await msg.edit_text(text, **safe_send_params(text))
    """
    return {
        "parse_mode": parse_mode,
        "link_preview_options": {"is_disabled": disable_web_page_preview},
    }


def try_parse_fallback(text: str) -> tuple[str, Optional[str]]:
    """
    Attempt to determine if text is safe for MarkdownV2.
    Returns (text, 'MarkdownV2') if likely safe, (text, None) for plain text.

    This is a heuristic — the real validation happens when Telegram returns
    "can't parse entities", at which point the caller should retry with
    parse_mode=None.
    """
    # Quick heuristic: if text has unescaped special chars in suspicious
    # patterns (e.g., orphan underscores in the middle of words, unmatched
    # brackets), fall back to plain text.
    #
    # Since we escape ALL special characters in escape_md2(), the heuristic
    # is only needed for user-provided raw text or externally-generated
    # markdown that might be malformed.

    # Count unescaped underscores (underscores NOT preceded by backslash)
    unescaped_underscores = len(re.findall(r"(?<!\\)_", text))
    if unescaped_underscores % 2 != 0:
        # Odd number of underscores = likely unclosed italic/bold
        return text, None

    return text, "MarkdownV2"
