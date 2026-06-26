"""
Async Gateway — Decoupled event router for telegram-commandcode.

The gateway acts as a thin, non-blocking router between PTB updates and
Command Code execution. It never blocks on long-running CLI calls — those
are dispatched as background asyncio.Tasks.

Architecture:
  1. PTB Handler receives Update
  2. Gateway routes to command handler or enqueues work
  3. Per-chat TaskGroup ensures sequential execution per user
  4. Reactions + edit-in-place status messages provide streaming UX
  5. Rate limiting and access control at the gateway layer
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from tempfile import gettempdir
from typing import Optional

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from .session import session_store, ChatSession
from .executor import (
    run_cmd,
    ExecOptions,
    process_tracker,
    DEFAULT_CMD_BIN,
    DEFAULT_YOLO,
    DEFAULT_MAX_TURNS,
)
from .formatter import escape_md2
from .chunking import SmartSplitter, find_file_paths
from .commands import handle_command

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "any").split(",")
MAX_PROMPT_LENGTH = 5000
RATE_LIMIT_WINDOW = 2.0  # seconds between successive prompts from same user

splitter = SmartSplitter()
DOWNLOAD_DIR = Path(gettempdir()) / "telegram-cmd"

# ── Per-chat processing gate ─────────────────────────────────────────────────

# Ensures sequential execution per chat — only one prompt runs at a time per user.
_processing: set[str] = set()
_chat_locks: dict[str, asyncio.Lock] = {}

# Rate limiting: user_id → last message timestamp (monotonic)
_rate_limits: dict[int, float] = {}

# Bot identity (cached from getMe)
_bot_username: Optional[str] = None
_bot_id: Optional[int] = None


def _get_chat_lock(chat_id: str) -> asyncio.Lock:
    """Get or create a per-chat async lock for sequential processing."""
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


def _is_allowed(user_id: int | None) -> bool:
    """Check access control."""
    if not user_id:
        return False
    if "any" in [a.strip() for a in ALLOWED_USERS]:
        return True
    return str(user_id) in [a.strip() for a in ALLOWED_USERS]


# ── Media handling ──────────────────────────────────────────────────────────

async def _download_telegram_file(file_id: str, ext: str = "") -> Optional[Path]:
    """Download a file from Telegram by file_id. Returns local path or None."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # This requires access to the bot instance — we use the update's context
        # Since _download is called from within a handler, we need to pass the bot.
        # We'll handle this differently — the caller passes the bot/context.
        pass
    except Exception as e:
        logger.warning("File download failed: %s", e)
    return None


# ── Group chat detection ────────────────────────────────────────────────────

def _is_bot_mentioned(msg_text: str, entities: list | None, bot_username: str | None) -> bool:
    """Check if the bot is @mentioned in the message entities."""
    if not entities or not bot_username:
        return False
    for e in entities:
        if hasattr(e, 'type') and str(e.type) == "mention":
            # Extract mention text
            if hasattr(e, 'offset') and hasattr(e, 'length'):
                mention = msg_text[e.offset:e.offset + e.length]
                if mention.lower() == f"@{bot_username.lower()}":
                    return True
    return False


def _is_reply_to_bot(reply_msg, bot_id: int | None) -> bool:
    """Check if the message is a reply to a bot message."""
    if not reply_msg or not bot_id:
        return False
    return getattr(getattr(reply_msg, 'from_user', None), 'id', None) == bot_id


# ── Message sending helpers (with parse fallback) ────────────────────────────

async def _send_message_safe(
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    *,
    bot=None,
    **kwargs,
):
    """
    Send a message with MarkdownV2, auto-falling back to plain text on parse error.
    Auto-splits long messages via SmartSplitter.
    """
    if not bot:
        return None

    # Pre-split for length
    chunks = splitter.split(text)
    last_msg = None

    for chunk in chunks.chunks:
        try:
            last_msg = await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                link_preview_options={"is_disabled": True},
                **kwargs,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "can't parse entities" in err_str and parse_mode:
                # Retry without formatting
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    link_preview_options={"is_disabled": True},
                    **kwargs,
                )
            elif "message is too long" in err_str:
                # Try with even smaller chunks
                small_splitter = SmartSplitter(max_chars=3000)
                for sub in small_splitter.split(chunk).chunks:
                    last_msg = await bot.send_message(
                        chat_id=chat_id, text=sub, **kwargs,
                    )
            else:
                raise

    return last_msg


