import { describe, expect, it, vi } from "vitest";
import { parseInbox, type InboxRow } from "../lib/inbox";
import { REPO, fixRow, prRow, tqRow } from "../lib/prRow.fixtures";
import type { TaskView, WorkingItem } from "./model";
import { SlotCache, slotSignature, threadsView, type ListActions, type ListModel } from "./threadsView";
import type { InboxAnswers } from "./views";
import { bands, buildSlots } from "./threads";
import { agoWords } from "./reltime";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { mkEntry, mkRow, mkTask, randomCase } from "./threads.fixtures";

const NOW = Date.parse("2026-11-15T10:30:00Z");
const MARKUP = "img, script, b, svg, iframe, object, embed, a[href^='javascript'], [onerror], [onload]";
const HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>';

function answers(over: Partial<InboxAnswers> = {}): InboxAnswers {
  return { openCard: vi.fn(), send: vi.fn(), retry: vi.fn(), busy: new Set(), notes: new Map(), drafts: new Map(),
    picks: new Map(), ...over };
}

function actions(a: InboxAnswers = answers(), over: Partial<ListActions> = {}): ListActions {
  return { answers: a, openRow: vi.fn(), openThread: vi.fn(), openTask: vi.fn(), setFilter: vi.fn(), setRecentAll: vi.fn(),
    retry: vi.fn(), ...over };
}

function model(over: Partial<ListModel> = {}): ListModel {
  return { rows: [], entries: [], working: "ok", workingCut: { more: false, total: 0 }, tasks: [], tasksMore: false,
    inboxMore: false, filter: null, recentAll: false, error: null, ...over };
}

function draw(m: ListModel, a: ListActions = actions(), cache = new SlotCache(), panel: HTMLElement | null = null) {
  const view = threadsView(m, a, cache, NOW, panel);
  document.body.replaceChildren(view);
  return view;
}

const rowEls = (v: ParentNode) => Array.from(v.querySelectorAll<HTMLElement>('[data-testid="m-inbox-row"]'));
const card = (v: ParentNode, key: string) => v.querySelector<HTMLElement>(`[data-testid="m-thread"][data-key="${key}"]`)!;
/** Whether a node is inside something the page folds (a hidden container or a closed details). */
const folded = (n: Element) => !!n.closest("[hidden]") || !!Array.from(document.querySelectorAll("details:not([open])")).find((d) => d.contains(n));

