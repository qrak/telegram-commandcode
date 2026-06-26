"""
Slash command handlers for telegram-commandcode.

Handles all Command Code slash commands. Each handler returns True if the
command was handled (message sent), or False/None if it should fall through
to prompt execution (via the gateway).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.ext import ContextTypes

from .session import session_store, ChatSession
from .executor import (
    run_cmd,
    ExecOptions,
    process_tracker,
    DEFAULT_CMD_BIN,
    DEFAULT_YOLO,
    DEFAULT_MAX_TURNS,
    EXIT_CODE_REASONS,
)
from .formatter import escape_md2, wrap_code, bold, italic, escape_user_input
from .chunking import SmartSplitter, find_file_paths

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Map Telegram-safe command names (no hyphens) to real CC slash commands
TG_TO_CC: dict[str, str] = {
    "add_dir": "/add-dir",
    "compactmode": "/compact-mode",
    "configuremodels": "/configure-models",
    "learntaste": "/learn-taste",
    "prcomments": "/pr-comments",
    "terminalsetup": "/terminal-setup",
}

# CLI subcommand mapping — commands that forward directly to `cmd <subcommand>`
CLI_MAP: dict[str, tuple[list[str], str]] = {
    "/feedback":    (["feedback"], "📝 Submitting feedback..."),
    "/learn-taste": (["learn-taste"], "🧠 Learning taste from repositories..."),
    "/login":       (["login"], "🔑 Authenticating..."),
    "/logout":      (["logout"], "👋 Logging out..."),
    "/mcp":         (["mcp"], "🔌 Managing MCP servers..."),
    "/skills":      (["skills"], "📦 Managing skills..."),
    "/taste":       (["taste"], "🎨 Managing taste..."),
}

# TUI-only commands
TUI_ONLY: set[str] = {"/ide", "/terminal-setup", "/rewind"}

# N/A remotely commands
NA_CMDS: set[str] = {"/exit", "/share", "/unshare"}

VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

splitter = SmartSplitter()


async def _send_chunked(update: Update, text: str, parse_mode: str = "MarkdownV2") -> None:
    """Send text, splitting into chunks if needed. Falls back to plain text on parse error."""
    from telegram.constants import ParseMode
    chat = update.effective_chat
    if not chat:
        return
    result = splitter.split(text)
    for chunk in result.chunks:
        try:
            await chat.send_message(
                chunk,
                parse_mode=parse_mode,
                link_preview_options={"is_disabled": True},
            )
        except Exception as e:
            if "can't parse entities" in str(e).lower() and parse_mode:
                await chat.send_message(
                    chunk,
                    link_preview_options={"is_disabled": True},
                )
            else:
                raise

    # If file fallback was used
    if result.should_send_as_file and result.file_path:
        try:
            with open(result.file_path, "rb") as f:
                await chat.send_document(
                    document=f,
                    filename=f"cc_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                    caption="📄 Full output",
                )
        except Exception as e:
            logger.error("Failed to send file fallback: %s", e)


async def _run_cli(args: list[str], timeout: int = 30) -> str:
    """
    Run a Command Code CLI subcommand and return its stdout (or stderr on failure).

    Args like ['info'], ['--version'], ['--list-models'] etc.
    """
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            DEFAULT_CMD_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        return out or err or f"(exit {proc.returncode})"
    except asyncio.TimeoutError:
        return "⚠️ Command timed out."
    except FileNotFoundError:
        return f"❌ Command '{DEFAULT_CMD_BIN}' not found."
    except Exception as e:
        return f"❌ {e}"


def _cc_config_path() -> Path:
    return Path.home() / ".commandcode" / "config.json"


def _read_cc_config() -> dict:
    try:
        return json.loads(_cc_config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cc_config(cfg: dict) -> None:
    _cc_config_path().parent.mkdir(parents=True, exist_ok=True)
    _cc_config_path().write_text(json.dumps(cfg, indent=2))


# ═══════════════════════════════════════════════════════════════════════════
# Command handler — returns True if handled, False/None if falls through
# ═══════════════════════════════════════════════════════════════════════════

async def handle_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Handle a Telegram slash command.

    Returns True if the command was handled (response sent or queued).
    Returns False if it should be treated as a regular prompt.
    Returns None if the update should be ignored.
    """
    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    if not text.startswith("/"):
        return False

    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id if update.effective_user else None
    username = update.effective_user.username or update.effective_user.first_name or "unknown"
    chat_type = update.effective_chat.type

    parts = text.split(maxsplit=1)
    raw_cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    # Convert TG-safe names to CC slash commands
    cc_slash = TG_TO_CC.get(raw_cmd.lstrip("/"), raw_cmd)

    state = session_store.get(chat_id)

    # ── /start ──
    if cc_slash == "/start":
        msg = (
            "🤖 *Command Code Bot*\n\n"
            "I connect Telegram to your Command Code CLI\\. "
            "All CC commands are available \\(type `/` to see them\\)\\.\n\n"
            "*Send any prompt* and I'll run `cmd \\-p` \\(headless mode\\)\\.\n\n"
            "_Key commands:_\n"
            "  `/goal <text>` \\- set a standing objective\n"
            "  `/steer <text>` \\- mid\\-session guidance\n"
            "  `/model <name>` \\- switch model\n"
            "  `/effort <level>` \\- set reasoning effort\n"
            "  `/background <p>` \\- run in background\n"
            "  `/queue <p>` \\- queue for next turn\n"
            "  `/status` \\- show environment\n"
            "  `/resume` \\- resume last session"
        )
        await _send_chunked(update, msg)
        return True

    # ── /help ──
    if cc_slash == "/help":
        await _send_chunked(
            update,
            "*Command Code commands:*\n\nType `/` in the message box to see all available commands\\.\n\n"
            "_Any other message → `cmd -p` prompt_",
        )
        return True

    # ── CLI-mapped commands ──
    if cc_slash in CLI_MAP:
        cli_args_base, status_msg = CLI_MAP[cc_slash]
        # Append user's extra args if any
        cli_args = cli_args_base + (args.split() if args else ["list"])
        await update.effective_chat.send_message(escape_md2(status_msg))
        await update.effective_chat.send_chat_action(action="typing")
        output = await _run_cli(cli_args, timeout=30)
        capped = output[:3800] + "\n...(truncated)" if len(output) > 3800 else output
        await _send_chunked(update, f"```\n{escape_md2(capped)}\n```")
        return True

    # ── /status ──
    if cc_slash == "/status":
        await update.effective_chat.send_chat_action(action="typing")
        try:
            whoami = await _run_cli(["whoami"], timeout=10)
            version = await _run_cli(["--version"], timeout=10)
        except Exception:
            whoami = "unknown"
            version = "?"

        model_name = state.model
        if not model_name:
            try:
                models_out = await _run_cli(["--list-models"], timeout=15)
                m = None
                for line in models_out.splitlines():
                    if "(default)" in line:
                        m = line.split()[0] if line.split() else None
                        break
                model_name = m or "unknown"
            except Exception:
                model_name = "unknown"

        session_info = (
            "Session: active (`/resume` to continue, `/clear` to reset)"
            if state.active
            else "Session: none (send any prompt to start)"
        )
        plan_info = "Plan mode: ON" if state.plan_mode else "Plan mode: off"
        steer_info = f"Steer: {state.steer[:60]}" if state.steer else "No steer set"
        goal_info = f"Goal: {state.goal[:60]}" if state.goal else "No goal set"

        msg = (
            f"╔══ *Command Code* ══\n"
            f"╟ Model: `{escape_md2(model_name)}`{' (default)' if not state.model else ''}\n"
            f"╟ Binary: `{escape_md2(DEFAULT_CMD_BIN)}` v{escape_md2(version)}\n"
            f"╟ Auth: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ {escape_md2(session_info)}\n"
            f"╟ {escape_md2(plan_info)} · YOLO: {'on' if DEFAULT_YOLO else 'off'} · Turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ {escape_md2(goal_info)}\n"
            f"╟ {escape_md2(steer_info)}\n"
            f"╚══ Use `/model` to switch, `/goal` to set objective, `/steer` to guide, `/clear` to reset"
        )
        await _send_chunked(update, msg)
        return True

    # ── /resume ──
    if cc_slash == "/resume":
        state.active = True
        session_store.update(chat_id, active=True)
        await update.effective_chat.send_message("🔄 Resuming last headless session...")
        return False  # Fall through to prompt execution

    # ── /clear, /new ──
    if cc_slash in ("/clear", "/new"):
        session_store.reset(chat_id)
        await update.effective_chat.send_message(
            "🧹 Session cleared\\. Model reset to default, plan mode off, goal/steer cleared\\. "
            "Next prompt starts fresh\\.",
        )
        return True

    # ── /model ──
    if cc_slash == "/model":
        if args:
            # Switch model
            session_store.update(chat_id, model=args)
            # Persist to CC config
            try:
                cfg = _read_cc_config()
                cfg["model"] = args
                _write_cc_config(cfg)
            except Exception:
                pass
            await update.effective_chat.send_message(
                f"✅ Switched to model: *{escape_md2(args)}*\n\n"
                f"Next prompts will use `\\-m {escape_md2(args)}`\\.",
            )
            return True

        # List models
        await update.effective_chat.send_chat_action(action="typing")
        try:
            models_out = await _run_cli(["--list-models"], timeout=15)
            preview = models_out[:3500] + "\n...(truncated)" if len(models_out) > 3500 else models_out
        except Exception:
            models_out = ""
            preview = "Could not fetch models. Run `cmd --list-models` locally."

        # Extract default
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

        await _send_chunked(
            update,
            f"🤖 *Available models*\n\n```\n{escape_md2(preview)}\n```\n"
            f"{current}\n"
            f"_Use `/model <name>` to switch, e\\.g\\. `/model claude\\-sonnet\\-4\\-6`_",
        )
        return True

    # ── /plan ──
    if cc_slash == "/plan":
        if args:
            # One-shot plan
            session_store.update(chat_id, plan_mode=True, one_shot_plan=True)
            return False  # Fall through to prompt execution
        # Toggle
        new_mode = not state.plan_mode
        session_store.update(chat_id, plan_mode=new_mode, one_shot_plan=False)
        status = "ON ✅" if new_mode else "OFF ❌"
        await update.effective_chat.send_message(
            f"📋 Plan mode: *{status}*\n\n"
            + (
                "Next prompts will run with `--plan`\\. Use `/plan` again to disable\\.\n"
                "_Or use `/plan <task>` for a one\\-shot plan\\._"
                if new_mode
                else "Next prompts will run in normal mode\\."
            ),
        )
        return True

    # ── /stop ──
    if cc_slash == "/stop":
        killed = await process_tracker.kill(chat_id)
        if killed:
            await update.effective_chat.send_message("🛑 Execution stopped by user\\.")
        else:
            await update.effective_chat.send_message("🤷 No active execution to stop\\.")
        return True

    # ── /retry ──
    if cc_slash == "/retry":
        if not state.last_prompt:
            await update.effective_chat.send_message(
                "🤷 No previous prompt to retry\\. Send a message first\\.",
            )
            return True
        return False  # Fall through with last_prompt

    # ── /whoami ──
    if cc_slash == "/whoami":
        pid = "active" if process_tracker.get(chat_id) else "idle"
        await update.effective_chat.send_message(
            f"*User info*\n"
            f"  ID: `{user_id or 'unknown'}`\n"
            f"  Username: @{escape_md2(str(username))}\n"
            f"  Platform: Telegram\n"
            f"  Chat type: {escape_md2(chat_type)}\n"
            f"  PID: {pid}",
        )
        return True

    # ── /background ──
    if cc_slash == "/background":
        if not args:
            await update.effective_chat.send_message(
                "Usage: `/background <prompt>` — run a task in the background\\.\n\n"
                "You'll be notified here when it completes\\.",
            )
            return True

        bg_id = f"bg_{int(datetime.now().timestamp())}"
        await update.effective_chat.send_message(
            f"🔄 Background task started: \"{escape_md2(args[:100])}\"\nTask ID: `{bg_id}`",
        )

        # Spawn background task
        import asyncio
        async def _bg_task():
            opts = ExecOptions(
                model=state.model,
                plan_mode=state.plan_mode,
                continue_session=state.active,
                add_dirs=state.add_dirs,
                timeout=1800,  # 30 min for bg tasks
            )
            result = await run_cmd(args, opts)
            out = result.output[:3800]
            status = "✅" if result.success else "⚠️"
            try:
                await update.effective_chat.send_message(
                    f"{status} *Background task complete* \\({bg_id}\\)\n\n{escape_md2(out)}",
                )
            except Exception as e:
                logger.error("Failed to send bg task result: %s", e)

        asyncio.create_task(_bg_task())
        return True

    # ── /review ──
    if cc_slash == "/review":
        pr_ref = f" #{args}" if args else ""
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message(f"🔍 Reviewing PR{escape_md2(pr_ref)}...")
        return False  # Fall through to prompt execution

    # ── /steer ──
    if cc_slash == "/steer":
        if not args:
            if state.steer:
                await update.effective_chat.send_message(
                    f"🧭 *Current steer:*\n\n{escape_md2(state.steer)}\n\n"
                    f"_Use `/steer clear` to remove it\\._",
                )
            else:
                await update.effective_chat.send_message(
                    "🧭 *No steer set\\.*\n\n"
                    "Use `/steer <instruction>` to guide the AI's behavior mid\\-session\\.",
                )
            return True
        if args.lower() == "clear":
            session_store.update(chat_id, steer=None)
            await update.effective_chat.send_message("🧭 Steer cleared\\.")
            return True
        session_store.update(chat_id, steer=args)
        await update.effective_chat.send_message(
            f"🧭 Steer set\\.\n\n{escape_md2(args)}\n\n"
            f"It will be applied to all subsequent prompts\\. Use `/steer clear` to remove\\.",
        )
        return True

    # ── /effort, /reasoning, /reason ──
    if cc_slash in ("/effort", "/reasoning", "/reason"):
        cfg = _read_cc_config()
        current_model = cfg.get("model", "default")
        current_effort = (cfg.get("reasoningEffort", {}) or {}).get(current_model)

        if not args:
            if current_effort:
                await update.effective_chat.send_message(
                    f"🧠 Current effort for `{escape_md2(current_model)}`: *{escape_md2(current_effort)}*\n\n"
                    f"Valid levels: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to change it\\.",
                )
            else:
                await update.effective_chat.send_message(
                    f"🧠 No effort set for `{escape_md2(current_model)}` (uses model default)\\.\n\n"
                    f"Valid levels: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}\n\n"
                    f"Use `/effort <level>` to set it\\.",
                )
            return True

        level = args.lower()
        if level not in VALID_EFFORT_LEVELS:
            await update.effective_chat.send_message(
                f"❌ Invalid effort level\\. Valid: {', '.join(f'`{l}`' for l in VALID_EFFORT_LEVELS)}",
            )
            return True

        cfg.setdefault("reasoningEffort", {})
        cfg["reasoningEffort"][current_model] = level
        _write_cc_config(cfg)
        await update.effective_chat.send_message(
            f"✅ Effort set to *{escape_md2(level)}* for `{escape_md2(current_model)}`\\.",
        )
        return True

    # ── /provider ──
    if cc_slash == "/provider":
        cfg = _read_cc_config()
        current = cfg.get("provider", "command-code")
        if not args:
            await update.effective_chat.send_message(
                f"🔌 Current provider: *{escape_md2(current)}*\n\n"
                f"Use `/provider <name>` to switch\\.\n"
                f"_(Note: only locally installed providers are available\\.)_",
            )
            return True
        cfg["provider"] = args
        _write_cc_config(cfg)
        await update.effective_chat.send_message(
            f"✅ Provider switched to *{escape_md2(args)}*\\.",
        )
        return True

    # ── /add-dir ──
    if cc_slash == "/add-dir":
        if not args:
            if not state.add_dirs:
                await update.effective_chat.send_message(
                    "📂 No directories added yet\\.\n\n"
                    "Use `/add\\-dir <path>` to add a directory to the workspace context\\.\n"
                    "Use `/add\\-dir clear` to remove all\\.",
                )
            else:
                dirs = "\n".join(
                    f"  {i + 1}\\. `{escape_md2(d)}`"
                    for i, d in enumerate(state.add_dirs)
                )
                await update.effective_chat.send_message(
                    f"📂 *Added directories:*\n{dirs}\n\n"
                    f"Use `/add\\-dir clear` to remove all, or add more with `/add\\-dir <path>`\\.",
                )
            return True
        if args.lower() == "clear":
            session_store.update(chat_id, add_dirs=[])
            await update.effective_chat.send_message("📂 All added directories cleared\\.")
            return True
        new_dirs = list(state.add_dirs) + [args]
        session_store.update(chat_id, add_dirs=new_dirs)
        await update.effective_chat.send_message(
            f"📂 Added directory: `{escape_md2(args)}`\n"
            f"Total: {len(new_dirs)}\\. Use `/add\\-dir clear` to remove all\\.",
        )
        return True

    # ── /pr-comments ──
    if cc_slash == "/pr-comments":
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message(f"🔍 Fetching PR comments{(' #' + args) if args else ''}...")
        return False  # Fall through to prompt execution

    # ── /compact ──
    if cc_slash == "/compact":
        await update.effective_chat.send_message(
            "ℹ️ In headless mode \\(`cmd \\-p`\\) there's no persistent conversation to compact "
            "— each prompt starts fresh\\.\n\n"
            "Use `/clear` to reset your session state \\(model, plan mode, steer\\), "
            "or just send a new prompt\\.",
        )
        return True

    # ── /memory ──
    if cc_slash == "/memory":
        if args:
            await update.effective_chat.send_chat_action(action="typing")
            await update.effective_chat.send_message(f"🧠 Managing memory: {escape_md2(args[:100])}...")
            return False  # Fall through to prompt execution
        else:
            paths = [
                Path("/etc/.commandcode/AGENTS.md"),
                Path.home() / ".commandcode" / "AGENTS.md",
                Path.cwd() / "AGENTS.md",
                Path.cwd() / ".commandcode" / "AGENTS.md",
            ]
            found = [p for p in paths if p.exists()]
            if found:
                listing = "\n".join(f"  \\- `{escape_md2(str(p))}`" for p in found)
                await update.effective_chat.send_message(
                    f"🧠 *Memory files found:*\n{listing}\n\n"
                    f"Use `/memory <instruction>` to modify memory\\.",
                )
            else:
                await update.effective_chat.send_message(
                    "🧠 No memory files found\\. Use `/memory <instruction>` to create one\\.",
                )
            return True

    # ── /agents ──
    if cc_slash == "/agents":
        agents_dir = Path.home() / ".commandcode" / "agents"
        if agents_dir.exists():
            await update.effective_chat.send_message(
                f"🤖 Agent configs stored at `{escape_md2(str(agents_dir))}`\\.\n\n"
                f"Interactive agent management \\(TUI\\) is not available remotely\\. "
                f"Describe what you want and I can help set it up via prompt\\.",
            )
        else:
            await update.effective_chat.send_message(
                "🤖 No agent configurations found\\.\n\n"
                "Interactive agent management \\(TUI\\) is not available remotely\\. "
                "Describe what you want and I can help set it up via prompt\\.",
            )
        return True

    # ── /init ──
    if cc_slash == "/init":
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message("📄 Initializing AGENTS\\.md\\.\\.\\.")
        return False  # Fall through to prompt execution

    # ── /goal ──
    if cc_slash == "/goal":
        if not args:
            if state.goal:
                await update.effective_chat.send_message(
                    f"🎯 *Current goal:*\n\n{escape_md2(state.goal)}\n\n"
                    f"_Use `/goal clear` to remove, `/goal <text>` to update\\._",
                )
            else:
                await update.effective_chat.send_message(
                    "🎯 *No goal set\\.*\n\n"
                    "Use `/goal <text>` to set a standing objective the agent works towards across turns\\.\n"
                    "Use `/goal clear` to remove it\\.\n"
                    "Use `/goal status` to check it\\.",
                )
            return True
        if args.lower() == "clear":
            session_store.update(chat_id, goal=None)
            await update.effective_chat.send_message("🎯 Goal cleared\\.")
            return True
        if args.lower() == "status":
            await update.effective_chat.send_message(
                f"🎯 *Goal:* {escape_md2(state.goal)}" if state.goal else "🎯 *No goal set\\.*"
            )
            return True
        session_store.update(chat_id, goal=args)
        await update.effective_chat.send_message(
            f"🎯 Goal set:\n\n{escape_md2(args)}\n\n"
            f"_This will be prepended to all subsequent prompts until cleared\\._",
        )
        return True

    # ── /queue ──
    if cc_slash == "/queue":
        if not args:
            if not state.queued_prompts:
                await update.effective_chat.send_message(
                    "📋 Queue is empty\\. Use `/queue <prompt>` to queue a prompt for the next turn\\.",
                )
            else:
                items = "\n".join(
                    f"  {i + 1}\\. {escape_md2(p[:80])}"
                    for i, p in enumerate(state.queued_prompts)
                )
                await update.effective_chat.send_message(f"📋 *Queued prompts:*\n{items}")
            return True
        new_queue = list(state.queued_prompts) + [args]
        session_store.update(chat_id, queued_prompts=new_queue)
        await update.effective_chat.send_message(
            f"📋 Queued prompt ({len(new_queue)} total)\\. "
            f"It will run after the current task completes\\.",
        )
        return True

    # ── /undo ──
    if cc_slash == "/undo":
        if not state.last_prompt:
            await update.effective_chat.send_message("🤷 No previous prompt to undo\\.")
            return True
        n = int(args) if args.isdigit() else 1
        await update.effective_chat.send_message(f"↩️ Undoing last {n} turn(s)\\. Re\\-running with adjusted context...")
        return False  # Fall through

    # ── /fork ──
    if cc_slash == "/fork":
        name = args or f"fork_{int(datetime.now().timestamp())}"
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message(f"🌿 Forking session as \"{escape_md2(name)}\"...")
        return False  # Fall through to prompt execution

    # ── /rename ──
    if cc_slash == "/rename":
        if not args:
            if state.session_name:
                await update.effective_chat.send_message(
                    f"📝 Current session name: {escape_md2(state.session_name)}",
                )
            else:
                await update.effective_chat.send_message(
                    "📝 No session name set\\. Use `/rename <name>` to name this session\\.",
                )
            return True
        session_store.update(chat_id, session_name=args)
        await update.effective_chat.send_message(f"📝 Session renamed to: *{escape_md2(args)}*")
        return True

    # ── /reload ──
    if cc_slash == "/reload":
        await update.effective_chat.send_message("🔄 Restarting bot... Session state will be preserved in config\\.")
        # Just exit — systemd/system manager should restart it
        import os as _os
        _os._exit(0)

    # ── /info ──
    if cc_slash == "/info":
        await update.effective_chat.send_chat_action(action="typing")
        output = await _run_cli(["info"], timeout=15)
        capped = output[:3800] + "\n...(truncated)" if len(output) > 3800 else output
        await _send_chunked(update, f"```\n{escape_md2(capped)}\n```")
        return True

    # ── /version ──
    if cc_slash == "/version":
        await update.effective_chat.send_chat_action(action="typing")
        ver = await _run_cli(["--version"], timeout=10)
        await update.effective_chat.send_message(f"📦 *Command Code* v{escape_md2(ver)}")
        return True

    # ── /usage ──
    if cc_slash == "/usage":
        await update.effective_chat.send_chat_action(action="typing")
        try:
            whoami = await _run_cli(["whoami"], timeout=10)
            version = await _run_cli(["--version"], timeout=10)
        except Exception:
            whoami = "unknown"
            version = "?"

        model_name = state.model or "default"
        await update.effective_chat.send_message(
            f"╔══ *Usage & Credits* ══\n"
            f"╟ User: {escape_md2(whoami or 'not logged in')}\n"
            f"╟ Version: v{escape_md2(version)}\n"
            f"╟ Model: `{escape_md2(model_name)}`\n"
            f"╟ Max turns: {DEFAULT_MAX_TURNS}\n"
            f"╟ YOLO: {'on' if DEFAULT_YOLO else 'off'}\n"
            f"╚══ _Detailed usage metrics require the TUI\\. Run `cmd` locally for full breakdown\\._",
        )
        return True

    # ── /update ──
    if cc_slash == "/update":
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message("⬆️ Updating Command Code...")
        output = await _run_cli(["update"], timeout=120)
        capped = output[:3800] + "\n...(truncated)" if len(output) > 3800 else output
        await _send_chunked(update, f"```\n{escape_md2(capped)}\n```")
        return True

    # ── /context ──
    if cc_slash == "/context":
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message("📊 Checking context window usage...")
        return False  # Fall through to prompt execution

    # ── /configure-models ──
    if cc_slash == "/configure-models":
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message("⚙️ Configuring model assignments...")
        return False  # Fall through to prompt execution

    # ── /compact-mode ──
    if cc_slash == "/compact-mode":
        if not args:
            await update.effective_chat.send_message(
                "🗜️ *Compact modes*\n\n"
                "Usage: `/compact\\-mode <mode>`\n\n"
                "In headless mode, compacting is handled per\\-session\\. "
                "Use `/clear` to start fresh, or send a prompt with compacting instructions\\.",
            )
            return True
        await update.effective_chat.send_chat_action(action="typing")
        await update.effective_chat.send_message(f"🗜️ Setting compact mode: {escape_md2(args)}...")
        return False  # Fall through

    # ── /courses ──
    if cc_slash == "/courses":
        await update.effective_chat.send_message(
            "📚 *Command Code Courses*\n\n"
            "[Open courses in browser](https://commandcode\\.ai/courses)\n\n"
            "_Learn how to get the most out of Command Code\\._",
        )
        return True

    # ── TUI-only ──
    if cc_slash in TUI_ONLY:
        await update.effective_chat.send_message(
            f"ℹ️ {cc_slash} is a TUI\\-only command \\(interactive terminal mode\\) "
            f"and cannot be executed remotely\\. Use it in a local `cmd` session\\.",
        )
        return True

    # ── N/A commands ──
    if cc_slash in NA_CMDS:
        await update.effective_chat.send_message(
            f"ℹ️ {cc_slash} is not applicable when using Command Code remotely via Telegram\\.",
        )
        return True

    # ── /cmd ──
    if cc_slash == "/cmd":
        if not args:
            await update.effective_chat.send_message(
                "Usage: `/cmd <prompt>` — run a prompt through Command Code",
            )
            return True
        return False  # Fall through to prompt execution

    # Unknown slash command → help
    if not args:
        await update.effective_chat.send_message(
            f"Unknown command: `{escape_md2(cc_slash)}`\\. Use /help to see available commands\\.",
        )
        return True

    return False  # Not a command → treat as regular prompt
