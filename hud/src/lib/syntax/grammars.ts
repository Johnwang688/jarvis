// The grammars tree-sitter colours with (ED-5, editor plan §2.6), and which
// Monaco language and which injected language each one serves. Pure data: the
// syntax worker, the client, the vendoring script and the tests all read it,
// and nothing here imports a file, so Node can read it too.
//
// Every grammar is MIT-licensed. Its `.wasm` comes from one of two places:
//  - an npm package pinned exactly in package.json, whose tarball the
//    lockfile's integrity hash fixes;
//  - for Markdown (its npm package ships no `.wasm`), the grammar's own
//    release assets, vendored under `vendor/tree-sitter/`.
// Either way `vendor/tree-sitter/manifest.json` holds each file's SHA-256, and
// a test fails if a file under it ever differs.
//
// C has no grammar of its own: the C++ grammar parses the C you write well
// enough to colour it (plan §2.6), and nvim's C++ queries inherit its C ones.

export interface Grammar {
  /** The `.wasm`, relative to `hud/`. */
  readonly wasm: string;
  /** Where it comes from: `npm:<package>@<version>` or `release:<repo>@<tag>`. */
  readonly source: string;
  /** nvim-treesitter's query directory for it (`runtime/queries/<nvim>/`). */
  readonly nvim: string;
}

export const GRAMMARS = {
  cpp: { wasm: "node_modules/tree-sitter-cpp/tree-sitter-cpp.wasm", source: "npm:tree-sitter-cpp@0.23.4", nvim: "cpp" },
  python: { wasm: "node_modules/tree-sitter-python/tree-sitter-python.wasm", source: "npm:tree-sitter-python@0.25.0", nvim: "python" },
  javascript: { wasm: "node_modules/tree-sitter-javascript/tree-sitter-javascript.wasm", source: "npm:tree-sitter-javascript@0.25.0", nvim: "javascript" },
  typescript: { wasm: "node_modules/tree-sitter-typescript/tree-sitter-typescript.wasm", source: "npm:tree-sitter-typescript@0.23.2", nvim: "typescript" },
  tsx: { wasm: "node_modules/tree-sitter-typescript/tree-sitter-tsx.wasm", source: "npm:tree-sitter-typescript@0.23.2", nvim: "tsx" },
  json: { wasm: "node_modules/tree-sitter-json/tree-sitter-json.wasm", source: "npm:tree-sitter-json@0.24.8", nvim: "json" },
  bash: { wasm: "node_modules/tree-sitter-bash/tree-sitter-bash.wasm", source: "npm:tree-sitter-bash@0.25.1", nvim: "bash" },
  css: { wasm: "node_modules/tree-sitter-css/tree-sitter-css.wasm", source: "npm:tree-sitter-css@0.25.0", nvim: "css" },
  html: { wasm: "node_modules/tree-sitter-html/tree-sitter-html.wasm", source: "npm:tree-sitter-html@0.23.2", nvim: "html" },
  yaml: {
    wasm: "node_modules/@tree-sitter-grammars/tree-sitter-yaml/tree-sitter-yaml.wasm",
    source: "npm:@tree-sitter-grammars/tree-sitter-yaml@0.7.1", nvim: "yaml",
  },
  toml: {
    wasm: "node_modules/@tree-sitter-grammars/tree-sitter-toml/tree-sitter-toml.wasm",
    source: "npm:@tree-sitter-grammars/tree-sitter-toml@0.7.0", nvim: "toml",
  },
  markdown: {
    wasm: "vendor/tree-sitter/tree-sitter-markdown.wasm",
    source: "release:tree-sitter-grammars/tree-sitter-markdown@v0.5.3", nvim: "markdown",
  },
  markdown_inline: {
    wasm: "vendor/tree-sitter/tree-sitter-markdown_inline.wasm",
    source: "release:tree-sitter-grammars/tree-sitter-markdown@v0.5.3", nvim: "markdown_inline",
  },
} as const satisfies Record<string, Grammar>;

export type GrammarName = keyof typeof GRAMMARS;

export const GRAMMAR_NAMES = Object.keys(GRAMMARS) as GrammarName[];

/** The runtime itself: web-tree-sitter's own `.wasm`. */
export const RUNTIME = { wasm: "node_modules/web-tree-sitter/web-tree-sitter.wasm", source: "npm:web-tree-sitter@0.27.0" } as const;

/**
 * Monaco's language id → the grammar that colours it. A language not here
 * keeps Monarch's colour alone (Go, Rust, SQL …, plan §2.6). `.tsx` is
 * TypeScript to Monaco and its own grammar here (`grammarFor`).
 */
export const FOR_LANGUAGE: Readonly<Record<string, GrammarName>> = {
  cpp: "cpp",
  c: "cpp",
  python: "python",
  javascript: "javascript",
  typescript: "typescript",
  json: "json",
  shell: "bash",
  css: "css",
  html: "html",
  yaml: "yaml",
  toml: "toml",
  markdown: "markdown",
};

/** The Monaco languages tree-sitter colours: the semantic tokens legend's modifiers. */
export const COVERED_LANGUAGES: readonly string[] = Object.keys(FOR_LANGUAGE);

/** The grammar for a model, from its Monaco language and its path. */
export function grammarFor(languageId: string, path = ""): GrammarName | null {
  if (languageId === "typescript" && /\.tsx$/i.test(path)) return "tsx";
  return FOR_LANGUAGE[languageId] ?? null;
}

/**
 * The Monaco language an injected grammar's tokens are styled as: their
 * semantic token modifier, so a per-language rule (`property.json`) holds
 * inside a Markdown fence too.
 */
export const STYLED_AS: Readonly<Record<GrammarName, string>> = {
  cpp: "cpp",
  python: "python",
  javascript: "javascript",
  typescript: "typescript",
  tsx: "typescript",
  json: "json",
  bash: "shell",
  css: "css",
  html: "html",
  yaml: "yaml",
  toml: "toml",
  markdown: "markdown",
  markdown_inline: "markdown",
};

/**
 * An injection's language name — a Markdown fence's info string, or a query's
 * `#set! injection.language` — as a grammar, or null for one not shipped
 * (which then keeps its host's colour: a fence stays `@markup.raw.block`).
 */
const ALIASES: Readonly<Record<string, GrammarName>> = {
  c: "cpp", h: "cpp", cpp: "cpp", "c++": "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", cuda: "cpp", arduino: "cpp",
  python: "python", py: "python", python3: "python", py3: "python", gyp: "python",
  javascript: "javascript", js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript", node: "javascript",
  typescript: "typescript", ts: "typescript", mts: "typescript", cts: "typescript",
  tsx: "tsx",
  json: "json", jsonc: "json", json5: "json",
  bash: "bash", sh: "bash", shell: "bash", zsh: "bash", ksh: "bash", console: "bash", shellsession: "bash",
  css: "css",
  html: "html", htm: "html", xhtml: "html",
  yaml: "yaml", yml: "yaml",
  toml: "toml",
  markdown: "markdown", md: "markdown",
  markdown_inline: "markdown_inline",
};

export function injectionGrammar(name: string): GrammarName | null {
  const key = name.trim().toLowerCase().replace(/^\{?\.?/, "").replace(/\}$/, "");
  return ALIASES[key] ?? null;
}