describe("the bands and the strip", () => {
  it("draws Needs you, Running and Recent in order, an approval inside its thread's card", () => {
    const rows = parseInbox([
      mkRow("appr:1", 1, { kind: "approval", thread_id: "aaaaaaaa", title: "Claude asks to use Bash", detail: { tool: "Bash", command: "ls" } }),
      mkRow("q:2", 2, { thread_id: "aaaaaaaa", detail: { text: "Which?", options: ["a", "b"] } }),
    ])!;
    const v = draw(model({
      rows, entries: [mkEntry("aaaaaaaa", "needs_input"), mkEntry("bbbbbbbb", "working"), mkEntry("cccccccc", "idle")],
      tasks: [mkTask("11111111", "running")],
    }));
    const order = Array.from(v.querySelectorAll("section.m-band")).map((s) => (s as HTMLElement).dataset.band);
    expect(order).toEqual(["needs", "running", "recent"]);
    const needs = card(v, "thread:aaaaaaaa");
    expect(needs.dataset.band).toBe("needs");
    expect(rowEls(needs).map((r) => r.dataset.id)).toEqual(["appr:1", "q:2"]);
    // #371: the question's one-tap option chips sit inside the thread card.
    expect(needs.querySelectorAll(".m-chips .m-chip").length).toBe(2);
    expect(card(v, "thread:bbbbbbbb").dataset.band).toBe("running");
    expect(card(v, "task:11111111").dataset.band).toBe("running");
    expect(card(v, "thread:cccccccc").dataset.band).toBe("recent");
    // The head is one button that opens the thread.
    const a = actions();
    const head = card(draw(model({ rows, entries: [mkEntry("aaaaaaaa", "needs_input")] }), a), "thread:aaaaaaaa")
      .querySelector<HTMLButtonElement>('[data-testid="m-thread-open"]')!;
    head.click();
    expect(a.openThread).toHaveBeenCalledWith("aaaaaaaa");
  });

  it("the strip shows Needs you with the badge's number, Running with its count, and Tasks; a chip toggles its filter", () => {
    const rows = [mkRow("a", 1), mkRow("b", 5), mkRow("c", 6)];
    const a = actions();
    const v = draw(model({ rows, entries: [mkEntry("aaaaaaaa", "working"), mkEntry("bbbbbbbb", "working")] }), a);
    expect(v.querySelector('[data-testid="m-chip-needs"]')!.textContent).toBe("Needs you · 2");
    expect((v.querySelector('[data-testid="m-chip-needs"]') as HTMLElement).dataset.tone).toBe("pending");
    expect(v.querySelector('[data-testid="m-chip-running"]')!.textContent).toBe("Running · 2");
    expect(v.querySelector('[data-testid="m-chip-tasks"]')!.textContent).toBe("Tasks");
    (v.querySelector('[data-testid="m-chip-running"]') as HTMLElement).click();
    expect(a.setFilter).toHaveBeenLastCalledWith("running");
    const on = draw(model({ rows, filter: "running", entries: [mkEntry("aaaaaaaa", "working")] }), a);
    expect(on.querySelector('[data-testid="m-chip-running"]')!.getAttribute("aria-pressed")).toBe("true");
    (on.querySelector('[data-testid="m-chip-running"]') as HTMLElement).click();
    expect(a.setFilter).toHaveBeenLastCalledWith(null);
  });

  it("the Needs you chip is not amber without a rank 1 or 2 row; one empty card replaces the empty headings", () => {
    const v = draw(model({ rows: [mkRow("a", 5)] }));
    expect((v.querySelector('[data-testid="m-chip-needs"]') as HTMLElement).dataset.tone).toBeUndefined();
    const none = draw(model({ rows: [] }));
    expect(none.querySelector('[data-testid="m-band-needs-head"]')).toBeNull();
    expect(none.querySelectorAll('[data-testid="m-empty"]')).toHaveLength(1);
    expect(none.querySelector('[data-testid="m-empty"]')!.textContent)
      .toBe("Nothing needs youNo approvals, questions or pull requests are waiting. Nothing is running.");
    expect(none.textContent).not.toContain("Nothing running");
  });

  it("a filter shows only its band, and the Tasks filter shows the tasks panel", () => {
    const rows = [mkRow("a", 3, { thread_id: "aaaaaaaa" })];
    const entries = [mkEntry("aaaaaaaa"), mkEntry("bbbbbbbb", "working")];
    const needs = draw(model({ rows, entries, filter: "needs" }));
    expect(needs.querySelector('[data-testid="m-band-running"]')).toBeNull();
    const panel = document.createElement("section");
    panel.dataset.testid = "m-tasks";
    const tasks = draw(model({ rows, entries, filter: "tasks" }), actions(), new SlotCache(), panel);
    expect(tasks.querySelector('[data-testid="m-tasks"]')).toBe(panel);
    expect(tasks.querySelector('[data-testid="m-band-needs"]')).toBeNull();
  });

  it("loading shows skeleton cards and a hidden Loading, aria-busy", () => {
    const v = draw(model({ rows: null, entries: null, working: "loading" }));
    expect(v.getAttribute("aria-busy")).toBe("true");
    expect(v.querySelectorAll(".m-skeleton").length).toBe(3);
    expect(v.querySelector(".m-sr")!.textContent).toBe("Loading…");
  });

  it("an inbox read that failed keeps the list and names the failure with Retry", () => {
    const a = actions();
    const v = draw(model({ rows: [mkRow("a", 3)], error: "The Inbox could not be read: boom" }), a);
    expect(v.querySelector('[data-testid="m-list-error"]')!.textContent).toContain("could not be read: boom");
    (v.querySelector('[data-testid="m-retry"]') as HTMLElement).click();
    expect(a.retry).toHaveBeenCalled();
    expect(rowEls(v).length).toBe(1);
  });
});

