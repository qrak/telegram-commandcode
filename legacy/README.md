# Legacy: the original Node.js bot

These files are the first implementation of this project, kept for reference
and for anyone who still runs them.

| File | What it is |
|---|---|
| `bot.js` | Single-file Telegram bot (Node, `node-telegram-bot-api`-style polling) |
| `discord.js` | Experimental Discord bridge from the same period |

**Status: archived.** No bug fixes, no security updates, not part of the npm
package, not covered by the test suite. Use the Python bot in
`telegram_commandcode/` instead — it has the same command set, persistent
sessions, and the security fixes described in `SECURITY.md`.

They are not deleted because git history is a poor place to read code, and
because `bot.js` documents the behaviour the Python port was written against.

## Running them (unsupported)

```bash
node legacy/bot.js
node legacy/discord.js
```

Both expect `TELEGRAM_BOT_TOKEN` (and `DISCORD_TOKEN` for the Discord one) in
the environment and call the `cmd` CLI directly. They predate the
fail-closed allow-list, so set `TELEGRAM_ALLOWED_USERS` yourself before
exposing them to anything.
