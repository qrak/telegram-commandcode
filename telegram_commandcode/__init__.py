"""
telegram-commandcode v2 — Async Python Telegram bot for Command Code CLI.

Bridges Telegram ↔ Command Code with Hermes Agent architecture:
- Decoupled async gateway (non-blocking event router)
- Persistent session state (survives restarts)
- Streaming progress (edit-in-place, no message flooding)
- Resilient chunking (smart 4096-char split + file fallback)
"""

__version__ = "2.0.0"
