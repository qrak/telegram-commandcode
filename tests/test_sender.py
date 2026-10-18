"""MessageSender behaviour: chunking, parse fallback, file fallback.

These tests use a fake bot, so no network and no Telegram token are needed.
"""

import pytest

from telegram_commandcode.chunking import FileFallbackResult
from telegram_commandcode.gateway import sender as sender_mod
from telegram_commandcode.gateway.sender import _send_message_safe


class FakeBot:
    def __init__(self, failures=None):
        self.messages = []          # (text, parse_mode)
        self.documents = []         # filenames
        self.failures = list(failures or [])

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if self.failures:
            error = self.failures.pop(0)
            if error is not None:
                raise error
        self.messages.append((text, parse_mode))

        class Msg:
            message_id = len(self.messages)

        return Msg()

    async def send_document(self, chat_id, document, filename=None, caption=None, **kwargs):
        self.documents.append(filename)


async def test_short_message_is_sent_once():
    bot = FakeBot()
    await _send_message_safe(1, "hello", bot=bot)
    assert bot.messages == [("hello", "MarkdownV2")]


async def test_empty_text_is_not_sent():
    bot = FakeBot()
    assert await _send_message_safe(1, "   ", bot=bot) is None
    assert bot.messages == []


async def test_long_message_is_split_into_several_sends():
    bot = FakeBot()
    await _send_message_safe(1, ("line of text\n" * 900), bot=bot)
    assert len(bot.messages) > 1
    for text, _ in bot.messages:
        assert len(text) <= 4096


async def test_parse_error_falls_back_to_plain_text():
    bot = FakeBot(failures=[Exception("Bad Request: can't parse entities")])
    await _send_message_safe(1, r"broken \*markup*", bot=bot)
    # The first attempt fails, the plain-text retry is the only recorded send.
    assert len(bot.messages) == 1
    assert bot.messages[0][1] is None


async def test_too_long_error_rechunks_without_parse_mode():
    bot = FakeBot(failures=[Exception("Bad Request: message is too long")])
    await _send_message_safe(1, "x" * 5000, bot=bot)
    assert bot.messages, "the message must still be delivered"
    # The chunk Telegram rejected is retried as plain text; later chunks keep
    # their formatting, so we assert at least one plain send rather than all.
    assert any(parse_mode is None for _, parse_mode in bot.messages)


async def test_file_fallback_sends_document_and_deletes_temp_file(monkeypatch, tmp_path):
    temp_file = tmp_path / "out.txt"
    temp_file.write_text("x" * 20000, encoding="utf-8")

    monkeypatch.setattr(
        sender_mod,
        "maybe_file_fallback",
        lambda text, threshold=sender_mod.FILE_FALLBACK_THRESHOLD: FileFallbackResult(
            preview_message="preview", file_path=temp_file
        ),
    )

    bot = FakeBot()
    await _send_message_safe(1, "y" * 20000, bot=bot)

    assert bot.documents, "document should have been sent"
    assert bot.messages[0][0] == "preview"
    assert not temp_file.exists(), "temp file must not leak"


async def test_file_fallback_failure_is_not_fatal(monkeypatch, tmp_path):
    temp_file = tmp_path / "out.txt"
    temp_file.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        sender_mod,
        "maybe_file_fallback",
        lambda text, threshold=sender_mod.FILE_FALLBACK_THRESHOLD: FileFallbackResult(
            preview_message="preview", file_path=temp_file
        ),
    )

    class BrokenBot(FakeBot):
        async def send_document(self, *args, **kwargs):
            raise RuntimeError("upload failed")

    bot = BrokenBot()
    await _send_message_safe(1, "y" * 20000, bot=bot)
    # Falls through to chunked sending instead of raising.
    assert len(bot.messages) > 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Exception("Bad Request: can't parse entities"), "parse"),
        (Exception("Bad Request: message is too long"), "too_long"),
        (Exception("Flood control exceeded. Retry after 5"), "flood"),
        (Exception("Connection error"), "connect"),
        (Exception("something else entirely"), "unknown"),
    ],
)
def test_error_classification(error, expected):
    assert sender_mod.classify_telegram_error(error) == expected
