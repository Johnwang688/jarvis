// Projects, expanding to chat threads and tasks; a task expands to its
// threads with role and provider — the orchestration view (§12.2). Windows
// projects (`/mnt/<drive>/…`) carry a badge and the 9p caution as its title.
//
// Two things the owner's first use asked for live here.
//
// **Creating a project is a button, not a tree row.** A `+ New project` row at
// the bottom of a tree of projects reads as one of them; the owner did not
// find it. It is now the first thing in the pane and looks like a control.
//
// **A chat thread can be dragged to another project** — and can also be moved
// from a menu, because a drag is not reachable from the keyboard and "the only
// way to do X is a mouse gesture" is the same class of problem as the button
// nobody found. Both paths go through `lib/threads.ts`, so the rule that a
// task's threads move with their task is stated once: a task thread is not
// draggable, and its menu says why rather than letting the owner discover it
// from a 409.

import { useEffect, useState } from "react";
import type { Project, Task, TaskThread, Thread } from "../types";
import { canMoveThread, wouldMove } from "../lib/threads";

interface MenuAt {
  threadId: string;
  /** Set when the row was a *task's* thread, which `/threads` does not list. */
  taskId: string | null;
  x: number;
  y: number;
}

export function Sidebar(props: {
  projects: Project[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  projectId: string | null;
  threadId: string | null;
  taskId: string | null;
  moveError: string;
  onPickProject: (id: string) => void;
  onPickThread: (id: string) => void;
  onPickTask: (id: string) => void;
  onNewProject: () => void;
  onNewThread: () => void;
  onNewTask: () => void;
  onMoveThread: (threadId: string, projectId: string) => void;
  onOpen: (what: "schedules" | "usage" | "route") => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [openTasks, setOpenTasks] = useState<Record<string, boolean>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [menu, setMenu] = useState<MenuAt | null>(null);
  const toggle = (id: string) => setExpanded((e) => ({ ...e, [id]: !e[id] }));

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

  return (
    <div className="pane" id="sidebar">
      <h2 className="bar">Projects</h2>
      <button type="button" id="newproject" data-testid="new-project" onClick={props.onNewProject}>
        + New project
      </button>
      {props.moveError ? (
        <div className="err movenote" data-testid="move-error">{props.moveError}</div>
      ) : null}
      <div className="scroll" data-testid="sidebar">
        {props.projects.map((p) => {
          const plat = props.platforms[p.id];
          const open = expanded[p.id] ?? p.id === props.projectId;
          const chats = props.threads.filter((t) => t.project_id === p.id && !t.task_id);
          const tasks = props.tasks.filter((t) => t.project_id === p.id);
          const target = over === p.id;
          return (
            <div key={p.id}>
              <div
                className={
                  "tree-row" +
                  (p.id === props.projectId ? " sel" : "") +
                  (target ? " droptarget" : "")
                }
                data-testid={`project-${p.id}`}
                onClick={() => {
                  props.onPickProject(p.id);
                  toggle(p.id);
                }}
                onDragOver={(e) => {
                  // preventDefault is what makes a drop legal at all, so the
                  // row only accepts a drag that would actually change something.
                  if (dragging && wouldMove(props.threads, dragging, p.id)) {
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
                  if (id) props.onMoveThread(id, p.id);
                }}
              >
                <span className="tw">{open ? "▾" : "▸"}</span>
                <span className="nm">{p.name}</span>
                {plat?.platform === "windows" ? (
                  <span className="badge win" title={plat.note || "Windows path worked from WSL"}>
                    WIN
                  </span>
                ) : null}
              </div>
              {open ? (
                <>
                  <div className="tree-row indent-1 muted small" onClick={props.onNewThread}>
                    <span className="tw">+</span>
                    <span className="nm">new chat thread</span>
                  </div>
                  {chats.map((t) => (
                    <div
                      key={t.id}
                      className={
                        "tree-row indent-1" +
                        (t.id === props.threadId ? " sel" : "") +
                        (dragging === t.id ? " dragging" : "")
                      }
                      data-testid={`thread-${t.id}`}
                      draggable
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
                    >
                      <span className="tw">·</span>
                      <span className="nm">{t.title || t.id}</span>
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
                  <div className="tree-row indent-1 muted small" onClick={props.onNewTask}>
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
      <div style={{ borderTop: "1px solid var(--border)", flex: "0 0 auto" }}>
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
      </div>

      {menu ? (
        <div
          className="threadmenu"
          data-testid="thread-menu"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
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
