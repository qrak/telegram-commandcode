"""
Resilient message chunking for Telegram's 4096-character limit.

SmartSplitter tries to break at paragraph boundaries, then sentence boundaries,
then word boundaries — only falling back to character-level splits as a last
resort.

Also handles file fallback: when output exceeds a configurable threshold,
writes content to a .txt file and returns a send_document instruction.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Telegram max message length is 4096. We use 4000 to leave headroom for
# headers like "(1/5)\n" and for MarkdownV2 escaping overhead.
TG_MAX_CHARS = 4096
DEFAULT_CHUNK_SIZE = 4000

# File fallback threshold — above this many total characters, send as document
# instead of splintering into dozens of messages.
FILE_FALLBACK_THRESHOLD = 15_000  # ~4 chunks max in chat

# Sentence boundary regex (handles . ! ? followed by space + capital or newline)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])")

# Paragraph boundary (double newline)
PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass
class ChunkResult:
    """Result of splitting a long text."""
    chunks: list[str] = field(default_factory=list)
    total_chars: int = 0
    num_chunks: int = 0
    should_send_as_file: bool = False
    file_path: Optional[Path] = None


class SmartSplitter:
    """
    Splits text into Telegram-safe chunks (≤ max_chars each).

    Strategy (in priority order):
    1. Under limit → return as single chunk
    2. Split at paragraph boundaries (double newline)
    3. Split at sentence boundaries (.!? followed by space + capital)
    4. Split at newline boundaries
    5. Split at word boundaries (spaces)
    6. Character-level split (last resort — tries to break at non-word chars)

    Each chunk is prefixed with "(N/M)\n" to indicate multi-part messages.
    """

    def __init__(
        self,
        max_chars: int = DEFAULT_CHUNK_SIZE,
        file_fallback_threshold: int = FILE_FALLBACK_THRESHOLD,
    ):
        self.max_chars = max_chars
        self.file_fallback_threshold = file_fallback_threshold

    def split(self, text: str, *, header_prefix: bool = True) -> ChunkResult:
        """
        Split text into Telegram-safe chunks.

        Args:
            text: The text to split.
            header_prefix: If True, prepend "(N/M)\n" to each chunk.

        Returns:
            ChunkResult with chunks list and metadata.
        """
        total = len(text)

        # Under single-chunk limit
        if total <= self.max_chars:
            return ChunkResult(
                chunks=[text],
                total_chars=total,
                num_chunks=1,
            )

        # File fallback for extremely long outputs
        if total > self.file_fallback_threshold:
            return self._file_fallback(text)

        # Split progressively
        chunks = self._split_progressive(text)
        num = len(chunks)

        # Add headers
        if header_prefix and num > 1:
            chunks = [f"({i + 1}/{num})\n{chunk}" for i, chunk in enumerate(chunks)]

        return ChunkResult(
            chunks=chunks,
            total_chars=total,
            num_chunks=num,
        )

    def _split_progressive(self, text: str) -> list[str]:
        """Apply splitting strategies in priority order."""
        # Strategy 2: paragraph boundaries
        result = self._split_by_regex(text, PARAGRAPH_RE)
        if self._all_fit(result):
            return result

        # Strategy 3: sentence boundaries
        result = self._split_by_regex(text, SENTENCE_RE)
        if self._all_fit(result):
            return result

        # Strategy 4: newline boundaries
        result = self._split_by_char(text, "\n")
        if self._all_fit(result):
            return result

        # Strategy 5: word boundaries (space)
        result = self._split_by_char(text, " ")
        if self._all_fit(result):
            return result

        # Strategy 6: character-level — try to break at punctuation/non-word
        return self._brute_force_split(text)

    def _all_fit(self, chunks: list[str]) -> bool:
        """Check if all chunks are within the limit."""
        return all(len(c) <= self.max_chars for c in chunks)

    def _split_by_regex(self, text: str, pattern: re.Pattern) -> list[str]:
        """Split text at regex boundaries, merging chunks to fit max_chars."""
        parts = pattern.split(text)
        return self._merge_chunks(parts)

    def _split_by_char(self, text: str, delimiter: str) -> list[str]:
        """Split text at delimiter, merging chunks to fit max_chars."""
        parts = text.split(delimiter)
        # Re-add delimiter to all but the last part
        merged = []
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                merged.append(part + delimiter)
            else:
                merged.append(part)
        return self._merge_chunks(merged)

    def _merge_chunks(self, parts: list[str]) -> list[str]:
        """Greedily merge parts into chunks ≤ max_chars."""
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for part in parts:
            part_len = len(part)

            # If a single part exceeds the limit, force-split it
            if part_len > self.max_chars:
                # Flush current chunk first
                if current:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                # Force-split the oversized part
                chunks.extend(self._brute_force_split(part))
                continue

            if current_len + part_len > self.max_chars:
                chunks.append("".join(current))
                current = [part]
                current_len = part_len
            else:
                current.append(part)
                current_len += part_len

        if current:
            chunks.append("".join(current))

        return chunks if chunks else [""]

    def _brute_force_split(self, text: str) -> list[str]:
        """Last resort: character-level split at chunk boundaries."""
        chunks = []
        start = 0
        text_len = len(text)

        while start < text_len:
            end = start + self.max_chars
            if end >= text_len:
                chunks.append(text[start:])
                break

            # Try to break at a nice boundary (non-word character)
            slice_end = end
            for scan in range(end, max(start, end - 100), -1):
                if not text[scan - 1].isalnum() and text[scan - 1] not in "'\"":
                    slice_end = scan
                    break

            chunks.append(text[start:slice_end])
            start = slice_end

        return chunks

    def _file_fallback(self, text: str) -> ChunkResult:
        """Write text to a temp file for send_document delivery."""
        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=".txt", prefix="cc_output_")
        os.close(fd)

        filepath = Path(path)
        filepath.write_text(text, encoding="utf-8")

        logger.info("File fallback: wrote %d chars to %s", len(text), filepath)

        # Return a single chunk with a note + the file path
        preview = text[:200]
        note = (
            f"📄 Output is {len(text):,} characters — sent as file.\n\n"
            f"Preview:\n```\n{preview}...\n```"
        )

        return ChunkResult(
            chunks=[note],
            total_chars=len(text),
            num_chunks=1,
            should_send_as_file=True,
            file_path=filepath,
        )


# ---------------------------------------------------------------------------
# File path detection (MEDIA: prefix + bare absolute paths)
# ---------------------------------------------------------------------------

MEDIA_RE = re.compile(r"MEDIA:(/[^\s\"')\]>]{3,})")
BARE_PATH_RE = re.compile(r"((?:/home|/tmp|/var|/usr|/etc|/opt|/mnt|/media|/run|/srv)/[^\s\"')\]>]{3,})")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}


def find_file_paths(text: str) -> list[dict]:
    """
    Scan text for file paths to auto-attach.
    Checks both MEDIA: prefix and bare absolute paths.
    Returns list of {path, type} where type is 'photo' or 'document'.
    """
    from pathlib import Path as _Path
    seen = set()
    results = []

    for match in MEDIA_RE.finditer(text):
        path_str = match.group(1).rstrip(".,;:!?)")
        if path_str in seen:
            continue
        seen.add(path_str)
        p = _Path(path_str)
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
        p = _Path(path_str)
        if p.exists():
            ext = p.suffix.lower()
            results.append({
                "path": str(p),
                "type": "photo" if ext in IMAGE_EXTENSIONS else "document",
            })

    return results[:5]  # max 5 auto-attachments
