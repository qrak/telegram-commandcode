"""
PromptCommands — commands that either return prompts for LLM execution
or manage prompt-related state (goal, steer, queue, background).

Lane C: /review, /init, /memory, /pr-comments, /cmd, /background, /retry
Lane A (toggle/read): /plan, /goal, /steer, /queue
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_md2
from telegram_commandcode.executor import ExecOptions, run_cmd

from .base import BaseCommandHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


class PromptCommands(BaseCommandHandler):
    """Handlers for: /background, /review, /plan, /goal, /steer, /cmd,
    /init, /memory, /retry, /queue, /pr-comments."""

    COMMANDS = {
        "/background": "handle_background",
        "/review": "handle_review",
        "/plan": "handle_plan",
        "/goal": "handle_goal",
        "/steer": "handle_steer",
        "/cmd": "handle_cmd",
        "/init": "handle_init",
        "/memory": "handle_memory",
        "/retry": "handle_retry",
        "/queue": "handle_queue",
        "/pr-comments": "handle_pr_comments",
    }

    # ── /background ────────────────────────────────────────────────────

    async def handle_background(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        if not args:
            await self.send_md(
                update,
                "Usage: `/background <prompt>` — run a task in the background\\.\n\n"
                "You'll be notified here when it completes\\.",
            )
            return None

        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        bg_id = f"bg_{int(datetime.now().timestamp())}"

        await self.send_md(
            update,
            f"🔄 Background task started: \"{escape_md2(args[:100])}\"\nTask ID: `{bg_id}`",
        )

        async def _bg_task() -> None:
            opts = ExecOptions(
                model=state.model,
                plan_mode=state.plan_mode,
                continue_session=state.active,
                add_dirs=list(state.add_dirs),
                timeout=1800,
            )
            result = await run_cmd(args, opts)
            out = result.output[:3800]
            status = "✅" if result.success else "⚠️"
            try:
                await self.send_md(
                    update,
                    f"{status} *Background task complete* \\({bg_id}\\)\n\n{escape_md2(out)}",
                )
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error(
                    "Failed to send bg task result: %s", exc,
                )

        asyncio.create_task(_bg_task())
        return None

    # ── /review ────────────────────────────────────────────────────────

    async def handle_review(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        pr_ref = f" #{args}" if args else ""
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        await self.send_md(update, f"🔍 Reviewing PR{escape_md2(pr_ref)}\\.\\.\\.")
        return (
            f"Review pull request{pr_ref}. Check for bugs, security issues, "
            "test gaps, and style problems."
        )

    # ── /plan ──────────────────────────────────────────────────────────

    async def handle_plan(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if args:
            # One-shot plan: /plan <task>
            self.update_state(chat_id, plan_mode=True, one_shot_plan=True)
            return args

        # Toggle
        new_mode = not state.plan_mode
        self.update_state(chat_id, plan_mode=new_mode, one_shot_plan=False)
        status = "ON ✅" if new_mode else "OFF ❌"
        msg = (
            f"📋 Plan mode: *{status}*\n\n"
            + (
                "Next prompts will run with `\\-\\-plan`\\. Use `/plan` again to disable\\.\n"
                "_Or use `/plan <task>` for a one\\-shot plan\\._"
                if new_mode
                else "Next prompts will run in normal mode\\."
            )
        )
        await self._send_chunked(update, msg)
        return None

    # ── /goal ──────────────────────────────────────────────────────────

    async def handle_goal(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            if state.goal:
                await self.send_md(
                    update,
                    f"🎯 *Current goal:*\n\n{escape_md2(state.goal)}\n\n"
                    f"_Use `/goal clear` to remove, `/goal <text>` to update\\._",
                )
            else:
                await self.send_md(
                    update,
                    "🎯 *No goal set\\.*\n\n"
                    "Use `/goal <text>` to set a standing objective the agent "
                    "works towards across turns\\.\n"
                    "Use `/goal clear` to remove it\\.\n"
                    "Use `/goal status` to check it\\.",
                )
            return None

        if args.lower() == "clear":
            self.update_state(chat_id, goal=None)
            await self.send_md(update, "🎯 Goal cleared\\.")
            return None

        if args.lower() == "status":
            await self.send_md(
                update,
                f"🎯 *Goal:* {escape_md2(state.goal)}"
                if state.goal
                else "🎯 *No goal set\\.*",
            )
            return None

        self.update_state(chat_id, goal=args)
        await self.send_md(
            update,
            f"🎯 Goal set:\n\n{escape_md2(args)}\n\n"
            "_This will be prepended to all subsequent prompts until cleared\\._",
        )
        return None

    # ── /steer ─────────────────────────────────────────────────────────

    async def handle_steer(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            if state.steer:
                await self.send_md(
                    update,
                    f"🧭 *Current steer:*\n\n{escape_md2(state.steer)}\n\n"
                    f"_Use `/steer clear` to remove it\\._",
                )
            else:
                await self.send_md(
                    update,
                    "🧭 *No steer set\\.*\n\n"
                    "Use `/steer <instruction>` to guide the AI's behavior mid\\-session\\.",
                )
            return None

        if args.lower() == "clear":
            self.update_state(chat_id, steer=None)
            await self.send_md(update, "🧭 Steer cleared\\.")
            return None

        self.update_state(chat_id, steer=args)
        await self.send_md(
            update,
            f"🧭 Steer set\\.\n\n{escape_md2(args)}\n\n"
            "It will be applied to all subsequent prompts\\. "
            "Use `/steer clear` to remove\\.",
        )
        return None

    # ── /cmd ───────────────────────────────────────────────────────────

    async def handle_cmd(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        if not args:
            await self.send_md(
                update,
                "Usage: `/cmd <prompt>` — run a prompt through Command Code",
            )
            return None
        return args

    # ── /init ──────────────────────────────────────────────────────────

    async def handle_init(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        await self.send_md(update, "📄 Initializing AGENTS\\.md\\.\\.\\.")
        return (
            "Create or update AGENTS.md for this project based on its "
            "structure, tech stack, and conventions."
        )

    # ── /memory ────────────────────────────────────────────────────────

    async def handle_memory(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        if args:
            chat = update.effective_chat
            if chat:
                await chat.send_chat_action(action="typing")
            await self.send_md(
                update,
                f"🧠 Managing memory: {escape_md2(args[:100])}\\.\\.\\.",
            )
            return (
                f"Manage Command Code memory. {args}\n\n"
                "Read AGENTS.md files if needed and make requested changes."
            )
        else:
            paths = [
                Path("/etc/.commandcode/AGENTS.md"),
                Path.home() / ".commandcode" / "AGENTS.md",
                Path.cwd() / "AGENTS.md",
                Path.cwd() / ".commandcode" / "AGENTS.md",
            ]
            found = [p for p in paths if p.exists()]
            if found:
                listing = "\n".join(
                    f"  \\- `{escape_md2(str(p))}`" for p in found
                )
                await self.send_md(
                    update,
                    f"🧠 *Memory files found:*\n{listing}\n\n"
                    "Use `/memory <instruction>` to modify memory\\.",
                )
            else:
                await self.send_md(
                    update,
                    "🧠 No memory files found\\. Use `/memory <instruction>` to create one\\.",
                )
            return None

    # ── /retry ─────────────────────────────────────────────────────────

    async def handle_retry(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if args:
            return args  # /retry <new prompt>
        if not state.last_prompt:
            await self.send_md(
                update,
                "🤷 No previous prompt to retry\\. Send a message first\\.",
            )
            return None
        return state.last_prompt

    # ── /queue ─────────────────────────────────────────────────────────

    async def handle_queue(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            if not state.queued_prompts:
                await self.send_md(
                    update,
                    "📋 Queue is empty\\. Use `/queue <prompt>` to queue a prompt for the next turn\\.",
                )
            else:
                items = "\n".join(
                    f"  {i + 1}\\. {escape_md2(p[:80])}"
                    for i, p in enumerate(state.queued_prompts)
                )
                await self.send_md(update, f"📋 *Queued prompts:*\n{items}")
            return None

        new_queue = list(state.queued_prompts) + [args]
        self.update_state(chat_id, queued_prompts=new_queue)
        await self.send_md(
            update,
            f"📋 Queued prompt ({len(new_queue)} total)\\. "
            "It will run after the current task completes\\.",
        )
        return None

    # ── /pr-comments ───────────────────────────────────────────────────

    async def handle_pr_comments(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")
        await self.send_md(
            update,
            f"🔍 Fetching PR comments{(' #' + args) if args else ''}\\.\\.\\.",
        )
        return (
            "Fetch and display all comments from the current GitHub pull request. "
            "First run gh pr view to identify the PR, then fetch and show comments."
        )
