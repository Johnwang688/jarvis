// @vitest-environment node
// What tree-sitter colour bundles (ED-5, editor plan §2.6): every query
// compiles against its own grammar with the pinned runtime; the bundled
// queries are exactly what the vendoring script makes of the vendored nvim
// files; every shipped .wasm is the one the manifest names; every pin agrees.

import { createHash } from "node:crypto";
import { Query } from "web-tree-sitter";
import { buildQuery } from "./translate";
import { GRAMMARS, GRAMMAR_NAMES, RUNTIME, type GrammarName } from "./grammars";
import { MARKDOWN, NVIM } from "./pins";
import { bundledQuery, bytes, exists, language, read } from "./testkit";
import { typeOf } from "./legend";

const KINDS = ["highlights", "injections"] as const;

/** The capture names a query uses, outside its strings and comments. */
function captureNames(query: string): Set<string> {
  const out = new Set<string>();
  for (let i = 0; i < query.length; i++) {
    const c = query[i];
    if (c === '"') {
      for (i++; i < query.length && query[i] !== '"'; i++) if (query[i] === "\\") i++;
    } else if (c === ";") {
      while (i < query.length && query[i] !== "\n") i++;
    } else if (c === "@") {
      const m = /^[A-Za-z_][\w.\-]*/.exec(query.slice(i + 1));
      if (m) out.add(m[0]);
    }
  }
  return out;
}
const sha256 = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

describe("every bundled query compiles against its bundled grammar", () => {
  for (const g of GRAMMAR_NAMES) {
    for (const kind of KINDS) {
      it(`${g}/${kind}.scm`, async () => {
        const lang = await language(g);
        const text = bundledQuery(g, kind);
        const q = new Query(lang, text);
        expect(q.patternCount()).toBeGreaterThan(0);
        // Only predicates web-tree-sitter or the worker evaluate are left.
        const left = new Set(q.predicates.flat().map((p) => p.operator));
        for (const op of left) expect(["has-ancestor?", "not-has-ancestor?", "has-parent?", "not-has-parent?", "offset!"]).toContain(op);
        q.delete();
      });
    }
  }

  it("and a query naming a node the grammar lacks is refused by the runtime (what the test relies on)", async () => {
    const lang = await language("cpp");
    expect(() => new Query(lang, "(no_such_node) @x")).toThrow(/no_such_node/);
  });
});

describe("the bundled queries are the vendoring script's output, exactly", () => {
  const readNvim = (lang: string, kind: string) =>
    exists(`vendor/nvim-treesitter/queries/${lang}/${kind}.scm`) ? read(`vendor/nvim-treesitter/queries/${lang}/${kind}.scm`) : null;
  const report = JSON.parse(read("src/syntax/queries/dropped.json")) as Record<string, Record<string, { files: string[]; patterns: number; dropped: unknown[]; stripped: unknown[] }>>;

  for (const g of GRAMMAR_NAMES) {
    it(`${g}: rebuilt from vendor/nvim-treesitter at ${NVIM.commit.slice(0, 12)}, with the same dropped list`, async () => {
      const lang = await language(g);
      const compiles = (text: string) => {
        try {
          new Query(lang, text).delete();
          return null;
        } catch (e) {
          return e instanceof Error ? e.message.split("\n")[0] : String(e);
        }
      };
      for (const kind of KINDS) {
        const built = buildQuery(GRAMMARS[g].nvim, kind, readNvim, compiles);
        const bundled = bundledQuery(g, kind);
        expect(bundled.endsWith(built.text), `${g}/${kind}.scm is not what the script builds; run scripts/vendor-queries.mjs --offline`).toBe(true);
        expect(bundled).toContain(NVIM.commit);
        const r = report[g][kind];
        expect(r.files).toEqual(built.files);
        expect(r.patterns).toBe(built.patterns);
        expect(r.dropped).toEqual(built.dropped.map(({ file, line, reason }) => ({ file, line, reason })));
        expect(r.stripped).toEqual(built.stripped.map(({ file, line, reason }) => ({ file, line, reason })));
      }
    });
  }

  it("the dropped list is short and each entry says why", () => {
    const all = Object.entries(report).flatMap(([g, kinds]) => Object.values(kinds).flatMap((k) => k.dropped.map((d) => ({ g, ...(d as object) }))));
    expect(all).toEqual([{ g: "html", file: "html_tags/injections.scm", line: 36, reason: "#gsub! is not supported" }]);
  });

  it("the queries carry nvim-treesitter's licence and a NOTICE naming the commit", () => {
    expect(read("src/syntax/queries/LICENSE")).toContain("Apache License");
    expect(read("src/syntax/queries/LICENSE")).toBe(read("vendor/nvim-treesitter/LICENSE"));
    expect(read("src/syntax/queries/NOTICE")).toContain(NVIM.commit);
  });

  it("every capture the queries draw with has a row in the Graphite scheme", () => {
    // Captures that draw nothing by design: nvim's spell and conceal marks, and private `_` captures.
    const silent = new Set(["spell", "nospell", "conceal", "conceal_lines"]);
    const missing = new Set<string>();
    for (const g of GRAMMAR_NAMES) {
      for (const name of captureNames(bundledQuery(g, "highlights"))) {
        if (silent.has(name) || name.startsWith("_")) continue;
        if (typeOf(name) < 0) missing.add(`${g}: @${name}`);
      }
    }
    expect([...missing]).toEqual([]);
  });
});

