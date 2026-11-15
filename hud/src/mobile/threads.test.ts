import { describe, expect, it } from "vitest";
import type { WorkingStatus } from "./model";
import { bandOf, bands, buildSlots, informational, type Slot } from "./threads";
import { K1, K2, K3, T1, T2, T3, T4, mkEntry, mkRow, mkTask, randomCase } from "./threads.fixtures";


const keyOf = (s: Slot) => s.key;
const rowIds = (slots: Slot[]) => slots.flatMap((s) => s.rows.map((r) => r.id));

describe("buildSlots: every received row lands in exactly one slot (IN-8 for received rows)", () => {
  it("applies the four rules in order", () => {
    const slots = buildSlots([mkEntry(T1)], [
      mkRow("q:1", 3, { thread_id: T1 }),                                // 1. its thread is an entry
      mkRow("tq:2", 2, { task_id: K1, thread_id: T2 }),                  // 2. a task wins over an orphan thread
      mkRow("q:3", 4, { thread_id: T3 }),                                // 3. orphan thread
      mkRow("pr:4", 3),                                                   // 4. a row alone
      mkRow("q:5", 3, { thread_id: T1, task_id: K1 }),                   // 1 again: the thread is an entry
    ], null);
    expect(slots.map(keyOf).sort()).toEqual([`orphan:${T3}`, `row:pr:4`, `task:${K1}`, `thread:${T1}`].sort());
    expect(slots.find((s) => s.key === `thread:${T1}`)!.rows.map((r) => r.id)).toEqual(["q:1", "q:5"]);
    expect(slots.find((s) => s.key === `task:${K1}`)!.rows.map((r) => r.id)).toEqual(["tq:2"]);
  });

  it("keeps an entry with no rows, and puts rows in rank order", () => {
    const slots = buildSlots([mkEntry(T1), mkEntry(T2)], [mkRow("a", 5, { thread_id: T2 }), mkRow("b", 1, { thread_id: T2 })], []);
    expect(slots.find((s) => s.key === `thread:${T1}`)!.rows).toEqual([]);
    expect(slots.find((s) => s.key === `thread:${T2}`)!.rows.map((r) => r.id)).toEqual(["b", "a"]);
  });

  it("Sol 4: a running task with no row and no entry still has a slot, in Running", () => {
    const slots = buildSlots([], [], [mkTask(K1, "running"), mkTask(K2, "verifying"), mkTask(K3, "done")]);
    expect(slots.map(keyOf).sort()).toEqual([`task:${K1}`, `task:${K2}`]);
    expect(slots.map(bandOf)).toEqual(["running", "running"]);
    expect(bands(slots).running.map(keyOf).sort()).toEqual([`task:${K1}`, `task:${K2}`]);
  });

  it("a task row attaches to the same slot its running task seeded, with the task merged in", () => {
    const slots = buildSlots([], [mkRow("tq:" + K1 + ":0", 2, { kind: "task_question", task_id: K1 })], [mkTask(K1, "running")]);
    expect(slots).toHaveLength(1);
    expect(slots[0].task!.id).toBe(K1);
    expect(slots[0].rows).toHaveLength(1);
    expect(bandOf(slots[0])).toBe("needs");   // a rank 2 row outranks "running"
  });

  it("with entries null (working failed or not read) every row still lands, by rules 2 to 4", () => {
    const rows = [mkRow("a", 1, { thread_id: T1 }), mkRow("b", 2, { task_id: K1 }), mkRow("c", 6), mkRow("d", 8, { thread_id: T2 })];
    const slots = buildSlots(null, rows, null);
    expect(rowIds(slots).sort()).toEqual(["a", "b", "c", "d"]);
    expect(slots.map(keyOf).sort()).toEqual([`orphan:${T1}`, `orphan:${T2}`, `row:c`, `task:${K1}`].sort());
    for (const s of slots) expect(["needs", "running", "recent"]).toContain(bandOf(s));
  });

  it("titles an orphan from its row's origin and a task from its brief", () => {
    const slots = buildSlots(null, [mkRow("a", 3, { thread_id: T1, origin: "Docs chat" })], [mkTask(K1, "running", { brief: "Fix the build" })]);
    expect(slots.find((s) => s.kind === "orphan")!.title).toBe("Docs chat");
    expect(slots.find((s) => s.kind === "task")!.title).toBe("Fix the build");
  });
});

