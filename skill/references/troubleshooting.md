# Troubleshooting

Work top-down. Most failures are configuration, not code.

## The agent does not see the tools

1. Restart the agent — MCP servers are loaded at startup, not per request.
2. Check the registration:
   - Command Code: `cmd mcp list`
   - Claude Code: `claude mcp list`
3. Run the server by hand to see startup errors:

   ```bash
   TELEGRAM_BOT_TOKEN=<token> npx -y github:qrak/telegram-commandcode
   ```

   A healthy server prints `MCP server ready (token configured)` to **stderr**
   and then waits. It does not exit.
4. If it exits immediately with `Schema is missing a method literal`, the
   installed SDK is newer than the server's code. Update to the latest version
   of this project.

## Only `telegram_status` is listed

The server started without a token. The token was set in the wrong place: it
must be in the MCP server's `env` block, not in your shell profile, unless the
agent inherits the shell.

## `chat not found`

- The chat id is wrong, or the human has never opened a conversation with the
  bot. Send `/start` in that chat first.
- Group ids are negative (`-100…`). Copy the id from an incoming update rather
  than typing it.
- A channel requires the bot to be an administrator.

## `bot was blocked by the user`

The human blocked the bot. Only they can unblock it, from the bot's profile in
Telegram. Retrying will not help.

## `can't parse entities`

The message contains unescaped markup. Either:

- drop `parse_mode` entirely (the server will fall back automatically once,
  losing formatting), or
- switch to HTML and escape `&`, `<`, `>`, or
- escape all MarkdownV2 special characters — see `formatting.md`.

## Messages arrive twice

Two processes are polling the same bot token. Telegram allows only one
`getUpdates` consumer per bot; a second bot instance or a second MCP server
steals updates. Stop the duplicate.

## `409 Conflict: terminated by other getUpdates request`

Same cause as above: another long-poll is active. This is common when a
`telegram-commandcode` bot daemon and an MCP server share one token.

## The bot replies but the agent never sees it

The MCP server is a different process from the bot daemon. Reading replies
requires `telegram_get_updates` from the same process that owns the token, and
`getUpdates` acknowledges messages — whichever consumer calls it first wins.
Pick one consumer: either the bot daemon or the MCP tools, not both.

## File not found

Local paths resolve relative to the MCP server's working directory, which is
often `$HOME` or the agent's launch directory — not the project. Always pass an
absolute path.

On Windows, `C:\Users\...` must be quoted in shell examples; inside the tool
argument it is a plain string.

## Upload rejected: `Request Entity Too Large`

Photo > 10 MB or document > 50 MB. Compress, split, or upload elsewhere and
send a link.

## Download rejected / truncated

The Bot API caps downloads at 20 MB. For larger files run a local Bot API
server; this project does not configure one.

## Rate limiting (`429`)

The server honours `retry_after` and retries automatically. If you see repeated
429s, the agent is sending too much: batch updates, or send one file instead of
ten messages.

## TLS / proxy errors

Corporate proxies that intercept TLS break the Bot API. Set `HTTPS_PROXY` for
the MCP server process, or allow-list `api.telegram.org`. Never send the token
through an untrusted proxy — it is part of the request URL.

## Everything looks fine but nothing is delivered

Check the chat id against the `update_id` stream: send a message from the
phone to the bot, then call `telegram_get_updates` with `offset: 0`. The update
payload contains the real `chat.id` to use.

## Reset

Delete the MCP server registration, re-add it with the token, restart the
agent. There is no local state to clear apart from downloaded files in the
system temp directory.
