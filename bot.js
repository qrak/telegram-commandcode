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
 */

import { spawn } from "node:child_process";
import { readFileSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

// ---------------------------------------------------------------------------
// Load .env if TELEGRAM_BOT_TOKEN not already set
// ---------------------------------------------------------------------------

function loadEnv() {
  if (process.env.TELEGRAM_BOT_TOKEN) return;
  const envPaths = [
    resolve(process.cwd(), ".env"),
    resolve(__dirname, ".env"),
  ];
  for (const p of envPaths) {
    if (existsSync(p)) {
      const lines = readFileSync(p, "utf8").split(/\r?\n/);
      for (const line of lines) {
        const m = line.match(/^TELEGRAM_BOT_TOKEN=(.*)/);
        if (m) {
          process.env.TELEGRAM_BOT_TOKEN = m[1].trim().replace(/^["']|["']$/g, "");
          return;
        }
      }
    }
  }
}
loadEnv();

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error("❌ TELEGRAM_BOT_TOKEN is required. Create a bot at @BotFather.");
  console.error("   Set via env var, or create a .env file with: TELEGRAM_BOT_TOKEN=your_token_here");
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
  const maxRetries = 3;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    const res = await fetch(url, {
      method: "POST",
      headers: isForm ? {} : { "Content-Type": "application/json" },
      body: isForm ? body : JSON.stringify(body),
    });

    if (res.ok) {
      return (await res.json()).result;
    }

    const errText = await res.text();

    // Retry on 429 (rate limit), 502 (bad gateway), 503 (service unavailable)
    if ((res.status === 429 || res.status === 502 || res.status === 503) && attempt < maxRetries) {
      const delay = Math.min(1000 * Math.pow(2, attempt) + Math.random() * 1000, 10000);
      await new Promise((r) => setTimeout(r, delay));
      continue;
    }

    throw new Error(`Telegram API ${method}: ${res.status} ${errText}`);
  }
}

async function sendMessage(chatId, text, parseMode = "MarkdownV2") {
  // Split long messages (Telegram limit: 4096 chars)
  const maxLen = 4000;

  async function send(text, pm) {
    return api("sendMessage", {
      chat_id: chatId,
      text,
      parse_mode: pm,
      link_preview_options: { is_disabled: true },
    }).catch(async (err) => {
      // If MarkdownV2 parse fails, fall back to plain text
      if (pm === "MarkdownV2" && err.message.includes("can't parse entities")) {
        return send(text, "");
      }
      throw err;
    });
  }

  if (text.length <= maxLen) {
    return send(text, parseMode);
  }

  // Send in chunks
  const chunks = [];
  for (let i = 0; i < text.length; i += maxLen) {
    chunks.push(text.slice(i, i + maxLen));
  }
  for (let i = 0; i < chunks.length; i++) {
    const header = `\\(${i + 1}/${chunks.length}\\)\n`;
    await send(header + chunks[i], parseMode);
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
// Reactions — visual feedback for message processing
// ---------------------------------------------------------------------------

async function setReaction(chatId, messageId, emoji) {
  return api("setMessageReaction", {
    chat_id: chatId,
    message_id: messageId,
    reaction: [{ type: "emoji", emoji }],
    is_big: false,
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Edit message — single-message streaming (edit in place)
// ---------------------------------------------------------------------------

async function editMessage(chatId, messageId, text, parseMode = "MarkdownV2") {
  return api("editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: parseMode,
    link_preview_options: { is_disabled: true },
  }).catch(async (err) => {
    if (parseMode === "MarkdownV2" && err.message.includes("can't parse entities")) {
      return editMessage(chatId, messageId, text, "");
    }
    // Message too old to edit — send new one
    if (err.message.includes("message can't be edited")) {
      return sendMessage(chatId, text, parseMode);
    }
    throw err;
  });
}

// ---------------------------------------------------------------------------
// File download from Telegram
// ---------------------------------------------------------------------------

const DOWNLOAD_DIR = join(tmpdir(), "telegram-cmd");

function ensureDownloadDir() {
  if (!existsSync(DOWNLOAD_DIR)) {
    mkdirSync(DOWNLOAD_DIR, { recursive: true });
  }
}

/**
 * Download a file from Telegram by file_id.
 * Returns the local file path, or null on failure.
 */
async function downloadTelegramFile(fileId, ext = "") {
  ensureDownloadDir();
  try {
    const fileInfo = await api("getFile", { file_id: fileId });
    if (!fileInfo || !fileInfo.file_path) return null;

    const url = `https://api.telegram.org/file/bot${BOT_TOKEN}/${fileInfo.file_path}`;
    const res = await fetch(url);
    if (!res.ok) return null;

    const buffer = Buffer.from(await res.arrayBuffer());
    const localName = `${Date.now()}_${fileId.slice(0, 8)}${ext || "." + fileInfo.file_path.split(".").pop()}`;
    const localPath = join(DOWNLOAD_DIR, localName);
    writeFileSync(localPath, buffer);
    return localPath;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Voice transcription (OpenAI Whisper API)
// ---------------------------------------------------------------------------

const OPENAI_KEY = process.env.OPENAI_API_KEY;

async function transcribeVoice(filePath) {
  if (!OPENAI_KEY) return null;

  try {
    const audio = readFileSync(filePath);
    const blob = new Blob([audio], { type: "audio/ogg" });
    const form = new FormData();
    form.set("file", blob, "voice.ogg");
    form.set("model", "whisper-1");

    const res = await fetch("https://api.openai.com/v1/audio/transcriptions", {
      method: "POST",
      headers: { Authorization: `Bearer ${OPENAI_KEY}` },
      body: form,
    });

    if (!res.ok) return null;
    const data = await res.json();
    return data.text || null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// File path detection in command output — for auto-sending files
// ---------------------------------------------------------------------------

const FILE_PATH_RE = /(\/(?:home|tmp|var|usr|etc|opt|mnt|media|run|srv)[^\s"'\])\]]{3,})/g;
const MEDIA_RE = /MEDIA:(\/[^\s"'\])\]]{3,})/g;

/**
 * Scan text for file paths that actually exist on disk.
 * Returns array of { path, type } where type is "photo" or "file".
 * Supports explicit MEDIA:/path/to/file markers (Hermes-compatible) and
 * bare absolute paths.
 */
function findFilePaths(text) {
  const matches = new Set();

  // Check for explicit MEDIA: markers first
  let m;
  while ((m = MEDIA_RE.exec(text)) !== null) {
    const p = m[1].replace(/[.,;:!?)]$/, "");
    if (existsSync(p)) {
      const lower = p.toLowerCase();
      const isPhoto = /\.(png|jpg|jpeg|gif|webp|bmp)$/.test(lower);
      matches.add(JSON.stringify({ path: p, type: isPhoto ? "photo" : "file" }));
    }
  }

  // Fallback: scan for bare absolute paths
  FILE_PATH_RE.lastIndex = 0;
  while ((m = FILE_PATH_RE.exec(text)) !== null) {
    const p = m[1].replace(/[.,;:!?)]$/, "");
    if (existsSync(p)) {
      const lower = p.toLowerCase();
      const isPhoto = /\.(png|jpg|jpeg|gif|webp|bmp)$/.test(lower);
      matches.add(JSON.stringify({ path: p, type: isPhoto ? "photo" : "file" }));
    }
  }
  return [...matches].map((s) => JSON.parse(s));
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

  // Model override — if user selected a model via /model
  if (sessionOpts.model) {
    args.push("-m", sessionOpts.model);
  }

  // Plan mode — if user enabled via /plan
  if (sessionOpts.planMode) {
    args.push("--plan");
  }

  // Session chaining: --continue resumes the most recent headless session
  if (sessionOpts.continue) {
    args.push("--continue");
  } else if (sessionOpts.resume) {
    args.push("--resume", sessionOpts.resume);
  }

  // Skip onboarding for automated runs
  args.push("--skip-onboarding");

  // Kill any previous running process for this user (interrupt support)
  if (sessionOpts.chatId) {
    killRunningProcess(sessionOpts.chatId);
  }

  return new Promise((resolve, reject) => {
    const child = spawn(CMD_BIN, args, {
      cwd,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 10 * 60 * 1000, // 10 minute timeout for complex tasks
    });

    // Register for interrupt support
    if (sessionOpts.chatId) {
      setRunningProcess(sessionOpts.chatId, child);
    }

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    child.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    child.on("close", (code) => {
      // Clean up process tracking
      if (sessionOpts.chatId) {
        runningProcesses.delete(sessionOpts.chatId);
      }

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
  { command: "model",      description: "List models or switch: /model <name>" },
  { command: "plan",       description: "Enter plan mode or plan a task" },
  { command: "prcomments", description: "Fetch PR comments — TUI" },            // /pr-comments
  { command: "provider",   description: "Select AI provider — TUI" },
  { command: "resume",     description: "Resume a past conversation" },
  { command: "review",     description: "Review a pull request — TUI" },
  { command: "rewind",     description: "Restore to previous checkpoint — TUI" },
  { command: "share",      description: "Share conversation — N/A remotely" },
  { command: "skills",     description: "Browse and manage agent skills" },      // → cmd skills
  { command: "steer",      description: "Give mid-session guidance: /steer <instruction>" },
  { command: "stop",       description: "Stop the running agent" },
  { command: "retry",      description: "Re-run the last prompt" },
  { command: "whoami",     description: "Show your user info" },
  { command: "background", description: "Run a task in background: /background <prompt>" },
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

// Per-user session state (supports concurrent users)
const sessions = new Map();

function getSession(chatId) {
  if (!sessions.has(chatId)) {
    sessions.set(chatId, { active: false, model: null, planMode: false, oneShotPlan: false, steer: null, lastPrompt: null });
  }
  return sessions.get(chatId);
}

/** Reset a single user's session state — /clear */
function resetSession(chatId) {
  sessions.delete(chatId);
  messageQueues.delete(chatId);
}

// ── Per-user message queue (non-blocking input like Hermes) ──
const messageQueues = new Map();  // chatId → { chatId, userMsgId, statusMsgId, prompt }[]
const processing = new Set();     // Set of chatIds currently processing a message

function enqueueMessage(chatId, userMsgId, statusMsgId, prompt) {
  if (!messageQueues.has(chatId)) {
    messageQueues.set(chatId, []);
  }

  // If user has a running process, mark the new prompt with interrupt prefix
  const hasRunning = getRunningProcess(chatId) !== null;
  const finalPrompt = hasRunning ? `⚡ Previous execution interrupted.\n\n${prompt}` : prompt;

  messageQueues.get(chatId).push({ chatId, userMsgId, statusMsgId, prompt: finalPrompt });
  // Acknowledge immediately
  setReaction(chatId, userMsgId, "⏳").catch(() => {});
  processQueue(chatId);
}

async function processQueue(chatId) {
  if (processing.has(chatId)) return;
  processing.add(chatId);

  try {
    while (messageQueues.get(chatId)?.length > 0) {
      const { chatId, userMsgId, statusMsgId, prompt } = messageQueues.get(chatId).shift();
      const state = getSession(chatId);
      await processPrompt(chatId, userMsgId, statusMsgId, prompt, state);
    }
  } catch (err) {
    console.error(`[queue] Error processing for ${chatId}:`, err.message);
  } finally {
    processing.delete(chatId);
  }
}

// Per-user running process tracking — supports interrupting running commands
const runningProcesses = new Map();

function getRunningProcess(chatId) {
  return runningProcesses.get(chatId) || null;
}

function setRunningProcess(chatId, child) {
  runningProcesses.set(chatId, child);
}

function killRunningProcess(chatId) {
  const child = runningProcesses.get(chatId);
  if (child && !child.killed) {
    child.kill("SIGINT");
    // Force kill after 3s if it didn't stop
    setTimeout(() => {
      if (!child.killed) child.kill("SIGKILL");
    }, 3000);
  }
  runningProcesses.delete(chatId);
}

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
      "🤖 *Command Code Bot*\n\n" +
      "I connect Telegram to your Command Code CLI\\. All 27 CC commands are available \\(type `/` to see them\\)\\.\n\n" +
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
    const state = getSession(chatId);
    await sendTyping(chatId);
    try {
      const { stdout: whoami } = await runCLI(["whoami"]);
      const { stdout: version } = await runCLI(["--version"]);

      // Get actual model name — custom override or parse default from --list-models
      let modelName = state.model;
      if (!modelName) {
        const { stdout: models } = await runCLI(["--list-models"], 10_000);
        const m = models?.match(/^(\S+)\s+.+\(default\)/m);
        modelName = m ? m[1] : "unknown";
      }

      const sessionInfo = state.active
        ? "Session: active (`/resume` to continue, `/clear` to reset)"
        : "Session: none (send any prompt to start)";

      const planInfo = state.planMode ? "Plan mode: ON" : "Plan mode: off";

      const steerInfo = state.steer
        ? "Steer: " + escapeMd(state.steer.slice(0, 60)) + (state.steer.length > 60 ? "…" : "")
        : "No steer set";

      await sendMessage(
        chatId,
        `╔══ *Command Code* ══\n` +
        `╟ Model: \`${escapeMd(modelName)}\`${state.model ? "" : " \\(default\\)"}\n` +
        `╟ Binary: \`${escapeMd(CMD_BIN)}\` v${escapeMd(version || "?")}\n` +
        `╟ Auth: ${escapeMd(whoami || "not logged in")}\n` +
        `╟ ${escapeMd(sessionInfo)}\n` +
        `╟ Plan: ${planInfo} · YOLO: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on" : "off"} · Turns: ${Number(process.env.COMMAND_CODE_MAX_TURNS) || 20}\n` +
        `╟ ${steerInfo}\n` +
        `╚══ Use \`/model\` to switch, \`/steer <msg>\` to guide, \`/clear\` to reset`
      );
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /resume ──
  if (ccSlash === "/resume") {
    const state = getSession(chatId);
    state.active = true;
    await sendTyping(chatId);
    await sendMessage(chatId, "🔄 Resuming last headless session...");
    try {
      const result = await runCommandCode(
        "Continue where we left off. Summarize context and ask what I'd like to do next.",
        process.env.HOME,
        { continue: true, model: state.model, planMode: state.planMode }
      );
      await sendMessage(chatId, `📋 *Session resumed:*\n${escapeMd(result)}`);
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /clear ──
  if (ccSlash === "/clear") {
    resetSession(chatId);
    await sendMessage(chatId, "🧹 Session cleared. Model reset to default, plan mode off. Next prompt starts fresh.");
    return true;
  }

  // ── /model ──
  if (ccSlash === "/model") {
    const state = getSession(chatId);
    // /model <name> → switch to that model
    if (args) {
      state.model = args;
      await sendMessage(chatId, `✅ Switched to model: *${escapeMd(args)}*\n\nNext prompts will use \`-m ${escapeMd(args)}\`.`);
      return true;
    }

    // /model (no args) → list available models
    await sendTyping(chatId);
    try {
      const { stdout } = await runCLI(["--list-models"], 15_000);
      const models = stdout || "Run `cmd --list-models` locally";
      const preview = models.length > 3500 ? models.slice(0, 3500) + "\n...(truncated)" : models;

      // Extract the default model from the listing (line with "(default)")
      let defaultModel = "unknown";
      const defaultMatch = stdout?.match(/^(\S+)\s+.+\(default\)/m);
      if (defaultMatch) defaultModel = defaultMatch[1];

      const current = state.model
        ? "\n*Currently selected:* `" + escapeMd(state.model) + "`\n"
        : "\n*Default model:* `" + escapeMd(defaultModel) + "`\n";
      await sendMessage(
        chatId,
        "🤖 *Available models*\n\n```\n" + escapeMd(preview) + "\n```\n" +
        current +
        "\n_Use `/model <name>` to switch, e\\.g\\. `/model claude-sonnet-4-6`_"
      );
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /plan ──
  if (ccSlash === "/plan") {
    const state = getSession(chatId);
    if (args) {
      // /plan <task> → run the task in plan mode once
      state.planMode = true;
      state.oneShotPlan = true;
      return false; // fall through to prompt execution
    }
    state.planMode = !state.planMode;
    state.oneShotPlan = false;
    const status = state.planMode ? "ON ✅" : "OFF ❌";
    await sendMessage(chatId, `📋 Plan mode: *${status}*\n\n` +
      (state.planMode
        ? "Next prompts will run with `--plan`. Use `/plan` again to disable.\n_Or use `/plan <task>` for a one-shot plan._"
        : "Next prompts will run in normal mode."));
    return true;
  }

  // ── /stop — kill running process ──
  if (ccSlash === "/stop") {
    const child = getRunningProcess(chatId);
    if (child && !child.killed) {
      killRunningProcess(chatId);
      await sendMessage(chatId, "🛑 Execution stopped by user\\.");
    } else {
      await sendMessage(chatId, "🤷 No active execution to stop\\.");
    }
    return true;
  }

  // ── /retry — re-run last prompt ──
  if (ccSlash === "/retry") {
    const state = getSession(chatId);
    if (!state.lastPrompt) {
      await sendMessage(chatId, "🤷 No previous prompt to retry\\. Send a message first\\.");
      return true;
    }
    return false; // fall through to prompt execution with lastPrompt
  }

  // ── /whoami — show user info ──
  if (ccSlash === "/whoami") {
    await sendMessage(
      chatId,
      `*User info*\n` +
      `  ID: \`${escapeMd(userId?.toString() || "unknown")}\`\n` +
      `  Username: @${escapeMd(username)}\n` +
      `  Access: ${ALLOWED.includes("any") ? "unrestricted" : "restricted"}\n` +
      `  Platform: Telegram\n` +
      `  Chat type: ${escapeMd(chatType)}\n` +
      `  PID: ${getRunningProcess(chatId) ? "active" : "idle"}`
    );
    return true;
  }

  // ── /background <prompt> — run in background, notify when done ──
  if (ccSlash === "/background") {
    if (!args) {
      await sendMessage(chatId, "Usage: `/background <prompt>` — run a task in the background\\.\n\nYou'll be notified here when it completes\\.");
      return true;
    }
    const state = getSession(chatId);
    const bgId = `bg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    await sendMessage(chatId, `🔄 Background task started: "${escapeMd(args.slice(0, 100))}"\nTask ID: \`${bgId}\``);

    // Spawn background cmd process (detached)
    const cmdArgs = ["-p", args, "--skip-onboarding"];
    if (process.env.COMMAND_CODE_YOLO !== "false") cmdArgs.push("--yolo");
    const maxTurns = Number(process.env.COMMAND_CODE_MAX_TURNS) || 20;
    cmdArgs.push("--max-turns", String(maxTurns));
    if (state.model) cmdArgs.push("-m", state.model);
    if (state.planMode) cmdArgs.push("--plan");

    const child = spawn(CMD_BIN, cmdArgs, {
      cwd: process.env.HOME,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 30 * 60 * 1000, // 30 min timeout for background tasks
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d.toString(); });
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      const output = stdout.trim() || stderr.trim() || `(exit ${code})`;
      const escaped = escapeMd(output.slice(0, 3800));
      const status = code === 0 ? "✅" : "⚠️";
      sendMessage(chatId, `${status} *Background task complete* \\(${bgId}\\)\n\n${escaped}`).catch(() => {});
    });
    child.on("error", (err) => {
      sendMessage(chatId, `❌ *Background task failed* \\(${bgId}\\): ${escapeMd(err.message)}`).catch(() => {});
    });
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

  // ── /steer ──
  if (ccSlash === "/steer") {
    const state = getSession(chatId);
    if (!args) {
      // /steer (no args) → show current steer
      if (state.steer) {
        await sendMessage(chatId, `🧭 *Current steer:*\n\n${escapeMd(state.steer)}\n\n_Use \`/steer clear\` to remove it._`);
      } else {
        await sendMessage(chatId, "🧭 *No steer set.*\n\nUse `/steer <instruction>` to guide the AI's behavior mid-session.");
      }
      return true;
    }
    if (args.toLowerCase() === "clear") {
      state.steer = null;
      await sendMessage(chatId, "🧭 Steer cleared.");
      return true;
    }
    state.steer = args;
    await sendMessage(chatId, `🧭 Steer set.\n\n${escapeMd(args)}\n\nIt will be applied to all subsequent prompts. Use \`/steer clear\` to remove it.`);
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

/**
 * Process a user prompt through Command Code with reactions + single-message editing.
 * Reacts to the user's original message (like Hermes), edits status message in-place.
 */
async function processPrompt(chatId, userMsgId, statusMsgId, prompt, state) {
  // React to user's message immediately (Hermes-style)
  await setReaction(chatId, userMsgId, "👀");

  try {
    // Store prompt for /retry
    state.lastPrompt = prompt;

    // Progress step 1: thinking
    await editMessage(chatId, statusMsgId, `🤔 *Processing:* ${escapeMd(prompt.slice(0, 100))}...`);

    // Prepend steer instruction if set
    const finalPrompt = state.steer ? `${state.steer}\n\n${prompt}` : prompt;

    const result = await runCommandCode(
      finalPrompt,
      process.env.HOME,
      { model: state.model, planMode: state.planMode, continue: state.active, chatId }
    );
    state.active = true;
    if (state.oneShotPlan) { state.planMode = false; state.oneShotPlan = false; }

    // Build final response
    const escapedResult = result ? escapeMd(result) : "";
    const doneMsg = result
      ? `✅ *Done:* ${escapeMd(prompt.slice(0, 100))}\n\n${escapedResult}`
      : `✅ *Done* \\— ${escapeMd(prompt.slice(0, 100))}`;

    // Edit the status message with the final result
    await editMessage(chatId, statusMsgId, doneMsg);

    // Auto-send files detected in output (MEDIA: prefix + bare paths)
    if (result) {
      const paths = findFilePaths(result);
      for (const { path, type } of paths.slice(0, 5)) {
        try {
          if (type === "photo") {
            const form = new FormData();
            form.set("chat_id", chatId);
            form.set("photo", new Blob([readFileSync(path)]), path.split("/").pop());
            await api("sendPhoto", form);
          } else {
            const form = new FormData();
            form.set("chat_id", chatId);
            form.set("document", new Blob([readFileSync(path)]), path.split("/").pop());
            await api("sendDocument", form);
          }
        } catch { /* best-effort */ }
      }
    }

    // React ✅ on user's message (replaces 👀)
    await setReaction(chatId, userMsgId, "✅");
    return result;
  } catch (err) {
    await editMessage(chatId, statusMsgId, `❌ *Error:* ${escapeMd(err.message)}`);
    await setReaction(chatId, userMsgId, "❌");
    throw err;
  }
}

/**
 * Check if the bot is @mentioned in a message's entities.
 */
function isBotMentioned(msg, botUsername) {
  if (!msg.entities) return false;
  const username = botUsername ? botUsername.toLowerCase() : "";
  if (!username) return true; // can't check — allow
  return msg.entities.some((e) => {
    if (e.type !== "mention") return false;
    const mention = msg.text?.slice(e.offset, e.offset + e.length).toLowerCase();
    return mention === `@${username}`;
  });
}

/**
 * Check if a message is a reply to one of the bot's own messages.
 */
function isReplyToBot(msg, botId) {
  if (!msg.reply_to_message || !botId) return false;
  return msg.reply_to_message.from?.id === botId;
}

async function poll() {
  try {
    const updates = await api("getUpdates", {
      offset: lastUpdateId + 1,
      timeout: 30,
      allowed_updates: ["message"],
    });

    // Get bot info once per poll cycle for mention/reply checks
    const botInfo = await api("getMe", {});
    const botUsername = botInfo.username;
    const botId = botInfo.id;

    for (const update of updates) {
      lastUpdateId = update.update_id;

      const msg = update.message;
      if (!msg) continue;

      const chatId = String(msg.chat.id);
      const chatType = msg.chat.type; // "private", "group", "supergroup"
      const userId = msg.from?.id;
      const username = msg.from?.username || msg.from?.first_name || "unknown";
      const text = (msg.text || msg.caption || "").trim();

      // ── Group chat: only respond when @mentioned or replying to bot ──
      if (chatType === "group" || chatType === "supergroup") {
        const mentioned = isBotMentioned(msg, botUsername);
        const replyToBot = isReplyToBot(msg, botId);
        if (!mentioned && !replyToBot) continue;
      }

      // ── Access control ──
      if (!isAllowed(userId)) {
        console.log(`⛔ Blocked message from user ${userId} (${username})`);
        await sendMessage(chatId, escapeMd("⛔ Sorry, you are not authorized to use this bot."));
        continue;
      }

      console.log(`📩 [${username}] [${chatType}] ${(text || "(media)").slice(0, 80)}`);

      // ── Handle media messages (photos, documents, voice) ──
      let mediaPrompt = null;
      let mediaDesc = "";

      // Photo
      if (msg.photo) {
        const photo = msg.photo[msg.photo.length - 1]; // largest size
        const localPath = await downloadTelegramFile(photo.file_id, ".jpg");
        if (localPath) {
          mediaPrompt = `User sent a photo (saved at ${localPath})${text ? `. Caption: ${text}` : ""}. Review any text visible in the image and respond appropriately.`;
          mediaDesc = "📷 Photo";
        }
      }

      // Document
      if (msg.document) {
        const doc = msg.document;
        const ext = doc.file_name ? "." + doc.file_name.split(".").pop() : "";
        const localPath = await downloadTelegramFile(doc.file_id, ext);
        if (localPath) {
          mediaPrompt = `User sent a file "${doc.file_name || "unnamed"}" (saved at ${localPath})${text ? `. Message: ${text}` : ""}. Read and process the file if needed.`;
          mediaDesc = "📄 File";
        }
      }

      // Voice
      if (msg.voice) {
        const voice = msg.voice;
        const localPath = await downloadTelegramFile(voice.file_id, ".ogg");
        if (localPath) {
          const transcription = await transcribeVoice(localPath);
          if (transcription) {
            mediaPrompt = `User sent a voice message. Transcription: "${transcription}". Respond to the content.`;
            mediaDesc = "🎤 Voice";
          } else {
            mediaPrompt = `User sent a voice message (saved at ${localPath}). The message could not be transcribed — let the user know.`;
            mediaDesc = "🎤 Voice (untranscribed)";
          }
        }
      }

      // If media was received, handle it as a prompt
      if (mediaPrompt) {
        const initialMsg = await sendMessage(chatId, `🚀 ${mediaDesc}: processing...`);
        const statusMsgId = initialMsg?.message_id || initialMsg?.result?.message_id;
        if (statusMsgId && msg.message_id) {
          enqueueMessage(chatId, msg.message_id, statusMsgId, mediaPrompt);
        }
        continue;
      }

      // ── Text messages only from here ──
      if (!text) continue;

      // ── Slash commands ──
      if (text.startsWith("/")) {
        const handled = await handleCommand(chatId, text);
        if (handled) continue;

        // /retry — re-run last prompt
        if (text.trim() === "/retry" || text.startsWith("/retry ")) {
          const state = getSession(chatId);
          const prompt = text.includes(" ") ? text.slice(7).trim() : state.lastPrompt;
          if (!prompt) {
            await sendMessage(chatId, "🤷 No previous prompt to retry\\.");
            continue;
          }
          const initialMsg = await sendMessage(chatId, `🔄 Retrying: \`${escapeMd(prompt.slice(0, 200))}\``);
          const statusMsgId = initialMsg?.message_id || initialMsg?.result?.message_id;
          if (statusMsgId && msg.message_id) {
            enqueueMessage(chatId, msg.message_id, statusMsgId, prompt);
          }
          continue;
        }

        // /cmd with args — treat as prompt
        if (text.startsWith("/cmd ")) {
          const prompt = text.slice(5).trim();
          if (!prompt) continue;
          const initialMsg = await sendMessage(chatId, `🚀 Running: \`${escapeMd(prompt.slice(0, 200))}\``);
          const statusMsgId = initialMsg?.message_id || initialMsg?.result?.message_id;
          if (statusMsgId && msg.message_id) {
            enqueueMessage(chatId, msg.message_id, statusMsgId, prompt);
          }
          continue;
        }
        continue;
      }

      // ── Regular prompt ──
      const initialMsg = await sendMessage(chatId, `🚀 Running: \`${escapeMd(text.slice(0, 200))}\``);
      const statusMsgId = initialMsg?.message_id || initialMsg?.result?.message_id;
      if (statusMsgId && msg.message_id) {
        enqueueMessage(chatId, msg.message_id, statusMsgId, text);
      }
    }
  } catch (err) {
    console.error("Poll error:", err.message);
    await new Promise((r) => setTimeout(r, 5000));
  }
}

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------

let shuttingDown = false;

function setupShutdown() {
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`\n${signal} received. Shutting down gracefully...`);
    // lastUpdateId is already in memory; if we wanted persistence
    // we could save it, but for a daemon restarting is fine.
    process.exit(0);
  };

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  setupShutdown();

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
