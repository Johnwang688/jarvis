// The open chat's transcript: what the phone holds of one chat's log, how a page joins it, and the reads
// that fetch those pages (PR 384 follow-up). No DOM here; PhoneApp draws and owns the network.
//
// **The window.** The daemon's log rows carry `seq`, the index of the log record each came from
// (jarvis/v2/remote_api.py `log`). Records the phone is not shown (tool events, thinking) have no row, so
// seqs are sparse. A window is `[base, next)` plus the rows in it, and its invariant is that every seq in
// that range is either a row the window holds or provably absent: `next` is the cursor, and it never moves
// past a seq the phone has not accounted for.
//
// **What a page proves.** There are exactly two requests:
//
// - **tail** (no `after`; opening a chat, or a chat with no window yet): the newest `LOG_TAIL` rows. With
//   `older: false` nothing came before them, so it proves `[0, next)`; with `older: true` it proves
//   `[first row's seq, next)`.
// - **forward** (`after=cursor`): the rows from the cursor on. It proves `[after, next)`. With `more`, `next`
//   is the last row's seq + 1 and the next page continues from there.
//
// The phone does not page backwards: older rows are at the desk (the module doc of remote_api.py), so
// "load more" here is a forward page after a `more`, not an older one.
//
// A partial page (`decodeLog`) already ends just after its last valid line, so it proves only that prefix.
//
// **Joining** (`mergeLog`): a page whose range touches or overlaps the window extends it on either side.
// Inside the window the window's rows stand (nothing there is unknown), so a repeat is never drawn twice and
// an older snapshot cannot erase or rewind anything. A tail entirely after the window (the chat grew by more
// than a tail while the phone was away) replaces it: splicing it on would leave a hole, and filling the hole
// could mean reading the whole log. A page entirely before the window is old news and is dropped.
//
// **Reads** (`ChatLogReads`, one per opening of a chat): at most one request in flight per kind, and a
// re-read owed per kind when another is asked for while one is in flight. A generation, raised by each
// opening, marks a response stale: it is dropped, but the re-read it was holding back still runs. Nothing
// is shared between chats, so a request that never answers holds back only its own chat, and a chat that is
// closed and opened again starts with fresh reads (the closed opening's request is abandoned, not waited for).

import type { LogPage, LogRow } from "./model";

export type LogKind = "tail" | "forward";

/**
 * Every seq in `[base, next)` is in `rows` (sorted, one per seq) or provably absent. (Rows at or past `next`
 * appear only if a page broke the daemon's rule; see `coverage`.)
 */
export interface LogWindow { rows: LogRow[]; base: number; next: number }

/**
 * What a join did to the window: "none" (nothing new), "append" (`added` after the old end), "insert"
 * (rows, or the start, before the old start), "reset" (a newer tail replaced the window) or "dropped"
 * (the page was older than the window).
 */
export type LogChange = "none" | "append" | "insert" | "reset" | "dropped";

export interface LogJoin { win: LogWindow; change: LogChange; added: LogRow[] }

/**
 * The range of seqs a page accounts for (see the header). `after` is the forward request's cursor. The
 * cursor never passes the page's own `next`: a row at or past it (which the daemon never sends) is kept as
 * a row, but proves nothing about the seqs before it.
 */
export function coverage(kind: LogKind, after: number | null, page: LogPage): { from: number; to: number } {
  const from = kind === "forward" ? Math.max(0, after ?? 0)
    : page.older ? (page.rows[0]?.seq ?? page.next) : 0;
  return { from, to: Math.max(from, page.next) };
}

const bySeq = (a: LogRow, b: LogRow) => a.seq - b.seq;

