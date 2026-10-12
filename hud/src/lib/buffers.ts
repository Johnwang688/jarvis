// The editor's buffers and tabs, as pure rules (ED-2, editor plan §2.3–§2.4).
// The registry that holds the models is `state/buffers.ts`; everything it and
// the File pane decide is here, so it can be tested without Monaco.

/**
 * What the editor may do with a file (`jarvis/v2/editor_policy.py`), and
 * `unknown`: a kind this window does not know, which it treats as the most
 * careful one it can still show — read-only, and closed to Jarvis.
 */
export type Kind = "text" | "env" | "gate" | "safety" | "binary" | "withheld" | "unknown";

/** Kinds the editor may save — when the read also says `editable`. Everything else opens read-only (or not at all). */
export const EDITABLE: ReadonlySet<Kind> = new Set<Kind>(["text", "env"]);

/** A kind from the daemon. Fails closed: anything unrecognised is `unknown`, never `text` (review of PR #49, L2). */
export function parseKind(v: unknown): Kind {
  return v === "text" || v === "env" || v === "gate" || v === "safety" || v === "binary" || v === "withheld"
    ? v : "unknown";
}

/**
 * Whether the editor may save what a read returned: the daemon's `editable`
 * and an editable kind, both. An env file that is not UTF-8 is `env` and not
 * editable (review of PR #49, MED1); a read that does not say is read-only.
 */
export function editableOf(kind: Kind, editable: unknown): boolean {
  return EDITABLE.has(kind) && editable === true;
}

/**
 * Jarvis may read a buffer's text: never an env file (the owner's `.env*`
 * rule), never a withheld one, and never one of a kind this window does not
 * know. Anything that would carry a buffer's text toward Jarvis — add to
 * chat today, find in files and the rest of ED-7 tomorrow — asks this first.
 */
export function jarvisMayRead(kind: Kind): boolean {
  return kind !== "env" && kind !== "withheld" && kind !== "unknown";
}

// ---- tabs -------------------------------------------------------------------

/**
 * A File pane's tabs: paths relative to the pane's project, in order; the
 * active one; and the preview tab (italic), which the next single click in
 * the tree replaces — until it is double-clicked or edited, which pins it.
 */
export interface Tabs {
  paths: string[];
  active: string | null;
  preview: string | null;
}

export const NO_TABS: Tabs = Object.freeze({ paths: [], active: null, preview: null }) as Tabs;

export const TABS_MAX = 40;
const PATH_MAX = 1024;

const isPath = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= PATH_MAX;

/** A stored value, field by field: anything malformed falls back on its own. */
export function parseTabs(raw: unknown): Tabs | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const v = raw as Record<string, unknown>;
  const paths = Array.isArray(v.paths) ? [...new Set(v.paths.filter(isPath))].slice(0, TABS_MAX) : [];
  if (!paths.length) return null;
  const active = isPath(v.active) && paths.includes(v.active) ? v.active : paths[0];
  const preview = isPath(v.preview) && paths.includes(v.preview) ? v.preview : null;
  return { paths, active, preview };
}

/**
 * Open `path`: activate its tab, or add one. A single click (`pin` false)
 * replaces the preview tab — unless the preview's buffer holds an edit
 * (`keepPreview`), which pins it instead — and the new tab is the preview. A
 * double click (`pin` true) adds a pinned tab, or pins the existing one.
 *
 * Past TABS_MAX the oldest **clean** tab other than the one opening goes
 * (`dirty` says which hold an unsaved edit); when every other tab holds one,
 * nothing opens and null comes back — the caller says why. A tab on an
 * unsaved edit is never closed behind the owner's back (review of PR #49, M1).
 */
export function openTab(
  t: Tabs,
  path: string,
  opts: { pin?: boolean; keepPreview?: (path: string) => boolean; dirty?: (path: string) => boolean } = {},
): Tabs | null {
  const pin = !!opts.pin;
  if (t.paths.includes(path)) {
    return { ...t, active: path, preview: pin && t.preview === path ? null : t.preview };
  }
  let paths = t.paths.slice();
  let preview = t.preview;
  if (preview && !pin && !(opts.keepPreview?.(preview))) {
    paths[paths.indexOf(preview)] = path;
    return { paths, active: path, preview: path };
  }
  if (preview && opts.keepPreview?.(preview)) preview = null;
  const at = t.active ? paths.indexOf(t.active) + 1 : paths.length;
  paths.splice(at, 0, path);
  if (paths.length > TABS_MAX) {
    const drop = paths.find((p) => p !== path && !opts.dirty?.(p));
    if (drop === undefined) return null;
    paths = paths.filter((p) => p !== drop);
    if (preview === drop) preview = null;
  }
  return { paths, active: path, preview: pin ? preview : path };
}

