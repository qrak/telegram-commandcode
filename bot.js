#!/usr/bin/env node
/**
 * telegram-commandcode bot daemon
 * 
 * Bridges Telegram ↔ Command Code CLI:
 *   1. Listens for messages on Telegram (long polling)
 *   2. Forwards each message to Command Code in headless mode
 *   3. Streams/sends the response back to Telegram
 * 
 * Usage:
 *   TELEGRAM_BOT_TOKEN=*** node bot.js
 * 
 *   Options (env vars):
 *     TELEGRAM_ALLOWED_USERS  — comma-separated list of user IDs (or "any")
 *     COMMAND_CODE_CMD        — path to the `cmd` binary (default: "cmd")
 *     TELEGRAM_DEFAULT_CHAT   — fallback chat ID for outgoing messages
 */

import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error("❌ TELEGRAM_BOT_TOKEN is required. Create a bot at @BotFather.");
  process.exit(1);
}

const API_BASE = `https://api.telegram.org/bot${BOT_TOKEN}`;
const CMD_BIN = process.env.COMMAND_CODE_CMD || "cmd";

// Access control: comma-separated user IDs or "any" for open access
const ALLOWED = (process.env.TELEGRAM_ALLOWED_USERS || "any")
  .split(",")
  .map((s) => s.trim());

// ---------------------------------------------------------------------------
// Telegram API
// ---------------------------------------------------------------------------

