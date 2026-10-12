// Quick open (Ctrl+P) and the command palette (Ctrl+Shift+P, F1): ED-7,
// editor plan §2.8 and §2.10. Both are one box over a filtered list:
// type to filter (fuzzily, lib/fuzzy.ts), ↑/↓ to move, Enter to take,
// Escape or a click outside to close.
//
// They are pickers like the others (`state.picker`): push-to-talk refuses
// while one is open, the microphone does not claim speech under one, and
// **under an approval card they close** (App) — and they sit beside the card's
// veil, so it makes them inert in the meantime. Space types a space: nothing
// here takes it (push-to-talk is Ctrl+D since PR #40).

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import { fuzzyFilter, splitLine } from "../lib/fuzzy";
import { filterEntries, type Entry } from "../lib/commands";
import type { FileList } from "../types";

/** The text of `label`, the characters at `positions` bold: plain text nodes, never markup. */
function marked(label: string, positions: readonly number[], offset = 0): ReactNode {
  if (!positions.length) return label;
  const at = new Set(positions.map((p) => p - offset));
  const out: ReactNode[] = [];
  let run = "";
  let bold = false;
  for (let i = 0; i < label.length; i++) {
    const b = at.has(i);
    if (b !== bold && run) {
      out.push(bold ? <b key={i}>{run}</b> : run);
      run = "";
    }
    bold = b;
    run += label[i];
  }
  if (run) out.push(bold ? <b key="end">{run}</b> : run);
  return <>{out}</>;
}

interface Row {
  key: string;
  main: ReactNode;
  detail?: ReactNode;
  keys?: string;
  title?: string;
}

