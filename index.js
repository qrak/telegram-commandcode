#!/usr/bin/env node
/**
 * telegram-commandcode — MCP server for Telegram integration
 * 
 * Exposes Telegram messaging tools to Command Code via MCP stdio transport.
 * 
 * Setup:
 *   1. Create a bot at @BotFather → get TELEGRAM_BOT_TOKEN
 *   2. cmd mcp add telegram -- npx telegram-commandcode
 *   3. Set env: TELEGRAM_BOT_TOKEN=<token> (or use --env flag)
 * 
 * @see https://commandcode.ai/docs/mcp
 */

import { readFileSync, existsSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

// ---------------------------------------------------------------------------
// Resolve bot token from env or local .env file
// ---------------------------------------------------------------------------

const __dirname = fileURLToPath(new URL(".", import.meta.url));

function loadEnv() {
  // 1. Check environment variable
  if (process.env.TELEGRAM_BOT_TOKEN) return;

  // 2. Try .env file in cwd
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

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const API_BASE = `https://api.telegram.org/bot${BOT_TOKEN}`;
const DEFAULT_CHAT_ID = process.env.TELEGRAM_DEFAULT_CHAT_ID || "";

// ---------------------------------------------------------------------------
// Telegram API helpers
// ---------------------------------------------------------------------------

async function api(method, body) {
  const url = `${API_BASE}/${method}`;
  const isFormData = body instanceof FormData;

  const res = await fetch(url, {
    method: "POST",
    headers: isFormData ? {} : { "Content-Type": "application/json" },
    body: isFormData ? body : JSON.stringify(body),
  });

  const data = await res.json();
  if (!data.ok) {
    throw new Error(`Telegram API error: ${data.description} (code ${data.error_code})`);
  }
  return data.result;
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const TOOLS = [
  {
    name: "telegram_send_message",
    description:
      "Send a text message to a Telegram chat. Supports Markdown formatting (use parse_mode=MarkdownV2 for rich text with **bold**, *italic*, `code`, etc.).",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: {
          type: "string",
          description: `Target chat ID (user, group, or channel). Default: ${DEFAULT_CHAT_ID || "(none configured)"}`,
        },
        text: {
          type: "string",
          description: "Message text to send.",
        },
        parse_mode: {
          type: "string",
          enum: ["MarkdownV2", "HTML"],
          description: "Parse mode for formatting. Use MarkdownV2 for rich text.",
        },
      },
      required: ["text"],
    },
  },
  {
    name: "telegram_send_photo",
    description:
      "Send a photo to a Telegram chat. Provide a public URL or an absolute local file path.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: {
          type: "string",
          description: `Target chat ID. Default: ${DEFAULT_CHAT_ID || "(none configured)"}`,
        },
        photo: {
          type: "string",
          description: "Photo source: a public URL (https://...) or absolute local file path.",
        },
        caption: {
          type: "string",
          description: "Optional caption.",
        },
        parse_mode: {
          type: "string",
          enum: ["MarkdownV2", "HTML"],
          description: "Caption parse mode.",
        },
      },
      required: ["photo"],
    },
  },
  {
    name: "telegram_send_file",
    description:
      "Send a file/document to a Telegram chat. Provide an absolute local file path or a public URL.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: {
          type: "string",
          description: `Target chat ID. Default: ${DEFAULT_CHAT_ID || "(none configured)"}`,
        },
        file_path: {
          type: "string",
          description: "Absolute path to the file to send (e.g., /tmp/report.pdf) or a public URL.",
        },
        caption: {
          type: "string",
          description: "Optional caption.",
        },
      },
      required: ["file_path"],
    },
  },
  {
    name: "telegram_get_updates",
    description:
      "Get recent messages/updates from the bot. Useful for checking if someone sent a command. Returns the 5 most recent updates (newest first).",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: "Max updates to return (default: 5, max: 20).",
        },
      },
    },
  },
];

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const server = new Server(
  {
    name: "telegram-commandcode",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// -- list tools --------------------------------------------------------------

server.setRequestHandler("tools/list", async () => {
  const ready = !!BOT_TOKEN;
  return {
    tools: ready
      ? TOOLS
      : [
          {
            name: "telegram_status",
            description:
              "⛔ Telegram is NOT configured. Set TELEGRAM_BOT_TOKEN environment variable (get one from @BotFather). Use --env when adding: cmd mcp add telegram -e TELEGRAM_BOT_TOKEN=<token> -- npx telegram-commandcode",
            inputSchema: { type: "object", properties: {} },
          },
        ],
  };
});

// -- call tools --------------------------------------------------------------

server.setRequestHandler("tools/call", async (req) => {
  const { name, arguments: args = {} } = req.params;

  if (!BOT_TOKEN) {
    return {
      content: [
        {
          type: "text",
          text: "⛔ TELEGRAM_BOT_TOKEN not set. Create a bot at @BotFather, then restart with the token. See .env.example or pass --env to cmd mcp add.",
        },
      ],
      isError: true,
    };
  }

  const chat_id = args.chat_id || DEFAULT_CHAT_ID;

  try {
    switch (name) {
      // ---- send_message ----
      case "telegram_send_message": {
        if (!chat_id) {
          throw new Error("chat_id is required — configure TELEGRAM_DEFAULT_CHAT_ID in .env or pass it explicitly.");
        }
        const msg = await api("sendMessage", {
          chat_id,
          text: args.text,
          parse_mode: args.parse_mode || "",
          link_preview_options: { is_disabled: true },
        });
        return {
          content: [
            {
              type: "text",
              text: `✅ Message sent to chat ${msg.chat.id} (message_id: ${msg.message_id})`,
            },
          ],
        };
      }

      // ---- send_photo ----
      case "telegram_send_photo": {
        if (!chat_id) {
          throw new Error("chat_id is required.");
        }

        const photo = args.photo;
        if (photo.startsWith("http://") || photo.startsWith("https://")) {
          // Send by URL
          const msg = await api("sendPhoto", {
            chat_id,
            photo,
            caption: args.caption || "",
            parse_mode: args.parse_mode || "",
          });
          return {
            content: [
              { type: "text", text: `✅ Photo sent (message_id: ${msg.message_id})` },
            ],
          };
        }

        // Send by local file path
        const filePath = resolve(photo);
        if (!existsSync(filePath)) {
          throw new Error(`File not found: ${filePath}`);
        }
        const form = new FormData();
        form.set("chat_id", chat_id);
        form.set(
          "photo",
          new Blob([readFileSync(filePath)]),
          filePath.split("/").pop()
        );
        if (args.caption) form.set("caption", args.caption);
        if (args.parse_mode) form.set("parse_mode", args.parse_mode);

        const msg = await api("sendPhoto", form);
        return {
          content: [
            { type: "text", text: `✅ Photo sent (message_id: ${msg.message_id})` },
          ],
        };
      }

      // ---- send_file ----
      case "telegram_send_file": {
        if (!chat_id) {
          throw new Error("chat_id is required.");
        }

        const f = args.file_path;
        if (f.startsWith("http://") || f.startsWith("https://")) {
          const msg = await api("sendDocument", {
            chat_id,
            document: f,
            caption: args.caption || "",
          });
          return {
            content: [
              { type: "text", text: `✅ Document sent (message_id: ${msg.message_id})` },
            ],
          };
        }

        const filePath = resolve(f);
        if (!existsSync(filePath)) {
          throw new Error(`File not found: ${filePath}`);
        }
        const form = new FormData();
        form.set("chat_id", chat_id);
        form.set(
          "document",
          new Blob([readFileSync(filePath)]),
          filePath.split("/").pop()
        );
        if (args.caption) form.set("caption", args.caption);

        const msg = await api("sendDocument", form);
        return {
          content: [
            { type: "text", text: `✅ Document sent: ${filePath.split("/").pop()} (message_id: ${msg.message_id})` },
          ],
        };
      }

      // ---- get_updates ----
      case "telegram_get_updates": {
        const limit = Math.min(args.limit || 5, 20);
        const updates = await api("getUpdates", { limit, offset: -1 });

        if (!updates || updates.length === 0) {
          return {
            content: [{ type: "text", text: "📭 No recent messages." }],
          };
        }

        const lines = updates.map((u) => {
          const msg = u.message || u.edited_message || {};
          const from = msg.from
            ? `${msg.from.first_name || ""} (@${msg.from.username || "no-username"})`
            : "unknown";
          return `[${msg.chat?.id}] ${from}: ${msg.text || msg.caption || "(no text)"}`;
        });

        return {
          content: [
            {
              type: "text",
              text: `📬 Last ${updates.length} update(s):\n${lines.map((l) => `  • ${l}`).join("\n")}`,
            },
          ],
        };
      }

      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (err) {
    return {
      content: [{ type: "text", text: `❌ ${err.message}` }],
      isError: true,
    };
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const transport = new StdioServerTransport();
await server.connect(transport);

// Log readiness to stderr (stdio transport — stdout is for MCP protocol JSON)
const status = BOT_TOKEN
  ? "✅ telegram-commandcode MCP server ready (token configured)"
  : "⚠️  telegram-commandcode MCP server running but TELEGRAM_BOT_TOKEN is NOT set";
process.stderr.write(`${status}\n`);