describe("every shipped .wasm is the one the manifest names", () => {
  const manifest = JSON.parse(read("vendor/tree-sitter/manifest.json")) as {
    runtime: { file: string; source: string; bytes: number; sha256: string };
    grammars: Record<string, { file: string; source: string; bytes: number; sha256: string }>;
  };

  it("the manifest lists exactly the grammars in lib/syntax/grammars.ts, and the runtime", () => {
    expect(Object.keys(manifest.grammars).sort()).toEqual([...GRAMMAR_NAMES].sort());
    expect(manifest.runtime).toMatchObject({ file: RUNTIME.wasm, source: RUNTIME.source });
  });

  for (const g of [...GRAMMAR_NAMES, "runtime"] as (GrammarName | "runtime")[]) {
    it(`${g}: SHA-256 and size as recorded`, () => {
      const entry = g === "runtime" ? manifest.runtime : manifest.grammars[g];
      const want = g === "runtime" ? RUNTIME : GRAMMARS[g];
      expect(entry.file).toBe(want.wasm);
      expect(entry.source).toBe(want.source);
      const b = bytes(entry.file);
      expect(b.length).toBe(entry.bytes);
      expect(sha256(b)).toBe(entry.sha256);
    });
  }

  it("the vendored Markdown grammar is the release's, as pinned", () => {
    for (const [name, hash] of Object.entries(MARKDOWN.files)) {
      expect(sha256(bytes(`vendor/tree-sitter/${name}`))).toBe(hash);
    }
    expect(read("vendor/tree-sitter/licenses/tree-sitter-markdown.txt")).toContain("MIT");
  });

  it("package.json pins each npm grammar and the runtime exactly, the lockfile holds an integrity hash for each", () => {
    const pkg = JSON.parse(read("package.json")) as { dependencies: Record<string, string> };
    const lock = JSON.parse(read("package-lock.json")) as { packages: Record<string, { version: string; integrity?: string }> };
    const npm = [RUNTIME.source, ...GRAMMAR_NAMES.map((g) => GRAMMARS[g].source)].filter((s) => s.startsWith("npm:"));
    for (const src of new Set(npm)) {
      const at = src.lastIndexOf("@");
      const name = src.slice(4, at), version = src.slice(at + 1);
      expect(pkg.dependencies[name], name).toBe(version);
      const entry = lock.packages[`node_modules/${name}`];
      expect(entry?.version, name).toBe(version);
      expect(entry?.integrity, name).toMatch(/^sha512-/);
      expect(read(`vendor/tree-sitter/licenses/${name.replace("/", "__")}.txt`)).toContain("MIT");
    }
  });
});