async def _edit_message_safe(
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    *,
    bot=None,
) -> bool:
    """
    Edit a message with MarkdownV2, falling back to plain text or new message.
    Returns True if the edit succeeded.
    """
    if not bot:
        return False

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            link_preview_options={"is_disabled": True},
        )
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str and parse_mode:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    link_preview_options={"is_disabled": True},
                )
                return True
            except Exception:
                pass
        elif "message can't be edited" in err_str:
            # Message too old to edit → send new one
            await _send_message_safe(chat_id, text, parse_mode=None, bot=bot)
            return True
        elif "message is not modified" in err_str:
            return True  # Same content, not an error
        logger.debug("Edit failed for msg %d: %s", message_id, err_str)
    return False


async def _set_reaction(chat_id: int, message_id: int, emoji: str, *, bot=None) -> None:
    """Set a reaction emoji on a message (best-effort)."""
    if not bot:
        return
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
            is_big=False,
        )
    except Exception:
        pass  # Reactions are cosmetic, failures are non-fatal


# ── Auto-attach files from output ────────────────────────────────────────────

async def _auto_send_files(chat_id: int, output_text: str, *, bot=None) -> None:
    """Scan output for MEDIA: paths and send them as attachments."""
    if not bot:
        return
    paths = find_file_paths(output_text)
    for item in paths:
        try:
            filepath = item["path"]
            if item["type"] == "photo":
                with open(filepath, "rb") as f:
                    await bot.send_photo(chat_id=chat_id, photo=f)
            else:
                with open(filepath, "rb") as f:
                    await bot.send_document(chat_id=chat_id, document=f)
        except Exception as e:
            logger.debug("Auto-send file failed for %s: %s", item.get("path"), e)


