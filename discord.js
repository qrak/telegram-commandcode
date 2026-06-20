#!/usr/bin/env node
/**
 * telegram-commandcode Discord bot
 * 
 * Bridges Discord ↔ Command Code CLI — same Hermes-style architecture as bot.js.
 * 
 * Usage:
 *   DISCORD_BOT_TOKEN=*** node discord.js
 * 
 *   Options (env vars):
 *     DISCORD_ALLOWED_USERS  — comma-separated user IDs (or "any")
 *     COMMAND_CODE_CMD       — path to the `cmd` binary (default: "cmd")
 */

import { spawn } from "node:child_process";
import { readFileSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { Client, GatewayIntentBits, Events, REST, Routes } from "discord.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

// ---------------------------------------------------------------------------
// Load .env if DISCORD_BOT_TOKEN not already set
// ---------------------------------------------------------------------------

function loadEnv() {
  if (process.env.DISCORD_BOT_TOKEN) return;
  const envPaths = [
    resolve(process.cwd(), ".env"),
    resolve(__dirname, ".env"),
  ];
  for (const p of envPaths) {
    if (existsSync(p)) {
      const lines = readFileSync(p, "utf8").split(/\r?\n/);
      for (const line of lines) {
        const m = line.match(/^DISCORD_BOT_TOKEN=(.*)/);
        if (m) {
          process.env.DISCORD_BOT_TOKEN = m[1].trim().replace(/^["']|["']$/g, "");
        }
      }
    }
  }
}
loadEnv();

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error("❌ DISCORD_BOT_TOKEN is required. Create a bot at https://discord.com/developers/applications");
  process.exit(1);
}

const CMD_BIN = process.env.COMMAND_CODE_CMD || "cmd";

const ALLOWED = (process.env.DISCORD_ALLOWED_USERS || "any")
  .split(",")
  .map((s) => s.trim());

// ---------------------------------------------------------------------------
// Per-user session state
// ---------------------------------------------------------------------------

const sessions = new Map();

function getSession(userId) {
  if (!sessions.has(userId)) {
    sessions.set(userId, { active: false, model: null, planMode: false, oneShotPlan: false, steer: null, lastPrompt: null });
  }
  return sessions.get(userId);
}

function resetSession(userId) {
  sessions.delete(userId);
}

// ── Per-user message queue ──
const messageQueues = new Map();
const processing = new Set();

function enqueueMessage(userId, channel, prompt) {
  if (!messageQueues.has(userId)) {
    messageQueues.set(userId, []);
  }
  const hasRunning = getRunningProcess(userId) !== null;
  const finalPrompt = hasRunning ? `⚡ Previous execution interrupted.\n\n${prompt}` : prompt;
  messageQueues.get(userId).push({ userId, channel, prompt: finalPrompt });
  processQueue(userId);
}

async function processQueue(userId) {
  if (processing.has(userId)) return;
  processing.add(userId);
  try {
    while (messageQueues.get(userId)?.length > 0) {
      const { channel, prompt } = messageQueues.get(userId).shift();
      const state = getSession(userId);
      await processPrompt(userId, channel, prompt, state);
    }
  } catch (err) {
    console.error(`[queue] Error for ${userId}:`, err.message);
  } finally {
    processing.delete(userId);
  }
}

// ── Process tracking ──
const runningProcesses = new Map();

function getRunningProcess(userId) {
  return runningProcesses.get(userId) || null;
}

function setRunningProcess(userId, child) {
  runningProcesses.set(userId, child);
}

function killRunningProcess(userId) {
  const child = runningProcesses.get(userId);
  if (child && !child.killed) {
    child.kill("SIGINT");
    setTimeout(() => { if (!child.killed) child.kill("SIGKILL"); }, 3000);
  }
  runningProcesses.delete(userId);
}

// ---------------------------------------------------------------------------
// Command Code runner
// ---------------------------------------------------------------------------

async function runCommandCode(prompt, cwd = process.env.HOME, sessionOpts = {}) {
  const args = ["-p", prompt];
  if (process.env.COMMAND_CODE_YOLO !== "false") args.push("--yolo");
  const maxTurns = Number(process.env.COMMAND_CODE_MAX_TURNS) || 20;
  args.push("--max-turns", String(maxTurns));
  if (sessionOpts.model) args.push("-m", sessionOpts.model);
  if (sessionOpts.planMode) args.push("--plan");
  if (sessionOpts.continue) args.push("--continue");
  args.push("--skip-onboarding");

  if (sessionOpts.userId) killRunningProcess(sessionOpts.userId);

  return new Promise((resolve, reject) => {
    const child = spawn(CMD_BIN, args, {
      cwd,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 10 * 60 * 1000,
    });

    if (sessionOpts.userId) setRunningProcess(sessionOpts.userId, child);

    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => { stdout += d.toString(); });
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      if (sessionOpts.userId) runningProcesses.delete(sessionOpts.userId);
      if (code === 0 || code === null) resolve(stdout.trim() || "(no output)");
      else {
        const reason = ({ 3: "not authenticated", 4: "permission denied" })[code] || `exit ${code}`;
        const errText = stderr.trim();
        resolve(errText ? `⚠️ ${CMD_BIN} ${reason}:\n${errText.slice(0, 1000)}` : `⚠️ ${CMD_BIN} ${reason}`);
      }
    });
    child.on("error", (err) => reject(new Error(`Failed to spawn ${CMD_BIN}: ${err.message}`)));
  });
}

