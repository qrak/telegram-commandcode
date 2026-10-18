#!/usr/bin/env node
/**
 * telegram-commandcode — MCP server for Telegram
 *
 * Exposes Telegram messaging tools to any MCP-capable coding agent
 * (Command Code, Claude Code, Cursor, Codex CLI, …) over stdio transport.
 *
 * Setup:
 *   1. Create a bot at @BotFather → get TELEGRAM_BOT_TOKEN
 *   2. Register the server with your agent, e.g.:
 *        cmd mcp add telegram -e TELEGRAM_BOT_TOKEN=<token> -- npx -y github:qrak/telegram-commandcode
 *   3. Optionally set TELEGRAM_DEFAULT_CHAT_ID so tools work without chat_id
 *
 * Docs: https://github.com/qrak/telegram-commandcode
 */

import { readFileSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

// ---------------------------------------------------------------------------
// Package metadata
// ---------------------------------------------------------------------------

const __dirname = fileURLToPath(new URL(".", import.meta.url));

function packageVersion() {
  try {
    const pkg = JSON.parse(readFileSync(join(__dirname, "package.json"), "utf8"));
    return pkg.version || "0.0.0";
  } catch {
    return "0.0.0";
  }
}

// ---------------------------------------------------------------------------
// Resolve bot token from env or local .env file
// ---------------------------------------------------------------------------

function loadEnv() {
  if (process.env.TELEGRAM_BOT_TOKEN) return;

  const envPaths = [resolve(process.cwd(), ".env"), resolve(__dirname, ".env")];
  for (const p of envPaths) {
    if (!existsSync(p)) continue;
    try {
      for (const line of readFileSync(p, "utf8").split(/\r?\n/)) {
        const m = line.match(/^\s*(TELEGRAM_BOT_TOKEN|TELEGRAM_DEFAULT_CHAT_ID)\s*=\s*(.*)\s*$/);
        if (!m) continue;
        const value = m[2].trim().replace(/^["']|["']$/g, "");
        if (value && !process.env[m[1]]) process.env[m[1]] = value;
      }
    } catch {
      // A malformed .env must never crash the server.
    }
  }
}
loadEnv();

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const DEFAULT_CHAT_ID = process.env.TELEGRAM_DEFAULT_CHAT_ID || "";
const MAX_TEXT = 4096; // Telegram limit for a single message

// Track the last update_id we acknowledged in this process.
let _lastUpdateId = 0;

// Redact the bot token from anything we surface to the agent or logs.
function redact(value) {
  const text = String(value ?? "");
  return BOT_TOKEN ? text.split(BOT_TOKEN).join("<redacted-token>") : text;
}

// ---------------------------------------------------------------------------
// Telegram API helpers
// ---------------------------------------------------------------------------

class TelegramError extends Error {
  constructor(message, code) {
    super(redact(message));
    this.name = "TelegramError";
    this.code = code;
  }
}

async function fetchWithRetry(url, init, maxRetries = 3) {
  let lastError;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      return await fetch(url, init);
    } catch (err) {
      lastError = err;
      if (attempt === maxRetries - 1) break;
      await new Promise((r) => setTimeout(r, Math.min(500 * 2 ** attempt, 4000)));
    }
  }
  throw new TelegramError(
    `Network error talking to Telegram: ${lastError?.message || "unknown"}`,
    0,
  );
}

/**
 * Call a Telegram Bot API method.
 *
 * Automatically retries 429/5xx, and retries once without `parse_mode` when
 * Telegram rejects the entities (the #1 cause of "message not sent" reports).
 */
async function api(method, body) {
  if (!BOT_TOKEN) {
    throw new TelegramError("TELEGRAM_BOT_TOKEN is not configured.", 401);
  }

  const isFormData = body instanceof FormData;
  const payload = isFormData ? body : JSON.stringify(body);

  for (let attempt = 1; attempt <= 4; attempt++) {
    const res = await fetchWithRetry(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: isFormData ? {} : { "Content-Type": "application/json" },
      body: payload,
    });

    let data;
    try {
      data = await res.json();
    } catch {
      throw new TelegramError(
        `Telegram returned a non-JSON response (HTTP ${res.status}).`,
        res.status,
      );
    }

    if (data.ok) return data.result;

    const description = data.description || `HTTP ${res.status}`;
    const parseFailure = /can't parse entities|parse entities/i.test(description);

    // MarkdownV2 escaping mistakes are recoverable: resend as plain text.
    if (parseFailure && !isFormData && body?.parse_mode) {
      const plain = { ...body };
      delete plain.parse_mode;
      return api(method, plain);
    }

    if ((res.status === 429 || res.status >= 500) && attempt < 4) {
      const retryAfter = Number(data.parameters?.retry_after || 0);
      const delay = retryAfter
        ? retryAfter * 1000
        : Math.min(1000 * 2 ** attempt + Math.random() * 500, 10000);
      await new Promise((r) => setTimeout(r, delay));
      continue;
    }

    throw new TelegramError(
      `Telegram API error: ${description} (code ${data.error_code ?? res.status})`,
      data.error_code ?? res.status,
    );
  }
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const CHAT_ID_PROP = {
  type: "string",
  description:
    "Target chat ID (user, group, or channel). Omit to use TELEGRAM_DEFAULT_CHAT_ID.",
};

const TOOLS = [
  {
    name: "telegram_send_message",
    description:
      "Send a text message to a Telegram chat. Supports MarkdownV2 and HTML parse modes. " +
      "Messages longer than 4096 characters are rejected by Telegram — split them first.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: CHAT_ID_PROP,
        text: { type: "string", description: "Message text to send." },
        parse_mode: {
          type: "string",
          enum: ["MarkdownV2", "HTML", "Markdown"],
          description:
            "Formatting mode. MarkdownV2 requires escaping _ * [ ] ( ) ~ ` > # + - = | { } . !",
        },
      },
      required: ["text"],
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  {
    name: "telegram_send_photo",
    description:
      "Send a photo via a public URL or an absolute local file path (max 10 MB for photos).",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: CHAT_ID_PROP,
        photo: {
          type: "string",
          description: "Public https:// URL or absolute path to a local image file.",
        },
        caption: { type: "string", description: "Optional caption (max 1024 chars)." },
        parse_mode: {
          type: "string",
          enum: ["MarkdownV2", "HTML"],
          description: "Caption parse mode.",
        },
      },
      required: ["photo"],
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  {
    name: "telegram_send_file",
    description:
      "Send a document/file via an absolute local path or public URL (max 50 MB). " +
      "Useful for build logs, diffs, screenshots and reports.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: CHAT_ID_PROP,
        file_path: {
          type: "string",
          description: "Absolute path to the file (e.g. /tmp/report.pdf) or a public URL.",
        },
        caption: { type: "string", description: "Optional caption (max 1024 chars)." },
        parse_mode: {
          type: "string",
          enum: ["MarkdownV2", "HTML"],
          description: "Caption parse mode.",
        },
      },
      required: ["file_path"],
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  {
    name: "telegram_get_updates",
    description:
      "Fetch recent incoming messages. By default only unseen updates are returned and " +
      "acknowledged; pass offset: 0 to re-read the backlog.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "number", description: "Max updates to return (default 5, max 20)." },
        offset: {
          type: "number",
          description: "Update offset. Omit to continue from the last seen update.",
        },
        timeout: {
          type: "number",
          description: "Long-polling timeout in seconds (default 0, max 50).",
        },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "telegram_health",
    description:
      "Check that the bot token works: returns bot id, username and capabilities.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "telegram_send_reaction",
    description:
      "Set an emoji reaction on a message (👀 working, ✅ done, ❌ failed). " +
      "The bot must be a member of the chat.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: CHAT_ID_PROP,
        message_id: { type: "number", description: "ID of the message to react to." },
        emoji: { type: "string", description: "One of the allowed Telegram reaction emoji." },
      },
      required: ["message_id", "emoji"],
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
  {
    name: "telegram_download_file",
    description:
      "Download a file from Telegram by file_id (max 20 MB via the Bot API) and return its local path.",
    inputSchema: {
      type: "object",
      properties: {
        file_id: { type: "string", description: "file_id from an incoming message." },
      },
      required: ["file_id"],
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  },
];

if (!BOT_TOKEN) {
  // Fail fast and loudly: an agent that sees a working-looking tool list will
  // keep trying to send messages that can never be delivered.
  TOOLS.length = 0;
  TOOLS.push({
    name: "telegram_status",
    description:
      "Telegram is NOT configured. Set TELEGRAM_BOT_TOKEN (from @BotFather) in the MCP " +
      "server environment, then restart the agent. See README → Setup.",
    inputSchema: { type: "object", properties: {} },
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function requireChatId(args) {
  const chatId = args.chat_id || DEFAULT_CHAT_ID;
  if (!chatId) {
    throw new Error(
      "No chat_id: pass one explicitly or set TELEGRAM_DEFAULT_CHAT_ID.",
    );
  }
  return chatId;
}

function localFile(filePath, label) {
  const absolute = resolve(filePath);
  if (!existsSync(absolute)) throw new Error(`${label} not found: ${absolute}`);
  return absolute;
}

function isUrl(value) {
  return /^https?:\/\//i.test(value);
}

// Where downloaded Telegram files are written.
const UPLOAD_DIR = join(tmpdir(), "telegram-commandcode");

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "telegram-commandcode", version: packageVersion() },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args = {} } = req.params;

  if (!BOT_TOKEN) {
    return {
      content: [
        {
          type: "text",
          text:
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and add the " +
            "token to this MCP server's environment (see README → Setup).",
        },
      ],
      isError: true,
    };
  }

  try {
    switch (name) {
      case "telegram_send_message": {
        const chatId = requireChatId(args);
        if (typeof args.text !== "string" || !args.text.trim()) {
          throw new Error("text must be a non-empty string.");
        }
        if (args.text.length > MAX_TEXT) {
          throw new Error(
            `Message is ${args.text.length} chars; Telegram allows ${MAX_TEXT}. Split it into chunks.`,
          );
        }
        const msg = await api("sendMessage", {
          chat_id: chatId,
          text: args.text,
          ...(args.parse_mode ? { parse_mode: args.parse_mode } : {}),
          link_preview_options: { is_disabled: true },
        });
        return textResult(`Message sent to chat ${msg.chat.id} (message_id ${msg.message_id}).`);
      }

      case "telegram_send_photo": {
        const chatId = requireChatId(args);
        if (typeof args.photo !== "string" || !args.photo) {
          throw new Error("photo must be a URL or an absolute file path.");
        }

        if (isUrl(args.photo)) {
          const msg = await api("sendPhoto", {
            chat_id: chatId,
            photo: args.photo,
            ...(args.caption ? { caption: args.caption } : {}),
            ...(args.parse_mode ? { parse_mode: args.parse_mode } : {}),
          });
          return textResult(`Photo sent (message_id ${msg.message_id}).`);
        }

        const filePath = localFile(args.photo, "Photo");
        const form = new FormData();
        form.set("chat_id", chatId);
        form.set("photo", new Blob([readFileSync(filePath)]), basename(filePath));
        if (args.caption) form.set("caption", args.caption);
        if (args.parse_mode) form.set("parse_mode", args.parse_mode);

        const msg = await api("sendPhoto", form);
        return textResult(`Photo sent: ${basename(filePath)} (message_id ${msg.message_id}).`);
      }

      case "telegram_send_file": {
        const chatId = requireChatId(args);
        if (typeof args.file_path !== "string" || !args.file_path) {
          throw new Error("file_path must be a URL or an absolute file path.");
        }

        if (isUrl(args.file_path)) {
          const msg = await api("sendDocument", {
            chat_id: chatId,
            document: args.file_path,
            ...(args.caption ? { caption: args.caption } : {}),
            ...(args.parse_mode ? { parse_mode: args.parse_mode } : {}),
          });
          return textResult(`Document sent (message_id ${msg.message_id}).`);
        }

        const filePath = localFile(args.file_path, "File");
        const form = new FormData();
        form.set("chat_id", chatId);
        form.set("document", new Blob([readFileSync(filePath)]), basename(filePath));
        if (args.caption) form.set("caption", args.caption);
        if (args.parse_mode) form.set("parse_mode", args.parse_mode);

        const msg = await api("sendDocument", form);
        return textResult(`Document sent: ${basename(filePath)} (message_id ${msg.message_id}).`);
      }

      case "telegram_get_updates": {
        const limit = Math.max(1, Math.min(Number(args.limit) || 5, 20));
        const timeout = Math.max(0, Math.min(Number(args.timeout) || 0, 50));
        const offset = args.offset !== undefined ? Number(args.offset) : _lastUpdateId + 1;
        const updates = await api("getUpdates", { limit, offset, timeout });

        if (!Array.isArray(updates) || updates.length === 0) {
          return textResult("No new messages.");
        }

        for (const u of updates) {
          if (typeof u.update_id === "number" && u.update_id > _lastUpdateId) {
            _lastUpdateId = u.update_id;
          }
        }

        const lines = updates.map((u) => {
          const msg = u.message || u.edited_message || u.channel_post || {};
          const from = msg.from
            ? `${msg.from.first_name || ""}${msg.from.username ? ` (@${msg.from.username})` : ""}`
            : "unknown";
          const body = msg.text || msg.caption || `[${Object.keys(msg).find((k) => k.endsWith("_id")) || "media"}]`;
          return `[${msg.chat?.id ?? "?"}] ${from}: ${body}`;
        });

        return textResult(`${updates.length} update(s):\n${lines.join("\n")}`);
      }

      case "telegram_health": {
        const me = await api("getMe", {});
        return textResult(
          `Bot connected: @${me.username} (${me.first_name})\n` +
            `ID: ${me.id}\n` +
            `Reads all group messages: ${me.can_read_all_group_messages ?? false}\n` +
            `Inline queries: ${me.supports_inline_queries ?? false}`,
        );
      }

      case "telegram_send_reaction": {
        const chatId = requireChatId(args);
        await api("setMessageReaction", {
          chat_id: chatId,
          message_id: args.message_id,
          reaction: [{ type: "emoji", emoji: args.emoji }],
          is_big: false,
        });
        return textResult(`Reaction ${args.emoji} set on message ${args.message_id}.`);
      }

      case "telegram_download_file": {
        const fileInfo = await api("getFile", { file_id: args.file_id });
        if (!fileInfo?.file_path) throw new Error("Telegram did not return a file path.");

        const res = await fetchWithRetry(
          `https://api.telegram.org/file/bot${BOT_TOKEN}/${fileInfo.file_path}`,
        );
        if (!res.ok) throw new Error(`Download failed (HTTP ${res.status}).`);

        const buffer = Buffer.from(await res.arrayBuffer());
        mkdirSync(UPLOAD_DIR, { recursive: true });
        const ext = fileInfo.file_path.includes(".")
          ? `.${fileInfo.file_path.split(".").pop()}`
          : "";
        const localPath = join(
          UPLOAD_DIR,
          `${Date.now()}_${String(args.file_id).slice(0, 8)}${ext}`,
        );
        writeFileSync(localPath, buffer);

        return textResult(
          `File downloaded\n  Path: ${localPath}\n  Size: ${buffer.length} bytes`,
        );
      }

      default:
        return { content: [{ type: "text", text: `Unknown tool: ${name}` }], isError: true };
    }
  } catch (err) {
    return {
      content: [{ type: "text", text: `Telegram error: ${redact(err.message)}` }],
      isError: true,
    };
  }
});

function textResult(text) {
  return { content: [{ type: "text", text }] };
}

const transport = new StdioServerTransport();
await server.connect(transport);

process.stderr.write(
  BOT_TOKEN
    ? "telegram-commandcode MCP server ready (token configured)\n"
    : "telegram-commandcode MCP server running WITHOUT TELEGRAM_BOT_TOKEN\n",
);
