# telegram-commandcode

**Telegram for your coding agent.** An MCP server that lets Claude Code, Cursor,
Codex CLI, Command Code or any other MCP client send you messages, files,
photos and progress updates — and read your replies back.

[![CI](https://github.com/qrak/telegram-commandcode/actions/workflows/ci.yml/badge.svg)](https://github.com/qrak/telegram-commandcode/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Node](https://img.shields.io/badge/node-%3E%3D18-brightgreen)](package.json)

```
   your agent ──MCP stdio──▶ telegram-commandcode ──HTTPS──▶ Telegram ──▶ your phone
        ▲                                                                     │
        └────────────────────── reply via getUpdates ◀───────────────────────┘
```

The repository contains two independent pieces:

| Piece | Direction | Language | What it does |
|---|---|---|---|
| **MCP server** (`index.js`) | agent → Telegram | Node | Seven tools: send message / photo / file, read updates, react, download, health check |
| **Bot daemon** (`telegram_commandcode/`) | Telegram → agent | Python | Chat with the local `cmd` CLI from your phone: 47 commands, streaming output, persistent sessions |

You can use either one alone. The MCP server is the part that works with every
agent; the daemon is specific to the Command Code CLI.

## Quick start — MCP server

You need a bot token from [@BotFather](https://t.me/BotFather) and your numeric
chat id (send the bot a message, then read `chat.id` from
`https://api.telegram.org/bot<TOKEN>/getUpdates`).

### Command Code

```bash
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_DEFAULT_CHAT_ID=<chat_id> \
  -- npx -y github:qrak/telegram-commandcode
```

### Claude Code

```bash
claude mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_DEFAULT_CHAT_ID=<chat_id> \
  -- npx -y github:qrak/telegram-commandcode
```

### Cursor / other MCP clients

Add a stdio server with the command `npx` and arguments
`["-y", "github:qrak/telegram-commandcode"]`, plus the two environment
variables above (`~/.cursor/mcp.json` for Cursor).

Restart the agent, then ask it to call `telegram_health`. You should get your
bot's username back.

### What the agent can do

| Tool | Purpose |
|---|---|
| `telegram_send_message` | Text, plain or formatted; 4096-char limit enforced with a clear error |
| `telegram_send_photo` | Local path or URL, up to 10 MB |
| `telegram_send_file` | Any document up to 50 MB — logs, diffs, PDFs, zips |
| `telegram_get_updates` | Read replies; only unseen messages by default |
| `telegram_send_reaction` | 👀 / ✅ / ❌ on a message |
| `telegram_download_file` | Pull a file the user sent, up to 20 MB |
| `telegram_health` | Verify the token before doing anything else |

The server retries 429/5xx with backoff, retries once without `parse_mode` when
Telegram rejects the markup, redacts the token from every error it returns,
and refuses to start silently without a token — it exposes a single
`telegram_status` tool that explains the setup instead.

## Quick start — bot daemon (Command Code only)

```bash
git clone https://github.com/qrak/telegram-commandcode.git
cd telegram-commandcode
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_ALLOWED_USERS="<your numeric user id>"
export COMMAND_CODE_WORKDIR="$HOME/projects/my-app"
telegram-commandcode
```

Then message the bot: a plain message becomes a prompt for `cmd`, and slash
commands map to the CLI — `/model`, `/plan`, `/review`, `/status`, `/stop` and
43 more. Type `/` in Telegram to see the menu.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** From @BotFather |
| `TELEGRAM_ALLOWED_USERS` | *(empty = locked)* | Comma-separated numeric ids, or `any` to opt in to open access |
| `COMMAND_CODE_CMD` | `cmd` | Path to the Command Code binary |
| `COMMAND_CODE_WORKDIR` | current directory | Where the agent runs |
| `COMMAND_CODE_YOLO` | `true` | `false` runs the agent without `--yolo` |
| `COMMAND_CODE_MAX_TURNS` | `20` | Max turns per prompt |
| `OPENAI_API_KEY` | — | Optional, enables voice transcription |

An empty allow-list is **fail-closed**: the bot replies with your numeric id
and instructions instead of executing anything. `TELEGRAM_ALLOWED_USERS=any`
is an explicit opt-in, not the default.

### Run it as a service

```bash
cp deploy/telegram-commandcode.service ~/.config/systemd/user/
# edit ExecStart / EnvironmentFile / WorkingDirectory first
systemctl --user daemon-reload
systemctl --user enable --now telegram-commandcode.service
```

## Security

This tool gives a chat app the ability to drive an agent that edits files and
runs shell commands. Read [SECURITY.md](SECURITY.md) before exposing it:

- set `TELEGRAM_ALLOWED_USERS` to your own id,
- run it in a project directory, not your home directory,
- treat the bot token like an SSH key,
- on a shared host, run the bot in a container under a dedicated user.

`COMMAND_CODE_YOLO=true` is the intended mode — and it is remote code execution
by design. Do not put production credentials on the host.

## Requirements

- **MCP server:** Node.js ≥ 18, a Telegram bot token.
- **Bot daemon:** Python ≥ 3.11, `python-telegram-bot` 20–21, the Command Code
  CLI on `PATH`. Command Code is closed-source and requires its own
  subscription; the bot daemon is only useful if you already use it.

## Development

```bash
npm install
npm test                       # MCP protocol tests (spawns the real server)

python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                         # 36 unit tests, no network, no `cmd` needed
```

CI runs both suites on Linux and Windows.

## Repository layout

```
index.js                     MCP server (the agent-facing part)
package.json                 npm metadata, tests, bin
telegram_commandcode/        Python bot daemon
  gateway/                   router, sender, processor, media
  commands/                  slash-command handlers (47)
  executor.py                async `cmd` subprocess runner
  formatter.py, chunking.py  MarkdownV2 escaping and 4096-char splitting
skill/                       publishable SKILL.md + references for agent
                             marketplaces (Agensi, Claude Code skills)
tests/                       pytest suite
test/                        MCP protocol tests (node:test)
legacy/                      original Node bot, archived and unsupported
deploy/                      systemd unit
```

## Publishing as an agent skill

`skill/` is a self-contained SKILL.md package: the playbook for when and how an
agent should use these tools, including MarkdownV2 pitfalls, limits, approval
loops and troubleshooting. Build the upload zip with:

```bash
node scripts/build-skill.mjs     # writes dist/telegram-bridge-skill.zip
```

The skill depends on this repository (MIT) but is not a copy of it: it encodes
the operating knowledge, while the MCP server stays free here.

## Status and known limits

- The MCP server is tested against `@modelcontextprotocol/sdk` 1.29 and Node 18/20/22.
- The bot daemon calls the `cmd` CLI with `-p <prompt>` and Command Code flags;
  it is coupled to that CLI's argument surface by definition.
- Command Code is not open source, so the daemon cannot be tested end-to-end in
  CI. Everything that does not need the binary is unit-tested.
- `telegram_get_updates` acknowledges updates: do not run the bot daemon and the
  MCP server against the same token at the same time, or they will steal each
  other's messages.

## License

MIT — see [LICENSE](LICENSE).
