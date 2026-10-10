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

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Tree, TreeEntry } from "../types";
import { CodeEditor } from "./Editor";
import { Markdown } from "./Markdown";
import { isMarkdown } from "../lib/monaco";

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
  /** A file opened here: the pane pins itself to this project. */
  onOpened?: (projectId: string) => void;
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
  // Read once, at mount: the key App gives this component is the project, so
  // a prop that changed under an open buffer would be a bug, not a feature.
  const [pid] = useState(props.projectId);
  const onOpened = props.onOpened;

  const open = useCallback(
    async (path: string) => {
      if (!pid) return;
      setNote("");
      try {
        const r = await api.readFile(pid, path);
        setFile({ path: r.path, content: r.content || "", mtime: r.mtime, protected: !!r.protected });
        setText(r.content || "");
        setRenderedOnly(isMarkdown(path));
        setTreeOpen(false);
        onOpened?.(pid);
      } catch (e: any) {
        setNote(e.message);
      }
    },
    [pid, onOpened],
  );

  const save = async () => {
    if (!pid || !file || file.protected) return;
    try {
      const r = await api.writeFile(pid, {
        path: file.path,
        content: text,
        expected_mtime: file.mtime,
      });
      setFile({ ...file, mtime: r.mtime, content: text });
      setNote("saved");
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409) {
        // Someone else wrote it. Never clobber: offer the reload.
        setNote("changed on disk since it was read — reload to see it, then re-apply your edit");
      } else {
        setNote(e.message);
      }
    }
  };

  // An unsaved edit is said out loud, so App can refuse to close this
  // buffer behind the owner's back (a "follow chat", a project that went
  // away, a window closing). The minimal guard; the editor plan does more.
  const dirty = !!file && !file.protected && text !== file.content;
  const tell = useRef(props.onDirty);
  tell.current = props.onDirty;
  useEffect(() => tell.current?.(dirty), [dirty]);
  useEffect(() => () => tell.current?.(false), []);

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
            onClick={save}
          >
            Save
          </button>
          {note ? <span className="err" data-testid="file-note">{note}</span> : null}
        </div>
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
            <CodeEditor path={file.path} value={text} onChange={setText} />
            <div className="mdrender" data-testid="md-preview">
              <Markdown text={text} className="" />
            </div>
          </div>
        ) : (
          <CodeEditor path={file.path} value={text} onChange={setText} />
        )}
      </div>
    </div>
  );
}
