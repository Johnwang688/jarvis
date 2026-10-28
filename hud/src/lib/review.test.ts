import { describe, expect, it } from "vitest";
import * as review from "./review";
import {
  afterChanged, allItems, approveBody, chipsOf, confirmationList, confirmedChanged, emptySelection, footerState,
  groupItems, keptSelection, leaveBody, parseReview, sectionTitle, summaryLine, toggled, type ReviewReport,
} from "./review";

const H = '<img src=x onerror="window.__pwned=1">';
const tag = (c: string) => c.repeat(64);

function row(id: string, kind: string, rank: number, over: Record<string, unknown> = {}) {
  return { id, kind, rank, title: `${kind} ${id}`, blocks: "b", project_id: null, thread_id: "t1", task_id: null,
    asked_at: "2026-03-11T04:30:00+00:00", expires_at: null, origin: "", detail: {}, ...over };
}

function deferred(id: string, over: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) {
  return row(`defer:${id}`, "deferred", 4, { ...over, detail: { deferral_id: id, tool: "Bash", grantable: true,
    why_not: "", expired: false, hud_only: false, count: 1, more: 0,
    card: { tool: "Bash", command: `make ${id}\nsecond line`, args: { command: `make ${id}` } }, ...detail } });
}

function item(r: Record<string, unknown>, over: Record<string, unknown> = {}) {
  return { row: r, section: "overnight", place: `chat:${r.thread_id ?? "t1"}`, approve: false, leave: false,
    why: "", tag: null, ...over };
}

function sample(): Record<string, unknown> {
  return {
    v: 1, now: "2026-03-11T07:30:00-05:00",
    quiet: { enabled: true, now: false, start: "2026-03-10T23:00-05:00", end: "2026-03-11T07:00-05:00" },
    summary: { approvals: 3, questions: 1, decisions: 1, blocked: 0, proposals: 0, live: 1 },
    live: [{ row: row("appr:L", "approval", 1) }],
    overnight: [
      item(deferred("a"), { approve: true, leave: true, tag: tag("a") }),
      item(deferred("b"), { approve: true, leave: true, tag: tag("b") }),
      item(deferred("c"), { approve: true, leave: true, tag: tag("c") }),
      item(deferred("w", { thread_id: "t2" }), { approve: false, leave: true, why: "withheld", place: "chat:t2" }),
      item(row("q:t2:1", "question", 2, { thread_id: "t2", asked_at: "2026-03-11T05:00:00+00:00" }), { place: "chat:t2" }),
      item(row("dec:d0123456789ab", "decision", 6, { detail: { decision_id: "d0123456789ab", question: "Which?",
        options: ["one", "two"], default: 1, why: "w", answer: null } })),
    ],
    before: [item(row("task:k1", "task", 4, { thread_id: null, task_id: "k1", asked_at: "2026-03-10T10:00:00+00:00" }),
      { section: "before", place: "task:k1" })],
    fyi: { count: 2 }, more: 0, open: 6, limits: { approve: 10, leave: 25, shown: 60 },
    projects: [{ id: "p1", name: "Calc " + H }],
    places: [{ key: "chat:t1", kind: "chat", id: "t1", project_id: "p1", title: "Fix " + H },
             { key: "chat:t2", kind: "chat", id: "t2", project_id: "p1", title: "Other" },
             { key: "task:k1", kind: "task", id: "k1", project_id: null, title: "migrate" }],
  };
}

function parsed(raw = sample()): ReviewReport {
  const out = parseReview(raw);
  expect(out).not.toBeNull();
  return out!;
}

