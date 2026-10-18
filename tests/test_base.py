"""Command handler plumbing: chunked sends, code-block escaping, CLI runner."""

import sys
import time

import pytest

from telegram_commandcode.commands import base as base_mod
from telegram_commandcode.commands.base import BaseCommandHandler


class FakeChat:
    def __init__(self, parse_failures=0):
        self.messages = []      # (text, parse_mode)
        self.documents = []     # filenames
        self.actions = []
        self.parse_failures = parse_failures

    async def send_message(self, text, parse_mode=None, **kwargs):
        if self.parse_failures:
            self.parse_failures -= 1
            raise Exception("Bad Request: can't parse entities")
        self.messages.append((text, parse_mode))

        class Msg:
            message_id = len(self.messages)

        return Msg()

    async def send_document(self, document, filename=None, caption=None, **kwargs):
        self.documents.append(filename)

    async def send_chat_action(self, action):
        self.actions.append(action)


class FakeUpdate:
    def __init__(self, chat):
        self.effective_chat = chat
        self.message = None


async def test_short_send_is_single_message():
    chat = FakeChat()
    await BaseCommandHandler()._send_chunked(FakeUpdate(chat), "hello")
    assert chat.messages == [("hello", "MarkdownV2")]


async def test_long_send_is_chunked():
    chat = FakeChat()
    await BaseCommandHandler()._send_chunked(FakeUpdate(chat), "word " * 3000)
    assert len(chat.messages) > 1
    assert all(len(text) <= 4096 for text, _ in chat.messages)


async def test_parse_failure_retries_without_parse_mode():
    chat = FakeChat(parse_failures=1)
    await BaseCommandHandler()._send_chunked(FakeUpdate(chat), "a.b")
    assert chat.messages == [("a.b", None)]


async def test_cli_output_inside_code_block_is_not_double_escaped():
    """Inside ``` fences only ` and \\ need escaping; escaping "." shows "\\."."""
    chat = FakeChat()
    handler = BaseCommandHandler()

    async def fake_run_cli_result(args, timeout=30):
        return "model.v2 (default)", False

    handler.run_cli_result = fake_run_cli_result
    await handler.run_cli_and_reply(FakeUpdate(chat), ["--version"])

    sent, _ = chat.messages[0]
    assert "model.v2 (default)" in sent
    assert r"\." not in sent
    assert chat.actions == ["typing"]


async def test_run_cli_returns_stdout(monkeypatch):
    monkeypatch.setattr(base_mod, "DEFAULT_CMD_BIN", sys.executable)
    out = await BaseCommandHandler().run_cli(["-c", "print('hello')"])
    assert out == "hello"


async def test_run_cli_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(base_mod, "DEFAULT_CMD_BIN", "definitely-not-a-real-binary-xyz")
    out = await BaseCommandHandler().run_cli(["--version"])
    assert "not found" in out


async def test_run_cli_kills_child_on_timeout(monkeypatch):
    monkeypatch.setattr(base_mod, "DEFAULT_CMD_BIN", sys.executable)
    started = time.monotonic()
    out = await BaseCommandHandler().run_cli(
        ["-c", "import time; time.sleep(30)"], timeout=0.3
    )
    elapsed = time.monotonic() - started
    assert "timed out" in out
    assert elapsed < 5, "run_cli must not wait for the child after a timeout"


def test_read_cc_config_tolerates_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        BaseCommandHandler, "cc_config_path", staticmethod(lambda: tmp_path / "nope.json")
    )
    assert BaseCommandHandler.read_cc_config() == {}


def test_write_then_read_cc_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(BaseCommandHandler, "cc_config_path", staticmethod(lambda: path))

    BaseCommandHandler.write_cc_config({"model": "x"})
    assert BaseCommandHandler.read_cc_config() == {"model": "x"}


def test_read_cc_config_tolerates_corrupt_json(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    monkeypatch.setattr(BaseCommandHandler, "cc_config_path", staticmethod(lambda: path))
    assert BaseCommandHandler.read_cc_config() == {}


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text_sends_nothing(text):
    chat = FakeChat()
    await BaseCommandHandler()._send_chunked(FakeUpdate(chat), text)
    assert chat.messages == []
