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
# 2. Start the daemon
TELEGRAM_BOT_TOKEN=*** \
TELEGRAM_ALLOWED_USERS=any \
node bot.js
```

### Env vars

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | `any` | Comma-separated user IDs for access control |
| `COMMAND_CODE_CMD` | `cmd` | Path to Command Code binary |
| `COMMAND_CODE_YOLO` | `true` | `false` → read-only mode (no file writes/shell) |
| `COMMAND_CODE_MAX_TURNS` | `20` | Max conversation turns per prompt |

### Slash Commands

Type `/` in the Telegram message box to see the command menu:

| Command | Action |
|---|---|
| `/cmd <prompt>` | Run a task through Command Code |
| `/status` | Check if `cmd` is available, session info, auth status |
| `/resume` | Continue the most recent session |
| `/clear` | Start a fresh session (forget context) |
| `/model` | List available AI models |
| `/help` | Show all commands |

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
2. Incoming message → `cmd -p "prompt" --yolo --max-turns 20`
3. `cmd -p` runs headless (non-interactive), outputs response to stdout
4. Response sent back to Telegram (auto-split for long messages)
5. Session chaining via `cmd -p --continue` (context preserved between messages)
6. `/clear` drops `--continue` → fresh session

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
| `telegram_send_message` | Send a text message (MarkdownV2 or HTML) |
| `telegram_send_photo` | Send a photo (URL or local file) |
| `telegram_send_file` | Send any file/document |
| `telegram_get_updates` | Read recent incoming messages |

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

# Bot daemon
TELEGRAM_BOT_TOKEN=*** node bot.js

# MCP server (for Command Code registration)
TELEGRAM_BOT_TOKEN=*** node index.js
```

Or via npx (for MCP mode):
```bash
cmd mcp add telegram -e TELEGRAM_BOT_TOKEN=*** -- npx github:qrak/telegram-commandcode
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

## License

MIT
