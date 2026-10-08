// Rename, edit and archive, as pure functions the free tests grade
// (decisions part B, docs/plans/2026-10-06-decisions.md).
//
// **The backend decides every name.** `uniqueName` mirrors
// `jarvis/v2/projects.unique_name` so the window can say "saves as X (1)"
// before the owner presses Enter, but what is saved is what the PATCH
// returns. The two share one table of cases (projects.test.ts and
// tests/v2/archive_check.py), because a preview that disagrees with the
// result is a promise the window then breaks.
//
// **What the window does when a project or thread goes away** — archived
// here, or archived from another window — is decided once, in
// `afterProjectGone` / `afterThreadGone`. The alternative is a compose row
// still aimed at a project that no longer takes messages, and the next send
// finding out with a 409.

import type { DeleteResult, Project, ProjectImpact, Task, Thread } from "../types";
import { LAST_PROJECT_KEY, lastProject, type Compose } from "./compose";

/**
 * What two names are compared on: case and surrounding spaces ignored. Python
 * uses `str.casefold()`, which JavaScript lacks; upper-then-lower plus folding
 * the final sigma reproduces it for the cases that differ from a plain
 * `toLowerCase()` (`ß` and `ẞ` → `ss`, ligatures such as `ﬁ` → `fi`, `ς` → `σ`).
 * Known remaining gap: `trim()` and Python's `strip()` disagree on a few
 * exotic characters (U+FEFF is trimmed here, U+001C–U+001F there). The backend
 * decides the saved name either way; this only drives the preview.
 */
export function nameKey(name: string | null | undefined): string {
  return (name || "").trim().toUpperCase().toLowerCase().replace(/ς/g, "σ");
}

const SUFFIX = /^(.*?) \((\d+)\)$/;

/** `wanted`, or `wanted (1)`, `(2)`, … — the first one nobody has. */
export function uniqueName(wanted: string, taken: (string | null | undefined)[]): string {
  const base = (wanted || "").trim();
  const keys = new Set(taken.filter(Boolean).map((t) => nameKey(t)));
  if (!keys.has(nameKey(base))) return base;
  const m = SUFFIX.exec(base);
  const stem = m ? m[1] : base;
  let n = 1;
  while (keys.has(nameKey(`${stem} (${n})`))) n++;
  return `${stem} (${n})`;
}

/**
 * Every name a project may not take: the others' — archived ones included,
 * because the backend counts them (`project_names_taken`) — and the Inbox's.
 * `archived` comes from `/archive`, since `/projects` hides archived projects.
 */
export function projectNamesTaken(projects: Project[], but?: string | null, archived: string[] = []): string[] {
  return [...projects.filter((p) => p.id !== but).map((p) => p.name), ...archived, "Inbox"];
}

/** Thread titles are unique within their project. */
export function threadTitlesTaken(threads: Thread[], projectId: string, but?: string | null): string[] {
  return threads.filter((t) => t.project_id === projectId && t.id !== but && t.title).map((t) => t.title);
}

/** The edit dialog's form, as the owner left it. */
export interface ProjectForm {
  name: string;
  root: string;
  profile: string;
  extra_dirs: string[];
  always_ask: string[];
}

export function formFrom(p: Project): ProjectForm {
  return {
    name: p.name,
    root: p.root,
    profile: p.profile,
    extra_dirs: [...(p.extra_dirs || [])],
    always_ask: [...(p.always_ask || [])],
  };
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

/**
 * The PATCH body for an edit: **only what changed**, and never the Inbox's
 * name or root (the backend refuses both, so sending them would turn a
 * profile change into a 409).
 */
export function projectEditBody(initial: Project, form: ProjectForm): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  const name = form.name.trim();
  const root = form.root.trim();
  if (!initial.inbox && name && name !== initial.name) body.name = name;
  if (!initial.inbox && root && root !== initial.root) body.root = root;
  if (form.profile !== initial.profile) body.profile = form.profile;
  if (!sameList(form.extra_dirs, initial.extra_dirs || [])) body.extra_dirs = form.extra_dirs;
  const rules = form.always_ask.map((r) => r.trim()).filter(Boolean);
  if (!sameList(rules, initial.always_ask || [])) body.always_ask = rules;
  return body;
}

