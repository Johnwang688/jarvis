import { describe, expect, it } from "vitest";
import { canMoveThread, moveThreadTo, wouldMove } from "./threads";
import type { Thread } from "../types";

const thread = (over: Partial<Thread>): Thread => ({
  id: "t1", project_id: "p1", role: "chat", provider: "fast",
  provider_session_id: null, task_id: null, title: "desk chat",
  created: "", updated: "", turns: 0, cost_usd: 0, tokens: 0,
  model: null, effort: null,
  ...over,
});

const THREADS: Thread[] = [
  thread({ id: "t1" }),
  thread({ id: "t2", project_id: "p2" }),
  thread({ id: "kt1", task_id: "k1", role: "implementer" }),
];

describe("which threads may move", () => {
  it("a chat thread may", () => {
    expect(canMoveThread(THREADS[0]).ok).toBe(true);
  });

  it("a task thread may not, and says why", () => {
    const m = canMoveThread(THREADS[2]);
    expect(m.ok).toBe(false);
    // The window has to state the rule, not discover it from a 409.
    expect(m.why).toMatch(/move with their task/);
  });

  it("a thread that is not there is not movable either", () => {
    expect(canMoveThread(null).ok).toBe(false);
  });
});

describe("the move reducer", () => {
  it("re-parents the one thread and nothing else", () => {
    const after = moveThreadTo(THREADS, "t1", "p2");
    expect(after.find((t) => t.id === "t1")!.project_id).toBe("p2");
    expect(after.find((t) => t.id === "t2")!.project_id).toBe("p2");
    expect(after.find((t) => t.id === "kt1")!.project_id).toBe("p1");
  });

  it("never mutates, so the caller can put the old list back on a 409", () => {
    const before = JSON.stringify(THREADS);
    const after = moveThreadTo(THREADS, "t1", "p2");
    expect(JSON.stringify(THREADS)).toBe(before);
    expect(after).not.toBe(THREADS);
    expect(after[0]).not.toBe(THREADS[0]);
  });

  it("returns the list unchanged — by identity — when the move is a no-op", () => {
    // Identity is what the drop target reads: a row that lights up for a drop
    // that would do nothing is a promise the PATCH then breaks.
    expect(moveThreadTo(THREADS, "t1", "p1")).toBe(THREADS);
    expect(moveThreadTo(THREADS, "t1", "")).toBe(THREADS);
    expect(moveThreadTo(THREADS, "nope", "p2")).toBe(THREADS);
  });

  it("refuses to move a task thread however it is asked", () => {
    expect(moveThreadTo(THREADS, "kt1", "p2")).toBe(THREADS);
    expect(wouldMove(THREADS, "kt1", "p2")).toBe(false);
  });

  it("wouldMove agrees with the reducer", () => {
    expect(wouldMove(THREADS, "t1", "p2")).toBe(true);
    expect(wouldMove(THREADS, "t1", "p1")).toBe(false);
  });
});
