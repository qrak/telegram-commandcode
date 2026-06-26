"""
MediaHandler — download, transcribe, and auto-attach for Telegram media.

Handles photos, documents, and voice messages sent to the bot.
Voice transcription requires OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Optional

from telegram.ext import ContextTypes

from telegram_commandcode.chunking import find_file_paths

if TYPE_CHECKING:
    from .gateway import BotGateway

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(gettempdir()) / "telegram-cmd"


class MediaHandler:
    """Download, transcribe, and auto-attach media files."""

    def __init__(self, gateway: "BotGateway"):
        self.gw = gateway
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── Download ───────────────────────────────────────────────────────

    async def download(
        self, context: ContextTypes.DEFAULT_TYPE, file_id: str, ext: str = "",
    ) -> Optional[Path]:
        """Download a Telegram file by file_id. Returns local path or None."""
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

    # ── Voice transcription ────────────────────────────────────────────

    @staticmethod
    async def transcribe_voice(file_path: Path) -> Optional[str]:
        """Transcribe a voice message via OpenAI Whisper (needs API key)."""
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

    # ── Auto-attach files from output ──────────────────────────────────

    async def auto_send_files(
        self, chat_id: int, output_text: str, *, bot=None,
    ) -> None:
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
                logger.debug(
                    "Auto-send file failed for %s: %s",
                    item.get("path"), e,
                )
