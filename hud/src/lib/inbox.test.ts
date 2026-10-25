// The owner inbox's pure rules (inbox A2): the row as the HUD accepts it, the
// groups, the count, Withdraw's window, links, untrusted text and the badge.

import { describe, expect, it, vi } from "vitest";
import {
  GROUPS, anyPending, answerPath, briefOf, clockOf, groupRows, linkOf, oneLine, parseInbox, parseRow,
  questionOf, setBadge, textBlock, waitingCount, withdrawOpen, withdrawUntil, type InboxRow,
} from "./inbox";

const row = (over: Record<string, unknown> = {}) => ({
  id: "q:t1:r1", kind: "question", rank: 2, title: "chat t1 is stopped until you answer",
  blocks: "chat t1 waits on this", project_id: "p1", thread_id: "t1", task_id: null,
  asked_at: "2026-10-25T10:00:00+00:00", expires_at: null, origin: "", detail: { text: "Which?" }, ...over,
});

describe("parseRow / parseInbox", () => {
  it("keeps a served row's fields", () => {
    const r = parseRow(row())!;
    expect(r).toMatchObject({ id: "q:t1:r1", kind: "question", rank: 2, thread_id: "t1", task_id: null });
    expect(r.detail).toEqual({ text: "Which?" });
  });

  it("drops what is not a row, never guessing", () => {
    for (const bad of [null, 3, "x", [], { ...row(), id: "" }, { ...row(), id: "noprefix" }, { ...row(), rank: 0 },
      { ...row(), rank: 9 }, { ...row(), rank: 1.5 }, { ...row(), rank: "1" }, { ...row(), kind: "" }]) {
      expect(parseRow(bad)).toBeNull();
    }
  });

  it("is null for a body that is not a list", () => {
    expect(parseInbox({ rows: [] })).toBeNull();
    expect(parseInbox(null)).toBeNull();
  });

  it("orders by rank, keeping the daemon's order (oldest first) within a rank, and drops duplicates", () => {
    const rows = parseInbox([
      row({ id: "task:b", kind: "task", rank: 8 }), row({ id: "prop:a", kind: "proposal", rank: 3 }),
      row({ id: "appr:x", kind: "approval", rank: 1 }), row({ id: "prop:c", kind: "proposal", rank: 3 }),
      row({ id: "appr:x", kind: "approval", rank: 1 }),
    ])!;
    expect(rows.map((r) => r.id)).toEqual(["appr:x", "prop:a", "prop:c", "task:b"]);
  });

  it("a detail that is not an object is empty, and ids that are not strings are null", () => {
    const r = parseRow(row({ detail: [1, 2], thread_id: 7, task_id: "" }))!;
    expect(r.detail).toEqual({});
    expect(r.thread_id).toBeNull();
    expect(r.task_id).toBeNull();
  });
});

describe("groups and the count", () => {
  const rows = parseInbox([1, 2, 3, 4, 5, 6, 7, 8].map((rank) => row({ id: `x:${rank}`, rank })))!;

  it("the four headers, by rank", () => {
    expect(GROUPS.map((g) => g.label)).toEqual(["Waiting now", "Waiting on you", "When you have time", "For your information"]);
    expect(groupRows(rows).map((g) => [g.group.key, g.rows.map((r) => r.rank)])).toEqual([
      ["now", [1, 2]], ["you", [3, 4, 5]], ["time", [6, 7]], ["fyi", [8]],
    ]);
  });

  it("hides an empty group", () => {
    const some = rows.filter((r) => r.rank === 1 || r.rank === 8);
    expect(groupRows(some).map((g) => g.group.key)).toEqual(["now", "fyi"]);
    expect(groupRows([])).toEqual([]);
  });

  it("counts ranks 1 to 5 only, and pending is ranks 1 and 2", () => {
    expect(waitingCount(rows)).toBe(5);
    expect(waitingCount(rows.filter((r) => r.rank > 5))).toBe(0);
    expect(anyPending(rows)).toBe(true);
    expect(anyPending(rows.filter((r) => r.rank > 2))).toBe(false);
  });
});

