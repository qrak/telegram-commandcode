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

import { spawn, execSync } from "node:child_process";
import { readFileSync, existsSync, writeFileSync, mkdirSync, readdirSync, statSync } from "node:fs";
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

const CC_CONFIG_PATH = resolve(process.env.HOME || "/home/qrak", ".commandcode", "config.json");

function readCCConfig() {
  try {
    return JSON.parse(readFileSync(CC_CONFIG_PATH, "utf8"));
  } catch { return {}; }
}

function writeCCConfig(config) {
  mkdirSync(resolve(CC_CONFIG_PATH, ".."), { recursive: true });
  writeFileSync(CC_CONFIG_PATH, JSON.stringify(config, null, 2));
}

const PROJECTS_DIR = resolve(process.env.HOME || "/home/qrak", ".commandcode", "projects", "home-qrak");

/**
 * Read the most recent session from Command Code's project storage.
 * Extracts the last user+assistant exchange as plain text context for /resume.
 */
function readLatestSession() {
  try {
    const files = readdirSync(PROJECTS_DIR).filter(f => f.endsWith(".jsonl") && !f.includes(".checkpoints."));
    if (files.length === 0) return null;
    // Sort by mtime descending, pick the latest non-checkpoint session
    files.sort((a, b) => statSync(resolve(PROJECTS_DIR, b)).mtimeMs - statSync(resolve(PROJECTS_DIR, a)).mtimeMs);
    const latest = resolve(PROJECTS_DIR, files[0]);
    const lines = readFileSync(latest, "utf8").split("\n").filter(Boolean);
    if (lines.length < 2) return null;

    // Walk backwards to find the last complete user→assistant turn
    let lastUser = null, lastAssistant = null;
    for (let i = lines.length - 1; i >= 0; i--) {
      try {
        const entry = JSON.parse(lines[i]);
        const role = entry.role;
        const text = entry.content?.find(c => c?.type === "text")?.text || "";
        if (!text) continue;
        if (role === "assistant" && !lastAssistant) {
          lastAssistant = text;
        } else if (role === "user" && lastAssistant && !lastUser) {
          lastUser = text;
          break;
        }
      } catch { continue; }
    }
    if (!lastUser && !lastAssistant) return null;

    let ctx = "--- Last Command Code session ---\n";
    if (lastUser) ctx += `[User]: ${lastUser.slice(0, 3000)}\n`;
    if (lastAssistant) ctx += `[Assistant]: ${lastAssistant.slice(0, 3000)}\n`;
    ctx += "--- End of session ---\n";
    return ctx;
  } catch { return null; }
}

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error("❌ TELEGRAM_BOT_TOKEN is required. Create a bot at @BotFather.");
  console.error("   Set via env var, or create a .env file with: TELEGRAM_BOT_TOKEN=your_token_here");
  process.exit(1);
}

const API_BASE = `https://api.telegram.org/bot${BOT_TOKEN}`;
const CMD_BIN = process.env.COMMAND_CODE_CMD || "cmd";