function PickList(props: {
  testid: string;
  placeholder: string;
  query: string;
  onQuery: (q: string) => void;
  rows: Row[];
  note?: string | null;
  onPick: (index: number) => void;
  onClose: () => void;
  /** Focus left the box for something else on the page: close, giving nothing focus back. */
  onLeave?: () => void;
}) {
  const [sel, setSel] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const onLeave = useRef(props.onLeave);
  onLeave.current = props.onLeave;
  useEffect(() => input.current?.focus(), []);
  useEffect(() => setSel(0), [props.query]);
  const n = props.rows.length;
  const at = Math.min(sel, Math.max(0, n - 1));
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-index="${at}"]`)?.scrollIntoView({ block: "nearest" });
  }, [at]);
  return (
    <div className="pickerveil quickveil" data-testid={props.testid} onMouseDown={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker quick" role="dialog" aria-label={props.placeholder} ref={box}>
        <input
          ref={input}
          className="quickinput"
          data-testid={`${props.testid}-input`}
          placeholder={props.placeholder}
          value={props.query}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => props.onQuery(e.target.value)}
          // Like VS Code's quick input, it closes when focus moves to
          // something else on the page (a click into a field, a pane taking
          // focus): a box left open over the window would sit on top of what
          // the owner went back to. The window losing focus keeps it.
          onBlur={() => setTimeout(() => {
            if (!document.hasFocus()) return;
            const at = document.activeElement;
            if (!at || at === document.body || box.current?.contains(at)) return;
            onLeave.current?.();
          }, 0)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              if (n) setSel((at + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
            } else if (e.key === "PageDown" || e.key === "PageUp") {
              e.preventDefault();
              setSel(Math.max(0, Math.min(n - 1, at + (e.key === "PageDown" ? 10 : -10))));
            } else if (e.key === "Enter") {
              e.preventDefault();
              if (!e.nativeEvent.isComposing) props.onPick(at);
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              props.onClose();
            }
          }}
        />
        {props.note ? <div className="quicknote muted small" data-testid={`${props.testid}-note`}>{props.note}</div> : null}
        <div className="rows" ref={list} role="listbox">
          {props.rows.map((r, i) => (
            <div
              key={r.key}
              data-index={i}
              data-key={r.key}
              role="option"
              aria-selected={i === at}
              className={"prow" + (i === at ? " sel" : "")}
              data-testid={`${props.testid}-row`}
              title={r.title}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => props.onPick(i)}
            >
              <span className="qmain">{r.main}</span>
              {r.detail ? <span className="qdetail">{r.detail}</span> : null}
              {r.keys ? <span className="sub">{r.keys}</span> : null}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// One listing per project for a little while: opening quick open twice in a
// row does not walk the project twice.
const LISTING_MS = 15_000;
const listings = new Map<string, { at: number; list: Promise<FileList> }>();

/** Forget a project's listing (a file was made, renamed or deleted). */
export function forgetListing(projectId?: string) {
  if (projectId) listings.delete(projectId);
  else listings.clear();
}

function listing(projectId: string): Promise<FileList> {
  const hit = listings.get(projectId);
  if (hit && Date.now() - hit.at < LISTING_MS) return hit.list;
  const list = api.listFiles(projectId);
  listings.set(projectId, { at: Date.now(), list });
  list.catch(() => listings.delete(projectId));
  return list;
}

export function QuickOpen(props: {
  projectId: string;
  projectName: string;
  /** The file open where it will land: `:42` alone goes to line 42 there. */
  activePath: string | null;
  onOpen: (path: string, line: number | null, col: number | null) => void;
  onClose: () => void;
  onLeave?: () => void;
}) {
  const [query, setQuery] = useState("");
  const [list, setList] = useState<FileList | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    listing(props.projectId).then(
      (l) => alive && setList(l),
      (e) => alive && setErr(e?.message || "could not list the files"),
    );
    return () => {
      alive = false;
    };
  }, [props.projectId]);

  const where = splitLine(query.trim());
  const env = useMemo(() => new Set(list?.env ?? []), [list]);
  const ranked = useMemo(
    () => (list && where.query ? fuzzyFilter(where.query, list.files, (p) => p, 200) : []),
    [list, where.query],
  );
  const rows: Row[] = [];
  const picks: string[] = [];
  if (!where.query && where.line !== null && props.activePath) {
    rows.push({ key: ":line", main: `Go to line ${where.line}${where.col ? `, column ${where.col}` : ""}`,
                detail: props.activePath });
    picks.push(props.activePath);
  }
  const shown = where.query ? ranked : (list?.files ?? []).slice(0, 200).map((item) => ({ item, positions: [] as number[] }));
  for (const r of shown) {
    const path = r.item;
    const slash = path.lastIndexOf("/");
    rows.push({
      key: path,
      main: <>{env.has(path) ? <span className="qlock" title="Jarvis can't read this file">🔒 </span> : null}
        {marked(path.slice(slash + 1), r.positions, slash + 1)}</>,
      detail: slash > 0 ? marked(path.slice(0, slash), r.positions.filter((p) => p < slash)) : undefined,
      title: path,
    });
    picks.push(path);
  }
  const note = err ? err
    : !list ? "Listing the files…"
    : list.truncated ? `${props.projectName}: the first ${list.files.length.toLocaleString()} files (a bound was reached)`
    : where.query && !ranked.length ? "No file matches."
    : null;
  return (
    <PickList
      testid="quickopen"
      placeholder={`Go to a file in ${props.projectName} (add :line to jump)`}
      query={query}
      onQuery={setQuery}
      rows={rows}
      note={note}
      onClose={props.onClose}
      onLeave={props.onLeave}
      onPick={(i) => {
        const path = picks[i];
        if (path) props.onOpen(path, where.line, where.col);
      }}
    />
  );
}

export function Palette(props: {
  entries: Entry[];
  onRun: (key: string) => void;
  onClose: () => void;
  onLeave?: () => void;
}) {
  const [query, setQuery] = useState("");
  const ranked = useMemo(() => filterEntries(query, props.entries), [query, props.entries]);
  return (
    <PickList
      testid="palette"
      placeholder="Run a command"
      query={query}
      onQuery={setQuery}
      rows={ranked.map((r) => ({
        key: r.item.key,
        main: marked(r.item.label, r.positions),
        detail: r.item.source === "editor" ? "editor" : undefined,
        keys: r.item.keys,
      }))}
      note={ranked.length ? null : "No command matches."}
      onClose={props.onClose}
      onLeave={props.onLeave}
      onPick={(i) => {
        const e = ranked[i];
        if (e) props.onRun(e.item.key);
      }}
    />
  );
}