describe("RD-3 glass strip and dense slot heads", () => {
  it("uses a translucent segmented track with no opaque sticky background", () => {
    const css = readFileSync(join(__dirname, "threads.css"), "utf8");
    const track = css.match(/\.m-strip\s*\{([^}]+)\}/)![1];
    expect(track).toContain("background: var(--m-seg, rgba(255, 255, 255, 0.06))");
    expect(track).not.toMatch(/sticky|--m-glass-sticky|#[\da-f]{3,8}/i);
  });

  it("keeps full accessible chip names and only the active filter pressed", () => {
    const rows = Array.from({ length: 125 }, (_, i) => mkRow(`q:${i}`, 3));
    for (const filter of [null, "needs", "running", "tasks"] as const) {
      const a = actions();
      const v = draw(model({ rows, filter }), a);
      const chips = Array.from(v.querySelectorAll<HTMLButtonElement>(".chip"));
      expect(chips.map((b) => b.getAttribute("aria-label"))).toEqual(["Needs you · 125", "Running · 0", "Tasks"]);
      expect(chips.map((b) => b.getAttribute("aria-pressed"))).toEqual(["needs", "running", "tasks"].map((id) => String(id === filter)));
      chips[0].click();
      expect(a.setFilter).toHaveBeenLastCalledWith(filter === "needs" ? null : "needs");
    }
  });

  it("draws the dot, project, kind, provider and relative age, retaining an error word", () => {
    const rows = [mkRow("appr:1", 1, { kind: "approval", thread_id: "aaaaaaaa", detail: { provider: "codex" } })];
    const v = draw(model({ rows, entries: [mkEntry("aaaaaaaa", "failed")] }));
    const c = card(v, "thread:aaaaaaaa");
    expect(c.querySelector('[data-testid="m-thread-dot"]')!.getAttribute("aria-hidden")).toBe("true");
    expect(c.querySelector(".m-thread-project")!.textContent).toBe("Calc");
    expect(c.querySelector(".m-thread-kind")!.textContent).toBe("Approval");
    expect(c.querySelector(".m-thread-failed")!.textContent).toBe("Failed");
    expect(c.querySelector(".m-thread-provider")!.textContent).toBe("Codex");
    expect(c.querySelector(".m-thread-age")!.textContent).toBe("30 min ago");
    expect(c.querySelector(".m-thread-age")!.getAttribute("title")).toBeTruthy();
    expect(c.querySelector(".m-thread-age")!.getAttribute("data-rt-at")).toBe(String(Date.parse("2026-11-15T10:00:00Z")));
  });

  it("uses row ages for orphan/standalone heads and omits missing fields instead of guessing a provider", () => {
    const rows = [mkRow("q:1", 2, { thread_id: "aaaaaaaa", asked_at: "2026-11-15T10:10:00Z" }),
      mkRow("q:2", 3, { thread_id: "aaaaaaaa", asked_at: "2026-11-15T10:20:00Z" }),
      mkRow("pr:1", 5, { kind: "pr", asked_at: "2026-11-15T10:00:00Z" }), mkRow("q:3", 3)];
    const v = draw(model({ rows }));
    expect(card(v, "orphan:aaaaaaaa").querySelector(".m-thread-age")!.textContent).toBe("10 min ago");
    expect(card(v, "row:pr:1").querySelector(".m-thread-kind")!.textContent).toBe("Pull request");
    expect(card(v, "row:pr:1").querySelector(".m-thread-age")!.textContent).toBe("30 min ago");
    expect(card(v, "row:q:3").querySelector(".m-thread-age")).toBeNull();
    expect(v.querySelector(".m-thread-provider, .m-thread-project")).toBeNull();
  });

  it("keeps a running task's kind, progress and open action", () => {
    const a = actions();
    const c = card(draw(model({ tasks: [mkTask("11111111", "running", { step: 3, steps: 7 })] }), a), "task:11111111");
    expect(c.querySelector(".m-thread-kind")!.textContent).toBe("Working");
    expect(c.querySelector(".m-thread-progress")!.textContent).toBe("step 3 of 7");
    c.querySelector<HTMLButtonElement>(".m-thread-open")!.click();
    expect(a.openTask).toHaveBeenCalledWith("11111111");
  });

  it("groups Recent heads in one list and keeps them tappable", () => {
    const a = actions();
    const v = draw(model({ entries: [mkEntry("aaaaaaaa", "idle"), mkEntry("bbbbbbbb", "unread")] }), a);
    const list = v.querySelector('[data-testid="m-recent-list"]')!;
    expect(list.querySelectorAll(":scope > .m-thread-recent")).toHaveLength(2);
    list.querySelector<HTMLButtonElement>(".m-thread-open")!.click();
    expect(a.openThread).toHaveBeenCalledWith("bbbbbbbb");
    expect(list.querySelector(".m-thread-kind")!.textContent).toBe("Done, unread");
  });

  it("puts one empty card above Recent; never claims nothing is running when that read is unavailable", () => {
    const v = draw(model({ entries: [mkEntry("aaaaaaaa", "idle")] }));
    expect(v.querySelectorAll(".m-empty-card")).toHaveLength(1);
    expect(Array.from(v.children).map((n) => (n as HTMLElement).dataset.testid)).toEqual(["m-strip", "m-band-needs", "m-band-recent"]);
    expect(v.querySelector('[data-testid="m-band-needs"] > .m-empty-card')).not.toBeNull();
    for (const over of [{ working: "unavailable" as const }, { tasks: null }, { tasksMore: true }, { tasks: [mkTask("11111111", "running")] }]) {
      const partial = draw(model(over));
      expect(partial.querySelectorAll(".m-empty-card")).toHaveLength(1);
      expect(partial.querySelector(".m-empty-card")!.textContent).not.toContain("Nothing is running");
    }
    expect(draw(model({ rows: null, error: "boom" })).querySelector(".m-empty-card")).toBeNull();
    expect(draw(model({ rows: [], error: "boom" })).querySelector(".m-empty-card")).toBeNull();
  });
});

