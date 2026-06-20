# telegram-commandcode

**Full Telegram ↔ Command Code bridge** — control your AI coding agent from Telegram and get notifications back.

```
           Telegram
               ↕
    ┌──────────┴──────────┐
    │  bot.js (daemon)    │  Telegram → Command Code
    │  index.js (MCP)     │  Command Code → Telegram
    └──────────┬──────────┘
               ↕
        command code CLI
```

## Two Modes

| Mode | File | Direction | What it does |
|---|---|---|---|
| **MCP Server** | `index.js` | CC → Telegram | Command Code agent sends you messages, files, photos |
| **Bot Daemon** | `bot.js` | Telegram → CC | You type prompts on Telegram → Command Code executes → result back to Telegram |

---

## Mode 1: Bot Daemon (Telegram → Command Code)

Control Command Code from Telegram like you're sitting at the terminal.

### Setup

```bash
# 1. Get bot token from @BotFather
# 2. Create .env file (or export TELEGRAM_BOT_TOKEN)
# 3. Start the daemon
TELEGRAM_ALLOWED_USERS=any node bot.js
```

Or via npx:
```bash
TELEGRAM_ALLOWED_USERS=any npx telegram-commandcode-bot
```

### Env vars

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather (or `.env` file) |
| `TELEGRAM_ALLOWED_USERS` | `any` | Comma-separated user IDs for access control |
| `COMMAND_CODE_CMD` | `cmd` | Path to Command Code binary |
| `COMMAND_CODE_YOLO` | `true` | `false` → read-only mode (no file writes/shell) |
| `COMMAND_CODE_MAX_TURNS` | `20` | Max conversation turns per prompt |
| `OPENAI_API_KEY` | *(optional)* | Required for voice message transcription (Whisper API) |

Both `index.js` and `bot.js` auto-load `TELEGRAM_BOT_TOKEN` from a `.env` file in the current directory or script directory — no need to export it manually.

The daemon supports **multiple concurrent users** — each user gets their own session state (model selection, plan mode, conversation context).

### Features

| Feature | Description |
|---|---|
| **👀 Reactions** | Bot reacts with 👀 while processing, ✅ on success, ❌ on error |
| **📷 Photo reception** | Send photos to the bot — they're downloaded and the path is passed to `cmd` |
| **📄 File reception** | Send documents — same flow, downloaded to `/tmp/telegram-cmd/` |
| **🎤 Voice transcription** | Voice messages transcribed via OpenAI Whisper (requires `OPENAI_API_KEY`) |
| **📎 Auto-send files** | File paths in `cmd` output are automatically uploaded as Telegram attachments |
| **👥 Group chat** | Bot responds when @mentioned or replied to in groups |
| **✏️ Single-message editing** | Status message is edited in-place with the final result — no chat clutter |
| **🔄 Session chaining** | `/resume` continues previous context, `/clear` starts fresh |

### Slash Commands

Type `/` in the Telegram message box — **all 27 Command Code commands** are registered:

**🟢 CLI-mapped (run directly)**

| Command | Action | Maps to |
|---|---|---|
| `/feedback <msg>` | Submit feedback | `cmd feedback` |
| `/learntaste` | Learn taste from other agents | `cmd learn-taste` |
| `/login` | Authenticate | `cmd login` |
| `/logout` | Remove auth | `cmd logout` |
| `/mcp [list/add/remove]` | Manage MCP servers | `cmd mcp` |
| `/skills [list/add/remove]` | Manage skills | `cmd skills` |
| `/taste [list/push/pull]` | Manage taste | `cmd taste` |
| `/status` | Version, auth, session info | `cmd whoami` + `cmd --version` |
| `/model [name]` | List models or `/model claude-sonnet-4-6` to switch | `cmd --list-models` or `cmd -m <name>` |
| `/resume` | Continue last session | `cmd -p --continue` |
| `/clear` | Fresh session (forget context) | drops `--continue` |
| `/plan [task]` | Toggle plan mode or `/plan <task>` for one-shot | `cmd -p --plan` |
| `/review <PR#>` | Review a pull request | `cmd -p` prompt |
| `/init` | Create AGENTS.md | `cmd -p` prompt |
| `/cmd <prompt>` | Explicit prompt alias | `cmd -p` prompt |

**ℹ️ TUI-only (informational)**

`/agents` · `/compact` · `/effort` · `/ide` · `/memory` · `/pr-comments` · `/provider` · `/rewind` · `/terminal-setup` · `/add-dir`

