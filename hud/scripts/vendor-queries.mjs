#!/usr/bin/env node
// Vendors what tree-sitter colour needs that npm does not pin (ED-5, editor
// plan §2.6). Dev-run, by hand, from `hud/`; never part of a build:
//
//     node scripts/vendor-queries.mjs            # fetch, then build
//     node scripts/vendor-queries.mjs --offline  # build from what is vendored
//
// 1. **Fetch** (network): nvim-treesitter's `highlights.scm`,
//    `injections.scm` and (ED-6) `folds.scm` for every language in
//    lib/syntax/grammars.ts, and every file they inherit, at the pinned
//    commit, into `vendor/nvim-treesitter/queries/`, with its LICENSE; and the
//    Markdown grammar's two release `.wasm` files and LICENSE into
//    `vendor/tree-sitter/`, each checked against the SHA-256 pinned below.
// 2. **Build** (offline): each language's bundled query
//    (`src/syntax/queries/<grammar>/`) from those files — `inherits` resolved,
//    translated for web-tree-sitter, compiled against its own grammar with the
//    pinned runtime, any pattern the grammar refuses dropped and listed
//    (lib/syntax/translate.ts) — plus `dropped.json`, the NOTICE,
//    `vendor/tree-sitter/manifest.json` (every shipped `.wasm`'s SHA-256) and
//    `vendor/nvim-treesitter/manifest.json` (every vendored nvim file's size
//    and SHA-256, with the commit).
//
// `src/lib/syntax/vendor.test.ts` repeats step 2 and fails on any difference,
// so the bundled queries are always exactly what this script makes of the
// vendored ones.

import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, writeFile, copyFile, stat } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildQuery } from "../src/lib/syntax/translate.ts";
import { GRAMMARS, GRAMMAR_NAMES, RUNTIME } from "../src/lib/syntax/grammars.ts";
import { NVIM, MARKDOWN } from "../src/lib/syntax/pins.ts";

const HUD = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const NVIM_DIR = path.join(HUD, "vendor/nvim-treesitter");
const TS_DIR = path.join(HUD, "vendor/tree-sitter");
const OUT = path.join(HUD, "src/syntax/queries");
const KINDS = ["highlights", "injections", "folds"];

const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

async function get(url) {
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return Buffer.from(await res.arrayBuffer());
}

async function fetchNvim() {
  const raw = (p) => `https://raw.githubusercontent.com/${NVIM.repo}/${NVIM.commit}/${p}`;
  const wanted = new Set();
  const queue = GRAMMAR_NAMES.map((g) => GRAMMARS[g].nvim);
  const files = {};
  while (queue.length) {
    const lang = queue.shift();
    if (wanted.has(lang)) continue;
    wanted.add(lang);
    for (const kind of KINDS) {
      const res = await fetch(raw(`runtime/queries/${lang}/${kind}.scm`));
      if (res.status === 404) continue;
      if (!res.ok) throw new Error(`${lang}/${kind}.scm: HTTP ${res.status}`);
      const text = await res.text();
      files[`${lang}/${kind}.scm`] = text;
      const m = /^;+\s*inherits\s*:?\s*([^\n]*)$/m.exec(text.split("\n").slice(0, 3).join("\n"));
      if (m) for (const p of m[1].split(",")) queue.push(p.trim().replace(/^\((.*)\)$/, "$1"));
    }
  }
  for (const [rel, text] of Object.entries(files)) {
    const to = path.join(NVIM_DIR, "queries", rel);
    await mkdir(path.dirname(to), { recursive: true });
    await writeFile(to, text);
  }
  await writeFile(path.join(NVIM_DIR, "LICENSE"), await get(raw("LICENSE")));
  console.log(`fetched ${Object.keys(files).length} nvim-treesitter query files at ${NVIM.commit.slice(0, 12)}`);
}

async function fetchMarkdown() {
  await mkdir(TS_DIR, { recursive: true });
  for (const [name, want] of Object.entries(MARKDOWN.files)) {
    const buf = await get(`https://github.com/${MARKDOWN.repo}/releases/download/${MARKDOWN.tag}/${name}`);
    const got = sha256(buf);
    if (got !== want) throw new Error(`${name}: SHA-256 ${got}, pinned ${want}; nothing written`);
    await writeFile(path.join(TS_DIR, name), buf);
  }
  const license = await get(`https://raw.githubusercontent.com/${MARKDOWN.repo}/${MARKDOWN.tag}/LICENSE`);
  await mkdir(path.join(TS_DIR, "licenses"), { recursive: true });
  await writeFile(path.join(TS_DIR, "licenses", "tree-sitter-markdown.txt"), license);
  console.log(`fetched the Markdown grammar ${MARKDOWN.tag}, hashes as pinned`);
}

