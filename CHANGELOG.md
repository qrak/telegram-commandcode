# Changelog

All notable changes to this project. The MCP server is consumed directly from
git (`npx github:qrak/telegram-commandcode`), so breaking changes ship to users
the moment they land on `main`.

## [Unreleased]

### Documentation

- README now leads with the MCP server as the maintained part and says plainly
  that Command Code ships its own Telegram bridge (`/rc`, 2026-09-29), so the
  bot daemon is not needed for "Command Code on my phone". The daemon is
  frozen at 2.2.0: bug fixes only, no new commands or flags. `AGENTS.md`
  records the same rule so future work goes to the MCP server.

## [2.2.0] — 2026-10-18

Implements the recommendations from the dead-code audit: the daemon now uses
the CLI flags that actually exist (`--resume`, `--fork-session`, `-n`,
`--effort`, `--config`, `--output-format json`) instead of resetting state and
hoping, and the dead code is gone.

### Added

- **Real session lifecycle.** The session id is captured from
  `--output-format json`, so `/resume <name|id>` (`--resume`), `/rename`
  (`-n`) and `/fork` (`--continue --fork-session`) do what their names say.
  `/session-file` shows the current id.
- **`/compact` compacts.** One read-only summarisation run produces a handoff
  brief; the next prompt starts a fresh session seeded with it, injected once.
  A failed run leaves the session untouched.
- **`/undo [N]` honours N.** It drops the last N prompts from the history and
  seeds the new session with a brief describing the state *before* them.
- **`/usage` and `/context` report real numbers** — tokens in/out, duration and
  session id from the CLI result, plus per-chat totals. The result message gets
  a small `8.4s · 12,345→4,100 tok` footer.
- **`/compact-mode`** accepts the documented values (`default`, `fast`) and is
  passed per run as `--config compact-mode=…`, persisted as `compactMode`.
- **`/effort`** is passed as `--effort` instead of only writing config.json.
- **`/provider`** validates against the documented auth providers and explains
  that a new process (and maybe `cmd login`) is needed.
- **`/login`** explains the host-side login (or `COMMAND_CODE_API_KEY`) and
  shows `cmd status --json`, instead of spawning a browser it cannot show.
  `/feedback <text>` passes the text; `/logout` warns that credentials are
  shared with the host CLI; `/update check` runs `--check-only`.
- **Background tasks are tracked separately.** `/stop` cancels the foreground
  prompt *and* the background tasks and reports both counts.
- 66 new tests (163 total): JSON frame parsing, new CLI flags, process-tracker
  foreground/background isolation, prompt assembly, usage accounting, and the
  compact/undo/fork/rename/config handlers.
- Live progress is rendered from JSON `tool_running` events ("⚙️ read: app.py")
  instead of raw stdout.

### Changed

- Model output now goes through `format_message()`, so headings, code fences
  and GFM tables render on Telegram instead of showing literal `**` and `|`.
- `--trust` is passed by default (`COMMAND_CODE_TRUST=false` to omit it), so a
  first-time project does not stall an automated run.
- `/mcp`, `/skills`, `/taste`, `/learn-taste` and `/feedback` pass their
  arguments through instead of appending a bogus `list`.

### Fixed

- `/background` used to `--continue` the chat's own session — racing the
  foreground lock and polluting the transcript. It now runs in an isolated
  session and cannot be stopped by `/stop` (it now can).
- When Telegram rejected MarkdownV2, the plain-text fallback resent the
  *escaped* text, showing backslashes. It now sends `strip_mdv2()` output.
- Subcommands that need a terminal (`cmd login`) report that instead of a bare
  "Command timed out".

### Removed

~350 lines of dead code found by vulture: `run_cmd_streaming`, `wrap_code`,
`wrap_inline_code`, `bold`, `italic`, `link`, `escape_user_input`,
`safe_send_params`, `try_parse_fallback`, `classify_error`, `all_sessions`,
`FileFallbackResult.should_send_as_file`, and the unused
`VALID_EFFORT_LEVELS` copy in the router. `format_message()` and its table
helpers were wired into the result path instead of deleted. `/configure-models`
now reports effort and compact mode rather than misreading `permissions` as
per-task model overrides.

## [2.1.1] — 2026-10-18

Review pass after 2.1.0. Everything below was found by reading the code and
covered by a regression test.

### Fixed

- **MarkdownV2 was double-escaped inside code fences.** CLI output, model lists
  and `/configure-models` were passed through `escape_md2()` before being
  wrapped in ```` ``` ````, so users saw literal `\.` and `\-` inside code blocks.
  Code-fence content now uses `escape_code_block()` (backtick and backslash
  only).
- **`run_cli` leaked the child process on timeout.** `asyncio.wait_for()` cancels
  the wait, not the process; a hung `cmd` stayed alive. It is now killed and
  reaped.
- **Auto-attached files did not work on Windows.** The path regex only matched
  POSIX roots and `MEDIA:/…`; `MEDIA:C:\…` and quoted paths with spaces were
  ignored. Bare-path scanning is also no longer fooled by a quoted path.
- **Temp files leaked** after the >15 000-character "send as file" fallback, in
  both the sender and the command layer.
- **Empty messages were sent** through `_send_chunked`, which Telegram rejects
  with `message text is empty`.
- `/reload` exited via `os._exit()` without killing running `cmd` children; it
  now stops them first and says that a supervisor is required to come back.
- `getMe()` failure during startup of a message no longer drops the update.
- `chat_id` was required for reactions but silently omitted when neither the
  argument nor the default was set — it now fails with an actionable message.
- Deprecated `asyncio.get_event_loop()` calls replaced with
  `time.monotonic()` / `time.time_ns()`.

### Added

- 61 new tests (97 total): message sender (chunking, parse fallback, file
  fallback, error classification), path detection (POSIX, Windows, quoted),
  command plumbing (code-block escaping, CLI timeouts, config I/O), gateway
  (rate limiting, identity caching), media auto-attach, dispatch-table
  consistency and representative slash-command handlers.
- `ruff` with a correctness-only rule set (E/F/W) in `dev` extras and CI.
- Two more MCP protocol tests: >4096 characters rejected client-side, reaction
  without a chat id fails before any network call.

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
