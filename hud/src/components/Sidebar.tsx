// Projects, expanding to chat threads and tasks; a task expands to its
// threads with role and provider — the orchestration view (§12.2). Windows
// projects (`/mnt/<drive>/…`) carry a badge and the 9p caution as its title.
//
// **New thread is the pinned button at the top**, as in the Claude Code
// desktop app. It opens a compose row in the last project worked in. The
// project chip in the input bar (or dragging the compose row) changes the
// project before the first message, and nothing reaches the server until
// that message is sent. The sidebar never decides where a message goes. It
// shows the active conversation's project (`activeProjectId`), and a project
// row only expands and collapses. There used to be a stored "selected
// project" that drifted from the thread a send actually went to
// (lib/compose.ts).
//
// **Creating a project is a button, not a tree row.** A `+ New project` row at
// the bottom of a tree of projects reads as one of them; the owner did not
// find it. It is the `+` in the PROJECTS header.
//
// **A chat thread can be dragged to another project** — and can also be moved
// from a menu, because a drag is not reachable from the keyboard and "the only
// way to do X is a mouse gesture" is the same class of problem as the button
// nobody found. Both paths go through `lib/threads.ts`, so the rule that a
// task's threads move with their task is stated once: a task thread is not
// draggable, and its menu says why rather than letting the owner discover it
// from a 409.

//
// **A project row has a `⋯` menu (and the same on right-click)**: Edit…,
// Rename…, Archive… (decisions B). **Rename is inline** for project and thread
// rows — double-click the name, or ⋯ → Rename — and the box swallows its own
// clicks, so the single click that expands a row still does only that. The
// Inbox cannot be renamed or archived, and its menu says why. Archive is the
// way to put a project or thread away; deleting happens only from the
// Archive view, and only to what is already archived.

import { useEffect, useRef, useState } from "react";
import type { Project, Task, TaskThread, Thread } from "../types";
import { canMoveThread, wouldMove } from "../lib/threads";
import { composeMovable, folderName, movedAway, type Compose } from "../lib/compose";
import { projectNamesTaken, threadTitlesTaken } from "../lib/projects";
import { InlineRename } from "./InlineRename";

interface ProjectMenuAt {
  projectId: string;
  x: number;
  y: number;
}

const INBOX_WHY = "The Inbox is where unplaced chat lands: it keeps its name and cannot be archived.";

/** What a drag is carrying: a thread id, or the not-yet-sent compose row. */
const COMPOSE_DRAG = "jarvis/compose";

interface MenuAt {
  threadId: string;
  /** Set when the row was a *task's* thread, which `/threads` does not list. */
  taskId: string | null;
  x: number;
  y: number;
}

