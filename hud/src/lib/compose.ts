// Where a message goes, decided in one place.
//
// The window's conversation is exactly one of two things: an existing thread
// (`threadId`), or a new thread that is not on the server yet (`compose`).
// **The chat pane decides where a message goes, and the sidebar only shows
// it.** There used to be a separately stored `projectId` the sidebar
// highlighted, and nothing kept it in step with the thread a send actually
// went to. Clicking a project, or creating one, sent the next message to the
// previous project's thread.
//
// So there is no stored project any more. `activeProjectId` derives it from
// the conversation, and `lastProject` picks the project a new thread starts
// in. Both are pure, which is where the free tests grade them.

import type { Project, ProviderName, Task, Thread } from "../types";

/** A new thread being composed. `openedId` is set once the server has the
 * thread but its first message has not gone through, so a retry reuses it
 * instead of opening a second one. */
export interface Compose {
  projectId: string | null;
  openedId?: string | null;
  /** The provider, model and effort chosen while composing (lib/threadmodel).
   * Absent means the defaults; a new thread never inherits the last one's. */
  provider?: ProviderName;
  model?: string | null;
  effort?: string | null;
  /** The send in flight for this compose row (its local message id): after
   * each await the send finds the pane holding this row again by it, so a
   * trade of conversations or another thread opened meanwhile never makes
   * it write to the wrong pane (review of PR #27). A row the owner replaces
   * loses it, and the send then leaves that pane alone. */
  sending?: string;
}

export const LAST_PROJECT_KEY = "jarvis.hud.lastProject";

/** Storage can be missing or throw (private window, blocked site data). A
 * read that fails is "nothing remembered", never an error. */
export function loadLastProject(storage?: Storage | null): string | null {
  try {
    return (storage ?? window.localStorage).getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

export function saveLastProject(id: string, storage?: Storage | null): void {
  try {
    (storage ?? window.localStorage).setItem(LAST_PROJECT_KEY, id);
  } catch {
    /* a convenience, never a reason for a send to fail */
  }
}

/**
 * The project a new thread starts in: the last one worked in.
 *
 * 1. The project of the last message this window sent, if it still exists.
 * 2. Otherwise the project of the chat thread most recently active on the
 *    server (`updated` moves on every turn), which also covers a fresh
 *    browser and turns that came from Discord.
 * 3. Otherwise the Inbox, then the first project, then null (no projects).
 */
export function lastProject(projects: Project[], threads: Thread[], stored: string | null): string | null {
  const exists = (id: string | null | undefined) => !!id && projects.some((p) => p.id === id);
  if (exists(stored)) return stored;
  const recent = threads
    .filter((t) => !t.task_id && exists(t.project_id))
    .sort((a, b) => (a.updated < b.updated ? 1 : a.updated > b.updated ? -1 : 0))[0];
  if (recent) return recent.project_id;
  const inbox = projects.find((p) => p.inbox);
  if (inbox) return inbox.id;
  return projects[0]?.id ?? null;
}

/**
 * The project the window is about: the compose project, else the open
 * thread's, else the selected task's. A task picked most recently wins, so
 * the File tab follows what the owner is looking at.
 */
export function activeProjectId(s: {
  compose: Compose | null;
  threadId: string | null;
  taskId: string | null;
  taskFocus: boolean;
  threads: Thread[];
  tasks: Task[];
}): string | null {
  const task = s.taskId ? s.tasks.find((t) => t.id === s.taskId) : undefined;
  if (s.taskFocus && task) return task.project_id;
  if (s.compose) return s.compose.projectId;
  const thread = s.threadId ? s.threads.find((t) => t.id === s.threadId) : undefined;
  if (thread) return thread.project_id;
  return task?.project_id ?? null;
}

/** Whether the compose row can be dropped on `projectId`: only while nothing
 * exists on the server yet, and only onto a different project. */
export function composeMovable(compose: Compose | null, projectId: string): boolean {
  return !!compose && !compose.openedId && !!projectId && compose.projectId !== projectId;
}

/** The last path component, for the "opened in" hint. */
export function folderName(path: string | null | undefined): string {
  if (!path) return "";
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** True when a thread works in a folder other than its project's root, which
 * after a move it does: a move re-labels a thread and never re-roots it. */
export function movedAway(thread: Thread, project: Project | undefined): boolean {
  if (!thread.cwd || !project) return false;
  const norm = (p: string) => p.replace(/[\\/]+$/, "");
  return norm(thread.cwd) !== norm(project.root);
}

/** The thread the window's conversation is on: the open thread, or the one a
 * compose row already opened on the server when its first message failed.
 * The model chips read this, so they stay on screen for that retry instead
 * of vanishing between "composing" and "a thread". */
export function conversationThreadId(s: { threadId: string | null; compose: Compose | null }): string | null {
  return s.threadId ?? s.compose?.openedId ?? null;
}