/** A page joined to the window (null: no window yet). Pure: the old window is not changed. */
export function mergeLog(win: LogWindow | null, kind: LogKind, after: number | null, page: LogPage): LogJoin {
  const { from, to } = coverage(kind, after, page);
  // The page's rows from its start on, in order, one per seq (a row before a forward page's cursor is not news).
  const rows: LogRow[] = [];
  for (const row of [...page.rows].sort(bySeq)) {
    if (!Number.isInteger(row.seq) || row.seq < from) continue;
    if (rows.length && row.seq === rows[rows.length - 1].seq) continue;
    rows.push(row);
  }
  if (!win) return { win: { rows, base: from, next: to }, change: "reset", added: rows };
  if (to < win.base) return { win, change: "dropped", added: [] };
  if (from > win.next) {
    // A forward page always starts at or before the cursor it was asked from, so only a tail lands here.
    return kind === "tail" ? { win: { rows, base: from, next: to }, change: "reset", added: rows }
      : { win, change: "dropped", added: [] };
  }
  // Inside [base, next) the window already knows every seq: only what lies outside it is news.
  const have = new Set(win.rows.map((row) => row.seq));
  const below = rows.filter((row) => row.seq < win.base);
  const above = rows.filter((row) => row.seq >= win.next && !have.has(row.seq));
  const base = Math.min(win.base, from);
  const next = Math.max(win.next, to);
  const last = win.rows.length ? win.rows[win.rows.length - 1].seq : -1;
  const inOrder = above.every((row) => row.seq > last);
  const merged = [...below, ...win.rows, ...above];
  if (!inOrder) merged.sort(bySeq);
  const change: LogChange = base < win.base || !inOrder ? "insert" : above.length ? "append" : "none";
  return { win: { rows: merged, base, next }, change, added: above };
}

/** What one opening's reads need from the app. */
export interface LogReadHost {
  /** This opening is the chat on screen. A response that arrives otherwise is stale. */
  shown(): boolean;
  /** A window exists (a forward read needs its cursor; without one a tail is read instead). */
  hasWindow(): boolean;
  /**
   * One request, applied only while `live()` holds. True when another forward page should follow (the page
   * said `more`); it is asked for from the window's cursor as it is then.
   */
  readPage(kind: LogKind, live: () => boolean): Promise<boolean>;
}

interface Slot { busy: boolean; again: boolean }

/** One opening's log reads: see the header. */
export class ChatLogReads {
  /** Raised by each opening (the same chat opened again included): responses of earlier ones are stale. */
  generation = 0;
  private readonly slots: Record<LogKind, Slot> = { tail: { busy: false, again: false }, forward: { busy: false, again: false } };

  constructor(private readonly host: LogReadHost) {}

  /** The chat opened again: what is in flight is stale from now on. */
  reopen(): void { this.generation += 1; }

  /** The newest rows (the opening read). */
  refresh(): Promise<void> { return this.run("tail"); }

  /** The rows after the cursor, page by page; asked again while one is in flight, it runs once more after. */
  follow(): Promise<void> { return this.run("forward"); }

  /** For tests: a request of this kind is in flight or being drained. */
  busy(kind: LogKind): boolean { return this.slots[kind].busy; }

  private async run(kind: LogKind): Promise<void> {
    // No cursor to read after: the newest rows instead (one tail, owed once, however many follows ask meanwhile).
    if (kind === "forward" && !this.host.hasWindow()) return this.run("tail");
    const slot = this.slots[kind];
    if (slot.busy) { slot.again = true; return; }
    slot.busy = true;
    try {
      do {
        slot.again = false;
        // Closed (or not on screen): the owed read goes with it. Opening the chat again reads it anyway.
        if (!this.host.shown()) return;
        // The window went (the chat's tail was refused): as above.
        if (kind === "forward" && !this.host.hasWindow()) { void this.run("tail"); return; }
        const generation = this.generation;
        const live = () => this.generation === generation && this.host.shown();
        let more = await this.host.readPage(kind, live);
        if (kind === "tail") {
          // A tail cut short by its size reads on. Not awaited: neither kind ever waits on the other's request.
          if (more && live()) void this.run("forward");
        } else {
          while (more && live()) more = await this.host.readPage("forward", live);
        }
        // A stale response ends here; `again` (asked for while it was out) still runs.
      } while (slot.again);
    } finally {
      slot.busy = false;
      slot.again = false;
    }
  }
}
