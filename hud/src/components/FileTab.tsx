// The File pane: the project tree, tabs, one editor whose model is swapped,
// the conflict bar, and markdown in a split view (editor left, rendered right,
// live) with a rendered-only toggle for reading docs.
//
// **Buffers are not this component's** (ED-2, editor plan §2.3–§2.4): every
// file's text lives in the registry (`state/buffers.ts`), one model per file
// on disk, keyed by its absolute path. This pane holds tabs on buffers — a
// reference each — so switching the pane to chat, re-picking its project or a
// render error unmounting it never takes an edit with it, and the same file
// in two panes is one buffer: one text, one dirty dot, one undo stack.
//
// **A File pane is one project's** (2026-10-09): its tabs are paths in its
// pinned project (`PaneSpec.editors`), and App keys this component by that
// project, so a different project is a fresh mount. A buffer saves through
// the project it was read through, against the etag it was read as; a 409 is
// the conflict bar, never a clobber.
//
// What the editor may do with a file is the daemon's one classifier
// (`jarvis/v2/editor_policy.py`): **withheld** files come with no content and
// show "withheld"; the gate's state, the safety layer and bytes that are not
// UTF-8 open read-only with the reason beside a lock; an **env file** (the
// owner's `.env*` rule) is the owner's to edit and carries "Jarvis can't read
// this file" — nothing here sends its text anywhere but its own save, and
// "Add to chat" is disabled for it.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import type { FsResult, Tree, TreeEntry } from "../types";
import { EditorStatus } from "./EditorStatus";
import { SearchView, type SearchAsk } from "./SearchView";
import { forgetListing } from "./QuickOpen";
import { CompareEditor, PaneEditor } from "./Editor";
import { ConflictBar } from "./ConflictBar";
import { EditorTabs, type TabInfo } from "./EditorTabs";
import { Markdown } from "./Markdown";
import { isMarkdown } from "../lib/monaco";
import {
  NO_TABS, TABS_MAX, chatExcerpt, closeTab, jarvisMayRead, openTab, pinTab, sameTabs, type Tabs,
} from "../lib/buffers";
import { buffers, refOf, type BufferView } from "../state/buffers";

function join(dir: string, name: string) {
  return dir ? `${dir}/${name}` : name;
}

/** A tree row the explorer's menu was opened on ("" and dir: the project itself). */
export interface MenuTarget {
  path: string;
  dir: boolean;
  policy?: "withheld" | "env";
}

