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
//
// **The dot left of each thread and task is what it is doing** (2026-10-08,
// lib/activity.ts): `·` idle, a pulsing ring and a sweeping row while it
// works, yellow when it waits for the owner, blue when it finished unread, a
// red ⚠ when it failed. A task row shows its phase this way too (the phase
// word is its tooltip), and a collapsed project shows its most urgent one at
// the row's end. Each dot has a fixed slot: no status moves a name.
//
// **Several chats at once (WP-B, 2026-10-09).** Every drawn chat pane's
// conversation is marked: the active one (the voice target, the chat used
// last) with the accent, the others with a dimmer mark, each row carrying the
// pane it is open in (`data-pane`). Each composing chat pane has its own
// compose row, numbered by pane when more than one chat pane is drawn. A
// click on a thread another pane shows focuses that pane — a thread is open
// in one pane at most; Alt+click, or "Open beside" in the row's ⋯ menu, opens
// it in the next pane to the right (from one pane, two columns).

import { useEffect, useRef, useState } from "react";
import type { Project, Task, TaskThread, Thread } from "../types";
import { canMoveThread, wouldMove } from "../lib/threads";
import { composeMovable, folderName, movedAway, type Compose } from "../lib/compose";
import type { PaneNo } from "../lib/workspace";
import { shortId, threadTooltip } from "../lib/threadmodel";
import { projectNamesTaken, threadTitlesTaken } from "../lib/projects";
import { InlineRename } from "./InlineRename";
import { CollapseButton } from "./Layout";
import { toCss } from "../lib/layout";
import { ACTIVITY_LABEL, NO_ACTIVITY, projectActivity, type ActivityStatus, type ActivityView } from "../lib/activity";

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
  /** What each thread and task is doing (lib/activity.ts). */
  activity?: ActivityView;
  /** The active conversation's project, derived, never stored. */
  activeProjectId: string | null;
  /** The threads open in drawn chat panes, the active one (the voice target) marked. */
  open: { pane: PaneNo; threadId: string; active: boolean }[];
  /** The drawn chat panes composing a new thread, each with its row's label. */
  composing: { pane: PaneNo; compose: Compose; label: string; active: boolean }[];
  taskId: string | null;
  moveError: string;
  /** `beside`: Alt+click or "Open beside" — the next pane to the right. */
  onPickThread: (id: string, beside?: boolean) => void;
  onPickTask: (id: string) => void;
  onNewProject: () => void;
  onNewThread: () => void;
  /** The row's own project, never "whichever is selected". */
  onNewTask: (projectId: string) => void;
  onMoveThread: (threadId: string, projectId: string) => void;
  onMoveCompose: (projectId: string, pane: PaneNo) => void;
  onOpen: (what: "schedules" | "usage" | "route") => void;
  // --- rename, edit, archive (decisions B); each resolves with the record
  // the backend saved, whose name may be numbered.
  onEditProject?: (projectId: string) => void;
  onArchiveProject?: (projectId: string) => void;
  onRenameProject?: (projectId: string, name: string) => Promise<Project>;
  onRenameThread?: (threadId: string, title: string) => Promise<Thread>;
  onArchiveThread?: (threadId: string) => Promise<unknown>;
  onOpenArchive?: () => void;
  /** The HUD zoom in percent: a menu placed from a screen rect is placed in zoomed space. */
  zoom?: number;
  /** Folds the sidebar to its rail (2026-10-08). */
  onCollapse?: () => void;
  /** An authorization card is up: the fold button is disabled (it would still answer Enter). */
  layoutBlocked?: boolean;
}) {
  const z = props.zoom ?? 100;
  const activity = props.activity ?? NO_ACTIVITY;
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [openTasks, setOpenTasks] = useState<Record<string, boolean>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  // The pane whose compose row is being dragged (the drag itself carries only COMPOSE_DRAG).
  const [dragPane, setDragPane] = useState<PaneNo | null>(null);
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
  const activeCompose = props.composing.find((c) => c.active)?.compose ?? null;
  useEffect(() => {
    if (props.activeProjectId) reveal(props.activeProjectId);
  }, [props.activeProjectId, activeCompose]);
  // And so is each other drawn chat pane's (only a split has any).
  const others = [
    ...props.open.filter((o) => !o.active).map((o) => props.threads.find((t) => t.id === o.threadId)?.project_id),
    ...props.composing.filter((c) => !c.active).map((c) => c.compose.projectId),
  ].filter((id): id is string => !!id);
  const othersKey = others.join(",");
  useEffect(() => {
    for (const id of others) reveal(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [othersKey]);

  /** Where a thread row is open: the active pane's accent, another pane's dimmer mark. */
  const openIn = (threadId: string) => props.open.find((o) => o.threadId === threadId) || null;
  const selClass = (threadId: string) => {
    const at = openIn(threadId);
    return at ? (at.active ? " sel" : " panesel") : "";
  };

  const draggedCompose = props.composing.find((c) => c.pane === dragPane)?.compose ?? null;
  /** Whether the current drag would do anything on this project row. */
  const accepts = (projectId: string) =>
    dragging === COMPOSE_DRAG
      ? composeMovable(draggedCompose, projectId)
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
    setMenu({ threadId: id, taskId, x: Math.round(toCss(r.left, z)), y: Math.round(toCss(r.bottom, z)) });
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
    // Screen pixels in, zoomed pixels out: the menu's `left` is scaled again.
    const x = Math.round(toCss(e.type === "contextmenu" ? e.clientX : r.left, z));
    const y = Math.round(toCss(e.type === "contextmenu" ? e.clientY : r.bottom, z));
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
      <div className="sidetop">
        <button type="button" id="newthread" data-testid="new-thread" onClick={props.onNewThread}>
          <span className="plus">+</span> New thread
        </button>
        {props.onCollapse ? (
          <CollapseButton side="left" onCollapse={props.onCollapse} disabled={props.layoutBlocked} />
        ) : null}
      </div>
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
          const composing = props.composing.filter((c) => c.compose.projectId === p.id);
          const chats = props.threads.filter((t) => t.project_id === p.id && !t.task_id);
          const tasks = props.tasks.filter((t) => t.project_id === p.id);
          const target = over === p.id;
          // Folded, the project row carries the most urgent dot of its rows.
          const folded = open ? "idle" : projectActivity(activity, chats.map((t) => t.id), tasks.map((t) => t.id));
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
                  setDragPane(null);
                  if (!id) return;
                  // The target opens, so what was dropped is still on screen.
                  reveal(p.id);
                  if (id === COMPOSE_DRAG) {
                    if (dragPane && composeMovable(draggedCompose, p.id)) props.onMoveCompose(p.id, dragPane);
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
                {/* At the row's end, always there: a dot coming or going never
                    moves the name, and the name stays level with the others. */}
                <span className="slot end">
                  {folded !== "idle" ? <Dot status={folded} testid={`project-activity-${p.id}`} /> : null}
                </span>
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
                  {composing.map((c) => (
                    <div
                      key={`compose-${c.pane}`}
                      className={
                        "tree-row indent-1 ghost" + (c.active ? " sel" : " panesel") +
                        (dragging === COMPOSE_DRAG && dragPane === c.pane ? " dragging" : "")
                      }
                      data-testid="compose-row"
                      data-pane={c.pane}
                      title="Not sent yet. Change its project with the chip in the input bar, or drag it."
                      draggable={!c.compose.openedId}
                      onDragStart={(e) => {
                        e.dataTransfer.setData("text/plain", COMPOSE_DRAG);
                        e.dataTransfer.effectAllowed = "move";
                        setDragging(COMPOSE_DRAG);
                        setDragPane(c.pane);
                      }}
                      onDragEnd={() => {
                        setDragging(null);
                        setDragPane(null);
                        setOver(null);
                      }}
                    >
                      <span className="tw">·</span>
                      <span className="nm">{c.label}</span>
                    </div>
                  ))}
                  {chats.map((t) => (
                    <div
                      key={t.id}
                      className={
                        "tree-row indent-1" +
                        selClass(t.id) +
                        (dragging === t.id ? " dragging" : "") +
                        (activity.threads[t.id] === "working" ? " act-working" : "")
                      }
                      data-testid={`thread-${t.id}`}
                      data-pane={openIn(t.id)?.pane}
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
                      onClick={(e) => props.onPickThread(t.id, e.altKey)}
                      title={threadTooltip(t, t.cwd)}
                    >
                      <span className="tw">
                        <Dot status={activity.threads[t.id] || "idle"} testid={`activity-${t.id}`} />
                      </span>
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
                      {t.model ? (
                        <span className="badge pin" data-testid={`pin-${t.id}`}>{shortId(t.model)}</span>
                      ) : null}
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
                          className={
                            "tree-row indent-1" +
                            (t.id === props.taskId ? " sel" : "") +
                            (activity.tasks[t.id] === "working" ? " act-working" : "")
                          }
                          data-testid={`task-${t.id}`}
                          data-state={t.state}
                          title={t.state}
                          onClick={() => {
                            props.onPickTask(t.id);
                            setOpenTasks((o) => ({ ...o, [t.id]: !tOpen }));
                          }}
                        >
                          <span className="tw">{tOpen ? "▾" : "▸"}</span>
                          <span className="slot">
                            <Dot status={activity.tasks[t.id] || "idle"} testid={`activity-${t.id}`} />
                          </span>
                          <span className="nm">{t.brief.slice(0, 40)}</span>
                        </div>
                        {tOpen
                          ? kids.map((k) => (
                              <div
                                key={k.thread_id}
                                className={
                                  "tree-row indent-2" +
                                  selClass(k.thread_id) +
                                  (activity.threads[k.thread_id] === "working" ? " act-working" : "")
                                }
                                data-testid={`taskthread-${k.thread_id}`}
                                data-pane={openIn(k.thread_id)?.pane}
                                // Not draggable, and the menu says why: tasks
                                // themselves move, their threads do not.
                                onContextMenu={(e) => openMenu(e, k.thread_id, t.id)}
                                onClick={(e) => props.onPickThread(k.thread_id, e.altKey)}
                              >
                                <span className="tw">
                                  <Dot
                                    status={activity.threads[k.thread_id] || "idle"}
                                    testid={`activity-${k.thread_id}`}
                                  />
                                </span>
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
          <button
            type="button"
            className="mrow"
            style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                     background: "none", textTransform: "none", letterSpacing: 0 }}
            data-testid="tmenu-beside"
            title="Open it in the next pane to the right (Alt+click)"
            onClick={() => {
              setMenu(null);
              props.onPickThread(menu.threadId, true);
            }}
          >
            Open beside
          </button>
          <div className="msep" />
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

/** One row's status. Idle is the plain `·` it always was; the rest carry a
 * label for the tooltip and for a screen reader. */
function Dot({ status, testid }: { status: ActivityStatus; testid: string }) {
  if (status === "idle") {
    return <span className="act idle" data-testid={testid} data-status="idle">·</span>;
  }
  const label = ACTIVITY_LABEL[status];
  return (
    <span
      className={`act ${status}`}
      data-testid={testid}
      data-status={status}
      title={label}
      role="img"
      aria-label={label}
    >
      {status === "failed" ? (
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <path d="M6 0.8 L11.6 11 H0.4 Z" />
          <rect x="5.25" y="4" width="1.5" height="3.8" rx="0.5" />
          <rect x="5.25" y="8.6" width="1.5" height="1.5" rx="0.6" />
        </svg>
      ) : null}
    </span>
  );
}
