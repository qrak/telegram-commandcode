"""Parsing of `cmd --output-format json` frames.

The CLI emits NDJSON: event frames while the run progresses, then exactly one
result frame. These tests pin the tolerant behaviour — a bad line must never
raise, and unknown event types must be ignored.
"""

import json

from telegram_commandcode.streamjson import (
    parse_frame,
    render_progress,
    render_result,
    tool_label,
)


def event_frame(event):
    return json.dumps({"type": "event", "event": event})


def result_frame(**fields):
    payload = {"type": "result", "subtype": "success", "finalText": "done"}
    payload.update(fields)
    return json.dumps(payload)


def test_parses_event_frame():
    frame = parse_frame(event_frame({"type": "tool_running", "toolName": "read_file"}))
    assert frame is not None
    assert frame.kind == "event"
    assert frame.event["toolName"] == "read_file"


def test_parses_result_frame():
    frame = parse_frame(result_frame(sessionId="abc", usage={"input_tokens": 5}))
    assert frame is not None
    assert frame.kind == "result"
    assert frame.result["sessionId"] == "abc"


def test_plain_text_line_is_not_a_frame():
    assert parse_frame("just some output") is None
    assert parse_frame("{not json") is None
    assert parse_frame("") is None


def test_non_object_json_is_not_a_frame():
    assert parse_frame("[1, 2, 3]") is None


def test_unknown_type_is_kept_as_other():
    frame = parse_frame(json.dumps({"type": "mystery"}))
    assert frame is not None
    assert frame.kind == "other"


def test_event_without_payload_is_other():
    frame = parse_frame(json.dumps({"type": "event"}))
    assert frame is not None
    assert frame.kind == "other"


def test_tool_running_progress_includes_description():
    assert render_progress(
        {"type": "tool_running", "toolName": "read_file", "description": "src/app.py"}
    ) == "⚙️ read: src/app.py"


def test_tool_running_without_description_uses_label():
    assert render_progress({"type": "tool_running", "toolName": "grep"}) == "⚙️ grep"


def test_failed_tool_result_is_marked():
    line = render_progress({"type": "tool_result", "toolName": "shell", "isError": True})
    assert line.startswith("❌")


def test_unknown_event_is_skipped():
    assert render_progress({"type": "usage_update"}) is None


def test_tool_label_falls_back_to_raw_name():
    assert tool_label("custom_tool") == "custom_tool"
    assert tool_label("") == "tool"


def test_render_result_normalises_types():
    parsed = render_result(
        {
            "subtype": "success",
            "sessionId": "s-1",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "durationMs": 1200,
            "stopReason": "end_turn",
            "finalText": "hi",
        }
    )
    assert parsed["session_id"] == "s-1"
    assert parsed["usage"]["input_tokens"] == 10
    assert parsed["duration_ms"] == 1200
    assert parsed["stop_reason"] == "end_turn"
    assert parsed["final_text"] == "hi"


def test_render_result_tolerates_missing_and_wrong_types():
    parsed = render_result({"usage": "nope", "finalText": None})
    assert parsed["usage"] == {}
    assert parsed["final_text"] == ""
    assert parsed["session_id"] is None
