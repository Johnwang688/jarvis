// The Diff pane's uncommitted changes (2026-10-12): what a Diff pane shows
// when no task is — the project's working tree against HEAD, like VS Code's
// Source Control view, **read-only** (no staging, reverting or committing
// here). Rules in lib/scm.ts.
//
//  - **The list** is ED-9's `git/status`, through its one poller
//    (state/gitStatus.ts): watched only while the pane is drawn, so a hidden
//    Diff pane asks nothing; it redraws only when the status changed.
//  - **The left side** is HEAD's text from `git/base` (empty for a file HEAD
//    does not hold), in a `jarvis-diff:` model this view makes and disposes.
//  - **The right side** is the file as the editor would show it: **the live
//    buffer's own model** when the file is open (state/buffers.ts — so an
//    unsaved edit shows, and the diff follows typing in the File pane; the
//    model is borrowed, never copied, held or disposed here), else the
//    editor's read route, in a `jarvis-diff:` model of its own. A deletion's
//    right side is empty.
//  - **An env file is listed and never diffed**: no request is made for it
//    at all — not `git/base` (which refuses it anyway), not a read — and its
//    buffer's model is never put in a diff. One the name rule misses (a link
//    named otherwise) is refused by `git/base` before any read.
//  - **Under a card** every control here is disabled (and inert, behind the
//    veil); the diff editor is read-only on both sides whatever happens.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import { buffers } from "../state/buffers";
import { gitStatus, type GitStatusStore } from "../state/gitStatus";
import { diffUri, loadMonaco, type Monaco } from "../lib/monaco";
import {
  absUnder, baseSide, ENV_NOTE, FOLDER_NOTE, readSide, rowShows, scmRows, scmSummary, SCM_WORDS,
  type ScmRow, type Side,
} from "../lib/scm";
import { FileIcon } from "./FileIcon";

/** A project's git status, watched while `projectId` is set (the pane is drawn). */
function useGitStatus(projectId: string | null, store: GitStatusStore = gitStatus) {
  const subscribe = useCallback(
    (fn: () => void) => (projectId ? store.subscribe(projectId, fn) : () => {}),
    [projectId, store],
  );
  const ver = useSyncExternalStore(subscribe, () => (projectId ? store.version(projectId) : 0));
  return {
    ver,
    data: projectId ? store.get(projectId) : null,
    error: projectId ? store.error(projectId) : null,
  };
}

