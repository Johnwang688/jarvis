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
// conversation is marked: the active one (the selected chat, the chat used
// last) with the accent, the others with a dimmer mark, each row carrying the
// pane it is open in (`data-pane`). Each composing chat pane has its own
// compose row, numbered by pane when more than one chat pane is drawn. A
// click on a thread another pane shows focuses that pane — a thread is open
// in one pane at most; Alt+click, or "Open beside" in the row's ⋯ menu, opens
// it in the next pane to the right (from one pane, two columns).

import { memo, useEffect, useRef, useState } from "react";
import type { Project, Task, TaskThread, Thread } from "../types";
import { canMoveThread, wouldMove } from "../lib/threads";
import { composeMovable, folderName, movedAway, type Compose } from "../lib/compose";
import type { PaneNo } from "../lib/workspace";
import { shortId, threadTooltip } from "../lib/threadmodel";
import { shortThreadId, threadLabel } from "../lib/threadTitle";
import { projectNamesTaken, threadTitlesTaken, unsortedWhy } from "../lib/projects";
import { loadShowMore, moreLabel, recentChats, saveShowMore } from "../lib/recentChats";
import { waitingTitle } from "../lib/relays";
import { InlineRename } from "./InlineRename";
import { HoverTitle } from "./HoverTitle";
import { CollapseButton } from "./Layout";
import { toCss } from "../lib/layout";
import { NO_ACTIVITY, projectActivity, type ActivityView } from "../lib/activity";
import { Dot } from "./ActivityDot";
import { WorkingBlock, WorkingDoneItem } from "./WorkingSection";
import { working } from "../state/working";
import { UNDO_TOAST_MS, loadSectionOpen, saveSectionOpen } from "../lib/working";
import type { InboxRow } from "../lib/inbox";

interface ProjectMenuAt {
  projectId: string;
  x: number;
  y: number;
}

/** What a drag is carrying: a thread id, or the not-yet-sent compose row. */
const COMPOSE_DRAG = "jarvis/compose";

interface MenuAt {
  threadId: string;
  /** Set when the row was a *task's* thread, which `/threads` does not list. */
  taskId: string | null;
  /** Opened from a row of the Inbox section, whose menu also offers Done. */
  working?: boolean;
  x: number;
  y: number;
}

/**
 * The sidebar. Memoized: typing in a chat box re-renders the window (the
 * box's words are the store's, WP-B), and every thread row redrawn per
 * keystroke grows with the owner's thread count (re-review of PR #27). The
 * window hands it stable callbacks and row lists.
 */
