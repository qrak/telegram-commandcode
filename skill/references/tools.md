# MCP tool contracts

All tools return MCP content blocks of type `text`. A failed call sets
`isError: true` and the text starts with `Telegram error:`.

## telegram_health

No inputs. Calls `getMe`.

```
Bot connected: @my_bot (My Bot)
ID: 123456789
Reads all group messages: False
Inline queries: True
```

Use it as the first call in every session, and after changing the token.

## telegram_send_message

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | ≤4096 chars, non-empty |
| `chat_id` | string | no | Falls back to `TELEGRAM_DEFAULT_CHAT_ID` |
| `parse_mode` | `MarkdownV2` \| `HTML` \| `Markdown` | no | Omit for plain text |

Returns: `Message sent to chat <id> (message_id <n>).`

The server disables link previews by default. If Telegram rejects the
entities, the server retries the same text without `parse_mode` — the message
arrives, but unformatted, so treat a formatting-free result as a signal to fix
your escaping.

## telegram_send_photo

| Field | Type | Required | Notes |
|---|---|---|---|
| `photo` | string | yes | `https://` URL or absolute local path |
| `chat_id` | string | no | Default chat if omitted |
| `caption` | string | no | ≤1024 chars |
| `parse_mode` | `MarkdownV2` \| `HTML` | no | For the caption |

Photos are recompressed by Telegram; for pixel-exact artifacts (UI diffs,
diagrams) send the PNG as a **file** instead.

## telegram_send_file

| Field | Type | Required | Notes |
|---|---|---|---|
| `file_path` | string | yes | `https://` URL or absolute local path |
| `chat_id` | string | no | Default chat if omitted |
| `caption` | string | no | ≤1024 chars |
| `parse_mode` | `MarkdownV2` \| `HTML` | no | For the caption |

The filename sent to Telegram is the basename of the local path. Rename the
file before sending if the name matters.

## telegram_get_updates

| Field | Type | Required | Notes |
|---|---|---|---|
| `limit` | number | no | 1–20, default 5 |
| `offset` | number | no | Omit to auto-acknowledge; `0` re-reads the backlog |
| `timeout` | number | no | Long-poll seconds, 0–50, default 0 |

Returns one line per update:

```
2 update(s):
[1141080547] Jan (@jan): looks good, ship it
[1141080547] Jan: and send the changelog
```

Only `message`, `edited_message` and `channel_post` are summarised. Calls with
the same process advance an internal `update_id` cursor, so a second call does
not return the same messages. Pass `offset: 0` to deliberately re-read.

## telegram_send_reaction

| Field | Type | Required | Notes |
|---|---|---|---|
| `message_id` | number | yes | From an update or a send result |
| `emoji` | string | yes | Must be an allowed Telegram reaction |
| `chat_id` | string | no | Default chat if omitted |

Common allowed reactions: 👍 👎 ❤️ 🔥 🎉 🤔 👀 ✅ ❌ ⚡ 🙏 💯. A rejected emoji
returns `REACTION_INVALID`; fall back to a text status instead.

## telegram_download_file

| Field | Type | Required | Notes |
|---|---|---|---|
| `file_id` | string | yes | From a photo/document/voice message |

Saves to the OS temp directory under `telegram-commandcode/` and returns the
absolute path plus size. Bot API downloads are capped at 20 MB; for larger
files the bot must run locally with a Bot API server, which this MCP server
does not support.

## Error codes

| Code | Meaning | What to do |
|---|---|---|
| 400 | Bad request (parse mode, chat id, length) | Fix the argument; do not retry unchanged |
| 401 | Invalid token | Re-issue the token in @BotFather |
| 403 | Blocked / not a member / no rights | Ask the human to unblock or promote the bot |
| 404 | `chat not found` | Wrong id, or the human never pressed Start |
| 429 | Rate limited | The server honours `retry_after`; wait it out |
| 5xx | Telegram outage | Retried automatically up to 3 times |

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from @BotFather |
| `TELEGRAM_DEFAULT_CHAT_ID` | no | Makes `chat_id` optional everywhere |