describe("a proposal's Withdraw window", () => {
  const prop = parseRow(row({ id: "prop:k", kind: "proposal", rank: 3,
    detail: { task_id: "k", brief: "b", withdraw_until: "2026-10-25T10:01:00+00:00" } }))!;
  const until = Date.parse("2026-10-25T10:01:00+00:00");

  it("is open before withdraw_until and closed from it on", () => {
    expect(withdrawUntil(prop)).toBe(until);
    expect(withdrawOpen(prop, until - 1)).toBe(true);
    expect(withdrawOpen(prop, until)).toBe(false);
    expect(withdrawOpen(prop, until + 5000)).toBe(false);
  });

  it("is never open without a time it can read, or on another kind", () => {
    const none = { ...prop, detail: { ...prop.detail, withdraw_until: "soon" } } as InboxRow;
    expect(withdrawOpen(none, 0)).toBe(false);
    expect(withdrawUntil({ ...prop, kind: "task" })).toBeNull();
  });
});

describe("links", () => {
  it("go to the thread when the row has one, else the task, else nowhere", () => {
    expect(linkOf(parseRow(row({ thread_id: "t1", task_id: "k1" }))!)).toEqual({ kind: "thread", id: "t1" });
    expect(linkOf(parseRow(row({ thread_id: null, task_id: "k1" }))!)).toEqual({ kind: "task", id: "k1" });
    expect(linkOf(parseRow(row({ thread_id: null, task_id: null }))!)).toBeNull();
  });
});

describe("untrusted text", () => {
  it("oneLine takes out format characters and folds lines", () => {
    expect(oneLine("a‮b​ c\n\nd\u0007e")).toBe("ab c d e");
    expect(oneLine(7)).toBe("");
    expect(oneLine("x".repeat(10), 4)).toBe("xxxx…");
  });

  it("textBlock keeps lines and takes out format and control characters", () => {
    expect(textBlock("line one\r\nline‮ two\u0000\n\tthree")).toBe("line one\nline two \n three");
  });

  it("a question's text and options, and a brief, are text", () => {
    const q = questionOf(parseRow(row({ detail: { text: "<img src=x onerror=alert(1)>", options: ["a", 3, "b‮"] } }))!);
    expect(q.text).toBe("<img src=x onerror=alert(1)>");
    expect(q.options).toEqual(["a", "b"]);
    expect(briefOf(parseRow(row({ detail: { brief: "do​ it" } }))!)).toBe("do it");
  });

  it("clockOf reads a time or says nothing", () => {
    expect(clockOf(null)).toBe("");
    expect(clockOf("not a time")).toBe("");
    expect(clockOf(new Date(2026, 9, 25, 9, 5, 7).getTime())).toBe("09:05:07");
  });
});

describe("the answer path", () => {
  it("puts the id through encodeURIComponent, once", () => {
    expect(answerPath("appr:Xk2/../a b")).toBe("/inbox/appr%3AXk2%2F..%2Fa%20b/answer");
  });
});

describe("the app badge", () => {
  it("sets the count, and clears at 0", () => {
    const nav = { setAppBadge: vi.fn(() => Promise.resolve()), clearAppBadge: vi.fn(() => Promise.resolve()) };
    setBadge(3, nav);
    expect(nav.setAppBadge).toHaveBeenCalledWith(3);
    setBadge(0, nav);
    expect(nav.clearAppBadge).toHaveBeenCalledTimes(1);
  });

  it("falls back to setAppBadge(0) without clearAppBadge", () => {
    const nav = { setAppBadge: vi.fn(() => Promise.resolve()) };
    setBadge(0, nav);
    expect(nav.setAppBadge).toHaveBeenCalledWith(0);
  });

  it("does nothing without the Badging API, and swallows every error", async () => {
    expect(() => setBadge(2, {})).not.toThrow();
    expect(() => setBadge(2, undefined)).not.toThrow();
    const throws = { setAppBadge: () => { throw new Error("NotAllowedError"); } };
    expect(() => setBadge(2, throws as any)).not.toThrow();
    const rejects = { setAppBadge: () => Promise.reject(new Error("NotAllowedError")),
      clearAppBadge: () => Promise.reject(new Error("no")) };
    expect(() => { setBadge(2, rejects); setBadge(0, rejects); }).not.toThrow();
    await new Promise((r) => setTimeout(r, 0));   // an unhandled rejection would fail the run
  });

  it("never touches the title", () => {
    document.title = "J.A.R.V.I.S.";
    setBadge(4, { setAppBadge: () => Promise.resolve() });
    expect(document.title).toBe("J.A.R.V.I.S.");
  });
});