# ═══════════════════════════════════════════════════════════════════════════
# Core message handler — dispatched by PTB
# ═══════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Entry point for all Telegram messages.

    This is the gateway — it routes, authorizes, rate-limits, and dispatches.
    Never blocks — heavy work is spawned as background tasks.
    """
    global _bot_username, _bot_id

    msg = update.message or update.edited_message
    if not msg:
        return

    chat_id = msg.chat_id
    chat_type = msg.chat.type
    user_id = msg.from_user.id if msg.from_user else None
    user_msg_id = msg.message_id
    username = (
        msg.from_user.username or msg.from_user.first_name or "unknown"
        if msg.from_user
        else "unknown"
    )
    text = (msg.text or msg.caption or "").strip()

    # Lazy bot identity
    if _bot_username is None and context.bot:
        me = await context.bot.get_me()
        _bot_username = me.username
        _bot_id = me.id

    # ── Group chat: only respond when @mentioned or replied to ──
    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        is_mentioned = _is_bot_mentioned(
            msg.text or "",
            getattr(msg, 'entities', None) or getattr(msg, 'caption_entities', None),
            _bot_username,
        )
        is_reply = _is_reply_to_bot(msg.reply_to_message, _bot_id)
        if not is_mentioned and not is_reply:
            return

    # ── Access control ──
    if not _is_allowed(user_id):
        logger.info("⛔ Blocked message from user %s (%s)", user_id, username)
        await _send_message_safe(
            chat_id,
            "⛔ Sorry, you are not authorized to use this bot\\.",
            bot=context.bot,
        )
        return

    logger.info("📩 [%s/%s] [%s] %s", username, user_id, chat_type, (text or "(media)")[:80])

    # ── Rate limiting ──
    now = asyncio.get_event_loop().time()
    if user_id:
        last = _rate_limits.get(user_id, 0)
        if now - last < RATE_LIMIT_WINDOW:
            logger.info("⏱️ Rate-limited user %s (%s)", user_id, username)
            await _set_reaction(chat_id, user_msg_id, "⏱️", bot=context.bot)
            return
        _rate_limits[user_id] = now

    # ── Prompt length validation ──
    if text and len(text) > MAX_PROMPT_LENGTH:
        await _send_message_safe(
            chat_id,
            f"⚠️ Prompt too long ({len(text)} chars)\\. Maximum allowed: {MAX_PROMPT_LENGTH} chars\\.",
            bot=context.bot,
        )
        return

    # ── Handle media messages (photos, documents, voice) ──
    media_prompt = None
    media_desc = ""

    if msg.photo:
        photo = msg.photo[-1]  # Largest size
        local_path = await _download_media(context, photo.file_id, ".jpg")
        if local_path:
            media_prompt = (
                f"User sent a photo (saved at {local_path})"
                f"{f'. Caption: {text}' if text else ''}. "
                "Review any text visible in the image and respond appropriately."
            )
            media_desc = "📷 Photo"

    elif msg.document:
        doc = msg.document
        ext = f".{doc.file_name.split('.')[-1]}" if doc.file_name else ""
        local_path = await _download_media(context, doc.file_id, ext)
        if local_path:
            media_prompt = (
                f'User sent a file "{doc.file_name or "unnamed"}" (saved at {local_path})'
                f"{f'. Message: {text}' if text else ''}. "
                "Read and process the file if needed."
            )
            media_desc = "📄 File"

    elif msg.voice:
        voice = msg.voice
        local_path = await _download_media(context, voice.file_id, ".ogg")
        if local_path:
            transcription = await _transcribe_voice(local_path)
            if transcription:
                media_prompt = (
                    f'User sent a voice message. Transcription: "{transcription}". '
                    "Respond to the content."
                )
                media_desc = "🎤 Voice"
            else:
                media_prompt = (
                    f"User sent a voice message (saved at {local_path}). "
                    "The message could not be transcribed — let the user know."
                )
                media_desc = "🎤 Voice (untranscribed)"

    if media_prompt:
        await _enqueue_and_process(
            context, chat_id, user_msg_id, media_prompt, media_desc
        )
        return

    # ── Text messages only from here ──
    if not text:
        return

    # ── Slash commands ──
    if text.startswith("/"):
        try:
            result = await handle_command(update, context)
        except Exception as e:
            logger.error("Command handler crashed on %s: %s", text.split()[0] if text else "?", e)
            await _send_message_safe(
                chat_id,
                f"❌ Internal error processing command\\.\n\n`{escape_md2(str(e)[:200])}`",
                bot=context.bot,
            )
            return
        if result is None:
            return  # Command fully handled (response sent by handler)
        if result:
            # Handler returned a prompt string → execute it
            await _enqueue_and_process(context, chat_id, user_msg_id, result)
        return

    # ── Regular prompt ──
    await _enqueue_and_process(context, chat_id, user_msg_id, text)


async def _download_media(context: ContextTypes.DEFAULT_TYPE, file_id: str, ext: str = "") -> Optional[Path]:
    """Download a Telegram media file. Returns local path or None."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        file_info = await context.bot.get_file(file_id)
        if not file_info or not file_info.file_path:
            return None

        local_name = f"{int(asyncio.get_event_loop().time())}_{file_id[:8]}{ext}"
        local_path = DOWNLOAD_DIR / local_name
        await file_info.download_to_drive(custom_path=str(local_path))
        return local_path if local_path.exists() else None
    except Exception as e:
        logger.warning("Media download failed: %s", e)
        return None


async def _transcribe_voice(file_path: Path) -> Optional[str]:
    """Transcribe a voice message using OpenAI Whisper (requires OPENAI_API_KEY)."""
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files={"file": ("voice.ogg", f, "audio/ogg")},
                    data={"model": "whisper-1"},
                )
            if response.status_code == 200:
                data = response.json()
                return data.get("text", "").strip() or None
    except Exception as e:
        logger.warning("Voice transcription failed: %s", e)
    return None


async def _enqueue_and_process(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_msg_id: int,
    prompt: str,
    media_desc: str = "",
) -> None:
    """
    Enqueue a prompt for processing and spawn a background task.
    Uses per-chat asyncio.Lock for sequential execution.
    """
    lock = _get_chat_lock(str(chat_id))

    # Spawn as background task — never block the handler
    async def _safe_process():
        try:
            await _process_with_lock(
                context, chat_id, user_msg_id, prompt, lock, media_desc
            )
        except Exception as e:
            logger.error("Background task crashed for chat %s: %s", chat_id, e)
            try:
                await _send_message_safe(
                    chat_id,
                    f"❌ Background task error: {escape_md2(str(e)[:300])}",
                    bot=context.bot,
                )
            except Exception:
                pass

    asyncio.create_task(_safe_process())


async def _process_with_lock(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_msg_id: int,
    prompt: str,
    lock: asyncio.Lock,
    media_desc: str = "",
) -> None:
    """
    Process a single prompt for a chat, holding the per-chat lock.
    Sequential within a chat, concurrent across chats.
    """
    async with lock:
        await _process_prompt(context, chat_id, user_msg_id, prompt, media_desc)


