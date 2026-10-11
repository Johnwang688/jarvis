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

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import type { Tree, TreeEntry } from "../types";
import { CompareEditor, PaneEditor } from "./Editor";
import { ConflictBar } from "./ConflictBar";
import { EditorTabs, type TabInfo } from "./EditorTabs";
import { Markdown } from "./Markdown";
import { isMarkdown } from "../lib/monaco";
import {
  EDITABLE, NO_TABS, chatExcerpt, closeTab, jarvisMayRead, openTab, pinTab, sameTabs, type Tabs,
} from "../lib/buffers";
import { buffers, refOf, type BufferView } from "../state/buffers";

function join(dir: string, name: string) {
  return dir ? `${dir}/${name}` : name;
}

function Node(props: {
  projectId: string;
  dir: string;
  depth: number;
  onOpen: (path: string, pin: boolean) => void;
  selected: string;
}) {
  const [tree, setTree] = useState<Tree | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .tree(props.projectId, props.dir, 1)
      .then(setTree)
      .catch((e) => setErr(e.message));
  }, [props.projectId, props.dir]);

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
          >
            <span className="tw">{e.policy === "env" ? "🔒" : "·"}</span>
            <span className="nm">{e.name}</span>
          </div>
        );
      })}
    </>
  );
}

/** How long a narrow pane's tree waits after a single click before it folds. */
const DOUBLE_CLICK_MS = 400;