async function api(method, body) {
  const url = `${API_BASE}/${method}`;
  const isForm = body instanceof FormData;
  const res = await fetch(url, {
    method: "POST",
    headers: isForm ? {} : { "Content-Type": "application/json" },
    body: isForm ? body : JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Telegram API ${method}: ${res.status} ${err}`);
  }
  return (await res.json()).result;
}

async function sendMessage(chatId, text) {
  // Split long messages (Telegram limit: 4096 chars)
  const maxLen = 4000;
  if (text.length <= maxLen) {
    return api("sendMessage", {
      chat_id: chatId,
      text,
      parse_mode: "MarkdownV2",
      link_preview_options: { is_disabled: true },
    });
  }

  // Send in chunks
  const chunks = [];
  for (let i = 0; i < text.length; i += maxLen) {
    chunks.push(text.slice(i, i + maxLen));
  }
  for (let i = 0; i < chunks.length; i++) {
    await api("sendMessage", {
      chat_id: chatId,
      text: `\\(${i + 1}/${chunks.length}\\)\n${chunks[i]}`,
      parse_mode: "MarkdownV2",
    });
  }
  return { message_id: "chunked" };
}

async function sendTyping(chatId) {
  return api("sendChatAction", { chat_id: chatId, action: "typing" }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Markdown escaping for Telegram's MarkdownV2
// ---------------------------------------------------------------------------

const ESCAPE_RE = /[_*[\]()~`>#+\-=|{}.!]/g;
function escapeMd(text) {
  return text.replace(ESCAPE_RE, "\\$&");
}

// ---------------------------------------------------------------------------
// Access control
// ---------------------------------------------------------------------------

function isAllowed(user) {
  if (ALLOWED.includes("any")) return true;
  const id = String(user?.id || user);
  return ALLOWED.includes(id);
}

// ---------------------------------------------------------------------------
// Command Code: run in headless mode
// ---------------------------------------------------------------------------

/**
 * Run a prompt through Command Code CLI in headless mode.
 * Uses `cmd -p "prompt"` (or stdin pipe) — the official headless API.
 * 
 * Flags used:
 *   -p "prompt"   — non-interactive, outputs response to stdout
 *   --yolo         — bypass all permission prompts (all tools enabled)
 *   --max-turns N  — cap conversation turns (default 10)
 *   --continue     — resume the most recent headless session in this dir
 * 
 * Returns combined stdout. Exit codes: 0=success, 1=error, 3=auth, 4=perms, etc.
 */
async function runCommandCode(prompt, cwd = process.env.HOME, sessionOpts = {}) {
  const args = ["-p", prompt];

  // Permissions: --yolo enables all tools (file writes, shell). 
  // Omit for read-only safety. Use --permission-mode plan for plan mode.
  if (process.env.COMMAND_CODE_YOLO !== "false") {
    args.push("--yolo");
  }

  // Turn limit
  const maxTurns = Number(process.env.COMMAND_CODE_MAX_TURNS) || 20;
  args.push("--max-turns", String(maxTurns));

  // Session chaining: --continue resumes the most recent headless session
  if (sessionOpts.continue) {
    args.push("--continue");
  } else if (sessionOpts.resume) {
    args.push("--resume", sessionOpts.resume);
  }

  // Skip onboarding for automated runs
  args.push("--skip-onboarding");

  return new Promise((resolve, reject) => {
    const child = spawn(CMD_BIN, args, {
      cwd,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 10 * 60 * 1000, // 10 minute timeout for complex tasks
    });

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    child.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    child.on("close", (code) => {
      const exitCodes = {
        0: "success",
        1: "general error",
        3: "not authenticated",
        4: "permission denied (use --yolo?)",
        5: "rate limited",
        6: "network failure",
        7: "server error (5xx)",
        130: "interrupted",
      };

      if (code === 0 || code === null) {
        resolve(stdout.trim() || "(no output)");
      } else {
        const reason = exitCodes[code] || `exit code ${code}`;
        const errText = stderr.trim();
        const msg = errText
          ? `⚠️ ${CMD_BIN} ${reason}:\n${stderr.slice(0, 1000)}`
          : `⚠️ ${CMD_BIN} ${reason} (no stderr)`;
        resolve(msg);
      }
    });

    child.on("error", (err) => {
      reject(new Error(`Failed to spawn ${CMD_BIN}: ${err.message}`));
    });
  });
}

// ---------------------------------------------------------------------------
// Bot commands (Telegram slash commands)
// ---------------------------------------------------------------------------

const BOT_COMMANDS = [
  { command: "cmd", description: "Run a prompt through Command Code" },
  { command: "status", description: "Check Command Code status and session info" },
  { command: "resume", description: "Continue the most recent session" },
  { command: "clear", description: "Start a fresh session (forget context)" },
  { command: "model", description: "Show/set the AI model in use" },
  { command: "help", description: "Show available commands" },
];

async function registerCommands() {
  try {
    await api("setMyCommands", { commands: BOT_COMMANDS });
    console.log("   Commands registered");
  } catch (err) {
    console.error("   ⚠️ Failed to register commands:", err.message);
  }
}

// Session tracking (for /resume, /clear)
let sessionActive = false;

/**
 * Handle slash commands. Returns true if handled, false if it's a regular prompt.
 */
async function handleCommand(chatId, text) {
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const args = parts.slice(1).join(" ");

  switch (cmd) {
    case "/cmd": {
      if (!args) {
        await sendMessage(chatId, "Usage: `/cmd <prompt>` — run a prompt through Command Code");
        return true;
      }
      // Fall through to regular prompt handling below
      return false;
    }

    case "/start": {
      await sendMessage(
        chatId,
        "🤖 *Command Code Bot*\n\n" +
        "I connect Telegram to your Command Code CLI\\.\n\n" +
        "*Send me any prompt* and I'll run it through `cmd \\-p`\\.\n\n" +
        "*Commands:*\n" +
        "  `/cmd <prompt>` \\- Run a task\n" +
        "  `/status` \\- Check session info\n" +
        "  `/resume` \\- Continue last session\n" +
        "  `/clear` \\- Start fresh\n" +
        "  `/model` \\- Show current model\n" +
        "  `/help` \\- Show this message\n\n" +
        "_Tip: type `/` in the message box to see all commands_"
      );
      return true;
    }

    case "/help": {
      const cmds = BOT_COMMANDS.map(
        (c) => `  /${c.command} \\- ${escapeMd(c.description)}`
      ).join("\n");
      await sendMessage(
        chatId,
        `*Available commands:*\n${cmds}\n\n_Any other message is treated as a prompt for Command Code\\._`
      );
      return true;
    }

    case "/status": {
      try {
        // Quick probe: run `cmd whoami` to check auth
        const child = spawn(CMD_BIN, ["whoami"], {
          env: { ...process.env },
          stdio: ["pipe", "pipe", "pipe"],
          timeout: 10_000,
        });
        let whoami = "";
        child.stdout.on("data", (d) => { whoami += d.toString(); });
        await new Promise((r) => child.on("close", r));

        const sessionInfo = sessionActive
          ? "Session: active \\(use `/resume` to continue, `/clear` to reset\\)"
          : "Session: none \\(send a prompt to start\\)";

        const binCode = "`" + escapeMd(CMD_BIN) + "`";
        await sendMessage(
          chatId,
          `🔧 *Command Code Status*\n` +
          `  Binary: ${binCode}\n` +
          `  Auth: ${whoami.trim() || "unknown"}\n` +
          `  ${escapeMd(sessionInfo)}\n` +
          `  YOLO mode: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on (all tools)" : "off (read\\-only)"}\n` +
          `  Max turns: ${Number(process.env.COMMAND_CODE_MAX_TURNS) || 20}`
        );
      } catch (err) {
        await sendMessage(chatId, escapeMd(`❌ Error checking status: ${err.message}`));
      }
      return true;
    }

    case "/resume": {
      sessionActive = true;
      await sendTyping(chatId);
      await sendMessage(chatId, "🔄 Resuming last headless session\\.\\.\\.");

      try {
        const result = await runCommandCode(
          "Continue where we left off. Summarize the previous context and ask what I'd like to do next.",
          process.env.HOME,
          { continue: true }
        );
        await sendMessage(chatId, `📋 *Session resumed:*\n${result}`);
      } catch (err) {
        await sendMessage(chatId, escapeMd(`❌ Error: ${err.message}`));
      }
      return true;
    }

    case "/clear": {
      sessionActive = false;
      await sendMessage(chatId, "🧹 Session cleared\\. Next prompt starts fresh \\(no context from previous runs\\)\\.");
      return true;
    }

    case "/model": {
      try {
        const child = spawn(CMD_BIN, ["--list-models"], {
          env: { ...process.env },
          stdio: ["pipe", "pipe", "pipe"],
          timeout: 10_000,
        });
        let stdout = "";
        child.stdout.on("data", (d) => { stdout += d.toString(); });
        await new Promise((r) => child.on("close", r));

        const models = stdout.trim() || "Run `cmd --list-models` locally to see models";
        const preview = models.length > 3500
          ? models.slice(0, 3500) + "\n...(truncated)"
          : models;

        const codeBlock = "```\n" + escapeMd(preview) + "\n```";
        await sendMessage(
          chatId,
          "🤖 *Available models*\n\n" + codeBlock + "\n\n" +
          "_Use `cmd -m <model>` locally to switch\\._"
        );
      } catch (err) {
        await sendMessage(chatId, `❌ Could not list models: ${escapeMd(err.message)}`);
      }
      return true;
    }

    default:
      // Unknown slash command
      if (text.startsWith("/")) {
        const escapedCmd = escapeMd(cmd);
        await sendMessage(chatId, "Unknown command: `" + escapedCmd + "`. Use /help to see available commands\\.");
        return true;
      }
      return false;
  }
}

// ---------------------------------------------------------------------------
// Long polling loop
// ---------------------------------------------------------------------------

let lastUpdateId = 0;

async function poll() {
  try {
    const updates = await api("getUpdates", {
      offset: lastUpdateId + 1,
      timeout: 30, // long polling (30s)
      allowed_updates: ["message"],
    });

    for (const update of updates) {
      lastUpdateId = update.update_id;

      const msg = update.message;
      if (!msg || !msg.text) continue;

      const chatId = String(msg.chat.id);
      const userId = msg.from?.id;
      const username = msg.from?.username || msg.from?.first_name || "unknown";
      const text = msg.text.trim();

      // Ignore empty messages
      if (!text) continue;

      // Access control
      if (!isAllowed(userId)) {
        console.log(`⛔ Blocked message from user ${userId} (${username})`);
        await sendMessage(chatId, escapeMd("⛔ Sorry, you are not authorized to use this bot."));
        continue;
      }

      console.log(`📩 [${username}] ${text.slice(0, 80)}`);

      // --- Check if it's a slash command ---
      if (text.startsWith("/")) {
        const handled = await handleCommand(chatId, text);
        if (handled) continue;
        // /cmd with args: strip the /cmd prefix and run the prompt
        if (text.startsWith("/cmd ")) {
          const prompt = text.slice(5).trim();
          if (!prompt) continue;
          // Don't return — fall through to the regular prompt handler below
          // But skip the "/cmd" echo — show the actual prompt instead
          await sendTyping(chatId);
          await sendMessage(chatId, escapeMd(`🚀 Running: \`${prompt.slice(0, 200)}\``));
          try {
            const result = await runCommandCode(
              prompt,
              process.env.HOME,
              sessionActive ? { continue: true } : {}
            );
            sessionActive = true;
            const finalText = result
              ? `✅ *Done:* ${escapeMd(prompt.slice(0, 100))}\n\n${result}`
              : `✅ *Done* \\— ${escapeMd(prompt.slice(0, 100))}`;
            await sendMessage(chatId, finalText);
            console.log(`✅ Completed /cmd from ${username}`);
          } catch (err) {
            await sendMessage(chatId, escapeMd(`❌ Error: ${err.message}`));
            console.error(`❌ Error for ${username}:`, err.message);
          }
          continue;
        }
        continue;
      }

      // --- Regular prompt → forward to Command Code ---
      await sendTyping(chatId);

      await sendMessage(
        chatId,
        escapeMd(`🚀 Running: \`${text.slice(0, 200)}\``)
      );

      try {
        const result = await runCommandCode(
          text,
          process.env.HOME,
          sessionActive ? { continue: true } : {}
        );
        sessionActive = true;
        const finalText = result
          ? `✅ *Done:* ${escapeMd(text.slice(0, 100))}\n\n${result}`
          : `✅ *Done* \\— ${escapeMd(text.slice(0, 100))}`;
        await sendMessage(chatId, finalText);
        console.log(`✅ Completed prompt from ${username}`);
      } catch (err) {
        await sendMessage(chatId, escapeMd(`❌ Error: ${err.message}`));
        console.error(`❌ Error for ${username}:`, err.message);
      }
    }
  } catch (err) {
    console.error("Poll error:", err.message);
    // Wait before retrying on error
    await new Promise((r) => setTimeout(r, 5000));
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  console.log(`🤖 telegram-commandcode bot starting`);
  console.log(`   Bot: @${(await api("getMe", {})).username}`);
  console.log(`   CMD: ${CMD_BIN}`);
  console.log(`   Access: ${ALLOWED.join(", ")}`);

  // Register Telegram bot commands (so typing "/" shows the menu)
  await registerCommands();

  // Clear any pending updates
  const existing = await api("getUpdates", {});
  if (existing.length > 0) {
    lastUpdateId = existing[existing.length - 1].update_id;
    console.log(`   Cleared ${existing.length} pending updates`);
  }

  console.log("   Listening... (Ctrl+C to stop)");

  // Main loop
  while (true) {
    await poll();
  }
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