// Security limits
const MAX_PROMPT_LENGTH = 5000; // max characters per prompt
const RATE_LIMIT_WINDOW = 2000;  // ms between successive prompts from same user

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
  const onChunk = sessionOpts.onChunk; // optional streaming callback(text) — called with accumulated stdout

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

  // Added directories — /add-dir support
  if (sessionOpts.addDirs && sessionOpts.addDirs.length > 0) {
    for (const dir of sessionOpts.addDirs) {
      args.push("--add-dir", dir);
    }
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
    let lastStream = 0;

    child.stdout.on("data", (data) => {
      stdout += data.toString();
      // Throttle streaming updates to ~1 per 500ms
      if (onChunk) {
        const now = Date.now();
        if (now - lastStream > 500) {
          lastStream = now;
          const text = stdout.trim();
          if (text) onChunk(text);
        }
      }
    });

    child.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    child.on("close", (code, signal) => {
      // Clean up process tracking
      if (sessionOpts.chatId) {
        runningProcesses.delete(sessionOpts.chatId);
      }

      // Final streaming update before resolving
      if (onChunk) {
        const text = stdout.trim();
        if (text) onChunk(text);
      }

      // Process killed by signal (not a normal exit) — always an error
      if (signal) {
        const msg = `⚠️ ${CMD_BIN} killed by ${signal}`;
        resolve(msg);
        return;
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

      if (code === 0) {
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
  { command: "add_dir",    description: "Add directory to workspace: /add-dir <path>" },
  { command: "agents",     description: "Show agent configuration info" },
  { command: "background", description: "Run a task in background: /background <prompt>" },
  { command: "clear",      description: "Clear conversation history (fresh start)" },
  { command: "cmd",        description: "Run a prompt: /cmd <prompt>" },
  { command: "compact",    description: "Compact conversation history" },
  { command: "compactmode", description: "Select compact mode: /compact-mode <mode>" },
  { command: "configuremodels", description: "Choose model per task: /configure-models" },
  { command: "context",    description: "Show context window usage" },
  { command: "courses",    description: "Open Command Code courses in browser" },
  { command: "effort",     description: "Set reasoning effort: /effort <low|medium|high|max>" },
  { command: "exit",       description: "Exit session (N/A remotely)" },
  { command: "feedback",   description: "Submit feedback about Command Code" },
  { command: "fork",       description: "Fork conversation into new session" },
  { command: "goal",       description: "Set objective: /goal <text|clear|status>" },
  { command: "help",       description: "Show available commands" },
  { command: "ide",        description: "Connect IDE — local only" },
  { command: "info",       description: "Show system information" },
  { command: "init",       description: "Initialize AGENTS.md for this project" },
  { command: "learntaste", description: "Learn taste from other agents" },
  { command: "login",      description: "Authenticate with Command Code" },
  { command: "logout",     description: "Remove stored authentication" },
  { command: "mcp",        description: "Manage MCP server connections" },
  { command: "about",      description: "Show bot info and links" },
  { command: "memory",     description: "Manage memory: /memory or /memory <instruction>" },
  { command: "model",      description: "List models or switch: /model <name>" },
  { command: "new",        description: "Clear session — same as /clear" },
  { command: "plan",       description: "Enter plan mode or plan a task" },
  { command: "prcomments", description: "Fetch PR comments for current branch" },
  { command: "provider",   description: "Set AI provider: /provider <name>" },
  { command: "queue",      description: "Queue prompt for next turn: /queue <prompt>" },
  { command: "reload",     description: "Restart bot and resume session" },
  { command: "rename",     description: "Rename current session: /rename <name>" },
  { command: "resume",     description: "Resume a past conversation" },
  { command: "retry",      description: "Re-run the last prompt" },
  { command: "review",     description: "Review a pull request: /review or /review <pr>" },
  { command: "rewind",     description: "Restore to previous checkpoint (TUI only)" },
  { command: "share",      description: "Share conversation — N/A remotely" },
  { command: "skills",     description: "Browse and manage agent skills" },
  { command: "status",     description: "Show environment status (model, session, config)" },
  { command: "steer",      description: "Give mid-session guidance: /steer <instruction>" },
  { command: "stop",       description: "Stop the running agent" },
  { command: "taste",      description: "Manage Taste learning" },
  { command: "terminalsetup", description: "VSCode keybindings — local only" },
  { command: "undo",       description: "Back up N turns and re-prompt: /undo [N]" },
  { command: "unshare",    description: "Stop sharing — N/A remotely" },
  { command: "update",     description: "Update Command Code to latest version" },
  { command: "usage",      description: "Show credits, plan, and usage metrics" },
  { command: "version",    description: "Show Command Code version" },
  { command: "whoami",     description: "Show your user info" },
];

// Map telegram-safe command names (no hyphens) back to real slash commands
const TG_TO_CC = {
  "add_dir": "/add-dir",
  "compactmode": "/compact-mode",
  "configuremodels": "/configure-models",
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
    const persistedModel = readCCConfig().model || null;
    sessions.set(chatId, { active: false, model: persistedModel, planMode: false, oneShotPlan: false, steer: null, goal: null, lastPrompt: null, addDirs: [], queuedPrompts: [], sessionName: null });
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
const rateLimits = new Map();    // userId → last message timestamp

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
      const { chatId: qChatId, userMsgId, statusMsgId, prompt } = messageQueues.get(chatId).shift();
      const state = getSession(qChatId);
      await processPrompt(qChatId, userMsgId, statusMsgId, prompt, state);
    }
  } catch (err) {
    console.error(`[queue] Error processing for ${chatId}:`, err.message);
  } finally {
    processing.delete(chatId);
  }
}

// Per-user running process tracking — supports interrupting running commands
const runningProcesses = new Map();
const backgroundProcesses = new Set(); // detached bg processes (not per-user)

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
      cwd: process.env.HOME,
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
async function handleCommand(chatId, text, userInfo = {}) {
  const { userId, username, chatType } = userInfo;
  const parts = text.split(/\s+/);
  const rawCmd = parts[0].toLowerCase();
  // Convert TG-safe name back to CC slash command (e.g. add_dir → /add-dir)
  const ccSlash = TG_TO_CC[rawCmd.slice(1)] || rawCmd;
  const args = parts.slice(1).join(" ");

  // ── Commands that forward directly to CLI subcommands ──
  const CLI_MAP = {
    "/feedback":  { args: ["feedback", args], msg: "📝 Submitting feedback..." },
    "/learntaste": { args: ["learn-taste", ...(args ? args.split(/\s+/) : [])], msg: "🧠 Learning taste from repositories..." },
    "/learn-taste": { args: ["learn-taste", ...(args ? args.split(/\s+/) : [])], msg: "🧠 Learning taste from repositories..." },
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
      // If command failed with an unknown-subcommand error, show help instead
      if (code !== 0 && stderr && !stdout && cliArgs.length > 1) {
        const helpResult = await runCLI(cliArgs.slice(0, 1).concat(["--help"]), 10_000);
        const helpText = helpResult.stdout || "";
        if (helpText) {
          const capped = helpText.length > 3800 ? helpText.slice(0, 3800) + "\n...(truncated)" : helpText;
          await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
        } else {
          await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
        }
      } else {
        const capped = output.length > 3800 ? output.slice(0, 3800) + "\n...(truncated)" : output;
        await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
      }
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /start ──
  if (ccSlash === "/start") {
    await api("sendMessage", {
      chat_id: chatId,
      text: "🤖 *Command Code Bot* — Telegram → CLI bridge\n\n" +
        "I connect Telegram to your Command Code\\. Send any prompt, and I'll run `cmd \\-p` in headless mode and stream the response back\\.\n\n" +
        "*✨ Features*\n" +
        "  🎤 Voice messages → Whisper transcription\n" +
        "  📷 Photo/file analysis via vision models\n" +
        "  ⏳ Live streaming response output\n" +
        "  🧹 Session chaining with `/clear`, `/resume`\n" +
        "  🎯 Goal + steer guidance\n" +
        "  📋 Queue multiple prompts\n" +
        "  🔄 Background tasks with `/background`\n" +
        "  💾 Taste learning & memory management\n\n" +
        "*Quick start:* send any message to chat\\!\n" +
        "Type `/help` for all commands, `/model` to pick a model\\.",
      parse_mode: "MarkdownV2",
      link_preview_options: { is_disabled: true },
      reply_markup: JSON.stringify({
        inline_keyboard: [
          [
            { text: "📋 Commands", callback_data: "help:commands" },
            { text: "🤖 Models", callback_data: "model:listall" },
          ],
        ],
      }),
    });
    return true;
  }

  // ── /about ──
  if (ccSlash === "/about") {
    await sendMessage(chatId,
      "🤖 *Command Code Bot*\n\n" +
      "A production\\-grade Telegram bridge for [Command Code](https://commandcode\\.ai)\\.\n\n" +
      "*Stack:* Node\\.js \\(ESM\\), Telegram Bot API, `cmd` CLI\n" +
      "*Commands:* 49 registered, organized into 8 categories\n" +
      "*Deployment:* systemd user service with auto\\-restart\n" +
      "*Media:* Photos \\, documents \\, voice → Whisper transcription\n" +
      "*Process mgmt:* Orphan cleanup \\, graceful shutdown \\, per\\-user queue\n\n" +
      "Source: [github\\.com/qrak/telegram\\-commandcode](https://github.com/qrak/telegram-commandcode)"
    );
    return true;
  }

  // ── /help ──
  if (ccSlash === "/help") {
    const categories = [
      {
        title: "💬 *Session*",
        cmds: ["new", "clear", "undo", "fork", "rename", "resume", "context", "compact"],
      },
      {
        title: "🎯 *Guidance*",
        cmds: ["goal", "steer", "plan", "queue", "retry", "background", "stop"],
      },
      {
        title: "🤖 *Models*",
        cmds: ["model", "effort", "provider", "configuremodels"],
      },
      {
        title: "📦 *Knowledge*",
        cmds: ["taste", "memory", "skills", "mcp", "learntaste"],
      },
      {
        title: "🔧 *System*",
        cmds: ["status", "info", "whoami", "usage", "version", "reload", "update"],
      },
      {
        title: "🛠️ *Tools*",
        cmds: ["add_dir", "init", "review", "prcomments", "agents"],
      },
      {
        title: "🔐 *Auth*",
        cmds: ["login", "logout"],
      },
      {
        title: "📖 *Help*",
        cmds: ["help", "start"],
      },
    ];

    const lines = [];
    for (const cat of categories) {
      const cmdLines = cat.cmds
        .filter(cmd => BOT_COMMANDS.some(bc => bc.command === cmd))
        .map(cmd => {
          const bc = BOT_COMMANDS.find(b => b.command === cmd);
          const slash = TG_TO_CC[cmd] || "/" + cmd;
          return `  \`${slash}\` \\- ${escapeMd(bc.description)}`;
        });
      if (cmdLines.length > 0) {
        lines.push(`${cat.title}\n${cmdLines.join("\n")}`);
      }
    }

    // Commands not in any category
    const categorized = new Set(categories.flatMap(c => c.cmds));
    const miscCmds = BOT_COMMANDS
      .filter(bc => !categorized.has(bc.command))
      .map(bc => {
        const slash = TG_TO_CC[bc.command] || "/" + bc.command;
        return `  \`${slash}\` \\- ${escapeMd(bc.description)}`;
      });
    if (miscCmds.length > 0) {
      lines.push(`📁 *Other*\n${miscCmds.join("\n")}`);
    }

    await sendMessage(chatId, lines.join("\n\n") + `\n\n_Any other message → ` + "`cmd -p`" + ` prompt_`);
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

      const goalInfo = state.goal
        ? "Goal: " + escapeMd(state.goal.slice(0, 60)) + (state.goal.length > 60 ? "…" : "")
        : "No goal set";

      await sendMessage(
        chatId,
        `╔══ *Command Code* ══\n` +
        `╟ Model: \`${escapeMd(modelName)}\`${state.model ? "" : " \\(default\\)"}\n` +
        `╟ Binary: \`${escapeMd(CMD_BIN)}\` v${escapeMd(version || "?")}\n` +
        `╟ Auth: ${escapeMd(whoami || "not logged in")}\n` +
        `╟ ${escapeMd(sessionInfo)}\n` +
        `╟ Plan: ${planInfo} · YOLO: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on" : "off"} · Turns: ${Number(process.env.COMMAND_CODE_MAX_TURNS) || 20}\n` +
        `╟ ${goalInfo}\n` +
        `╟ ${steerInfo}\n` +
        `╚══ Use \`/model\` to switch, \`/goal\` to set objective, \`/steer\` to guide, \`/clear\` to reset`
      );
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /resume — resumes with actual session context from CC storage ──
  if (ccSlash === "/resume") {
    const state = getSession(chatId);
    state.active = true;
    await sendTyping(chatId);
    await sendMessage(chatId, "🔄 Resuming last headless session...");
    try {
      // Read the last conversation turn from Command Code's session storage
      const sessionCtx = readLatestSession();
      const prompt = sessionCtx
        ? `Continue the previous session. Here's the last exchange:\n\n${sessionCtx}\n\nSummarize what we were working on based on that context, and ask what I'd like to do next. If you can't determine the context, ask me to explain.`
        : "Continue where we left off. I don't have previous session context — ask me what we were working on.";
      const result = await runCommandCode(
        prompt,
        process.env.HOME,
        { continue: true, model: state.model, planMode: state.planMode, addDirs: state.addDirs }
      );
      await sendMessage(chatId, `📋 *Session resumed:*\n${escapeMd(result)}`);
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /clear and /new ──
  if (ccSlash === "/clear" || ccSlash === "/new") {
    resetSession(chatId);
    await sendMessage(chatId, "🧹 Session cleared. Model reset to default, plan mode off, goal/steer cleared. Next prompt starts fresh.");
    return true;
  }

  // ── /model ──
  if (ccSlash === "/model") {
    const state = getSession(chatId);
    // /model <name> → switch to that model
    if (args) {
      state.model = args;
      // Persist model selection so it survives bot restarts
      try {
        const cfg = readCCConfig();
        cfg.model = args;
        writeCCConfig(cfg);
      } catch {}
      await sendMessage(chatId, `✅ Switched to model: *${escapeMd(args)}*\n\nNext prompts will use \`-m ${escapeMd(args)}\`.`);
      return true;
    }

    // /model (no args) → show interactive model picker
    await sendTyping(chatId);
    try {
      const { stdout } = await runCLI(["--list-models"], 15_000);

      // Extract the default model
      let defaultModel = "unknown";
      const defaultMatch = stdout?.match(/^(\S+)\s+.+\(default\)/m);
      if (defaultMatch) defaultModel = defaultMatch[1];

      const currentLabel = state.model
        ? `*Currently:* ${escapeMd(state.model)}`
        : `*Default:* ${escapeMd(defaultModel)}`;

      // Build inline keyboard: popular models for quick-select
      const popularModels = [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "moonshotai/Kimi-K2.7-Code",
        "Qwen/Qwen3.7-Max",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gpt-5.5",
        "google/gemini-3.5-flash",
      ];

      const rows = [];
      for (let i = 0; i < popularModels.length; i += 2) {
        const row = [];
        const name = popularModels[i].split("/").pop();
        row.push({ text: name.length > 20 ? name.slice(0, 18) + "…" : name, callback_data: "model:" + popularModels[i] });
        if (i + 1 < popularModels.length) {
          const name2 = popularModels[i + 1].split("/").pop();
          row.push({ text: name2.length > 20 ? name2.slice(0, 18) + "…" : name2, callback_data: "model:" + popularModels[i + 1] });
        }
        rows.push(row);
      }
      rows.push([{ text: "📋 Show all models", callback_data: "model:listall" }]);

      await api("sendMessage", {
        chat_id: chatId,
        text: `🤖 *Pick a model*\n${currentLabel}\n\n_Tap a button or type \`/model <name>\`_`,
        parse_mode: "MarkdownV2",
        link_preview_options: { is_disabled: true },
        reply_markup: JSON.stringify({ inline_keyboard: rows }),
      });
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ Failed to fetch models: ${err.message}`));
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
    if (state.addDirs.length > 0) {
      for (const dir of state.addDirs) {
        cmdArgs.push("--add-dir", dir);
      }
    }

    const child = spawn(CMD_BIN, cmdArgs, {
      cwd: process.env.HOME,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 30 * 60 * 1000, // 30 min timeout for background tasks
    });
    backgroundProcesses.add(child);
    child.on("close", () => { backgroundProcesses.delete(child); });

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
      const state = getSession(chatId);
      const result = await runCommandCode(`Review pull request${prArg}. Check for bugs, security issues, test gaps, and style problems.`, process.env.HOME, { model: state.model, planMode: state.planMode, chatId, addDirs: state.addDirs });
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

  // ── /effort <level> or /effort (show current) — also /reasoning, /reason ──
  if (ccSlash === "/effort" || ccSlash === "/reasoning" || ccSlash === "/reason") {
    const validLevels = ["low", "medium", "high", "xhigh", "max"];
    const cfg = readCCConfig();
    const currentModel = cfg.model || "default";
    const currentEffort = cfg.reasoningEffort?.[currentModel];

    if (!args) {
      if (currentEffort) {
        await sendMessage(chatId, `🧠 Current effort for \`${escapeMd(currentModel)}\`: *${escapeMd(currentEffort)}*\n\nValid levels: ${validLevels.map(l => "`" + l + "`").join(", ")}\n\nUse \`/effort <level>\` to change it.`);
      } else {
        await sendMessage(chatId, `🧠 No effort set for \`${escapeMd(currentModel)}\` (uses model default).\n\nValid levels: ${validLevels.map(l => "`" + l + "`").join(", ")}\n\nUse \`/effort <level>\` to set it.`);
      }
      return true;
    }

    const level = args.toLowerCase();
    if (!validLevels.includes(level)) {
      await sendMessage(chatId, `❌ Invalid effort level. Valid: ${validLevels.map(l => "`" + l + "`").join(", ")}`);
      return true;
    }

    cfg.reasoningEffort = cfg.reasoningEffort || {};
    cfg.reasoningEffort[currentModel] = level;
    writeCCConfig(cfg);
    await sendMessage(chatId, `✅ Effort set to *${escapeMd(level)}* for \`${escapeMd(currentModel)}\`.\n\nNext prompts use reasoning effort: ${escapeMd(level)}.`);
    return true;
  }

  // ── /provider <name> or /provider (show current) ──
  if (ccSlash === "/provider") {
    const cfg = readCCConfig();
    const current = cfg.provider || "command-code";

    if (!args) {
      await sendMessage(chatId, `🔌 Current provider: *${escapeMd(current)}*\n\nUse \`/provider <name>\` to switch.\n_(Note: only locally installed providers are available.)_`);
      return true;
    }

    cfg.provider = args;
    writeCCConfig(cfg);
    await sendMessage(chatId, `✅ Provider switched to *${escapeMd(args)}*.\n\nNext sessions will use \`${escapeMd(args)}\` as the provider.`);
    return true;
  }

  // ── /add-dir <path> or /add-dir (list) ──
  if (ccSlash === "/add-dir") {
    const state = getSession(chatId);
    if (!args) {
      if (state.addDirs.length === 0) {
        await sendMessage(chatId, "📂 No directories added yet.\n\nUse `/add-dir <path>` to add a directory to the workspace context.\nUse `/add-dir clear` to remove all.");
      } else {
        const dirs = state.addDirs.map((d, i) => `  ${i + 1}\\. \`${escapeMd(d)}\``).join("\n");
        await sendMessage(chatId, `📂 *Added directories:*\n${dirs}\n\nUse \`/add-dir clear\` to remove all, or add more with \`/add-dir <path>\`.`);
      }
      return true;
    }
    if (args.toLowerCase() === "clear") {
      state.addDirs = [];
      await sendMessage(chatId, "📂 All added directories cleared.");
      return true;
    }
    state.addDirs.push(args);
    await sendMessage(chatId, `📂 Added directory: \`${escapeMd(args)}\`\nTotal: ${state.addDirs.length}\\. Use \`/add-dir clear\` to remove all.`);
    return true;
  }

  // ── /pr-comments (inject gh commands as a prompt) ──
  if (ccSlash === "/pr-comments") {
    const prArg = args ? ` #${args}` : "";
    await sendTyping(chatId);
    await sendMessage(chatId, escapeMd(`🔍 Fetching PR comments${prArg}...`));
    try {
      const state = getSession(chatId);
      const result = await runCommandCode(
        `Fetch and display all comments from the current GitHub pull request${prArg}. First run \`gh pr view --json number,headRepository,title\` to identify the PR, then fetch the comments and show them with author, timestamp, and content.`,
        process.env.HOME,
        { model: state.model, planMode: false, chatId, addDirs: state.addDirs }
      );
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /compact — in headless mode, there's no persistent session to compact ──
  if (ccSlash === "/compact") {
    await sendMessage(chatId, "ℹ️ In headless mode \\(`cmd \\-p`\\) there's no persistent conversation to compact — each prompt starts fresh\\.\n\nUse `/clear` to reset your session state \\(model, plan mode, steer\\), or just send a new prompt\\.");
    return true;
  }

  // ── /memory (show current AGENTS.md or manage via prompt) ──
  if (ccSlash === "/memory") {
    if (args) {
      // /memory <prompt> — pass as a prompt to manage memory
      const state = getSession(chatId);
      await sendTyping(chatId);
      await sendMessage(chatId, escapeMd(`🧠 Managing memory: ${args.slice(0, 100)}...`));
      try {
        const result = await runCommandCode(
          `Manage Command Code memory. ${args}\n\nRead AGENTS.md files if needed and make requested changes.`,
          process.env.HOME,
          { model: state.model, planMode: false, chatId, addDirs: state.addDirs }
        );
        await sendMessage(chatId, escapeMd(result));
      } catch (err) {
        await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
      }
    } else {
      // Show current memory files
      const paths = [
        "/etc/.commandcode/AGENTS.md",
        resolve(process.env.HOME, ".commandcode/AGENTS.md"),
        resolve(process.cwd(), "AGENTS.md"),
        resolve(process.cwd(), ".commandcode/AGENTS.md"),
      ];
      const found = paths.filter(p => existsSync(p));
      if (found.length > 0) {
        const listing = found.map(p => "  \\- `" + escapeMd(p) + "`").join("\n");
        await sendMessage(chatId, `🧠 *Memory files found:*\n${listing}\n\nUse \`/memory <instruction>\` to modify memory, e\\.g\\. \`/memory add a note about project conventions\`\\.\n\nThe interactive TUI \\(/memory with no args\\) is not available remotely\\.`);
      } else {
        await sendMessage(chatId, "🧠 No memory files found\\. Use `/memory <instruction>` to create one, e\\.g\\. `/memory set up project conventions for this repo`\\.");
      }
    }
    return true;
  }

  // ── /agents (show agent info, acknowledge TUI limitation) ──
  if (ccSlash === "/agents") {
    const agentsDir = resolve(process.env.HOME, ".commandcode/agents");
    const dirExists = existsSync(agentsDir);
    if (dirExists) {
      await sendMessage(chatId, `🤖 Agent configs stored at \`${escapeMd(agentsDir)}\`\\.\n\nInteractive agent management \\(TUI\\) is not available remotely\\. Describe what you want and I can help set it up via prompt\\.`);
    } else {
      await sendMessage(chatId, "🤖 No agent configurations found\\.\n\nInteractive agent management \\(TUI\\) is not available remotely\\. Describe what you want and I can help set it up via prompt\\.");
    }
    return true;
  }

  // ── /rewind (TUI-only — requires session state) ──
  if (ccSlash === "/rewind") {
    await sendMessage(chatId, "ℹ️ /rewind requires an active TUI session and cannot be used remotely\\.\n\nTo revert changes, describe what you need reverted and I can help\\. Or use `git checkout` commands directly\\.");
    return true;
  }

  // ── /init ──
  if (ccSlash === "/init") {
    const state = getSession(chatId);
    await sendTyping(chatId);
    await sendMessage(chatId, "📄 Initializing AGENTS.md...");
    try {
      const result = await runCommandCode("Create or update AGENTS.md for this project based on its structure, tech stack, and conventions.", process.env.HOME, { model: state.model, planMode: state.planMode, chatId, addDirs: state.addDirs });
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /goal — set a standing objective that's prepended to every prompt ──
  if (ccSlash === "/goal") {
    const state = getSession(chatId);
    if (!args) {
      if (state.goal) {
        await sendMessage(chatId, `🎯 *Current goal:*\n\n${escapeMd(state.goal)}\n\n_Use \`/goal clear\` to remove, \`/goal <text>\` to update._`);
      } else {
        await sendMessage(chatId, "🎯 *No goal set.*\n\nUse `/goal <text>` to set a standing objective the agent works towards across turns.\nUse `/goal clear` to remove it.\nUse `/goal status` to check it.");
      }
      return true;
    }
    if (args.toLowerCase() === "clear") {
      state.goal = null;
      await sendMessage(chatId, "🎯 Goal cleared.");
      return true;
    }
    if (args.toLowerCase() === "status") {
      await sendMessage(chatId, state.goal ? `🎯 *Goal:* ${escapeMd(state.goal)}` : "🎯 *No goal set.*");
      return true;
    }
    state.goal = args;
    await sendMessage(chatId, `🎯 Goal set:\n\n${escapeMd(args)}\n\n_This will be prepended to all subsequent prompts until cleared._`);
    return true;
  }

  // ── /queue — queue a prompt for the next turn without interrupting ──
  if (ccSlash === "/queue") {
    if (!args) {
      const state = getSession(chatId);
      if (state.queuedPrompts.length === 0) {
        await sendMessage(chatId, "📋 Queue is empty. Use `/queue <prompt>` to queue a prompt for the next turn.");
      } else {
        const items = state.queuedPrompts.map((p, i) => `  ${i + 1}\\. ${escapeMd(p.slice(0, 80))}`).join("\n");
        await sendMessage(chatId, `📋 *Queued prompts:*\n${items}`);
      }
      return true;
    }
    const state = getSession(chatId);
    state.queuedPrompts.push(args);
    await sendMessage(chatId, `📋 Queued prompt (${state.queuedPrompts.length} total). It will run after the current task completes.`);
    return true;
  }

  // ── /undo — back up N turns (in headless mode, re-run with context) ──
  if (ccSlash === "/undo") {
    const state = getSession(chatId);
    const n = parseInt(args) || 1;
    if (!state.lastPrompt) {
      await sendMessage(chatId, "🤷 No previous prompt to undo.");
      return true;
    }
    await sendMessage(chatId, `↩️ Undoing last ${n} turn(s). Re-running with adjusted context...`);
    // In headless mode, we can't truly rewind. Instead we start fresh and
    // re-run the last prompt with --continue to pick up prior context.
    state.active = false;
    try {
      const result = await runCommandCode(
        `Re-run this prompt, ignoring the previous response: ${state.lastPrompt}`,
        process.env.HOME,
        { model: state.model, planMode: state.planMode, chatId, addDirs: state.addDirs }
      );
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /fork — fork the conversation into a new session ──
  if (ccSlash === "/fork") {
    const state = getSession(chatId);
    const name = args || `fork_${Date.now()}`;
    await sendTyping(chatId);
    await sendMessage(chatId, `🌿 Forking session as "${escapeMd(name)}"...`);
    try {
      const result = await runCommandCode(
        "Continue this conversation in a new forked session. Summarize what we've done so far.",
        process.env.HOME,
        { continue: true, model: state.model, planMode: state.planMode, addDirs: state.addDirs }
      );
      state.sessionName = name;
      await sendMessage(chatId, `🌿 *Forked session:* ${escapeMd(name)}\n\n${escapeMd(result)}`);
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /rename — rename the current session ──
  if (ccSlash === "/rename") {
    const state = getSession(chatId);
    if (!args) {
      await sendMessage(chatId, state.sessionName ? `📝 Current session name: ${escapeMd(state.sessionName)}` : "📝 No session name set. Use `/rename <name>` to name this session.");
      return true;
    }
    state.sessionName = args;
    await sendMessage(chatId, `📝 Session renamed to: *${escapeMd(args)}*`);
    return true;
  }

  // ── /reload — restart the bot process ──
  if (ccSlash === "/reload") {
    await sendMessage(chatId, "🔄 Restarting bot... Session state will be preserved in config.");
    // Persist current session model before restart
    const state = getSession(chatId);
    if (state.model) {
      try {
        const cfg = readCCConfig();
        cfg.model = state.model;
        writeCCConfig(cfg);
      } catch {}
    }
    // Restart this process
    setTimeout(() => {
      process.exit(0);
    }, 1000);
    return true;
  }

  // ── /info — system information (cmd info) ──
  if (ccSlash === "/info") {
    await sendTyping(chatId);
    try {
      const { stdout, stderr, code } = await runCLI(["info"], 15_000);
      const output = stdout || stderr || `(exit ${code})`;
      const capped = output.length > 3800 ? output.slice(0, 3800) + "\n...(truncated)" : output;
      await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /version — show Command Code version ──
  if (ccSlash === "/version") {
    await sendTyping(chatId);
    try {
      const { stdout } = await runCLI(["--version"], 10_000);
      await sendMessage(chatId, `📦 *Command Code* v${escapeMd(stdout.trim())}`);
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /usage — show credits, plan, and usage metrics ──
  if (ccSlash === "/usage") {
    await sendTyping(chatId);
    try {
      const { stdout: whoami } = await runCLI(["whoami"], 10_000);
      const { stdout: version } = await runCLI(["--version"], 10_000);
      const state = getSession(chatId);
      let modelName = state.model || "default";
      if (!state.model) {
        const { stdout: models } = await runCLI(["--list-models"], 10_000);
        const m = models?.match(/^(\S+)\s+.+\(default\)/m);
        if (m) modelName = m[1];
      }
      await sendMessage(
        chatId,
        `╔══ *Usage & Credits* ══\n` +
        `╟ User: ${escapeMd(whoami || "not logged in")}\n` +
        `╟ Version: v${escapeMd(version?.trim() || "?")}\n` +
        `╟ Model: \`${escapeMd(modelName)}\`\n` +
        `╟ Max turns: ${Number(process.env.COMMAND_CODE_MAX_TURNS) || 20}\n` +
        `╟ YOLO: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on" : "off"}\n` +
        `╚══ _Detailed usage metrics require the TUI. Run \`cmd\` locally for full breakdown._`
      );
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /update — update Command Code to latest version ──
  if (ccSlash === "/update") {
    await sendTyping(chatId);
    await sendMessage(chatId, "⬆️ Updating Command Code...");
    try {
      const { stdout, stderr, code } = await runCLI(["update"], 120_000);
      const output = stdout || stderr || `(exit ${code})`;
      const capped = output.length > 3800 ? output.slice(0, 3800) + "\n...(truncated)" : output;
      await sendMessage(chatId, "```\n" + escapeMd(capped) + "\n```");
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /context — show context window usage ──
  if (ccSlash === "/context") {
    const state = getSession(chatId);
    await sendTyping(chatId);
    await sendMessage(chatId, "📊 Checking context window usage...");
    try {
      const result = await runCommandCode(
        "Show the current context window usage and breakdown. How many tokens are in context?",
        process.env.HOME,
        { continue: true, model: state.model, planMode: false, chatId, addDirs: state.addDirs }
      );
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /configure-models — choose which model runs each task ──
  if (ccSlash === "/configure-models") {
    const state = getSession(chatId);
    await sendTyping(chatId);
    await sendMessage(chatId, "⚙️ Configuring model assignments...");
    try {
      const result = await runCommandCode(
        "Show the current model configuration for each built-in task (main, summary, title, etc.) and help me configure which model runs each task.",
        process.env.HOME,
        { model: state.model, planMode: false, chatId, addDirs: state.addDirs }
      );
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /compact-mode — select a compact mode ──
  if (ccSlash === "/compact-mode") {
    const state = getSession(chatId);
    if (!args) {
      await sendMessage(chatId, "🗜️ *Compact modes*\n\nUsage: `/compact-mode <mode>`\n\nIn headless mode, compacting is handled per-session. Use `/clear` to start fresh, or send a prompt with compacting instructions.");
      return true;
    }
    await sendTyping(chatId);
    await sendMessage(chatId, `🗜️ Setting compact mode: ${escapeMd(args)}...`);
    try {
      const result = await runCommandCode(
        `Set the compact mode to: ${args}. Apply this compact mode configuration.`,
        process.env.HOME,
        { continue: true, model: state.model, planMode: false, chatId, addDirs: state.addDirs }
      );
      await sendMessage(chatId, escapeMd(result));
    } catch (err) {
      await sendMessage(chatId, escapeMd(`❌ ${err.message}`));
    }
    return true;
  }

  // ── /courses — open Command Code courses ──
  if (ccSlash === "/courses") {
    await sendMessage(chatId, "📚 *Command Code Courses*\n\n[Open courses in browser](https://commandcode.ai/courses)\n\n_Learn how to get the most out of Command Code._");
    return true;
  }

  // ── TUI-only commands (inform the user) ──
  // Most TUI commands now have Telegram-native implementations above.
  // These are genuinely interactive-only and can't work remotely.
  const TUI_ONLY = new Set([
    "/ide", "/terminal-setup",
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
let botInfoCache = null; // cached getMe result

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

    // Prepend goal and steer instructions if set
    const prefixParts = [];
    if (state.goal) prefixParts.push(`[GOAL] ${state.goal}`);
    if (state.steer) prefixParts.push(`[GUIDANCE] ${state.steer}`);
    const finalPrompt = prefixParts.length > 0 ? `${prefixParts.join("\n")}\n\n${prompt}` : prompt;

    const result = await runCommandCode(
      finalPrompt,
      process.env.HOME,
      {
        model: state.model,
        planMode: state.planMode,
        continue: state.active,
        chatId,
        addDirs: state.addDirs,
        // Stream output chunks as they arrive (edits status msg in place)
        onChunk: (chunk) => {
          const preview = escapeMd(chunk).slice(0, 3500);
          editMessage(chatId, statusMsgId, `⏳ *Running:* ${escapeMd(prompt.slice(0, 80))}...\n\n\`\`\`\n${preview}\n\`\`\``).catch(() => {});
        },
      }
    );
    state.active = true;
    if (state.oneShotPlan) { state.planMode = false; state.oneShotPlan = false; }

    // Build final response — use warning/error prefix if cmd failed
    const isError = result?.startsWith("⚠️") || result?.startsWith("❌");
    const prefix = isError ? "⚠️" : "✅";
    const escapedResult = result ? escapeMd(result) : "";
    const doneMsg = result
      ? `${prefix} *${isError ? "Failed" : "Done"}:* ${escapeMd(prompt.slice(0, 100))}\n\n${escapedResult}`
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

    // Process queued prompts if any (Hermes-style queue draining)
    if (state.queuedPrompts.length > 0) {
      const nextPrompt = state.queuedPrompts.shift();
      await sendMessage(chatId, `📋 *Running queued prompt:* ${escapeMd(nextPrompt.slice(0, 100))}`);
      const queueMsg = await sendMessage(chatId, `🚀 Running: \`${escapeMd(nextPrompt.slice(0, 200))}\``);
      const queueStatusId = queueMsg?.message_id || queueMsg?.result?.message_id;
      if (queueStatusId) {
        await processPrompt(chatId, userMsgId, queueStatusId, nextPrompt, state);
      }
    }

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
      allowed_updates: ["message", "callback_query"],
    });

    // Get bot info (cached) for mention/reply checks
    if (!botInfoCache) botInfoCache = await api("getMe", {});
    const botUsername = botInfoCache.username;
    const botId = botInfoCache.id;

    for (const update of updates) {
      lastUpdateId = update.update_id;

      // ── Handle callback queries (inline keyboard button presses) ──
      if (update.callback_query) {
        const cq = update.callback_query;
        const cqChatId = String(cq.message.chat.id);
        const cqMsgId = cq.message.message_id;
        const cqUserId = cq.from?.id;

        if (!isAllowed(cqUserId)) {
          await api("answerCallbackQuery", { callback_query_id: cq.id, text: "⛔ Not authorized", show_alert: true }).catch(() => {});
          continue;
        }

        if (cq.data.startsWith("model:")) {
          const modelId = cq.data.slice(6);

          // "listall" — show the full model list as text
          if (modelId === "listall") {
            await api("answerCallbackQuery", { callback_query_id: cq.id }).catch(() => {});
            const { stdout } = await runCLI(["--list-models"], 15_000);
            const models = stdout || "No models found";
            const preview = models.length > 3800 ? models.slice(0, 3800) + "\n...(truncated)" : models;
            await editMessage(cqChatId, cqMsgId, "🤖 *All available models*\n\n```\n" + escapeMd(preview) + "\n```\n_Use `/model <name>` to switch_");
            continue;
          }

          const state = getSession(cqChatId);
          state.model = modelId;
          try {
            const cfg = readCCConfig();
            cfg.model = modelId;
            writeCCConfig(cfg);
          } catch {}
          await api("answerCallbackQuery", { callback_query_id: cq.id, text: `✅ Switched to ${modelId.split("/").pop()}`, show_alert: false }).catch(() => {});
          await editMessage(cqChatId, cqMsgId, `✅ Switched to model: *${escapeMd(modelId)}*`);
        }

        // "help:commands" — show categorized command list
        if (cq.data === "help:commands") {
          await api("answerCallbackQuery", { callback_query_id: cq.id }).catch(() => {});
          // Simulate /help by re-rendering the command that handles /help
          const msg = cq.message;
          // We send a reply with the help text via sendMessage since editMessage
          // would likely be too long — use sendMessage with text reply
          const helpText = await (async () => {
            const categories = [
              { title: "💬 *Session*", cmds: ["new", "clear", "undo", "fork", "rename", "resume", "context", "compact"] },
              { title: "🎯 *Guidance*", cmds: ["goal", "steer", "plan", "queue", "retry", "background", "stop"] },
              { title: "🤖 *Models*", cmds: ["model", "effort", "provider", "configuremodels"] },
              { title: "📦 *Knowledge*", cmds: ["taste", "memory", "skills", "mcp", "learntaste"] },
              { title: "🔧 *System*", cmds: ["status", "info", "whoami", "usage", "version", "reload", "update"] },
              { title: "🛠️ *Tools*", cmds: ["add_dir", "init", "review", "prcomments", "agents"] },
              { title: "🔐 *Auth*", cmds: ["login", "logout"] },
            ];
            return categories.map(cat => {
              const cmdLines = cat.cmds
                .filter(cmd => BOT_COMMANDS.some(bc => bc.command === cmd))
                .map(cmd => {
                  const bc = BOT_COMMANDS.find(b => b.command === cmd);
                  const slash = TG_TO_CC[cmd] || "/" + cmd;
                  return `  \`${slash}\` \\- ${escapeMd(bc.description)}`;
                });
              return `${cat.title}\n${cmdLines.join("\n")}`;
            }).join("\n\n");
          })();
          await sendMessage(cqChatId, helpText);
        }
        continue;
      }

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

      // ── Rate limiting ──
      const now = Date.now();
      const lastMsg = rateLimits.get(userId);
      if (lastMsg && (now - lastMsg) < RATE_LIMIT_WINDOW) {
        console.log(`⏱️ Rate-limited user ${userId} (${username})`);
        setReaction(chatId, msg.message_id, "⏱️").catch(() => {});
        continue;
      }
      rateLimits.set(userId, now);

      // ── Prompt length validation ──
      if (text && text.length > MAX_PROMPT_LENGTH) {
        await sendMessage(chatId, escapeMd(`⚠️ Prompt too long (${text.length} chars). Maximum allowed: ${MAX_PROMPT_LENGTH} chars.`));
        continue;
      }

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
        const handled = await handleCommand(chatId, text, { userId, username, chatType });
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
// Orphan cleanup — kill stale cmd processes from crashed bot instances
// ---------------------------------------------------------------------------

/**
 * Kill any orphaned `cmd -p` processes that aren't tracked by this bot.
 * Called on startup to clean up processes left by a crashed bot instance.
 */
function killOrphanedCmdProcesses() {
  try {
    const result = execSync("ps axo pid,args --no-headers | grep -E 'cmd.* -p ' || true", {
      encoding: "utf8",
      timeout: 5000,
    });
    if (!result) return;

    const lines = result.trim().split("\n").filter(Boolean);
    let killed = 0;
    for (const line of lines) {
      const parts = line.trim().split(/\s+/);
      const pid = parseInt(parts[0], 10);
      if (!pid || parts.length < 2) continue;
      // Skip the grep command itself and our own process
      if (line.includes("grep") || line.includes("sh -c")) continue;
      try {
        process.kill(pid, "SIGTERM");
        // Force kill after 2s if it didn't stop
        setTimeout(() => {
          try { process.kill(pid, "SIGKILL"); } catch {}
        }, 2000);
        killed++;
      } catch {}
    }
    if (killed > 0) console.log(`   Killed ${killed} orphaned cmd process(es)`);
  } catch {
    // ps/grep not available — skip orphan cleanup
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

    // Kill all tracked running processes so they don't become orphans
    for (const [chatId] of runningProcesses) {
      killRunningProcess(chatId);
    }
    // Also kill detached background processes
    for (const child of backgroundProcesses) {
      if (!child.killed) {
        try {
          child.kill("SIGINT");
          setTimeout(() => { if (!child.killed) child.kill("SIGKILL"); }, 3000);
        } catch {}
      }
    }

    setTimeout(() => process.exit(0), 500);
  };

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  setupShutdown();
  killOrphanedCmdProcesses();

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
