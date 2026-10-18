"""Unit tests for MarkdownV2 escaping.

A single unescaped character makes Telegram reject the whole message, so this
is the highest-value thing in the formatter to pin down with tests.
"""

from telegram_commandcode.formatter import escape_md2, format_message, strip_mdv2

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


# ── format_message: model markdown → Telegram MarkdownV2 ───────────────


def test_bold_is_converted_to_markdownv2_bold():
    assert format_message("**done**") == "*done*"


def test_headers_become_bold():
    assert format_message("## Plan") == "*Plan*"


def test_plain_parentheses_are_escaped():
    assert format_message("deploy (v2)") == r"deploy \(v2\)"


def test_code_fence_body_is_never_escaped():
    out = format_message("```python\nx = (1)\n```")
    assert "x = (1)" in out
    assert r"\(" not in out


def test_markdown_link_is_kept_as_a_link():
    out = format_message("[docs](https://example.com/a)")
    assert out == "[docs](https://example.com/a)"


def test_pipe_table_becomes_bullet_groups():
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    out = format_message(table)
    assert "|" not in out
    assert "• b: 2" in out


def test_table_inside_code_fence_is_left_alone():
    out = format_message("```\n| a | b |\n|---|---|\n| 1 | 2 |\n```")
    assert "| a | b |" in out


def test_empty_input_is_returned_unchanged():
    assert format_message("") == ""