export const Sidebar = memo(function Sidebar(props: {
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
  /** The threads open in drawn chat panes, the active one (the selected chat) marked. */
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
  onOpen: (what: "schedules") => void;
  // --- rename, edit, archive (decisions B); each resolves with the record
  // the backend saved, whose name may be numbered.
  onEditProject?: (projectId: string) => void;
  onArchiveProject?: (projectId: string) => void;
  onRenameProject?: (projectId: string, name: string) => Promise<Project>;
  onRenameThread?: (threadId: string, title: string) => Promise<Thread>;
  onArchiveThread?: (threadId: string) => Promise<unknown>;
  /** The HUD zoom in percent: a menu placed from a screen rect is placed in zoomed space. */
  zoom?: number;
  /** Folds the sidebar to its rail (2026-10-08). */
  onCollapse?: () => void;
  /** An authorization card is up: the fold button is disabled (it would still answer Enter). */
  layoutBlocked?: boolean;
  /**
   * The owner inbox's row above the projects (inbox A2): what waits on the
   * owner, ranks 1 to 5 (`count`; null while the inbox is not there to read),
   * amber when any of it is pending (ranks 1 and 2), and whether a drawn pane
   * shows the inbox. Absent: no row (older callers and tests).
   */
  inbox?: { count: number | null; pending: boolean; shown: boolean };
  /** Opens the Needs you page; given a thread id, at that thread's group (WS-3). */
  onOpenInbox?: (threadId?: string) => void;
  /** The morning review's row, under the Inbox's (MR-10): `count` is what is open in
   * it (null while unread); absent or null when the daemon has no review. */
  review?: { count: number | null; shown: boolean } | null;
  onOpenReview?: () => void;
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
  // The Inbox section under the owner Inbox row (WS-2). The store is read by
  // `WorkingBlock`, not here: a refresh redraws that block only.
  const [workOpen, setWorkOpen] = useState<boolean>(() => loadSectionOpen());
  const toggleWork = () =>
    setWorkOpen((o) => {
      saveSectionOpen(!o);
      return !o;
    });
  // Done's "Marked Done" toast: the entry's title and the daemon's token, for UNDO_TOAST_MS.
  // A thread's Undo carries the daemon's token; a Needs you row's is the inbox's `{clear: true}` (no token).
  const [undo, setUndo] = useState<
    { kind: "thread"; id: string; token: string; title: string } | { kind: "row"; id: string; title: string } | null
  >(null);
  // Projects whose full chat list the owner asked for (remembered, not stored on the server).
  const [showAll, setShowAll] = useState<Record<string, boolean>>(() => loadShowMore());
  const toggleShowAll = (id: string) =>
    setShowAll((m) => {
      const next = { ...m, [id]: !m[id] };
      saveShowMore(next);
      return next;
    });
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

  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuOpener = useRef<HTMLElement | null>(null);
  // Keyboard: focus goes to the menu's first enabled item as it opens and back to the
  // button that opened it when it closes (unless the owner has put it somewhere else).
  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button.mrow:not(:disabled)")?.focus();
    const opener = menuOpener.current;
    return () => {
      const a = document.activeElement;
      if (opener && opener.isConnected && (!a || a === document.body)) opener.focus();
    };
  }, [menu]);

  /** Arrow keys for a `role="menu"` (both menus): Up and Down move between the enabled items and
   * wrap, Home and End go to the ends. Other keys are left alone (Tab, Escape, Enter keep working). */
  const menuKeys = (e: React.KeyboardEvent<HTMLElement>) => {
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const items = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button.mrow:not(:disabled)"));
    if (!items.length) return;
    e.preventDefault();
    e.stopPropagation();
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    const to = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1
      : e.key === "ArrowDown" ? (at + 1) % items.length : (at <= 0 ? items.length - 1 : at - 1);
    items[to].focus();
  };

  const openMenu = (e: React.MouseEvent, id: string, taskId: string | null = null, inWorking = false) => {
    e.preventDefault();
    e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    menuOpener.current = e.currentTarget as HTMLElement;
    // The reason Done is greyed out is as of the last read: read again as the menu opens.
    if (inWorking) void working.refresh();
    setMenu({ threadId: id, taskId, working: inWorking, x: Math.round(toCss(r.left, z)), y: Math.round(toCss(r.bottom, z)) });
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

  // The toast goes by itself; Undo before then posts the daemon's token.
  useEffect(() => {
    if (!undo) return;
    const t = setTimeout(() => setUndo(null), UNDO_TOAST_MS);
    return () => clearTimeout(t);
  }, [undo]);

  const markDone = (id: string) => {
    setMenu(null);
    const thread = props.threads.find((t) => t.id === id);
    const title = thread ? threadLabel(thread).text : "Thread";
    void working.done(id).then((r) => {
      // A 409 is the list having moved on: it is read again and redrawn, with no message.
      if (r.kind === "done") setUndo({ kind: "thread", id, token: r.token, title });
      else if (r.kind === "error") setNote({ text: `Could not mark it done: ${r.message}`, error: true });
    });
  };
  const markRowDone = (row: InboxRow) => {
    void working.dismissRow(row.id).then((r) => {
      if (r.kind === "done") setUndo({ kind: "row", id: row.id, title: row.title });
      else setNote({ text: `Could not mark it done: ${r.message}`, error: true });
    });
  };
  const undoDone = (u: NonNullable<typeof undo>) => {
    setUndo(null);
    const go = u.kind === "thread" ? working.undo(u.id, u.token) : working.undismissRow(u.id);
    void go.then((r) => {
      if (r.kind === "error") setNote({ text: `Could not undo: ${r.message}`, error: true });
    });
  };

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
      {props.inbox && props.onOpenInbox ? (
        // The owner Inbox row is the header of the In progress list (WS-3): `WorkingBlock` draws it
        // and the list under it, and a click folds the list (it opens the Needs you page only where
        // the daemon has no list to fold).
        <WorkingBlock
          open={workOpen}
          onToggle={toggleWork}
          inboxCount={props.inbox.count}
          pending={props.inbox.pending}
          shown={props.inbox.shown}
          threads={props.threads}
          projects={props.projects}
          activity={activity}
          selClass={selClass}
          paneOf={(id) => openIn(id)?.pane}
          dragging={dragging}
          onPick={props.onPickThread}
          onOpenInbox={props.onOpenInbox}
          onMenu={(e, id) => openMenu(e, id, null, true)}
          onDone={markDone}
          onDoneRow={markRowDone}
          onDragStart={(e, id) => {
            e.dataTransfer.setData("text/plain", id);
            e.dataTransfer.effectAllowed = "move";
            setDragging(id);
          }}
          onDragEnd={() => {
            setDragging(null);
            setOver(null);
          }}
        />
      ) : null}
      {props.review && props.onOpenReview ? (
        // The morning review (MR-10): what waited overnight, in one report. It
        // only opens the view; it answers nothing.
        <button
          type="button"
          className={"tree-row ownerreview" + (props.review.shown ? " sel" : "")}
          data-testid="owner-review"
          aria-label={props.review.count ? `Morning review, ${props.review.count} open` : "Morning review"}
          title="What waited overnight, grouped by chat"
          onClick={props.onOpenReview}
        >
          <span className="tw">☀</span>
          <span className="nm">Morning review</span>
          {props.review.count ? (
            <span className="badge reviewcount" data-testid="owner-review-count">{props.review.count}</span>
          ) : null}
        </button>
      ) : null}
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
      {/* "Marked Done", bottom left of the window, for UNDO_TOAST_MS or until its × (owner, 2026-11-15).
          Keyed by the token, so a second Done restarts the countdown bar. */}
      {undo ? (
        <div className="workingtoast" key={undo.kind === "thread" ? undo.token : `row:${undo.id}`}
             data-testid="working-toast" data-kind={undo.kind} role="status">
          <span className="wt-text">
            <span className="wt-head">Marked Done</span>
            <span className="nm" data-testid="working-toast-title" title={undo.title}>{undo.title}</span>
          </span>
          <button type="button" className="wt-undo" data-testid="working-undo" onClick={() => undoDone(undo)}>Undo</button>
          <button type="button" className="wt-close" data-testid="working-toast-close" aria-label="Dismiss"
                  title="Dismiss" onClick={() => setUndo(null)}>×</button>
          <span className="wt-bar" aria-hidden="true" style={{ animationDuration: `${UNDO_TOAST_MS}ms` }} />
        </div>
      ) : null}
      <div className="scroll" data-testid="sidebar">
        {props.projects.map((p) => {
          const plat = props.platforms[p.id];
          const open = isOpen(p.id);
          const composing = props.composing.filter((c) => c.compose.projectId === p.id);
          const chats = props.threads.filter((t) => t.project_id === p.id && !t.task_id);
          // Five most recent, newest first; the open, running and waiting ones are never hidden.
          const recent = recentChats(chats, {
            expanded: !!showAll[p.id],
            kept: new Set([
              ...props.open.map((o) => o.threadId),
              ...(renaming?.kind === "thread" ? [renaming.id] : []),
            ]),
            status: (id) => activity.threads[id],
          });
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
                  // Files from outside are not a move: left alone, so the
                  // window stages them in the selected chat (decisions W-6;
                  // re-review of PR #27 — this row used to swallow them).
                  if (!dragging && e.dataTransfer.types.includes("Files")) {
                    setOver(null);
                    return;
                  }
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
                  {recent.shown.map((t) => (
                    <div
                      key={t.id}
                      className={
                        "tree-row indent-1 titled" +
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
                      ) : (() => {
                        // One line, scrolling on hover; the id stays in ⋯.
                        const label = threadLabel(t);
                        return (
                          <HoverTitle
                            text={label.text}
                            untitled={label.untitled}
                            testid={`thread-name-${t.id}`}
                            onDoubleClick={(e) => {
                              e.stopPropagation();
                              if (props.onRenameThread) setRenaming({ kind: "thread", id: t.id });
                            }}
                          />
                        );
                      })()}
                      {movedAway(t, p) ? (
                        <span className="moved" data-testid={`moved-${t.id}`}>↪ {folderName(t.cwd)}</span>
                      ) : null}
                      <span className={`badge ${t.provider}`}>{t.provider}</span>
                      {/* Chat-to-chat (C5): messages from other chats wait for this one — pending, so amber. */}
                      {(t.relays_waiting ?? 0) > 0 ? (
                        <span className="badge relay" data-testid={`relays-waiting-${t.id}`}
                          title={waitingTitle(t.relays_waiting!)}>↪{t.relays_waiting}</span>
                      ) : null}
                      {t.model ? (
                        <span className="badge pin" data-testid={`pin-${t.id}`} title={t.model}>{shortId(t.model)}</span>
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
                  {recent.more ? (
                    <button
                      type="button"
                      className="tree-row indent-1 muted small morechats"
                      data-testid={`more-chats-${p.id}`}
                      aria-expanded={!!showAll[p.id]}
                      onClick={() => toggleShowAll(p.id)}
                    >
                      <span className="tw">{showAll[p.id] ? "▴" : "▾"}</span>
                      <span className="nm">{moreLabel(recent.hidden, !!showAll[p.id])}</span>
                    </button>
                  ) : null}
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
      </div>

      {pmenu && pmenuProject ? (
        <div
          className="threadmenu"
          data-testid="project-menu"
          role="menu"
          tabIndex={-1}
          onKeyDown={menuKeys}
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
            <div className="mwhy" data-testid="pmenu-inbox-why">{unsortedWhy(pmenuProject.name)}</div>
          ) : null}
        </div>
      ) : null}

      {menu ? (
        <div
          className="threadmenu"
          data-testid="thread-menu"
          ref={menuRef}
          role="menu"
          tabIndex={-1}
          onKeyDown={menuKeys}
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {menu.working ? <WorkingDoneItem threadId={menu.threadId} onDone={markDone} /> : null}
          <button
            type="button"
            className="mrow"
            role="menuitem"
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
          {/* Chat titles: the id lives here now, not on the row. */}
          <button
            type="button"
            className="mrow"
            role="menuitem"
            style={{ display: "block", width: "100%", textAlign: "left", border: "none",
                     background: "none", textTransform: "none", letterSpacing: 0 }}
            data-testid="tmenu-copyid"
            title={`Copy the thread id ${menu.threadId}`}
            onClick={() => {
              const id = menu.threadId;
              setMenu(null);
              try {
                void navigator.clipboard?.writeText(id).catch(() => {});
              } catch {
                /* no clipboard here: the id is still shown in the menu */
              }
            }}
          >
            Copy id <span className="mid" data-testid="tmenu-id">{shortThreadId(menu.threadId)}</span>
          </button>
          <div className="msep" />
          {movable.ok && menuThread ? (
            <>
              <button
                type="button"
                className="mrow"
                role="menuitem"
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
                role="menuitem"
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
                  role="menuitem"
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
});