// ── File path detection + MEDIA: prefix ──
const FILE_PATH_RE = /(\/(?:home|tmp|var|usr|etc|opt|mnt|media|run|srv)[^\s"'\])\]]{3,})/g;
const MEDIA_RE = /MEDIA:(\/[^\s"'\])\]]{3,})/g;

function findFilePaths(text) {
  const matches = new Set();
  let m;
  while ((m = MEDIA_RE.exec(text)) !== null) {
    const p = m[1].replace(/[.,;:!?)]$/, "");
    if (existsSync(p)) {
      matches.add(JSON.stringify({ path: p, type: /\.(png|jpg|jpeg|gif|webp|bmp)$/i.test(p) ? "photo" : "file" }));
    }
  }
  FILE_PATH_RE.lastIndex = 0;
  while ((m = FILE_PATH_RE.exec(text)) !== null) {
    const p = m[1].replace(/[.,;:!?)]$/, "");
    if (existsSync(p)) {
      matches.add(JSON.stringify({ path: p, type: /\.(png|jpg|jpeg|gif|webp|bmp)$/i.test(p) ? "photo" : "file" }));
    }
  }
  return [...matches].map((s) => JSON.parse(s));
}

// ── CLI runner for subcommands ──
async function runCLI(args, timeout = 15_000) {
  return new Promise((resolve) => {
    const child = spawn(CMD_BIN, args, {
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
      timeout,
    });
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => { stdout += d.toString(); });
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => resolve({ code, stdout: stdout.trim(), stderr: stderr.trim() }));
    child.on("error", (err) => resolve({ code: -1, stdout: "", stderr: err.message }));
  });
}

// ---------------------------------------------------------------------------
// Process a prompt (Hermes-style reaction + edit cycle for Discord)
// ---------------------------------------------------------------------------

