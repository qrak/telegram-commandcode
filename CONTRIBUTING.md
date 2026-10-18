# Contributing

Small project, small rules.

## Before you open a PR

```bash
npm test
python -m pytest        # with `pip install -e ".[dev]"` in a venv
node scripts/build-skill.mjs
```

All three must pass. Nothing in the suite needs a Telegram token, network
access, or the Command Code binary.

## Ground rules

- **Keep the MCP server dependency-free** apart from the MCP SDK. It is
  downloaded and executed by `npx` on every agent start.
- **Do not add a second `getUpdates` consumer.** One token, one poller.
- **Never loosen the access-control default.** `TELEGRAM_ALLOWED_USERS` unset
  means deny. `any` must stay an explicit opt-in.
- **Escape dynamic values, not markup.** Use `escape_md2()` on interpolated
  strings only.
- **No new network endpoints** without declaring them in `skill/SKILL.md`
  (`external_urls`) and in the README. That declaration is what passes the
  marketplace security scan.
- **Windows is a supported platform.** No bare `SIGINT`, no `/tmp` assumptions,
  no `split("/")` on paths.

## Commit messages

Conventional-ish, imperative, one line, then a body explaining *why*:

```
fix: use MCP SDK request schemas instead of method strings

setRequestHandler("tools/list", …) throws "Schema is missing a method literal"
with @modelcontextprotocol/sdk 1.x, so the server never connected.
```

## Adding a slash command

1. Add the method to the right `commands/*.py` class and register it in that
   class's `COMMANDS` dict (the key is the Command Code slash command).
2. If the Telegram-safe name differs (hyphens are not allowed), add a mapping
   in `commands/router.py::TG_TO_CC`.
3. Add the menu entry to `BOT_COMMANDS` in `bot.py` — `pytest` and the
   `AGENTS.md` invariants assume the menu matches the dispatch table.
4. Add a test if the command has any logic beyond forwarding a prompt.

## Reporting bugs

Include the agent/client, OS, Node or Python version, the exact tool call and
the error text (with the token redacted). "It does not work" is not
reproducible; `telegram_health` output is.