describe("bandOf is total, with an explicit Recent fallback", () => {
  const slot = (over: Partial<Slot>): Slot => ({ key: "k", kind: "row", threadId: null, taskId: null, entry: null, task: null,
    rows: [], title: "", ...over });

  it("rank 5 is Needs you and rank 6 is not", () => {
    expect(bandOf(slot({ rows: [mkRow("a", 5)] }))).toBe("needs");
    expect(bandOf(slot({ rows: [mkRow("a", 6)] }))).toBe("recent");
  });

  it("needs_input with no row is Needs you; working is Running; the rest are Recent", () => {
    const entry = (status: WorkingStatus) => slot({ kind: "thread", entry: mkEntry(T1, status) });
    expect(bandOf(entry("needs_input"))).toBe("needs");
    expect(bandOf(entry("working"))).toBe("running");
    for (const s of ["failed", "unread", "idle"] as const) expect(bandOf(entry(s))).toBe("recent");
    // An unknown label (a future word) is Recent, never nothing.
    expect(bandOf(slot({ kind: "thread", entry: { ...mkEntry(T1), status: "brand_new" as WorkingStatus } }))).toBe("recent");
  });

  it("a task slot: running and verifying are Running; no state or another state is Recent", () => {
    expect(bandOf(slot({ kind: "task", task: mkTask(K1, "running") }))).toBe("running");
    expect(bandOf(slot({ kind: "task", task: mkTask(K1, "verifying") }))).toBe("running");
    expect(bandOf(slot({ kind: "task", task: mkTask(K1, "done") }))).toBe("recent");
    expect(bandOf(slot({ kind: "task", task: null }))).toBe("recent");
    expect(bandOf(slot({ kind: "orphan", rows: [mkRow("a", 7)] }))).toBe("recent");
  });

  it("every slot shape buildSlots makes has a band, with entries and tasks null", () => {
    const slots = buildSlots(null, [mkRow("a", 1, { task_id: K1 }), mkRow("b", 8, { thread_id: T1 }), mkRow("c", 4)], null);
    expect(slots.map(bandOf).sort()).toEqual(["needs", "needs", "recent"]);
  });
});

describe("order", () => {
  it("Needs you: lowest rank, then soonest expiry, then label, then newest", () => {
    const slots = buildSlots([mkEntry(T1, "needs_input", { last: "2026-11-15T09:00:00Z" })], [
      mkRow("a", 3, { thread_id: T2 }), mkRow("b", 2, { thread_id: T3, expires_at: "2026-11-15T12:00:00Z" }),
      mkRow("c", 2, { thread_id: T4, expires_at: "2026-11-15T11:00:00Z" }),
    ], null);
    expect(bands(slots).needs.map(keyOf)).toEqual([`orphan:${T4}`, `orphan:${T3}`, `orphan:${T2}`, `thread:${T1}`]);
  });

  it("Recent: failed, unread, idle, then the slots with no label, newest first", () => {
    const slots = buildSlots([
      mkEntry(T1, "idle"), mkEntry(T2, "failed", { last: "2026-11-15T01:00:00Z" }), mkEntry(T3, "unread"),
    ], [mkRow("x", 7, { thread_id: T4, asked_at: "2026-11-15T12:00:00Z" })], null);
    expect(bands(slots).recent.map(keyOf)).toEqual([`thread:${T2}`, `thread:${T3}`, `thread:${T1}`, `orphan:${T4}`]);
  });

  it("Running: newest first", () => {
    const slots = buildSlots([mkEntry(T1, "working", { last: "2026-11-15T09:00:00Z" }), mkEntry(T2, "working", { last: "2026-11-15T10:00:00Z" })], [], []);
    expect(bands(slots).running.map(keyOf)).toEqual([`thread:${T2}`, `thread:${T1}`]);
  });

  it("only rank 8 row slots are informational: never a thread, a task or an orphan, never rank 1 to 7", () => {
    const slots = buildSlots([mkEntry(T1)], [mkRow("a", 8), mkRow("b", 8, { thread_id: T1 }), mkRow("c", 8, { task_id: K1 }),
      mkRow("d", 8, { thread_id: T2 }), mkRow("e", 7)], null);
    expect(slots.filter(informational).map(keyOf)).toEqual(["row:a"]);
    expect(bands(slots).info.map(keyOf)).toEqual(["row:a"]);
  });
});

// -- the property: every row, exactly once ----------------------------------------------------

describe("property: no row is dropped, doubled or misplaced (overlapping thread and task ids)", () => {
  it("holds for 400 generated cases", () => {
    for (let seed = 1; seed <= 400; seed++) {
      const { entries, tasks, rows } = randomCase(seed);
      const slots = buildSlots(entries, rows, tasks);
      // Exactly once.
      expect(rowIds(slots).sort(), `seed ${seed}`).toEqual(rows.map((x) => x.id).sort());
      // Keys are unique.
      expect(new Set(slots.map(keyOf)).size, `seed ${seed}`).toBe(slots.length);
      const split = bands(slots);
      // The bands partition the slots.
      const placed = [...split.needs, ...split.running, ...split.recent, ...split.info].map(keyOf);
      expect(placed.sort(), `seed ${seed}`).toEqual(slots.map(keyOf).sort());
      // Ranks 1 to 5 are only ever in Needs you; no slot with such a row is informational.
      for (const s of slots) {
        if (s.rows.some((x) => x.rank <= 5)) expect(split.needs, `seed ${seed}`).toContain(s);
        if (s.rows.some((x) => x.rank <= 7)) expect(split.info, `seed ${seed}`).not.toContain(s);
      }
      // Every running task has its slot, with or without rows.
      for (const t of tasks ?? []) {
        if (t.state === "running" || t.state === "verifying") expect(slots.map(keyOf), `seed ${seed}`).toContain(`task:${t.id}`);
      }
      // Every entry has its slot.
      for (const e of entries ?? []) expect(slots.map(keyOf), `seed ${seed}`).toContain(`thread:${e.thread_id}`);
    }
  });
});
