"""
Telegram MarkdownV2 / HTML formatting utilities.

Handles:
- Safe escaping of Telegram-special characters for MarkdownV2
- Auto-fallback to plain text on parse errors
- Code block wrapping
- Link formatting
- Hermes-style format_message: full markdown → MarkdownV2 pipeline
  (GFM tables → bullet groups, headers→bold, bold/italic/strikethrough conversion,
   placeholders for protected regions, safety-net escaping)
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


def strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escapes to produce clean plain text.

    Ported from Hermes Agent ``_strip_mdv2()``.
    Removes escape backslashes before special characters and strips MarkdownV2
    formatting markers (bold, italic, strikethrough, spoiler) so the fallback
    plain-text path doesn't show stray syntax characters.
    """
    # Remove escape backslashes before special characters
    cleaned = re.sub(r'\\([_*\[\]()~`>#\+\-=|{}.!\\])', r'\1', text)
    # Remove MarkdownV2 bold (*text*)
    cleaned = re.sub(r'\*([^*\n]+)\*', r'\1', cleaned)
    # Remove MarkdownV2 italic (_text_) — use word boundaries to avoid snake_case
    cleaned = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'\1', cleaned)
    # Remove MarkdownV2 strikethrough (~text~)
    cleaned = re.sub(r'~([^~\n]+)~', r'\1', cleaned)
    # Remove MarkdownV2 spoiler (||text||)
    cleaned = re.sub(r'\|\|([^|\n]+)\|\|', r'\1', cleaned)
    return cleaned


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
    """Attempt to determine if text is safe for MarkdownV2.
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


# ═══════════════════════════════════════════════════════════════════════════
# Hermes-style format_message — full markdown → MarkdownV2 conversion pipeline
# ═══════════════════════════════════════════════════════════════════════════

# Matches GFM table delimiter rows
_TABLE_SEPARATOR_RE = re.compile(
    r'^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$'
)


def _is_table_row(line: str) -> bool:
    """Return True if *line* could plausibly be a table data row."""
    stripped = line.strip()
    return bool(stripped) and '|' in stripped


def _split_table_row(line: str) -> list[str]:
    """Split a simple GFM table row into stripped cell values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_block_for_telegram(table_block: list[str]) -> str:
    """Render a detected GFM table as Telegram-friendly row groups.

    Ported from Hermes Agent ``_render_table_block_for_telegram()``.
    Telegram's MarkdownV2 has no table syntax — pipe characters render as
    backslash-pipe text. Converting each row into a bold heading plus bullet
    list keeps tables readable on mobile clients.
    """
    if len(table_block) < 3:
        return "\n".join(table_block)

    headers = _split_table_row(table_block[0])
    if len(headers) < 2:
        return "\n".join(table_block)

    first_data = _split_table_row(table_block[2]) if len(table_block) > 2 else []
    has_row_label = len(first_data) == len(headers) + 1

    rendered: list[str] = []
    for idx, row in enumerate(table_block[2:], start=1):
        cells = _split_table_row(row)
        if has_row_label:
            heading = cells[0] if cells and cells[0] else f"Row {idx}"
            data_cells = cells[1:]
        else:
            heading = next((c for c in cells if c), f"Row {idx}")
            data_cells = cells

        if len(data_cells) < len(headers):
            data_cells.extend([""] * (len(headers) - len(data_cells)))
        elif len(data_cells) > len(headers):
            data_cells = data_cells[:len(headers)]

        bullets: list[str] = []
        for header, value in zip(headers, data_cells):
            if not has_row_label and value == heading:
                continue
            bullets.append(f"• {header}: {value}")

        group = [f"**{heading}**", *bullets]
        rendered.append("\n".join(group))

    return "\n\n".join(rendered)


def _wrap_markdown_tables(text: str) -> str:
    """Rewrite GFM-style pipe tables into Telegram-friendly bullet groups.

    Ported from Hermes Agent ``_wrap_markdown_tables()``.
    Tables inside existing fenced code blocks are left alone.
    """
    if '|' not in text or '-' not in text:
        return text

    lines = text.split('\n')
    out: list[str] = []
    in_fence = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith('```'):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        if ('|' in line and i + 1 < len(lines)
                and _TABLE_SEPARATOR_RE.match(lines[i + 1])):
            table_block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_block.append(lines[j])
                j += 1
            out.append(_render_table_block_for_telegram(table_block))
            i = j
            continue

        out.append(line)
        i += 1

    return '\n'.join(out)


