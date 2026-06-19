# telegram-commandcode

**MCP server for Telegram integration with [Command Code](https://commandcode.ai)** — send messages, photos, and files straight from your AI coding agent.

```
> "Run the tests, then send me the results on Telegram"
  ✅ Tests pass → telegram_send_message → you get notified
```

## How It Works

This is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server. Once registered in Command Code, you get these tools:

| Tool | What it does |
|---|---|
| `telegram_send_message` | Send a text message (Markdown or HTML) |
| `telegram_send_photo` | Send a photo (URL or local file) |
| `telegram_send_file` | Send any file/document |
| `telegram_get_updates` | Read recent incoming messages |

## Quickstart

### 1. Create a Telegram Bot

Talk to [@BotFather](https://t.me/BotFather) on Telegram:

```
/newbot
→ pick a name and username
→ copy the token (looks like: 123456:ABCdef...)
```

### 2. Install & Register in Command Code

```bash
# Install globally via npm
npm i -g github:qrak/telegram-commandcode

# Or run directly via npx
npx github:qrak/telegram-commandcode
```

Then register it in your project:

```bash
# One-liner with token
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=123456:ABCdef... \
  -- npx telegram-commandcode

# Or with a .env file (create .env from .env.example first)
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=123456:ABCdef... \
  -e TELEGRAM_DEFAULT_CHAT_ID=1141080547 \
  -- npx telegram-commandcode
```

### 3. Find your chat ID

Message your bot on Telegram, then ask Command Code:

```
> Use telegram_get_updates to get recent messages
```

Or via curl:

```bash
curl https://api.telegram.org/bot<TOKEN>/getUpdates | jq ".result[0].message.chat.id"
```

### 4. Use It

Now Command Code automatically discovers the tools. Just ask naturally:

```
> Run the build, and if it passes send "✅ Build OK" to Telegram
```

```
> Deploy to staging, then telegram_send_message to chat 1141080547
```

```
> Write the release notes to /tmp/CHANGELOG.md and send it via telegram_send_file
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **Yes** | Bot token from @BotFather |
| `TELEGRAM_DEFAULT_CHAT_ID` | No | Default target chat — if set, you can omit `chat_id` in tool calls |

### Scopes

| Scope | Command |
|---|---|
| **Project** (default) | `cmd mcp add telegram -e TOKEN=... -- npx telegram-commandcode` |
| **Global** (all projects) | `cmd mcp add -s user telegram -e TOKEN=... -- npx telegram-commandcode` |
| **Shared** (team) | `cmd mcp add -s project telegram -e TOKEN=... -- npx telegram-commandcode` |

## Example Session

```
> Build the CLI, run the test suite, and send results to my Telegram

  [Command Code builds the project...]
  [Runs tests: 42 pass, 0 fail]

  Using telegram tool: telegram_send_message
  → ✅ CLI built successfully. Tests: 42 passed, 0 failed.

  You get this on your phone:
  ┌──────────────────────────┐
  │ ✅ CLI built successfully │
  │ Tests: 42 passed, 0 failed │
  └──────────────────────────┘
```

## Manual Testing

You can run the server directly to test:

```bash
# Set token
export TELEGRAM_BOT_TOKEN=123456:ABCdef...

# Send a message (via MCP JSON-RPC over stdio)
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"telegram_send_message","arguments":{"text":"Hello from Command Code!","chat_id":"1141080547"}}}' | node index.js
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TELEGRAM_BOT_TOKEN not set` | Token not passed. Check `cmd mcp get telegram` or restart with `-e` flag |
| `chat_id is required` | Set `TELEGRAM_DEFAULT_CHAT_ID` in .env or pass `chat_id` in every call |
| Server starts but tools are ✗ | Check `/mcp` menu in Command Code — server should show green |
| `File not found` | Use absolute paths. For files in your project, use full path like `/home/user/project/report.pdf` |

## License

MIT — use it, fork it, ship it.
