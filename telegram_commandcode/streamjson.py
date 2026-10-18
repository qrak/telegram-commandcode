"""
Parsing for ``cmd --output-format json`` (newline-delimited JSON).

Headless runs emit one JSON object per line in two shapes:

    {"type": "event",  "event": {"type": "tool_running", "toolName": "read_file",
                                 "description": "...", "toolCallId": "..."}}
    {"type": "result", "subtype": "success", "sessionId": "...",
     "usage": {...}, "durationMs": 8421, "finalText": "..."}

Everything here is deliberately tolerant: unknown event types are ignored
(they are forward-compatible by design), non-JSON lines are surfaced as
plain text, and a malformed frame never raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# Human labels for the tools we know. Unknown tools fall back to their raw id.
_TOOL_LABELS = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "multi_edit_file": "edit",
    "list_directory": "list",
    "glob": "glob",
    "grep": "grep",
    "shell": "shell",
    "run_command": "shell",
    "web_search": "search",
    "web_fetch": "fetch",
    "todo_write": "todo",
    "task": "subagent",
    "ask_user_question": "ask",
}


@dataclass
class StreamFrame:
    """One parsed NDJSON frame."""

    kind: str                                  # "event" | "result" | "other"
    event: dict = field(default_factory=dict)  # payload when kind == "event"
    result: dict = field(default_factory=dict)  # payload when kind == "result"


def parse_frame(line: str) -> Optional[StreamFrame]:
    """Parse a single stdout line into a frame, or None if it is not JSON.

    None means "this line is ordinary text" — callers keep it as raw output.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    kind = obj.get("type")
    if kind == "event" and isinstance(obj.get("event"), dict):
        return StreamFrame("event", event=obj["event"])
    if kind == "result":
        return StreamFrame("result", result=obj)
    return StreamFrame("other", result=obj)


def tool_label(tool_name: str) -> str:
    """Short human label for a tool name."""
    return _TOOL_LABELS.get(tool_name, tool_name or "tool")


def render_progress(event: dict) -> Optional[str]:
    """Turn an event into a one-line progress message, or None to skip it.

    The goal is a short activity log for the Telegram status message:
    "⚙️ read src/app.py" beats a wall of JSON.
    """
    etype = str(event.get("type") or "")

    if etype in ("tool_running", "tool_start", "tool_started"):
        name = event.get("toolName") or event.get("tool") or ""
        label = tool_label(str(name))
        description = str(event.get("description") or "").strip()
        return f"⚙️ {label}: {description}" if description else f"⚙️ {label}"

    if etype in ("tool_done", "tool_result", "tool_finished"):
        name = event.get("toolName") or event.get("tool") or ""
        label = tool_label(str(name))
        failed = event.get("isError") or event.get("error")
        return f"{'❌' if failed else '✅'} {label}"

    # Assistant prose may arrive as a delta; showing it live would duplicate
    # the final answer, so only handle an explicit text-carrying event.
    text = event.get("text") or event.get("message")
    if isinstance(text, str) and text.strip():
        return text.strip()

    return None


def render_result(result: dict) -> dict:
    """Extract the fields of a result frame into a normalised dict."""
    usage = result.get("usage")
    return {
        "final_text": str(result.get("finalText") or ""),
        "session_id": result.get("sessionId") or None,
        "usage": usage if isinstance(usage, dict) else {},
        "duration_ms": result.get("durationMs"),
        "stop_reason": result.get("stopReason"),
        "subtype": result.get("subtype"),
        "error": result.get("error") or "",
    }