describe("what the phone does not show, and the working route's own state", () => {
  it("In progress unavailable sits in the Running band, and every row still lands", () => {
    const rows = [mkRow("a", 1, { thread_id: "aaaaaaaa" }), mkRow("b", 3, { task_id: "11111111" }), mkRow("c", 7)];
    const v = draw(model({ rows, entries: null, working: "unavailable", tasks: [mkTask("22222222", "running")] }));
    expect(v.querySelector('[data-testid="m-band-running"] [data-testid="m-working-unavailable"]')!.textContent)
      .toBe("In progress unavailable");
    expect(rowEls(v).map((r) => r.dataset.id).sort()).toEqual(["a", "b", "c"]);
    // A running task still shows, from /m/api/tasks.
    expect(card(v, "task:22222222").dataset.band).toBe("running");
  });

  it("working more: Showing 100 of N, the rest at the desk; inbox more; tasks more under Running and on the Tasks list", () => {
    const entries = Array.from({ length: 100 }, (_, i) => mkEntry(i.toString(16).padStart(8, "0")));
    const v = draw(model({ entries, workingCut: { more: true, total: 130 }, inboxMore: true, tasksMore: true }));
    expect(v.querySelector('[data-testid="m-working-more"]')!.textContent).toBe("Showing 100 of 130: the rest are at the desk");
    expect(v.querySelector('[data-testid="m-inbox-more"]')!.textContent).toBe("More waiting than the phone shows: open the Inbox at the desk");
    expect(v.querySelector('[data-testid="m-band-running"] [data-testid="m-tasks-more"]')!.textContent)
      .toContain("Showing the newest 200 tasks: older ones, running ones included, are at the desk");
    expect(v.querySelector('[data-testid="m-working-unavailable"]')).toBeNull();
  });

  it("Recent shows 5 and then Show all (N); the rest stay in the page, folded", () => {
    const entries = Array.from({ length: 8 }, (_, i) => mkEntry(`0000000${i}`, "idle", { last: `2026-11-15T0${i}:00:00Z` }));
    const rows = parseInbox([mkRow("x", 7, { thread_id: "00000000" })])!;
    const a = actions();
    const v = draw(model({ entries, rows }), a);
    const all = v.querySelector<HTMLElement>('[data-testid="m-recent-all"]')!;
    expect(all.textContent).toBe("Show all (8)");
    expect(v.querySelectorAll('[data-testid="m-recent-rest"] [data-testid="m-thread"]').length).toBe(3);
    expect(v.querySelector<HTMLElement>('[data-testid="m-recent-rest"]')!.hidden).toBe(true);
    all.click();
    expect(a.setRecentAll).toHaveBeenCalledWith(true);
    const open = draw(model({ entries, rows, recentAll: true }), a);
    expect(open.querySelector<HTMLElement>('[data-testid="m-recent-rest"]')!.hidden).toBe(false);
    expect(open.querySelector('[data-testid="m-recent-all"]')!.textContent).toBe("Show fewer");
  });

  it("rank 8 row slots fold to 'N for your information', their rows still in the page", () => {
    const rows = [mkRow("a", 8, { kind: "missed" }), mkRow("b", 8, { kind: "missed" }), mkRow("c", 6, { kind: "missed" })];
    const v = draw(model({ rows }));
    expect(v.querySelector('[data-testid="m-info"] summary')!.textContent).toBe("2 for your information");
    expect(rowEls(v.querySelector('[data-testid="m-info"]')!).map((r) => r.dataset.id)).toEqual(["a", "b"]);
    expect(card(v, "row:c").dataset.band).toBe("recent");
  });
});