async function processPrompt(userId, channel, prompt, state) {
  state.lastPrompt = prompt;

  // Send initial status message
  const msg = await channel.send(`🤔 Processing: ${prompt.slice(0, 100)}...`);

  try {
    const result = await runCommandCode(prompt, process.env.HOME, {
      model: state.model, planMode: state.planMode, continue: state.active, userId,
    });

    state.active = true;
    if (state.oneShotPlan) { state.planMode = false; state.oneShotPlan = false; }

    // Send final result
    const truncated = result ? result.slice(0, 1900) : "";
    const doneMsg = `✅ Done:\n\`\`\`\n${truncated}\n\`\`\``;
    await msg.edit(doneMsg).catch(() => channel.send(doneMsg));

    // Auto-send files
    if (result) {
      const paths = findFilePaths(result);
      for (const { path, type } of paths.slice(0, 5)) {
        try {
          if (type === "photo") await channel.send({ files: [path] });
          else await channel.send({ files: [path] });
        } catch { /* best-effort */ }
      }
    }
    return result;
  } catch (err) {
    await msg.edit(`❌ Error: ${err.message}`).catch(() => channel.send(`❌ Error: ${err.message}`));
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Discord bot
// ---------------------------------------------------------------------------

async function main() {
  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.DirectMessages,
      GatewayIntentBits.GuildMembers,
    ],
  });

  client.once(Events.ClientReady, async () => {
    console.log(`✅ Discord bot logged in as ${client.user.tag}`);

    // Register slash commands
    const commands = [
      { name: "status", description: "Show Command Code status (model, auth, session info)" },
      { name: "model", description: "List models or switch: /model <name>" },
      { name: "steer", description: "Set mid-session guidance: /steer <instruction>" },
      { name: "clear", description: "Clear conversation history (fresh start)" },
      { name: "resume", description: "Resume last session" },
      { name: "stop", description: "Stop the running agent" },
      { name: "retry", description: "Re-run the last prompt" },
      { name: "whoami", description: "Show your user info" },
      { name: "plan", description: "Toggle plan mode or set a task" },
      { name: "help", description: "Show available commands" },
    ];

    try {
      const rest = new REST({ version: "10" }).setToken(BOT_TOKEN);
      await rest.put(Routes.applicationCommands(client.user.id), { body: commands });
      console.log(`   ${commands.length} slash commands registered`);
    } catch (err) {
      console.error("   ⚠️ Failed to register commands:", err.message);
    }

    console.log("   Listening...");
  });

  client.on(Events.MessageCreate, async (msg) => {
    // Ignore bot messages
    if (msg.author.bot) return;

    const userId = msg.author.id;
    const username = msg.author.username;
    const channel = msg.channel;
    const content = msg.content.trim();

    // Access control
    if (!ALLOWED.includes("any") && !ALLOWED.includes(userId)) {
      console.log(`⛔ Blocked message from ${username} (${userId})`);
      return;
    }

    console.log(`📩 [${username}] ${content.slice(0, 80)}`);

    // Handle slash commands
    if (content.startsWith("/")) {
      const [cmd, ...argsArr] = content.slice(1).split(/\s+/);
      const args = argsArr.join(" ");
      const state = getSession(userId);

      switch (cmd) {
        case "help": {
          const cmds = [
            "/status", "/model", "/steer", "/stop", "/retry", "/whoami",
            "/plan", "/clear", "/resume", "/background", "/help",
          ];
          await channel.send(`**Commands:**\n${cmds.map((c) => `  ${c}`).join("\n")}\n\nAny other message → \`cmd -p\` prompt`);
          return;
        }

        case "status": {
          const { stdout: whoami } = await runCLI(["whoami"]);
          const { stdout: version } = await runCLI(["--version"]);
          let modelName = state.model;
          if (!modelName) {
            const { stdout: models } = await runCLI(["--list-models"], 10_000);
            const m = models?.match(/^(\S+)\s+.+\(default\)/m);
            modelName = m ? m[1] : "unknown";
          }
          await channel.send(
            `**Command Code Status**\n` +
            `Model: \`${modelName}\`${state.model ? "" : " (default)"}\n` +
            `Version: \`${version || "?"}\`\n` +
            `Auth: ${whoami || "not logged in"}\n` +
            `Session: ${state.active ? "active" : "none"}\n` +
            `Plan: ${state.planMode ? "ON" : "off"} · YOLO: ${process.env.COMMAND_CODE_YOLO !== "false" ? "on" : "off"}\n` +
            `Steer: ${state.steer ? state.steer.slice(0, 60) : "none"}`
          );
          return;
        }

        case "model": {
          if (args) {
            state.model = args;
            await channel.send(`✅ Switched to model: \`${args}\``);
          } else {
            const { stdout: models } = await runCLI(["--list-models"], 15_000);
            const preview = (models || "No models").slice(0, 1900);
            const current = state.model ? `Currently: \`${state.model}\`` : "Using default model";
            await channel.send(`**Available models**\n\`\`\`\n${preview}\n\`\`\`\n${current}`);
          }
          return;
        }

        case "steer": {
          if (!args) {
            if (state.steer) await channel.send(`**Current steer:**\n${state.steer}\n\nUse \`/steer clear\` to remove.`);
            else await channel.send("No steer set. Use `/steer <instruction>` to guide the agent.");
            return;
          }
          if (args === "clear") { state.steer = null; await channel.send("Steer cleared."); return; }
          state.steer = args;
          await channel.send(`🧭 Steer set.\n\n${args}\n\nApplied to all subsequent prompts. Use \`/steer clear\` to remove.`);
          return;
        }

        case "stop": {
          if (getRunningProcess(userId)) {
            killRunningProcess(userId);
            await channel.send("🛑 Execution stopped by user.");
          } else {
            await channel.send("No active execution to stop.");
          }
          return;
        }

        case "retry": {
          if (!state.lastPrompt) { await channel.send("No previous prompt to retry."); return; }
          enqueueMessage(userId, channel, state.lastPrompt);
          return;
        }

        case "whoami": {
          await channel.send(
            `**User info**\nID: \`${userId}\`\nUsername: @${username}\nAccess: ${ALLOWED.includes("any") ? "unrestricted" : "restricted"}\nPlatform: Discord`
          );
          return;
        }

        case "plan": {
          if (args) { state.planMode = true; state.oneShotPlan = true; }
          else { state.planMode = !state.planMode; state.oneShotPlan = false; }
          await channel.send(`Plan mode: ${state.planMode ? "ON" : "OFF"}`);
          return;
        }

        case "clear": { resetSession(userId); await channel.send("Session cleared."); return; }

        case "resume": {
          state.active = true;
          enqueueMessage(userId, channel, "Continue where we left off. Summarize context and ask what I'd like to do next.");
          return;
        }

        case "background": {
          if (!args) { await channel.send("Usage: `/background <prompt>`"); return; }
          const bgId = `bg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
          await channel.send(`🔄 Background task started: "${args.slice(0, 100)}"\nTask ID: \`${bgId}\``);
          const cmdArgs = ["-p", args, "--skip-onboarding"];
          if (process.env.COMMAND_CODE_YOLO !== "false") cmdArgs.push("--yolo");
          cmdArgs.push("--max-turns", String(Number(process.env.COMMAND_CODE_MAX_TURNS) || 20));
          if (state.model) cmdArgs.push("-m", state.model);
          if (state.planMode) cmdArgs.push("--plan");
          const child = spawn(CMD_BIN, cmdArgs, {
            cwd: process.env.HOME, env: { ...process.env },
            stdio: ["pipe", "pipe", "pipe"], timeout: 30 * 60 * 1000,
          });
          let stdout = "", stderr = "";
          child.stdout.on("data", (d) => { stdout += d.toString(); });
          child.stderr.on("data", (d) => { stderr += d.toString(); });
          child.on("close", (code) => {
            const output = stdout.trim() || stderr.trim() || `(exit ${code})`;
            const status = code === 0 ? "✅" : "⚠️";
            channel.send(`${status} Background task complete (${bgId})\n\`\`\`\n${output.slice(0, 1900)}\n\`\`\``).catch(() => {});
          });
          child.on("error", (err) => channel.send(`❌ Background task failed (${bgId}): ${err.message}`).catch(() => {}));
          return;
        }

        default:
          // Unknown slash command → treat as prompt
          break;
      }
    }

    // ── Regular prompt → forward to Command Code ──
    const state = getSession(userId);
    // Prepend steer if set
    const finalPrompt = state.steer ? `${state.steer}\n\n${content}` : content;
    enqueueMessage(userId, channel, finalPrompt);
  });

  await client.login(BOT_TOKEN);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
