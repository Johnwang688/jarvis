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
      const [{ monaco, applyGraphite }, EditorWorker] = await Promise.all([
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
      // The Graphite editor scheme (ED-3): the theme "jarvis", built from the
      // HUD's tokens on :root, and C, C++ and Python's tokenizers refined so
      // its rows show under Monarch (lib/editorTheme.ts, lib/monarchRefine.ts).
      applyGraphite();
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

/**
 * A File view's model URI: `file:///<project>/<path>`, so Monaco's registry
 * picks the language from the extension, the file name or a shebang line.
 * The query keeps two panes holding the same file on two models until ED-2's
 * buffers share one (by absolute path, which the read route does not return
 * yet).
 */
export function fileUri(monaco: Monaco, projectId: string, path: string) {
  return monaco.Uri.from({ scheme: "file", path: `/${projectId}/${path}`, query: `editor=${++serial}` });
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
