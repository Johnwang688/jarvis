// The Diff pane's uncommitted changes (2026-10-12, the owner: "the Diff
// doesn't work right now it just says to select a Task"). Pure, so the rules
// are testable without a repository or a window.
//
//  - **Which mode a Diff pane is in** (`diffMode`): **a Diff pane never
//    shows another task than a drawn Task pane** — while one is drawn and
//    shows a task, the Diff shows that task's diff (review of PR #67, L3);
//    else, while a task is what the window is about (picked more recently
//    than a thread — the rule Editor and Browser panes follow), that task's
//    diff, exactly as before; else the uncommitted changes of the pane's
//    pinned project (a pane's pin is its own: a File pane that opened a file
//    and became a Diff pane keeps it); else those of the project the chat is
//    in; else nothing to show. `why` says which, for the pane's caption.
//  - **The rows** (`scmRows`): a project's `git/status` (ED-9), one row per
//    changed file — modified, added, untracked, renamed, deleted, conflict —
//    and per folder git reported untracked whole; ignored entries are never
//    listed. Each wears a letter and the editor's file icon.
//  - **What a row shows** (`rowShows`): a diff, except an env file — listed,
//    never diffed: its committed text is refused by `git/base`, and its
//    working text goes in no request but its own save — and a folder.
//  - **The sides** (`baseSide`, `readSide`): HEAD's text on the left, the
//    file as the editor would show it on the right, or a sentence instead of
//    a diff (binary, too large, not a regular file).
//  - **The list at scale** (`scmWindow`, `revealTop`, `scmKeyStep`): windowed,
//    with keys to move the selection — see the section below.

import { isEnvName } from "./fileIcons";

export type DiffMode =
  /** `pane`: a drawn Task pane shows it; `focus`: it was picked more recently than a thread. */
  | { kind: "task"; taskId: string; why: "pane" | "focus" }
  | { kind: "project"; projectId: string; pinned: boolean }
  | { kind: "none" };

/**
 * What a Diff pane shows. `pin` is the pane's own project (the picker in its
 * header), `follow` the project the window is about (the chat's),
 * `taskShown` whether a drawn Task pane shows the window's task. A task
 * wins over the pin, so a task click always shows its diff in a drawn Diff
 * pane (`workspace.show` relies on it), and a Diff pane never shows another
 * task than a drawn Task pane; picking a project in the pane's picker takes
 * the window off the task (App) — while no Task pane holds it.
 */
export function diffMode(a: {
  pin: string | null;
  projectIds: readonly string[];
  taskId: string | null;
  taskFocus: boolean;
  taskShown: boolean;
  taskIds: readonly string[];
  follow: string | null;
}): DiffMode {
  const task = a.taskId && a.taskIds.includes(a.taskId) ? a.taskId : null;
  if (task && a.taskShown) return { kind: "task", taskId: task, why: "pane" };
  if (task && a.taskFocus) return { kind: "task", taskId: task, why: "focus" };
  if (a.pin && a.projectIds.includes(a.pin)) return { kind: "project", projectId: a.pin, pinned: true };
  if (a.follow && a.projectIds.includes(a.follow)) return { kind: "project", projectId: a.follow, pinned: false };
  return { kind: "none" };
}

/** Why the pane shows what it shows: the caption's second half. */
export function diffWhy(mode: DiffMode): string {
  if (mode.kind === "task") return mode.why === "pane" ? "the Task pane's task" : "the task picked last";
  if (mode.kind === "project") return mode.pinned ? "this pane's project" : "the chat's project";
  return "";
}

export type ScmState = "modified" | "added" | "untracked" | "renamed" | "deleted" | "conflict";

/** The letter a row wears (VS Code's Source Control letters). */
export const SCM_LETTER: Readonly<Record<ScmState, string>> = {
  modified: "M",
  added: "A",
  untracked: "U",
  renamed: "R",
  deleted: "D",
  conflict: "C",
};

export const SCM_WORDS: Readonly<Record<ScmState, string>> = {
  modified: "modified since the last commit",
  added: "added (staged, not committed)",
  untracked: "untracked: git does not know it",
  renamed: "renamed (staged)",
  deleted: "deleted",
  conflict: "a merge conflict",
};

