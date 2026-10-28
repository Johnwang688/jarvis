// The morning review's report (MR, lib/review.ts), held outside React like
// state/inbox.ts: read from the owner-only `GET /inbox/review` when the view
// opens, again on every `inbox_changed` (the report is the daemon's
// projection of the same rows), on every (re)connect, and after each answer.
// One read at a time; an event heard during a read asks for one more after
// it. Nothing is stored.

import { useSyncExternalStore } from "react";
import { api, ApiError } from "../api";
import { parseReview, type ReviewReport } from "../lib/review";

export type ReviewStatus = "loading" | "ready" | "unavailable" | "error";

export interface ReviewSnapshot {
  report: ReviewReport | null;
  status: ReviewStatus;
  message: string;
  reads: number;
}

export class ReviewStore {
  private snap: ReviewSnapshot = { report: null, status: "loading", message: "", reads: 0 };
  private listeners = new Set<() => void>();
  private running: Promise<void> | null = null;
  private again = false;

  constructor(private fetch: () => Promise<unknown>) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };

  snapshot = (): ReviewSnapshot => this.snap;

  private set(next: Partial<ReviewSnapshot>) {
    this.snap = { ...this.snap, ...next };
    for (const fn of Array.from(this.listeners)) fn();
  }

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
      raw = await this.fetch();
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      const message = error instanceof Error && error.message ? error.message : "the daemon could not be reached";
      this.set({ status: status === 404 || status === 403 ? "unavailable" : "error", message, reads: this.snap.reads + 1 });
      return;
    }
    const report = parseReview(raw);
    if (!report) {
      this.set({ status: "error", message: "the daemon's review was not one", reads: this.snap.reads + 1 });
      return;
    }
    this.set({ report, status: "ready", message: "", reads: this.snap.reads + 1 });
  }
}

export const review = new ReviewStore(() => api.review());

export function useReview(store: ReviewStore = review): ReviewSnapshot {
  return useSyncExternalStore(store.subscribe, store.snapshot);
}
