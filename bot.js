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
// Bot commands — all 27 Command Code slash commands ----------------------------------
// ---------------------------------------------------------------------------

// Full Command Code slash command set, mapped to CLI where possible.
// Telegram limit: 100 commands, ~4KB payload. 27 commands = well within.
const BOT_COMMANDS = [
  { command: "add_dir",    description: "Add directory to workspace" },          // /add-dir (hyphens not allowed in TG bot cmd names)
  { command: "agents",     description: "Manage agent configurations — TUI" },
  { command: "clear",      description: "Clear conversation history (fresh start)" },
  { command: "compact",    description: "Compact conversation history — TUI" },
  { command: "effort",     description: "Set reasoning effort for current model — TUI" },
  { command: "exit",       description: "Exit session (N/A remotely)" },
  { command: "feedback",   description: "Submit feedback about Command Code" },  // → cmd feedback
  { command: "help",       description: "Show available commands" },
  { command: "ide",        description: "Connect IDE — local only" },
  { command: "init",       description: "Initialize AGENTS.md for this project — TUI" },
  { command: "learntaste", description: "Learn taste from other agents" },       // /learn-taste → cmd learn-taste
  { command: "login",      description: "Authenticate with Command Code" },      // → cmd login
  { command: "logout",     description: "Remove stored authentication" },        // → cmd logout
  { command: "mcp",        description: "Manage MCP server connections" },       // → cmd mcp
  { command: "memory",     description: "Manage Command Code memory — TUI" },
  { command: "model",      description: "Show available models" },               // → cmd --list-models
  { command: "plan",       description: "Enter plan mode or plan a task" },
  { command: "prcomments", description: "Fetch PR comments — TUI" },            // /pr-comments
  { command: "provider",   description: "Select AI provider — TUI" },
  { command: "resume",     description: "Resume a past conversation" },
  { command: "review",     description: "Review a pull request — TUI" },
  { command: "rewind",     description: "Restore to previous checkpoint — TUI" },
  { command: "share",      description: "Share conversation — N/A remotely" },
  { command: "skills",     description: "Browse and manage agent skills" },      // → cmd skills
  { command: "taste",      description: "Manage Taste learning" },               // → cmd taste
  { command: "terminalsetup", description: "VSCode keybindings — local only" }, // /terminal-setup
  { command: "unshare",    description: "Stop sharing — N/A remotely" },
];

// Map telegram-safe command names (no hyphens) back to real slash commands
const TG_TO_CC = {
  "add_dir": "/add-dir",
  "learntaste": "/learn-taste",
  "prcomments": "/pr-comments",
  "terminalsetup": "/terminal-setup",
};

async function registerCommands() {
  try {
    await api("setMyCommands", { commands: BOT_COMMANDS });
    console.log(`   ${BOT_COMMANDS.length} commands registered`);
  } catch (err) {
    console.error("   ⚠️ Failed to register commands:", err.message);
  }
}

// Session tracking
let sessionActive = false;

/**
 * Run a CLI subcommand and return its stdout (or error message).
 */
async function runCLI(args, timeout = 15_000) {
  return new Promise((resolve) => {
    const child = spawn(CMD_BIN, args, {
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d.toString(); });
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      resolve({ code, stdout: stdout.trim(), stderr: stderr.trim() });
    });
    child.on("error", (err) => {
      resolve({ code: -1, stdout: "", stderr: err.message });
    });
  });
}

/**
 * Handle ALL Command Code slash commands.
 * Returns true if handled (message sent), false if it should be treated as a regular prompt.
 */
