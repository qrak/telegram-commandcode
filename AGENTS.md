# AGENTS.md — telegram-commandcode

Async Python Telegram bot bridging Telegram ↔ Command Code CLI.
Architected after the Hermes Agent Telegram module patterns:
thin gateway, per-chat locking, streaming UX, resilient delivery.

## Codebase Tree

```
telegram_commandcode/
├── __init__.py                     # 11 lines  — version export
├── bot.py                          # 252 lines — Application startup, main()
├── session.py                      # 135 lines — ChatSession, SessionStore (persistent)
├── executor.py                     # 286 lines — ExecOptions, CmdResult, run_cmd, process tracker
├── chunking.py                     # 296 lines — truncate_message, chunk_escaped, file fallback
├── formatter.py                    # 433 lines — MD2 escaping, format_message pipeline

├── gateway/                        # Async gateway layer
│   ├── __init__.py                 # 11 lines  — re-exports BotGateway
│   ├── gateway.py                  # 96 lines  — BotGateway class (chat locks, rate limits, identity)
│   ├── sender.py                   # 335 lines — MessageSender (_send_message_safe, _edit_message_safe)
│   ├── processor.py                # 207 lines — PromptProcessor (enqueue, process_with_lock, _process_prompt)
│   ├── media.py                    # 104 lines — MediaHandler (download, transcribe, auto-attach)
│   └── router.py                   # 239 lines — MessageRouter (group detection, access control, dispatch)

└── commands/                       # Slash-command handlers
    ├── __init__.py                 # 10 lines  — re-exports CommandRouter
    ├── router.py                   # 129 lines — CommandRouter (dispatch table, handle_command)
    ├── base.py                     # 195 lines — BaseCommandHandler (shared send_md, run_cli, config I/O)
    ├── info_cmds.py                # 377 lines — /help, /start, /status, /whoami, /context, /info, /version, ...
    ├── session_cmds.py             # 179 lines — /clear, /resume, /undo, /fork, /compact, /rename, /yolo, /stop
    ├── config_cmds.py              # 241 lines — /model, /provider, /effort, /configure-models, /compact-mode
    ├── prompt_cmds.py              # 353 lines — /background, /review, /plan, /goal, /steer, /cmd, /init, /memory, /retry, /queue
    └── cli_cmds.py                 # 122 lines — /feedback, /login, /mcp, /skills, /taste, /add-dir
```

## Architecture

### Gateway Layer (`gateway/`)

`BotGateway` is the central class — it owns all per-instance state that
was previously module-level globals:

- `chat_locks` — per-chat `asyncio.Lock` for sequential processing
- `rate_limits` — per-user timestamp map for rate limiting
- `bot_username` / `bot_id` — cached from `getMe()`

Three sub-components live on it as instance attributes:

| Component | Class | Role |
|---|---|---|
| `sender` | `MessageSender` | Send/edit/reaction primitives with MarkdownV2→plain fallback, flood control, network retry |
| `processor` | `PromptProcessor` | Background task enqueue, per-chat lock acquisition, `_process_prompt` execution pipeline |
| `media` | `MediaHandler` | Download Telegram files, transcribe voice (Whisper), auto-attach `MEDIA:` paths |

`MessageRouter` is instantiated per-update (no state, just routing logic).
It handles group-chat detection, access control, and dispatches to
`CommandRouter` for slash commands or `PromptProcessor` for raw prompts.

### Command Layer (`commands/`)

`BaseCommandHandler` provides shared machinery:
- `send_md()` / `_send_chunked()` — MarkdownV2-safe message delivery
- `run_cli()` / `run_cli_and_reply()` — async subprocess runner for `cmd <subcommand>`
- `read_cc_config()` / `write_cc_config()` — Command Code config file I/O
- `get_state()` / `update_state()` / `reset_state()` — session state access

Each command category is a subclass of `BaseCommandHandler` with a
`COMMANDS` dict mapping slash names to method names:

| Module | Class | Count |
|---|---|---|
| `info_cmds.py` | `InfoCommands` | 12 (help, start, status, whoami, context, info, version, usage, update, agents, courses, reload) |
| `session_cmds.py` | `SessionCommands` | 9 (clear, new, resume, undo, fork, compact, rename, yolo, stop) |
| `config_cmds.py` | `ConfigCommands` | 7 (model, provider, effort, reasoning, reason, configure-models, compact-mode) |
| `prompt_cmds.py` | `PromptCommands` | 11 (background, review, plan, goal, steer, cmd, init, memory, retry, queue, pr-comments) |
| `cli_cmds.py` | `CliCommands` | 8 (feedback, learn-taste, login, logout, mcp, skills, taste, add-dir) |

`CommandRouter` constructs one instance of each handler, merges all
`COMMANDS` dicts into a flat dispatch table, and resolves each slash
command with a single dictionary lookup (no if/elif chain).

### Lane Classification

Commands fall into three lanes:
- **Lane A** (local, no LLM): `/model`, `/status`, `/whoami`, `/version`, `/info`, `/help`, `/agents`, `/courses`, `/configure-models`, `/compact-mode`, `/effort`, `/provider`, `/feedback`, `/login`, `/logout`, `/mcp`, `/skills`, `/taste`, `/add-dir`
- **Lane B** (state engineering, no LLM): `/clear`, `/new`, `/resume`, `/undo`, `/fork`, `/compact`, `/rename`, `/yolo`, `/stop`
- **Lane C** (LLM execution): everything else returns a prompt string → executed via `cmd -p`

### Data Flow

```
Telegram Update
  → bot.py: MessageHandler → _message_handler()
    → BotGateway.handle_message()
      → MessageRouter.handle()
        ├─ Slash command?
        │   → CommandRouter.handle_command()
        │     → BaseCommandHandler subclass method
        │       → Returns None (handled) or str (prompt for LLM)
        └─ Regular prompt / returned prompt string
            → PromptProcessor.enqueue_and_process()
              → asyncio.create_task(acquire lock → _process_prompt())
                → run_cmd(prompt, ExecOptions)
                → edit status message in-place
                → react to user message
                → drain queued prompts
```

### Key Design Decisions

1. **No globals** — `BotGateway` owns all mutable state as instance attributes.
   `session_store` and `process_tracker` are the only singletons (filesystem + OS
   process state, respectively).
2. **DRY via inheritance** — `BaseCommandHandler` eliminates the same
   `_send_chunked`/`_run_cli`/`_read_cc_config` blocks repeated 20+ times.
3. **Error boundaries everywhere** — `_process_prompt` has a try/except around
   the full pipeline. `MessageRouter.handle` wraps command routing. Background
   tasks have their own catch-and-report.
4. **MarkdownV2 with auto-fallback** — every send/edit goes through
   `_send_message_safe`/`_edit_message_safe`, which classify errors and retry
   with plain text when the parser rejects.
5. **Code-block-aware chunking** — `truncate_message` (ported from Hermes)
   never splits inside fenced or inline code, measures by UTF-16, and appends
   `(1/3)` indicators.

## Entry Point

```bash
TELEGRAM_BOT_TOKEN=*** python -m telegram_commandcode.bot
```

Or via the installed console script:
```bash
telegram-commandcode
```

Environment variables:
- `TELEGRAM_BOT_TOKEN` (required)
- `TELEGRAM_ALLOWED_USERS` (default: "any")
- `COMMAND_CODE_CMD` (default: "cmd")
- `COMMAND_CODE_YOLO` (default: "true")
- `COMMAND_CODE_MAX_TURNS` (default: 20)
- `OPENAI_API_KEY` (optional, for voice transcription)
