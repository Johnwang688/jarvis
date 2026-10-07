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
          // Graphite. vs-dark's token colours were drawn for #1e1e1e, so they
          // sit naturally on the pane colour; only the chrome moves.
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
          // Red here means "removed", the universal diff convention; the low
          // alpha keeps it from reading as an error.
          "diffEditor.insertedTextBackground": "#6dd08f26",
          "diffEditor.removedTextBackground": "#f8717126",
          "diffEditor.insertedLineBackground": "#6dd08f12",
          "diffEditor.removedLineBackground": "#f8717112",
          "diffEditor.diagonalFill": "#2c2c2a",
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
