"""
BotGateway — central class owning all per-instance gateway state.

Replaces module-level globals (_rate_limits, _chat_locks, _bot_username,
_bot_id) with instance attributes.  Wires together the sender, processor,
and router components that were previously inlined in a single 782-line file.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from .sender import MessageSender
from .processor import PromptProcessor
from .media import MediaHandler

logger = logging.getLogger(__name__)


class BotGateway:
    """Central gateway owning all per-instance state.

    Attributes:
        sender:     MessageSender   — send/edit/reaction primitives
        processor:  PromptProcessor — background task enqueue + execution
        media:      MediaHandler   — download, transcribe, auto-attach

        chat_locks:  dict[str, asyncio.Lock] — per-chat sequential execution
        rate_limits: dict[int, float]        — user_id → last message time

        bot_username: Optional[str] — cached from getMe()
        bot_id:       Optional[int] — cached from getMe()
    """

    # Config defaults
    MAX_PROMPT_LENGTH = 5000
    RATE_LIMIT_WINDOW = 2.0  # seconds
    FILE_FALLBACK_THRESHOLD = 15_000
    TG_MAX_CHARS = 4096
    INDICATOR_RESERVE = 10

    def __init__(self):
        self.chat_locks: dict[str, asyncio.Lock] = {}
        self.rate_limits: dict[int, float] = {}

        self.bot_username: Optional[str] = None
        self.bot_id: Optional[int] = None

        # Sub-components
        self.sender = MessageSender(self)
        self.processor = PromptProcessor(self)
        self.media = MediaHandler(self)

    # ── Chat lock management ───────────────────────────────────────────

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get-or-create a per-chat async lock for sequential processing."""
        if chat_id not in self.chat_locks:
            self.chat_locks[chat_id] = asyncio.Lock()
        return self.chat_locks[chat_id]

    # ── Bot identity ───────────────────────────────────────────────────

    async def ensure_bot_identity(self, bot) -> None:
        """Lazy-load bot username and id from getMe()."""
        if self.bot_username is None and bot:
            me = await bot.get_me()
            self.bot_username = me.username
            self.bot_id = me.id

    # ── Rate limiting ──────────────────────────────────────────────────

    def check_rate_limit(self, user_id: int | None) -> bool:
        """Return True if the user should be allowed past rate limiting."""
        if user_id is None:
            return True
        now = asyncio.get_event_loop().time()
        last = self.rate_limits.get(user_id, 0)
        if now - last < self.RATE_LIMIT_WINDOW:
            return False
        self.rate_limits[user_id] = now
        return True

    # ── Main entry point (called by PTB handler) ───────────────────────

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Entry point for all Telegram messages — see router.py."""
        from .router import MessageRouter
        await MessageRouter(self).handle(update, context)