const LISTED: ReadonlySet<string> = new Set(Object.keys(SCM_LETTER));

export interface ScmRow {
  /** Project-relative, as `git/status` says it. */
  path: string;
  state: ScmState;
  letter: string;
  /** A folder git reported whole (untracked): its files are not listed one by one. */
  folder: boolean;
  /** A credential env file by name (`.env`, `.env.*`, never `.env.example`): listed, never diffed. */
  env: boolean;
}

const base = (p: string) => p.slice(p.lastIndexOf("/") + 1);
const trim = (p: string) => p.replace(/^\/+|\/+$/g, "");

/** The part of `git/status`'s answer the list needs. */
export interface ScmStatusLike {
  repo?: boolean;
  files?: Record<string, string> | null;
  dirs?: Record<string, string> | null;
}

/** One row per changed file and per untracked folder, by path; ignored entries never. */
export function scmRows(status: ScmStatusLike | null | undefined): ScmRow[] {
  if (!status) return [];
  const out: ScmRow[] = [];
  const files = status.files && typeof status.files === "object" ? status.files : {};
  for (const [raw, state] of Object.entries(files)) {
    const path = trim(raw);
    if (!path || typeof state !== "string" || !LISTED.has(state)) continue;
    const s = state as ScmState;
    out.push({ path, state: s, letter: SCM_LETTER[s], folder: false, env: isEnvName(base(path)) });
  }
  const dirs = status.dirs && typeof status.dirs === "object" ? status.dirs : {};
  for (const [raw, state] of Object.entries(dirs)) {
    const path = trim(raw);
    // "" is the project's own folder (`wholeUntracked`); ignored folders are never listed.
    if (!path || state !== "untracked") continue;
    out.push({ path, state: "untracked", letter: SCM_LETTER.untracked, folder: true, env: false });
  }
  return out.sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0));
}

/** git reported the project's own folder untracked whole: nothing in it is known to git yet. */
export function wholeUntracked(status: ScmStatusLike | null | undefined): boolean {
  return status?.dirs?.[""] === "untracked";
}

/** What clicking a row shows: its diff, the env sentence, or the folder sentence. */
export function rowShows(row: Pick<ScmRow, "env" | "folder">): "diff" | "env" | "folder" {
  if (row.env) return "env";
  if (row.folder) return "folder";
  return "diff";
}

export const ENV_NOTE = "Env files aren't diffed here: their text goes nowhere but the editor. Open it in the editor to see it.";
export const FOLDER_NOTE = "An untracked folder: git reports it whole, so its files are not listed one by one. Open its files from the editor's tree.";

/** The line above the list: how many, on which branch, or why there is nothing. */
export function scmSummary(status: (ScmStatusLike & { branch?: string | null; truncated?: boolean; note?: string }) | null,
  rows: readonly ScmRow[], error: string | null): string {
  if (error) return `No git status here: ${error}`;
  if (!status) return "Reading git status…";
  if (status.repo === false) return "Not a git repository: there are no commits to compare with.";
  const on = status.branch ? ` on ${status.branch}` : "";
  if (wholeUntracked(status)) return `This whole folder is untracked${on}: git does not know any of it yet.`;
  if (!rows.length) return `No uncommitted changes${on}.`;
  const n = rows.length;
  const line = `${n} uncommitted change${n === 1 ? "" : "s"}${on}`;
  if (status.truncated) return `${line} · TRUNCATED: ${status.note || "not every change is listed"}`;
  return line;
}

/** A side of the diff: text, or a sentence shown instead of the diff. */
export type Side = { text: string } | { note: string };

/**
 * HEAD's side from `git/base`'s answer: its text, empty for a file HEAD does
 * not hold (untracked, added, no commit yet), or a sentence instead of a diff.
 */
