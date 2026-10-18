# Telegram formatting without breakage

Telegram parses a message only if **every** special character in the chosen
mode is escaped. One mistake returns HTTP 400 and the whole message is lost.
The MCP server retries once without `parse_mode` so delivery usually succeeds,
but the formatting is silently dropped.

## Decision order

1. **Plain text** (no `parse_mode`) — always safe. Use unless formatting adds
   meaning.
2. **HTML** — use when you need bold, code, links or preformatted blocks.
   Almost impossible to break by accident if you escape `&`, `<`, `>`.
3. **MarkdownV2** — only when the human asks for it or the surrounding system
   already produces it. Most expensive to get right.

## HTML mode

Supported tags: `<b>`, `<strong>`, `<i>`, `<em>`, `<u>`, `<s>`, `<code>`,
`<pre>`, `<a href="...">`, `<blockquote>`, `<tg-spoiler>`.

```html
<b>Deploy finished</b>
<code>api</code> → production in 48s
<pre>commit 4f2a1c9
tests 214 passed</pre>
<a href="https://example.com/run/42">Open the run</a>
```

Escape the payload itself: `&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;`.
Inside `<code>`/`<pre>` you still must escape those three characters.

## MarkdownV2 mode

Characters that must be escaped everywhere outside code entities:

```
_ * [ ] ( ) ~ ` > # + - = | { } . !
```

Plus `\` itself, always escaped first.

Leaking unescaped characters is the usual cause of `can't parse entities`.
Note that `.` and `-` and `(` `)` appear in ordinary prose constantly — this is
why MarkdownV2 is a poor default for free text.

Code entities are the exception: inside `` `code` `` and ```` ```pre``` ````,
only `` ` `` and `\` need escaping, and the closing fence must be escaped if it
appears in the body.

### Escaping helper (JavaScript)

```js
const MD2 = /[_*[\]()~`>#+\-=|{}.!\\]/g;
const escapeMd2 = (s) => s.replace(MD2, (c) => "\\" + c);
```

### Escaping helper (Python)

```python
import re
MD2 = re.compile(r"[_*\[\]()~`>#+\-=|{}.!\\]")
def escape_md2(text: str) -> str:
    return MD2.sub(lambda m: "\\" + m.group(0), text)
```

Apply the helper to **each dynamic value**, then concatenate with the static
formatting markup. Never escape the markup itself.

## Chunking long output

Telegram's limit is 4096 characters after parsing. Splitting on a fixed index
can cut a code fence or a surrogate pair in half.

A safe splitter:

1. If the text is under 3800 chars, send as-is.
2. Otherwise prefer a paragraph boundary (`\n\n`), then a line break, then a
   sentence end, then a space — searching backwards from 3800.
3. Never split inside a fenced block: if the candidate cut is between an
   opening and a closing fence, move the cut before the opening fence.
4. Prefix each part with `(1/3)` when sending more than one.
5. If the result would be more than 4 parts, send a `.txt` file instead.

For code or logs longer than ~15 000 characters, always attach a file; chat is
not a log viewer.

## Captions

Captions share the same parse modes but are limited to 1024 characters. A
caption is a headline, not a summary. Put the detail in the message that
accompanies the file, or in the file itself.

## Reactions instead of messages

Use a reaction to acknowledge without adding noise: 👀 when work starts, ✅
when it ends, ❌ on failure. Reactions are best-effort — never make the task
depend on one succeeding.

## Anti-patterns

- Bold applied to an entire multi-paragraph message (unreadable on mobile).
- Tables built from `|` and `-` — they do not render; use `<pre>` or a file.
- More than two emoji per message.
- Escaping the whole message including markup, which produces literal `\*`.
- Retrying the identical broken payload: it will fail identically. Fix the
  escape or drop `parse_mode`.
