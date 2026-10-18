---
name: telegram-bridge
description: Send messages, files, photos and progress updates to Telegram from a coding agent, and read replies back. Use when the user asks to "send this to Telegram", "ping me on Telegram", "send the screenshot/report/build log to my phone", wants remote approval or status updates for a long task, or needs a bot-to-human notification channel from CI or a headless machine. Requires the telegram-commandcode MCP server and a bot token.
version: 1.0.0
author: qrak
license: MIT
external_urls:
  - https://api.telegram.org
---

# Telegram Bridge

Give the agent a phone. This skill covers the full loop: verifying the Telegram
MCP connection, choosing the right tool, formatting messages that Telegram will
not reject, sending artifacts, and reading the human's reply.

## 0. Preflight — always do this first

Before the first send in a session, call `telegram_health`.

| Result | Meaning | Action |
|---|---|---|
| Bot id, username | Ready | Continue |
| `TELEGRAM_BOT_TOKEN is not set` / only tool is `telegram_status` | Not configured | Walk the user through Setup below, then stop |
| HTTP 401 | Token revoked or wrong | Tell the user to run `/revoke` in @BotFather and re-add the token |
| Network error | Host cannot reach `api.telegram.org` | Report it; do not retry more than twice |

Never claim a message was delivered without a successful tool result. If the
call fails, say so and show the agent's error text.

### Setup (only if the tool is missing)

Ask for the numeric chat ID and have the user create a bot with @BotFather.
The token must be set in the MCP server environment — **never ask the user to
paste it into the chat**.

Command Code:

```bash
cmd mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_DEFAULT_CHAT_ID=<chat_id> \
  -- npx -y github:qrak/telegram-commandcode
```

Claude Code:

```bash
claude mcp add telegram \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_DEFAULT_CHAT_ID=<chat_id> \
  -- npx -y github:qrak/telegram-commandcode
```

After adding, the agent must be restarted before the tools appear.

## 1. Choosing the tool

| Goal | Tool | Notes |
|---|---|---|
| Status, summary, alert | `telegram_send_message` | Plain text by default |
| Screenshot, chart, rendered page | `telegram_send_photo` | URL or absolute local path, ≤10 MB |
| Log, diff, PDF, zip, report | `telegram_send_file` | URL or absolute local path, ≤50 MB |
| "Did they reply?" | `telegram_get_updates` | Returns only unseen updates by default |
| Working / done / failed marker | `telegram_send_reaction` | Needs the message id |
| Pick up a file the user sent | `telegram_download_file` | ≤20 MB via Bot API |

Omit `chat_id` when `TELEGRAM_DEFAULT_CHAT_ID` is configured; pass it
explicitly when sending to a second chat.

## 2. Write for a phone, not for a log

The human is on a lock screen. Default to short, scannable updates.

- **One message per milestone**, not per tool call. A 40-step task should send
  2–4 messages, not 40.
- Lead with the outcome: `✅ Build passed — deployed to staging` before details.
- Put the actionable line first and the evidence after it.
- Keep messages under ~1500 characters. Telegram's hard limit is 4096; longer
  content belongs in a file (`telegram_send_file`), not an essay.
- Use emoji as status markers only: 👀 started, ✅ done, ❌ failed, ⚠️ needs a
  decision. They are not decoration.
- Do not send raw stack traces or full diffs as messages. Attach them.
- When the user must decide something, end with the exact question and options.

## 3. Formatting that does not break

Telegram rejects a message with `400 can't parse entities` if a single
MarkdownV2 special character is unescaped. The MCP server retries once as plain
text, but the formatting is then lost.

Rules:

1. **Prefer no `parse_mode`.** Plain text is always delivered.
2. If you need formatting, use `parse_mode: "HTML"` — it is far more forgiving
   than MarkdownV2 and supports `<b>`, `<i>`, `<code>`, `<pre>`, `<a href>`.
3. Only use MarkdownV2 when the user explicitly asks for it, and escape every
   one of: `_ * [ ] ( ) ~ \` > # + - = | { } . !`
4. Escape backslashes first (`\` → `\\`), then the rest.
5. Code goes in triple backticks or `<pre>`; never inside bold/italic.
6. In HTML mode, escape `<`, `>`, `&` in the content itself.

Full rules and a copy-paste escaping helper: `references/formatting.md`.

## 4. Limits worth knowing

| Limit | Value |
|---|---|
| Message text | 4096 chars |
| Caption | 1024 chars |
| Photo upload | 10 MB |
| Document upload | 50 MB |
| File download via Bot API | 20 MB |
| Inline keyboard buttons | 100 |

Exceeding a limit returns a 400 — chunk the text or send a file instead of
retrying blindly.

## 5. Recipes

Ready-to-adapt prompts for the common jobs (deploy notice, CI failure,
screenshot of a UI change, approval gate, inbox loop, daily digest) are in
`references/recipes.md`. Load that file when the task matches one of them
rather than inventing a new pattern.

## 6. Safety rules

- **Never send** bot tokens, API keys, `.env` contents, cookies, private keys,
  customer data, or full environment dumps. If a file might contain secrets,
  grep it first and redact.
- **Confirm before sending** to a chat you have not used in this session, and
  before sending anything that leaves the user's organisation.
- Do not send more than one message per 2 seconds to the same chat; Telegram
  rate-limits and the MCP server will back off.
- Do not create loops where the agent reads its own messages with
  `telegram_get_updates` and replies to them indefinitely. Track the last
  `update_id` you handled.
- If the user says "stop messaging me", stop immediately and do not send a
  confirmation message.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `chat not found` | Wrong id, or the user never started the bot | Send `/start` to the bot; use the numeric id from `getUpdates` |
| `bot was blocked by the user` | User blocked it | Unblock in Telegram |
| `not enough rights` | Bot is not admin in the channel | Promote the bot or send to a user chat |
| `can't parse entities` | Unescaped MarkdownV2 | Drop `parse_mode`, or switch to HTML |
| Message >4096 | Too long | Chunk or send as a file |
| Nothing arrives | Wrong token or wrong chat id | `telegram_health`, then `telegram_get_updates` |

## 8. When the job is done

Send exactly one closing message: what changed, where it runs, and the single
next action for the human. Then stop sending.

## References

- `references/tools.md` — tool-by-tool contracts and error codes
- `references/formatting.md` — MarkdownV2/HTML escaping, chunking
- `references/recipes.md` — six end-to-end workflows
- `references/troubleshooting.md` — extended diagnosis
- Source: <https://github.com/qrak/telegram-commandcode> (MIT)
