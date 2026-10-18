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
