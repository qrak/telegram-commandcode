"""Session persistence tests — state must survive a bot restart."""

from telegram_commandcode.session import ChatSession, SessionStore


def test_defaults_are_sane():
    s = ChatSession(chat_id="1")
    assert s.active is False
    assert s.yolo is True
    assert s.add_dirs == []
    assert s.queued_prompts == []


def test_unknown_keys_are_ignored_on_load():
    s = ChatSession.from_dict({"chat_id": "1", "model": "x", "legacy_field": True})
    assert s.model == "x"


def test_update_persists_across_instances(tmp_path):
    path = tmp_path / "sessions.json"

    store = SessionStore(path)
    store.update("42", model="claude", goal="ship it")

    reloaded = SessionStore(path)
    session = reloaded.get("42")
    assert session.model == "claude"
    assert session.goal == "ship it"


def test_get_creates_missing_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    assert store.get("7").chat_id == "7"


def test_reset_clears_state_but_keeps_chat_id(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.update("9", model="m", active=True)
    store.reset("9")
    session = store.get("9")
    assert session.chat_id == "9"
    assert session.model is None
    assert session.active is False