/** Pin a tab (a double click on it, or an edit): it is no longer the preview. */
export function pinTab(t: Tabs, path: string): Tabs {
  return t.preview === path ? { ...t, preview: null } : t;
}

/** Close a tab: the next one to the right becomes active, else the one to the left. */
export function closeTab(t: Tabs, path: string): Tabs {
  const at = t.paths.indexOf(path);
  if (at < 0) return t;
  const paths = t.paths.filter((p) => p !== path);
  const active = t.active !== path ? t.active : paths[Math.min(at, paths.length - 1)] ?? null;
  return { paths, active, preview: t.preview === path ? null : t.preview };
}

export function sameTabs(a: Tabs | null | undefined, b: Tabs | null | undefined): boolean {
  if (!a || !b) return !a && !b;
  return a.active === b.active && a.preview === b.preview && a.paths.length === b.paths.length
    && a.paths.every((p, i) => p === b.paths[i]);
}

// ---- the change poll --------------------------------------------------------

/** A buffer's conflict with the disk: changed (with the disk's etag) or gone. */
export type Conflict = { state: "changed"; etag: string; again?: boolean } | { state: "gone" };

/** One row of `POST …/files/stat`. */
export interface StatRow {
  etag?: string | null;
  gone?: boolean;
  kind?: string;
  error?: number;
}

export type PollAction = "none" | "reload" | "conflict" | "gone" | "clear";

/**
 * What a poll's answer means for one buffer (plan §2.4's table):
 *  - the disk still holds what the buffer was read or saved as: nothing (or
 *    the conflict clears, when the disk came back to it);
 *  - changed under a clean buffer: reload it in place;
 *  - changed under an edit: the conflict bar, never a reload;
 *  - gone: the bar, "Save to recreate" or "Close".
 * A save in flight decides for itself; a row the poll could not read leaves
 * the buffer alone.
 */
export function pollAction(
  buf: { etag: string | null; dirty: boolean; saving: boolean; conflict: Conflict | null },
  row: StatRow | undefined,
): PollAction {
  if (!row || row.error !== undefined || row.kind === "withheld" || buf.saving) return "none";
  if (row.gone) return buf.conflict?.state === "gone" ? "none" : "gone";
  if (typeof row.etag !== "string") return "none";
  if (row.etag === buf.etag) return buf.conflict ? "clear" : "none";
  if (!buf.dirty) return "reload";
  if (buf.conflict?.state === "changed" && buf.conflict.etag === row.etag) return "none";
  return "conflict";
}

/**
 * The smallest single edit that turns `prev` into `next`: their common start
 * and end kept, so a reload in place moves no cursor or scroll it need not.
 * Never splits a surrogate pair or a CRLF.
 */
export function minimalEdit(prev: string, next: string): { start: number; end: number; text: string } | null {
  if (prev === next) return null;
  const max = Math.min(prev.length, next.length);
  let a = 0;
  while (a < max && prev.charCodeAt(a) === next.charCodeAt(a)) a++;
  if (a > 0 && isHigh(prev.charCodeAt(a - 1))) a--;
  if (a > 0 && prev[a - 1] === "\r") a--;
  let b = 0;
  while (b < max - a && prev.charCodeAt(prev.length - 1 - b) === next.charCodeAt(next.length - 1 - b)) b++;
  if (b > 0 && isLow(prev.charCodeAt(prev.length - b))) b--;
  if (b > 0 && prev[prev.length - b] === "\n" && prev[prev.length - b - 1] === "\r") b--;
  return { start: a, end: prev.length - b, text: next.slice(a, next.length - b) };
}

const isHigh = (c: number) => c >= 0xd800 && c <= 0xdbff;
const isLow = (c: number) => c >= 0xdc00 && c <= 0xdfff;

