"""Unit tests for the CLI executor — argument construction and workdir
resolution. No subprocess, no Command Code binary, no network."""

import sys
from pathlib import Path

import pytest

from telegram_commandcode import executor


def opts(**kwargs):
    return executor.ExecOptions(**kwargs)


def test_prompt_is_the_last_argument():
    args = opts().build_args("do the thing")
    assert args[-2:] == ["-p", "do the thing"]


def test_prompt_starting_with_dash_is_not_misparsed():
    args = opts().build_args("--help me fix this")
    assert args[-1] == "--help me fix this"


def test_yolo_flag_present_by_default():
    assert "--yolo" in opts(yolo=True).build_args("x")


def test_yolo_flag_can_be_disabled():
    assert "--yolo" not in opts(yolo=False).build_args("x")


def test_model_and_turns_are_forwarded():
    args = opts(model="gpt-5", max_turns=7).build_args("x")
    assert args[args.index("-m") + 1] == "gpt-5"
    assert args[args.index("--max-turns") + 1] == "7"


def test_add_dir_is_repeatable():
    args = opts(add_dirs=["/a", "/b"]).build_args("x")
    assert [a for i, a in enumerate(args) if args[i - 1] == "--add-dir"] == ["/a", "/b"]


def test_continue_session_flag():
    assert "--continue" in opts(continue_session=True).build_args("x")
    assert "--continue" not in opts(continue_session=False).build_args("x")


def test_workdir_defaults_to_cwd(monkeypatch):
    monkeypatch.setattr(executor, "DEFAULT_WORKDIR", "")
    assert executor._workdir(opts()) == str(Path.cwd())


def test_workdir_env_is_used(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "DEFAULT_WORKDIR", str(tmp_path))
    assert executor._workdir(opts()) == str(tmp_path)


def test_explicit_cwd_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "DEFAULT_WORKDIR", "/somewhere/else")
    assert executor._workdir(opts(cwd=tmp_path)) == str(tmp_path)


def test_invalid_int_env_falls_back(monkeypatch):
    monkeypatch.setenv("COMMAND_CODE_MAX_TURNS", "not-a-number")
    assert executor._int_env("COMMAND_CODE_MAX_TURNS", 20) == 20


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exit-code semantics")
def test_missing_binary_returns_error_result():
    import asyncio

    result = asyncio.run(
        executor.run_cmd("hi", opts(), chat_id=None),
    )
    # `cmd` does not exist on this POSIX test machine.
    assert result.success is False


def test_exit_code_reasons_are_mapped():
    result = executor.CmdResult(stdout="", stderr="", exit_code=4)
    assert "permission" in result.human_reason


# ── Flags documented in the CLI reference ──────────────────────────────


def test_effort_flag_is_forwarded():
    args = opts(effort="high").build_args("x")
    assert args[args.index("--effort") + 1] == "high"


def test_effort_flag_is_absent_by_default():
    assert "--effort" not in opts().build_args("x")


def test_resume_target_beats_continue():
    args = opts(continue_session=True, resume_session="abc123").build_args("x")
    assert args[args.index("--resume") + 1] == "abc123"
    assert "--continue" not in args


def test_fork_session_implies_continue():
    args = opts(fork_session=True).build_args("x")
    assert "--continue" in args
    assert "--fork-session" in args


def test_fork_session_keeps_an_explicit_resume():
    args = opts(fork_session=True, resume_session="s1").build_args("x")
    assert "--resume" in args
    assert "--continue" not in args
    assert "--fork-session" in args


def test_session_name_is_forwarded():
    args = opts(session_name="auth refactor").build_args("x")
    assert args[args.index("-n") + 1] == "auth refactor"


def test_compact_mode_uses_config_flag():
    args = opts(compact_mode="fast").build_args("x")
    assert args[args.index("--config") + 1] == "compact-mode=fast"


def test_json_output_is_on_by_default():
    args = opts().build_args("x")
    assert args[args.index("--output-format") + 1] == "json"


def test_json_output_can_be_turned_off():
    assert "--output-format" not in opts(json_output=False).build_args("x")


def test_trust_can_be_disabled():
    assert "--trust" in opts(trust=True).build_args("x")
    assert "--trust" not in opts(trust=False).build_args("x")


def test_output_prefers_model_answer_over_raw_stdout():
    result = executor.CmdResult(
        stdout='{"type":"result"}', stderr="", exit_code=0,
        final_text="the answer",
    )
    assert result.output == "the answer"


def test_new_exit_codes_have_reasons():
    assert "max turns" in executor.CmdResult("", "", 8).human_reason
    assert "credits" in executor.CmdResult("", "", 10).human_reason
    assert "interrupted" in executor.CmdResult("", "", 130).human_reason


def test_success_requires_a_zero_exit_and_no_timeout():
    assert executor.CmdResult("", "", 0, success=True).success is True
    assert executor.CmdResult("", "", 1).success is False


# ── Process tracker: foreground and background slots ───────────────────


class FakeProc:
    """A stand-in for asyncio.subprocess.Process."""

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def send_signal(self, sig):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.terminated = True
        self.returncode = 1

    async def wait(self):
        return self.returncode


async def test_foreground_and_background_are_independent():
    tracker = executor.RunningProcessTracker()
    foreground, background = FakeProc(), FakeProc()
    tracker.register("1", foreground)
    tracker.register_bg("1", background)

    assert tracker.get("1") is foreground
    assert tracker.background_count("1") == 1

    assert await tracker.kill("1") is True
    assert foreground.terminated is True
    assert background.terminated is False, "stopping a prompt must not kill background work"

    assert await tracker.kill_background("1") == 1
    assert background.terminated is True
    assert tracker.background_count("1") == 0


async def test_finished_process_is_not_reported_as_running():
    tracker = executor.RunningProcessTracker()
    proc = FakeProc()
    proc.returncode = 0
    tracker.register("1", proc)
    assert tracker.get("1") is None
    assert tracker.is_running("1") is False


async def test_kill_without_a_process_is_false():
    tracker = executor.RunningProcessTracker()
    assert await tracker.kill("nope") is False
    assert await tracker.kill_background("nope") == 0
