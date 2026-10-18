"""
ConfigCommands — model selection, provider switching, effort levels,
configure-models, compact-mode display/toggle.

All Lane A — local state reads/writes, zero LLM involvement. The settings
written here are also passed to every run as CLI flags, so a change takes
effect on the next prompt without restarting anything:

    /model <id>          → -m <id>
    /effort <level>      → --effort <level>
    /compact-mode <m>    → --config compact-mode=<m>
    /provider <name>     → provider key in ~/.commandcode/config.json
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import Update

from telegram_commandcode.formatter import escape_code_block, escape_md2

from .base import BaseCommandHandler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Levels documented for /effort and --effort (the model decides which apply).
VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# config.json "provider": the selected auth provider.
VALID_PROVIDERS = ("command-code", "anthropic", "github-copilot", "codex")

# config.json "compactMode" / --config compact-mode: auto-compaction strategy.
VALID_COMPACT_MODES = ("default", "fast")


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
            # Persist to CC config so TUI sessions agree
            try:
                cfg = self.read_cc_config()
                cfg["model"] = args
                self.write_cc_config(cfg)
            except Exception:
                logger.warning("Could not persist model to CC config")

            extra = ""
            if state.effort:
                extra = (
                    f"\n\n_Reasoning effort `{escape_md2(state.effort)}` stays "
                    "set and is passed to the new model\\._"
                )
            await self.send_md(
                update,
                f"✅ Switched to model: *{escape_md2(args)}*\n\n"
                f"Next prompts will use `\\-m {escape_md2(args)}`\\.{extra}",
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
            f"🤖 *Available models*\n\n```\n{escape_code_block(preview)}\n```\n"
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
                f"🔌 Current auth provider: *{escape_md2(current)}*\n\n"
                f"Available: {', '.join(f'`{p}`' for p in VALID_PROVIDERS)}\n\n"
                "Use `/provider <name>` to switch\\.\n"
                "_The provider is read when a new Command Code process starts, "
                "so the next prompt picks it up\\. Switching to anthropic or "
                "codex may need `cmd login` on the host first\\._",
            )
            return None

        provider = args.strip().lower()
        if provider not in VALID_PROVIDERS:
            await self.send_md(
                update,
                f"❌ Unknown provider `{escape_md2(provider)}`\\.\n"
                f"Valid: {', '.join(f'`{p}`' for p in VALID_PROVIDERS)}",
            )
            return None

        cfg["provider"] = provider
        self.write_cc_config(cfg)
        await self.send_md(
            update,
            f"✅ Provider set to *{escape_md2(provider)}* in "
            "`~/\\.commandcode/config\\.json`\\.\n\n"
            "The next prompt starts a new Command Code process with it\\. "
            "If that provider is not authenticated yet, run `cmd login` on the "
            "host \\(or `/status` here to check\\)\\.",
        )
        return None

    # ── /effort ────────────────────────────────────────────────────────

    async def handle_effort(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        cfg = self.read_cc_config()
        configured = (cfg.get("reasoningEffort") or {}).get(
            cfg.get("model", "default")
        )
        current = state.effort or configured

        if not args:
            model = state.model or cfg.get("model", "default")
            if current:
                await self.send_md(
                    update,
                    f"🧠 Reasoning effort for `{escape_md2(model)}`: "
                    f"*{escape_md2(current)}*\n\n"
                    f"Levels: {', '.join(f'`{lv}`' for lv in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to change it\\.",
                )
            else:
                await self.send_md(
                    update,
                    f"🧠 No effort set for `{escape_md2(model)}` \\(model default\\)\\.\n\n"
                    f"Levels: {', '.join(f'`{lv}`' for lv in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to set it\\.",
                )
            return None

        level = args.strip().lower()
        if level not in VALID_EFFORT_LEVELS:
            await self.send_md(
                update,
                f"❌ Invalid effort level\\. Valid: "
                f"{', '.join(f'`{lv}`' for lv in VALID_EFFORT_LEVELS)}",
            )
            return None

        # Passed per run as --effort, and persisted for TUI sessions too.
        self.update_state(chat_id, effort=level)
        try:
            model_key = state.model or cfg.get("model", "default")
            cfg.setdefault("reasoningEffort", {})
            cfg["reasoningEffort"][model_key] = level
            self.write_cc_config(cfg)
        except Exception:
            logger.warning("Could not persist reasoning effort to CC config")

        await self.send_md(
            update,
            f"✅ Reasoning effort *{escape_md2(level)}*\\.\n\n"
            "Next prompts will pass `\\-\\-effort "
            f"{escape_md2(level)}` to Command Code\\.",
        )
        return None

    # ── /configure-models ──────────────────────────────────────────────

    async def handle_configure_models(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat = update.effective_chat
        if chat:
            await chat.send_chat_action(action="typing")

        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)
        cfg = self.read_cc_config()
        current_model = state.model or cfg.get("model", "default")
        current_provider = cfg.get("provider", "command-code")

        effort_map = cfg.get("reasoningEffort") or {}
        if isinstance(effort_map, dict) and effort_map:
            effort_lines = "\n".join(
                f"  • `{escape_md2(str(k))}` → `{escape_md2(str(v))}`"
                for k, v in list(effort_map.items())[:10]
            )
            effort_section = f"\n*Reasoning effort:*\n{effort_lines}\n"
        else:
            effort_section = "\n_No reasoning effort configured\\._\n"

        try:
            models_out = await self.run_cli(["--list-models"], timeout=15)
        except Exception:
            models_out = "(could not fetch models)"

        msg = (
            f"⚙️ *Model Configuration*\n\n"
            f"• Provider: `{escape_md2(current_provider)}`\n"
            f"• Model: `{escape_md2(current_model)}`\n"
            f"• Effort: `{escape_md2(state.effort or 'model default')}`\n"
            f"• Compact mode: `{escape_md2(state.compact_mode or 'default')}`\n"
            f"{effort_section}\n"
            f"*Available models:*\n```\n{escape_code_block(models_out[:800])}\n```\n"
            f"\\- Use `/model <name>` to switch the model\n"
            f"\\- Use `/provider <name>` to switch the auth provider\n"
            f"\\- Use `/effort <level>` to set reasoning effort\n"
            f"\\- Use `/compact\\-mode <default|fast>` for auto\\-compaction"
        )
        await self._send_chunked(update, msg)
        return None

    # ── /compact-mode ──────────────────────────────────────────────────

    async def handle_compact_mode(
        self, update: Update, context, args: str,
    ) -> Optional[str]:
        chat_id = str(update.effective_chat.id)
        state = self.get_state(chat_id)

        if not args:
            current = state.compact_mode or self.read_cc_config().get(
                "compactMode", "default"
            )
            await self.send_md(
                update,
                f"🗜️ *Compact mode:* `{escape_md2(current)}`\n\n"
                f"Available: {', '.join(f'`{m}`' for m in VALID_COMPACT_MODES)}\n\n"
                "• `default` — nothing until 90% of the window, then summarize\n"
                "• `fast` — also trims old tool output at 50% and 80%\n\n"
                f"Use `/compact\\-mode <mode>` to change it\\.",
            )
            return None

        mode = args.strip().lower()
        if mode not in VALID_COMPACT_MODES:
            await self.send_md(
                update,
                f"❌ Invalid mode `{escape_md2(mode)}`\\. "
                f"Use: {', '.join(f'`{m}`' for m in VALID_COMPACT_MODES)}",
            )
            return None

        self.update_state(chat_id, compact_mode=mode)
        try:
            cfg = self.read_cc_config()
            cfg["compactMode"] = mode
            self.write_cc_config(cfg)
        except Exception:
            logger.warning("Could not persist compact mode to CC config")

        await self.send_md(
            update,
            f"🗜️ Compact mode set to *{escape_md2(mode)}*\\.\n\n"
            f"Every run now passes `\\-\\-config compact\\-mode={escape_md2(mode)}`, "
            "and the setting is saved for the TUI too\\.",
        )
        return None
