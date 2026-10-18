"""Unit tests for MarkdownV2 escaping.

A single unescaped character makes Telegram reject the whole message, so this
is the highest-value thing in the formatter to pin down with tests.
"""

from telegram_commandcode.formatter import escape_md2, strip_mdv2

SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def test_every_special_character_is_escaped():
    for char in SPECIALS:
        escaped = escape_md2(char)
        assert escaped == "\\" + char, (char, escaped)


def test_plain_text_is_unchanged():
    assert escape_md2("hello world 123") == "hello world 123"


def test_markdown_syntax_is_neutralised():
    assert escape_md2("*bold*") == r"\*bold\*"
    assert escape_md2("a.b") == r"a\.b"


def test_backslash_itself_is_escaped():
    assert escape_md2("\\") == "\\\\"


def test_escaping_twice_is_not_idempotent():
    # Callers must escape raw values exactly once; escaping an escaped string
    # adds another backslash before every character. This pins that contract.
    once = escape_md2("a.b")
    twice = escape_md2(once)
    assert once == r"a\.b"
    assert twice != once


def test_strip_mdv2_round_trips_basic_text():
    raw = "deploy.passed (v1.2.3)!"
    assert strip_mdv2(escape_md2(raw)) == raw
