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
        // The multi-cursor commands (it brings find with it), so the next-
        // match command has a key to move to (`editorKeyRules`, below).
        import("monaco-editor/esm/vs/editor/contrib/multicursor/browser/multicursor.js"),
      ]);
      monaco.editor.addKeybindingRules(editorKeyRules(monaco as unknown as MonacoKeys));
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

/** The bits of Monaco's API the key rules need: a plain object passes in tests. */
export interface MonacoKeys {
  KeyMod: { CtrlCmd: number; Shift: number; chord(first: number, second: number): number };
  KeyCode: { KeyD: number; KeyK: number };
}

/** Where Monaco's "add selection to next find match" lives now (was Ctrl+D). */
export const NEXT_MATCH_KEY = "Ctrl+Shift+D";

/**
 * Monaco's keys that would fight the HUD's (2026-10-10). Ctrl+D is push-to-
 * talk in the editor too (lib/talkkey.ts: the window takes it in the capture
 * phase, before Monaco sees it), so Monaco's two Ctrl+D commands lose it:
 *
 *   add selection to next find match    Ctrl+D         → Ctrl+Shift+D
 *   move last selection to next match   Ctrl+K Ctrl+D  → unbound
 *
 * Ctrl+Shift+D is bound by nothing in Monaco (D is otherwise only a Mac Ctrl
 * binding) or in the HUD, and reads as "add". **The chord gets no new key**
 * (review of PR #40): any Ctrl+K chord makes Ctrl+K start chord mode, and a
 * Ctrl+K followed by push-to-talk's Ctrl+D — which Monaco never sees — left
 * it waiting, so the next key typed was swallowed as the chord's end. With
 * the chord gone, Ctrl+K starts nothing in the HUD's Monaco (multicursor's
 * was its only Ctrl+K chord). An editor that loads more chords (ED-1) has to
 * decide what the talk key does to a pending one.
 *
 * The old bindings are removed rather than left shadowed, so Monaco's own
 * record of its keys (its menus and command list) says where they went.
 * Ctrl+Shift+D outside the editor is Chrome's "bookmark all tabs"; in the
 * editor Monaco takes it, and its default is prevented.
 */
export function editorKeyRules(m: MonacoKeys): { keybinding: number; command: string; when?: string }[] {
  const ctrlD = m.KeyMod.CtrlCmd | m.KeyCode.KeyD;
  const ctrlShiftD = m.KeyMod.CtrlCmd | m.KeyMod.Shift | m.KeyCode.KeyD;
  const ctrlK = m.KeyMod.CtrlCmd | m.KeyCode.KeyK;
  const add = "editor.action.addSelectionToNextFindMatch";
  const move = "editor.action.moveSelectionToNextFindMatch";
  return [
    { keybinding: ctrlD, command: `-${add}` },
    { keybinding: ctrlShiftD, command: add, when: "editorFocus" },
    { keybinding: m.KeyMod.chord(ctrlK, ctrlD), command: `-${move}` },
  ];
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
