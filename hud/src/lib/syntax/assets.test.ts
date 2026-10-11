// The syntax worker's loader gate (ED-5): it fetches the runtime and the
// grammars the HUD ships, from the HUD's own origin, and nothing else — and
// nothing in the worker fetches or compiles any other way.

import { fetchShipped, QUERIES, RefusedUrl, RUNTIME_URL, shippedUrl, WASM_URLS } from "./assets";
import { GRAMMAR_NAMES } from "./grammars";
import workerSource from "./worker.ts?raw";
import engineSource from "./engine.ts?raw";
import clientSource from "./client.ts?raw";
import assetsSource from "./assets.ts?raw";

const BASE = "http://127.0.0.1:8402/assets/worker-abc.js";
const MAP = ["/assets/web-tree-sitter-1.wasm", "/assets/tree-sitter-cpp-2.wasm"];

describe("the loader refuses any URL outside its map", () => {
  it("lets through a shipped file, relative or absolute on the same origin", () => {
    expect(shippedUrl("/assets/tree-sitter-cpp-2.wasm", BASE, MAP)).toBe("http://127.0.0.1:8402/assets/tree-sitter-cpp-2.wasm");
    expect(shippedUrl("http://127.0.0.1:8402/assets/web-tree-sitter-1.wasm", BASE, MAP)).toBe("http://127.0.0.1:8402/assets/web-tree-sitter-1.wasm");
    expect(shippedUrl("tree-sitter-cpp-2.wasm", BASE, MAP)).toBe("http://127.0.0.1:8402/assets/tree-sitter-cpp-2.wasm");
  });

  it.each([
    ["another origin", "http://evil.example/assets/tree-sitter-cpp-2.wasm"],
    ["a protocol-relative URL", "//evil.example/assets/tree-sitter-cpp-2.wasm"],
    ["another port on loopback", "http://127.0.0.1:8403/assets/tree-sitter-cpp-2.wasm"],
    ["another path here", "/assets/tree-sitter-evil.wasm"],
    ["a query string", "/assets/tree-sitter-cpp-2.wasm?x=1"],
    ["a data: URL", "data:application/wasm;base64,AGFzbQEAAAA="],
    ["a blob: URL", "blob:http://127.0.0.1:8402/0c0c0c0c"],
    ["javascript:", "javascript:alert(1)"],
    ["a file: URL", "file:///home/johnw/x.wasm"],
    ["nothing", ""],
  ])("refuses %s", (_what, url) => {
    expect(() => shippedUrl(url, BASE, MAP)).toThrow(RefusedUrl);
  });

  it("refuses before any request is made", async () => {
    const calls: string[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = (async (u: string) => {
      calls.push(String(u));
      return new Response(new Uint8Array([0]));
    }) as typeof fetch;
    try {
      await expect(fetchShipped("http://evil.example/x.wasm")).rejects.toThrow(RefusedUrl);
      expect(calls).toEqual([]);
    } finally {
      globalThis.fetch = real;
    }
  });

  it("its map is the runtime and one URL per grammar, each with its queries", () => {
    expect(Object.keys(WASM_URLS).sort()).toEqual([...GRAMMAR_NAMES].sort());
    expect(new Set([RUNTIME_URL, ...Object.values(WASM_URLS)]).size).toBe(GRAMMAR_NAMES.length + 1);
    for (const u of [RUNTIME_URL, ...Object.values(WASM_URLS)]) expect(u).toMatch(/\.wasm$/);
    for (const g of GRAMMAR_NAMES) expect(QUERIES[g].highlights).toMatch(/@/);
  });
});

describe("nothing in the syntax code fetches or compiles another way", () => {
  const code = (s: string) => s.replace(/\/\/[^\n]*/g, "");
  it("fetch() is called in one place, the gate", () => {
    expect([...code(assetsSource).matchAll(/\bfetch\(/g)]).toHaveLength(1);
    for (const s of [workerSource, engineSource, clientSource]) expect(code(s)).not.toMatch(/\bfetch\(/);
  });
  it("Language.load takes only the gate's bytes; the runtime's .wasm comes through the gate too; nothing is located by name", () => {
    const w = code(workerSource);
    expect([...w.matchAll(/Language\.load\(/g)]).toHaveLength(1);
    expect(w).toMatch(/Language\.load\(await fetchShipped\(WASM_URLS\[g\]\)\)/);
    expect(w).toMatch(/wasmBinary = await fetchShipped\(RUNTIME_URL\)/);
    expect(w).toMatch(/locateFile: \(path: string\) => \{\s*throw/);
    for (const s of [w, code(engineSource), code(clientSource)]) {
      expect(s).not.toMatch(/importScripts|WebAssembly\.(compile|instantiate)|new Function|\beval\(/);
    }
    expect(code(engineSource)).not.toMatch(/Language\.load/);
  });
});
