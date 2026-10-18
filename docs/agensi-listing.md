# Agensi listing copy and submission checklist

Everything needed to publish `skill/` on <https://www.agensi.io>. The upload
artifact is `dist/telegram-bridge-skill.zip`, produced by
`node scripts/build-skill.mjs`.

## Listing metadata

**Title** (≤ 60 chars)

```
Telegram Bridge for Coding Agents
```

**Tagline** (≤ 140 chars)

```
Send messages, files and screenshots to Telegram from Claude Code, Cursor or Codex — and read approvals back.
```

**Category:** Developer Tools / Notifications & Communication

**Tags:** telegram, notifications, mcp, coding-agent, claude-code, cursor,
human-in-the-loop, remote-approval, ci, screenshots

**Price**

| Option | When | Notes |
|---|---|---|
| **Free** (recommended first) | No audience yet | Maximises installs, builds the creator profile; Agensi ranks paid skills above equally relevant free ones, so a paid v2 later is easy |
| **$9 one-time** | Once the free listing has ~50 installs and 2–3 reviews | $6.30 net per sale after the 30% fee |
| **$19** | Only with a demo video and a support commitment | Buyers compare against free GitHub docs |

Do not price below $5 unless you want credit-only purchases (Agensi rule:
under $5 is credit-only, $5+ can be bought by card).

**External URLs (must be declared in both SKILL.md and the Permissions tab)**

```
https://api.telegram.org
```

No other hosts are contacted. The MCP server is installed from GitHub by the
buyer's agent; that is a package registry fetch, not a runtime call from the
skill.

## Long description

> **Your agent can now reach your phone.**
>
> Telegram Bridge teaches a coding agent when and how to use the
> `telegram-commandcode` MCP server: send a summary when a long build finishes,
> attach the failing test log instead of pasting 400 lines, send a screenshot
> of the UI it just changed, and ask for approval before a destructive
> migration — then actually read your reply.
>
> **What it handles that raw prompting does not**
>
> - MarkdownV2 escaping rules that otherwise make Telegram reject the message
>   with `400 can't parse entities` (the single most common failure).
> - Telegram's real limits: 4096-char messages, 1024-char captions, 10 MB
>   photos, 50 MB documents, 20 MB Bot API downloads.
> - The one-consumer-per-token rule: `getUpdates` acknowledges messages, so a
>   bot daemon and an MCP server on the same token steal each other's updates.
> - Approval loops with a timeout and an explicit "silence is not approval"
>   rule.
> - A troubleshooting matrix for `chat not found`, `bot was blocked`,
>   `409 Conflict`, missing rights, and rate limits.
>
> **Requirements:** the free, MIT-licensed `telegram-commandcode` MCP server
> (installed with one `npx` command) and a Telegram bot token from @BotFather.
> Works with Claude Code, Cursor, Codex CLI, Command Code and any other MCP
> client.
>
> **Includes:** SKILL.md, tool contracts, formatting guide with escaping
> helpers, six end-to-end recipes, extended troubleshooting.

## Submission checklist

1. **Account** — sign in at agensi.io. Google SSO avoids the email-verification
   bug the platform is known for.
2. **Payout** — Stripe Connect (Poland is supported) or USDC via Zoneless.
   Required before a *paid* skill can go live; a free listing does not need it.
   Stripe onboarding means KYC: legal name, address, tax id.
3. **Upload** — Creator Dashboard → Submit a Skill → `dist/telegram-bridge-skill.zip`.
   `SKILL.md` must be at the archive root (it is).
4. **Frontmatter** — `name`, `description`, `version`, `license`,
   `external_urls` are present and validated by the build script. Keep
   `description` trigger-rich: it is what makes an agent load the skill.
5. **Permissions tab** — "Allowed Hosts" must contain exactly
   `https://api.telegram.org`. A mismatch with `external_urls` is an automatic
   rejection by the 8-point scan.
6. **Review** — automated scan, then AI + human review, 24–48 h. Rejections are
   usually undeclared network access, obfuscated code, or a listing that
   promises more than the skill does.
7. **After approval** — install it yourself through a connected agent as a
   normal buyer, from a clean environment, following only the SKILL.md. If the
   setup steps fail there, the money-back guarantee applies forever and the
   listing is a liability.

## Review risks specific to this listing

| Risk | Mitigation |
|---|---|
| "It only wraps a free GitHub repo" | The skill's value is the operating knowledge (escaping, limits, approval loop, troubleshooting), not a copy of the code. The repo is linked openly and stays MIT. §6.3a explicitly allows linking to your source repository. |
| "It requires a third-party paid CLI" | Only the *bot daemon* needs Command Code. The MCP server and the skill work with any agent. Say so in the description. |
| "It does network calls" | Declared: `api.telegram.org`, explained in the description. No telemetry, no analytics, no hidden endpoints. |
| "Prompt injection / data exfiltration" | The skill tells the agent to redact secrets before sending and to confirm before messaging a new chat. Keep that language; it is also the honest behaviour. |
| Refunds | The guarantee is unlimited in time. A skill that cannot be installed in five minutes generates refunds and support load. Free launch de-risks this. |

## Language

Write the listing in English. Agensi's buyers and search index are
English-first; a Polish listing would only add noise.

## What *not* to put in the listing

- Links to Gumroad/Payhip/your own checkout (§6.3a — instant removal).
- Claims about "65,000 visitors" or earnings.
- A promise that the token setup is automatic. It is a manual bot-token step,
  and pretending otherwise is the fastest route to a refund.