async def _process_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_msg_id: int,
    prompt: str,
    media_desc: str = "",
) -> None:
    """
    Execute a prompt through Command Code with Hermes-style UX:

    1. React 👀 on user's message
    2. Send placeholder "Processing..." message
    3. Edit in-place with progress
    4. Final edit with result (or error)
    5. React ✅ or ❌
    6. Auto-send detected files
    7. Drain queued prompts
    """
    chat_id_str = str(chat_id)
    state = session_store.get(chat_id_str)
    bot = context.bot

    # React to user's message immediately
    await _set_reaction(chat_id, user_msg_id, "👀", bot=bot)

    # Store prompt for /retry
    state.last_prompt = prompt
    session_store.update(chat_id_str, last_prompt=prompt)

    try:
        # Send initial status message
        truncated = prompt[:100]
        header = f"🚀 {media_desc}: " if media_desc else "🚀 Running: "
        status_text = f"{header}`{escape_md2(truncated)}`"
        status_msg = await _send_message_safe(chat_id, status_text, bot=bot)
        status_msg_id = status_msg.message_id if status_msg else None

        # Edit to show "thinking" progress
        if status_msg_id:
            await _edit_message_safe(
                chat_id, status_msg_id,
                f"🤔 *Processing:* {escape_md2(truncated)}...",
                bot=bot,
            )

        # Build the full prompt with goal/steer prefixes
        prefix_parts = []
        if state.goal:
            prefix_parts.append(f"[GOAL] {state.goal}")
        if state.steer:
            prefix_parts.append(f"[GUIDANCE] {state.steer}")
        final_prompt = "\n".join(prefix_parts) + "\n\n" + prompt if prefix_parts else prompt

        # Check if process is already running (interrupt)
        if process_tracker.get(chat_id_str):
            final_prompt = f"⚡ Previous execution interrupted.\n\n{final_prompt}"

        # Execute Command Code
        opts = ExecOptions(
            model=state.model,
            plan_mode=state.plan_mode,
            continue_session=state.active,
            add_dirs=list(state.add_dirs),
        )
        result = await run_cmd(final_prompt, opts, chat_id=chat_id_str)

        # Update session state
        state.active = True
        if state.one_shot_plan:
            state.plan_mode = False
            state.one_shot_plan = False
        session_store.update(
            chat_id_str,
            active=True,
            plan_mode=state.plan_mode,
            one_shot_plan=state.one_shot_plan,
        )

        # Build final response
        is_error = result.is_error
        prefix = "⚠️" if is_error else "✅"
        result_text = result.output or "(no output)"

        # Truncate very long outputs
        if len(result_text) > 3800:
            result_text = result_text[:3800] + "\n...(truncated)"

        done_msg = (
            f"{prefix} *{'Failed' if is_error else 'Done'}:* "
            f"{escape_md2(truncated)}\n\n"
            f"{escape_md2(result_text)}"
        )

        # Edit status message with result
        if status_msg_id:
            edited = await _edit_message_safe(chat_id, status_msg_id, done_msg, bot=bot)
            if not edited:
                await _send_message_safe(chat_id, done_msg, bot=bot)
        else:
            await _send_message_safe(chat_id, done_msg, bot=bot)

        # Auto-send detected files
        await _auto_send_files(chat_id, result.output, bot=bot)

        # React ✅ on user's message
        await _set_reaction(chat_id, user_msg_id, "✅" if not is_error else "❌", bot=bot)

        # Drain queued prompts
        if state.queued_prompts:
            next_prompt = state.queued_prompts.pop(0)
            session_store.update(chat_id_str, queued_prompts=list(state.queued_prompts))
            await _send_message_safe(
                chat_id,
                f"📋 *Running queued prompt:* {escape_md2(next_prompt[:100])}",
                bot=bot,
            )
            # Process recursively (same chat, new prompt)
            await _process_prompt(context, chat_id, user_msg_id, next_prompt)

    except Exception as e:
        logger.error("Process error for chat %s: %s", chat_id, e)
        error_msg = f"❌ *Error:* {escape_md2(str(e))}"
        await _send_message_safe(chat_id, error_msg, bot=bot)
        await _set_reaction(chat_id, user_msg_id, "❌", bot=bot)
