"""
Resilient message chunking for Telegram's 4096-char limit.

Ported from Hermes Agent's ``truncate_message()`` (base.py):
- Code-block-aware: never splits inside ```fenced``` or `inline` code
- UTF-16 measurement: Telegram counts surrogate pairs as 2 units
- Smart split points: prefers newlines, then spaces, avoids inline-code breaks
- Chunk indicators: (1/3) suffixes with MarkdownV2-safe escaping
- File fallback: very long outputs (>15K chars) sent as .txt document

Also includes file-path detection (MEDIA: prefix + bare absolute paths) for
auto-attachment.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# ── Length measurement ─────────────────────────────────────────────────────

def utf16_len(s: str) -> int:
    """Count UTF-16 code units in *s*.

    Telegram's message-length limit (4,096) is measured in **UTF-16 code units**,
    **not** Python ``len()`` (Unicode code-points).  Characters outside the Basic
    Multilingual Plane (emoji like 😀, CJK Extension B, musical symbols, …) are
    encoded as surrogate pairs and therefore consume **two** UTF-16 code units
    each, even though ``len()`` counts them as one.

    Ported from Hermes Agent ``utf16_len()``.
    """
    return len(s.encode("utf-16-le")) // 2


def _custom_unit_to_cp(s: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Return the largest codepoint offset *n* such that ``len_fn(s[:n]) <= budget``.

    Used by ``truncate_message`` when *len_fn* measures length in units
    different from Python codepoints (e.g. UTF-16 code units).  Falls back to
    binary search which is O(log n) calls to *len_fn*.
    """
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ── Chunking ───────────────────────────────────────────────────────────────

# Telegram max message length (UTF-16 code units)
TG_MAX_CHARS = 4096

# File fallback threshold — above this many total chars, send as document
FILE_FALLBACK_THRESHOLD = 15_000

# Room reserved for " (XX/XX)" indicator and up-to-3-digit numbers
INDICATOR_RESERVE = 10

# Matches a chunk indicator on a code-fence line (e.g. ``` \(1/2\))
_CHUNK_INDICATOR_ON_FENCE_RE = re.compile(
    r'(?m)^``` (?P<indicator>(?:\\)?\(\d+/\d+(?:\\)?\))$'
)


def _separate_chunk_indicator_from_fence(text: str) -> str:
    """Move ``(N/M)`` chunk markers off Telegram code-fence lines.

    ``truncate_message()`` appends chunk indicators to the end of a chunk. When
    the chunk had to close an in-progress fenced code block, that creates a
    line like `` ``` \(1/2\)`` after MarkdownV2 escaping. Telegram does not
    treat that as a clean closing fence, so it can reject MarkdownV2 and fall
    back to plain text. Put the indicator on its own line immediately after the
    closing fence.
    """
    return _CHUNK_INDICATOR_ON_FENCE_RE.sub(r'```\n\g<indicator>', text)


def truncate_message(
    content: str,
    max_length: int = TG_MAX_CHARS,
    *,
    len_fn: Callable[[str], int] = utf16_len,
) -> List[str]:
    """Split a long message into Telegram-safe chunks.

    Code-block-aware: when a split falls inside a triple-backtick code block,
    the fence is closed at the end of the current chunk and reopened (with the
    original language tag) at the start of the next chunk.  Inline code spans
    (`` `...` ``) are never split — the split point is moved left.

    Multi-chunk responses receive indicators like ``(1/3)`` with
    MarkdownV2-safe parentheses: ``\(1/3\)``.

    Args:
        content: The full message content (already MarkdownV2-formatted).
        max_length: Maximum length per chunk (in *len_fn* units).
        len_fn: Length function — defaults to ``utf16_len`` for Telegram.

    Returns:
        List of message chunks, each ≤ max_length in len_fn units.
    """
    _len = len_fn
    if _len(content) <= max_length:
        return [content]

    FENCE_CLOSE = "\n```"
    chunks: List[str] = []
    remaining = content
    carry_lang: Optional[str] = None

    while remaining:
        # Continue an open code block from the previous chunk
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""

        # How much body text fits after accounting for prefix, closing fence,
        # and chunk indicator.
        headroom = max_length - INDICATOR_RESERVE - _len(prefix) - _len(FENCE_CLOSE)
        if headroom < 1:
            headroom = max_length // 2

        # Everything remaining fits in one final chunk
        if _len(prefix) + _len(remaining) <= max_length - INDICATOR_RESERVE:
            chunks.append(prefix + remaining)
            break

        # Map the custom-unit headroom to a codepoint slice position
        if _len is not len:
            cp_limit = _custom_unit_to_cp(remaining, headroom, _len)
        else:
            cp_limit = headroom
        region = remaining[:cp_limit]

        # Prefer splitting at a newline
        split_at = region.rfind("\n")
        if split_at < cp_limit // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = cp_limit

        # Avoid splitting inside an inline code span (`...`).
        # If the candidate has an odd number of unescaped backticks,
        # the split falls inside inline code — move the split point left.
        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe_split = candidate.rfind(" ", 0, last_bt)
                nl_split = candidate.rfind("\n", 0, last_bt)
                safe_split = max(safe_split, nl_split)
                if safe_split > cp_limit // 4:
                    split_at = safe_split

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body

        # Track code-block state through the chunk body
        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""

        if in_code:
            full_chunk += FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None

        chunks.append(full_chunk)

    # Append chunk indicators: (1/3), (2/3), ...
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)
        ]

    return chunks