export function Sidebar(props: {
  projects: Project[];
  /** Archived projects' names: a rename preview counts them, as the backend does. */
  archivedNames?: string[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  /** The active conversation's project, derived, never stored. */
  activeProjectId: string | null;
  compose: Compose | null;
  threadId: string | null;
  taskId: string | null;
  moveError: string;
  onPickThread: (id: string) => void;
  onPickTask: (id: string) => void;
  onNewProject: () => void;
  onNewThread: () => void;
  /** The row's own project, never "whichever is selected". */
  onNewTask: (projectId: string) => void;
  onMoveThread: (threadId: string, projectId: string) => void;
  onMoveCompose: (projectId: string) => void;
  onOpen: (what: "schedules" | "usage" | "route") => void;
  // --- rename, edit, archive (decisions B); each resolves with the record
  // the backend saved, whose name may be numbered.
  onEditProject?: (projectId: string) => void;
  onArchiveProject?: (projectId: string) => void;
  onRenameProject?: (projectId: string, name: string) => Promise<Project>;
  onRenameThread?: (threadId: string, title: string) => Promise<Thread>;
  onArchiveThread?: (threadId: string) => Promise<unknown>;
  onOpenArchive?: () => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [openTasks, setOpenTasks] = useState<Record<string, boolean>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [menu, setMenu] = useState<MenuAt | null>(null);
  const [pmenu, setPmenu] = useState<ProjectMenuAt | null>(null);
  const [renaming, setRenaming] = useState<{ kind: "project" | "thread"; id: string } | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const pmenuFirst = useRef<HTMLButtonElement | null>(null);
  const pmenuOpener = useRef<HTMLElement | null>(null);
  const isOpen = (id: string) => expanded[id] ?? id === props.activeProjectId;
  const toggle = (id: string) => setExpanded((e) => ({ ...e, [id]: !(e[id] ?? id === props.activeProjectId) }));
  const reveal = (id: string) => setExpanded((e) => ({ ...e, [id]: true }));

  // The project holding the active conversation is always open: a new thread
  // or a picked thread must never sit inside a collapsed project.
  useEffect(() => {
    if (props.activeProjectId) reveal(props.activeProjectId);
  }, [props.activeProjectId, props.compose]);

  /** Whether the current drag would do anything on this project row. */
  const accepts = (projectId: string) =>
    dragging === COMPOSE_DRAG
      ? composeMovable(props.compose, projectId)
      : !!dragging && wouldMove(props.threads, dragging, projectId);

  // A menu that outlives what opened it is a menu that acts on the wrong row.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setMenu(null);
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc, true);
    };
  }, [menu]);

  // A task's threads are not in `/threads` — they are reached through the task
  // — so the menu carries the task id it was opened from and asks the same
  // `canMoveThread` about a stub. One rule, one place, whichever row asked.
  const menuThread = menu ? props.threads.find((t) => t.id === menu.threadId) || null : null;
  const movable = canMoveThread(
    menu?.taskId ? ({ id: menu.threadId, task_id: menu.taskId } as Thread) : menuThread,
  );

  const openMenu = (e: React.MouseEvent, id: string, taskId: string | null = null) => {
    e.preventDefault();
    e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setMenu({ threadId: id, taskId, x: Math.round(r.left), y: Math.round(r.bottom) });
  };

  // The project menu: same closing rules as the thread menu, plus focus goes
  // to its first item on open and back to the ⋯ that opened it on close.
  useEffect(() => {
    if (!pmenu) return;
    pmenuFirst.current?.focus();
    const close = () => setPmenu(null);
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setPmenu(null);
        pmenuOpener.current?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc, true);
    };
  }, [pmenu]);

  const openProjectMenu = (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    const el = e.currentTarget as HTMLElement;
    pmenuOpener.current = el.querySelector?.("button.menu") || el;
    const r = el.getBoundingClientRect();
    const x = e.type === "contextmenu" ? e.clientX : Math.round(r.left);
    const y = e.type === "contextmenu" ? e.clientY : Math.round(r.bottom);
    setMenu(null);
    setPmenu({ projectId: id, x, y });
  };

  // A refusal or a renumbered name explains itself, then gets out of the way.
  useEffect(() => {
    if (!note) return;
    const t = setTimeout(() => setNote(null), 6000);
    return () => clearTimeout(t);
  }, [note]);

  const saveRename = (kind: "project" | "thread", id: string, name: string) => {
    const go = kind === "project" ? props.onRenameProject?.(id, name) : props.onRenameThread?.(id, name);
    return Promise.resolve(go)
      .then((saved: any) => {
        const got = kind === "project" ? saved?.name : saved?.title;
        setNote(got && got !== name ? { text: `“${name}” is taken; saved as “${got}”.`, error: false } : null);
      })
      .catch((e) => setNote({ text: `Could not rename: ${e?.message || e}`, error: true }))
      .finally(() => setRenaming(null));
  };

  const archiveThread = (id: string) => {
    setMenu(null);
    Promise.resolve(props.onArchiveThread?.(id))
      .then(() => setNote({ text: "Archived. Restore it from Archive at any time.", error: false }))
      .catch((e) => setNote({ text: `Could not archive: ${e?.message || e}`, error: true }));
  };

  const pmenuProject = pmenu ? props.projects.find((p) => p.id === pmenu.projectId) || null : null;

  return (
    <div className="pane" id="sidebar">
      <button type="button" id="newthread" data-testid="new-thread" onClick={props.onNewThread}>
        <span className="plus">+</span> New thread
      </button>
      <div className="barrow">
        <h2 className="bar">Projects</h2>
        <button
          type="button"
          id="newproject"
          data-testid="new-project"
          title="New project"
          aria-label="New project"
          onClick={props.onNewProject}
        >
          +
        </button>
      </div>
      {props.moveError ? (
        <div className="err movenote" data-testid="move-error">{props.moveError}</div>
      ) : null}
      {note ? (
        <div className={(note.error ? "err " : "muted small ") + "movenote"} data-testid="rename-note">
          {note.text}
        </div>
      ) : null}
      <div className="scroll" data-testid="sidebar">
        {props.projects.map((p) => {
          const plat = props.platforms[p.id];
          const open = isOpen(p.id);
          const composing = !!props.compose && props.compose.projectId === p.id && !props.threadId;
          const chats = props.threads.filter((t) => t.project_id === p.id && !t.task_id);
          const tasks = props.tasks.filter((t) => t.project_id === p.id);
          const target = over === p.id;
          return (
            <div key={p.id}>
              <div
                className={
                  "tree-row" +
                  (p.id === props.activeProjectId ? " sel" : "") +
                  (target ? " droptarget" : "")
                }
                data-testid={`project-${p.id}`}
                onClick={() => toggle(p.id)}
                onContextMenu={(e) => openProjectMenu(e, p.id)}
                onDragOver={(e) => {
                  // preventDefault is what makes a drop legal at all, so the
                  // row only accepts a drag that would actually change something.
                  if (accepts(p.id)) {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "move";
                    if (!target) setOver(p.id);
                  }
                }}
                onDragLeave={() => setOver((o) => (o === p.id ? null : o))}
                onDrop={(e) => {
                  e.preventDefault();
                  const id = e.dataTransfer.getData("text/plain") || dragging;
                  setOver(null);
                  setDragging(null);
                  if (!id) return;
                  // The target opens, so what was dropped is still on screen.
                  reveal(p.id);
                  if (id === COMPOSE_DRAG) {
                    if (composeMovable(props.compose, p.id)) props.onMoveCompose(p.id);
                  } else {
                    props.onMoveThread(id, p.id);
                  }
                }}
              >
                <span className="tw">{open ? "▾" : "▸"}</span>
                {renaming?.kind === "project" && renaming.id === p.id ? (
                  <InlineRename
                    value={p.name}
                    taken={projectNamesTaken(props.projects, p.id, props.archivedNames)}
                    testid={`rename-project-${p.id}`}
                    onSave={(name) => saveRename("project", p.id, name)}
                    onCancel={() => setRenaming(null)}
                  />
                ) : (
                  <span
                    className="nm"
                    data-testid={`project-name-${p.id}`}
                    title={p.inbox ? undefined : "double-click to rename"}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      if (!p.inbox && props.onRenameProject) setRenaming({ kind: "project", id: p.id });
                    }}
                  >
                    {p.name}
                  </span>
                )}
                {plat?.platform === "windows" ? (
                  <span className="badge win" title={plat.note || "Windows path worked from WSL"}>
                    WIN
                  </span>
                ) : null}
                <button
                  type="button"
                  className="menu"
                  data-testid={`project-menu-${p.id}`}
                  title="edit, rename, archive…"
                  aria-label={`${p.name}: project menu`}
                  onClick={(e) => openProjectMenu(e, p.id)}
                >
                  ⋯
                </button>
              </div>
              {open ? (
                <>
                  {composing ? (
                    <div
                      className={"tree-row indent-1 ghost sel" + (dragging === COMPOSE_DRAG ? " dragging" : "")}
                      data-testid="compose-row"
                      title="Not sent yet. Change its project with the chip in the input bar, or drag it."
                      draggable={!props.compose?.openedId}
                      onDragStart={(e) => {
                        e.dataTransfer.setData("text/plain", COMPOSE_DRAG);
                        e.dataTransfer.effectAllowed = "move";
                        setDragging(COMPOSE_DRAG);
                      }}
                      onDragEnd={() => {
                        setDragging(null);
                        setOver(null);
                      }}
                    >
                      <span className="tw">·</span>
                      <span className="nm">New thread</span>
                    </div>
                  ) : null}
                  {chats.map((t) => (
                    <div
                      key={t.id}
                      className={
                        "tree-row indent-1" +
                        (t.id === props.threadId ? " sel" : "") +
                        (dragging === t.id ? " dragging" : "")
                      }
                      data-testid={`thread-${t.id}`}
                      draggable={!(renaming?.kind === "thread" && renaming.id === t.id)}
                      onDragStart={(e) => {
                        e.dataTransfer.setData("text/plain", t.id);
                        e.dataTransfer.effectAllowed = "move";
                        setDragging(t.id);
                      }}
                      onDragEnd={() => {
                        setDragging(null);
                        setOver(null);
                      }}
                      onContextMenu={(e) => openMenu(e, t.id)}
                      onClick={() => props.onPickThread(t.id)}
                      title={t.cwd ? `works in ${t.cwd}` : undefined}
                    >
                      <span className="tw">·</span>
                      {renaming?.kind === "thread" && renaming.id === t.id ? (
                        <InlineRename
                          value={t.title || ""}
                          taken={threadTitlesTaken(props.threads, p.id, t.id)}
                          testid={`rename-thread-${t.id}`}
                          onSave={(title) => saveRename("thread", t.id, title)}
                          onCancel={() => setRenaming(null)}
                        />
                      ) : (
                        <span
                          className="nm"
                          data-testid={`thread-name-${t.id}`}
                          onDoubleClick={(e) => {
                            e.stopPropagation();
                            if (props.onRenameThread) setRenaming({ kind: "thread", id: t.id });
                          }}
                        >
                          {t.title || t.id}
                        </span>
                      )}
                      {movedAway(t, p) ? (
                        <span className="moved" data-testid={`moved-${t.id}`}>↪ {folderName(t.cwd)}</span>
                      ) : null}
                      <span className={`badge ${t.provider}`}>{t.provider}</span>
                      <button
                        type="button"
                        className="menu"
                        data-testid={`thread-menu-${t.id}`}
                        title="move to…"
                        onClick={(e) => openMenu(e, t.id)}
                      >
                        ⋯
                      </button>
                    </div>
                  ))}
                  <div
                    className="tree-row indent-1 muted small"
                    data-testid={`new-task-${p.id}`}
                    onClick={() => props.onNewTask(p.id)}
                  >
                    <span className="tw">+</span>
                    <span className="nm">new task</span>
                  </div>
                  {tasks.map((t) => {
                    const tOpen = openTasks[t.id] ?? t.id === props.taskId;
                    const kids = props.taskThreads[t.id] || [];
                    return (
                      <div key={t.id}>
                        <div
                          className={"tree-row indent-1" + (t.id === props.taskId ? " sel" : "")}
                          data-testid={`task-${t.id}`}
                          onClick={() => {
                            props.onPickTask(t.id);
                            setOpenTasks((o) => ({ ...o, [t.id]: !tOpen }));
                          }}
                        >
                          <span className="tw">{tOpen ? "▾" : "▸"}</span>
                          <span className="nm">{t.brief.slice(0, 40)}</span>
                          <span className={`phase ${t.state}`}>{t.state}</span>
                        </div>
                        {tOpen
                          ? kids.map((k) => (
                              <div
                                key={k.thread_id}
                                className={
                                  "tree-row indent-2" + (k.thread_id === props.threadId ? " sel" : "")
                                }
                                data-testid={`taskthread-${k.thread_id}`}
                                // Not draggable, and the menu says why: tasks
                                // themselves move, their threads do not.
                                onContextMenu={(e) => openMenu(e, k.thread_id, t.id)}
                                onClick={() => props.onPickThread(k.thread_id)}
                              >
                                <span className="tw">·</span>
                                <span className="nm">{k.role}</span>
                                <span className={`badge ${k.provider}`}>{k.provider}</span>
                                <button
                                  type="button"
                                  className="menu"
                                  data-testid={`thread-menu-${k.thread_id}`}
                                  onClick={(e) => openMenu(e, k.thread_id, t.id)}
                                >
                                  ⋯
                                </button>
                              </div>
                            ))
                          : null}
                      </div>
                    );
                  })}
                </>
              ) : null}
            </div>
          );
        })}
      </div>
      <div className="sidebar-foot" style={{ flex: "0 0 auto" }}>
        <div className="tree-row" data-testid="open-schedules" onClick={() => props.onOpen("schedules")}>
          <span className="tw">⏱</span>
          <span className="nm">Schedules</span>
        </div>
        <div className="tree-row" data-testid="open-usage" onClick={() => props.onOpen("usage")}>
          <span className="tw">$</span>
          <span className="nm">Usage</span>
        </div>
        <div className="tree-row" data-testid="open-route" onClick={() => props.onOpen("route")}>
          <span className="tw">⇄</span>
          <span className="nm">Route</span>
        </div>
        {props.onOpenArchive ? (
          <div className="tree-row" data-testid="open-archive" onClick={props.onOpenArchive}>
            <span className="tw">▤</span>
            <span className="nm">Archive</span>
          </div>
        ) : null}
      </div>

      {pmenu && pmenuProject ? (
        <div
          className="threadmenu"
          data-testid="project-menu"
          role="menu"
          style={{ left: pmenu.x, top: pmenu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <div className="mhead">{pmenuProject.name}</div>
          <button
            type="button"
            className="mrow"
            ref={pmenuFirst}
            role="menuitem"
            style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                     background: "none", textTransform: "none", letterSpacing: 0 }}
            data-testid="pmenu-edit"
            onClick={() => {
              setPmenu(null);
              props.onEditProject?.(pmenuProject.id);
            }}
          >
            Edit…
          </button>
          <button
            type="button"
            className="mrow"
            role="menuitem"
            disabled={pmenuProject.inbox}
            style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                     background: "none", textTransform: "none", letterSpacing: 0 }}
            data-testid="pmenu-rename"
            onClick={() => {
              setPmenu(null);
              reveal(pmenuProject.id);
              setRenaming({ kind: "project", id: pmenuProject.id });
            }}
          >
            Rename…
          </button>
          <div className="msep" />
          <button
            type="button"
            className="mrow danger"
            role="menuitem"
            disabled={pmenuProject.inbox}
            style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                     background: "none", textTransform: "none", letterSpacing: 0 }}
            data-testid="pmenu-archive"
            onClick={() => {
              setPmenu(null);
              props.onArchiveProject?.(pmenuProject.id);
            }}
          >
            Archive…
          </button>
          {pmenuProject.inbox ? (
            <div className="mwhy" data-testid="pmenu-inbox-why">{INBOX_WHY}</div>
          ) : null}
        </div>
      ) : null}

      {menu ? (
        <div
          className="threadmenu"
          data-testid="thread-menu"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {movable.ok && menuThread ? (
            <>
              <button
                type="button"
                className="mrow"
                style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                         background: "none", textTransform: "none", letterSpacing: 0 }}
                data-testid="tmenu-rename"
                onClick={() => {
                  setMenu(null);
                  reveal(menuThread.project_id);
                  setRenaming({ kind: "thread", id: menuThread.id });
                }}
              >
                Rename…
              </button>
              <button
                type="button"
                className="mrow danger"
                style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                         background: "none", textTransform: "none", letterSpacing: 0 }}
                data-testid="tmenu-archive"
                onClick={() => archiveThread(menuThread.id)}
              >
                Archive…
              </button>
              <div className="msep" />
            </>
          ) : null}
          <div className="mhead">Move to…</div>
          {movable.ok ? (
            props.projects
              .filter((p) => p.id !== menuThread?.project_id)
              .map((p) => (
                <button
                  type="button"
                  className="mrow"
                  key={p.id}
                  style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                           background: "none", textTransform: "none", letterSpacing: 0 }}
                  data-testid={`move-to-${p.id}`}
                  onClick={() => {
                    setMenu(null);
                    props.onMoveThread(menu.threadId, p.id);
                  }}
                >
                  {p.name}
                </button>
              ))
          ) : (
            <div className="mwhy" data-testid="move-refused">{movable.why}</div>
          )}
          {movable.ok && props.projects.length < 2 ? (
            <div className="mwhy">nowhere else to put it yet</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