describe("the morning review's report (lib/review.ts)", () => {
  it("reads the daemon's report, and refuses anything that is not one", () => {
    const r = parsed();
    expect(r.overnight.map((i) => i.row.id)).toEqual(["defer:a", "defer:b", "defer:c", "defer:w", "q:t2:1",
      "dec:d0123456789ab"]);
    expect(r.live.map((x) => x.id)).toEqual(["appr:L"]);
    expect((r.fyi)).toBe(2);
    expect(r.projects.get("p1")).toBe("Calc " + H);
    expect(parseReview({ ...sample(), v: 2 })).toBeNull();
    expect(parseReview([])).toBeNull();
    expect(parseReview(null)).toBeNull();
  });

  it("ticks only a deferred approval with its tag, never a Browser card, a day-old row or another kind", () => {
    const raw = sample();
    const list = raw.overnight as Record<string, unknown>[];
    list.push(item(deferred("h", {}, { hud_only: true }), { approve: true, leave: true, tag: tag("c") }));
    list.push(item(deferred("x", { rank: 8 }, { expired: true }), { approve: true, leave: true, tag: tag("d") }));
    list.push(item(deferred("n"), { approve: true, leave: true, tag: "nope" }));
    list.push(item(row("q:t1:9", "question", 2), { approve: true, tag: tag("e") }));
    const r = parsed(raw);
    const by = new Map(r.overnight.map((i) => [i.row.id, i]));
    expect(by.get("defer:a")!.approve).toBe(true);
    for (const id of ["defer:h", "defer:x", "defer:n", "q:t1:9"]) {
      expect(by.get(id)!.approve).toBe(false);
      expect(by.get(id)!.tag).toBeNull();
    }
    expect(by.get("q:t1:9")!.leave).toBe(false);
  });

  it("groups by project, then chat or task, the most urgent then the oldest; within a chat, by kind", () => {
    const r = parsed();
    const groups = groupItems(r, r.overnight);
    expect(groups.map((g) => g.name)).toEqual(["Calc " + H]);
    // chat t2 holds a rank-2 question: more urgent than chat t1's rank-4 deferrals.
    expect(groups[0].places.map((p) => p.place.key)).toEqual(["chat:t2", "chat:t1"]);
    // Deferred approvals first, then questions.
    expect(groups[0].places[0].items.map((i) => i.row.id)).toEqual(["defer:w", "q:t2:1"]);
    expect(groups[0].places[1].items.map((i) => i.row.id)).toEqual(["defer:a", "defer:b", "defer:c", "dec:d0123456789ab"]);
    const before = groupItems(r, r.before);
    expect(before.map((g) => g.name)).toEqual(["Other"]);
  });

  it("one jump chip per chat or task, to its first anchor; the summary in code's words", () => {
    const r = parsed();
    const chips = chipsOf(r);
    expect(chips.map((c) => c.key)).toEqual(["chat:t2", "chat:t1", "task:k1"]);
    expect(chips[0].anchor).toBe("review-overnight-chat_t2");
    expect(chips[2].anchor).toBe("review-before-task_k1");
    expect(chips[1].title).toBe("Fix " + H);
    expect(summaryLine(r)).toBe("1 live approval · 3 deferred approvals · 1 question · 1 decision");
  });
});

