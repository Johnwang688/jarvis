// Monaco, loaded on demand. The File and Diff tabs are the only callers, so a
// window that never opens them never pays for the editor.
//
// What is loaded is a whole editor without intellisense (editor plan §2.2,
// ED-1): Monaco 0.57's own editing features and its Monarch languages, chosen
// in `monacoBundle.ts`, with no language-service worker of any kind
// (decisions E-1). The one worker is `editor.worker`, for word suggestions,
// diffs and links.
//
// Workers are wired through Vite's `?worker` imports rather than a CDN: the
// HUD is served from the daemon on loopback and must work with no network at
// all, which is also what keeps an agent-written page from being a script
// source for the window that gates approvals.

import type * as MonacoNS from "monaco-editor/editor";

export type Monaco = typeof MonacoNS;

let loading: Promise<Monaco> | null = null;
let loaded: Monaco | null = null;

// ---- colours --------------------------------------------------------------

/**
 * The editor's colours until ED-3's Graphite scheme replaces them
 * (decisions E-4: no syntax colour may be amber or red). ED-1 is what first
 * tokenizes anything, so vs-dark's token colours finally show — and several
 * of them are exactly what E-4 rules out: strings and attribute values in
 * #ce9178 (salmon), `invalid` and `string.sql` in red, `annotation` and
 * `metatag` in red. Those, and only those, are moved onto the sibling hues the
 * plan's scheme uses (§2.5); everything else stays vs-dark's until ED-3.
 */
export const INTERIM_RULES: { token: string; foreground?: string; fontStyle?: string }[] = [
  { token: "string", foreground: "a6c79b" },
  { token: "string.sql", foreground: "a6c79b" },
  { token: "string.value.json", foreground: "a6c79b" },
  { token: "attribute.value", foreground: "a6c79b" },
  { token: "meta.tag", foreground: "a6c79b" },
  { token: "keyword.json", foreground: "b9aade" },
  { token: "metatag", foreground: "8ec5d6" },
  { token: "annotation", foreground: "a8b5c2" },
  { token: "invalid", foreground: "e2dfd7" },
];

/**
 * Graphite chrome. vs-dark's own was drawn for #1e1e1e, so it sits naturally
 * on the pane colour; what moves is the chrome, and — now that ED-1 loads find,
 * bracket-pair colours and the unicode highlighter — every chrome colour Monaco
 * defaults to orange, gold or amber (find matches, the first bracket pair,
 * the unicode border, warnings). Amber is the HUD's "waiting on you" and red
 * its error; the editor may not borrow either.
 */
export const CHROME: Record<string, string> = {
  "editor.background": "#1a1a19",
  "editorGutter.background": "#1a1a19",
  "editorLineNumber.foreground": "#88847b",
  "editorLineNumber.activeForeground": "#b4b0a8",
  "editor.lineHighlightBackground": "#222221",
  "editor.lineHighlightBorder": "#00000000",
  "editor.selectionBackground": "#6fc3df40",
  "editor.inactiveSelectionBackground": "#6fc3df22",
  "editorCursor.foreground": "#9ad6ea",
  "editorIndentGuide.background1": "#2c2c2a",
  "editorIndentGuide.activeBackground1": "#46453f",
  "editorWidget.background": "#222221",
  "editorWidget.border": "#46453f",
  "scrollbarSlider.background": "#ffffff14",
  "scrollbarSlider.hoverBackground": "#ffffff22",
  "scrollbarSlider.activeBackground": "#ffffff30",
  // Find: Monaco's other-match colour is orange.
  "editor.findMatchBackground": "#6fc3df59",
  "editor.findMatchBorder": "#6fc3df",
  "editor.findMatchHighlightBackground": "#6fc3df26",
  "editorOverviewRuler.findMatchForeground": "#6fc3df99",
  // Bracket pairs: Monaco's first colour is gold, which reads as amber, and
  // an unmatched bracket is red.
  "editorBracketHighlight.foreground1": "#8ec5d6",
  "editorBracketHighlight.foreground2": "#b9aade",
  "editorBracketHighlight.foreground3": "#a8b5c2",
  "editorBracketHighlight.unexpectedBracket.foreground": "#e2dfd7",
  "editorBracketMatch.border": "#98948b",
  // The unicode highlighter's box is amber-ish by default.
  "editorUnicodeHighlight.border": "#b9aade",
  // Nothing raises a warning today (no language service); should anything,
  // it is never painted amber.
  "editorWarning.foreground": "#b4b0a8",
  "editorOverviewRuler.warningForeground": "#b4b0a8",
  // Red here means "removed", the universal diff convention; the low alpha
  // keeps it from reading as an error.
  "diffEditor.insertedTextBackground": "#6dd08f26",
  "diffEditor.removedTextBackground": "#f8717126",
  "diffEditor.insertedLineBackground": "#6dd08f12",
  "diffEditor.removedLineBackground": "#f8717112",
  "diffEditor.diagonalFill": "#2c2c2a",
};