def chunk_escaped(chunks: List[str]) -> List[str]:
    """Escape MarkdownV2 parentheses in chunk indicators and fix fence lines."""
    result = []
    for chunk in chunks:
        # Escape the (N/M) indicator: replace " (1/2)" with " \(1/2\)"
        chunk = re.sub(r" \((\d+)/(\d+)\)$", r" \(\1/\2\)", chunk)
        # Move indicators off fence lines
        chunk = _separate_chunk_indicator_from_fence(chunk)
        result.append(chunk)
    return result


# ── File fallback ───────────────────────────────────────────────────────────

@dataclass
class FileFallbackResult:
    """Result when output is written to a file instead of chunking."""
    preview_message: str
    file_path: Path
    should_send_as_file: bool = True


def maybe_file_fallback(text: str, threshold: int = FILE_FALLBACK_THRESHOLD) -> Optional[FileFallbackResult]:
    """If *text* exceeds *threshold*, write to a temp file for document delivery."""
    if len(text) <= threshold:
        return None

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="cc_output_")
    os.close(fd)

    filepath = Path(path)
    filepath.write_text(text, encoding="utf-8")

    preview = text[:200]
    note = (
        f"📄 Output is {len(text):,} characters — sent as file.\n\n"
        f"Preview:\n```\n{preview}...\n```"
    )

    logger.info("File fallback: wrote %d chars to %s", len(text), filepath)
    return FileFallbackResult(preview_message=note, file_path=filepath)


# ── File path detection (MEDIA: prefix + bare absolute paths) ──────────────

MEDIA_RE = re.compile(r"MEDIA:(/[^\s\"')\]>]{3,})")
BARE_PATH_RE = re.compile(
    r"((?:/home|/tmp|/var|/usr|/etc|/opt|/mnt|/media|/run|/srv)"
    r"/[^\s\"')\]>]{3,})"
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}


def find_file_paths(text: str) -> list[dict]:
    """Scan text for file paths to auto-attach.

    Checks both ``MEDIA:`` prefix and bare absolute paths.
    Returns list of ``{path, type}`` where type is ``'photo'`` or ``'document'``.
    """
    seen = set()
    results = []

    for match in MEDIA_RE.finditer(text):
        path_str = match.group(1).rstrip(".,;:!?)")
        if path_str in seen:
            continue
        seen.add(path_str)
        p = Path(path_str)
        if p.exists():
            ext = p.suffix.lower()
            results.append({
                "path": str(p),
                "type": "photo" if ext in IMAGE_EXTENSIONS else "document",
            })

    # Fallback: bare paths (only if not already found via MEDIA:)
    for match in BARE_PATH_RE.finditer(text):
        path_str = match.group(1).rstrip(".,;:!?)")
        if path_str in seen:
            continue
        seen.add(path_str)
        p = Path(path_str)
        if p.exists():
            ext = p.suffix.lower()
            results.append({
                "path": str(p),
                "type": "photo" if ext in IMAGE_EXTENSIONS else "document",
            })

    return results[:5]  # max 5 auto-attachments
