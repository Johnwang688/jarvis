// The explorer's git colours (ED-9): which state a File pane's tree row is
// drawn in, from `GET /projects/{id}/git/status` (state/gitStatus.ts). Pure.
//
//  - **A file** wears its own state, else that of an untracked or ignored
//    folder git reported whole (`dirs`, "" for the project's own folder).
//  - **A folder** wears the state git reported for it whole, else the
//    strongest state among the files under it — a conflict over a change
//    over an addition over an untracked file; ignored files never colour a
//    folder, and a deleted file (not in the tree) colours its folder as a
//    change.
//  - **What a tree subscribes to is a key** (`gitKey`): one string, equal
//    exactly when the states are, so the tree's memoised rows redraw when a
//    status changes — never per keystroke, never on a poll that saw nothing new.

export type GitState = "modified" | "added" | "untracked" | "ignored" | "renamed" | "deleted" | "conflict";

const STATES: ReadonlySet<string> = new Set(["modified", "added", "untracked", "ignored", "renamed", "deleted", "conflict"]);

/** What a folder holding a file in that state is drawn as, and how strongly it counts. */
const FOLDER_OF: Record<GitState, { as: GitState; rank: number } | null> = {
  conflict: { as: "conflict", rank: 4 },
  modified: { as: "modified", rank: 3 },
  deleted: { as: "modified", rank: 3 },
  renamed: { as: "added", rank: 2 },
  added: { as: "added", rank: 2 },
  untracked: { as: "untracked", rank: 1 },
  ignored: null,
};

/** The part of the status route's answer the colours need. */
export interface GitStatusLike {
  files?: Record<string, string> | null;
  dirs?: Record<string, string> | null;
}

const trim = (p: string) => p.replace(/^\/+|\/+$/g, "");

function entries(map: Record<string, string> | null | undefined): [string, GitState][] {
  if (!map || typeof map !== "object") return [];
  const out: [string, GitState][] = [];
  for (const [k, v] of Object.entries(map)) {
    if (typeof v === "string" && STATES.has(v)) out.push([trim(k), v as GitState]);
  }
  return out.sort((x, y) => (x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : 0));
}

/** One string, equal exactly when the colours would be: files and folders, sorted. */
export function gitKey(status: GitStatusLike | null | undefined): string {
  if (!status) return "";
  const f = entries(status.files);
  const d = entries(status.dirs);
  if (!f.length && !d.length) return "";
  return JSON.stringify([f, d]);
}

export interface GitMarks {
  /** A file row's state, or null for a clean one. */
  file(path: string): GitState | null;
  /** A folder row's state, or null. */
  folder(path: string): GitState | null;
  readonly size: number;
}

export const NO_GIT: GitMarks = { file: () => null, folder: () => null, size: 0 };

/** The marks for a key `gitKey` made. */
export function gitMarksOfKey(key: string): GitMarks {
  if (!key) return NO_GIT;
  let files: [string, GitState][];
  let dirs: [string, GitState][];
  try {
    [files, dirs] = JSON.parse(key);
  } catch {
    return NO_GIT;
  }
  return gitMarksOf({ files: Object.fromEntries(files), dirs: Object.fromEntries(dirs) });
}

/** The marks for a status answer. */
export function gitMarksOf(status: GitStatusLike | null | undefined): GitMarks {
  const fileMap = new Map(entries(status?.files));
  const dirMap = new Map(entries(status?.dirs));
  if (!fileMap.size && !dirMap.size) return NO_GIT;
  // Every folder above a changed file, at its strongest.
  const agg = new Map<string, { as: GitState; rank: number }>();
  for (const [path, state] of fileMap) {
    const up = FOLDER_OF[state];
    if (!up) continue;
    const parts = path.split("/");
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/");
      const had = agg.get(dir);
      if (!had || had.rank < up.rank) agg.set(dir, up);
    }
  }
  // A folder git reported whole (untracked, ignored), or the project itself.
  const whole = (path: string): GitState | null => {
    const parts = path ? path.split("/") : [];
    for (let i = parts.length; i >= 0; i--) {
      const at = dirMap.get(parts.slice(0, i).join("/"));
      if (at) return at;
    }
    return null;
  };
  return {
    file(path) {
      const p = trim(path);
      return fileMap.get(p) ?? whole(p);
    },
    folder(path) {
      const p = trim(path);
      return whole(p) ?? agg.get(p)?.as ?? null;
    },
    size: fileMap.size + dirMap.size,
  };
}

/** The words a row's tooltip uses. */
export const GIT_WORDS: Record<GitState, string> = {
  modified: "modified since the last commit",
  added: "added (staged, not committed)",
  untracked: "untracked: git does not know it",
  ignored: "ignored by git",
  renamed: "renamed (staged)",
  deleted: "deleted",
  conflict: "a merge conflict",
};
