// The File tab: the project tree, a file in Monaco, a save guarded by mtime,
// and markdown in a split view (editor left, rendered right, live) with a
// rendered-only toggle for reading docs.
//
// A protected credential name comes back `{"protected": true}` with **no
// content**, 200 — so the tab says "withheld" and there is nothing to show,
// which is the v1 secrets layer's answer rather than an error the owner has to
// interpret.
//
// The mtime guard is the read-before-write rule with a second writer in mind:
// a 409 means someone else wrote the file since it was read, and the only safe
// answer is to reload rather than to clobber.
//
// **A File pane is one project's, for as long as it is mounted** (2026-10-09).
// The tab used to save to `pid + file.path` with whatever project was current
// *at save time*, so a file pane left open while the chat moved to another
// project would have written its buffer into the wrong project. Now the pane
// pins its project when it opens a file (`onOpened`), and App keys this
// component by that project: a different project is a fresh mount with
// nothing open, so a save can never land anywhere but where it was read.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Tree, TreeEntry } from "../types";
import { CodeEditor } from "./Editor";
import { Markdown } from "./Markdown";
import { isMarkdown } from "../lib/monaco";
import { afterSave, fileSaved, onFileSaved } from "../lib/filesync";

interface Open {
  path: string;
  content: string;
  mtime: number;
  protected: boolean;
}

function join(dir: string, name: string) {
  return dir ? `${dir}/${name}` : name;
}

function Node(props: {
  projectId: string;
  dir: string;
  depth: number;
  onOpen: (path: string) => void;
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
        if (e.kind === "dir") {
          const isOpen = !!open[path];
          return (
            <div key={path}>
              <div className={cls} onClick={() => setOpen({ ...open, [path]: !isOpen })}>
                <span className="tw">{isOpen ? "▾" : "▸"}</span>
                <span className="nm">{e.name}</span>
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
            className={cls + (props.selected === path ? " sel" : "")}
            data-testid={`file-${path}`}
            onClick={() => props.onOpen(path)}
          >
            <span className="tw">·</span>
            <span className="nm">{e.name}</span>
          </div>
        );
      })}
    </>
  );
}

