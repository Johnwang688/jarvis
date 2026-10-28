// The owner inbox's pure rules (inbox A2): the row as the HUD accepts it, the
// groups, the count, Withdraw's window, links, untrusted text and the badge.

import { describe, expect, it, vi } from "vitest";
import {
  CLOSING_MS, GROUPS, anyPending, answerPath, briefOf, clockOf, groupRows, linkOf, missedNote, oneLine, parseInbox,
  parseRow, questionOf, setBadge, textBlock, waitingCount, withdrawHint, withdrawUntil, type InboxRow,
  decisionBody, decisionOf, decisionPath, CHECK_IMPORT_PATH, checkOf, checkReceipt, checkStepPath, parseDrafts,
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

describe("a proposal's Withdraw window: a hint, never a gate", () => {
  const prop = parseRow(row({ id: "prop:k", kind: "proposal", rank: 3,
    detail: { task_id: "k", brief: "b", withdraw_until: "2026-10-25T10:01:00+00:00" } }))!;
  const until = Date.parse("2026-10-25T10:01:00+00:00");

  it("says open until CLOSING_MS before withdraw_until, and closing from then on, past it included", () => {
    expect(withdrawUntil(prop)).toBe(until);
    expect(withdrawHint(prop, until - CLOSING_MS - 1)).toBe("open");
    expect(withdrawHint(prop, until - CLOSING_MS)).toBe("closing");
    expect(withdrawHint(prop, until)).toBe("closing");
    expect(withdrawHint(prop, until + 5000)).toBe("closing");
  });

  it("never claims more than a hint: there is no 'closed' at all, whatever the clocks say", () => {
    for (const t of [until - 3_600_000, until - 90_000, until, until + 90_000, until + 3_600_000]) {
      expect(["open", "closing"]).toContain(withdrawHint(prop, t));
    }
  });

  it("a time it cannot read is 'open', and another kind has no window", () => {
    const none = { ...prop, detail: { ...prop.detail, withdraw_until: "soon" } } as InboxRow;
    expect(withdrawHint(none, until + 1e9)).toBe("open");
    expect(withdrawUntil({ ...prop, kind: "task" })).toBeNull();
  });
});

describe("links", () => {
  it("go to the thread when the row has one, else the task, else nowhere", () => {
    expect(linkOf(parseRow(row({ thread_id: "t1", task_id: "k1" }))!)).toEqual({ kind: "thread", id: "t1" });
    expect(linkOf(parseRow(row({ thread_id: null, task_id: "k1" }))!)).toEqual({ kind: "task", id: "k1" });
    expect(linkOf(parseRow(row({ thread_id: null, task_id: null }))!)).toBeNull();
  });

  it("a 'denied while you were away' row links to its chat only, never its task", () => {
    const missed = (over: Record<string, unknown>) => parseRow(row({ id: "missed:R1", kind: "missed", rank: 8, ...over }))!;
    expect(linkOf(missed({ thread_id: "t1", task_id: "k1" }))).toEqual({ kind: "thread", id: "t1" });
    expect(linkOf(missed({ thread_id: null, task_id: "k1" }))).toBeNull();
  });
});

describe("a 'denied while you were away' row (inbox A3)", () => {
  const missed = (resolution: unknown, over: Record<string, unknown> = {}) => parseRow(row({
    id: "missed:R1", kind: "missed", rank: 8, title: "Denied while you were away: Bash",
    detail: { req_id: "R1", tool: "Bash", origin: "", thread_id: "t1", task_id: null, at: "x", resolution }, ...over,
  }))!;

  it("says why in code's words, chosen by its resolution, never the served text", () => {
    expect(missedNote(missed("timeout"))).toEqual({ why: "Nobody answered in time, so it was denied.",
      next: "To try it again, ask from its chat." });
    expect(missedNote(missed("shutdown"))!.why).toBe("Jarvis stopped before you answered, so it was denied.");
    for (const odd of ["<img src=x onerror=alert(1)>", 7, null, undefined]) {
      expect(missedNote(missed(odd))!.why).toBe("It was denied without an answer.");
    }
    expect(missedNote(missed("timeout", { thread_id: null }))!.next).toBe("Its chat is not known.");
  });

  it("is information only: rank 8, not counted, not pending, nothing for any other kind", () => {
    const m = missed("timeout");
    expect(groupRows([m]).map((g) => g.group.key)).toEqual(["fyi"]);
    expect(waitingCount([m])).toBe(0);
    expect(anyPending([m])).toBe(false);
    expect(missedNote(parseRow(row())!)).toBeNull();
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

// Inbox B1 (IN-5): a decision row's words and what its Answer posts.
describe("decisions", () => {
  const dec = (detail: Record<string, unknown> = {}, over: Record<string, unknown> = {}) => parseRow(row({
    id: "dec:d00000000000a", kind: "decision", rank: 6, title: "chat t1 asks for your decision",
    blocks: "nothing waits; chat t1 hears your answer as a message",
    detail: { decision_id: "d00000000000a", question: "Q?‮", options: ["a", "b"], default: 0, why: "W",
              answer: null, delivery: null, ...detail },
    ...over,
  }))!;

  it("reads an open decision as text, the suggestion an index in range", () => {
    const d = decisionOf(dec())!;
    expect(d).toEqual({ id: "d00000000000a", question: "Q?", options: ["a", "b"], suggested: 0, why: "W", open: true, answer: "" });
    expect(decisionOf(dec({ default: 5 }))!.suggested).toBeNull();
    expect(decisionOf(dec({ default: true }))!.suggested).toBeNull();
    expect(decisionOf(dec({ default: 1.5 }))!.suggested).toBeNull();
  });

  it("refuses a detail that is not a decision: never answerable", () => {
    for (const bad of [{ decision_id: "x" }, { decision_id: "../d00000000000a" }, { options: ["one"] },
                       { options: ["a", "b", "c", "d", "e", "f", "g"] }, { options: ["a", ""] }, { options: "a,b" }]) {
      expect(decisionOf(dec(bad))).toBeNull();
    }
    expect(decisionOf(dec({}, { kind: "question" }))).toBeNull();
  });

  it("an option that draws the HUD's own marker is never answerable (review of PR #309)", () => {
    for (const forged of ["yes (suggested)", "yes (SUGGESTED)", "yes （suggested）"]) {
      expect(decisionOf(dec({ options: [forged, "no"] }))).toBeNull();
    }
    expect(decisionOf(dec({ options: ["suggested by me", "no"] }))).not.toBeNull();
  });

  it("an answered one says the owner's words: the option's text, or what they typed", () => {
    expect(decisionOf(dec({ answer: { choice: 1, text: null, at: "x" } }))).toMatchObject({ open: false, answer: "b" });
    expect(decisionOf(dec({ answer: { choice: null, text: "mine", at: "x" } }))).toMatchObject({ open: false, answer: "mine" });
  });

  it("Answer posts the typed words over the pick, else the pick, else nothing", () => {
    const d = decisionOf(dec())!;
    expect(decisionBody(1, "  my words ", d)).toEqual({ text: "my words" });
    expect(decisionBody(1, "   ", d)).toEqual({ choice: 1 });
    expect(decisionBody(0, "", d)).toEqual({ choice: 0 });
    expect(decisionBody(null, "", d)).toBeNull();
    expect(decisionBody(7, "", d)).toBeNull();
  });

  it("its path is its own id through encodeURIComponent", () => {
    expect(decisionPath("d00000000000a")).toBe("/inbox/decisions/d00000000000a");
    expect(decisionPath("a/b")).toBe("/inbox/decisions/a%2Fb");
  });

  it("rank 6 sits under When you have time and is not counted", () => {
    const rows = [dec()];
    expect(groupRows(rows).map((g) => g.group.key)).toEqual(["time"]);
    expect(waitingCount(rows)).toBe(0);
  });
});

// Inbox B1 (IN-5): a decision rows

describe("live checks (inbox B2)", () => {
  const row = (detail: Record<string, unknown>, over: Record<string, unknown> = {}) => parseRow({
    id: "chk:c0123456789ab", kind: "check", rank: 7, title: "t", blocks: "b",
    detail: { check_id: "c0123456789ab", title: "T", prs: [301], steps: [{ do: "a", expect: "b" }], results: [],
              deploy: { state: "deployed", text: "", markable: true }, ...detail },
    ...over,
  })!;

  it("reads a check as text, markable only when the daemon says so and it is not done", () => {
    const c = checkOf(row({}))!;
    expect(c).toMatchObject({ id: "c0123456789ab", title: "T", prs: [301], done: false, markable: true });
    expect(checkOf(row({ deploy: { state: "not deployed", text: "x", markable: false } }))!.markable).toBe(false);
    expect(checkOf(row({ deploy: { markable: "yes" } }))!.markable).toBe(false);
    const done = checkOf(row({ results: [{ step: 0, verdict: "pass", note: "n‮", at: "x" }] }))!;
    expect(done.done).toBe(true);
    expect(done.markable).toBe(false);
    expect(done.steps[0]).toMatchObject({ verdict: "pass", note: "n" });
  });

  it("is null for anything that is not a check it can read", () => {
    for (const detail of [{ check_id: "../x" }, { check_id: "d0123456789ab" }, { steps: [] }, { steps: "a" },
                          { steps: ["a"] }]) {
      expect(checkOf(row(detail))).toBeNull();
    }
    expect(checkOf(row({}, { kind: "decision" }))).toBeNull();
    expect(checkOf(row({ results: [{ step: 0, verdict: "maybe" }] }))!.steps[0].verdict).toBeNull();
  });

  it("encodes its paths", () => {
    expect(checkStepPath("c0123456789ab", 2)).toBe("/inbox/checks/c0123456789ab/steps/2");
    expect(checkStepPath("a/b", 1.7)).toBe("/inbox/checks/a%2Fb/steps/1");
    expect(CHECK_IMPORT_PATH).toBe("/inbox/checks/import");
  });

  it("the receipt escapes what a step says, so it stays one Markdown line", () => {
    const c = checkOf(row({ title: "# T", steps: [{ do: "1. `x`\n> q", expect: "*e*" }],
                            results: [{ step: 0, verdict: "fail", note: "[a](b)", at: "2026-10-25T09:00:00Z" }] }))!;
    const md = checkReceipt(c, "Claude · chat ‹x› (t1)");
    const lines = md.split("\n");
    expect(lines[0]).toBe("## Live check c0123456789ab: \\# T");
    expect(lines).toContain("1. **FAIL** 1. \\`x\\` \\> q");
    expect(lines).toContain("   - Expect: \\*e\\*");
    expect(lines).toContain("   - Note: \\[a\\](b)");
    expect(lines.filter((l) => l.startsWith("#")).length).toBe(1);
  });

  it("reads the import's drafts as text", () => {
    expect(parseDrafts({})).toBeNull();
    const got = parseDrafts({ path: "/p/docs/plans/x.md", digest: "d", drafts: [
      { index: 0, title: "A‮", prs: [5, "6"], steps: [{ do: "a", expect: "" }], savable: true, reason: null },
      { index: "1" }] })!;
    expect(got.drafts).toEqual([{ index: 0, title: "A", prs: [5], steps: [{ do: "a", expect: "" }], savable: true, reason: null }]);
  });
});
