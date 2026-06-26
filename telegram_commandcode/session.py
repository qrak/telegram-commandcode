"""
Persistent session state for telegram-commandcode.

Each Telegram chat gets its own session with model selection, plan mode,
steer/goal directives, queued prompts, and conversation state. Sessions
persist across bot restarts via a JSON file at ~/.commandcode/telegram_sessions.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

SESSIONS_FILE = Path.home() / ".commandcode" / "telegram_sessions.json"


@dataclass
class ChatSession:
    """Per-chat session state. One instance per Telegram chat_id."""

    chat_id: str
    active: bool = False          # Whether a conversation is ongoing (--continue)
    model: Optional[str] = None   # Override model (passed as -m)
    plan_mode: bool = False       # Plan mode toggle
    one_shot_plan: bool = False   # /plan <task> → one-shot, auto-disables after
    steer: Optional[str] = None   # Mid-session guidance prepended to prompts
    goal: Optional[str] = None    # Standing objective prepended to prompts
    last_prompt: Optional[str] = None  # For /retry
    add_dirs: list[str] = field(default_factory=list)  # --add-dir paths
    queued_prompts: list[str] = field(default_factory=list)  # /queue items
    session_name: Optional[str] = None  # /rename target
    compact_mode: Optional[str] = None  # /compact-mode strategy (default, aggressive, gentle)
    yolo: bool = True                     # /yolo toggle (--yolo flag)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChatSession":
        # Filter out keys not in the dataclass (backward compat)
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        # Ensure add_dirs and queued_prompts are lists
        filtered.setdefault("add_dirs", [])
        filtered.setdefault("queued_prompts", [])
        return cls(**filtered)


class SessionStore:
    """
    Thread-safe, file-backed session store.

    Reads from disk on first access and writes back on every mutation.
    Uses a simple mutex for concurrent access safety.
    """

    def __init__(self, filepath: Path = SESSIONS_FILE):
        self._filepath = filepath
        self._lock = Lock()
        self._sessions: dict[str, ChatSession] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Lazy-load sessions from disk on first access."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._sessions = {}
            if self._filepath.exists():
                try:
                    data = json.loads(self._filepath.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for chat_id, sess_data in data.items():
                            try:
                                self._sessions[chat_id] = ChatSession.from_dict(sess_data)
                            except Exception:
                                logger.warning("Skipping corrupt session for chat %s", chat_id)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("Failed to load sessions file: %s", e)
            self._loaded = True

    def _save(self) -> None:
        """Persist all sessions to disk."""
        try:
            self._filepath.parent.mkdir(parents=True, exist_ok=True)
            data = {cid: sess.to_dict() for cid, sess in self._sessions.items()}
            self._filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to save sessions: %s", e)

    def get(self, chat_id: str) -> ChatSession:
        """Get or create a session for a chat."""
        self._ensure_loaded()
        with self._lock:
            if chat_id not in self._sessions:
                self._sessions[chat_id] = ChatSession(chat_id=chat_id)
                self._save()
            return self._sessions[chat_id]

    def update(self, chat_id: str, **kwargs) -> ChatSession:
        """Update a session's fields and persist."""
        self._ensure_loaded()
        with self._lock:
            sess = self._sessions.get(chat_id, ChatSession(chat_id=chat_id))
            for key, value in kwargs.items():
                if hasattr(sess, key):
                    setattr(sess, key, value)
            self._sessions[chat_id] = sess
            self._save()
            return sess

    def reset(self, chat_id: str) -> None:
        """Reset a session to factory defaults."""
        with self._lock:
            self._sessions.pop(chat_id, None)
            self._save()

    @property
    def all_sessions(self) -> dict[str, ChatSession]:
        """Return a snapshot of all sessions (read-only)."""
        self._ensure_loaded()
        with self._lock:
            return dict(self._sessions)


# Global singleton
session_store = SessionStore()