describe("a first inbox read that failed", () => {
  it("shows the running thread and task that loaded, a neutral line, and never Nothing needs you or All clear", () => {
    const v = draw(model({ rows: null, error: "The Inbox could not be read: boom", entries: [mkEntry("aaaaaaaa", "working")],
      tasks: [mkTask("11111111", "running")] }));
    expect(v.querySelector('[data-testid="m-list-error"]')).not.toBeNull();
    expect(v.querySelector('[data-testid="m-inbox-unreadable"]')!.textContent).toBe("The Inbox could not be read");
    expect(v.textContent).not.toContain("Nothing needs you");
    expect(v.querySelector('[data-testid="m-empty"]')).toBeNull();
    expect(v.querySelector('[data-testid="m-chip-needs"]')!.textContent).toBe("Needs you · ?");
    expect(v.querySelector('[data-testid="m-chip-running"]')!.textContent).toBe("Running · 2");
    expect(card(v, "thread:aaaaaaaa").dataset.band).toBe("running");
    expect(card(v, "task:11111111").dataset.band).toBe("running");
  });

  it("a hidden rank 5 row sits in Recent while the chip counts 0", () => {
    const rows = [mkRow("q:5", 5, { hidden: { snoozedUntil: "2099-01-01T00:00:00Z", dismissedAt: null } })];
    const v = draw(model({ rows }));
    expect(v.querySelector('[data-testid="m-chip-needs"]')!.textContent).toBe("Needs you · 0");
    expect(card(v, "row:q:5").dataset.band).toBe("recent");
  });
});

describe("IN-8 through rendering: each received row is drawn once; ranks 1 to 4 are never folded", () => {
  it("holds for generated cases with overlapping thread and task ids, in every working state", () => {
    for (let seed = 1; seed <= 150; seed++) {
      const c = randomCase(seed);
      for (const working of ["ok", "unavailable"] as const) {
        const entries = working === "ok" ? c.entries : null;
        const v = draw(model({ rows: c.rows, entries, tasks: c.tasks, working, recentAll: false }));
        const drawn = rowEls(v).map((r) => r.dataset.id!);
        expect(drawn.sort(), `seed ${seed} ${working}`).toEqual(c.rows.map((r) => r.id).sort());
        for (const el of rowEls(v)) {
          const rank = c.rows.find((r) => r.id === el.dataset.id)!.rank;
          if (rank <= 4) expect(folded(el), `seed ${seed} ${working} rank ${rank}`).toBe(false);
        }
        // Every rank 1 to 5 row sits in the Needs you band.
        for (const el of rowEls(v)) {
          const rank = c.rows.find((r) => r.id === el.dataset.id)!.rank;
          if (rank <= 5) expect(el.closest("[data-band]")!.getAttribute("data-band"), `seed ${seed}`).toBe("needs");
        }
      }
    }
  });

  it("holds under every filter, a reused cache, and a failed first read: what is drawn is exactly its bands' rows", () => {
    const cache = new SlotCache();
    for (let seed = 1; seed <= 150; seed++) {
      const c = randomCase(seed);
      const split = bands(buildSlots(c.entries, c.rows, c.tasks));
      const ids = (list: { rows: { id: string }[] }[]) => list.flatMap((s) => s.rows.map((r) => r.id)).sort();
      const want = { needs: ids(split.needs), running: ids(split.running) };
      for (const filter of [null, "needs", "running"] as const) {
        const v = draw(model({ rows: c.rows, entries: c.entries, tasks: c.tasks, filter }), actions(), cache);
        const drawn = rowEls(v).map((r) => r.dataset.id!).sort();
        const expected = filter === "needs" ? want.needs : filter === "running" ? want.running : c.rows.map((r) => r.id).sort();
        expect(drawn, `seed ${seed} filter ${filter}`).toEqual(expected);
        for (const el of rowEls(v)) {
          if (c.rows.find((r) => r.id === el.dataset.id)!.rank <= 4) expect(folded(el), `seed ${seed}`).toBe(false);
        }
      }
      // A failed first read: no inbox row is drawn (none was received), the entries and tasks still are.
      const failed = draw(model({ rows: null, error: "boom", entries: c.entries, tasks: c.tasks }), actions(), cache);
      expect(rowEls(failed).length, `seed ${seed}`).toBe(0);
      expect(failed.textContent, `seed ${seed}`).not.toContain("All clear");
    }
  });

  it("an archived thread's row, an orphan, a row with no thread and a task row are each drawn", () => {
    const rows = parseInbox([
      mkRow("q:orphan", 3, { thread_id: "dddddddd", origin: "Old chat" }), mkRow("pr:alone", 3, { kind: "pr" }),
      mkRow("tq:1", 2, { kind: "task_question", task_id: "11111111", thread_id: "aaaaaaaa" }),
    ])!;
    const v = draw(model({ rows, entries: [mkEntry("aaaaaaaa", "idle")], tasks: [mkTask("11111111", "blocked")] }));
    expect(rowEls(v).map((r) => r.dataset.id).sort()).toEqual(["pr:alone", "q:orphan", "tq:1"]);
    expect(card(v, "orphan:dddddddd").querySelector(".m-thread-title")!.textContent).toBe("Old chat");
  });
});