const READONLY_LABEL: Record<string, string> = {
  gate: "read-only: permission state",
  safety: "read-only: safety layer",
  binary: "read-only: not text",
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
  // A narrow pane folds its tree once a file opens — a moment after a single
  // click, so the second click of a double click (pin) still lands on the row.
  const [treeHold, setTreeHold] = useState(false);
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (holdTimer.current) clearTimeout(holdTimer.current);
  }, []);
  const [mdRead, setMdRead] = useState<Record<string, boolean>>({});
  // Reload over an unsaved edit, or closing the last tab on one: what was asked.
  const [ask, setAsk] = useState<{ kind: "reload" | "close"; path: string } | null>(null);
  const [comparing, setComparing] = useState<{ abs: string; disk: string } | null>(null);
  const editorRef = useRef<any>(null);

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

  // Compare ends when the conflict does (Keep mine, Take theirs) or the tab changes.
  useEffect(() => {
    if (comparing && (comparing.abs !== activeAbs || !view?.conflict)) setComparing(null);
  }, [comparing, activeAbs, view?.conflict]);

  // ---- gestures ---------------------------------------------------------------

  const open = (path: string, pin: boolean) => {
    setAsk(null);
    setTreeOpen(false);
    if (holdTimer.current) clearTimeout(holdTimer.current);
    setTreeHold(!pin);
    if (!pin) holdTimer.current = setTimeout(() => setTreeHold(false), DOUBLE_CLICK_MS);
    setTabs(openTab(tabsNow.current, path, { pin, keepPreview: (p) => !!bufOf(p)?.dirty }));
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
    if (!b || !EDITABLE.has(b.kind)) {
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

  const addToChat = () => {
    if (!view || !active || !jarvisMayRead(view.kind) || !props.onAddToChat) return;
    const ed = editorRef.current;
    const model = buffers.model(view.abs);
    if (!model) return;
    let from = 1;
    let to = 1;
    let text = "";
    const sel = ed?.getSelection?.();
    if (sel && ed.getModel?.() === model) {
      if (sel.isEmpty()) {
        from = to = sel.startLineNumber;
        text = (model as any).getLineContent(from);
      } else {
        from = sel.startLineNumber;
        to = sel.endColumn === 1 && sel.endLineNumber > from ? sel.endLineNumber - 1 : sel.endLineNumber;
        text = (model as any).getValueInRange(sel);
      }
    } else {
      text = model.getValue();
      to = Math.max(1, text.split("\n").length);
    }
    const refused = props.onAddToChat(chatExcerpt(active, text, from, to));
    setNote(refused || "added to the chat's box — not sent");
  };

  const toggleCompare = async () => {
    if (comparing || !view) {
      setComparing(null);
      return;
    }
    const disk = await buffers.readDisk(view.abs);
    if (disk !== null && alive.current) setComparing({ abs: view.abs, disk });
  };

  // ---- drawing ------------------------------------------------------------------

  const info = (path: string): TabInfo => {
    const b = bufOf(path);
    if (!b) return { dirty: false, loading: true };
    return {
      dirty: b.dirty,
      conflict: !!b.conflict,
      mark: b.kind === "env" ? "env" : b.kind === "withheld" ? "withheld" : EDITABLE.has(b.kind) ? null : "readonly",
    };
  };

  const md = !!active && isMarkdown(active);
  const read = md && mdRead[active!] !== false;
  const editable = !!view && EDITABLE.has(view.kind);
  const model = view && view.kind !== "withheld" ? buffers.model(view.abs) : null;
  // The editor exists from the first buffer it shows, and stays: its model is swapped.
  const [everEditor, setEverEditor] = useState(false);
  useEffect(() => {
    if (model && !everEditor) setEverEditor(true);
  }, [model, everEditor]);
  const mdText = useMdText(md ? view?.abs ?? null : null);

  if (!pid) return <div className="pad muted">Pick a project.</div>;

  const showTree = !props.narrow || treeOpen || treeHold || !active;
  const env = view?.kind === "env";
  return (
    <div className="split">
      <div className="filetree" data-testid="filetree" style={showTree ? undefined : { display: "none" }}>
        <Node projectId={pid} dir="" depth={0} onOpen={open} selected={active || ""} />
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
          ) : view && !EDITABLE.has(view.kind) && view.kind !== "withheld" ? (
            <span className="chip edro" data-testid="editor-readonly-reason" title={view.readonly || undefined}>
              🔒 {READONLY_LABEL[view.kind] || "read-only"}
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
            disabled={!view || !editable || !view.dirty || view.saving}
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
            busy={view.saving}
            onCompare={() => void toggleCompare()}
            onKeep={() => void buffers.keepMine(view.abs)}
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
          <div className={"edbody" + (md && !read && !comparing ? " mdsplit" : "")}
               data-testid={md && !read && !comparing ? "md-split" : undefined}
               style={!active || view?.kind === "withheld" ? { display: "none" } : undefined}>
            <PaneEditor
              model={model}
              viewKey={view?.abs ?? null}
              path={active || ""}
              readOnly={!editable}
              hidden={read || !!comparing || !model}
              onSave={() => void save()}
              editorRef={(ed) => {
                editorRef.current = ed;
              }}
            />
            {md && !read && !comparing ? (
              <div className="mdrender" data-testid="md-preview">
                <Markdown text={mdText} className="" />
              </div>
            ) : null}
            {md && read && !comparing ? (
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
        {view && view.kind !== "withheld" ? <StatusBar view={view} /> : null}
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

/** Encoding and line endings, and what a save does to mixed ones. */
function StatusBar(props: { view: BufferView }) {
  const v = props.view;
  const writes = v.modelEol || "LF";
  const eol = useMemo(() => (v.eol === "mixed" ? `mixed (saving writes ${writes} throughout)` : v.eol || writes), [v.eol, writes]);
  return (
    <div className="edstatus" data-testid="editor-status" data-eol={v.eol ?? ""}>
      <span>{v.encoding === "utf-8-bom" ? "UTF-8 with BOM" : v.encoding ? "UTF-8" : "not text"}</span>
      <span data-testid="editor-eol" className={v.eol === "mixed" ? "warnish" : undefined}>{eol}</span>
      {v.kind === "env" ? <span>env file · only you can read it</span> : null}
    </div>
  );
}
