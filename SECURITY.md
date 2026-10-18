# Security Policy

## The threat model in one paragraph

This project connects a chat app to an AI coding agent that can read and write
files and run shell commands. **Anyone who can message the bot can drive the
agent.** Treat the bot token like an SSH key and the allow-list like a firewall
rule: if it is wrong, the exposure is remote code execution on the host.

## Non-negotiable rules

1. **Never run with the default open allow-list on a public host.**
   Set `TELEGRAM_ALLOWED_USERS` to your numeric user ID(s). `"any"` is only
   acceptable for a throwaway container with no credentials and no network
   access to anything you care about.
2. **`COMMAND_CODE_YOLO=true` means the agent may edit files and run commands
   without asking.** It is the default because that is the point of the tool —
   but it is a remote-shell-equivalent. Use a dedicated user account or a
   container, never your main workstation profile with production credentials.
3. **Never paste the bot token into a chat, an issue, or a screenshot.**
   Tokens belong in the environment or a `.env` file that is git-ignored.
4. **Keep the agent's working directory narrow.** Run it in a project
   directory, not `$HOME`.
5. **Rotate immediately** via @BotFather (`/revoke`) if a token leaks, and
   audit the bot's recent messages.

## Reporting a vulnerability

Open a private security advisory on GitHub:
<https://github.com/qrak/telegram-commandcode/security/advisories/new>

Please do not open a public issue for anything that could be used to attack a
running bot. Include the version, the platform, and a minimal reproduction.
Expect an initial response within 7 days.

## Supported versions

| Version | Supported |
|---|---|
| 2.x (MCP server + Python bot) | ✅ |
| 1.x (legacy Node bot in `legacy/`) | ❌ — archived, no fixes |

## Known limitations (by design)

- The MCP server trusts the agent process it runs under. It does not add an
  approval layer of its own; use the agent's permission prompts for that.
- The bot executes whatever the agent produces. Sandbox the host if the agent
  works on untrusted repositories.
- Telegram Bot API traffic is TLS to `api.telegram.org`; the token is sent in
  the URL path, which is standard for the Bot API but means proxy logs can
  capture it. Do not route through an untrusted HTTP proxy.
