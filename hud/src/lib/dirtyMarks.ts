// Which rows of a File pane's tree wear the unsaved-changes dot (the owner's
// ask: "like VS Code"). Pure: the window reads the buffer registry
// (state/buffers.ts) and hands the answer to the tree.
//
//  - **Dirty is the buffer's**, one per file on disk (`abs`), so every pane
//    whose tree shows that file marks it: the buffer's own project by the
//    path it was read as, and any other project by where the file sits under
//    that project's root (one file reached from two projects is one buffer).
//  - **A collapsed folder holding a dirty file at any depth wears the dot**;
//    an open one does not, because its dirty child is on screen wearing it.
//  - **What a tree subscribes to is a key** (`dirtyKey`): the sorted dirty
//    paths joined, so a component compares strings and redraws only when a
//    buffer goes clean or dirty — never per keystroke, never on a save that
//    changed nothing it shows.

/** The part of a buffer view this needs (state/buffers.ts `BufferView`). */
export interface DirtyBuffer {
  abs: string;
  projectId: string;
  path: string;
  dirty: boolean;
  aliases?: readonly { projectId: string; path: string }[];
}

const trimSlashes = (p: string) => p.replace(/^\/+|\/+$/g, "");

/**
 * The folder a project's tree finds other projects' buffers under: where its
 * root resolves (`root_real`, the daemon's), since a buffer's `abs` is the
 * resolved path — a root reached through a symlink would match nothing as
 * written. The root as written when the daemon did not say (an older one, or
 * a root it could not resolve).
 */
export function treeRoot(project: { root: string; root_real?: string | null } | null | undefined): string | null {
  if (!project) return null;
  return project.root_real || project.root || null;
}

/**
 * The tree paths — relative to project `projectId`'s root — of every dirty
 * buffer that project's tree shows. `root` is the project's folder: a file
 * opened through another project is found by its absolute path under it.
 */
export function dirtyPathsIn(bufs: readonly DirtyBuffer[], projectId: string, root?: string | null): string[] {
  // A root of "/" is "" here, so every absolute path is under it.
  const top = root ? root.replace(/\/+$/, "") : null;
  const out = new Set<string>();
  for (const b of bufs) {
    if (!b.dirty) continue;
    const aliases = b.aliases?.filter(a => a.projectId === projectId);
    if (aliases?.length) {
      for (const alias of aliases) { const p = trimSlashes(alias.path); if (p) out.add(p); }
      continue;
    }
    if (b.projectId === projectId) {
      const p = trimSlashes(b.path);
      if (p) out.add(p);
      continue;
    }
    if (top !== null && b.abs.startsWith(top + "/")) {
      const p = trimSlashes(b.abs.slice(top.length + 1));
      if (p) out.add(p);
    }
  }
  return [...out].sort();
}

/** What a tree compares: one string, equal exactly when the dirty set is. */
export const dirtyKey = (paths: readonly string[]) => paths.join("\u0000");

export interface Marks {
  /** A file row: this file has unsaved changes. */
  file(path: string): boolean;
  /** A folder row: something under it, at any depth, has unsaved changes. */
  folder(path: string): boolean;
  readonly size: number;
}

/** The marks for a set of dirty paths: the files themselves and every folder above each. */
export function marksOf(paths: Iterable<string>): Marks {
  const files = new Set<string>();
  const folders = new Set<string>();
  for (const raw of paths) {
    const p = trimSlashes(raw);
    if (!p) continue;
    files.add(p);
    const parts = p.split("/");
    for (let i = 1; i < parts.length; i++) folders.add(parts.slice(0, i).join("/"));
  }
  return {
    file: (path) => files.has(trimSlashes(path)),
    folder: (path) => folders.has(trimSlashes(path)),
    size: files.size,
  };
}

/** The marks for a key `dirtyKey` made. */
export const marksOfKey = (key: string): Marks => marksOf(key ? key.split("\u0000") : []);

export const NO_MARKS: Marks = marksOf([]);

/**
 * Whether a tree row wears the dot: a file when it is dirty, a folder only
 * while it is collapsed and holds a dirty file.
 */
export function rowDot(marks: Marks, path: string, kind: "file" | "dir", open: boolean): boolean {
  return kind === "file" ? marks.file(path) : !open && marks.folder(path);
}

/**
 * The rows that show a dot, given the dirty files and which folders are
 * expanded (a folder not in `expanded` is collapsed, and what is inside it is
 * not drawn): each dirty file's dot lands on the outermost collapsed folder
 * above it, else on the file itself.
 */
export function visibleDots(dirty: Iterable<string>, expanded: ReadonlySet<string>): Set<string> {
  const marks = marksOf(dirty);
  const out = new Set<string>();
  for (const raw of dirty) {
    const p = trimSlashes(raw);
    if (!p) continue;
    const parts = p.split("/");
    let row = p;
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/");
      if (!expanded.has(dir)) {
        row = dir;
        break;
      }
    }
    const isDir = row !== p;
    if (rowDot(marks, row, isDir ? "dir" : "file", isDir ? false : true)) out.add(row);
  }
  return out;
}
