// Two File panes on one file (WP-G2, plan §3.5): a split copies a clean File
// pane, so the same file can be open twice. A save in one pane tells the
// others in this window, and a clean pane on that file reads it again; a pane
// holding an unsaved edit is never reloaded under it — it says the file
// changed on disk, as the mtime guard would on its next save. (One shared
// buffer, WP-G3, was not built: decisions S-8.)

export interface Saved {
  projectId: string;
  path: string;
  mtime: number;
}

type Listener = (saved: Saved, from: object) => void;

const listeners = new Set<Listener>();

/** A pane (`from`) saved `path` in project `projectId`, now at `mtime`. */
export function fileSaved(saved: Saved, from: object) {
  for (const fn of Array.from(listeners)) fn(saved, from);
}

/** Hear every save in this window; the result stops hearing. */
export function onFileSaved(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/**
 * What a pane does about a save it heard: nothing when it is the pane that
 * saved, or another file, project or the version it already has; a clean pane
 * reads the file again; a pane with an unsaved edit keeps it and says why.
 */
export function afterSave(
  saved: Saved, mine: { projectId: string | null; path: string | null; mtime: number | null; dirty: boolean },
  self: boolean,
): "ignore" | "reload" | "note" {
  if (self || !mine.projectId || mine.path === null) return "ignore";
  if (saved.projectId !== mine.projectId || saved.path !== mine.path || saved.mtime === mine.mtime) return "ignore";
  return mine.dirty ? "note" : "reload";
}
