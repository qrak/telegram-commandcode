"""
MessageRouter — group chat detection, access control, command routing,
and prompt dispatching.  This is the "thin gateway" that routes incoming
Telegram updates without blocking on execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from telegram_commandcode.commands.router import CommandRouter
from telegram_commandcode.formatter import escape_md2

if TYPE_CHECKING:
    from .gateway import BotGateway

logger = logging.getLogger(__name__)

# Allowed users list from env
ALLOWED_USERS = __import__("os").getenv("TELEGRAM_ALLOWED_USERS", "any").split(",")

# Global command router instance (lightweight, stateless after init)
_command_router = CommandRouter()


class MessageRouter:
    """Per-update message routing logic.

    Instantiated fresh per update — no instance state, just a namespace
    for the routing methods.  All persistent state lives on BotGateway.
    """

    def __init__(self, gateway: "BotGateway"):
        self.gw = gateway

    async def handle(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Main entry point — dispatch based on message type."""
        msg = update.message or update.edited_message
        if not msg:
            return

        chat_id = msg.chat_id
        chat_type = msg.chat.type
        user_id = msg.from_user.id if msg.from_user else None
        user_msg_id = msg.message_id
        text = (msg.text or msg.caption or "").strip()

        # Ensure bot identity is cached
        await self.gw.ensure_bot_identity(context.bot)

        # ── Group chat: only respond when @mentioned or replied to ──
        if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if not self._should_respond_in_group(msg):
                return

        # ── Access control ──
        if not self._is_allowed(user_id):
            username = (
                msg.from_user.username
                or msg.from_user.first_name
                or "unknown"
                if msg.from_user
                else "unknown"
            )
            logger.info("⛔ Blocked message from user %s (%s)", user_id, username)
            await self.gw.sender.send_message(
                chat_id,
                "⛔ Sorry, you are not authorized to use this bot\\.",
                bot=context.bot,
            )
            return

        logger.info(
            "📩 [%s/%s] [%s] %s",
            getattr(msg.from_user, "username", "?"),
            user_id,
            chat_type,
            (text or "(media)")[:80],
        )

        # ── Rate limiting ──
        if not self.gw.check_rate_limit(user_id):
            logger.info("⏱️ Rate-limited user %s", user_id)
            await self.gw.sender.set_reaction(
                chat_id, user_msg_id, "⏱️", bot=context.bot,
            )
            return

        # ── Prompt length validation ──
        if text and len(text) > self.gw.MAX_PROMPT_LENGTH:
            await self.gw.sender.send_message(
                chat_id,
                f"⚠️ Prompt too long ({len(text)} chars)\\. Maximum allowed: {self.gw.MAX_PROMPT_LENGTH} chars\\.",
                bot=context.bot,
            )
            return

        # ── Media messages ──
        media_prompt = await self._handle_media(msg, context, text)
        if media_prompt:
            await self.gw.processor.enqueue_and_process(
                context, chat_id, user_msg_id, media_prompt,
                self._media_desc(msg),
            )
            return

        if not text:
            return

        # ── Slash commands ──
        if text.startswith("/"):
            try:
                result = await _command_router.handle_command(update, context)
            except Exception as e:
                logger.error(
                    "Command handler crashed on %s: %s",
                    text.split()[0] if text else "?", e,
                )
                await self.gw.sender.send_message(
                    chat_id,
                    f"❌ Internal error processing command\\.\n\n`{escape_md2(str(e)[:200])}`",
                    bot=context.bot,
                )
                return
            if result is None:
                return  # Fully handled
            if result:
                await self.gw.processor.enqueue_and_process(
                    context, chat_id, user_msg_id, result,
                )
            return

        # ── Regular prompt ──
        await self.gw.processor.enqueue_and_process(
            context, chat_id, user_msg_id, text,
        )

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _is_allowed(user_id: int | None) -> bool:
        if not user_id:
            return False
        if "any" in [a.strip() for a in ALLOWED_USERS]:
            return True
        return str(user_id) in [a.strip() for a in ALLOWED_USERS]

    def _should_respond_in_group(self, msg) -> bool:
        """Only respond in groups when @mentioned or replied to."""
        msg_text = msg.text or ""
        entities = (
            getattr(msg, "entities", None)
            or getattr(msg, "caption_entities", None)
        )
        is_mentioned = self._is_bot_mentioned(msg_text, entities)
        is_reply = self._is_reply_to_bot(msg.reply_to_message)
        return is_mentioned or is_reply

    def _is_bot_mentioned(self, msg_text: str, entities: list | None) -> bool:
        """Check if the bot is @mentioned in the message entities."""
        if not entities or not self.gw.bot_username:
            return False
        for e in entities:
            if hasattr(e, "type") and str(e.type) == "mention":
                if hasattr(e, "offset") and hasattr(e, "length"):
                    mention = msg_text[e.offset : e.offset + e.length]
                    if mention.lower() == f"@{self.gw.bot_username.lower()}":
                        return True
        return False

    def _is_reply_to_bot(self, reply_msg) -> bool:
        """Check if the message is a reply to a bot message."""
        if not reply_msg or not self.gw.bot_id:
            return False
        return (
            getattr(getattr(reply_msg, "from_user", None), "id", None)
            == self.gw.bot_id
        )

    async def _handle_media(
        self, msg, context: ContextTypes.DEFAULT_TYPE, text: str,
    ) -> Optional[str]:
        """Build a prompt from media messages; return None if no media."""
        if msg.photo:
            photo = msg.photo[-1]
            local_path = await self.gw.media.download(context, photo.file_id, ".jpg")
            if local_path:
                return (
                    f"User sent a photo (saved at {local_path})"
                    f"{f'. Caption: {text}' if text else ''}. "
                    "Review any text visible in the image and respond appropriately."
                )

        elif msg.document:
            doc = msg.document
            ext = f".{doc.file_name.split('.')[-1]}" if doc.file_name else ""
            local_path = await self.gw.media.download(context, doc.file_id, ext)
            if local_path:
                return (
                    f'User sent a file "{doc.file_name or "unnamed"}" (saved at {local_path})'
                    f"{f'. Message: {text}' if text else ''}. "
                    "Read and process the file if needed."
                )

        elif msg.voice:
            voice = msg.voice
            local_path = await self.gw.media.download(context, voice.file_id, ".ogg")
            if local_path:
                transcription = await self.gw.media.transcribe_voice(local_path)
                if transcription:
                    return (
                        f'User sent a voice message. Transcription: "{transcription}". '
                        "Respond to the content."
                    )
                else:
                    return (
                        f"User sent a voice message (saved at {local_path}). "
                        "The message could not be transcribed — let the user know."
                    )

        return None

    @staticmethod
    def _media_desc(msg) -> str:
        """Short human-readable media type descriptor."""
        if msg.photo:
            return "📷 Photo"
        if msg.document:
            return "📄 File"
        if msg.voice:
            return "🎤 Voice"
        return ""
