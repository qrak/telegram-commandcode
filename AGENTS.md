# AGENTS.md — telegram-commandcode

Two deliverables in one repository:

1. **`index.js`** — MCP stdio server, agent → Telegram. This is the primary
   product and the part that works with every MCP client.
2. **`telegram_commandcode/`** — Python Telegram bot, Telegram → `cmd` CLI.
   Coupled to Command Code's closed-source argument surface.

`legacy/` holds the original Node bot. Archived, unsupported, excluded from the
npm package. Do not add features there.

## Repository map

```
index.js                      MCP server (7 tools, SDK 1.29 schemas)
package.json                  npm metadata, bin, npm test
scripts/build-skill.mjs       builds dist/telegram-bridge-skill.zip
skill/                        publishable SKILL.md + 4 reference docs
test/mcp.test.mjs             node:test protocol tests (spawns the server)
tests/                        pytest suite (97 tests, no network, no `cmd`)
legacy/                       old Node bot + Discord bridge (archived)
deploy/                       systemd unit

telegram_commandcode/
├── bot.py                    PTB Application, 47-command menu, startup
├── session.py                ChatSession dataclass + file-backed SessionStore
├── executor.py               async `cmd` runner, per-chat process tracker
├── chunking.py               code-fence-aware 4096-char splitter
├── formatter.py              MarkdownV2 escaping, table rendering
├── gateway/
│   ├── gateway.py            BotGateway: locks, rate limits, identity
│   ├── router.py             access control, group mention logic, dispatch
│   ├── sender.py             send/edit with parse fallback + flood control
│   ├── processor.py          enqueue → per-chat lock → streaming pipeline
│   └── media.py              download, transcribe, auto-attach MEDIA: paths
└── commands/
    ├── router.py             dispatch table (47 entries, TG→CC name map)
    ├── base.py               shared send_md/run_cli/config/state helpers
    ├── info_cmds.py          12 commands
    ├── session_cmds.py        9 commands
    ├── config_cmds.py         7 commands
    ├── prompt_cmds.py        11 commands
    └── cli_cmds.py            8 commands
```

## Invariants

- **One consumer per bot token.** `getUpdates` acknowledges messages. Running
  the daemon and the MCP server on the same token makes them steal updates.
  This is documented, not fixed.
- **Fail-closed access.** An unset `TELEGRAM_ALLOWED_USERS` means nobody is
  allowed; the bot replies with the caller's numeric id. Never change the
  default back to `any`.
- **Prompt is the last argv element** in `ExecOptions.build_args`, so a prompt
  starting with `-` is not parsed as a flag.
- **All Telegram sends go through `MessageSender`** (or `BaseCommandHandler`),
  which applies MarkdownV2 → plain fallback, chunking and flood control.
  Do not call `bot.send_message` directly.
- **Escape dynamic values, never markup.** `escape_md2()` is applied to
  interpolated values only; the surrounding `*bold*`/backticks are static.
- **The MCP server must start without a token** and expose `telegram_status`.
  A silent failure here was the original "it does not work" bug.
- **Windows:** no `SIGINT` delivery to other processes; `process.terminate()` is
  used. Spawning `cmd` requires the real binary on `PATH`.

## Testing

```bash
npm test                                  # 4 protocol tests, ~3 s
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && pytest         # 97 tests, ~2 s
node scripts/build-skill.mjs              # validates SKILL.md frontmatter
```

Nothing in the test suite needs a Telegram token, network access, or the
Command Code binary. If a change needs one of those to test, it needs a
seam (an injected runner or a fake binary) before it can be merged.

## Release checklist

1. `npm test` and `pytest` green.
2. `node scripts/build-skill.mjs` — bump `version:` in `skill/SKILL.md` when
   the skill's behaviour changes.
3. Update `CHANGELOG.md` and the version in `package.json`, `pyproject.toml`,
   `telegram_commandcode/__init__.py`.
4. Tag `vX.Y.Z`. The MCP server is consumed straight from git by `npx`, so a
   broken `main` reaches users immediately.