// ---- languages and links --------------------------------------------------

function registerExtras(monaco: Monaco) {
  // VEX headers are C++ (`robot-config.h`, `vex.h`): Monaco gives `.h` to C.
  // The last registration of an extension wins.
  monaco.languages.register({ id: "cpp", extensions: [".h"] });
  // A script with no extension is known by its shebang: Python's definition
  // says so already, the shell's does not.
  monaco.languages.register({ id: "shell", firstLine: "^#!.*\\b(?:ba|da|k|z)?sh\\b", filenames: [".bashrc", ".profile", ".bash_profile"] });
  // TOML has no Monarch definition; INI's tokenizer and `#` comments fit it
  // (sections, keys, strings) until tree-sitter colours it (ED-5).
  monaco.languages.register({ id: "toml", extensions: [".toml"], aliases: ["TOML", "toml"] });
  monaco.languages.registerTokensProviderFactory("toml", {
    create: async () => (await import("monaco-editor/languages/definitions/ini/ini")).language as any,
  });
  monaco.languages.onLanguageEncountered("toml", async () => {
    const ini = await import("monaco-editor/languages/definitions/ini/ini");
    monaco.languages.setLanguageConfiguration("toml", ini.conf as any);
  });
  // A credential env file (ED-2, the owner's `.env*` rule) gets a language of
  // its own, set on its model and never detected: Monaco's word suggestions
  // draw only on documents of the same language, so a key in `.env.production`
  // is never offered while typing in a file Jarvis can read. INI's tokenizer
  // fits `KEY=value` and `#` comments.
  monaco.languages.register({ id: ENV_LANGUAGE, aliases: ["dotenv"] });
  monaco.languages.registerTokensProviderFactory(ENV_LANGUAGE, {
    create: async () => (await import("monaco-editor/languages/definitions/ini/ini")).language as any,
  });
  // JSON's Monaco language came with its language service, which E-1 leaves
  // out — so without this a `.json` file would be plain text. A small Monarch
  // definition: strings, numbers, the three literals, `//` and `/* */`
  // comments (jsonc), brackets and auto-close.
  monaco.languages.register({
    id: "json", extensions: [".json", ".jsonc", ".code-workspace", ".babelrc"], filenames: [".prettierrc"],
    aliases: ["JSON", "json"], mimetypes: ["application/json"],
  });
  monaco.languages.setMonarchTokensProvider("json", JSON_MONARCH as any);
  monaco.languages.setLanguageConfiguration("json", {
    comments: { lineComment: "//", blockComment: ["/*", "*/"] },
    brackets: [["{", "}"], ["[", "]"]],
    autoClosingPairs: [
      { open: "{", close: "}", notIn: ["string"] },
      { open: "[", close: "]", notIn: ["string"] },
      { open: '"', close: '"', notIn: ["string"] },
    ],
  });

  // Links open only http and https, in a new window that cannot reach this
  // one (noopener). Everything else — a `file:` link, `command:`, `mailto:`,
  // a scheme nobody thought of — is answered "handled" and opens nothing:
  // Monaco's own fallback for a non-http link is to navigate the window, and
  // this window is the one that gates approvals. (`file:` links inside the
  // project opening in the editor come with ED-2's buffers.)
  monaco.editor.registerLinkOpener({
    open(resource) {
      if (resource.scheme === "http" || resource.scheme === "https") {
        window.open(resource.toString(true), "_blank", "noopener,noreferrer");
      }
      return true;
    },
  });
}

