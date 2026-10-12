// Find in files (Ctrl+Shift+F): ED-7, editor plan §2.8. The File pane's
// left column, beside its tree: a query, four toggles (case, word, regular
// expression, respect .gitignore — on by default), a file filter, and the
// matches grouped by file. A click opens the file at the match.
//
// The search is the daemon's (`POST /projects/{id}/search`, owner-only): it
// shares `grep_files`' protected-file filter, so a credential bundle or an
// env file is never searched, never shown; it stops at 2,000 matches and
// 10 s and says so. Refused on the Inbox, which is the whole home folder.
// What the owner types goes to that one route and nowhere else.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import type { SearchMatch, SearchResult } from "../types";

function highlighted(text: string, ranges: readonly [number, number][]): ReactNode {
  if (!ranges.length) return text;
  const out: ReactNode[] = [];
  let at = 0;
  ranges.forEach(([a, z], i) => {
    if (a < at || z > text.length) return;
    if (a > at) out.push(text.slice(at, a));
    out.push(<mark key={i}>{text.slice(a, z)}</mark>);
    at = z;
  });
  if (at < text.length) out.push(text.slice(at));
  return <>{out}</>;
}

export interface SearchAsk {
  nonce: number;
  query?: string;
}

export function SearchView(props: {
  projectId: string;
  /** Ctrl+Shift+F again: focus the box (and take the selection, when given). */
  ask: SearchAsk | null;
  visible: boolean;
  blocked?: boolean;
  onOpen: (path: string, line: number, col: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [glob, setGlob] = useState("");
  const [flags, setFlags] = useState({ case: false, word: false, regex: false, ignore: true });
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [folded, setFolded] = useState<Record<string, boolean>>({});
  const box = useRef<HTMLInputElement>(null);
  const seq = useRef(0);

  useEffect(() => {
    if (!props.ask) return;
    if (props.ask.query) setQuery(props.ask.query);
    requestAnimationFrame(() => {
      box.current?.focus();
      box.current?.select();
    });
  }, [props.ask?.nonce]);

  const run = (q = query) => {
    const mine = ++seq.current;
    if (!q) {
      setResult(null);
      setErr("");
      return;
    }
    setBusy(true);
    setErr("");
    api.searchFiles(props.projectId, { query: q, glob: glob.trim() || undefined, ...flags }).then(
      (r) => {
        if (mine !== seq.current) return;
        setResult(r);
        setBusy(false);
      },
      (e: any) => {
        if (mine !== seq.current) return;
        setResult(null);
        setErr(e?.message || "the search failed");
        setBusy(false);
      },
    );
  };

  // A little after the last keystroke, and at once when a toggle changes.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const t = setTimeout(() => run(), query ? 350 : 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, glob, flags]);

  const groups = useMemo(() => {
    const out = new Map<string, SearchMatch[]>();
    for (const m of result?.matches ?? []) {
      const list = out.get(m.path);
      if (list) list.push(m);
      else out.set(m.path, [m]);
    }
    return [...out];
  }, [result]);

  const toggle = (key: keyof typeof flags, label: string, title: string) => (
    <button
      type="button"
      className={"stoggle" + (flags[key] ? " on" : "")}
      aria-pressed={flags[key]}
      data-testid={`search-${key}`}
      title={title}
      disabled={props.blocked}
      onClick={() => setFlags((f) => ({ ...f, [key]: !f[key] }))}
    >
      {label}
    </button>
  );

  const total = result?.matches.length ?? 0;
  return (
    <div className="searchview" data-testid="searchview" style={props.visible ? undefined : { display: "none" }}>
      <div className="searchbar">
        <input
          ref={box}
          data-testid="search-input"
          placeholder="Search the project"
          value={query}
          spellCheck={false}
          autoComplete="off"
          disabled={props.blocked}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              run();
            }
          }}
        />
        <div className="stoggles">
          {toggle("case", "Aa", "Match case")}
          {toggle("word", "ab", "Whole word")}
          {toggle("regex", ".*", "Regular expression")}
          {toggle("ignore", "⊘", "Respect .gitignore (on: ignored files are not searched)")}
        </div>
        <input
          data-testid="search-glob"
          className="sglob"
          placeholder="files to include, e.g. *.cpp"
          value={glob}
          spellCheck={false}
          disabled={props.blocked}
          onChange={(e) => setGlob(e.target.value)}
        />
      </div>
      <div className="searchnote muted small" data-testid="search-note">
        {err ? <span className="err">{err}</span>
          : busy ? "Searching…"
          : result ? `${total.toLocaleString()} match${total === 1 ? "" : "es"} in ${result.files} file${result.files === 1 ? "" : "s"}`
            + (result.truncated ? ` — stopped at ${result.limit.toLocaleString()}; narrow the search` : "")
            + (result.timed_out ? " — stopped after 10 s; narrow the search" : "")
          : ""}
      </div>
      <div className="searchresults" data-testid="search-results">
        {groups.map(([path, matches]) => (
          <div key={path} className="sgroup">
            <div className="sfile" data-testid={`search-file-${path}`} title={path}
                 onClick={() => setFolded((f) => ({ ...f, [path]: !f[path] }))}>
              <span className="tw">{folded[path] ? "▸" : "▾"}</span>
              <span className="nm">{path.slice(path.lastIndexOf("/") + 1)}</span>
              <span className="sdir">{path.lastIndexOf("/") > 0 ? path.slice(0, path.lastIndexOf("/")) : ""}</span>
              <span className="scount">{matches.length}</span>
            </div>
            {folded[path] ? null : matches.map((m) => (
              <div
                key={`${m.line}:${m.ranges[0]?.[0] ?? 0}`}
                className="smatch"
                data-testid={`search-hit-${path}:${m.line}`}
                onClick={() => props.onOpen(path, m.line, (m.ranges[0]?.[0] ?? 0) + 1)}
              >
                <span className="sline">{m.line}</span>
                <span className="stext">{highlighted(m.text, m.ranges)}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
