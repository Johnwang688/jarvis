// The File pane's status bar (ED-7, editor plan §2.8): where the cursor is
// (Ln, Col, and how much is selected), the language, the indentation, the
// encoding and line endings the ED-2 read said, and whether the buffer is
// saved, unsaved or saving. Read from the editor's own events — never the
// buffer's text per keystroke.

import { useEffect, useState } from "react";
import type { BufferView } from "../state/buffers";

interface Place {
  line: number;
  col: number;
  /** Characters selected, across every selection. */
  chars: number;
  /** How many selections (more than one: multi-cursor). */
  cursors: number;
}

const LANGUAGE_NAMES: Record<string, string> = {
  cpp: "C++", c: "C", python: "Python", javascript: "JavaScript", typescript: "TypeScript",
  markdown: "Markdown", json: "JSON", ini: "INI", shell: "Shell", plaintext: "Plain Text",
  dotenv: "Env", html: "HTML", css: "CSS", yaml: "YAML", xml: "XML", rust: "Rust", java: "Java",
  go: "Go", sql: "SQL", dockerfile: "Dockerfile", makefile: "Makefile", lua: "Lua",
};

export function languageName(id: string | null | undefined): string {
  if (!id) return "";
  return LANGUAGE_NAMES[id] ?? id;
}

function placeOf(ed: any): Place | null {
  const pos = ed?.getPosition?.();
  if (!pos) return null;
  const model = ed.getModel?.();
  const sels: any[] = ed.getSelections?.() ?? [];
  let chars = 0;
  for (const s of sels) {
    if (s && model && !s.isEmpty?.()) chars += model.getValueLengthInRange?.(s) ?? 0;
  }
  return { line: pos.lineNumber, col: pos.column, chars, cursors: Math.max(1, sels.length) };
}

function indentOf(ed: any): string {
  const opts = ed?.getModel?.()?.getOptions?.();
  if (!opts) return "";
  return opts.insertSpaces ? `Spaces: ${opts.indentSize ?? opts.tabSize}` : `Tab Size: ${opts.tabSize}`;
}

export function EditorStatus(props: { view: BufferView; editor: any | null }) {
  const v = props.view;
  const ed = props.editor;
  const [place, setPlace] = useState<Place | null>(() => placeOf(ed));
  const [indent, setIndent] = useState(() => indentOf(ed));
  useEffect(() => {
    if (!ed) {
      setPlace(null);
      return;
    }
    const update = () => {
      setPlace(placeOf(ed));
      setIndent(indentOf(ed));
    };
    update();
    const subs = [
      ed.onDidChangeCursorSelection?.(update),
      ed.onDidChangeModel?.(update),
      ed.onDidChangeModelOptions?.(update),
    ].filter(Boolean);
    return () => subs.forEach((s: any) => s.dispose());
  }, [ed, v.abs]);

  const writes = v.modelEol || "LF";
  const eol = v.eol === "mixed" ? `mixed (saving writes ${writes} throughout)` : v.eol || writes;
  const state = v.conflict ? (v.conflict.state === "gone" ? "deleted on disk" : "changed on disk")
    : v.dirty ? "● unsaved" : v.editable ? "saved" : "read-only";
  return (
    <div className="edstatus" data-testid="editor-status" data-eol={v.eol ?? ""} data-state={state}>
      {place ? (
        <span data-testid="status-pos">
          Ln {place.line}, Col {place.col}
          {place.cursors > 1 ? ` · ${place.cursors} selections` : ""}
          {place.chars ? ` (${place.chars} selected)` : ""}
        </span>
      ) : null}
      <span data-testid="status-state" className={v.dirty ? "warnish" : undefined}>{state}</span>
      {/* The registry's save state (ED-2): a write on its way (a slow disk: /mnt/c),
          and a second Ctrl+S waiting for it — the one follow-up save. */}
      {v.saving ? (
        <span data-testid="editor-saving" role="status" data-queued={String(!!v.queued)}>
          {v.queued ? "saving… then again" : "saving…"}
        </span>
      ) : null}
      <span className="grow" />
      {indent ? <span data-testid="status-indent">{indent}</span> : null}
      <span data-testid="status-enc">{v.encoding === "utf-8-bom" ? "UTF-8 with BOM" : v.encoding ? "UTF-8" : "not text"}</span>
      <span data-testid="editor-eol" className={v.eol === "mixed" ? "warnish" : undefined}>{eol}</span>
      <span data-testid="status-lang">{languageName(v.language)}</span>
      {v.kind === "env" ? <span>env file · only you can read it</span> : null}
    </div>
  );
}
