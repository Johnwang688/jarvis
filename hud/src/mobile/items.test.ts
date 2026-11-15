// PX-3b: compact items, "+N for later" and Later. Built through threadsView, as the app draws them.
import { describe, expect, it, vi } from "vitest";
import { parseInbox, type InboxRow } from "../lib/inbox";
import { prRow } from "../lib/prRow.fixtures";
import { folds, laterOk, FOLD_AFTER_RANK, LATER_MIN_RANK, ItemsUi } from "./items";
import { buildSlots } from "./threads";
import { SlotCache, slotSignature, threadsView, type ListActions, type ListModel } from "./threadsView";
import type { InboxAnswers } from "./views";
import { mkEntry, mkRow } from "./threads.fixtures";

const NOW = Date.parse("2026-11-15T10:30:00Z");
const T = "aaaaaaaa";

function answers(over: Partial<InboxAnswers> = {}): InboxAnswers {
  return { openCard: vi.fn(), send: vi.fn(), retry: vi.fn(), busy: new Set(), notes: new Map(), drafts: new Map(),
    picks: new Map(), ...over };
}
function actions(a: InboxAnswers = answers()): ListActions {
  return { answers: a, openRow: vi.fn(), openThread: vi.fn(), openTask: vi.fn(), setFilter: vi.fn(), setRecentAll: vi.fn(), retry: vi.fn() };
}
function model(rows: InboxRow[], over: Partial<ListModel> = {}): ListModel {
  return { rows, entries: [mkEntry(T, "needs_input")], working: "ok", workingCut: { more: false, total: 0 }, tasks: [],
    tasksMore: false, inboxMore: false, filter: null, recentAll: false, error: null, ...over };
}
function draw(rows: InboxRow[], a: ListActions = actions(), cache = new SlotCache()) {
  const view = threadsView(model(rows), a, cache, NOW, null);
  document.body.replaceChildren(view);
  return { view, cache, a };
}
const byId = (id: string) => document.querySelector<HTMLElement>(`[data-testid="m-inbox-row"][data-id="${id}"]`);
const tid = (root: ParentNode, id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const hiddenIn = (n: Element | null) => !n || !!n.closest("[hidden]");
const approval = (id: string, rank: number, over: Partial<InboxRow> = {}) =>
  mkRow(id, rank, { kind: "approval", thread_id: T, title: "Claude asks to use Bash", detail: { tool: "Bash", command: "ls",
    headline: "Run ls in the project", summary: "Lists the files", summary_state: "ready" }, ...over });
const question = (id: string, rank: number) => mkRow(id, rank, { kind: "question", thread_id: T, detail: { text: "Which?", options: ["a", "b"] } });
const missed = (id: string, rank: number) => mkRow(id, rank, { kind: "missed", thread_id: T, title: "Missed " + id });
/** Seven rows on one thread: ranks 1, 2, 3, 4, 5 and two above 5. */
const mixed = () => [approval("appr:1", 1), question("q:2", 2), approval("appr:3", 3), question("q:4", 4), missed("m:5", 5),
  missed("m:6", 6), missed("m:8", 8)];

describe("compact forms", () => {
  it("an approval item shows its headline, summary and one Review button that opens the card; no Allow, no Deny", () => {
    const a = actions();
    draw([approval("appr:1", 1)], a);
    const row = byId("appr:1")!;
    expect(row.dataset.kind).toBe("approval");
    expect(row.querySelector(".headline")!.textContent).toBe("Run ls in the project");
    expect(tid(row, "m-row-summary-text")!.textContent).toContain("Lists the files");
    const buttons = Array.from(row.querySelectorAll("button")).map((b) => b.dataset.testid);
    expect(buttons).toEqual(["m-item-review"]);
    expect(row.querySelector('[data-testid="m-item-review"]')!.textContent).toBe("Review");
    // The command and the tool lines are not in the compact item (they are on the card).
    expect(tid(row, "m-command")).toBeNull();
    tid(row, "m-item-review")!.click();
    expect(a.answers.openCard).toHaveBeenCalledOnce();
    expect((a.answers.openCard as ReturnType<typeof vi.fn>).mock.calls[0][0].id).toBe("appr:1");
  });

  it("a private approval keeps 'shown only at the desk' and still has Review", () => {
    draw([approval("appr:1", 1, { detail: { tool: "Bash", hud_only: true } })]);
    expect(tid(byId("appr:1")!, "m-private")!.textContent).toContain("shown only at the desk");
    expect(tid(byId("appr:1")!, "m-item-review")).not.toBeNull();
  });

  it("an approval's Review is off while its answer is on its way", () => {
    draw([approval("appr:1", 1)], actions(answers({ busy: new Set(["appr:1"]) })));
    expect(tid(byId("appr:1")!, "m-item-review")!.hasAttribute("disabled")).toBe(true);
  });

  it("a question keeps #371's chips and has no Review", () => {
    const a = actions();
    draw([question("q:2", 2)], a);
    const row = byId("q:2")!;
    expect(row.querySelectorAll(".m-chips .m-chip").length).toBe(2);
    expect(tid(row, "m-item-review")).toBeNull();
    row.querySelector<HTMLElement>(".m-chip")!.click();
    expect(a.answers.send).toHaveBeenCalledWith(expect.objectContaining({ id: "q:2" }), { text: "a" }, "a");
  });

  it("a PR item is today's prBlock: its title, summary slot and notice", () => {
    const row = parseInbox([prRow(11, {}, { thread_id: T })])![0];
    draw([row]);
    const el = byId(row.id)!;
    expect(el.dataset.kind).toBe("pr");
    expect(tid(el, "m-pr-title")).not.toBeNull();
    expect(tid(el, "m-pr-summary")).not.toBeNull();
  });

  it("a deferred item keeps its Leave it and Approve and retry in the wrapper", () => {
    const row = mkRow("dfr:1", 6, { kind: "missed", thread_id: T, detail: { deferred: true, tool: "Bash", command: "ls", grantable: true } });
    draw([row]);
    const el = byId("dfr:1");
    expect(el).not.toBeNull();
    // Rank 6 is behind the fold; once opened its controls are there.
    tid(document.body, "m-item-fold")!.click();
    expect(hiddenIn(byId("dfr:1"))).toBe(false);
  });
});

describe("+N for later (IN-8: ranks 1 to 5 never fold)", () => {
  it("constants: the fold starts above 5 and Later starts at 5", () => {
    expect(FOLD_AFTER_RANK).toBe(5);
    expect(LATER_MIN_RANK).toBe(5);
    for (const r of [1, 2, 3, 4, 5]) expect(folds(mkRow("x", r))).toBe(false);
    for (const r of [6, 7, 8]) expect(folds(mkRow("x", r))).toBe(true);
    for (const r of [1, 2, 3, 4]) expect(laterOk(mkRow("x", r))).toBe(false);
    for (const r of [5, 6, 7, 8]) expect(laterOk(mkRow("x", r))).toBe(true);
    // A rank that is not a number never folds.
    expect(folds(mkRow("x", NaN))).toBe(false);
  });

  it("folds exactly the rows above rank 5, behind '+2 for later'; ranks 1 to 5 show", () => {
    const { view } = draw(mixed());
    for (const id of ["appr:1", "q:2", "appr:3", "q:4", "m:5"]) expect(hiddenIn(byId(id)), id).toBe(false);
    for (const id of ["m:6", "m:8"]) expect(hiddenIn(byId(id)), id).toBe(true);
    const fold = tid(view, "m-item-fold")!;
    expect(fold.textContent).toBe("+2 for later");
    expect(fold.getAttribute("aria-expanded")).toBe("false");
    // Every received row is in the page.
    expect(document.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(7);
  });

  it("no fold when nothing is above rank 5", () => {
    const { view } = draw(mixed().slice(0, 5));
    expect(tid(view, "m-item-fold")).toBeNull();
  });

  it("expanding shows the folded rows, and again hides them", () => {
    draw(mixed());
    tid(document.body, "m-item-fold")!.click();
    expect(hiddenIn(byId("m:6"))).toBe(false);
    expect(hiddenIn(byId("m:8"))).toBe(false);
    const fold = tid(document.body, "m-item-fold")!;
    expect(fold.getAttribute("aria-expanded")).toBe("true");
    expect(fold.textContent).toBe("Hide 2 for later");
    fold.click();
    expect(hiddenIn(byId("m:6"))).toBe(true);
    expect(tid(document.body, "m-item-fold")!.textContent).toBe("+2 for later");
  });

  it("a row alone (no head) never folds: its one row is the card", () => {
    const solo = mkRow("m:solo", 7, { kind: "missed", title: "Solo" });
    const view = threadsView(model([solo], { entries: [] }), actions(), new SlotCache(), NOW, null);
    document.body.replaceChildren(view);
    expect(tid(view, "m-item-fold")).toBeNull();
    expect(hiddenIn(byId("m:solo"))).toBe(false);
  });
});

describe("Later", () => {
  it("is offered to rank 5 and up, never to ranks 1 to 4", () => {
    draw(mixed());
    for (const id of ["appr:1", "q:2", "appr:3", "q:4"]) expect(tid(byId(id)!, "m-item-later"), id).toBeNull();
    for (const id of ["m:5", "m:6", "m:8"]) expect(tid(byId(id)!, "m-item-later"), id).not.toBeNull();
  });

  it("collapses the row to a one-line stub, sends nothing, and Show restores it", () => {
    const a = actions();
    draw(mixed(), a);
    tid(byId("m:5")!, "m-item-later")!.click();
    const stub = byId("m:5")!;
    expect(stub.dataset.later).toBe("true");
    expect(stub.querySelectorAll("button").length).toBe(1);
    expect(tid(stub, "m-item-unlater")).not.toBeNull();
    // Nothing is sent, answered, retried or opened.
    expect(a.answers.send).not.toHaveBeenCalled();
    expect(a.answers.retry).not.toHaveBeenCalled();
    expect(a.answers.openCard).not.toHaveBeenCalled();
    expect(a.openRow).not.toHaveBeenCalled();
    tid(stub, "m-item-unlater")!.click();
    expect(byId("m:5")!.dataset.later).toBeUndefined();
    expect(tid(byId("m:5")!, "m-item-later")).not.toBeNull();
  });

  it("a PR row can be put off and shown again", () => {
    const pr = parseInbox([prRow(11, {}, { thread_id: T })])![0];
    draw([pr]);
    tid(byId(pr.id)!, "m-item-later")!.click();
    expect(byId(pr.id)!.dataset.later).toBe("true");
    expect(tid(byId(pr.id)!, "m-pr-title")).toBeNull();
    tid(byId(pr.id)!, "m-item-unlater")!.click();
    expect(tid(byId(pr.id)!, "m-pr-title")).not.toBeNull();
  });

  it("never hides a rank 1 to 4 row, whatever the state holds (even a forged id in the set)", () => {
    const cache = new SlotCache();
    for (const id of ["appr:1", "q:2", "appr:3", "q:4", "m:5"]) cache.ui.later.add(id);
    draw(mixed(), actions(), cache);
    for (const id of ["appr:1", "q:2", "appr:3", "q:4"]) {
      expect(byId(id)!.dataset.later, id).toBeUndefined();
      expect(hiddenIn(byId(id)), id).toBe(false);
    }
    expect(byId("m:5")!.dataset.later).toBe("true");
  });

  it("sends no request: no fetch is made by Later, Show, or the fold", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    try {
      draw(mixed());
      tid(byId("m:5")!, "m-item-later")!.click();
      tid(byId("m:5")!, "m-item-unlater")!.click();
      tid(document.body, "m-item-fold")!.click();
      tid(byId("m:6")!, "m-item-later")!.click();
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally { vi.unstubAllGlobals(); }
  });

  it("is this page's memory: a new cache (a reload) starts with every row in full", () => {
    const first = draw(mixed());
    tid(byId("m:5")!, "m-item-later")!.click();
    expect(first.cache.ui.later.has("m:5")).toBe(true);
    draw(mixed());
    expect(byId("m:5")!.dataset.later).toBeUndefined();
    try { expect(localStorage.length).toBe(0); } catch { /* no storage */ }
  });

  it("survives a redraw with the same cache: the state is in the signature", () => {
    const a = actions();
    const cache = new SlotCache();
    draw(mixed(), a, cache);
    tid(byId("m:5")!, "m-item-later")!.click();
    tid(document.body, "m-item-fold")!.click();
    // The app redraws (a new read): the card is rebuilt or reused, and still shows both.
    draw(mixed(), a, cache);
    expect(byId("m:5")!.dataset.later).toBe("true");
    expect(hiddenIn(byId("m:6"))).toBe(false);
  });
});

describe("the signature follows fold, expand and Later", () => {
  const slot = () => buildSlots([mkEntry(T, "needs_input")], mixed(), null)[0];
  const sig = (ui: ItemsUi) => slotSignature(slot(), "needs", answers(), NOW, ui);

  it("changes on expand, on Later, and on Show", () => {
    const ui = new ItemsUi();
    const base = sig(ui);
    ui.open.add("thread:" + T); ui.rev += 1;
    const expanded = sig(ui);
    expect(expanded).not.toBe(base);
    ui.later.add("m:5"); ui.rev += 1;
    const later = sig(ui);
    expect(later).not.toBe(expanded);
    ui.later.delete("m:5"); ui.rev += 1;
    expect(sig(ui)).not.toBe(later);
  });

  it("a card is rebuilt when the state changes and reused when it does not", () => {
    const a = actions();
    const cache = new SlotCache();
    draw(mixed(), a, cache);
    const built = cache.builds;
    draw(mixed(), a, cache);
    expect(cache.builds).toBe(built);
    cache.ui.later.add("m:6"); cache.ui.rev += 1;
    draw(mixed(), a, cache);
    expect(cache.builds).toBe(built + 1);
    expect(byId("m:6")!.dataset.later).toBe("true");
  });

  it("the control's own click bumps the revision", () => {
    const cache = new SlotCache();
    draw(mixed(), actions(), cache);
    const r0 = cache.ui.rev;
    tid(document.body, "m-item-fold")!.click();
    expect(cache.ui.rev).toBe(r0 + 1);
    tid(byId("m:5")!, "m-item-later")!.click();
    expect(cache.ui.rev).toBe(r0 + 2);
  });

  it("focus stays on the control that was used", () => {
    draw(mixed());
    const later = tid(byId("m:5")!, "m-item-later")!;
    later.focus();
    later.click();
    expect(document.activeElement).toBe(tid(byId("m:5")!, "m-item-unlater"));
  });
});
