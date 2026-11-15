// Fixtures for the thread list's tests (PX-2): rows, entries, tasks and a seeded generator of cases
// with overlapping thread and task ids. Not a test file: shared by threads.test.ts and threadsView.test.ts.
import type { InboxRow } from "../lib/inbox";
import type { TaskView, WorkingItem, WorkingStatus } from "./model";

export const T1 = "aaaaaaaa", T2 = "bbbbbbbb", T3 = "cccccccc", T4 = "dddddddd";
export const K1 = "11111111", K2 = "22222222", K3 = "33333333";

export function mkRow(id: string, rank: number, over: Partial<InboxRow> = {}): InboxRow {
  return { id, kind: "question", rank, title: "t " + id, blocks: "", project_id: null, thread_id: null, task_id: null,
    asked_at: null, expires_at: null, origin: "", detail: {}, hidden: null, markable: false, ...over };
}
export function mkEntry(thread_id: string, status: WorkingStatus = "idle", over: Partial<WorkingItem> = {}): WorkingItem {
  return { thread_id, project_id: "99999999", project: "Calc", title: "chat " + thread_id, status, last: "2026-11-15T10:00:00Z", ...over };
}
export function mkTask(id: string, state: string, over: Partial<TaskView> = {}): TaskView {
  return { id, project_id: "99999999", brief: "brief " + id, state, updated: "2026-11-15T10:00:00Z", step: 1, steps: 3,
    openQuestion: "", plan: [], report: null, questions: [], branch: "", ...over };
}

/** A small seeded generator (mulberry32): the same cases every run. */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function randomCase(seed: number) {
  const r = rng(seed);
  const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(r() * xs.length)];
  const threads = [T1, T2, T3, T4], taskIds = [K1, K2, K3];
  const kinds = ["approval", "question", "task_question", "proposal", "task", "missed", "check", "pr", "decision"];
  const statuses: WorkingStatus[] = ["needs_input", "failed", "unread", "working", "idle"];
  const states = ["running", "verifying", "blocked", "done", "failed", "intake", ""];
  const entries = r() < 0.2 ? null : threads.filter(() => r() < 0.5).map((t) => mkEntry(t, pick(statuses)));
  const tasks = r() < 0.2 ? null : taskIds.filter(() => r() < 0.6).map((t) => mkTask(t, pick(states)));
  const rows: InboxRow[] = [];
  const n = Math.floor(r() * 14);
  for (let i = 0; i < n; i++) {
    rows.push(mkRow(`${pick(["q", "appr", "tq", "pr"])}:${i}`, 1 + Math.floor(r() * 8), {
      kind: pick(kinds), thread_id: r() < 0.55 ? pick(threads) : null, task_id: r() < 0.4 ? pick(taskIds) : null,
      asked_at: r() < 0.5 ? "2026-11-15T10:00:00Z" : null, expires_at: r() < 0.3 ? "2026-11-15T11:00:00Z" : null,
    }));
  }
  return { entries, tasks, rows };
}