export function baseSide(state: string, text: string | null | undefined): Side {
  switch (state) {
    case "ok":
      return { text: text ?? "" };
    case "new":
    case "no_head":
    case "no_repo":
      return { text: "" };
    case "binary":
      return { note: "A binary file: there is no text to compare." };
    case "too_large":
      return { note: "Too large to diff here: HEAD's copy is over 2 MB." };
    case "not_a_file":
      return { note: "Not a regular file in HEAD (a link or a submodule): there is no text to compare." };
    default:
      return { note: "git could not say what HEAD holds for this file." };
  }
}

/**
 * The working side from the editor's read (its `kind`, editor_policy's): text
 * and the read-only safety files are shown; an env file never is; anything
 * else is a sentence.
 */
export function readSide(kind: string | null | undefined, isProtected: boolean): "show" | "env" | "binary" | "withheld" {
  if (isProtected) return "withheld";
  if (kind === "env") return "env";
  if (kind === "text" || kind === "safety") return "show";
  if (kind === "binary") return "binary";
  return "withheld";
}

// ---- the list at scale (the Diff follow-ups, 2026-10-12) ----------------------
//
// `git/status` lists up to 20,000 entries, and a DOM of 20,000 rows made every
// keystroke anywhere in the window cost 84–102 ms of style recalculation (the
// review of PR #67; 9.8 ms with the list hidden). So the list is windowed:
// only the rows in view, and `SCM_OVERSCAN` either side, are drawn, inside a
// spacer as tall as every row. Rows are one fixed height, so where a row is
// is arithmetic — no measuring. The units are the list's own CSS pixels
// (`scrollTop`, `clientHeight`), which the HUD's zoom does not change.

/** A change-list row's height in CSS px. ProjectDiff pins each row to it. */
export const SCM_ROW_PX = 24;
/** Rows drawn past each edge of the view, so a scroll shows rows already there. */
export const SCM_OVERSCAN = 12;
/** Rows drawn while the list has no height to measure (hidden, or not laid out yet). */
export const SCM_UNMEASURED_ROWS = 40;

/** The rows to draw, `[first, last)`, for a list scrolled to `scrollTop` with `viewPx` in view. */
export function scmWindow(count: number, scrollTop: number, viewPx: number,
  row = SCM_ROW_PX, over = SCM_OVERSCAN): { first: number; last: number } {
  if (count <= 0) return { first: 0, last: 0 };
  const top = Math.min(count - 1, Math.max(0, Math.floor((Number.isFinite(scrollTop) ? scrollTop : 0) / row)));
  const inView = viewPx > 0 ? Math.ceil(viewPx / row) + 1 : SCM_UNMEASURED_ROWS;
  return { first: Math.max(0, top - over), last: Math.min(count, top + inView + over) };
}

/** The `scrollTop` that brings row `index` fully into view, or null when it already is (or nothing is measured). */
export function revealTop(index: number, scrollTop: number, viewPx: number, row = SCM_ROW_PX): number | null {
  if (index < 0 || viewPx <= 0) return null;
  const top = index * row;
  const bottom = top + row;
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewPx) return Math.max(0, bottom - viewPx);
  return null;
}

/**
 * Where a key moves the list's selection: the row index to pick, or null for
 * a key the list does not take. `index` is the selected row's (-1: none);
 * `pageRows` how many rows a page is. Enter picks the selected row again
 * (it reads both sides again, as a click on it does).
 */
export function scmKeyStep(key: string, index: number, count: number, pageRows: number): number | null {
  if (count <= 0) return null;
  const last = count - 1;
  const page = Math.max(1, pageRows);
  const at = Math.min(index, last);
  switch (key) {
    case "ArrowDown":
      return at < 0 ? 0 : Math.min(last, at + 1);
    case "ArrowUp":
      return at < 0 ? 0 : Math.max(0, at - 1);
    case "Home":
      return 0;
    case "End":
      return last;
    case "PageDown":
      return Math.min(last, Math.max(0, at) + page);
    case "PageUp":
      return Math.max(0, at - page);
    case "Enter":
      return at < 0 ? null : at;
    default:
      return null;
  }
}

/** Where a project-relative path resolves, from the project's resolved root (for an open buffer's `abs`). */
export function absUnder(root: string | null | undefined, path: string): string | null {
  if (!root) return null;
  const top = root.replace(/\/+$/, "");
  return `${top}/${trim(path)}`;
}
