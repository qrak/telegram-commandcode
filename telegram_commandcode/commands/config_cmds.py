"""
ConfigCommands — model selection, provider switching, effort levels,
configure-models, compact-mode display/toggle.

All Lane A — local state reads/writes, zero LLM involvement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigCommands(BaseCommandHandler):
    """Handlers for: /model, /provider, /effort, /configure-models,
    /compact-mode."""

    COMMANDS = {
        "/model": "handle_model",
        "/provider": "handle_provider",
        "/effort": "handle_effort",
        "/reasoning": "handle_effort",
        "/reason": "handle_effort",
        "/configure-models": "handle_configure_models",
        "/compact-mode": "handle_compact_mode",
    }

    # ── /model ─────────────────────────────────────────────────────────

    async def handle_model(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if args:
            # Switch model
            self.update_state(chat_id, model=args)
            # Persist to CC config
            try:
                cfg = self.read_cc_config()
                cfg["model"] = args
                self.write_cc_config(cfg)
            except Exception:
                pass
            await self.send_md(
                update,
                f"✅ Switched to model: *{escape_md2(args)}*\n\n"
                f"Next prompts will use `\\-m {escape_md2(args)}`\\.",
            )
            return None

        # List models
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")

        try:
            models_out = await self.run_cli(["--list-models"], timeout=15)
            preview = (
                models_out[:3500] + "\n...(truncated)"
                if len(models_out) > 3500
                else models_out
            )
        except Exception:
            models_out = ""
            preview = "Could not fetch models. Run `cmd --list-models` locally."

        default_model = "unknown"
        for line in (models_out or "").splitlines():
            if "(default)" in line:
                default_model = line.split()[0] if line.split() else "unknown"
                break

        current = (
            f"\n*Currently selected:* `{escape_md2(state.model)}`\n"
            if state.model
            else f"\n*Default model:* `{escape_md2(default_model)}`\n"
        )

        await self._send_chunked(
            update,
            f"🤖 *Available models*\n\n```\n{escape_md2(preview)}\n```\n"
            f"{current}\n"
            f"_Use `/model <name>` to switch, e\\.g\\. `/model claude\\-sonnet\\-4\\-6`_",
        )
        return None

    # ── /provider ──────────────────────────────────────────────────────

    async def handle_provider(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        cfg = self.read_cc_config()
        current = cfg.get("provider", "command-code")

        if not args:
            await self.send_md(
                update,
                f"🔌 Current provider: *{escape_md2(current)}*\n\n"
                "Use `/provider <name>` to switch\\.\n"
                "_\\(Note: only locally installed providers are available\\.\\)_",
            )
            return None

        cfg["provider"] = args
        self.write_cc_config(cfg)
        await self.send_md(
            update,
            f"✅ Provider switched to *{escape_md2(args)}*\\.",
        )
        return None

    # ── /effort ────────────────────────────────────────────────────────

    async def handle_effort(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        cfg = self.read_cc_config()
        current_model = cfg.get("model", "default")
        current_effort = (cfg.get("reasoningEffort", {}) or {}).get(current_model)

        if not args:
            if current_effort:
                await self.send_md(
                    update,
                    f"🧠 Current effort for `{escape_md2(current_model)}`: *{escape_md2(current_effort)}*\n\n"
                    f"Valid levels: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to change it\\.",
                )
            else:
                await self.send_md(
                    update,
                    f"🧠 No effort set for `{escape_md2(current_model)}` (uses model default)\\.\n\n"
                    f"Valid levels: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to set it\\.",
                )
            return None

        level = args.lower()
        if level not in VALID_EFFORT_LEVELS:
            await self.send_md(
                update,
                f"❌ Invalid effort level\\. Valid: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}",
            )
            return None

        cfg.setdefault("reasoningEffort", {})
        cfg["reasoningEffort"][current_model] = level
        self.write_cc_config(cfg)
        await self.send_md(
            update,
            f"✅ Effort set to *{escape_md2(level)}* for `{escape_md2(current_model)}`\\.",
        )
        return None

    # ── /configure-models ──────────────────────────────────────────────

    async def handle_configure_models(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")

        cfg = self.read_cc_config()
        current_model = cfg.get("model", "default")
        current_provider = cfg.get("provider", "command-code")

        per_task = cfg.get("modelOverrides", cfg.get("permissions", {}))
        if isinstance(per_task, dict) and per_task:
            task_lines = "\n".join(
                f"  • `{escape_md2(str(k))}` → `{escape_md2(str(v))}`"
                for k, v in list(per_task.items())[:10]
            )
            task_section = f"\n*Per\\-task model overrides:*\n{task_lines}\n"
        else:
            task_section = "\n_No per\\-task overrides configured\\._\n"

        try:
            models_out = await self.run_cli(["--list-models"], timeout=15)
        except Exception:
            models_out = "(could not fetch models)"

        msg = (
            f"⚙️ *Model Configuration*\n\n"
            f"• Provider: `{escape_md2(current_provider)}`\n"
            f"• Default model: `{escape_md2(current_model)}`\n"
            f"{task_section}\n"
            f"*Available models:*\n```\n{escape_md2(models_out[:800])}\n```\n"
            f"\\- Use `/model <name>` to switch the default model\n"
            f"\\- Use `/provider <name>` to switch provider\n"
            f"\\- Set per\\-task overrides via CC config or the TUI\n"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /compact-mode ──────────────────────────────────────────────────

    async def handle_compact_mode(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        valid_modes = ("default", "aggressive", "gentle")

        if not args:
            current = state.compact_mode or "default"
            await self.send_md(
                update,
                f"🗜️ *Compact mode:* `{escape_md2(current)}`\n\n"
                f"Available modes: {', '.join(f'`{m}`' for m in valid_modes)}\n\n"
                f"Use `/compact\\-mode <mode>` to change\\.",
            )
            return None

        mode = args.lower()
        if mode not in valid_modes:
            await self.send_md(
                update,
                f"❌ Invalid mode: `{escape_md2(mode)}`\\. "
                f"Use: {', '.join(f'`{m}`' for m in valid_modes)}\\.",
            )
            return None

        self.update_state(chat_id, compact_mode=mode)
        await self.send_md(
            update,
            f"🗜️ Compact mode set to *{escape_md2(mode)}*\\.",
        )
        return None