def format_message(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 format.

    Ported from Hermes Agent ``TelegramAdapter.format_message()``.

    Protected regions (code blocks, inline code) are extracted first so
    their contents are never modified.  Standard markdown constructs
    (headers, bold, italic, links) are translated to MarkdownV2 syntax,
    and all remaining special characters are escaped.

    The pipeline:
      0. Rewrite GFM pipe tables → Telegram-friendly bullet groups
      1. Protect fenced code blocks (``` ... ```) with placeholders
      2. Protect inline code (`...`) with placeholders
      3. Convert markdown links: [text](url) → MarkdownV2
      4. Convert headers: ## Title → *Title*
      5. Convert bold: **text** → *text*
      6. Convert italic: *text* → _text_
      7. Convert strikethrough: ~~text~~ → ~text~
      8. Convert spoiler: ||text|| → ||text|| (protect from | escaping)
      9. Convert blockquotes: > text
      10. Escape remaining special characters
      11. Restore placeholders
      12. Safety net: escape unescaped ( ) { } outside code spans
    """
    if not content:
        return content

    placeholders: dict = {}
    counter = [0]

    def _ph(value: str) -> str:
        key = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    text = content

    # 0) Rewrite GFM tables
    text = _wrap_markdown_tables(text)

    # 1) Protect fenced code blocks
    def _protect_fenced(m):
        raw = m.group(0)
        open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
        opening = raw[:open_end]
        body_and_close = raw[open_end:]
        body = body_and_close[:-3]
        body = body.replace('\\', '\\\\').replace('`', '\\`')
        return _ph(opening + body + '```')

    text = re.sub(
        r'(```(?:[^\n]*\n)?[\s\S]*?```)',
        _protect_fenced,
        text,
    )

    # 2) Protect inline code
    text = re.sub(
        r'(`[^`]+`)',
        lambda m: _ph(m.group(0).replace('\\', '\\\\')),
        text,
    )

    # 3) Convert markdown links
    def _convert_link(m):
        display = escape_md2(m.group(1))
        url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
        return _ph(f'[{display}]({url})')

    text = re.sub(
        r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)',
        _convert_link, text,
    )

    # 4) Convert headers: ### Title → *Title*
    def _convert_header(m):
        inner = m.group(1).strip()
        inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
        return _ph(f'*{escape_md2(inner)}*')

    text = re.sub(
        r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE,
    )

    # 5) Convert bold: **text** → *text*
    text = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _ph(f'*{escape_md2(m.group(1))}*'),
        text,
    )

    # 6) Convert italic: *text* → _text_ (avoid newlines to not break bullets)
    text = re.sub(
        r'\*([^*\n]+)\*',
        lambda m: _ph(f'_{escape_md2(m.group(1))}_'),
        text,
    )

    # 7) Convert strikethrough: ~~text~~ → ~text~
    text = re.sub(
        r'~~(.+?)~~',
        lambda m: _ph(f'~{escape_md2(m.group(1))}~'),
        text,
    )

    # 8) Convert spoiler: ||text|| → ||text|| (protect from | escaping)
    text = re.sub(
        r'\|\|(.+?)\|\|',
        lambda m: _ph(f'||{escape_md2(m.group(1))}||'),
        text,
    )

    # 9) Convert blockquotes
    def _convert_blockquote(m):
        prefix = m.group(1)
        content = m.group(2)
        if prefix.startswith('**') and content.endswith('||'):
            return _ph(f'{prefix} {escape_md2(content[:-2])}||')
        return _ph(f'{prefix} {escape_md2(content)}')

    text = re.sub(
        r'^((?:\*\*)?>{1,3}) (.+)$',
        _convert_blockquote, text, flags=re.MULTILINE,
    )

    # 10) Escape remaining special characters
    text = escape_md2(text)

    # 11) Restore placeholders (reverse order for nested resolution)
    for key in reversed(list(placeholders.keys())):
        text = text.replace(key, placeholders[key])

    # 12) Safety net: escape bare ( ) { } outside code spans
    _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
    _safe_parts = []
    for _idx, _seg in enumerate(_code_split):
        if _idx % 2 == 1:
            _safe_parts.append(_seg)
        else:
            def _esc_bare(m, _seg=_seg):
                s = m.start()
                ch = m.group(0)
                if s > 0 and _seg[s - 1] == '\\':
                    return ch
                if ch == '(' and s > 0 and _seg[s - 1] == ']':
                    return ch
                if ch == ')':
                    before = _seg[:s]
                    if '](http' in before or '](' in before:
                        depth = 0
                        for j in range(s - 1, max(s - 2000, -1), -1):
                            if _seg[j] == '(':
                                depth -= 1
                                if depth < 0:
                                    if j > 0 and _seg[j - 1] == ']':
                                        return ch
                                    break
                            elif _seg[j] == ')':
                                depth += 1
                return '\\' + ch
            _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
    text = ''.join(_safe_parts)

    return text