Bot politely explains these require interactive TUI mode.

**⛔ N/A remotely**

`/exit` · `/share` · `/unshare`

### Usage

```
You (Telegram):  Build a CLI that tells the date, using TypeScript
Bot:             🚀 Running: `Build a CLI that tells the date...`
                 ✅ Done: Built date-cli with TypeScript, tsup, vitest...

You:             /resume
Bot:             🔄 Resuming last headless session...
                 📋 *Session resumed:* The previous task was building a date CLI...

You:             /clear
Bot:             🧹 Session cleared. Next prompt starts fresh.

You:             /status
Bot:             🔧 Command Code Status
                   Binary: `cmd`
                   Auth: qrak
                   Session: active (use /resume, /clear)
                   YOLO mode: on (all tools)
                   Max turns: 20
```

### How it works

1. Daemon polls Telegram via `getUpdates` (long polling, 30s timeout)
2. Incoming message → bot adds 👀 reaction, sends a status message
3. `cmd -p "prompt" --yolo --max-turns 20` runs headless
4. Status message is **edited in place** with the final result (no duplicate messages)
5. Reaction changes to ✅ (success) or ❌ (error)
6. File paths detected in output are auto-sent as Telegram attachments
7. Session chaining via `cmd -p --continue` (context preserved between messages)
8. `/clear` drops `--continue` → fresh session

---

## Mode 2: MCP Server (Command Code → Telegram)

Let your Command Code agent send you notifications while it works.

### Register in Command Code

```bash
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=*** \
  -e TELEGRAM_DEFAULT_CHAT_ID=1141080547 \
  -- npx github:qrak/telegram-commandcode
```

### MCP Tools

| Tool | What it does |
|---|---|
| `telegram_send_message` | Send a text message (MarkdownV2 or HTML, auto-fallback to plain text on parse error) |
| `telegram_send_photo` | Send a photo (URL or local file) |
| `telegram_send_file` | Send any file/document |
| `telegram_get_updates` | Read recent incoming messages (with offset tracking — no duplicates) |
| `telegram_send_reaction` | Set emoji reaction on a message (👀 ✅ ❌ 👍 ❤️) |
| `telegram_download_file` | Download a file from Telegram by file_id, returns local path |
| `telegram_health` | Check bot connection: name, username, status |

### Usage

```
> Run the build, and if it passes send "✅ Build OK" to Telegram

  [Command Code builds...]
  Using telegram tool: telegram_send_message
  → You get notified on your phone 📱
```

---

## Running Both

Use both modes for full two-way interaction:

```bash
# Terminal 1 — Telegram → Command Code daemon
TELEGRAM_BOT_TOKEN=*** node bot.js

# Terminal 2 — Register MCP in your project
cd my-project
cmd mcp add telegram -e TELEGRAM_BOT_TOKEN=*** -- node index.js
cmd  # start interactive session
```

Now you can:
- Send prompts from your phone → executed by `cmd`
- Agent sends you notifications when done
- `/resume` to continue sessions from anywhere

---

## Quick Install

```bash
# Clone
git clone https://github.com/qrak/telegram-commandcode.git
cd telegram-commandcode
npm install

# Optional: create .env from template
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN

# Bot daemon
TELEGRAM_ALLOWED_USERS=any node bot.js

# MCP server (for Command Code registration)
TELEGRAM_BOT_TOKEN=*** node index.js
```

Or via npx (without cloning):
```bash
# Bot daemon (Telegram → Command Code)
TELEGRAM_ALLOWED_USERS=any npx telegram-commandcode-bot

# MCP server (Command Code → Telegram)
cmd mcp add telegram -e TELEGRAM_BOT_TOKEN=*** -- npx telegram-commandcode
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TELEGRAM_BOT_TOKEN not set` | Export it or create `.env` file from `.env.example` |
| `cmd: command not found` | Command Code not installed: `npm i -g command-code` |
| Bot doesn't respond | Check `TELEGRAM_ALLOWED_USERS` — your user ID must be in the list |
| Session context lost | Use `/resume` (not `/clear`) to keep context between messages |
| File not found (MCP) | Use absolute paths. For project files: `/home/user/project/file.pdf` |
| Exit code 3 (auth) | Run `cmd login` on the machine first |
| Messages fail to send (MCP) | Check Telegram API limits. Retries with exponential backoff on 429/502/503 |
| Markdown formatting broken | Messages auto-fallback to plain text if Telegram rejects malformed formatting |

## License

MIT
