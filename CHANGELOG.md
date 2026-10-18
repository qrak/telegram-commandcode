# Changelog

All notable changes to this project. The MCP server is consumed directly from
git (`npx github:qrak/telegram-commandcode`), so breaking changes ship to users
the moment they land on `main`.

## [2.1.0] — 2026-10-18

### Fixed

- **MCP server could not start at all.** `setRequestHandler("tools/list", …)`
  is invalid with `@modelcontextprotocol/sdk` 1.x; it threw
  `Schema is missing a method literal` before the transport connected. Now uses
  `ListToolsRequestSchema` / `CallToolRequestSchema`, and the dependency is
  pinned to `^1.29.0`. This is the bug behind "it does not work" on a fresh
  install.
- **MarkdownV2 failures were fatal.** A single unescaped character made the
  whole send fail. The server now retries once without `parse_mode`.
- **Token could leak into error strings.** Fetch/network errors contain the
  request URL, which contains the bot token. All surfaced errors are redacted.
- **Windows uploads** used `path.split("/")` for the multipart filename, which
  produced an empty filename on `C:\…` paths. Uses `path.basename`.
- **Hung subprocesses.** `run_cmd_with_progress` / `run_cmd_streaming` applied
  the timeout only after stdout closed, so a process that kept stdout open ran
  forever. Both now enforce an absolute deadline per read.
- **Bot ran the agent in `$HOME`.** The working directory now defaults to the
  process directory, overridable with `COMMAND_CODE_WORKDIR`.
- **Windows `/stop`** used `SIGINT`, which cannot be delivered to another
  process on Windows. Uses `terminate()` there.
- **Telegram menu drift.** `/cmd` and `/reason` were implemented but missing
  from the menu; six TUI-only or remote-impossible commands were listed but did
  nothing. Menu now matches the 47-entry dispatch table exactly.
- Removed a risky orphan-process killer that scanned `ps` output for `cmd -p`
  and could SIGKILL unrelated processes.
- `SyntaxWarning` from non-raw docstrings in `chunking.py`.

### Changed

- **Security: `TELEGRAM_ALLOWED_USERS` is fail-closed.** Unset now means
  "nobody", and the bot replies with the caller's numeric id. Previously the
  default was `any`, i.e. anyone who found the bot could drive an agent with
  `--yolo`.
- `ExecOptions.build_args` puts the prompt last so prompts starting with `-` are
  not misparsed.
- Legacy Node bot and Discord bridge moved to `legacy/` and removed from the
  npm `files`/`bin` list.
- `skills/` package added: `SKILL.md` + references, built into
  `dist/telegram-bridge-skill.zip` by `scripts/build-skill.mjs`.

### Added

- `LICENSE` (MIT), `SECURITY.md`, `AGENTS.md` rewrite, CI for Node 18/20/22 and
  Python 3.11/3.13 on Linux and Windows.
- 36 pytest unit tests and 4 MCP protocol tests.
- `docs/agensi-listing.md` — marketplace listing copy and submission checklist.

## [2.0.0] — 2026-10

- Python rewrite of the bot ("Hermes architecture"): async gateway, per-chat
  locks, persistent sessions, streaming edits, 47 slash commands.
- MCP server kept as the agent → Telegram direction.

## [1.x] — 2026

- Original single-file Node bot, Discord bridge, MCP server.
