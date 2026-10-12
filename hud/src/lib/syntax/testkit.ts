// The syntax tests' kit (ED-5): the pinned web-tree-sitter and the bundled
// grammars and queries, loaded from disk under Node, exactly as built — the
// same files the worker fetches in the window. Not imported by the HUD.

import { existsSync, readFileSync } from "node:fs";
import { Language, Parser, Query } from "web-tree-sitter";
import { SyntaxEngine, type EngineOptions, type GrammarAssets } from "./engine";
import { GRAMMARS, type GrammarName } from "./grammars";

/** `hud/`, as a filesystem path ending in `/`. */
export const HUD = decodeURIComponent(new URL("../../../", import.meta.url).pathname);

export const read = (rel: string) => readFileSync(HUD + rel, "utf8");
export const bytes = (rel: string) => readFileSync(HUD + rel);
export const exists = (rel: string) => existsSync(HUD + rel);

let init: Promise<void> | null = null;
export const runtime = () => (init ??= Parser.init());

const languages = new Map<GrammarName, Promise<Language>>();
export function language(g: GrammarName): Promise<Language> {
  let p = languages.get(g);
  if (!p) {
    p = runtime().then(() => Language.load(HUD + GRAMMARS[g].wasm));
    languages.set(g, p);
  }
  return p;
}

export const bundledQuery = (g: GrammarName, kind: "highlights" | "injections") => read(`src/syntax/queries/${g}/${kind}.scm`);

export async function assets(g: GrammarName): Promise<GrammarAssets> {
  const lang = await language(g);
  const inj = bundledQuery(g, "injections");
  return {
    language: lang,
    highlights: new Query(lang, bundledQuery(g, "highlights")),
    injections: /\(/.test(inj.replace(/;[^\n]*/g, "")) ? new Query(lang, inj) : null,
  };
}

/** An engine on the bundled grammars and queries. */
export async function nodeEngine(opts: Partial<EngineOptions> = {}): Promise<SyntaxEngine> {
  await runtime();
  return new SyntaxEngine({ load: assets, ...opts });
}