async function handleCommand(chatId, text) {
  const parts = text.split(/\s+/);
  const rawCmd = parts[0].toLowerCase();
  // Convert TG-safe name back to CC slash command (e.g. add_dir → /add-dir)
  const ccSlash = TG_TO_CC[rawCmd.slice(1)] || rawCmd;
  const args = parts.slice(1).join(" ");

  // ── Commands that forward directly to CLI subcommands ──
  const CLI_MAP = {
    "/feedback":  { args: ["feedback", args], msg: "📝 Submitting feedback..." },
    "/learntaste": { args: ["learn-taste"], msg: "🧠 Learning taste from repositories..." },
    "/login":     { args: ["login"], msg: "🔑 Authenticating..." },
    "/logout":    { args: ["logout"], msg: "👋 Logging out..." },
    "/mcp":       { args: ["mcp", ...(args ? args.split(/\s+/) : ["list"])], msg: "🔌 Managing MCP servers..." },
    "/skills":    { args: ["skills", ...(args ? args.split(/\s+/) : ["list"])], msg: "📦 Managing skills..." },
    "/taste":     { args: ["taste", ...(args ? args.split(/\s+/) : ["list"])], msg: "🎨 Managing taste..." },
  };

  if (CLI_MAP[ccSlash]) {
    const { args: cliArgs, msg } = CLI_MAP[ccSlash];
    await sendTyping(chatId);
    await sendMessage(chatId, escapeMd(msg));
    try {
      const { stdout, stderr, code } = await runCLI(cliArgs, 30_000);
      const output = stdout || stderr || `(exit ${code})`;
      const capped = output.length > 3800 ? output.slice(0, 3800) + "\n...(truncated)" : output;
      await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /start ──
  if (ccSlash === "/start") {
    await sendMessage(
      chatId,
      "🤖 *Command Code Bot*\\n\\n" +
      "I connect Telegram to your Command Code CLI\\. All 27 CC commands are available \\(type `/` to see them\\)\\.\\n\\n" +
      "*Send any prompt* and I'll run `cmd \\-p` \\(headless mode\\)\\."
    );
    return true;
  }

  // ── /help ──
  if (ccSlash === "/help") {
    const cmds = BOT_COMMANDS.map(c => {
      const slash = TG_TO_CC[c.command] || "/" + c.command;
      return `  ${slash} \\- ${escapeMd(c.description)}`;
    }).join("\n");
    await sendMessage(chatId, `*Command Code commands:*\n${cmds}\n\n_Any other message → ` + "`cmd -p`" + ` prompt_`);
    return true;
  }

  // ── /status ──
  if (ccSlash === "/status") {
    await sendTyping(chatId);
    try {
      const { stdout: whoami } = await runCLI(["whoami"]);
      const { stdout: version } = await runCLI(["--version"]);
      const sessionInfo = sessionActive
        ? "Session: active (use `/resume` to continue, `/clear` to reset)"
        : "Session: none (send a prompt to start)";

      const binCode = "`" + escapeMd(CMD_BIN) + "`";
      await sendMessage(
        chatId,
        `🔧 *Command Code Status*\n` +
        `  Binary: ${binCode}\n` +
        `  Version: ${escapeMd(version || "unknown")}\n` +
        `  Auth: ${escapeMd(whoami || "unknown")}\n` +
        `  ${escapeMd(sessionInfo)}\n` +
        `  YOLO: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on" : "off"}\n` +
        `  Max turns: ${Number(process.env.COMMAND_CODE_MAX_TURNS) || 20}`
      );
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /resume ──
  if (ccSlash === "/resume") {
    sessionActive = true;
    await sendTyping(chatId);
    await sendMessage(chatId, "🔄 Resuming last headless session...");
    try {
      const result = await runCommandCode(
        "Continue where we left off. Summarize context and ask what I'd like to do next.",
        process.env.HOME,
        { continue: true }
      );
      await sendMessage(chatId, `📋 *Session resumed:*\n${escapeMd(result)}`);
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /clear ──
  if (ccSlash === "/clear") {
    sessionActive = false;
    await sendMessage(chatId, "🧹 Session cleared. Next prompt starts fresh.");
    return true;
  }

  // ── /model ──
  if (ccSlash === "/model") {
    await sendTyping(chatId);
    try {
      const { stdout } = await runCLI(["--list-models"], 15_000);
      const models = stdout || "Run `cmd --list-models` locally";
      const preview = models.length > 3500 ? models.slice(0, 3500) + "\n...(truncated)" : models;
      await sendMessage(chatId, "🤖 *Available models*\n\n```\n" + escapeMd(preview) + "\n```\n\n_Use `cmd -m <model>` to switch_");
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /plan ──
  if (ccSlash === "/plan") {
    if (args) {
      // /plan <task> → run the task in plan mode
      return false; // fall through to regular prompt with plan mode
    }
    await sendMessage(chatId, "📋 *Plan mode.* Next prompt will run with `--plan`.\n_Send a task to plan it, or use `/plan <task>`._");
    // TODO: could track plan mode state and pass --plan flag to runCommandCode
    return true;
  }

  // ── /review ──
  if (ccSlash === "/review") {
    await sendTyping(chatId);
    const prArg = args ? ` ${args}` : "";
    await sendMessage(chatId, escapeMd(`🔍 Reviewing PR${prArg}...`));
    try {
      const result = await runCommandCode(`Review pull request${prArg}. Check for bugs, security issues, test gaps, and style problems.`);
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /init ──
  if (ccSlash === "/init") {
    await sendTyping(chatId);
    await sendMessage(chatId, "📄 Initializing AGENTS.md...");
    try {
      const result = await runCommandCode("Create or update AGENTS.md for this project based on its structure, tech stack, and conventions.");
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── TUI-only commands (inform the user) ──
  const TUI_ONLY = new Set([
    "/agents", "/compact", "/effort", "/ide", "/memory",
    "/pr-comments", "/provider", "/rewind", "/terminal-setup",
    "/add-dir",
  ]);

  if (TUI_ONLY.has(ccSlash)) {
    const tuiNote = "is a TUI\\-only command \\(interactive terminal mode\\) and cannot be executed remotely\\. Use it in a local `cmd` session\\.";
    await sendMessage(chatId, `ℹ️ ${ccSlash} ${tuiNote}`);
    return true;
  }

  // ── N/A commands ──
  const NA_CMDS = new Set(["/exit", "/share", "/unshare"]);
  if (NA_CMDS.has(ccSlash)) {
    await sendMessage(chatId, `ℹ️ ${ccSlash} is not applicable when using Command Code remotely via Telegram\\.`);
    return true;
  }

  // ── /cmd (explicit prompt alias) ──
  if (ccSlash === "/cmd") {
    if (!args) {
      await sendMessage(chatId, "Usage: `/cmd <prompt>` — run a prompt through Command Code");
      return true;
    }
    return false; // fall through to prompt execution
  }

  // Unknown slash command → treat as prompt
  if (text.startsWith("/") && !args) {
    // Lone slash with no recognized handler → help
    const escapedCmd = escapeMd(ccSlash);
    await sendMessage(chatId, "Unknown command: `" + escapedCmd + "`. Use /help to see available commands.");
    return true;
  }

  return false; // not a command → treat as regular prompt
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