const JSON_MONARCH = {
  defaultToken: "",
  tokenPostfix: ".json",
  tokenizer: {
    root: [
      [/"(?:[^"\\]|\\.)*"(?=\s*:)/, "string.key.json"],
      [/"(?:[^"\\]|\\.)*"/, "string.value.json"],
      [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, "number"],
      [/\b(?:true|false|null)\b/, "keyword.json"],
      [/\/\/.*$/, "comment"],
      [/\/\*/, "comment", "@comment"],
      [/[{}[\]]/, "delimiter.bracket"],
      [/[,:]/, "delimiter"],
    ],
    comment: [
      [/[^/*]+/, "comment"],
      [/\*\//, "comment", "@pop"],
      [/[/*]/, "comment"],
    ],
  },
};

// ---- loading --------------------------------------------------------------

export function loadMonaco(): Promise<Monaco> {
  if (!loading) {
    loading = (async () => {
      const [{ monaco }, EditorWorker] = await Promise.all([
        import("./monacoBundle"),
        import("monaco-editor/editor/editor.worker?worker"),
      ]);
      // The one worker. Asked for any other label (a language service's),
      // there is none to give: E-1 ships no language workers at all.
      (self as any).MonacoEnvironment = {
        getWorker: (_id: string, label: string) => {
          if (label && label !== "editorWorkerService") throw new Error(`no ${label} worker in the HUD`);
          return new (EditorWorker.default as any)();
        },
      };
      monaco.editor.defineTheme("jarvis", {
        base: "vs-dark",
        inherit: true,
        rules: INTERIM_RULES,
        colors: CHROME,
      });
      registerExtras(monaco as unknown as Monaco);
      loaded = monaco as unknown as Monaco;
      return loaded;
    })();
  }
  return loading;
}

export const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path);

// ---- models ---------------------------------------------------------------

let serial = 0;

/** The language an env file's model is given (and nothing else is). */
export const ENV_LANGUAGE = "dotenv";

/**
 * A buffer's model (ED-2, `state/buffers.ts`): one per file on disk, its URI
 * the file's absolute path, so Monaco's registry picks the language from the
 * extension, the file name or a shebang line. An env file is `dotenv`.
 */
export async function makeModel(text: string, abs: string, kind: string) {
  const monaco = await loadMonaco();
  const uri = abs.startsWith("/")
    ? monaco.Uri.file(abs)
    : monaco.Uri.from({ scheme: "jarvis-buffer", path: `/${abs}` });
  // One buffer per file, so a model at this URI is a leftover: replace it.
  monaco.editor.getModel(uri)?.dispose();
  return monaco.editor.createModel(text, kind === "env" ? ENV_LANGUAGE : undefined, uri);
}

/** A diff side's model URI. Its own scheme, so a leak is countable. */
export function diffUri(monaco: Monaco, path: string, side: "original" | "modified") {
  return monaco.Uri.from({ scheme: "jarvis-diff", path: `/${path}`, query: `${side}=${++serial}` });
}

// ---- the card --------------------------------------------------------------

// While an approval card is up every editor goes read-only (as WP-D's
// terminals stop taking input); the card itself takes focus and makes the
// rest of the window inert, so Escape is the card's and a stray `y⏎` lands
// nowhere. App says so from above the workspace's error boundary.
let blocked = false;
const holds = new Set<(blocked: boolean) => void>();

export function setEditorsBlocked(value: boolean) {
  if (blocked === value) return;
  blocked = value;
  for (const f of holds) f(value);
}

export const editorsBlocked = () => blocked;

/** Told every change; returns the unsubscribe. */
export function onEditorsBlocked(f: (blocked: boolean) => void): () => void {
  holds.add(f);
  return () => holds.delete(f);
}

// ---- test hooks (window.__hud) --------------------------------------------

/** Every live model's URI. Empty until Monaco has loaded. */
export function monacoModels(): string[] {
  return loaded ? loaded.editor.getModels().map((m) => m.uri.toString()) : [];
}

/** Every live code editor (a diff editor's two included): read-only, focused. */
export function monacoEditors(): { uri: string | null; readOnly: boolean; focused: boolean }[] {
  const m = loaded;
  if (!m) return [];
  return m.editor.getEditors().map((ed) => ({
    uri: ed.getModel()?.uri.toString() ?? null,
    readOnly: !!ed.getOption(m.editor.EditorOption.readOnly),
    focused: ed.hasTextFocus(),
  }));
}

/** The language Monaco's registry gives a path (and first line). */
export function monacoLanguage(path: string, text = ""): string | null {
  const m = loaded;
  if (!m) return null;
  const model = m.editor.createModel(text, undefined, m.Uri.from({ scheme: "file", path: `/probe/${++serial}/${path}` }));
  const id = model.getLanguageId();
  model.dispose();
  return id;
}
