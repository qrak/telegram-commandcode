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


# ═══════════════════════════════════════════════════════════════════════
# Session lifecycle: compact, undo, fork, rename, resume, stop
# ═══════════════════════════════════════════════════════════════════════

from telegram_commandcode.commands import session_cmds as session_mod  # noqa: E402
from telegram_commandcode.executor import CmdResult  # noqa: E402


@pytest.fixture
def handoff(monkeypatch):
    """Replace the summarisation run with a canned success."""
    calls = []

    async def fake_run_cmd(prompt, opts, **kwargs):
        calls.append({"prompt": prompt, "opts": opts})
        return CmdResult(
            stdout="", stderr="", exit_code=0, success=True,
            final_text="BRIEF",
        )

    monkeypatch.setattr(session_mod, "run_cmd", fake_run_cmd)
    return calls


@pytest.fixture
def failing_handoff(monkeypatch):
    async def fake_run_cmd(prompt, opts, **kwargs):
        return CmdResult(
            stdout="", stderr="boom", exit_code=7, success=False,
            subtype="error",
        )

    monkeypatch.setattr(session_mod, "run_cmd", fake_run_cmd)


async def test_compact_without_a_session_does_not_call_the_cli(store, handoff):
    await SessionCommands().handle_compact(FakeUpdate(), None, "")
    assert handoff == []
    assert store.get("1").active is False


async def test_compact_seeds_a_fresh_session(store, handoff):
    store.update("1", active=True, goal="ship", model="m")
    await SessionCommands().handle_compact(FakeUpdate(), None, "")

    assert len(handoff) == 1
    assert handoff[0]["opts"].continue_session is True
    assert handoff[0]["opts"].yolo is False, "summarising must not edit files"

    state = store.get("1")
    assert state.seed_context == "BRIEF"
    assert state.active is False, "the summary starts a new session"
    assert state.goal is None


async def test_compact_failure_changes_nothing(store, failing_handoff):
    store.update("1", active=True, goal="ship")
    await SessionCommands().handle_compact(FakeUpdate(), None, "")

    state = store.get("1")
    assert state.seed_context is None
    assert state.active is True
    assert state.goal == "ship"


async def test_undo_drops_the_last_n_turns(store, handoff):
    store.update("1", active=True, history=["a", "b", "c"], last_prompt="c")
    await SessionCommands().handle_undo(FakeUpdate(), None, "2")

    assert len(handoff) == 1
    assert "- b" in handoff[0]["prompt"]
    assert "- c" in handoff[0]["prompt"]

    state = store.get("1")
    assert state.history == ["a"]
    assert state.last_prompt == "a"
    assert state.seed_context == "BRIEF"


async def test_undo_defaults_to_one_turn(store, handoff):
    store.update("1", active=True, history=["a", "b"])
    await SessionCommands().handle_undo(FakeUpdate(), None, "")
    assert store.get("1").history == ["a"]


async def test_undo_rejects_non_numeric_argument(store, handoff):
    store.update("1", active=True, history=["a"])
    await SessionCommands().handle_undo(FakeUpdate(), None, "two")
    assert handoff == []
    assert store.get("1").history == ["a"]


async def test_undo_without_an_active_session_does_nothing(store, handoff):
    store.update("1", history=["a"])
    await SessionCommands().handle_undo(FakeUpdate(), None, "1")
    assert handoff == []


async def test_fork_defers_to_the_next_prompt(store):
    store.update("1", active=True, session_name="main")
    await SessionCommands().handle_fork(FakeUpdate(), None, "spike")

    state = store.get("1")
    assert state.fork_next == "spike"
    assert state.active is True, "the fork happens on the next run"


async def test_fork_without_a_session_is_refused(store):
    await SessionCommands().handle_fork(FakeUpdate(), None, "spike")
    assert store.get("1").fork_next is None


async def test_rename_sets_the_session_name(store):
    await SessionCommands().handle_rename(FakeUpdate(), None, "auth work")
    assert store.get("1").session_name == "auth work"


async def test_resume_with_a_target_sets_a_one_shot_flag(store):
    prompt = await SessionCommands().handle_resume(FakeUpdate(), None, "sess-42")

    state = store.get("1")
    assert state.resume_target == "sess-42"
    assert state.active is True
    assert isinstance(prompt, str) and prompt


async def test_resume_list_shows_known_sessions(store):
    chat = FakeChat()
    store.update("1", session_name="named", session_id="abc")
    await SessionCommands().handle_resume(FakeUpdate(chat), None, "list")
    assert "named" in chat.messages[0]
    assert "abc" in chat.messages[0]


async def test_stop_kills_foreground_and_background(store, monkeypatch):
    from telegram_commandcode import executor

    calls = {"fg": 0, "bg": 0}

    async def fake_kill(chat_id):
        calls["fg"] += 1
        return True

    async def fake_kill_bg(chat_id):
        calls["bg"] += 1
        return 2

    monkeypatch.setattr(executor.process_tracker, "kill", fake_kill)
    monkeypatch.setattr(executor.process_tracker, "kill_background", fake_kill_bg)

    await SessionCommands().handle_stop(FakeUpdate(), None, "")
    assert calls == {"fg": 1, "bg": 1}


# ═══════════════════════════════════════════════════════════════════════
# Config: effort, provider, compact mode
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def cc_config(monkeypatch):
    written = {}
    monkeypatch.setattr(ConfigCommands, "read_cc_config", classmethod(lambda cls: {}))
    monkeypatch.setattr(
        ConfigCommands, "write_cc_config", classmethod(lambda cls, cfg: written.update(cfg))
    )
    return written


async def test_effort_updates_state_and_config(store, cc_config):
    store.update("1", model="gpt-5")
    await ConfigCommands().handle_effort(FakeUpdate(), None, "high")

    assert store.get("1").effort == "high"
    assert cc_config["reasoningEffort"] == {"gpt-5": "high"}


async def test_effort_rejects_unknown_levels(store, cc_config):
    await ConfigCommands().handle_effort(FakeUpdate(), None, "turbo")
    assert store.get("1").effort is None
    assert "reasoningEffort" not in cc_config


async def test_compact_mode_is_written_to_config(store, cc_config):
    await ConfigCommands().handle_compact_mode(FakeUpdate(), None, "fast")
    assert store.get("1").compact_mode == "fast"
    assert cc_config["compactMode"] == "fast"


async def test_compact_mode_rejects_the_old_invented_values(store, cc_config):
    await ConfigCommands().handle_compact_mode(FakeUpdate(), None, "aggressive")
    assert store.get("1").compact_mode is None
    assert "compactMode" not in cc_config


async def test_provider_accepts_documented_values(store, cc_config):
    await ConfigCommands().handle_provider(FakeUpdate(), None, "anthropic")
    assert cc_config["provider"] == "anthropic"


async def test_provider_rejects_unknown_names(store, cc_config):
    await ConfigCommands().handle_provider(FakeUpdate(), None, "skynet")
    assert "provider" not in cc_config