describe("keyed reuse: a card is rebuilt when anything it renders from changes", () => {
  /** Draws twice with `change` between, the slot's own rows untouched; whether its card was rebuilt. */
  function rebuilt(rows: InboxRow[], opts: { entries?: WorkingItem[]; tasks?: TaskView[]; a?: InboxAnswers; key: string;
                                            change: (ctx: { a: InboxAnswers; m: ListModel }) => void; now?: () => number }) {
    const a = opts.a ?? answers();
    const m = model({ rows, entries: opts.entries ?? [], tasks: opts.tasks ?? [] });
    const cache = new SlotCache();
    const first = card(draw(m, actions(a), cache), opts.key);
    expect(first).toBeTruthy();
    const again = card(draw(m, actions(a), cache), opts.key);
    expect(again, "unchanged: reused").toBe(first);
    opts.change({ a, m });
    const next = card(draw(m, actions(a), cache), opts.key);
    return next !== first;
  }
  const rowA = () => parseInbox([mkRow("q:1", 3, { thread_id: "aaaaaaaa", detail: { text: "Which?", options: [] } })])!;
  const entries = [mkEntry("aaaaaaaa", "needs_input")];

  it("reuses an unchanged card (the same node)", () => {
    const rows = rowA();
    const cache = new SlotCache();
    const m = model({ rows, entries });
    const first = card(draw(m, actions(), cache), "thread:aaaaaaaa");
    const builds = cache.builds;
    expect(card(draw(m, actions(), cache), "thread:aaaaaaaa")).toBe(first);
    expect(cache.builds).toBe(builds);
  });

  it("the global `limited` flips a PR row's buttons (views.ts:158) with its rows unchanged", () => {
    const raw = prRow(11, { actions: ["merge", "fix"], flags: { flagged: true, items: [], more: 0, incomplete: false } });
    const rows = parseInbox([raw])!;
    const pr = (limited: boolean) => ({ act: vi.fn(), cancelFix: vi.fn(), question: vi.fn(), limited, progress: new Map<string, string>(), rows });
    const a = answers({ pr: pr(false) });
    expect(rebuilt(rows, { key: `row:${rows[0].id}`, a, change: ({ a: x }) => { x.pr = pr(true); } })).toBe(true);
  });

  it("another row appearing resolves a PR fix's question (views.ts:215) with the PR's own rows unchanged", () => {
    const raw = fixRow(32, "question");
    const rows = parseInbox([raw])!;
    const taskId = (raw.detail.fix as unknown as { task_id: string }).task_id;
    const a = answers({ pr: { act: vi.fn(), cancelFix: vi.fn(), question: vi.fn(), limited: false, progress: new Map(), rows } });
    const withQuestion = parseInbox([raw, tqRow(taskId)])!;
    expect(rebuilt(rows, { key: `row:${rows[0].id}`, a,
      change: ({ a: x }) => { x.pr = { ...x.pr!, rows: withQuestion }; } })).toBe(true);
    // The two states read differently, so the rebuild is a real change.
    const read = (r: readonly InboxRow[]) => card(draw(model({ rows: [rows[0]] }), actions(answers({ pr: { act: vi.fn(), cancelFix: vi.fn(),
      question: vi.fn(), limited: false, progress: new Map(), rows: r } }))), `row:${rows[0].id}`)
      .querySelector('[data-testid="m-pr-fix-question"]')!.textContent;
    expect(read(rows)).toBe("Open its task");
    expect(read(withQuestion)).toBe("Answer its question");
  });

  it("a task's brief and updated time (views.ts:709) rebuild its card", () => {
    const key = "task:11111111";
    const t = mkTask("11111111", "running");
    expect(rebuilt([], { key, tasks: [t], change: ({ m }) => { m.tasks = [{ ...t, brief: "A new brief" }]; } })).toBe(true);
    expect(rebuilt([], { key, tasks: [t], change: ({ m }) => { m.tasks = [{ ...t, updated: "2026-11-15T10:29:00Z" }]; } })).toBe(true);
    expect(rebuilt([], { key, tasks: [t], change: ({ m }) => { m.tasks = [{ ...t, step: 2 }]; } })).toBe(true);
    expect(rebuilt([], { key, tasks: [t], change: ({ m }) => { m.tasks = [{ ...t, state: "verifying" }]; } })).toBe(true);
  });

  it("the entry's label, title and last activity, and a row's own fields, rebuild it", () => {
    const rows = rowA();
    const key = "thread:aaaaaaaa";
    const mk = (over: Partial<WorkingItem>) => ({ ...entries[0], ...over });
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.entries = [mk({ title: "renamed" })]; } })).toBe(true);
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.entries = [mk({ status: "working" })]; } })).toBe(true);
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.entries = [mk({ last: "2026-11-15T10:20:00Z" })]; } })).toBe(true);
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.rows = [{ ...rows[0], title: "new title" }]; } })).toBe(true);
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.rows = [{ ...rows[0], expires_at: "2026-11-15T11:00:00Z" }]; } })).toBe(true);
    expect(rebuilt(rows, { key, entries, change: ({ m }) => { m.rows = [{ ...rows[0], detail: { text: "Changed?", options: [] } }]; } })).toBe(true);
  });

  it("each UI state rebuilds the card: sending, a refusal note, progress, withdrawing, picks, the command's open state", () => {
    const rows = parseInbox([mkRow("prop:11111111", 3, { kind: "proposal", task_id: "11111111", detail: { brief: "b" } })])!;
    const a = answers({ withdraw: { confirming: new Set(), ask: vi.fn(), back: vi.fn() }, expanded: new Map(),
      toggle: vi.fn(), pr: { act: vi.fn(), cancelFix: vi.fn(), question: vi.fn(), limited: false, progress: new Map(), rows } });
    const key = "task:11111111";
    const id = rows[0].id;
    const cases: [string, (x: InboxAnswers) => void][] = [
      ["sending", (x) => (x.busy as Set<string>).add(id)],
      ["a refusal note", (x) => (x.notes as Map<string, string>).set(id, "Not sent: refused")],
      ["progress", (x) => (x.pr!.progress as Map<string, string>).set(id, "Merging…")],
      ["withdrawing", (x) => (x.withdraw!.confirming as Set<string>).add(id)],
      ["a pick", (x) => x.picks.set(id, 1)],
      ["the command opened", (x) => (x.expanded as Map<string, boolean>).set(id, true)],
    ];
    for (const [name, change] of cases) {
      expect(rebuilt(rows, { key, a: answers({ ...a, busy: new Set(), notes: new Map(), picks: new Map(),
        expanded: new Map(), withdraw: { confirming: new Set(), ask: vi.fn(), back: vi.fn() },
        pr: { ...a.pr!, progress: new Map() } }), change: ({ a: x }) => change(x) }), name).toBe(true);
    }
  });

  it("a draft is the one exception: typing changes no signature, and the draft survives a rebuild", () => {
    const rows = rowA();
    const a = answers();
    const slot = buildSlots(entries, rows, null)[0];
    const before = slotSignature(slot, "needs", a, NOW);
    a.drafts.set("q:1", "half an answer");
    expect(slotSignature(slot, "needs", a, NOW)).toBe(before);
    // Rebuilt for another reason, the box is filled from the draft.
    const v = draw(model({ rows, entries }), actions(a));
    expect((v.querySelector('[data-testid="m-answer-text"]') as HTMLTextAreaElement).value).toBe("half an answer");
  });

  it("the age words change the signature (a card never shows a stale age)", () => {
    const slot = buildSlots([mkEntry("aaaaaaaa")], [], null)[0];
    const a = answers();
    expect(slotSignature(slot, "recent", a, NOW)).not.toBe(slotSignature(slot, "recent", a, NOW + 3 * 3600_000));
    expect(agoWords("2026-11-15T10:00:00Z", NOW)).toBe("30 min ago");
    expect(agoWords("2026-11-15T05:00:00Z", NOW)).toBe("5 h ago");
    expect(agoWords("2026-11-10T10:00:00Z", NOW)).toBe("5 d ago");
    expect(agoWords("nope", NOW)).toBe("");
  });

  it("redraws the rendered provider when only its detail field changes", () => {
    const rows = [mkRow("appr:1", 1, { kind: "approval", thread_id: "aaaaaaaa", detail: { provider: "claude" } })];
    const m = model({ rows, entries });
    const cache = new SlotCache();
    const a = actions();
    const first = card(draw(m, a, cache), "thread:aaaaaaaa");
    expect(first.querySelector(".m-thread-provider")!.textContent).toBe("Claude");
    rows[0].detail.provider = "codex";
    const next = card(draw(m, a, cache), "thread:aaaaaaaa");
    expect(next).not.toBe(first);
    expect(next.querySelector(".m-thread-provider")!.textContent).toBe("Codex");
  });

  it("redraws the rendered age when only time passes, including a row-only slot", () => {
    for (const m of [model({ entries: [mkEntry("aaaaaaaa")] }),
      model({ rows: [mkRow("q:1", 3, { thread_id: "aaaaaaaa", asked_at: "2026-11-15T10:00:00Z" })] })]) {
      const cache = new SlotCache();
      const a = actions();
      const first = threadsView(m, a, cache, NOW, null).querySelector('[data-testid="m-thread"]')!;
      const next = threadsView(m, a, cache, NOW + 60_000, null).querySelector('[data-testid="m-thread"]')!;
      expect(next).not.toBe(first);
      expect(next.querySelector(".m-thread-age")!.textContent).toBe("31 min ago");
    }
  });

  it("redraws the absolute-time tooltip across midnight even if relative words stay the same", () => {
    const at = new Date(2026, 10, 15, 23, 30, 30).getTime();
    const before = new Date(2026, 10, 15, 23, 59, 55).getTime();
    const after = before + 10_000;
    const m = model({ entries: [mkEntry("aaaaaaaa", "idle", { last: new Date(at).toISOString() })] });
    const cache = new SlotCache();
    const a = actions();
    const first = threadsView(m, a, cache, before, null).querySelector('[data-testid="m-thread"]')!;
    // Move across local midnight within the same relative minute.
    const next = threadsView(m, a, cache, after, null).querySelector('[data-testid="m-thread"]')!;
    expect(next.querySelector(".m-thread-age")!.textContent).toBe(first.querySelector(".m-thread-age")!.textContent);
    expect(next).not.toBe(first);
    expect(next.querySelector(".m-thread-age")!.getAttribute("title"))
      .not.toBe(first.querySelector(".m-thread-age")!.getAttribute("title"));
  });

  it("a card that moves band is rebuilt for its new band", () => {
    const m = model({ entries: [mkEntry("aaaaaaaa", "idle")] });
    const cache = new SlotCache();
    const first = card(draw(m, actions(), cache), "thread:aaaaaaaa");
    m.entries = [mkEntry("aaaaaaaa", "working")];
    const next = card(draw(m, actions(), cache), "thread:aaaaaaaa");
    expect(next).not.toBe(first);
    expect(next.dataset.band).toBe("running");
  });

  it("drops cache entries for slots that are gone", () => {
    const cache = new SlotCache();
    draw(model({ entries: [mkEntry("aaaaaaaa"), mkEntry("bbbbbbbb")] }), actions(), cache);
    expect(cache.map.size).toBe(2);
    draw(model({ entries: [mkEntry("aaaaaaaa")] }), actions(), cache);
    expect([...cache.map.keys()]).toEqual(["thread:aaaaaaaa"]);
  });
});