describe("the selection (MR-4)", () => {
  it("starts empty, ticks only by its own tap, and has no select-all", () => {
    expect(emptySelection().size).toBe(0);
    const r = parsed();
    const a = r.overnight[0];
    let sel = toggled(emptySelection(), a.row.id, a);
    expect([...sel]).toEqual(["defer:a"]);
    sel = toggled(sel, a.row.id, a);
    expect(sel.size).toBe(0);
    // A question cannot be ticked.
    expect(toggled(emptySelection(), "q:t2:1", r.overnight.find((i) => i.row.id === "q:t2:1")).size).toBe(0);
    // Nothing exported selects everything [mutation: a "select all"].
    expect(Object.keys(review).filter((k) => /select.?all|all.?select|tick.?all/i.test(k))).toEqual([]);
    expect(String(review.emptySelection)).not.toMatch(/add\(/);
  });

  it("Approve selected posts exactly the ticked items, with their tags, in display order", () => {
    const r = parsed();
    // Three approvable, two ticked: the list and the body are exactly those two.
    expect(r.overnight.filter((i) => i.approve).length).toBe(3);
    const sel = new Set(["defer:b", "defer:a"]);
    expect(approveBody(r, sel)).toEqual({ items: [{ id: "defer:a", tag: tag("a") }, { id: "defer:b", tag: tag("b") }] });
    expect(leaveBody(r, sel)).toEqual({ items: [{ id: "defer:a" }, { id: "defer:b" }] });
    expect(confirmationList(r, sel)).toEqual([
      { id: "defer:a", where: "Chat: Fix " + H, tool: "Bash", firstLine: "make a" },
      { id: "defer:b", where: "Chat: Fix " + H, tool: "Bash", firstLine: "make b" }]);
    expect(approveBody(r, emptySelection())).toBeNull();
  });

  it("is off while a ticked item cannot be approved (and says which), past 10, or on a limited phone", () => {
    const r = parsed();
    const ok = footerState(r, new Set(["defer:a"]));
    expect([ok.approve, ok.leave]).toEqual([true, true]);
    const withheld = footerState(r, new Set(["defer:a", "defer:w"]));
    expect(withheld.approve).toBe(false);
    expect(withheld.leave).toBe(true);
    expect(withheld.approveWhy.join(" ")).toContain("withheld text: answer it at the desk");
    expect(withheld.approveWhy.join(" ")).toContain("Bash (chat Other)");
    expect(approveBody(r, new Set(["defer:a", "defer:w"]))).toBeNull();
    const limited = footerState(r, new Set(["defer:a"]), { limited: "limited: Funnel is on" });
    expect(limited.approve).toBe(false);
    const raw = sample();
    (raw.overnight as unknown[]).splice(0, 4, ...Array.from({ length: 11 }, (_, i) =>
      item(deferred(`m${i}`), { approve: true, leave: true, tag: tag(String(i % 10)) })));
    const many = parsed(raw);
    const all = new Set(many.overnight.filter((i) => i.approve).map((i) => i.row.id));
    expect(all.size).toBe(11);
    expect(footerState(many, all).approve).toBe(false);
    expect(footerState(many, all).approveWhy).toContain("At most 10 at once.");
    expect(approveBody(many, all)).toBeNull();
    // The daemon cannot raise the owner's limit.
    expect(parsed({ ...sample(), limits: { approve: 50, leave: 99 } }).limits).toEqual({ approve: 10, leave: 25 });
  });

  it("a 409 unticks what changed and says why; a re-read unticks what left", () => {
    const after = afterChanged(new Set(["defer:a", "defer:b"]), [{ id: "defer:b", reason: "answered" }]);
    expect([...after.selection]).toEqual(["defer:a"]);
    expect(after.notes.get("defer:b")).toBe("Unticked: already answered or gone.");
    const r = parsed();
    expect([...keptSelection(r, new Set(["defer:a", "defer:gone"]))]).toEqual(["defer:a"]);
  });
});

describe("the review of #329's fixes", () => {
  it("MED 1: a re-read that drops or changes a confirmed item is caught", () => {
    const r = parsed();
    const confirmed = approveBody(r, new Set(["defer:a", "defer:b"]))!.items;
    expect(confirmedChanged(r, confirmed)).toEqual([]);
    const dropped = sample();
    (dropped.overnight as Record<string, unknown>[]).splice(1, 1);
    expect(confirmedChanged(parsed(dropped), confirmed)).toEqual(["defer:b"]);
    const retagged = sample();
    (retagged.overnight as Record<string, unknown>[])[1].tag = tag("f");
    expect(confirmedChanged(parsed(retagged), confirmed)).toEqual(["defer:b"]);
    const closed = sample();
    Object.assign((closed.overnight as Record<string, unknown>[])[0], { approve: false, why: "chat-closed" });
    expect(confirmedChanged(parsed(closed), confirmed)).toEqual(["defer:a"]);
  });

  it("NIT 7: rows asked after the night are 'Waiting now', never 'from before'", () => {
    const raw = sample();
    raw.since = [item(row("q:t3:1", "question", 2, { thread_id: "t3" }), { section: "since", place: "chat:t3" })];
    const r = parsed(raw);
    expect(r.since.map((i) => i.row.id)).toEqual(["q:t3:1"]);
    expect(sectionTitle(r, "since")).toBe("Waiting now");
    expect(sectionTitle(r, "before")).toBe("Still waiting from before");
    expect(allItems(r).map((i) => i.section)).toEqual([...r.overnight.map(() => "overnight"), "since", "before"]);
    expect(chipsOf(r).map((c) => c.key)).toContain("chat:t3");
    expect(parsed(sample()).since).toEqual([]);
  });
});

