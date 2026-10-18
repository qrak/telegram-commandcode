/**
 * MCP server protocol tests.
 *
 * These spawn the real server over stdio and speak JSON-RPC to it, so they
 * catch the class of bug that made the server refuse to start at all
 * ("Schema is missing a method literal" with @modelcontextprotocol/sdk 1.x).
 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";

const NODE = process.env.NODE_BIN || process.execPath;

function startServer(env = {}) {
  const child = spawn(NODE, ["index.js"], {
    cwd: process.cwd(),
    env: { ...process.env, TELEGRAM_BOT_TOKEN: "", ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });

  let buffer = "";
  const messages = [];
  const waiters = [];

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      const message = JSON.parse(line);
      messages.push(message);
      for (const waiter of [...waiters]) {
        const match = waiter.match(message);
        if (match) {
          waiters.splice(waiters.indexOf(waiter), 1);
          waiter.resolve(message);
        }
      }
    }
  });

  const stderr = [];
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => stderr.push(chunk));

  const send = (payload) => child.stdin.write(JSON.stringify(payload) + "\n");

  const waitFor = (match, timeoutMs = 5000) =>
    new Promise((resolve, reject) => {
      const existing = messages.find(match);
      if (existing) return resolve(existing);
      const waiter = { match, resolve };
      waiters.push(waiter);
      const timer = setTimeout(() => {
        waiters.splice(waiters.indexOf(waiter), 1);
        reject(new Error(`timeout; stderr=${stderr.join("")}`));
      }, timeoutMs);
      const originalResolve = waiter.resolve;
      waiter.resolve = (value) => {
        clearTimeout(timer);
        originalResolve(value);
      };
    });

  const stop = async () => {
    if (child.exitCode !== null) return;
    const exited = once(child, "exit");
    child.kill();
    await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 2000))]);
  };

  return { child, send, waitFor, stop, stderr };
}

async function handshake(server) {
  server.send({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "test", version: "1.0.0" },
    },
  });
  await server.waitFor((m) => m.id === 1);

  server.send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  server.send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });

  const reply = await server.waitFor((m) => m.id === 2);
  return reply.result.tools;
}

test("starts and answers initialize without a token", async () => {
  const server = startServer();
  try {
    const tools = await handshake(server);
    assert.equal(tools.length, 1);
    assert.equal(tools[0].name, "telegram_status");
  } finally {
    await server.stop();
  }
});

test("lists all seven tools when a token is configured", async () => {
  const server = startServer({ TELEGRAM_BOT_TOKEN: "123456:TEST" });
  try {
    const tools = await handshake(server);
    const names = tools.map((tool) => tool.name).sort();
    assert.deepEqual(names, [
      "telegram_download_file",
      "telegram_get_updates",
      "telegram_health",
      "telegram_send_file",
      "telegram_send_message",
      "telegram_send_photo",
      "telegram_send_reaction",
    ]);
    for (const tool of tools) {
      assert.equal(tool.inputSchema.type, "object");
      assert.ok(tool.description.length > 20);
    }
  } finally {
    await server.stop();
  }
});

test("unknown tool returns an error result instead of crashing", async () => {
  const server = startServer({ TELEGRAM_BOT_TOKEN: "123456:TEST" });
  try {
    await handshake(server);
    server.send({
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: { name: "does_not_exist", arguments: {} },
    });
    const reply = await server.waitFor((m) => m.id === 3);
    assert.equal(reply.result.isError, true);
  } finally {
    await server.stop();
  }
});

test("send_message without chat_id fails before any network call", async () => {
  const server = startServer({ TELEGRAM_BOT_TOKEN: "123456:TEST" });
  try {
    await handshake(server);
    server.send({
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: { name: "telegram_send_message", arguments: { text: "hi" } },
    });
    const reply = await server.waitFor((m) => m.id === 4);
    assert.equal(reply.result.isError, true);
    assert.match(reply.result.content[0].text, /chat_id/i);
  } finally {
    await server.stop();
  }
});

test("messages over 4096 characters are rejected client-side", async () => {
  const server = startServer({
    TELEGRAM_BOT_TOKEN: "123456:TEST",
    TELEGRAM_DEFAULT_CHAT_ID: "42",
  });
  try {
    await handshake(server);
    server.send({
      jsonrpc: "2.0",
      id: 5,
      method: "tools/call",
      params: {
        name: "telegram_send_message",
        arguments: { text: "x".repeat(4097) },
      },
    });
    const reply = await server.waitFor((m) => m.id === 5);
    assert.equal(reply.result.isError, true);
    assert.match(reply.result.content[0].text, /4096/);
  } finally {
    await server.stop();
  }
});

test("reaction without a chat id fails before any network call", async () => {
  const server = startServer({ TELEGRAM_BOT_TOKEN: "123456:TEST" });
  try {
    await handshake(server);
    server.send({
      jsonrpc: "2.0",
      id: 6,
      method: "tools/call",
      params: {
        name: "telegram_send_reaction",
        arguments: { message_id: 1, emoji: "👀" },
      },
    });
    const reply = await server.waitFor((m) => m.id === 6);
    assert.equal(reply.result.isError, true);
    assert.match(reply.result.content[0].text, /chat_id/i);
  } finally {
    await server.stop();
  }
});
