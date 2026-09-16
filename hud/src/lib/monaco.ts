// Monaco, loaded on demand. The File and Diff tabs are the only callers, so a
// window that never opens them never pays for the editor.
//
// Workers are wired through Vite's `?worker` imports rather than a CDN: the
// HUD is served from the daemon on loopback and must work with no network at
// all, which is also what keeps an agent-written page from being a script
// source for the window that gates approvals.

import type * as MonacoNS from "monaco-editor";

let loading: Promise<typeof MonacoNS> | null = null;

export function loadMonaco(): Promise<typeof MonacoNS> {
  if (!loading) {
    loading = (async () => {
      const [monaco, EditorWorker] = await Promise.all([
        import("monaco-editor/esm/vs/editor/editor.api"),
        import("monaco-editor/esm/vs/editor/editor.worker?worker"),
      ]);
      (self as any).MonacoEnvironment = {
        getWorker: () => new (EditorWorker.default as any)(),
      };
      monaco.editor.defineTheme("jarvis", {
        base: "vs-dark",
        inherit: true,
        rules: [],
        colors: {
          "editor.background": "#050a12",
          "editorGutter.background": "#050a12",
          "editorLineNumber.foreground": "#3c5a78",
        },
      });
      return monaco as unknown as typeof MonacoNS;
    })();
  }
  return loading;
}

/** Monaco's language id for a path, by extension. Unknown means plaintext. */
export function languageFor(path: string): string {
  const ext = (path.split(".").pop() || "").toLowerCase();
  const map: Record<string, string> = {
    ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
    py: "python", json: "json", md: "markdown", markdown: "markdown",
    css: "css", html: "html", yml: "yaml", yaml: "yaml", sh: "shell",
    toml: "ini", ini: "ini", sql: "sql", rs: "rust", go: "go",
  };
  return map[ext] || "plaintext";
}

export const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path);
