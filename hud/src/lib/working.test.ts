// lib/working: the parse, D-9's order, the words, and the fold choice.

import { describe, expect, it } from "vitest";
import type { ActivityView } from "./activity";
import { attachedHint, kindLabel, loadSectionOpen, orderEntries, parseWorking, rowPending, saveSectionOpen,
  type WorkingEntry } from "./working";

const e = (id: string, last: string): WorkingEntry => ({
  thread_id: id, project_id: "p1", since: "", last, seq: 1, status: "idle", attached: [], why_not: null,
});
const act = (threads: ActivityView["threads"]): ActivityView => ({ threads, tasks: {} });

describe("orderEntries (D-9)", () => {
  it("needs you, failed, done-unread, working, your move; newest last first within a label", () => {
    const entries = [
      e("idle-new", "2026-11-14T12:00:00Z"), e("idle-old", "2026-11-14T08:00:00Z"),
      e("work", "2026-11-14T09:00:00Z"), e("unread", "2026-11-14T09:30:00Z"),
      e("fail", "2026-11-14T07:00:00Z"), e("need-old", "2026-11-14T06:00:00Z"), e("need-new", "2026-11-14T11:00:00Z"),
    ];
    const view = act({ work: "working", unread: "unread", fail: "failed", "need-old": "needs_input", "need-new": "needs_input" });
    expect(orderEntries(entries, view).map((x) => x.thread_id)).toEqual(
      ["need-new", "need-old", "fail", "unread", "work", "idle-new", "idle-old"]);
  });

  it("does not change what it was given, and keeps the daemon's order on a tie", () => {
    const entries = [e("b", "2026-11-14T10:00:00Z"), e("a", "2026-11-14T10:00:00Z")];
    const copy = [...entries];
    expect(orderEntries(entries, act({})).map((x) => x.thread_id)).toEqual(["b", "a"]);
    expect(entries).toEqual(copy);
  });
});

describe("parseWorking", () => {
  it("keeps entries and needs_you, drops what is not one, never trusts a missing list", () => {
    const view = parseWorking({
      entries: [{ thread_id: "t1", project_id: "p1", since: "s", last: "l", seq: 4, status: "working", rows: [{ id: "dec:1", kind: "decision", rank: 6, title: "t" }, { id: "q:2", kind: "question", rank: 2, title: "q" }], why_not: "a turn is running" },
        { thread_id: "", seq: 1 }, { thread_id: "t2", seq: "x" }, 7],
      needs_you: [{ id: "pr:a", kind: "pr", rank: 5, title: "PR" }, { id: "bad" }],
      count: 2, pending: true,
    });
    expect(view?.entries.map((x) => x.thread_id)).toEqual(["t1"]);
    expect(view?.entries[0]).toMatchObject({ why_not: "a turn is running", seq: 4 });
    expect(view?.entries[0].attached).toEqual([{ kind: "decision", rank: 6 }, { kind: "question", rank: 2 }]);
    expect(view?.needs_you.map((r) => r.id)).toEqual(["pr:a"]);
    expect(view).toMatchObject({ count: 2, pending: true });
    expect(parseWorking({ entries: [] })).toBeNull();
    expect(parseWorking(null)).toBeNull();
  });
});

describe("labels", () => {
  it("kind words come from the kind, never from served text; amber is rank 1 and 2", () => {
    expect(kindLabel({ kind: "pr" })).toBe("PR");
    expect(kindLabel({ kind: "<img onerror=x>" })).toBe("Item");
    expect(rowPending({ rank: 2 })).toBe(true);
    expect(rowPending({ rank: 3 })).toBe(false);
  });
});

describe("attachedHint", () => {
  it("names the kind when all alike, else counts items", () => {
    expect(attachedHint([])).toBe("");
    expect(attachedHint([{ kind: "decision", rank: 6 }])).toBe("1 decision");
    expect(attachedHint([{ kind: "check", rank: 6 }, { kind: "check", rank: 6 }])).toBe("2 checks");
    expect(attachedHint([{ kind: "decision", rank: 6 }, { kind: "question", rank: 2 }])).toBe("2 items");
  });

  it("names the kind of what waits on the thread (WS-3): approval, question, decision, check", () => {
    expect(attachedHint([{ kind: "approval", rank: 1 }])).toBe("1 approval");
    expect(attachedHint([{ kind: "question", rank: 2 }])).toBe("1 question");
    expect(attachedHint([{ kind: "task_question", rank: 2 }, { kind: "question", rank: 2 }])).toBe("2 questions");
    expect(attachedHint([{ kind: "decision", rank: 6 }])).toBe("1 decision");
    expect(attachedHint([{ kind: "check", rank: 7 }])).toBe("1 check");
    expect(attachedHint([{ kind: "approval", rank: 1 }, { kind: "check", rank: 7 }, { kind: "check", rank: 7 }])).toBe("3 items");
  });
});

describe("the fold choice", () => {
  it("defaults to open and survives storage that throws", () => {
    expect(loadSectionOpen()).toBe(true);
    const real = globalThis.localStorage;
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get() { throw new Error("blocked"); } });
    try {
      expect(loadSectionOpen()).toBe(true);
      expect(() => saveSectionOpen(false)).not.toThrow();
    } finally {
      Object.defineProperty(globalThis, "localStorage", { configurable: true, value: real });
    }
  });
});