async function build() {
  const { Parser, Language, Query } = await import("web-tree-sitter");
  await Parser.init();
  const readNvim = (lang, kind) => {
    const p = path.join(NVIM_DIR, "queries", lang, `${kind}.scm`);
    return existsSync(p) ? readFileSync(p, "utf8") : null;
  };
  const report = {};
  const manifest = { runtime: await entry(RUNTIME), grammars: {} };
  for (const g of GRAMMAR_NAMES) {
    const language = await Language.load(path.join(HUD, GRAMMARS[g].wasm));
    const compiles = (text) => {
      try {
        new Query(language, text).delete();
        return null;
      } catch (e) {
        return e instanceof Error ? e.message.split("\n")[0] : String(e);
      }
    };
    report[g] = {};
    for (const kind of KINDS) {
      const built = buildQuery(GRAMMARS[g].nvim, kind, readNvim, compiles);
      const header = `; Built by scripts/vendor-queries.mjs from nvim-treesitter ${NVIM.commit} (Apache-2.0):\n`
        + (built.files.length
          ? `; ${built.files.map((f) => `runtime/queries/${f}/${kind}.scm`).join(", ")}.\n`
          : `; nvim-treesitter has no runtime/queries/${GRAMMARS[g].nvim}/${kind}.scm: no patterns.\n`)
        + `; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.\n\n`;
      const to = path.join(OUT, g, `${kind}.scm`);
      await mkdir(path.dirname(to), { recursive: true });
      await writeFile(to, header + built.text);
      report[g][kind] = {
        files: built.files,
        patterns: built.patterns,
        dropped: built.dropped.map(({ file, line, reason }) => ({ file, line, reason })),
        stripped: built.stripped.map(({ file, line, reason }) => ({ file, line, reason })),
      };
      console.log(`${g}/${kind}: ${built.patterns} patterns, ${built.dropped.length} dropped, ${built.stripped.length} directives stripped`);
    }
    manifest.grammars[g] = await entry(GRAMMARS[g]);
  }
  await writeFile(path.join(OUT, "dropped.json"), `${JSON.stringify(report, null, 2)}\n`);
  await copyFile(path.join(NVIM_DIR, "LICENSE"), path.join(OUT, "LICENSE"));
  await writeFile(path.join(OUT, "NOTICE"), notice());
  await mkdir(path.join(TS_DIR, "licenses"), { recursive: true });
  for (const g of GRAMMAR_NAMES) {
    const src = GRAMMARS[g].source;
    if (!src.startsWith("npm:")) continue;
    const pkg = src.slice(4, src.lastIndexOf("@"));
    await copyFile(path.join(HUD, "node_modules", pkg, "LICENSE"), path.join(TS_DIR, "licenses", `${pkg.replace("/", "__")}.txt`));
  }
  await copyFile(path.join(HUD, "node_modules/web-tree-sitter/LICENSE"), path.join(TS_DIR, "licenses", "web-tree-sitter.txt"));
  await writeFile(path.join(TS_DIR, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  await writeFile(path.join(NVIM_DIR, "manifest.json"), `${JSON.stringify(await nvimManifest(), null, 2)}\n`);
  console.log("wrote src/syntax/queries/, vendor/tree-sitter/manifest.json and vendor/nvim-treesitter/manifest.json");
}

/** Every vendored nvim-treesitter file (the manifest itself aside): its size and SHA-256, by path, and the commit. */
async function nvimManifest() {
  const files = {};
  const walk = async (dir) => {
    for (const d of (await readdir(dir, { withFileTypes: true })).sort((a, b) => (a.name < b.name ? -1 : 1))) {
      const p = path.join(dir, d.name);
      if (d.isDirectory()) await walk(p);
      else if (p !== path.join(NVIM_DIR, "manifest.json")) {
        const buf = await readFile(p);
        files[path.relative(NVIM_DIR, p).split(path.sep).join("/")] = { bytes: buf.length, sha256: sha256(buf) };
      }
    }
  };
  await walk(NVIM_DIR);
  return { repo: NVIM.repo, commit: NVIM.commit, files };
}

async function entry({ wasm, source }) {
  const buf = await readFile(path.join(HUD, wasm));
  const { size } = await stat(path.join(HUD, wasm));
  return { file: wasm, source, bytes: size, sha256: sha256(buf) };
}

function notice() {
  return `The highlight, injection and fold queries in this directory are derived from
nvim-treesitter (https://github.com/${NVIM.repo}), runtime/queries/,
at commit ${NVIM.commit} (${NVIM.date}), licensed under the Apache License,
Version 2.0 (LICENSE beside this file). The unmodified files are vendored under
hud/vendor/nvim-treesitter/queries/.

Modifications, made by hud/scripts/vendor-queries.mjs (lib/syntax/translate.ts):
  - each language's \`; inherits:\` files are concatenated ahead of its own;
  - \`#lua-match?\` (Lua patterns) is rewritten as \`#match?\` (JavaScript regular
    expressions), and its \`not-\`/\`any-\` forms likewise;
  - \`#set! @capture key value\` (nvim-only metadata) is removed from the pattern;
  - patterns using a predicate or directive web-tree-sitter and the HUD's
    syntax worker do not support, or naming a node the pinned grammar does not
    have, are removed. dropped.json lists every one, with its reason.
`;
}

const offline = process.argv.includes("--offline");
if (!offline) {
  await fetchNvim();
  await fetchMarkdown();
}
await build();
