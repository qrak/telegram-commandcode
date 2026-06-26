# telegram-commandcode v2

**Async Python Telegram bot for Command Code CLI** — Hermes Agent architecture patterns.

Control your AI coding agent from Telegram with streaming progress, persistent sessions,
and resilient message delivery. Rewritten in Python with `python-telegram-bot` 20.x.

```
           Telegram
               ↕
    ┌──────────┴──────────┐
    │  bot.py (gateway)   │  Async Python PTB bot
    │  index.js (MCP)     │  Node.js MCP server (unchanged)
    └──────────┬──────────┘
               ↕
        command code CLI
```

## Two Modes

| Mode | File | Language | Direction | Purpose |
|---|---|---|---|---|
| **Bot Daemon** | `telegram_commandcode/bot.py` | Python | Telegram → CC | Async gateway with streaming progress |
| **MCP Server** | `index.js` | Node.js | CC → Telegram | Agent sends messages, files, photos |

## Architecture (v2)

```
telegram_commandcode/
├── bot.py         # PTB Application — entry point & lifecycle
├── gateway.py     # Async event router — non-blocking dispatch
├── commands.py    # 46+ slash command handlers
├── executor.py    # Async subprocess runner for `cmd`
├── session.py     # Persistent session state (JSON-backed)
├── formatter.py   # MarkdownV2 escaping & safe formatting
├── chunking.py    # Smart 4096-char split + file fallback
└── __init__.py
```

### Key Architectural Patterns

| Pattern | Implementation |
|---|---|
| **Decoupled Gateway** | PTB message handler is thin — never blocks. Long work dispatched via `asyncio.create_task()` in per-chat `asyncio.Lock` |
| **Persistent Sessions** | `ChatSession` dataclass stored in `~/.commandcode/telegram_sessions.json`. Survives restarts |
| **Streaming Progress** | Edit-in-place: one status message, progressively edited with stages (🤔→✅/❌). No message flooding |
| **Resilient Chunking** | `SmartSplitter`: splits at paragraph → sentence → word boundaries, capped at 4000 chars. File fallback at 15K chars |
| **Error Boundaries** | Every `send_message`/`edit_message_text` wrapped with parse-fallback. Reactions are best-effort |

## Quick Install

```bash
# Clone and install
git clone https://github.com/qrak/telegram-commandcode.git
cd telegram-commandcode
pip install -e "."

# Or with voice transcription support
pip install -e ".[voice]"
```

### Requirements

- Python 3.11+
- `python-telegram-bot[job-queue]>=20.8`
- Command Code CLI (`cmd`) installed and on PATH
- (Optional) `openai` for voice transcription

## Setup

```bash
# 1. Get bot token from @BotFather
# 2. Set environment variables
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_ALLOWED_USERS="any"  # or comma-separated user IDs

# 3. Start the bot
telegram-commandcode
# or: python -m telegram_commandcode.bot
```

### Env Vars

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | `any` | Comma-separated user IDs |
| `COMMAND_CODE_CMD` | `cmd` | Path to Command Code binary |
| `COMMAND_CODE_YOLO` | `true` | `false` → read-only mode |
| `COMMAND_CODE_MAX_TURNS` | `20` | Max conversation turns per prompt |
| `OPENAI_API_KEY` | *(optional)* | For voice message transcription |

### systemd Service (persistent)

```ini
# ~/.config/systemd/user/telegram-commandcode.service
[Unit]
Description=Telegram Command Code Bot
After=network-online.target

[Service]
Type=simple
ExecStart=/home/user/.local/bin/telegram-commandcode
Environment="TELEGRAM_BOT_TOKEN=your_token"
Environment="COMMAND_CODE_YOLO=true"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now telegram-commandcode.service
```

## Features

| Feature | Description |
|---|---|
| 👀 **Reactions** | 👀 while processing, ✅ on success, ❌ on error |
| 📷 **Photo reception** | Photos downloaded and passed to `cmd` |
| 📄 **File reception** | Documents sent to `/tmp/telegram-cmd/` |
| 🎤 **Voice transcription** | OpenAI Whisper (requires `OPENAI_API_KEY`) |
| 📎 **Auto-send files** | `MEDIA:/path` in output → auto-attached |
| 👥 **Group chat** | Responds when @mentioned or replied to |
| ✏️ **Single-message editing** | Status message edited in-place — no chat clutter |
| 🔄 **Session chaining** | `/resume` reads CC session history |
| 🎯 **Goal tracking** | `/goal <text>` sets standing objective |
| 🧭 **Mid-session steering** | `/steer <text>` guides all subsequent prompts |
| 📋 **Prompt queueing** | `/queue <prompt>` queues for next turn |
| 🔄 **Background tasks** | `/background <prompt>` runs detached |
| ⚙️ **Config persistence** | Model/provider/effort persist to `~/.commandcode/config.json` |

## Slash Commands

All 46+ commands from the Node.js version are implemented. Type `/` in Telegram to see the menu.

**CLI-mapped**: `/feedback`, `/learntaste`, `/login`, `/logout`, `/mcp`, `/skills`, `/taste`, `/info`, `/version`, `/update`

**Config & session**: `/status`, `/model`, `/effort`, `/provider`, `/add-dir`, `/goal`, `/steer`, `/plan`, `/compact-mode`, `/configure-models`, `/context`

**Session control**: `/resume`, `/clear`, `/new`, `/fork`, `/rename`, `/undo`, `/retry`, `/queue`, `/background`, `/stop`, `/reload`

**Prompt-based**: `/review`, `/init`, `/memory`, `/pr-comments`, `/agents`, `/cmd`

## MCP Server (unchanged)

The Node.js MCP server (`index.js`) remains for Command Code → Telegram direction:

```bash
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=*** \
  -e TELEGRAM_DEFAULT_CHAT_ID=1141080547 \
  -- npx github:qrak/telegram-commandcode
```

## License

MIT
