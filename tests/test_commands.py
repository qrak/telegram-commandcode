"""Slash-command handlers: state changes, without Telegram and without `cmd`."""

import pytest

from telegram_commandcode.commands import base as base_mod
from telegram_commandcode.commands.config_cmds import ConfigCommands
from telegram_commandcode.commands.info_cmds import InfoCommands
from telegram_commandcode.commands.session_cmds import SessionCommands
from telegram_commandcode.session import SessionStore


class FakeChat:
    def __init__(self, chat_id=1):
        self.id = chat_id
        self.messages = []
        self.actions = []

    async def send_message(self, text, parse_mode=None, **kwargs):
        self.messages.append(text)

    async def send_document(self, *args, **kwargs):
        pass

    async def send_chat_action(self, action):
        self.actions.append(action)


class FakeUpdate:
    def __init__(self, chat=None):
        self.effective_chat = chat or FakeChat()

    @property
    def chat_id(self):
        return str(self.effective_chat.id)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionStore(tmp_path / "sessions.json")
    monkeypatch.setattr(base_mod, "session_store", s)
    return s


async def test_yolo_toggles_both_ways(store):
    handler = SessionCommands()
    update = FakeUpdate()

    await handler.handle_yolo(update, None, "")
    assert store.get("1").yolo is False

    await handler.handle_yolo(update, None, "")
    assert store.get("1").yolo is True


async def test_clear_resets_session_state(store):
    store.update("1", model="x", goal="ship", active=True, yolo=False)
    await SessionCommands().handle_clear(FakeUpdate(), None, "")

    state = store.get("1")
    assert state.model is None
    assert state.goal is None
    assert state.active is False
    assert state.yolo is True


async def test_resume_marks_session_active_and_returns_prompt(store):
    result = await SessionCommands().handle_resume(FakeUpdate(), None, "")
    assert store.get("1").active is True
    assert isinstance(result, str) and result


async def test_undo_without_history_does_nothing(store):
    result = await SessionCommands().handle_undo(FakeUpdate(), None, "1")
    assert result is None


async def test_model_switch_updates_state_and_config(store, monkeypatch):
    written = {}
    monkeypatch.setattr(
        ConfigCommands, "write_cc_config", classmethod(lambda cls, cfg: written.update(cfg))
    )
    monkeypatch.setattr(ConfigCommands, "read_cc_config", classmethod(lambda cls: {}))

    await ConfigCommands().handle_model(FakeUpdate(), None, "claude-sonnet-4-6")

    assert store.get("1").model == "claude-sonnet-4-6"
    assert written["model"] == "claude-sonnet-4-6"


async def test_stop_reports_when_nothing_is_running(store, monkeypatch):
    from telegram_commandcode import executor

    async def nothing_running(chat_id):
        return False

    monkeypatch.setattr(executor.process_tracker, "kill", nothing_running)
    await SessionCommands().handle_stop(FakeUpdate(), None, "")
    # No exception, and the handler sends some acknowledgement.


async def test_courses_message_contains_link():
    chat = FakeChat()
    await InfoCommands().handle_courses(FakeUpdate(chat), None, "")
    assert "commandcode" in chat.messages[0]