describe("untrusted text stays text", () => {
  it("renders hostile titles, origins, project names and briefs as text", () => {
    const rows = parseInbox([mkRow("q:1", 3, { thread_id: "dddddddd", origin: HOSTILE }), mkRow("q:2", 3, { title: HOSTILE })])!;
    const v = draw(model({ rows, entries: [mkEntry("aaaaaaaa", "idle", { title: HOSTILE, project: HOSTILE })],
      tasks: [mkTask("11111111", "running", { brief: HOSTILE })] }));
    expect(v.querySelectorAll(MARKUP).length).toBe(0);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(v.textContent).toContain(HOSTILE);
  });

  it("renders a provider as text", () => {
    const v = draw(model({ rows: [mkRow("appr:1", 1, { kind: "approval", detail: { provider: HOSTILE } })] }));
    expect(v.querySelector(".m-thread-provider")!.textContent).toContain("<img");
    expect(v.querySelectorAll(MARKUP)).toHaveLength(0);
  });
});

describe("the interleaved order is the bands'", () => {
  it("draws the cards in bands() order inside each band", () => {
    const c = randomCase(7);
    const v = draw(model({ rows: c.rows, entries: c.entries, tasks: c.tasks }));
    const want = bands(buildSlots(c.entries, c.rows, c.tasks));
    const keys = (band: string) => Array.from(v.querySelectorAll<HTMLElement>(`section[data-band="${band}"] > [data-testid="m-thread"], section[data-band="${band}"] [data-testid="m-recent-rest"] > [data-testid="m-thread"]`))
      .map((n) => n.dataset.key);
    expect(keys("needs")).toEqual(want.needs.map((s) => s.key));
    expect(keys("running")).toEqual(want.running.map((s) => s.key));
    expect(REPO).toBeTruthy();
  });
});
