// Projects, expanding to chat threads and tasks; a task expands to its
// threads with role and provider — the orchestration view (§12.2). Windows
// projects (`/mnt/<drive>/…`) carry a badge and the 9p caution as its title.

import { useState } from "react";
import type { Project, Task, TaskThread, Thread } from "../types";

export function Sidebar(props: {
  projects: Project[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  projectId: string | null;
  threadId: string | null;
  taskId: string | null;
  onPickProject: (id: string) => void;
  onPickThread: (id: string) => void;
  onPickTask: (id: string) => void;
  onNewProject: () => void;
  onNewThread: () => void;
  onNewTask: () => void;
  onOpen: (what: "schedules" | "usage" | "route") => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [openTasks, setOpenTasks] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setExpanded((e) => ({ ...e, [id]: !e[id] }));

  return (
    <div className="pane" id="sidebar">
      <h2 className="bar">Projects</h2>
      <div className="scroll" data-testid="sidebar">
        {props.projects.map((p) => {
          const plat = props.platforms[p.id];
          const open = expanded[p.id] ?? p.id === props.projectId;
          const chats = props.threads.filter((t) => t.project_id === p.id && !t.task_id);
          const tasks = props.tasks.filter((t) => t.project_id === p.id);
          return (
            <div key={p.id}>
              <div
                className={"tree-row" + (p.id === props.projectId ? " sel" : "")}
                data-testid={`project-${p.id}`}
                onClick={() => {
                  props.onPickProject(p.id);
                  toggle(p.id);
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
                      className={"tree-row indent-1" + (t.id === props.threadId ? " sel" : "")}
                      data-testid={`thread-${t.id}`}
                      onClick={() => props.onPickThread(t.id)}
                    >
                      <span className="tw">·</span>
                      <span className="nm">{t.title || t.id}</span>
                      <span className={`badge ${t.provider}`}>{t.provider}</span>
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
                                onClick={() => props.onPickThread(k.thread_id)}
                              >
                                <span className="tw">·</span>
                                <span className="nm">{k.role}</span>
                                <span className={`badge ${k.provider}`}>{k.provider}</span>
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
      <div style={{ borderTop: "1px solid var(--cyan-faint)", flex: "0 0 auto" }}>
        <div className="tree-row" data-testid="new-project" onClick={props.onNewProject}>
          <span className="tw">+</span>
          <span className="nm">New project</span>
        </div>
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
    </div>
  );
}
