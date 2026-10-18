#!/usr/bin/env node
/**
 * Build the Agensi / Claude Code skill package.
 *
 * Produces dist/telegram-bridge-skill.zip with SKILL.md at the archive root,
 * which is what both marketplaces expect. No dependencies: the ZIP writer
 * below is intentionally small and deterministic so the hash of the artifact
 * only changes when the content changes.
 */

import { deflateRawSync } from "node:zlib";
import { readFileSync, readdirSync, statSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join, relative, sep } from "node:path";

const ROOT = process.cwd();
const SOURCE = join(ROOT, "skill");
const OUT_DIR = join(ROOT, "dist");
const OUT_FILE = join(OUT_DIR, "telegram-bridge-skill.zip");

// ---------------------------------------------------------------------------
// Minimal ZIP writer (deflate, no data descriptors, fixed timestamps)
// ---------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function dosDateTime(date) {
  const time =
    (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2);
  const day =
    ((date.getFullYear() - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
  return { time, day };
}

function listFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry.startsWith(".")) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...listFiles(full));
    else out.push(full);
  }
  return out.sort();
}

function buildZip(files, root) {
  const now = new Date();
  const fixed = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
    12,
    0,
    0,
  );
  const { time, day } = dosDateTime(fixed);

  const localChunks = [];
  const centralChunks = [];
  let offset = 0;

  for (const file of files) {
    const name = relative(root, file).split(sep).join("/");
    const nameBytes = Buffer.from(name, "utf8");
    // Normalise line endings so the artifact is identical on Windows and Linux.
    const content = Buffer.from(
      readFileSync(file, "utf8").replace(/\r\n/g, "\n"),
      "utf8",
    );
    const compressed = deflateRawSync(content, { level: 9 });
    const crc = crc32(content);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(0x0800, 6); // UTF-8 names
    local.writeUInt16LE(8, 8); // deflate
    local.writeUInt16LE(time, 10);
    local.writeUInt16LE(day, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);

    localChunks.push(local, nameBytes, compressed);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(8, 10);
    central.writeUInt16LE(time, 12);
    central.writeUInt16LE(day, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(content.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30); // extra
    central.writeUInt16LE(0, 32); // comment
    central.writeUInt16LE(0, 34); // disk
    central.writeUInt16LE(0, 36); // internal attrs
    central.writeUInt32LE(0, 38); // external attrs
    central.writeUInt32LE(offset, 42);

    centralChunks.push(central, nameBytes);
    offset += local.length + nameBytes.length + compressed.length;
  }

  const centralSize = centralChunks.reduce((sum, b) => sum + b.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);

  return Buffer.concat([...localChunks, ...centralChunks, end]);
}

// ---------------------------------------------------------------------------
// Validate and build
// ---------------------------------------------------------------------------

const skillFile = join(SOURCE, "SKILL.md");
let skill;
try {
  // A Windows checkout has CRLF; the frontmatter parser below expects LF.
  skill = readFileSync(skillFile, "utf8").replace(/\r\n/g, "\n");
} catch {
  console.error(`Missing ${skillFile}`);
  process.exit(1);
}

const frontmatter = skill.match(/^---\n([\s\S]*?)\n---/);
if (!frontmatter) {
  console.error("SKILL.md has no YAML frontmatter block");
  process.exit(1);
}
for (const key of ["name", "description", "version", "license"]) {
  if (!new RegExp(`^${key}:`, "m").test(frontmatter[1])) {
    console.error(`SKILL.md frontmatter is missing "${key}"`);
    process.exit(1);
  }
}
if (!/^external_urls:/m.test(frontmatter[1])) {
  console.warn('warning: no external_urls declared (Telegram traffic goes to api.telegram.org)');
}

const files = listFiles(SOURCE);
if (files.length === 0) {
  console.error("skill/ is empty");
  process.exit(1);
}

mkdirSync(OUT_DIR, { recursive: true });
rmSync(OUT_FILE, { force: true });
const zip = buildZip(files, SOURCE);
writeFileSync(OUT_FILE, zip);

const kb = (zip.length / 1024).toFixed(1);
console.log(`Built ${relative(ROOT, OUT_FILE)} (${kb} kB, ${files.length} files)`);
for (const file of files) console.log(`  ${relative(SOURCE, file).split(sep).join("/")}`);