/** The line-ending style of a text: LF, CRLF, CR, mixed, or null with none. */
export function eolOf(text: string): "LF" | "CRLF" | "CR" | "mixed" | null {
  let lf = 0;
  let crlf = 0;
  let cr = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c === 13) {
      if (text.charCodeAt(i + 1) === 10) {
        crlf++;
        i++;
      } else cr++;
    } else if (c === 10) lf++;
  }
  const kinds = ([["LF", lf], ["CRLF", crlf], ["CR", cr]] as const).filter(([, n]) => n > 0);
  if (!kinds.length) return null;
  return kinds.length === 1 ? kinds[0][0] : "mixed";
}

// ---- who changed it ---------------------------------------------------------

/** A running turn's write that named a file (`tool_started`, any thread): its absolute path. */
export interface Touch {
  path: string;
  writer: string;
  at: number;
}

export const TOUCH_MS = 5 * 60_000;
export const TOUCHES_MAX = 200;

/**
 * The tools that write a file, by name, and where each names it: Claude's
 * Write/Edit/MultiEdit/NotebookEdit, Codex's `apply_patch` (`paths`, or
 * `changes[].path`), and v1's `write_file`/`edit_file`. A read names a file
 * too, and is no writer (review of PR #49).
 */
const WRITE_TOOLS: Record<string, readonly string[]> = {
  Write: ["file_path"], Edit: ["file_path"], MultiEdit: ["file_path"], NotebookEdit: ["notebook_path"],
  write_file: ["path"], edit_file: ["path"], apply_patch: ["paths"],
};

/** Join a relative path onto a folder, lexically (`.` and `..` resolved); null if it climbs out of `/`. */
function joinAbs(dir: string, rel: string): string | null {
  const out: string[] = [];
  for (const part of `${dir}/${rel}`.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!out.length) return null;
      out.pop();
    } else out.push(part);
  }
  return "/" + out.join("/");
}

/**
 * The absolute paths a tool call writes, or none: only a write tool's, and a
 * relative one only against the folder its thread works in (`cwd`) — never
 * matched by a suffix, which named a writer for a same-named file in another
 * project (review of PR #49).
 */
export function writtenPaths(name: unknown, args: unknown, cwd?: string | null): string[] {
  if (typeof name !== "string" || !Object.prototype.hasOwnProperty.call(WRITE_TOOLS, name)) return [];
  if (!args || typeof args !== "object") return [];
  const a = args as Record<string, unknown>;
  const raw: unknown[] = [];
  for (const key of WRITE_TOOLS[name]) {
    const v = a[key];
    if (Array.isArray(v)) raw.push(...v);
    else raw.push(v);
  }
  if (name === "apply_patch" && Array.isArray(a.changes)) {
    for (const c of a.changes) raw.push(c && typeof c === "object" ? (c as Record<string, unknown>).path : null);
  }
  const out = new Set<string>();
  for (const v of raw.slice(0, 64)) {
    if (typeof v !== "string" || !v.trim() || v.length > PATH_MAX) continue;
    const p = v.trim();
    const abs = p.startsWith("/") ? joinAbs("", p) : cwd && cwd.startsWith("/") ? joinAbs(cwd, p) : null;
    if (abs) out.add(abs);
  }
  return [...out];
}

/**
 * The writer the activity feed last saw write `abs` (newest first, within
 * TOUCH_MS): absolute paths only, matched exactly.
 */
export function writerOf(touches: readonly Touch[], abs: string, now: number): string | null {
  for (let i = touches.length - 1; i >= 0; i--) {
    const t = touches[i];
    if (now - t.at > TOUCH_MS) break;
    if (t.path === abs) return t.writer;
  }
  return null;
}

// ---- add to chat --------------------------------------------------------------

/**
 * What "Add to chat" puts in the selected chat's box, unsent: where it is
 * (`path:L10-24`) and the lines, fenced with a fence longer than any run of
 * backticks inside them.
 */
export function chatExcerpt(path: string, text: string, from: number, to: number): string {
  const where = from === to ? `${path}:L${from}` : `${path}:L${from}-${to}`;
  const runs = text.match(/`+/g) || [];
  const fence = "`".repeat(Math.max(3, ...runs.map((r) => r.length + 1)));
  return `${where}\n${fence}\n${text.replace(/\n$/, "")}\n${fence}\n`;
}