export function FileTab(props: {
  projectId: string | null;
  /** A file opened here: the pane pins itself to this project and remembers the file (WP-G2). */
  onOpened?: (projectId: string, path: string) => void;
  /** The file to open when this mounts (a reload, a split's copy), relative to the project. */
  filePath?: string;
  /** A narrow split pane: the tree folds behind a toggle. */
  narrow?: boolean;
  /** The buffer holds an unsaved edit (or no longer does). Told false on unmount. */
  onDirty?: (dirty: boolean) => void;
}) {
  const [file, setFile] = useState<Open | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [renderedOnly, setRenderedOnly] = useState(false);
  const [treeOpen, setTreeOpen] = useState(false);
  // Opening another file (or Reload) over an unsaved edit: what was asked for.
  const [ask, setAsk] = useState<{ path: string; reload: boolean } | null>(null);
  // Read once, at mount: the key App gives this component is the project, so
  // a prop that changed under an open buffer would be a bug, not a feature.
  const [pid] = useState(props.projectId);
  const onOpened = props.onOpened;
  // This pane, to the other File panes it tells about its saves (lib/filesync.ts).
  const [me] = useState(() => ({}));
  // Which buffer this is: bumped each time a file is read in. A save's
  // answer lands only on the buffer it was sent from — "Discard and open"
  // during a save used to put the old path back over the new file's text,
  // and the next save wrote that text to the old file (review of PR #43).
  const generation = useRef(0);
  const fileRef = useRef(file);
  fileRef.current = file;
  // An unsaved edit is said out loud, so App can refuse to close this
  // buffer behind the owner's back (a "follow chat", a project picked in the
  // header, a project that went away, a window closing).
  const tell = useRef(props.onDirty);
  tell.current = props.onDirty;

  const read = useCallback(
    async (path: string) => {
      if (!pid) return;
      setNote("");
      setAsk(null);
      try {
        const r = await api.readFile(pid, path);
        generation.current += 1;
        setFile({ path: r.path, content: r.content || "", mtime: r.mtime, protected: !!r.protected });
        setText(r.content || "");
        setRenderedOnly(isMarkdown(path));
        setTreeOpen(false);
        onOpened?.(pid, r.path);
      } catch (e: any) {
        setNote(e.message);
      }
    },
    [pid, onOpened],
  );

  // An unsaved edit is never dropped without a word (ED-1): opening another
  // file, or Reload, over one asks first, and "keep editing" leaves the
  // buffer exactly as it was.
  const dirty = !!file && !file.protected && text !== file.content;
  const open = (path: string) => {
    if (dirty && file) setAsk({ path, reload: path === file.path });
    else void read(path);
  };

  // `content` is the editor's own text when Ctrl+S saves: a keystroke's
  // re-render may not have landed in `text` yet.
  const save = async (content: string = text) => {
    const sent = fileRef.current;
    if (!pid || !sent || sent.protected || content === sent.content) return;
    const gen = generation.current;
    try {
      const r = await api.writeFile(pid, {
        path: sent.path,
        content,
        expected_mtime: sent.mtime,
      });
      // Another file was read in while this was on its way: its answer is
      // not this buffer's.
      if (generation.current !== gen) return;
      setFile((cur) => (cur && cur.path === sent.path ? { ...cur, mtime: r.mtime, content } : cur));
      setNote("saved");
      fileSaved({ projectId: pid, path: sent.path, mtime: r.mtime }, me);
    } catch (e: any) {
      if (generation.current !== gen) return;
      if (e instanceof ApiError && e.status === 409) {
        // Someone else wrote it. Never clobber: offer the reload.
        setNote("changed on disk since it was read — reload to see it, then re-apply your edit");
      } else {
        setNote(e.message);
      }
    }
  };

  // Said as it happens, never a frame later: a keystroke tells App from the
  // editor's own change event (`edit`, below), and every other change of
  // `dirty` (a save, a read) in a layout effect, before the window paints.
  // A passive effect left 22–44 ms in which Save was enabled but App still
  // thought the pane clean, and a project picked in that window remounted
  // the pane and dropped the edit (review of PR #43).
  useLayoutEffect(() => tell.current?.(dirty), [dirty]);
  useEffect(() => () => tell.current?.(false), []);
  const edit = useCallback((value: string) => {
    setText(value);
    const f = fileRef.current;
    tell.current?.(!!f && !f.protected && value !== f.content);
  }, []);

  // The file this pane had open (a reload) or was split from: read at mount.
  const first = useRef(props.filePath);
  useEffect(() => {
    if (first.current) void open(first.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A save in another pane on this file (lib/filesync.ts): read it again when
  // clean; with an unsaved edit, keep it and say the file changed on disk.
  const mine = useRef({ projectId: pid, path: null as string | null, mtime: null as number | null, dirty: false });
  mine.current = { projectId: pid, path: file?.path ?? null, mtime: file?.mtime ?? null, dirty };
  const reread = useRef(read);
  reread.current = read;
  useEffect(() => onFileSaved((saved, from) => {
    const act = afterSave(saved, mine.current, from === me);
    if (act === "reload") void reread.current(saved.path);
    else if (act === "note") setNote("changed on disk since it was read — reload to see it, then re-apply your edit");
  }), [me]);

  if (!pid) return <div className="pad muted">Pick a project.</div>;

  const showTree = !props.narrow || treeOpen || !file;
  return (
    <div className="split">
      <div className="filetree" data-testid="filetree" style={showTree ? undefined : { display: "none" }}>
        <Node projectId={pid} dir="" depth={0} onOpen={open} selected={file?.path || ""} />
      </div>
      <div className="editorwrap">
        <div className="toolbar">
          {props.narrow && file ? (
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
          <span className="path" data-testid="file-path">{file?.path || "no file open"}</span>
          {file && isMarkdown(file.path) ? (
            <button type="button" data-testid="toggle-rendered" onClick={() => setRenderedOnly((v) => !v)}>
              {renderedOnly ? "Edit" : "Read"}
            </button>
          ) : null}
          <button
            type="button"
            data-testid="file-reload"
            disabled={!file}
            onClick={() => file && open(file.path)}
          >
            Reload
          </button>
          <button
            type="button"
            data-testid="file-save"
            disabled={!file || file.protected || text === file.content}
            onClick={() => void save()}
          >
            Save
          </button>
          {note ? <span className="err" data-testid="file-note">{note}</span> : null}
        </div>
        {ask && file ? (
          <div className="toolbar warn" data-testid="file-discard-ask" role="alert">
            <span>
              {file.path} has an unsaved edit.{" "}
              {ask.reload ? "Reloading throws it away." : `Opening ${ask.path} throws it away.`}
            </span>
            <button type="button" data-testid="file-discard-no" onClick={() => setAsk(null)}>
              Keep editing
            </button>
            <button type="button" data-testid="file-discard-yes" onClick={() => void read(ask.path)}>
              {ask.reload ? "Discard and reload" : "Discard and open"}
            </button>
          </div>
        ) : null}
        {!file ? (
          <div className="pad muted">Open a file from the tree.</div>
        ) : file.protected ? (
          // No content ever arrives for these, and the tab says so rather than
          // showing an empty editor that looks like an empty file.
          <div className="pad" data-testid="file-withheld">
            <b>withheld</b> — this is a protected credential file. Its contents are never sent to
            this window.
          </div>
        ) : isMarkdown(file.path) && renderedOnly ? (
          <div className="mdrender" data-testid="md-rendered">
            <Markdown text={text} className="" />
          </div>
        ) : isMarkdown(file.path) ? (
          <div className="mdsplit" data-testid="md-split">
            <CodeEditor projectId={pid} path={file.path} value={text} onChange={edit} onSave={save} />
            <div className="mdrender" data-testid="md-preview">
              <Markdown text={text} className="" />
            </div>
          </div>
        ) : (
          <CodeEditor projectId={pid} path={file.path} value={text} onChange={edit} onSave={save} />
        )}
      </div>
    </div>
  );
}
