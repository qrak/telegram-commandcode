"""Prompt assembly and usage accounting in the processor layer.

These are pure functions: no Telegram, no subprocess, no network.
"""

from telegram_commandcode.executor import CmdResult
from telegram_commandcode.gateway.processor import (
    build_final_prompt,
    merge_usage,
    usage_footer,
)
from telegram_commandcode.session import ChatSession


def test_plain_prompt_is_untouched():
    assert build_final_prompt(ChatSession(chat_id="1"), "hello") == "hello"


def test_seed_context_is_injected_before_the_prompt():
    state = ChatSession(chat_id="1", seed_context="SUMMARY")
    prompt = build_final_prompt(state, "next step?")
    assert prompt.startswith("[COMPACTED CONTEXT]\nSUMMARY")
    assert prompt.endswith("next step?")


def test_goal_and_steer_are_both_prepended():
    state = ChatSession(
        chat_id="1", goal="ship it", steer="be terse", seed_context="S",
    )
    prompt = build_final_prompt(state, "go")
    assert prompt.index("S") < prompt.index("[GOAL] ship it")
    assert prompt.index("[GOAL] ship it") < prompt.index("[GUIDANCE] be terse")
    assert prompt.index("[GUIDANCE] be terse") < prompt.index("go")


def test_merge_usage_sums_numeric_keys():
    assert merge_usage({"input_tokens": 5}, {"input_tokens": 7, "output_tokens": 2}) == {
        "input_tokens": 12,
        "output_tokens": 2,
    }


def test_merge_usage_ignores_bools():
    merged = merge_usage({}, {"truncated": True, "input_tokens": 3})
    assert merged["truncated"] is True
    assert merged["input_tokens"] == 3


def test_usage_footer_shows_duration_and_tokens():
    result = CmdResult(
        stdout="", stderr="", exit_code=0,
        duration_ms=8421, usage={"input_tokens": 1234, "output_tokens": 56},
    )
    footer = usage_footer(result)
    assert "8.4s" in footer
    assert "1,234→56 tok" in footer


def test_usage_footer_is_empty_without_data():
    assert usage_footer(CmdResult(stdout="", stderr="", exit_code=0)) == ""


def test_failed_result_reports_a_human_reason():
    result = CmdResult(stdout="", stderr="", exit_code=10)
    assert result.human_reason == "insufficient credits"
    assert result.is_error is True


def test_result_output_prefers_final_text():
    result = CmdResult(
        stdout='{"raw": true}', stderr="", exit_code=0, final_text="clean answer",
    )
    assert result.output == "clean answer"


def test_error_subtype_marks_the_result_as_error():
    result = CmdResult(stdout="", stderr="", exit_code=0, subtype="error")
    assert result.is_error is True
