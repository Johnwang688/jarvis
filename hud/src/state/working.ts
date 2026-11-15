// The owner's working set (WS-2), held outside React like state/inbox.ts: read
// from the owner-only `GET /working` and read again on every `working_changed`
// bus record (no ids, no data: the daemon's list is a projection and the HUD
// never patches it), on every (re)connect, and after each Done or Undo.
//
// One read at a time: an event heard while a read is on its way asks for
// exactly one more read after it, so the last read always started after the
// last event. A failed read keeps what was drawn (a message says so); an
// older daemon (404) or a page that is not the owner's (403) has no list at
// all, and the sidebar draws no section.

import { useSyncExternalStore } from "react";
import { api, ApiError } from "../api";
import { parseWorking, type WorkingEntry } from "../lib/working";
import type { InboxRow } from "../lib/inbox";

export type WorkingStatus = "loading" | "ready" | "unavailable" | "error";

export interface WorkingSnapshot {
  entries: WorkingEntry[];
  needsYou: InboxRow[];
  count: number;
  pending: boolean;
  status: WorkingStatus;
  message: string;
  /** A read has landed (and the daemon has not since said it has no list): what is drawn is the daemon's,
   * possibly stale after a failed read. False until then, whatever the error. */
  loaded: boolean;
}

export type DoneResult =
  | { kind: "done"; token: string }
  | { kind: "stale" }
  | { kind: "gone" }
  | { kind: "error"; message: string };

export type UndoResult = { kind: "undone" } | { kind: "stale" } | { kind: "error"; message: string };

export interface WorkingDeps {
  fetch: () => Promise<unknown>;
  done: (id: string, seq: number) => Promise<unknown>;
  undo: (id: string, token: string) => Promise<unknown>;
}

const EMPTY: WorkingSnapshot = {
  entries: [], needsYou: [], count: 0, pending: false, status: "loading", message: "", loaded: false,
};

const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : "the daemon could not be reached");

/** Structural equality of two snapshots (the lists are small and plain). */
function sameSnapshot(a: WorkingSnapshot, b: WorkingSnapshot): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

export class WorkingStore {
  private snap: WorkingSnapshot = EMPTY;
  private listeners = new Set<() => void>();
  private running: Promise<void> | null = null;
  private again = false;

  constructor(private deps: WorkingDeps) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };

  snapshot = (): WorkingSnapshot => this.snap;

  /** Publish a change. A read that says what the last one said publishes nothing: no listener
   * (so no sidebar row) is redrawn for a refresh that found the list as it was. */
  private set(next: Partial<WorkingSnapshot>) {
    const merged = { ...this.snap, ...next };
    if (sameSnapshot(this.snap, merged)) return;
    this.snap = merged;
    for (const fn of Array.from(this.listeners)) fn();
  }

  /** Read again: now, or right after the read already on its way. Resolves when the list is current. */
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
      const status = error instanceof ApiError ? error.status : 0;
      const gone = status === 404 || status === 403;
      this.set({ status: gone ? "unavailable" : "error", message: messageOf(error), ...(gone ? { entries: [], needsYou: [], count: 0, pending: false, loaded: false } : {}) });
      return;
    }
    const view = parseWorking(raw);
    if (!view) {
      this.set({ status: "error", message: "the daemon's working list was not a list" });
      return;
    }
    this.set({ entries: view.entries, needsYou: view.needs_you, count: view.count, pending: view.pending,
               status: "ready", message: "", loaded: true });
  }

  /** Done for one entry, with the `seq` this window drew it with. A 409 is not
   * an error: the list moved on, so it is read again and drawn anew. */
  done = async (id: string): Promise<DoneResult> => {
    const entry = this.snap.entries.find((e) => e.thread_id === id);
    if (!entry) return { kind: "gone" };
    try {
      const body = await this.deps.done(id, entry.seq);
      void this.refresh();
      const token = (body as { token?: unknown } | null)?.token;
      return typeof token === "string" && token ? { kind: "done", token } : { kind: "stale" };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        void this.refresh();
        return { kind: "stale" };
      }
      return { kind: "error", message: messageOf(error) };
    }
  };

  undo = async (id: string, token: string): Promise<UndoResult> => {
    try {
      await this.deps.undo(id, token);
      void this.refresh();
      return { kind: "undone" };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        void this.refresh();
        return { kind: "stale" };
      }
      return { kind: "error", message: messageOf(error) };
    }
  };
}

export const working = new WorkingStore({
  fetch: () => api.getWorking(),
  done: (id, seq) => api.workingDone(id, seq),
  undo: (id, token) => api.workingUndo(id, token),
});

export function useWorking(store: WorkingStore = working): WorkingSnapshot {
  return useSyncExternalStore(store.subscribe, store.snapshot);
}
