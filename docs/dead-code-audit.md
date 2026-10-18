# Dead code and unimplemented features

Audit date: 2026-10-18, version 2.1.1.

Tools used:

| Language | Tool | Command | Result |
|---|---|---|---|
| Python | [vulture](https://github.com/jendrikseipp/vulture) 2.14 | `python -m vulture` | 15 dead symbols (~350 LOC) |
| JavaScript | [ESLint](https://eslint.org) 8 (`no-unused-vars`, `no-unreachable`) | see below | clean |
| JavaScript | [knip](https://knip.dev) 5 | `npm run deadcode` | clean (legacy/ ignored) |

Reproduce:

```bash
python -m vulture        # config in pyproject.toml: ignore_names = ["handle_*"]
npm run deadcode         # knip, entry = test/*.test.mjs, legacy/ ignored
npx eslint@8.57.1 --no-eslintrc --config <eslintrc.json> index.js scripts/build-skill.mjs test/mcp.test.mjs
```

There are **no** `TODO`, `FIXME`, `NotImplemented` or `raise NotImplementedError`
markers in the shipped code (the only `TODO` strings live in `node_modules`).
The gaps below are behavioural, not annotated.

## 1. Dead code (confirmed by reference count)

Every symbol below has exactly one occurrence in the package: its definition.
Nothing in the tests or docs references it either.

| Symbol | Location | LOC | Notes |
|---|---|---|---|
| `format_message()` | `formatter.py:268` | 166 | Table rendering + markdown conversion. Pulls in four private helpers (`_is_table_row`, `_split_table_row`, `_render_table_block_for_telegram`, `_wrap_markdown_tables`, `formatter.py:163-266`) that are dead *because* it is. ~270 LOC total. |
| `run_cmd_streaming()` | `executor.py:300` | 100 | Superseded by `run_cmd_with_progress()`, which is what the processor calls. |
| `try_parse_fallback()` | `formatter.py:128` | 23 | Parse-failure heuristic; the sender uses `classify_telegram_error()` instead. |
| `safe_send_params()` | `formatter.py:110` | 16 | Only referenced from its own docstring example. |
| `escape_user_input()` | `formatter.py:98` | 10 | Duplicate of `escape_md2()`. |
| `classify_error()` | `gateway/sender.py:90` | 8 | Static wrapper around `classify_telegram_error()`; no callers. |
| `all_sessions()` | `session.py:126` | 6 | No command lists sessions; `/resume` resumes implicitly. |
| `link()` | `formatter.py:91` | 5 | Unused formatting helper. |
| `human_reason()` | `executor.py:74` | 5 | Used by one test, never in production. Keep or delete with the test. |
| `wrap_inline_code()` | `formatter.py:77` | 4 | Unused formatting helper. |
| `register_bg()` / `unregister_bg()` | `executor.py:150,153` | 4 | Background-process tracking that nothing registers into — `_bg_processes` is always empty. |
| `bold()` / `italic()` | `formatter.py:83,87` | 4 | Unused formatting helpers. |
| `FileFallbackResult.should_send_as_file` | `chunking.py:225` | 1 | Field is set to `True` and never read. |

Impact: ~350 lines to maintain, review and security-scan for zero behaviour.
`format_message()` is the big one — it looks like a feature (Telegram table
rendering) that was built and never wired into the sender.

## 2. False positives (do not delete)

Vulture also reports ~40 `handle_*` methods in `commands/`. They are dispatched
by name through each class's `COMMANDS = {"/cmd": "method_name"}` table via
`getattr()`, which vulture cannot follow. They are all reachable; the
`ignore_names = ["handle_*"]` entry in `pyproject.toml` suppresses them. The
dispatch table itself is covered by `tests/test_dispatch.py`, which fails if a
command points at a missing or non-async method.

## 3. Features that are advertised but not implemented

Ordered by how likely they are to disappoint a user.

| Command | Claim | Reality | Severity |
|---|---|---|---|
| `/compact` | "Compact conversation history" | `reset_state()` — wipes model, goal, steer, plan mode. No `cmd` call, no summarisation. The reply does say "All state reset", so the menu and the behaviour disagree. | **High** |
| `/compact-mode <mode>` | "Select compact mode" | Stores `state.compact_mode`. Nothing reads it: `ExecOptions` has no compact field and no flag is passed to `cmd`. The setting is inert. | **High** |
| `/undo [N]` | "Back up N turns and re-prompt" | `n` is parsed and used only in the confirmation text. Behaviour is identical for `/undo 1` and `/undo 10`: session is reset and the last prompt is re-run. No rewind exists. | **High** |
| `/fork` | "Fork conversation into new session" | Resets local state and stores a name. No fork happens in Command Code; the old session is not preserved. | Medium |
| `/rename <name>` | "Rename current session" | Stores `session_name`; `/resume` never reads it and no `--session` argument is passed. Cosmetic label only. | Medium |
| `/background <prompt>` | "Run a task in background", implied stoppable | Runs `run_cmd()` **without** `chat_id`, so it is not registered in `process_tracker`; `/stop` cannot stop it, and the dead `register_bg()` shows tracking was planned but never wired. It also bypasses the per-chat lock, so it can race the main conversation. | Medium |
| `/login`, `/logout`, `/feedback`, `/update`, `/learn-taste` | Forwarded to `cmd <subcommand>` | These subcommands are interactive or open a browser in the TUI. The bot runs them non-interactively with a 30–120 s timeout, so they will most likely hang and return "Command timed out". Cannot be fixed without a headless mode in Command Code. | Medium |
| `/provider`, `/effort`, `/reason` | "Set provider / reasoning effort" | Only write `~/.commandcode/config.json` (`provider`, `reasoningEffort`). No CLI flag is passed, so this works **only if** Command Code reads exactly that file and schema. Unverified; if the schema differs, the commands silently do nothing. | Medium |
| `/resume` | README: "reads CC session history" | Sets `active=True`, which adds `--continue` to the next invocation. It does not read or list history. Wording is stronger than the implementation. | Low |
| `/agents` | "Show agent configuration info" | Prints whether `~/.commandcode/agents` exists; admits TUI management is unavailable. | Low (documented) |
| `/usage` | "Show credits, plan, and usage metrics" | Shows user, version, model, max turns, YOLO; states detailed metrics need the TUI. | Low (documented) |
| `/context` | "Show context window usage" | Shows local session state; explicitly says context accounting is server-side. | Low (documented) |

### Features built but never connected

- **Telegram table rendering** (`format_message`) — implemented, tested by
  nobody, called by nobody.
- **Streaming subprocess output** (`run_cmd_streaming`) — implemented; the bot
  uses batched progress edits instead.
- **Background process tracking** (`register_bg`) — data structure and kill
  loop exist; nothing registers.
- **Session listing** (`all_sessions`) — no `/sessions` command exists.

## 4. JavaScript / MCP server

Clean. ESLint reports no unused variables or unreachable code; knip reports no
unused files, exports or dependencies (only `legacy/`, deliberately ignored).
All seven MCP tools are implemented and their descriptions match the code,
including the limits (4096 chars, 10 MB photo, 50 MB document, 20 MB download).

The only structural note: `index.js` is a single 570-line module. It is not
dead code, but extracting the Telegram API client and the tool handlers would
make it unit-testable without spawning a process.

## 5. Recommended order of work

1. **Make the menu honest** (1 hour): change `/compact` and `/undo` descriptions
   to what they do, or remove `/compact-mode` from `BOT_COMMANDS` until it is
   wired. Misleading commands cost more trust than missing ones.
2. **Delete the confirmed dead code** (30 min): `format_message` + its four
   helpers, `run_cmd_streaming`, the formatter helpers, `all_sessions`,
   `classify_error`, `register_bg`/`unregister_bg` + `_bg_processes`,
   `should_send_as_file`. Keep `human_reason` (tested) or drop both.
3. **Wire or remove `/compact-mode`** (unknown): needs a real Command Code flag;
   if none exists, remove it.
4. **Decide on `/background`** (1–2 hours): either register it in a separate
   tracker (so `/stop` can target it) or state in the message that it is
   fire-and-forget.
5. **Verify `/provider` and `/effort`** against a real Command Code install —
   this is the only item that cannot be settled by reading the code.
6. **Add the gates to CI** (optional): `python -m vulture` and
   `npm run deadcode`. Vulture exits non-zero while the dead code above exists,
   so either clean first or run it non-blocking.
