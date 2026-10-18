# Recipes

Each recipe is a prompt the agent can follow. Adapt names, keep the ordering:
verify connection → do work → send the smallest useful message.

## 1. Notify when a long job finishes

> Run the test suite. When it finishes, send me a Telegram message with the
> outcome, the failing test names if any, and the runtime. Keep it under 10
> lines. If it fails, attach the full log as a file instead of pasting it.

Why it works: one message, outcome first, evidence attached rather than
inlined. Send it from a `finally`-style path so a crash still notifies.

## 2. Progress updates for a multi-step task

> For this refactor, send exactly three Telegram updates: (1) when you have the
> plan, one line with the step count; (2) if you hit a blocker that changes the
> plan; (3) when it is done, with files changed and the verification command I
> should run. Do not send an update for each file edit.

Rules of thumb:

- Update on **state changes**, not on activity.
- A blocker message must contain the decision you need, not just the problem.
- Never send "still working" messages; silence is a valid status.

## 3. Send a screenshot of a UI change

> Take a screenshot of the page at 1280×800 after the change, save it to
> `/tmp/ui-after.png`, and send it to Telegram with the caption
> "Settings page — after". If the file is larger than 10 MB, send it as a file
> instead of a photo.

Notes:

- `telegram_send_photo` recompresses; for pixel diffs send the PNG as a
  document.
- Absolute paths only. Relative paths resolve against the MCP server's cwd,
  which is not the project directory.
- If the screenshot tool writes to a temp dir with spaces, quote the path.

## 4. Approval gate before a destructive action

> Before running the migration, send me a Telegram message describing what will
> change, how to roll back, and the exact command. Ask me to reply `yes` or
> `no`. Poll for my reply for up to 10 minutes. Proceed only on an explicit
> `yes`; anything else means abort.

Implementation outline:

1. `telegram_send_message` with the question and a message id.
2. Poll `telegram_get_updates` every 5–10 seconds with `timeout: 20`.
3. Match only messages from the expected `chat_id` **and** user id.
4. On timeout, do not proceed; report the timeout to the human in the chat.
5. Never treat silence, a reaction, or an emoji as approval.

## 5. Remote inbox for a headless agent

> Check Telegram for instructions every 60 seconds. Treat each new message as a
> task. Reply with a one-line acknowledgement only if the task will take more
> than a minute. Stop after 20 iterations or when I send `stop`.

Guardrails:

- Remember the highest `update_id` you processed; do not re-run old messages
  after a restart.
- Cap the loop. An unbounded polling loop plus an auto-replying agent is how
  you get an infinite conversation.
- `stop`, `halt`, and `kill` should all terminate the loop.
- If the update is a photo or document, call `telegram_download_file` with its
  `file_id` before trying to read it.

## 6. Daily digest

> Every day at 18:00, send one Telegram message: what shipped, what is blocked,
> and the top item for tomorrow. Three bullets maximum. If nothing shipped, say
> so in one line.

Scheduling belongs to the agent's scheduler or the OS (cron, systemd timer),
not to a sleep loop inside the MCP server. The server is stateless between
calls apart from the update cursor.

## 7. Send a build artifact

> After the build, send `dist/app.zip` to Telegram with a caption containing
> the version and the commit hash. Delete the file from `/tmp` afterwards.

Check the size first: the Bot API caps uploads at 50 MB for documents. If the
artifact is bigger, upload it somewhere and send the link instead.

## Message shapes that work

```
✅ Deploy finished
api → production in 48s
commit 4f2a1c9 · 214 tests passed
```

```
❌ Test suite failed
3 failures in tests/test_auth.py
Full log attached.
```

```
⚠️ Need a decision
Migration will drop the legacy_sessions table and cannot be undone.
Reply "yes" to proceed, "no" to abort. I will wait 10 minutes.
```