function Node(props: {
  projectId: string;
  dir: string;
  depth: number;
  onOpen: (path: string, pin: boolean) => void;
  selected: string;
  /** Bumped after an explorer operation: every open folder reads itself again. */
  version: number;
  onMenu: (e: React.MouseEvent, target: MenuTarget) => void;
}) {
  const [tree, setTree] = useState<Tree | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .tree(props.projectId, props.dir, 1)
      .then((t) => {
        setTree(t);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [props.projectId, props.dir, props.version]);

  if (err) return <div className="err pad">{err}</div>;
  if (!tree) return <div className="muted pad small">…</div>;

  const cls = `tree-row indent-${Math.min(3, props.depth)}`;
  return (
    <>
      {tree.entries.map((e: TreeEntry) => {
        const path = join(props.dir, e.name);
        const withheld = e.policy === "withheld";
        if (e.kind === "dir") {
          const isOpen = !!open[path] && !withheld;
          return (
            <div key={path}>
              <div
                className={cls + (withheld ? " withheld" : "")}
                data-testid={`dir-${path}`}
                data-policy={e.policy}
                title={withheld ? "A credential folder: its contents are never listed here" : undefined}
                onClick={() => !withheld && setOpen({ ...open, [path]: !isOpen })}
                onContextMenu={(ev) => props.onMenu(ev, { path, dir: true, policy: e.policy })}
              >
                <span className="tw">{withheld ? "·" : isOpen ? "▾" : "▸"}</span>
                <span className="nm">{e.name}</span>
                {withheld ? <span className="treemark">withheld</span> : null}
              </div>
              {isOpen ? (
                <Node
                  projectId={props.projectId}
                  dir={path}
                  depth={props.depth + 1}
                  onOpen={props.onOpen}
                  selected={props.selected}
                  version={props.version}
                  onMenu={props.onMenu}
                />
              ) : null}
            </div>
          );
        }
        return (
          <div
            key={path}
            className={cls + (props.selected === path ? " sel" : "") + (withheld ? " withheld" : "")}
            data-testid={`file-${path}`}
            data-policy={e.policy}
            title={e.policy === "env" ? "Jarvis can't read this file" : withheld ? "withheld" : undefined}
            onClick={() => props.onOpen(path, false)}
            onDoubleClick={() => props.onOpen(path, true)}
            onContextMenu={(ev) => props.onMenu(ev, { path, dir: false, policy: e.policy })}
          >
            <span className="tw">{e.policy === "env" ? "🔒" : "·"}</span>
            <span className="nm">{e.name}</span>
          </div>
        );
      })}
    </>
  );
}

/**
 * What App asks of a File pane (ED-7): open a file (at a line), go to a line,
 * show the search, or one of the palette's file commands. Each ask is acted on
 * once, by its nonce.
 */
export type FileAsk = { nonce: number } & (
  | { kind: "open"; path: string; line?: number | null; col?: number | null }
  | { kind: "line"; line: number; col?: number | null }
  | { kind: "search"; query?: string }
  | { kind: "newFile" | "newFolder" | "save" | "reload" | "closeTab" | "nextTab" | "prevTab" | "copyPath" | "addToChat" }
);

/** The explorer's question in the tree column: a name to type, or a delete to confirm. */
type FsAsk =
  | { kind: "new_file"; dir: string; value: string }
  | { kind: "new_folder"; dir: string; value: string }
  | { kind: "rename"; path: string; dir: boolean; value: string }
  | { kind: "delete"; path: string; dir: boolean };

const parentOf = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");

/** The zoom the window is drawn at (CSS `zoom` on #root): a fixed menu is placed in its pixels. */
function zoomNow(): number {
  const z = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ui-zoom"));
  return Number.isFinite(z) && z > 0 ? z : 1;
}

const READONLY_LABEL: Record<string, string> = {
  gate: "read-only: permission state",
  safety: "read-only: safety layer",
  binary: "read-only: not text",
  unknown: "read-only: not a kind this window knows",
};

export function FileTab(props: {
  /** The pane this is: a tab's hold on a buffer is `<pane>|<path>`. */
  pane: number;
  projectId: string | null;
  /** The pane's tabs (`PaneSpec.editors`), in this project; null for none. */
  tabs: Tabs | null;
  /** New tabs, pinning the pane to `projectId` in the same step. */
  onTabs?: (projectId: string, tabs: Tabs | null) => void;
  /** A narrow split pane: the tree folds behind a toggle. */
  narrow?: boolean;
  /** An approval card is up: nothing here acts (the veil makes it inert too). */
  blocked?: boolean;
  /** "Add to chat": the selected chat's box gets this text, unsent. A sentence back is a refusal. */
  onAddToChat?: (text: string) => string | null;
  /** What App asks of this pane (ED-7): open at a line, the search, a palette command. */
  ask?: FileAsk | null;
  /**
   * The explorer renamed `from` to `to` in this pane's project (a file or a
   * folder): every pane's tabs follow — this project's by path, and through
   * `moves` (the registry's holds) any other project reaching the same file.
   */
  onRenamed?: (projectId: string, from: string, to: string, moves: { pane: string; from: string; to: string }[]) => void;
}) {
  // Read once, at mount: App keys this component by its project.
  const [pid] = useState(props.projectId);
  const pane = props.pane;
  useSyncExternalStore(buffers.subscribe, buffers.version);
  const tabs = props.tabs ?? NO_TABS;
  // The latest tabs, written ahead of the render that brings them back: two
  // gestures between renders must not undo each other.
  const tabsNow = useRef(tabs);
  if (!sameTabs(tabsNow.current, tabs)) tabsNow.current = tabs;
  const onTabsRef = useRef(props.onTabs);
  onTabsRef.current = props.onTabs;
  const setTabs = useCallback((next: Tabs) => {
    if (!pid || sameTabs(tabsNow.current, next)) return;
    tabsNow.current = next;
    onTabsRef.current?.(pid, next.paths.length ? next : null);
  }, [pid]);

  // Each tab's buffer: the absolute path its hold is on, or why it failed.
  const [opened, setOpened] = useState<Record<string, string>>({});
  const held = useRef(new Map<string, string>());
  const loading = useRef(new Set<string>());
  const discarding = useRef(new Set<string>());
  const alive = useRef(true);
  const [note, setNote] = useState("");
  const [treeOpen, setTreeOpen] = useState(false);
  const [mdRead, setMdRead] = useState<Record<string, boolean>>({});
  // Reload over an unsaved edit, or closing the last tab on one: what was asked.
  const [ask, setAsk] = useState<{ kind: "reload" | "close"; path: string } | null>(null);
  // Compare: the disk's text it shows, and the etag of that text — what Keep
  // mine saves against while it is open.
  const [comparing, setComparing] = useState<{ abs: string; disk: string; etag: string | null } | null>(null);
  const editorRef = useRef<any>(null);
  // The live editor, as state as well: the status bar follows its cursor, and
  // a reveal waits for it to show the buffer.
  const [ed, setEd] = useState<any>(null);
  // The tree column shows the files or the search (ED-7).
  const [side, setSide] = useState<"files" | "search">("files");
  const [searchAsk, setSearchAsk] = useState<SearchAsk | null>(null);
  const [treeVersion, setTreeVersion] = useState(0);
  const [menu, setMenu] = useState<(MenuTarget & { x: number; y: number }) | null>(null);
  const [fsAsk, setFsAsk] = useState<FsAsk | null>(null);
  const [fsBusy, setFsBusy] = useState(false);
  const [fsErr, setFsErr] = useState("");
  // A place to put the cursor once the buffer is in the editor: quick open's
  // `:line`, a search hit.
  const reveal = useRef<{ path: string; line: number; col: number } | null>(null);

  // Hold what the tabs name; let go of what they no longer do.
  const pathsKey = tabs.paths.join("\u0000");
  useEffect(() => {
    if (!pid) return;
    const want = new Set(tabsNow.current.paths);
    for (const [path, abs] of [...held.current]) {
      if (want.has(path)) continue;
      held.current.delete(path);
      buffers.release(abs, refOf(pane, path), { discard: discarding.current.delete(path) });
      setOpened((o) => {
        const next = { ...o };
        delete next[path];
        return next;
      });
    }
    for (const path of want) {
      if (held.current.has(path) || loading.current.has(path)) continue;
      loading.current.add(path);
      buffers.open(pid, path, refOf(pane, path)).then(
        (view) => {
          loading.current.delete(path);
          if (!alive.current || !tabsNow.current.paths.includes(path)) {
            buffers.release(view.abs, refOf(pane, path));
            return;
          }
          held.current.set(path, view.abs);
          setOpened((o) => ({ ...o, [path]: view.abs }));
          if (isMarkdown(path)) setMdRead((m) => (path in m ? m : { ...m, [path]: true }));
        },
        (e: any) => {
          loading.current.delete(path);
          if (!alive.current) return;
          // A tab whose file cannot be read (gone, out of scope) closes, and says why.
          setNote(`${path}: ${e?.status === 404 ? "not found" : e?.message || "could not be read"}`);
          setTabs(closeTab(tabsNow.current, path));
        },
      );
    }
  }, [pid, pathsKey, pane, setTabs]);

  // Unmounting lets go of every hold — never of an edit: a dirty buffer no
  // tab holds stays in the registry until it is reopened or discarded.
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      for (const [path, abs] of held.current) buffers.release(abs, refOf(pane, path));
      held.current.clear();
    };
  }, [pane]);

  // What the registry says about this pane's buffers (a save, a reload in
  // place) is said here.
  useEffect(() => buffers.onNotice((abs, text) => {
    const paths = [...held.current].filter(([, a]) => a === abs).map(([p]) => p);
    if (!paths.length) return;
    const active = tabsNow.current.active;
    setNote(paths.includes(active ?? "") ? text : `${paths[0]}: ${text}`);
  }), []);

  const bufOf = (path: string | null): BufferView | null => {
    const abs = path ? opened[path] : undefined;
    return abs ? buffers.get(abs) : null;
  };
  const active = tabs.active;
  const view = bufOf(active);
  const activeAbs = active ? opened[active] ?? null : null;

  // An edit pins the preview tab.
  useEffect(() => {
    const p = tabs.preview;
    if (p && bufOf(p)?.dirty) setTabs(pinTab(tabsNow.current, p));
  });

  // Compare ends when the conflict does (Keep mine, Take theirs) or the tab
  // changes; when the disk moves again under it, its left side is read again
  // — Keep mine saves against what the owner can see. Read again once per
  // new conflict etag (`rereadFor`), never "until the two agree": a disk that
  // moved again after the bar's etag answered with a third etag, and the
  // re-read looped — 343 GETs a second, unbounded while the window was hidden
  // and the poll asleep (review of PR #49, M2).
  const conflictEtag = view?.conflict?.state === "changed" ? view.conflict.etag : null;
  const rereadFor = useRef<string | null>(null);
  useEffect(() => {
    if (!comparing) return;
    if (comparing.abs !== activeAbs || !view?.conflict) {
      setComparing(null);
      return;
    }
    if (conflictEtag && comparing.etag !== conflictEtag && rereadFor.current !== conflictEtag) {
      rereadFor.current = conflictEtag;
      const abs = comparing.abs;
      void buffers.readDisk(abs).then((d) => {
        if (d && alive.current) setComparing((c) => (c && c.abs === abs ? { abs, disk: d.text, etag: d.etag } : c));
      });
    }
  }, [comparing, activeAbs, view?.conflict, conflictEtag]);

  // ---- gestures ---------------------------------------------------------------

  const open = (path: string, pin: boolean) => {
    setAsk(null);
    setTreeOpen(false);
    // A narrow pane folds its tree behind "Files" the moment a file opens, so
    // there is no browsing click to click: every open there is a pinned tab.
    const pinned = pin || !!props.narrow;
    const dirty = (p: string) => !!bufOf(p)?.dirty;
    const next = openTab(tabsNow.current, path, { pin: pinned, keepPreview: dirty, dirty });
    if (!next) {
      // Past the cap only a clean tab is ever closed to make room (M1).
      setNote(`${TABS_MAX} tabs are open and every other one holds an unsaved edit: save or close one first`);
      return;
    }
    setTabs(next);
  };

  const close = (path: string, discard = false) => {
    setAsk(null);
    if (discard) discarding.current.add(path);
    setTabs(closeTab(tabsNow.current, path));
  };

  /** × or middle-click: the last tab on an unsaved edit asks first. */
  const requestClose = (path: string) => {
    const b = bufOf(path);
    if (b && b.dirty && b.refs.length <= 1) setAsk({ kind: "close", path });
    else close(path);
  };

  const save = useCallback(async (path: string | null = tabsNow.current.active) => {
    const abs = path ? held.current.get(path) : undefined;
    if (!abs) return "skipped";
    const b = buffers.get(abs);
    if (!b || !b.editable) {
      if (b) setNote(`${b.path} is read-only here`);
      return "skipped";
    }
    return buffers.save(abs);
  }, []);

  const reload = () => {
    if (!active || !view || view.kind === "withheld") return;
    if (view.dirty) setAsk({ kind: "reload", path: active });
    else void buffers.reload(view.abs);
  };

  /**
   * "Add selection to chat" (ED-2's minimal one, extended in ED-7): each
   * selection — or the cursor's line, with none — goes into the selected
   * chat's box as `path:Lx-y` and a fenced excerpt, **unsent** (decisions
   * W-6). Several cursors give several excerpts, top to bottom. An env file,
   * a withheld one or one of an unknown kind never gets here
   * (`jarvisMayRead`): its text goes in no request but its own save.
   */
  const addToChat = () => {
    if (!view || !active || !jarvisMayRead(view.kind) || !props.onAddToChat) return;
    const model = buffers.model(view.abs) as any;
    if (!model) return;
    const editor = editorRef.current;
    const sels: any[] = editor && editor.getModel?.() === model ? [...(editor.getSelections?.() ?? [])] : [];
    sels.sort((a, b) => a.startLineNumber - b.startLineNumber || a.startColumn - b.startColumn);
    const parts: string[] = [];
    const seen = new Set<string>();
    const push = (text: string, from: number, to: number) => {
      const part = chatExcerpt(active, text, from, to);
      if (!seen.has(part)) {
        seen.add(part);
        parts.push(part);
      }
    };
    if (!sels.length) {
      const text = model.getValue();
      push(text, 1, Math.max(1, text.split("\n").length));
    }
    for (const sel of sels) {
      if (sel.isEmpty()) {
        push(model.getLineContent(sel.startLineNumber), sel.startLineNumber, sel.startLineNumber);
      } else {
        const from = sel.startLineNumber;
        const to = sel.endColumn === 1 && sel.endLineNumber > from ? sel.endLineNumber - 1 : sel.endLineNumber;
        push(model.getValueInRange(sel), from, to);
      }
    }
    const refused = props.onAddToChat(parts.join("\n"));
    setNote(refused || (parts.length > 1 ? `${parts.length} excerpts added to the chat's box — not sent`
      : "added to the chat's box — not sent"));
  };

  const toggleCompare = async () => {
    if (comparing || !view) {
      setComparing(null);
      return;
    }
    const disk = await buffers.readDisk(view.abs);
    if (disk !== null && alive.current) setComparing({ abs: view.abs, disk: disk.text, etag: disk.etag });
  };

  // ---- navigation (ED-7) ---------------------------------------------------------

  /** Open `path` pinned, and put the cursor at `line` (and `col`) once it shows. */
  const openAt = (path: string, line?: number | null, col?: number | null) => {
    if (line) {
      reveal.current = { path, line, col: col ?? 1 };
      // A markdown file opens rendered; a place in it is a place in its text.
      if (isMarkdown(path)) setMdRead((m) => ({ ...m, [path]: false }));
    }
    open(path, true);
  };

  // The cursor goes where it was asked once the editor shows that buffer
  // (Monaco loads late the first time; a tab's buffer is read first).
  useEffect(() => {
    const r = reveal.current;
    if (!r || !ed || active !== r.path || !view || view.kind === "withheld") return;
    const m = buffers.model(view.abs) as any;
    if (!m || ed.getModel?.() !== m) return;
    reveal.current = null;
    const line = Math.max(1, Math.min(r.line, m.getLineCount?.() ?? r.line));
    ed.setPosition?.({ lineNumber: line, column: Math.max(1, r.col) });
    ed.revealLineInCenter?.(line);
    if (!props.blocked) ed.focus?.();
  });

  const bump = () => {
    setTreeVersion((v) => v + 1);
    if (pid) forgetListing(pid);
  };

  /** The explorer's menu, at the pointer (in the pixels the zoomed window is drawn in). */
  const openMenu = (e: React.MouseEvent, target: MenuTarget) => {
    e.preventDefault();
    e.stopPropagation();
    if (props.blocked) return;
    const z = zoomNow();
    setMenu({ ...target, x: e.clientX / z, y: e.clientY / z });
  };

  // The menu closes on a click elsewhere, Escape, a scroll or a card.
  useEffect(() => {
    if (!menu) return;
    const close = (ev: Event) => {
      if (ev instanceof KeyboardEvent && ev.key !== "Escape") return;
      if (ev.type === "mousedown" && (ev.target as HTMLElement | null)?.closest?.("[data-testid='explorer-menu']")) return;
      setMenu(null);
    };
    document.addEventListener("mousedown", close, true);
    document.addEventListener("keydown", close, true);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("mousedown", close, true);
      document.removeEventListener("keydown", close, true);
      window.removeEventListener("blur", close);
    };
  }, [menu]);
  // Under a card nothing of the explorer's stays open.
  useLayoutEffect(() => {
    if (!props.blocked) return;
    setMenu(null);
    setFsAsk(null);
  }, [props.blocked]);

  const askFs = (next: FsAsk) => {
    setMenu(null);
    setFsErr("");
    setFsAsk(next);
    if (props.narrow) setTreeOpen(true);
    setSide("files");
  };

  /** Run what the prompt asked: through the daemon's `fs` route only (owner-only). */
  const runFs = async (a: FsAsk) => {
    if (!pid || fsBusy || props.blocked) return;
    const clean = (v: string) => v.trim().replace(/^\/+|\/+$/g, "");
    setFsErr("");
    setFsBusy(true);
    try {
      if (a.kind === "new_file" || a.kind === "new_folder") {
        const name = clean(a.value);
        if (!name) throw new Error("type a name");
        const path = a.dir ? `${a.dir}/${name}` : name;
        const res = await api.fileOp(pid, { op: a.kind, path });
        setFsAsk(null);
        bump();
        if (a.kind === "new_file") openAt(res.path);
        else setNote(`made ${res.path}/`);
      } else if (a.kind === "rename") {
        const to = clean(a.value);
        if (!to || to === a.path) {
          setFsAsk(null);
          return;
        }
        const res: FsResult = await api.fileOp(pid, { op: "rename", path: a.path, to });
        setFsAsk(null);
        bump();
        // The buffers follow first (the edit, the undo stack, every hold),
        // then every pane's tabs (App): a tab on the old name finds the same
        // buffer under the new one.
        const moves = res.from_abs ? buffers.renamed(res.from_abs, res.abs, res.dir ? null : res.kind) : [];
        props.onRenamed?.(pid, a.path, res.path, moves);
        setNote(`renamed to ${res.path}`);
      } else {
        const res = await api.fileOp(pid, { op: "delete", path: a.path });
        setFsAsk(null);
        bump();
        // An open tab on it says "deleted on disk" at once; an unsaved edit
        // stays in its buffer, and Save puts the file back.
        void buffers.poll();
        setNote(`moved ${a.path} to the trash${res.trash?.location ? ` (${res.trash.location})` : ""}`);
      }
    } catch (e: any) {
      setFsErr(e?.message || "that did not work");
    } finally {
      setFsBusy(false);
    }
  };

  /** Open buffers on `path` or under it that hold an unsaved edit: what a delete must warn about. */
  const dirtyUnder = (path: string): string[] =>
    buffers.snapshot().filter((b) => b.projectId === pid && b.dirty
      && (b.path === path || b.path.startsWith(`${path}/`))).map((b) => b.path);

  // What App asks (quick open, a search hit elsewhere, the palette), once each.
  const doneAsk = useRef(0);
  const appAsk = props.ask;
  useEffect(() => {
    const ask = appAsk;
    if (!ask || ask.nonce === doneAsk.current || !pid) return;
    doneAsk.current = ask.nonce;
    switch (ask.kind) {
      case "open":
        openAt(ask.path, ask.line, ask.col);
        return;
      case "line":
        if (active) openAt(active, ask.line, ask.col);
        return;
      case "search":
        setSide("search");
        if (props.narrow) setTreeOpen(true);
        setSearchAsk({ nonce: ask.nonce, query: ask.query });
        return;
      case "newFile":
      case "newFolder":
        askFs({ kind: ask.kind === "newFile" ? "new_file" : "new_folder", dir: active ? parentOf(active) : "", value: "" });
        return;
      case "save":
        void save();
        return;
      case "reload":
        reload();
        return;
      case "closeTab":
        if (active) requestClose(active);
        return;
      case "nextTab":
      case "prevTab": {
        const t = tabsNow.current;
        if (!t.active || t.paths.length < 2) return;
        const i = t.paths.indexOf(t.active);
        const j = (i + (ask.kind === "nextTab" ? 1 : t.paths.length - 1)) % t.paths.length;
        setTabs({ ...t, active: t.paths[j] });
        return;
      }
      case "copyPath":
        if (active) {
          void navigator.clipboard?.writeText(active).then(
            () => setNote(`copied ${active}`), () => setNote("the clipboard refused"));
        }
        return;
      case "addToChat":
        if (!props.blocked) addToChat();
        return;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appAsk?.nonce, pid]);

  // ---- drawing ------------------------------------------------------------------

  const info = (path: string): TabInfo => {
    const b = bufOf(path);
    if (!b) return { dirty: false, loading: true };
    return {
      dirty: b.dirty,
      conflict: !!b.conflict,
      saving: b.saving,
      mark: b.kind === "env" ? "env" : b.kind === "withheld" ? "withheld" : b.editable ? null : "readonly",
    };
  };

  const md = !!active && isMarkdown(active);
  const read = md && mdRead[active!] !== false;
  // The read's `editable`, never the kind alone (MED1).
  const editable = !!view && view.editable;
  const model = view && view.kind !== "withheld" ? buffers.model(view.abs) : null;
  // The editor exists from the first buffer it shows, and stays: its model is swapped.
  const [everEditor, setEverEditor] = useState(false);
  useEffect(() => {
    if (model && !everEditor) setEverEditor(true);
  }, [model, everEditor]);
  const mdText = useMdText(md ? view?.abs ?? null : null);

  if (!pid) return <div className="pad muted">Pick a project.</div>;

  const showTree = !props.narrow || treeOpen || !active;
  const env = view?.kind === "env";
  return (
    <div className="split">
      <div className="filetree" data-testid="filetree" style={showTree ? undefined : { display: "none" }}>
        <div className="sidetabs" role="tablist">
          <button type="button" role="tab" data-testid="side-files" aria-selected={side === "files"}
                  className={side === "files" ? "on" : undefined} onClick={() => setSide("files")}>
            Files
          </button>
          <button type="button" role="tab" data-testid="side-search" aria-selected={side === "search"}
                  className={side === "search" ? "on" : undefined} title="Find in files (Ctrl+Shift+F)"
                  onClick={() => {
                    setSide("search");
                    setSearchAsk({ nonce: Date.now() });
                  }}>
            Search
          </button>
          {side === "files" ? (
            <>
              <button type="button" className="sideact" data-testid="explorer-new-file" title="New file"
                      disabled={props.blocked}
                      onClick={() => askFs({ kind: "new_file", dir: active ? parentOf(active) : "", value: "" })}>
                +
              </button>
              <button type="button" className="sideact" data-testid="explorer-new-folder" title="New folder"
                      disabled={props.blocked}
                      onClick={() => askFs({ kind: "new_folder", dir: active ? parentOf(active) : "", value: "" })}>
                +▸
              </button>
            </>
          ) : null}
        </div>
        {fsAsk && side === "files" ? (
          <FsPrompt
            ask={fsAsk}
            busy={fsBusy}
            err={fsErr}
            dirty={fsAsk.kind === "delete" ? dirtyUnder(fsAsk.path) : []}
            onChange={(value) => setFsAsk((a) => (a && a.kind !== "delete" ? { ...a, value } : a))}
            onOk={() => void runFs(fsAsk)}
            onCancel={() => {
              setFsAsk(null);
              setFsErr("");
            }}
          />
        ) : null}
        <div className="treebody" style={side === "files" ? undefined : { display: "none" }}
             onContextMenu={(e) => openMenu(e, { path: "", dir: true })}>
          <Node projectId={pid} dir="" depth={0} onOpen={open} selected={active || ""}
                version={treeVersion} onMenu={openMenu} />
        </div>
        <SearchView projectId={pid} ask={searchAsk} visible={side === "search"} blocked={props.blocked}
                    onOpen={(path, line, col) => openAt(path, line, col)} />
        {menu ? (
          <ExplorerMenu
            target={menu}
            onClose={() => setMenu(null)}
            onNewFile={() => askFs({ kind: "new_file", dir: menu.dir ? menu.path : parentOf(menu.path), value: "" })}
            onNewFolder={() => askFs({ kind: "new_folder", dir: menu.dir ? menu.path : parentOf(menu.path), value: "" })}
            onRename={() => askFs({ kind: "rename", path: menu.path, dir: menu.dir, value: menu.path })}
            onDelete={() => askFs({ kind: "delete", path: menu.path, dir: menu.dir })}
            onCopy={() => {
              const path = menu.path;
              setMenu(null);
              void navigator.clipboard?.writeText(path).then(() => setNote(`copied ${path}`), () => setNote("the clipboard refused"));
            }}
          />
        ) : null}
      </div>
      <div className="editorwrap">
        <EditorTabs
          tabs={tabs}
          info={info}
          onActivate={(p) => setTabs({ ...tabsNow.current, active: p })}
          onPin={(p) => setTabs(pinTab(tabsNow.current, p))}
          onClose={requestClose}
        />
        <div className="toolbar">
          {props.narrow && active ? (
            <button
              type="button"
              data-testid="tree-toggle"
              aria-pressed={showTree}
              title={showTree ? "Hide the files" : "Show the files"}
              onClick={() => setTreeOpen((o) => !o)}
            >
              Files
            </button>
          ) : null}
          <span className="path" data-testid="file-path">{active || "no file open"}</span>
          {env ? (
            <span className="chip edlock" data-testid="editor-lock" title={view?.readonly || undefined}>
              🔒 Jarvis can't read this file
            </span>
          ) : null}
          {view && !view.editable && view.kind !== "withheld" ? (
            // An env file that is not UTF-8 carries both: Jarvis can't read
            // it, and the editor won't write it (MED1).
            <span className="chip edro" data-testid="editor-readonly-reason" title={view.readonly || undefined}>
              🔒 {READONLY_LABEL[view.kind === "env" ? "binary" : view.kind] || "read-only"}
            </span>
          ) : null}
          {md ? (
            <button type="button" data-testid="toggle-rendered"
                    onClick={() => setMdRead((m) => ({ ...m, [active!]: !read }))}>
              {read ? "Edit" : "Read"}
            </button>
          ) : null}
          <button
            type="button"
            data-testid="add-to-chat"
            disabled={!view || !jarvisMayRead(view.kind) || !props.onAddToChat || props.blocked}
            title={env ? "Jarvis can't read this file, so it never goes to a chat"
              : "Put the selection (or this line) in the selected chat's box, unsent"}
            onClick={addToChat}
          >
            Add to chat
          </button>
          <button type="button" data-testid="file-reload" disabled={!view || view.kind === "withheld"} onClick={reload}>
            Reload
          </button>
          <button
            type="button"
            data-testid="file-save"
            // Not disabled while a save is out: a press then is the one
            // follow-up save (`buffers.save`), as Ctrl+S is (#49, LOW-2).
            disabled={!view || !editable || !view.dirty}
            onClick={() => void save()}
          >
            Save
          </button>
          {note ? <span className="err" data-testid="file-note">{note}</span> : null}
        </div>
        {view?.conflict && active ? (
          <ConflictBar
            path={active}
            conflict={view.conflict}
            dirty={view.dirty}
            comparing={!!comparing}
            // Keep mine and Save to recreate must stay disabled while a save
            // is out: pressed then, `buffers.save` would make them the one
            // follow-up, which saves against the buffer's etag and drops the
            // `expected` they pass (delta review of PR #49, LOW-2).
            busy={view.saving}
            onCompare={() => void toggleCompare()}
            onKeep={() => {
              // Against the disk the owner was shown: Compare's left side
              // while it is open, else the etag this bar was drawn for.
              const shown = comparing && comparing.abs === view.abs ? comparing.etag
                : view.conflict?.state === "changed" ? view.conflict.etag : null;
              void buffers.keepMine(view.abs, shown);
            }}
            onTake={() => {
              setComparing(null);
              void buffers.takeTheirs(view.abs);
            }}
            onRecreate={() => void buffers.save(view.abs, { expected: null })}
            onClose={() => requestClose(active)}
          />
        ) : null}
        {ask ? (
          <div className="toolbar warn" data-testid={ask.kind === "close" ? "editor-close-ask" : "file-discard-ask"} role="alert">
            <span>
              {ask.path} has an unsaved edit.{" "}
              {ask.kind === "reload" ? "Reloading throws it away." : "Save it before closing?"}
            </span>
            {ask.kind === "reload" ? (
              <>
                <button type="button" data-testid="file-discard-no" onClick={() => setAsk(null)}>
                  Keep editing
                </button>
                <button
                  type="button"
                  data-testid="file-discard-yes"
                  onClick={() => {
                    const abs = held.current.get(ask.path);
                    setAsk(null);
                    if (abs) void buffers.reload(abs);
                  }}
                >
                  Discard and reload
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  data-testid="close-save"
                  onClick={async () => {
                    const path = ask.path;
                    setAsk(null);
                    if ((await save(path)) === "saved") close(path);
                  }}
                >
                  Save
                </button>
                <button type="button" data-testid="close-discard" onClick={() => close(ask.path, true)}>
                  Don't save
                </button>
                <button type="button" data-testid="close-cancel" onClick={() => setAsk(null)}>
                  Cancel
                </button>
              </>
            )}
          </div>
        ) : null}
        {!active ? (
          <div className="pad muted">Open a file from the tree.</div>
        ) : view?.kind === "withheld" ? (
          // No content ever arrives for these, and the tab says so rather than
          // showing an empty editor that looks like an empty file.
          <div className="pad" data-testid="file-withheld">
            <b>withheld</b> — this is a credential file. Its contents are never sent to this window.
          </div>
        ) : null}
        {everEditor ? (
          <div className={"edbody" + (md && model && !read && !comparing ? " mdsplit" : "")}
               data-testid={md && model && !read && !comparing ? "md-split" : undefined}
               style={!active || view?.kind === "withheld" ? { display: "none" } : undefined}>
            <PaneEditor
              model={model}
              viewKey={view?.abs ?? null}
              path={active || ""}
              readOnly={!editable}
              hidden={read || !!comparing || !model}
              onSave={() => void save()}
              editorRef={(editor) => {
                editorRef.current = editor;
                setEd(editor);
              }}
              mayAddToChat={!!view && jarvisMayRead(view.kind) && !!props.onAddToChat}
              onAddToChat={addToChat}
            />
            {md && model && !read && !comparing ? (
              <div className="mdrender" data-testid="md-preview">
                <Markdown text={mdText} className="" />
              </div>
            ) : null}
            {md && model && read && !comparing ? (
              <div className="mdrender" data-testid="md-rendered">
                <Markdown text={mdText} className="" />
              </div>
            ) : null}
            {comparing && model ? (
              <CompareEditor model={model} disk={comparing.disk} path={active || ""} readOnly={!editable}
                             onSave={() => void save()} />
            ) : null}
          </div>
        ) : active && view && view.kind !== "withheld" ? (
          <div className="pad muted">…</div>
        ) : null}
        {view && view.kind !== "withheld" ? <EditorStatus view={view} editor={ed} /> : null}
      </div>
    </div>
  );
}

