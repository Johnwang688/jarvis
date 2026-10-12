// What the syntax worker loads, and the one gate it loads through (ED-5,
// editor plan §2.6). Imported by `worker.ts` only, so neither the grammars'
// URLs nor the query texts are in any chunk the window itself runs.
//
// Each `.wasm` is a Vite `?url` import: built into `dist/assets/` with a
// content hash, served by the daemon from the HUD's own origin, never fetched
// from anywhere else. The queries are `?raw` imports — bundled text, never
// fetched at all.
//
// **The loader refuses any URL outside its map.** `fetchShipped` fetches only
// the runtime's own `.wasm` and the grammars below, resolved against the
// worker's origin; anything else — another origin, another path on this one,
// a `data:` or `blob:` URL — is refused before a request is made. A grammar
// is code the worker runs (WebAssembly, under the HUD's CSP's
// `'wasm-unsafe-eval'`), so the set it may come from is this file and nothing
// a page, a file or a message can name.

import runtimeUrl from "web-tree-sitter/web-tree-sitter.wasm?url";
import cppUrl from "tree-sitter-cpp/tree-sitter-cpp.wasm?url";
import pythonUrl from "tree-sitter-python/tree-sitter-python.wasm?url";
import javascriptUrl from "tree-sitter-javascript/tree-sitter-javascript.wasm?url";
import typescriptUrl from "tree-sitter-typescript/tree-sitter-typescript.wasm?url";
import tsxUrl from "tree-sitter-typescript/tree-sitter-tsx.wasm?url";
import jsonUrl from "tree-sitter-json/tree-sitter-json.wasm?url";
import bashUrl from "tree-sitter-bash/tree-sitter-bash.wasm?url";
import cssUrl from "tree-sitter-css/tree-sitter-css.wasm?url";
import htmlUrl from "tree-sitter-html/tree-sitter-html.wasm?url";
import yamlUrl from "@tree-sitter-grammars/tree-sitter-yaml/tree-sitter-yaml.wasm?url";
import tomlUrl from "@tree-sitter-grammars/tree-sitter-toml/tree-sitter-toml.wasm?url";
import markdownUrl from "../../../vendor/tree-sitter/tree-sitter-markdown.wasm?url";
import markdownInlineUrl from "../../../vendor/tree-sitter/tree-sitter-markdown_inline.wasm?url";

import cppHighlights from "../../syntax/queries/cpp/highlights.scm?raw";
import cppInjections from "../../syntax/queries/cpp/injections.scm?raw";
import pythonHighlights from "../../syntax/queries/python/highlights.scm?raw";
import pythonInjections from "../../syntax/queries/python/injections.scm?raw";
import javascriptHighlights from "../../syntax/queries/javascript/highlights.scm?raw";
import javascriptInjections from "../../syntax/queries/javascript/injections.scm?raw";
import typescriptHighlights from "../../syntax/queries/typescript/highlights.scm?raw";
import typescriptInjections from "../../syntax/queries/typescript/injections.scm?raw";
import tsxHighlights from "../../syntax/queries/tsx/highlights.scm?raw";
import tsxInjections from "../../syntax/queries/tsx/injections.scm?raw";
import jsonHighlights from "../../syntax/queries/json/highlights.scm?raw";
import jsonInjections from "../../syntax/queries/json/injections.scm?raw";
import bashHighlights from "../../syntax/queries/bash/highlights.scm?raw";
import bashInjections from "../../syntax/queries/bash/injections.scm?raw";
import cssHighlights from "../../syntax/queries/css/highlights.scm?raw";
import cssInjections from "../../syntax/queries/css/injections.scm?raw";
import htmlHighlights from "../../syntax/queries/html/highlights.scm?raw";
import htmlInjections from "../../syntax/queries/html/injections.scm?raw";
import yamlHighlights from "../../syntax/queries/yaml/highlights.scm?raw";
import yamlInjections from "../../syntax/queries/yaml/injections.scm?raw";
import tomlHighlights from "../../syntax/queries/toml/highlights.scm?raw";
import tomlInjections from "../../syntax/queries/toml/injections.scm?raw";
import markdownHighlights from "../../syntax/queries/markdown/highlights.scm?raw";
import markdownInjections from "../../syntax/queries/markdown/injections.scm?raw";
import markdownInlineHighlights from "../../syntax/queries/markdown_inline/highlights.scm?raw";
import markdownInlineInjections from "../../syntax/queries/markdown_inline/injections.scm?raw";

import type { GrammarName } from "./grammars";

export const RUNTIME_URL: string = runtimeUrl;

export const WASM_URLS: Readonly<Record<GrammarName, string>> = {
  cpp: cppUrl,
  python: pythonUrl,
  javascript: javascriptUrl,
  typescript: typescriptUrl,
  tsx: tsxUrl,
  json: jsonUrl,
  bash: bashUrl,
  css: cssUrl,
  html: htmlUrl,
  yaml: yamlUrl,
  toml: tomlUrl,
  markdown: markdownUrl,
  markdown_inline: markdownInlineUrl,
};

export const QUERIES: Readonly<Record<GrammarName, { highlights: string; injections: string }>> = {
  cpp: { highlights: cppHighlights, injections: cppInjections },
  python: { highlights: pythonHighlights, injections: pythonInjections },
  javascript: { highlights: javascriptHighlights, injections: javascriptInjections },
  typescript: { highlights: typescriptHighlights, injections: typescriptInjections },
  tsx: { highlights: tsxHighlights, injections: tsxInjections },
  json: { highlights: jsonHighlights, injections: jsonInjections },
  bash: { highlights: bashHighlights, injections: bashInjections },
  css: { highlights: cssHighlights, injections: cssInjections },
  html: { highlights: htmlHighlights, injections: htmlInjections },
  yaml: { highlights: yamlHighlights, injections: yamlInjections },
  toml: { highlights: tomlHighlights, injections: tomlInjections },
  markdown: { highlights: markdownHighlights, injections: markdownInjections },
  markdown_inline: { highlights: markdownInlineHighlights, injections: markdownInlineInjections },
};

export class RefusedUrl extends Error {}

/**
 * The URL `url` names, if it is one the HUD ships (the runtime or a grammar,
 * on this origin); otherwise throws `RefusedUrl`. Pure: the gate itself.
 */
export function shippedUrl(url: string, base: string, map: readonly string[] = [RUNTIME_URL, ...Object.values(WASM_URLS)]): string {
  let asked: URL;
  try {
    asked = new URL(url, base);
  } catch {
    throw new RefusedUrl(`not a grammar the HUD ships: ${String(url).slice(0, 120)}`);
  }
  const origin = new URL(base).origin;
  for (const known of map) {
    const k = new URL(known, base);
    if (k.origin === origin && asked.origin === origin && asked.href === k.href) return k.href;
  }
  throw new RefusedUrl(`not a grammar the HUD ships: ${asked.href.slice(0, 120)}`);
}

/** The bytes of a shipped `.wasm`; any other URL is refused before a request is made. */
export async function fetchShipped(url: string): Promise<Uint8Array> {
  const href = shippedUrl(url, self.location.href);
  const res = await fetch(href, { credentials: "same-origin", cache: "default" });
  if (!res.ok) throw new Error(`${href}: HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}
