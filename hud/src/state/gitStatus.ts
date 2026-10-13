// Each project's git status (ED-9), polled while something shows it: a File
// pane's tree (its colours, lib/gitMarks.ts) or a gutter (state/gitGutter.ts,
// which reads HEAD's commit id to know when HEAD's text moved).
//
//  - **One poll per project**, however many trees and gutters watch it: every
//    `POLL_MS` while the window is visible, at once when the first watcher
//    arrives and when the window gets focus; never two requests at once. The
//    daemon caches its answer for 2 s, so a poll is cheap when nothing moved.
//  - **Watchers hear only a change**: an answer equal to the last one (by its
//    `gitKey` and HEAD) bumps nothing, so a tree's memoised rows do not redraw.
//  - **A refusal** (a repository whose own config names a program, a
//    credential folder) is kept as `error`, with no states, and asked again
//    only every `REFUSED_MS`.

import { api } from "../api";
import { gitKey } from "../lib/gitMarks";
import type { GitStatus as GitStatusData } from "../types";

export const POLL_MS = 3_000;
export const REFUSED_MS = 30_000;

interface Entry {
  data: GitStatusData | null;
  error: string | null;
  key: string;
  subs: Set<() => void>;
  ver: number;
  timer: ReturnType<typeof setTimeout> | null;
  inflight: boolean;
  refusedUntil: number;
}

export interface GitStatusDeps {
  fetch(projectId: string): Promise<GitStatusData>;
  now(): number;
  visible(): boolean;
}

const defaultDeps: GitStatusDeps = {
  fetch: (projectId) => api.gitStatus(projectId),
  now: () => Date.now(),
  visible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
};

export class GitStatusStore {
  private entries = new Map<string, Entry>();
  private focusHooked = false;

  constructor(private deps: GitStatusDeps = defaultDeps, private pollMs = POLL_MS) {}

  /** Watch a project's status; the poll runs while anyone watches. */
  subscribe = (projectId: string, fn: () => void): (() => void) => {
    let e = this.entries.get(projectId);
    if (!e) {
      e = { data: null, error: null, key: "", subs: new Set(), ver: 0, timer: null, inflight: false, refusedUntil: 0 };
      this.entries.set(projectId, e);
    }
    const entry = e;
    entry.subs.add(fn);
    this.hookFocus();
    if (entry.subs.size === 1) {
      void this.refresh(projectId);
      this.schedule(projectId);
    }
    return () => {
      entry.subs.delete(fn);
      if (!entry.subs.size) {
        if (entry.timer) clearTimeout(entry.timer);
        entry.timer = null;
      }
    };
  };

  get(projectId: string): GitStatusData | null {
    return this.entries.get(projectId)?.data ?? null;
  }

  error(projectId: string): string | null {
    return this.entries.get(projectId)?.error ?? null;
  }

  /** Bumped only when the project's status changed. */
  version(projectId: string): number {
    return this.entries.get(projectId)?.ver ?? 0;
  }

  /** Ask now (a watcher's first look, the window's focus). One request at a time. */
  async refresh(projectId: string): Promise<void> {
    const e = this.entries.get(projectId);
    if (!e || e.inflight || !e.subs.size || !this.deps.visible()) return;
    if (e.error && this.deps.now() < e.refusedUntil) return;
    e.inflight = true;
    try {
      const data = await this.deps.fetch(projectId);
      // The branch and `truncated` too: the Diff pane's memoized list says
      // both, and hears nothing the key misses (delta review of PR #67).
      const key = `${data?.repo ? 1 : 0}|${data?.head ?? ""}|${data?.branch ?? ""}|${data?.truncated ? 1 : 0}|${gitKey(data)}`;
      const changed = key !== e.key || e.error !== null;
      e.data = data;
      e.error = null;
      e.key = key;
      if (changed) this.bump(e);
    } catch (err: any) {
      const status = typeof err?.status === "number" ? err.status : 0;
      if (status === 403 || status === 404) {
        // Refused: no colours, and not asked again for a while.
        const was = e.error;
        e.data = null;
        e.error = String(err?.message || "refused");
        e.key = "";
        e.refusedUntil = this.deps.now() + REFUSED_MS;
        if (was !== e.error) this.bump(e);
      }
      // Anything else (the daemon restarting): keep what was shown, ask again next time.
    } finally {
      e.inflight = false;
    }
  }

  private bump(e: Entry) {
    e.ver += 1;
    for (const fn of Array.from(e.subs)) fn();
  }

  private schedule(projectId: string) {
    const e = this.entries.get(projectId);
    if (!e || !e.subs.size) return;
    if (e.timer) clearTimeout(e.timer);
    e.timer = setTimeout(() => {
      e.timer = null;
      void this.refresh(projectId).finally(() => this.schedule(projectId));
    }, this.pollMs);
  }

  private hookFocus() {
    if (this.focusHooked || typeof window === "undefined") return;
    this.focusHooked = true;
    window.addEventListener("focus", () => {
      for (const [id, e] of this.entries) if (e.subs.size) void this.refresh(id);
    });
  }
}

export const gitStatus = new GitStatusStore();