/** A markdown buffer's text for its preview: read when the buffer changes, and a little after the last keystroke. */
function useMdText(abs: string | null): string {
  const model = abs ? buffers.model(abs) : null;
  const [seen, setSeen] = useState<{ model: unknown; text: string }>({ model: null, text: "" });
  useEffect(() => {
    if (!model) return;
    setSeen({ model, text: model.getValue() });
    let t: ReturnType<typeof setTimeout> | null = null;
    const sub = model.onDidChangeContent(() => {
      if (t) clearTimeout(t);
      t = setTimeout(() => !model.isDisposed() && setSeen({ model, text: model.getValue() }), 120);
    });
    return () => {
      sub.dispose();
      if (t) clearTimeout(t);
    };
  }, [model]);
  // Another buffer than the one last read: read it now, not a frame later.
  if (!model) return "";
  return seen.model === model ? seen.text : model.getValue();
}

/** The explorer's question: a name for a new file or folder, a new name, or a delete to confirm. */
function FsPrompt(props: {
  ask: FsAsk;
  busy: boolean;
  err: string;
  /** Unsaved edits on or under a path about to be deleted. */
  dirty: string[];
  onChange: (value: string) => void;
  onOk: () => void;
  onCancel: () => void;
}) {
  const a = props.ask;
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.focus();
    if (a.kind === "rename") {
      // The name without its extension selected, as VS Code does.
      const slash = a.value.lastIndexOf("/") + 1;
      const dot = a.value.lastIndexOf(".");
      el.setSelectionRange(slash, dot > slash ? dot : a.value.length);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a.kind, "path" in a ? a.path : a.dir]);
  const label = a.kind === "new_file" ? `New file in ${a.dir || "the project"}/`
    : a.kind === "new_folder" ? `New folder in ${a.dir || "the project"}/`
    : a.kind === "rename" ? `Rename or move ${a.path}${a.dir ? "/" : ""} to`
    : `Move ${a.path}${a.dir ? "/" : ""} to the trash?`;
  return (
    <div className="fsprompt" data-testid="fs-prompt" data-kind={a.kind}
         onKeyDown={(e) => {
           if (e.key === "Escape") {
             e.preventDefault();
             e.stopPropagation();
             props.onCancel();
           }
         }}>
      <div className="small">{label}</div>
      {a.kind !== "delete" ? (
        <input
          ref={input}
          data-testid="fs-input"
          value={a.value}
          spellCheck={false}
          autoComplete="off"
          // Read-only while it runs, never disabled: a disabled box drops
          // focus, and Escape would then have nowhere to land.
          readOnly={props.busy}
          onChange={(e) => props.onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              props.onOk();
            }
          }}
        />
      ) : props.dirty.length ? (
        <div className="small warnish" data-testid="fs-dirty">
          {props.dirty.length === 1 ? `${props.dirty[0]} has` : `${props.dirty.length} open files have`} an unsaved
          edit. It stays in its tab, and Save puts the file back.
        </div>
      ) : null}
      <div className="fsbuttons">
        <button type="button" data-testid="fs-ok" disabled={props.busy} onClick={props.onOk}>
          {a.kind === "delete" ? "Move to trash" : a.kind === "rename" ? "Rename" : "Create"}
        </button>
        <button type="button" data-testid="fs-cancel" onClick={props.onCancel}>Cancel</button>
      </div>
      {props.err ? <div className="err small" data-testid="fs-error">{props.err}</div> : null}
    </div>
  );
}

