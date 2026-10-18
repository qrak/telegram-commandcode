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
from typing import TYPE_CHECKING, Set

from telegram.ext import ContextTypes

from telegram_commandcode.session import ChatSession, session_store
from telegram_commandcode.executor import (
    run_cmd_with_progress,
    ExecOptions,
)
from telegram_commandcode.formatter import escape_md2, format_message

if TYPE_CHECKING:
    from .gateway import BotGateway

logger = logging.getLogger(__name__)

# ── Backtick sanitizer for live preview code blocks ───────────────────

def _sanitize_backticks(text: str) -> str:
    """Collapse runs of 3+ backticks to 2, preventing code-block injection."""
    return re.sub(r"```+", "``", text)


# ── Prompt assembly ───────────────────────────────────────────────────

def build_final_prompt(state: ChatSession, prompt: str) -> str:
    """Combine the pending handoff brief, goal and steer with the prompt.

    The compacted-context block is injected exactly once: the run that follows
    a /compact or /undo seeds the new session with it, and the transcript then
    carries it forward on its own.
    """
    parts: list[str] = []
    if state.seed_context:
        parts.append("[COMPACTED CONTEXT]\n" + state.seed_context)
    if state.goal:
        parts.append("[GOAL] " + state.goal)
    if state.steer:
        parts.append("[GUIDANCE] " + state.steer)
    if not parts:
        return prompt
    return "\n\n".join(parts) + "\n\n" + prompt


def merge_usage(total: dict, usage: dict) -> dict:
    """Add one run's token usage into the chat's running totals."""
    merged = dict(total or {})
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = merged.get(key, 0) + value
        else:
            merged[key] = value
    return merged


def usage_footer(result) -> str:
    """Short '⏱ 8.4s · 12,345→4,100 tok' line for the result message."""
    parts: list[str] = []
    if result.duration_ms:
        parts.append(f"{result.duration_ms / 1000:.1f}s")
    usage = result.usage or {}
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    if isinstance(tokens_in, int) and isinstance(tokens_out, int):
        parts.append(f"{tokens_in:,}→{tokens_out:,} tok")
    if result.stop_reason and result.stop_reason != "end_turn":
        parts.append(str(result.stop_reason).replace("_", " "))
    if not parts:
        return ""
    return "\n\n_" + " · ".join(parts) + "_"


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

        # Store prompt for /retry and /undo
        session_store.remember(chat_id_str, prompt)

        send = self.gw.sender.send_message
        edit = self.gw.sender.edit_message

        try:
            truncated = prompt[:100]
            header = f"🚀 {media_desc}: " if media_desc else "🚀 Running: "
            # No inline code here: escape_md2 output inside `...` would render
            # its backslashes literally, and prompts often contain backticks.
            status_text = f"{header}{escape_md2(truncated)}"
            status_msg = await send(chat_id, status_text, bot=bot)
            status_msg_id = status_msg.message_id if status_msg else None

            if status_msg_id:
                await edit(
                    chat_id, status_msg_id,
                    f"🤔 *Processing:* {escape_md2(truncated)}...",
                    bot=bot,
                )

            final_prompt = build_final_prompt(state, prompt)

            opts = ExecOptions(
                model=state.model,
                effort=state.effort,
                plan_mode=state.plan_mode,
                continue_session=state.active,
                resume_session=state.resume_target,
                fork_session=bool(state.fork_next),
                session_name=state.session_name,
                compact_mode=state.compact_mode,
                add_dirs=list(state.add_dirs),
                yolo=state.yolo,
            )

            # ── Streaming progress callback ──
            # Edits the status message in-place every ~1.5s with the
            # latest activity so the user sees live progress, not a
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

                preview = batch_text.strip()
                if not preview:
                    return

                # Don't escape inside a code block — escape_md2 would produce
                # literal backslashes. Only sanitize backtick runs.
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

            # ── Persist the outcome ──
            if state.one_shot_plan:
                state.plan_mode = False
                state.one_shot_plan = False

            updates: dict = {
                "active": True,
                "plan_mode": state.plan_mode,
                "one_shot_plan": state.one_shot_plan,
            }
            if result.session_id:
                updates["session_id"] = result.session_id
            if result.usage:
                updates["last_usage"] = dict(result.usage)
                updates["total_usage"] = merge_usage(state.total_usage, result.usage)
            if result.duration_ms is not None:
                updates["last_duration_ms"] = result.duration_ms
            if state.seed_context:
                updates["seed_context"] = None  # injected once
            if state.resume_target:
                updates["resume_target"] = None
            if state.fork_next:
                updates["fork_next"] = None
                updates["session_name"] = state.fork_next
            session_store.update(chat_id_str, **updates)

            is_error = result.is_error
            prefix = "⚠️" if is_error else "✅"
            label = "Failed" if is_error else "Done"
            if is_error and result.exit_code != 0:
                label = f"Failed · {result.human_reason}"

            result_text = result.output or "(no output)"
            body = format_message(result_text)
            if len(body) > 3800:
                body = body[:3800] + "\n…(truncated)"

            done_msg = (
                f"{prefix} *{escape_md2(label)}:* "
                f"{escape_md2(truncated)}\n\n{body}{usage_footer(result)}"
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