/** One line per thing an edit leaves where it was, for the dialog's Effects. */
export function editEffects(initial: Project, body: Record<string, unknown>, impact: ProjectImpact | null): string[] {
  const out: string[] = [];
  if (typeof body.root === "string") {
    const n = impact?.on_root.threads ?? 0;
    const tasks = impact?.on_root.tasks ?? [];
    out.push(`${plural(n, "thread")} keep working in ${initial.root}; new threads and tasks use ${body.root}.`);
    if (tasks.length) out.push(`${plural(tasks.length, "unfinished task")} finish and commit in ${initial.root}.`);
    if (impact?.worktrees.length) out.push(`Worktrees stay where they are, under ${initial.root}.`);
  }
  if (typeof body.profile === "string") {
    out.push("The profile applies to work started from now on; open threads and running workers keep theirs.");
  }
  return out;
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The archive confirmation, as plain lines read back from `/impact`. */
export function archiveSummary(impact: ProjectImpact): string[] {
  const lines = [
    `${plural(impact.chat_threads.count, "chat thread")} and ${plural(impact.tasks.total, "task")} are kept, hidden, with everything in them.`,
  ];
  if (impact.task_threads) lines.push(`${plural(impact.task_threads, "task thread")} go with their tasks.`);
  const live = impact.schedules.filter((s) => s.enabled).length;
  if (impact.schedules.length) {
    lines.push(`${plural(live, "schedule")} ${live === 1 ? "is" : "are"} paused until you restore it.`);
  }
  if (impact.worktrees.length) {
    lines.push(`Left on disk: ${impact.worktrees.map((w) => w.path + (w.branch ? ` (${w.branch})` : "")).join(", ")}.`);
  }
  lines.push(`Nothing inside ${impact.root} is touched. Restore it from Archive at any time.`);
  return lines;
}

/** What a permanent delete removes and what it leaves, for its confirmation. */
export function deleteSummary(impact: ProjectImpact, retentionDays: number | null): string[] {
  const lines = [
    `Jarvis's records go to the trash: ${plural(impact.chat_threads.count, "chat thread")}, ` +
      `${plural(impact.tasks.total, "task")}, ${plural(impact.task_threads, "task thread")}, ` +
      `${plural(impact.schedules.length, "schedule")}.`,
  ];
  const existing = impact.worktrees.filter((w) => w.exists);
  lines.push(existing.length
    ? `Left on disk, with their branches: ${existing.map((w) => w.path).join(", ")}.`
    : "No worktree of it is on disk any more.");
  lines.push(`Nothing inside ${impact.root} is touched.`);
  if (retentionDays) lines.push(`The trash keeps them for ${retentionDays} days.`);
  return lines;
}

/**
 * What the Archive view says after a permanent delete. The records always
 * leave every list; when the move to the trash itself failed they are staged
 * under the data root and retried later (`recover_staging`), and the window
 * must say so rather than claim they are in the trash.
 */
export function deletedNotice(what: string, result: DeleteResult | null | undefined): string {
  const trash = result?.trash;
  if (trash?.where === "staged") {
    return `Deleted ${what} from Jarvis's lists, but the move to the trash failed` +
      (trash.error ? ` (${trash.error})` : "") +
      `. The records are staged for retry${trash.path ? ` in ${trash.path}` : ""}; ` +
      "Jarvis tries again at its next start and every six hours.";
  }
  if (trash?.where === "windows") return `Deleted ${what}; Jarvis's records are in the Recycle Bin.`;
  return `Deleted ${what}; Jarvis's records are in the trash.`;
}

/** Clear the remembered project when it is the one that went away. */
export function forgetLastProject(id: string, storage?: Storage | null): void {
  try {
    const s = storage ?? window.localStorage;
    if (s.getItem(LAST_PROJECT_KEY) === id) s.removeItem(LAST_PROJECT_KEY);
  } catch {
    /* a convenience, never a reason for anything to fail */
  }
}

/** The slice of the window's state a disappearing project or thread touches. */
export interface Where {
  projects: Project[];
  threads: Thread[];
  tasks: Task[];
  threadId: string | null;
  compose: Compose | null;
  taskId: string | null;
}

export interface WherePatch {
  projects: Project[];
  threads: Thread[];
  tasks: Task[];
  threadId: string | null;
  compose: Compose | null;
  taskId: string | null;
  /** True when the open conversation was in what went away. */
  displaced: boolean;
}

/**
 * The window after project `id` was archived or deleted. Its rows go; a
 * compose row aimed at it is re-aimed to the last project worked in; an open
 * thread or task inside it closes into a new thread there.
 */
export function afterProjectGone(s: Where, id: string, stored: string | null): WherePatch {
  const projects = s.projects.filter((p) => p.id !== id);
  const threads = s.threads.filter((t) => t.project_id !== id);
  const tasks = s.tasks.filter((t) => t.project_id !== id);
  const keep = stored === id ? null : stored;
  const fallback = lastProject(projects, threads, keep);
  const openThread = s.threadId ? s.threads.find((t) => t.id === s.threadId) : undefined;
  const threadGone = !!openThread && openThread.project_id === id;
  const openTask = s.taskId ? s.tasks.find((t) => t.id === s.taskId) : undefined;
  const taskGone = !!openTask && openTask.project_id === id;
  let compose = s.compose;
  if (compose && compose.projectId === id) compose = { projectId: fallback };
  let threadId = s.threadId;
  if (threadGone) {
    threadId = null;
    compose = { projectId: fallback };
  }
  return {
    projects, threads, tasks, threadId, compose,
    taskId: taskGone ? null : s.taskId,
    displaced: threadGone || taskGone || (!!s.compose && s.compose.projectId === id),
  };
}

/** The window after one chat thread was archived or deleted. */
export function afterThreadGone(s: Where, id: string): WherePatch {
  const gone = s.threads.find((t) => t.id === id);
  const threads = s.threads.filter((t) => t.id !== id);
  const open = s.threadId === id || (!!s.compose?.openedId && s.compose.openedId === id);
  return {
    projects: s.projects, threads, tasks: s.tasks, taskId: s.taskId,
    threadId: open ? null : s.threadId,
    compose: open ? { projectId: gone?.project_id ?? s.compose?.projectId ?? null } : s.compose,
    displaced: open,
  };
}