/** The explorer's right-click menu. A withheld row offers nothing but its path. */
function ExplorerMenu(props: {
  target: MenuTarget & { x: number; y: number };
  onClose: () => void;
  onNewFile: () => void;
  onNewFolder: () => void;
  onRename: () => void;
  onDelete: () => void;
  onCopy: () => void;
}) {
  const t = props.target;
  const box = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState({ left: t.x, top: t.y });
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const z = zoomNow();
    const w = window.innerWidth / z;
    const h = window.innerHeight / z;
    setAt({
      left: Math.max(0, Math.min(t.x, w - el.offsetWidth - 4)),
      top: t.y + el.offsetHeight > h ? Math.max(0, t.y - el.offsetHeight) : t.y,
    });
  }, [t.x, t.y]);
  const root = t.path === "";
  const withheld = t.policy === "withheld";
  const item = (testid: string, label: string, run: () => void) => (
    <button type="button" role="menuitem" data-testid={testid} onClick={run}>{label}</button>
  );
  return (
    <div ref={box} className="explorermenu" role="menu" data-testid="explorer-menu"
         style={{ left: at.left, top: at.top }}>
      {!withheld && (t.dir || !root) ? (
        <>
          {item("menu-new-file", "New File…", props.onNewFile)}
          {item("menu-new-folder", "New Folder…", props.onNewFolder)}
        </>
      ) : null}
      {!root && !withheld ? (
        <>
          {item("menu-rename", "Rename…", props.onRename)}
          {item("menu-delete", "Delete", props.onDelete)}
        </>
      ) : null}
      {!root ? item("menu-copy-path", "Copy Relative Path", props.onCopy) : null}
    </div>
  );
}
