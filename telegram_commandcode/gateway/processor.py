"""
PromptProcessor — background task enqueue, per-chat locking,
and prompt execution pipeline (the "process_with_lock" pattern).

All heavy work is spawned via asyncio.create_task — the PTB
update handler returns immediately and is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Optional, Set

from telegram import Update
from telegram.ext import ContextTypes

from telegram_commandcode.session import session_store
from telegram_commandcode.executor import (
    run_cmd,
    run_cmd_with_progress,
    ExecOptions,
    process_tracker,
)
from telegram_commandcode.formatter import escape_md2

if TYPE_CHECKING:
    from .gateway import BotGateway

logger = logging.getLogger(__name__)

# ── Backtick sanitizer for live preview code blocks ───────────────────

def _sanitize_backticks(text: str) -> str:
    """Collapse runs of 3+ backticks to 2, preventing code-block injection."""
    return re.sub(r"```+", "``", text)


class PromptProcessor:
    """Handles enqueue, lock acquisition, and prompt execution.

    Each chat gets its own asyncio.Lock — prompts within a chat are
    sequential; prompts across chats run concurrently.  Errors in
    background tasks send an apology to the user.
    """

    def __init__(self, gateway: "BotGateway"):
        self.gw = gateway
        self._pending_tasks: Set[asyncio.Task] = set()

    # ── Task lifecycle ──────────────────────────────────────────────────

    def _track_task(self, task: asyncio.Task) -> None:
        """Register a background task so we can await it on shutdown."""
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def wait_pending(self, timeout: float = 10.0) -> None:
        """Await all tracked background tasks with a timeout (for shutdown)."""
        if not self._pending_tasks:
            return
        logger.info(
            "Waiting for %d background task(s) (timeout %ds)...",
            len(self._pending_tasks), timeout,
        )
        pending = list(self._pending_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%d task(s) did not finish within %.1fs — cancelling",
                len(self._pending_tasks), timeout,
            )
            for t in list(self._pending_tasks):
                t.cancel()

    # ── Public API ─────────────────────────────────────────────────────

    async def enqueue_and_process(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_msg_id: int,
        prompt: str,
        media_desc: str = "",
    ) -> None:
        """Spawn a background task that acquires the per-chat lock and runs."""
        lock = self.gw.get_chat_lock(str(chat_id))

        async def _safe_process() -> None:
            try:
                async with lock:
                    await self._process_prompt(
                        context, chat_id, user_msg_id, prompt, media_desc,
                    )
            except Exception as e:
                logger.error(
                    "Background task crashed for chat %s: %s", chat_id, e,
                )
                try:
                    await self.gw.sender.send_message(
                        chat_id,
                        f"❌ Background task error: {escape_md2(str(e)[:300])}",
                        bot=context.bot,
                    )
                except Exception:
                    pass

        task = asyncio.create_task(_safe_process())
        self._track_task(task)

    # ── Internal pipeline ──────────────────────────────────────────────

    async def _process_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_msg_id: int,
        prompt: str,
        media_desc: str = "",
    ) -> None:
        """Execute a single prompt through Command Code with streaming UX.

        1. React 👀 on user's message
        2. Send placeholder status message
        3. Edit in-place with progress
        4. Final edit with result
        5. React ✅ or ❌
        6. Auto-send files
        7. Drain queued prompts
        """
        chat_id_str = str(chat_id)
        state = session_store.get(chat_id_str)
        bot = context.bot

        # React immediately
        await self.gw.sender.set_reaction(chat_id, user_msg_id, "👀", bot=bot)

        # Store prompt for /retry
        session_store.update(chat_id_str, last_prompt=prompt)

        # Used in error recovery (line 176, 190)
        send = self.gw.sender.send_message
        edit = self.gw.sender.edit_message

        try:
            truncated = prompt[:100]
            header = f"🚀 {media_desc}: " if media_desc else "🚀 Running: "
            status_text = f"{header}`{escape_md2(truncated)}`"
            status_msg = await send(chat_id, status_text, bot=bot)
            status_msg_id = status_msg.message_id if status_msg else None

            if status_msg_id:
                await edit(
                    chat_id, status_msg_id,
                    f"🤔 *Processing:* {escape_md2(truncated)}...",
                    bot=bot,
                )

            # Build full prompt with goal/steer prefixes
            prefix_parts = []
            if state.goal:
                prefix_parts.append(f"[GOAL] {state.goal}")
            if state.steer:
                prefix_parts.append(f"[GUIDANCE] {state.steer}")
            final_prompt = (
                "\n".join(prefix_parts) + "\n\n" + prompt
                if prefix_parts
                else prompt
            )

            if process_tracker.get(chat_id_str):
                final_prompt = f"⚡ Previous execution interrupted.\n\n{final_prompt}"

            opts = ExecOptions(
                model=state.model,
                plan_mode=state.plan_mode,
                continue_session=state.active,
                add_dirs=list(state.add_dirs),
                yolo=state.yolo,
            )

            # ── Streaming progress callback ──
            # Edits the status message in-place every ~1.5s with the
            # latest output so the user sees live progress, not a
            # frozen "Processing..." message.

            last_edit = asyncio.get_event_loop().time()
            EDIT_COOLDOWN = 1.5  # seconds between edits (Telegram rate limit)

            async def _on_progress(batch_text: str) -> None:
                nonlocal last_edit
                now = asyncio.get_event_loop().time()
                if now - last_edit < EDIT_COOLDOWN:
                    return  # Too soon — skip this edit

                if not status_msg_id:
                    return

                # Show the most recent output as a live preview
                preview = batch_text.strip()
                if not preview:
                    return

                # Build a compact live status: emoji + prefix + preview
                # Don't use escape_md2 inside a code block — it produces
                # literal backslashes the user can see. Instead, just
                # sanitize any backtick sequences that could break the block.
                safe_preview = _sanitize_backticks(preview[:1200])
                live_msg = (
                    f"🔄 *Working:* {escape_md2(truncated)}\n\n"
                    f"```\n{safe_preview}\n```"
                )
                try:
                    await edit(chat_id, status_msg_id, live_msg, bot=bot)
                    last_edit = now
                except Exception:
                    pass  # Never let a progress edit crash the pipeline

            result = await run_cmd_with_progress(
                final_prompt, opts,
                chat_id=chat_id_str,
                on_progress=_on_progress,
                batch_interval=1.5,
            )

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

            is_error = result.is_error
            prefix = "⚠️" if is_error else "✅"
            result_text = result.output or "(no output)"

            if len(result_text) > 3800:
                result_text = result_text[:3800] + "\n...(truncated)"

            done_msg = (
                f"{prefix} *{'Failed' if is_error else 'Done'}:* "
                f"{escape_md2(truncated)}\n\n{escape_md2(result_text)}"
            )

            if status_msg_id:
                edited = await edit(chat_id, status_msg_id, done_msg, bot=bot)
                if not edited:
                    await send(chat_id, done_msg, bot=bot)
            else:
                await send(chat_id, done_msg, bot=bot)

            # Auto-send detected files
            await self.gw.media.auto_send_files(chat_id, result.output, bot=bot)

            # React on user's message
            await self.gw.sender.set_reaction(
                chat_id, user_msg_id,
                "✅" if not is_error else "❌",
                bot=bot,
            )

            # Drain queued prompts
            if state.queued_prompts:
                next_prompt = state.queued_prompts.pop(0)
                session_store.update(
                    chat_id_str, queued_prompts=list(state.queued_prompts),
                )
                await send(
                    chat_id,
                    f"📋 *Running queued prompt:* {escape_md2(next_prompt[:100])}",
                    bot=bot,
                )
                await self._process_prompt(
                    context, chat_id, user_msg_id, next_prompt,
                )

        except Exception as e:
            logger.error("Process error for chat %s: %s", chat_id, e)
            error_msg = f"❌ *Error:* {escape_md2(str(e))}"
            await send(chat_id, error_msg, bot=bot)
            await self.gw.sender.set_reaction(
                chat_id, user_msg_id, "❌", bot=bot,
            )
