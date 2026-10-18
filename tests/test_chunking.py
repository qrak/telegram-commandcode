"""Unit tests for message chunking — the part that decides whether a long
agent answer is delivered as several messages instead of one rejected one."""

from telegram_commandcode.chunking import (
    TG_MAX_CHARS,
    chunk_escaped,
    truncate_message,
    utf16_len,
)


def test_short_message_is_one_chunk():
    assert truncate_message("hello") == ["hello"]


def test_utf16_len_counts_surrogate_pairs():
    assert utf16_len("abc") == 3
    assert utf16_len("😀") == 2
    assert utf16_len("😀😀") == 4


def test_long_message_splits_under_limit():
    text = ("word " * 1200).strip()
    chunks = truncate_message(text)
    assert len(chunks) > 1
    assert all(utf16_len(c) <= TG_MAX_CHARS for c in chunks)


def test_chunks_are_numbered():
    chunks = truncate_message("x " * 4000)
    assert chunks[0].endswith(f"(1/{len(chunks)})")
    assert chunks[-1].endswith(f"({len(chunks)}/{len(chunks)})")


def test_code_fence_is_balanced_in_every_chunk():
    body = "\n".join(f"line {i} of a long log output" for i in range(400))
    text = f"```js\n{body}\n```"
    chunks = truncate_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:80]


def test_chunk_escaped_escapes_indicator_parentheses():
    chunks = chunk_escaped(["hello (1/2)", "world (2/2)"])
    assert chunks[0].endswith(r"\(1/2\)")
    assert chunks[1].endswith(r"\(2/2\)")


def test_empty_string_yields_single_empty_chunk():
    assert truncate_message("") == [""]