export function ProjectDiff(props: {
  projectId: string;
  /** Where the project's folder resolves (`treeRoot`): an open buffer's `abs` is under it. */
  root: string | null;
  /** Drawn now: the status is polled only then. */
  drawn: boolean;
  narrow?: boolean;
  /** The file the pane had open (a reload, a split's copy). */
  filePath?: string;
  onSelect?: (path: string) => void;
  blocked: boolean;
  /** Open in the editor (ED-7's `placeFile`: never over this view or a VEX view). */
  onOpen: (path: string) => void;
}) {
  const { ver, data, error } = useGitStatus(props.drawn ? props.projectId : null);
  const rows = useMemo(() => scmRows(data), [ver, data]);
  const [sel, setSel] = useState(props.filePath ?? "");
  const [listOpen, setListOpen] = useState(false);
  const [nonce, setNonce] = useState(0);
  // The file the pane had (a reload, a split's copy, another project's pick
  // while following the chat) is kept only if this list has it once it arrives.
  const seeded = useRef(!!props.filePath);
  useEffect(() => {
    if (!seeded.current || !data) return;
    seeded.current = false;
    if (sel && !rows.some((r) => r.path === sel)) setSel("");
  }, [data, rows, sel]);

  const row = rows.find((r) => r.path === sel) ?? null;
  const showList = !props.narrow || listOpen || !sel;
  const pick = (r: ScmRow) => {
    if (props.blocked) return;
    setSel(r.path);
    setListOpen(false);
    // Picking it again asks again: a file not open in the editor is read once.
    setNonce((n) => n + 1);
    props.onSelect?.(r.path);
  };

  return (
    <div className="split" data-testid="scm" data-project={props.projectId}>
      <div className="filetree" data-testid="scmlist" style={showList ? undefined : { display: "none" }}>
        <div className="pad small muted" data-testid="scm-summary">{scmSummary(data, rows, error)}</div>
        <div className="treebody">
          {rows.map((r) => (
            <div
              key={r.path}
              className={"tree-row scmrow" + (sel === r.path ? " sel" : "")}
              data-testid={`scm-${r.path}`}
              data-git={r.state}
              data-env={r.env ? "true" : undefined}
              aria-disabled={props.blocked || undefined}
              title={`${r.path}: ${SCM_WORDS[r.state]}${r.env ? " · an env file, not diffed here" : ""}`}
              onClick={() => pick(r)}
            >
              {r.folder ? <span className="ficon" aria-hidden="true" /> : <FileIcon name={r.path} />}
              <span className="nm">{r.folder ? `${r.path}/` : r.path}</span>
              <span className="scmletter" data-testid={`scm-letter-${r.path}`}>{r.letter}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="editorwrap">
        <div className="toolbar">
          {props.narrow && sel ? (
            <button
              type="button"
              data-testid="tree-toggle"
              aria-pressed={showList}
              disabled={props.blocked}
              title={showList ? "Hide the changed files" : "Show the changed files"}
              onClick={() => setListOpen((o) => !o)}
            >
              Files
            </button>
          ) : null}
          <span className="path" data-testid="scm-path">{sel || "no file selected"}</span>
          {row && row.state !== "deleted" && !row.folder ? (
            <button type="button" data-testid="scm-open" disabled={props.blocked}
                    title="Open this file in the editor" onClick={() => !props.blocked && props.onOpen(row.path)}>
              Open
            </button>
          ) : null}
          {row && rowShows(row) === "diff" ? (
            <button type="button" data-testid="scm-refresh" disabled={props.blocked}
                    title="Read both sides again" onClick={() => !props.blocked && setNonce((n) => n + 1)}>
              ↻
            </button>
          ) : null}
        </div>
        {!sel ? (
          <div className="pad muted">Pick a changed file.</div>
        ) : !row ? (
          <div className="pad muted" data-testid="scm-clean">{sel} has no uncommitted changes now.</div>
        ) : rowShows(row) === "env" ? (
          <Note testid="scm-env" text={ENV_NOTE} />
        ) : rowShows(row) === "folder" ? (
          <Note testid="scm-folder" text={FOLDER_NOTE} />
        ) : (
          <ScmFile
            key={`${props.projectId}|${row.path}`}
            projectId={props.projectId}
            root={props.root}
            row={row}
            head={data?.head ?? null}
            statusVer={ver}
            nonce={nonce}
          />
        )}
      </div>
    </div>
  );
}

function Note(props: { testid: string; text: string }) {
  return <div className="pad muted" data-testid={props.testid}>{props.text}</div>;
}

/** What `git/base` said: HEAD's side, or why there is none. */
type BaseRes =
  | { kind: "side"; side: Side; deleted: boolean }
  | { kind: "env" }
  | { kind: "refused"; text: string }
  | { kind: "gone" }
  | { kind: "error"; text: string };

/** What the editor's read said, for a file no buffer holds. */
type ReadRes =
  | { kind: "text"; text: string; abs: string | null }
  | { kind: "env" }
  | { kind: "note"; text: string };

const READ_NOTES: Record<string, string> = {
  binary: "A binary file: there is no text to compare.",
  withheld: "Withheld: this file's text is never sent to this window.",
};

/** The open buffer for this project file, if one is: by its project and path, or where it resolves. */
function liveAbsOf(projectId: string, path: string, root: string | null, readAbs: string | null): string | null {
  for (const b of buffers.snapshot()) if (b.projectId === projectId && b.path === path) return b.abs;
  const guess = absUnder(root, path);
  if (guess && buffers.get(guess)) return guess;
  if (readAbs && buffers.get(readAbs)) return readAbs;
  return null;
}

/** One changed file's two sides. Mounted only for a row `rowShows` says is a diff. */
function ScmFile(props: {
  projectId: string;
  root: string | null;
  row: ScmRow;
  head: string | null;
  statusVer: number;
  nonce: number;
}) {
  const { projectId, row } = props;
  const [base, setBase] = useState<BaseRes | null>(null);
  const [read, setRead] = useState<ReadRes | null>(null);
  const bufVer = useSyncExternalStore(buffers.subscribe, buffers.version);
  const readAbs = read?.kind === "text" ? read.abs : null;
  const liveAbs = useMemo(
    () => liveAbsOf(projectId, row.path, props.root, readAbs),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bufVer, projectId, row.path, props.root, readAbs],
  );
  const liveView = liveAbs ? buffers.get(liveAbs) : null;
  const liveModel = liveAbs ? (buffers.model(liveAbs) as any) : null;
  const live = !!liveModel && !liveModel.isDisposed?.() && typeof liveModel.pushEditOperations === "function";

  // HEAD's side: asked again when HEAD moves, or on ↻ / a click.
  useEffect(() => {
    let dead = false;
    setBase(null);
    api.gitBase(projectId, row.path).then(
      (b) => {
        if (dead) return;
        setBase({ kind: "side", side: baseSide(b.state, b.text), deleted: !!b.deleted });
      },
      (e: any) => {
        if (dead) return;
        const text = String(e?.message || "refused");
        if (e?.status === 403) setBase(/env file/i.test(text) ? { kind: "env" } : { kind: "refused", text });
        else if (e?.status === 404) setBase({ kind: "gone" });
        else setBase({ kind: "error", text });
      },
    );
    return () => {
      dead = true;
    };
  }, [projectId, row.path, props.head, props.nonce]);

  const deleted = row.state === "deleted" || (base?.kind === "side" && base.deleted);
  const baseOk = base?.kind === "side" && "text" in base.side;
  // The working side, for a file no buffer holds: read only once HEAD's side
  // is known and allowed (a refusal there — an env file by resolution — means
  // no read at all), and again when the project's status moves or on ↻.
  const needRead = baseOk && !deleted && !live;
  useEffect(() => {
    if (!needRead) return;
    let dead = false;
    api.readFile(projectId, row.path).then(
      (r) => {
        if (dead) return;
        const what = readSide(r.kind, !!r.protected);
        // An env file's text is dropped here, unused: it is never shown in a diff.
        if (what === "env") setRead({ kind: "env" });
        else if (what === "show") setRead({ kind: "text", text: r.content ?? "", abs: typeof r.abs === "string" ? r.abs : null });
        else setRead({ kind: "note", text: READ_NOTES[what] });
      },
      (e: any) => {
        if (dead) return;
        setRead({
          kind: "note",
          text: e?.status === 413 ? "Too large to diff here: the file is over 2 MB." : `Could not read it: ${e?.message || "error"}`,
        });
      },
    );
    return () => {
      dead = true;
    };
  }, [needRead, projectId, row.path, props.statusVer, props.nonce]);

  if (!base) return <div className="pad muted">…</div>;
  if (base.kind === "env") return <Note testid="scm-env" text={ENV_NOTE} />;
  if (base.kind === "refused") return <Note testid="scm-refused" text={`Not diffed here: ${base.text}`} />;
  if (base.kind === "gone") return <Note testid="scm-gone" text="Not on disk and not in HEAD: there is nothing to compare." />;
  if (base.kind === "error") return <Note testid="scm-error" text={`Could not read what HEAD holds: ${base.text}`} />;
  if ("note" in base.side) return <Note testid="scm-note" text={base.side.note} />;
  const before = base.side.text;
  if (deleted) return <ScmDiffEditor path={row.path} before={before} after="" live={null} />;
  if (live) {
    // The buffer's kind decides, never only its name: an env buffer is never diffed.
    const what = readSide(liveView?.kind, liveView?.kind === "withheld");
    if (what === "env") return <Note testid="scm-env" text={ENV_NOTE} />;
    if (what !== "show") return <Note testid="scm-note" text={READ_NOTES[what]} />;
    return <ScmDiffEditor path={row.path} before={before} after={null} live={liveModel} />;
  }
  if (!read) return <div className="pad muted">…</div>;
  if (read.kind === "env") return <Note testid="scm-env" text={ENV_NOTE} />;
  if (read.kind === "note") return <Note testid="scm-note" text={read.text} />;
  return <ScmDiffEditor path={row.path} before={before} after={read.text} live={null} />;
}

/**
 * HEAD's text on the left, the working file on the right, read-only on both
 * sides. `live` is an open buffer's own model: borrowed — this view never
 * disposes it, and lets go of it the moment the registry is about to.
 */
function ScmDiffEditor(props: { path: string; before: string; after: string | null; live: any | null }) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let dead = false;
    let ed: any = null;
    let original: any = null;
    let modified: any = null;
    let watch: { dispose(): void } | null = null;
    loadMonaco()
      .then((monaco: Monaco) => {
        if (dead || !host.current) return;
        const live = props.live && !props.live.isDisposed() ? props.live : null;
        ed = monaco.editor.createDiffEditor(host.current, {
          theme: "jarvis",
          automaticLayout: true,
          readOnly: true,
          originalEditable: false,
          renderSideBySide: true,
          // HEAD on the left, the working file on the right, even in a narrow
          // pane: never folded into one inline view (as Compare).
          useInlineViewWhenSpaceIsLimited: false,
          fontSize: 12,
          minimap: { enabled: false },
        });
        original = monaco.editor.createModel(props.before, live?.getLanguageId?.(), diffUri(monaco, props.path, "original"));
        if (live) {
          // The registry drops a buffer no tab holds: let go of its model
          // first, so nothing here shows a disposed model.
          watch = live.onWillDispose?.(() => ed?.setModel(null)) ?? null;
        } else {
          modified = monaco.editor.createModel(props.after ?? "", undefined, diffUri(monaco, props.path, "modified"));
        }
        ed.setModel({ original, modified: live ?? modified });
      })
      .catch(() => setFailed(true));
    return () => {
      dead = true;
      watch?.dispose();
      // The editor first, so nothing still shows the models it lets go of;
      // the buffer's model is the registry's and stays.
      ed?.setModel(null);
      ed?.dispose();
      original?.dispose();
      modified?.dispose();
    };
  }, [props.path, props.before, props.after, props.live]);

  if (failed) {
    return (
      <pre className="editorhost" data-testid="scm-fallback" style={{ overflow: "auto", padding: 8 }}>
        {props.live ? "" : props.after}
      </pre>
    );
  }
  return <div className="editorhost" data-testid="scm-editor" data-live={props.live ? "true" : "false"} ref={host} />;
}
