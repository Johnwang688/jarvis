// The owner inbox's rows, held outside React (like state/subagents.ts), read
// from the owner-only `GET /inbox` (inbox A1) and read again on every
// `inbox_changed` bus record (`{ids, reason}`, never text: an empty `ids`
// means "read it all again", and so does any other, since the rows are the
// daemon's projection and the HUD never patches them itself), on every
// (re)connect, and after each answer.
//
// One read at a time: an event heard while a read is on its way asks for one
// more read after it, so the last read always started after the last event
// (never an older answer drawn over a newer one). Nothing is stored: a reload
// reads it all again. After each read the installed app's badge follows the
// count (lib/inbox `setBadge`), only when the count changed; a read that
// failed (unavailable or an error) clears it.

import { useSyncExternalStore } from "react";
import { api, ApiError } from "../api";
import { nextSnoozeEnd, parseInbox, setBadge, waitingCount, type InboxRow } from "../lib/inbox";

export type InboxStatus = "loading" | "ready" | "unavailable" | "error";

export interface InboxSnapshot {
  rows: InboxRow[];
  status: InboxStatus;
  /** The daemon's sentence for an unavailable or failed read. */
  message: string;
  /** How many reads have landed: the face check and the view's 409 path read it. */
  reads: number;
}

export interface InboxDeps {
  fetch: () => Promise<unknown>;
  badge: (count: number) => void;
  /** Inbox B4: a timer for the read at the earliest snooze's end (tests pass their own). */
  later?: (ms: number, fn: () => void) => () => void;
  now?: () => number;
}

/** How long after a snooze's end (by this window's clock) the backup read runs. */
export const SNOOZE_READ_SLACK_MS = 1_000;
/** The longest one backup wait is (a timer beyond 2^31 ms fires at once). */
export const SNOOZE_READ_MAX_MS = 3_600_000;

const defaultLater = (ms: number, fn: () => void) => {
  const t = setTimeout(fn, ms);
  return () => clearTimeout(t);
};

export class InboxStore {
  private snap: InboxSnapshot = { rows: [], status: "loading", message: "", reads: 0 };
  private listeners = new Set<() => void>();
  private running: Promise<void> | null = null;
  private again = false;
  private badged: number | null = null;
  private cancelSnooze: (() => void) | null = null;

  constructor(private deps: InboxDeps) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };

  snapshot = (): InboxSnapshot => this.snap;

  private set(next: Partial<InboxSnapshot>) {
    this.snap = { ...this.snap, ...next };
    for (const fn of Array.from(this.listeners)) fn();
  }

  /** Read again: now, or right after the read already on its way. Resolves when the rows are current. */
  refresh = (): Promise<void> => {
    if (this.running) {
      this.again = true;
      return this.running;
    }
    this.running = (async () => {
      try {
        do {
          this.again = false;
          await this.readOnce();
        } while (this.again);
      } finally {
        this.running = null;
      }
    })();
    return this.running;
  };

  private async readOnce() {
    let raw: unknown;
    try {
      raw = await this.deps.fetch();
    } catch (error) {
      // An older daemon has no route (404); a page that is not the owner's
      // own is refused (403). Either way the inbox is not there to show.
      const status = error instanceof ApiError ? error.status : 0;
      const message = error instanceof Error && error.message ? error.message : "the daemon could not be reached";
      this.set({ status: status === 404 || status === 403 ? "unavailable" : "error", message,
                 reads: this.snap.reads + 1 });
      this.badge(0);
      return;
    }
    const rows = parseInbox(raw);
    if (!rows) {
      this.set({ status: "error", message: "the daemon's inbox was not a list", reads: this.snap.reads + 1 });
      this.badge(0);
      return;
    }
    this.set({ rows, status: "ready", message: "", reads: this.snap.reads + 1 });
    this.badge(waitingCount(rows));
    this.armSnooze(rows);
  }

  /** Inbox B4, a backup to the daemon's `snooze_ended`: read again just after
   * the earliest snooze this read holds ends, so its row comes back even if
   * that event was missed. One timer, replaced by every read. */
  private armSnooze(rows: InboxRow[]) {
    this.cancelSnooze?.();
    this.cancelSnooze = null;
    const end = nextSnoozeEnd(rows);
    if (end === null) return;
    const now = (this.deps.now ?? Date.now)();
    const wait = Math.min(Math.max(end - now, 0) + SNOOZE_READ_SLACK_MS, SNOOZE_READ_MAX_MS);
    this.cancelSnooze = (this.deps.later ?? defaultLater)(wait, () => {
      this.cancelSnooze = null;
      void this.refresh();
    });
  }

  /** The badge follows the last read, only when it changes. A read that failed
   * clears it: a count the window can no longer vouch for is not shown, as
   * the sidebar shows none (review of PR #305). */
  private badge(count: number) {
    if (count === this.badged) return;
    this.badged = count;
    this.deps.badge(count);
  }
}

// Inbox B4: every row, hidden ones flagged (`?all=1`), so the view can offer
// "Show hidden (n)"; the count and the badge (`waitingCount`) leave them out.
export const inbox = new InboxStore({ fetch: () => api.inboxAll(), badge: (n) => setBadge(n) });

export function useInbox(store: InboxStore = inbox): InboxSnapshot {
  return useSyncExternalStore(store.subscribe, store.snapshot);
}
