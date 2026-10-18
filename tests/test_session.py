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


def test_new_fields_survive_a_restart(tmp_path):
    path = tmp_path / "s.json"
    store = SessionStore(path)
    store.update(
        "1",
        effort="high",
        session_id="sess-123",
        session_name="auth work",
        seed_context="brief",
        compact_mode="fast",
        history=["a", "b"],
        last_usage={"input_tokens": 10},
        total_usage={"input_tokens": 10},
        last_duration_ms=900,
    )

    session = SessionStore(path).get("1")
    assert session.effort == "high"
    assert session.session_id == "sess-123"
    assert session.session_name == "auth work"
    assert session.seed_context == "brief"
    assert session.compact_mode == "fast"
    assert session.history == ["a", "b"]
    assert session.last_usage == {"input_tokens": 10}
    assert session.last_duration_ms == 900


def test_older_session_files_load_without_new_fields(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"1": {"chat_id": "1", "model": "m"}}', encoding="utf-8")
    session = SessionStore(path).get("1")
    assert session.history == []
    assert session.last_usage == {}
    assert session.total_usage == {}
    assert session.effort is None


def test_remember_tracks_history_and_last_prompt(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.remember("1", "first")
    session = store.remember("1", "second")
    assert session.history == ["first", "second"]
    assert session.last_prompt == "second"


def test_remember_caps_history(tmp_path):
    from telegram_commandcode.session import HISTORY_LIMIT

    store = SessionStore(tmp_path / "s.json")
    for i in range(HISTORY_LIMIT + 10):
        store.remember("1", f"p{i}")

    session = store.get("1")
    assert len(session.history) == HISTORY_LIMIT
    assert session.history[-1] == f"p{HISTORY_LIMIT + 9}"
    assert session.history[0] == "p10"
