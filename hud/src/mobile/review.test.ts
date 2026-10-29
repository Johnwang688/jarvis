import { describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { reviewView, type ReviewActions, type ReviewState } from "./review";
import { parseReview, type ReviewItem } from "../lib/review";

const H = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>';
const MARKUP = "img, script, svg, iframe, [onerror], [onload]";
const tag = (c: string) => c.repeat(64);

function row(id: string, kind: string, rank: number, over: Record<string, unknown> = {}) {
  return { id, kind, rank, title: `${kind} ${id}`, blocks: "b", project_id: null, thread_id: "aaaaaaaa", task_id: null,
    asked_at: "2026-03-11T04:30:00+00:00", expires_at: null, origin: "", detail: {}, ...over };
}

function deferred(id: string, detail: Record<string, unknown> = {}) {
  return row(`defer:${id}`, "deferred", 4, { detail: { deferral_id: id, tool: "Bash", grantable: true, why_not: "",
    expired: false, hud_only: false, count: 1, more: 0, card: { tool: "Bash", command: `make ${id} ${H}` }, ...detail } });
}

function report(over: Record<string, unknown> = {}) {
  const items = [
    { row: deferred("a"), section: "overnight", place: "chat:aaaaaaaa", approve: true, leave: true, why: "", tag: tag("a") },
    { row: deferred("b"), section: "overnight", place: "chat:aaaaaaaa", approve: true, leave: true, why: "", tag: tag("b") },
    { row: deferred("c"), section: "overnight", place: "chat:aaaaaaaa", approve: true, leave: true, why: "", tag: tag("c") },
    { row: deferred("d"), section: "overnight", place: "chat:aaaaaaaa", approve: false, leave: true, why: "withheld", tag: null },
    { row: row("dec:d0123456789ab", "decision", 6, { detail: { decision_id: "d0123456789ab", question: "Which " + H,
      options: ["one", "two " + H], default: 1, why: "w", answer: null } }),
      section: "overnight", place: "chat:aaaaaaaa", approve: false, leave: false, why: "", tag: null },
  ];
  return { v: 1, quiet: { enabled: true, now: false, start: null, end: null },
    summary: { approvals: 3, questions: 0, decisions: 1, blocked: 0, proposals: 0, live: 0 }, live: [],
    overnight: items, before: [], fyi: { count: 0 }, more: 0, open: 4, limits: { approve: 10, leave: 25 },
    projects: [{ id: "p1", name: "Calc" }],
    places: [{ key: "chat:aaaaaaaa", kind: "chat", id: "aaaaaaaa", project_id: "p1", title: "Fix " + H }],
    group: { ok: true, why: "" }, opens_here: false, ...over };
}

function state(over: Partial<ReviewState> = {}): ReviewState {
  return { report: parseReview(report()), selection: new Set(), notes: new Map(), receipts: new Map(), busy: new Set(),
    groupBusy: false, confirming: false, flash: "", drafts: new Map(), picks: new Map(), withdrawing: new Set(),
    online: true, ...over };
}

function actions(over: Partial<ReviewActions> = {}): ReviewActions {
  const none = () => {};
  return { toggle: none, approveAsk: none, approveConfirm: none, approveCancel: none, leaveSelected: none, retry: none,
    leaveOne: none, send: none, withdrawAsk: none, withdrawBack: none, openCard: none, open: none, openInbox: none,
    jump: none, ...over };
}

describe("the phone's Review tab (mobile/review.ts)", () => {
  it("draws every daemon string as text, no box ticked, no select-all, no default chosen", () => {
    const view = reviewView(state(), actions(), null);
    document.body.replaceChildren(view);
    expect(view.querySelectorAll(MARKUP).length).toBe(0);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    const boxes = Array.from(view.querySelectorAll<HTMLInputElement>('[data-testid="m-review-tick"]'));
    expect(boxes.length).toBe(4);
    expect(boxes.some((b) => b.checked)).toBe(false);
    expect(view.textContent).not.toMatch(/select all/i);
    const radios = Array.from(view.querySelectorAll<HTMLInputElement>('[data-testid="m-review-option"]'));
    expect(radios.length).toBe(2);
    expect(radios.some((r) => r.checked)).toBe(false);
    expect(view.querySelector('[data-testid="m-review-default"]')).not.toBeNull();
    expect((view.querySelector('[data-testid="m-review-decision-send"]') as HTMLButtonElement).disabled).toBe(true);
    expect(view.querySelector('[data-testid="m-review-chip"]')!.getAttribute("data-anchor")).toBe("review-overnight-chat_aaaaaaaa");
    expect(view.querySelector("#review-overnight-chat_aaaaaaaa")).not.toBeNull();
  });

  it("a tap ticks only its own box; the footer names what blocks Approve", () => {
    const toggle = vi.fn((_i: ReviewItem) => {});
    const view = reviewView(state(), actions({ toggle }), null);
    document.body.replaceChildren(view);
    const box = view.querySelector<HTMLInputElement>('[data-testid="m-review-tick"][data-id="defer:b"]')!;
    box.click();
    expect(toggle).toHaveBeenCalledTimes(1);
    expect(toggle.mock.calls[0][0].row.id).toBe("defer:b");
    const ticked = reviewView(state({ selection: new Set(["defer:a", "defer:d"]) }), actions(), null);
    const approve = ticked.querySelector<HTMLButtonElement>('[data-testid="m-review-approve"]')!;
    expect(approve.disabled).toBe(true);
    expect(ticked.querySelector('[data-testid="m-review-blockers"]')!.textContent).toContain("answer it at the desk");
    expect(ticked.querySelector<HTMLButtonElement>('[data-testid="m-review-leave"]')!.disabled).toBe(false);
    const fine = reviewView(state({ selection: new Set(["defer:a"]) }), actions(), null);
    expect(fine.querySelector<HTMLButtonElement>('[data-testid="m-review-approve"]')!.disabled).toBe(false);
    expect(fine.querySelector('[data-testid="m-review-count"]')!.textContent).toBe("1 selected");
  });

  it("a limited phone gets no group approval, but keeps the per-row Approve and retry", () => {
    const view = reviewView(state({ selection: new Set(["defer:a"]) }), actions(), "limited: Funnel is on");
    expect(view.querySelector<HTMLButtonElement>('[data-testid="m-review-approve"]')!.disabled).toBe(true);
    expect(view.querySelector('[data-testid="m-limited"]')).not.toBeNull();
    expect(view.querySelectorAll('[data-testid="m-review-retry"]').length).toBe(3);
  });

  it("the confirmation lists exactly the ticked items", () => {
    const view = reviewView(state({ selection: new Set(["defer:b", "defer:a"]), confirming: true }), actions(), null);
    const ids = Array.from(view.querySelectorAll<HTMLElement>('[data-testid="m-review-confirm-item"]')).map((li) => li.dataset.id);
    expect(ids).toEqual(["defer:a", "defer:b"]);
    expect(view.querySelector('[data-testid="m-review-confirm-go"]')).not.toBeNull();
  });
});

// -- the app's flow -------------------------------------------------------------------

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

function harness(review: Record<string, unknown>) {
  const posts: { path: string; value: any }[] = [];
  const replies: Record<string, Posted> = {};
  const gets: Record<string, Got> = {};
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows: [] } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null } } };
  gets["/m/api/review"] = { ok: true, body: review };
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path) => gets[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } },
    postJson: async (path, value) => { posts.push({ path, value }); return replies[path] ?? { status: 200, body: { ok: true } }; },
    getCredential: credential,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  const click = async (selector: string) => { root.querySelector<HTMLElement>(selector)!.click(); await settle(); };
  return { app, root, posts, replies, gets, credential, settle, click };
}

describe("the phone app's review (MR-11, MR-12)", () => {
  it("opens on the Review tab when the morning push was the last one, else on the Inbox", async () => {
    const opens = harness(report({ opens_here: true }));
    opens.app.start();
    await opens.settle();
    expect(opens.app.tab).toBe("review");
    const tab = (h: { root: HTMLElement }) => h.root.querySelector<HTMLButtonElement>('[data-testid="m-tab-review"]')!;
    expect([tab(opens).hidden, tab(opens).disabled]).toEqual([false, false]);
    const not = harness(report({ opens_here: false }));
    not.app.start();
    await not.settle();
    expect(not.app.tab).toBe("inbox");
    expect([tab(not).hidden, tab(not).disabled]).toEqual([false, false]);
    const night = harness(report({ quiet: { enabled: true, now: true, start: null, end: null } }));
    night.app.start();
    await night.settle();
    // M9a (phone audit): no review, the tab keeps its slot, off, so Chats and Tasks never move.
    expect([tab(night).hidden, tab(night).disabled]).toEqual([false, true]);
    expect(Array.from(night.root.querySelectorAll("nav.tabs button")).map((b) => b.getAttribute("data-testid")))
      .toEqual(["m-tab-inbox", "m-tab-review", "m-tab-chats", "m-tab-tasks"]);
  });

  it("tick 2 of 3, confirm: one challenge for exactly those, one passkey, then exactly those ids and tags", async () => {
    const h = harness(report({ opens_here: true }));
    h.replies["/m/api/review/approve/options"] = { status: 200, body: { count: 2, publicKey: { challenge: "AAEC" } } };
    h.replies["/m/api/review/approve"] = { status: 200, body: { ok: true, approved: 2, results: [] } };
    h.app.start();
    await h.settle();
    await h.click('[data-testid="m-review-tick"][data-id="defer:a"]');
    await h.click('[data-testid="m-review-tick"][data-id="defer:b"]');
    expect(h.posts).toEqual([]);
    await h.click('[data-testid="m-review-approve"]');
    expect(h.posts).toEqual([]);
    expect(h.credential).not.toHaveBeenCalled();
    const listed = Array.from(h.root.querySelectorAll<HTMLElement>('[data-testid="m-review-confirm-item"]')).map((x) => x.dataset.id);
    expect(listed).toEqual(["defer:a", "defer:b"]);
    await h.click('[data-testid="m-review-confirm-go"]');
    const items = [{ id: "defer:a", tag: tag("a") }, { id: "defer:b", tag: tag("b") }];
    expect(h.posts).toEqual([
      { path: "/m/api/review/approve/options", value: { items } },
      { path: "/m/api/review/approve", value: { items, assertion: { id: "cred", response: {} } } }]);
    expect(h.credential).toHaveBeenCalledTimes(1);
    expect(h.root.querySelector('[data-testid="m-review-flash"]')!.textContent).toContain("Approved 2 once each");
    expect(h.app.reviewSel.size).toBe(0);
  });

  it("a 409 changed unticks those, says why, and approves nothing", async () => {
    const h = harness(report({ opens_here: true }));
    h.replies["/m/api/review/approve/options"] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.replies["/m/api/review/approve"] = { status: 409, body: { error: "changed", changed: [{ id: "defer:b", reason: "answered" }] } };
    h.app.start();
    await h.settle();
    await h.click('[data-testid="m-review-tick"][data-id="defer:a"]');
    await h.click('[data-testid="m-review-tick"][data-id="defer:b"]');
    await h.click('[data-testid="m-review-approve"]');
    await h.click('[data-testid="m-review-confirm-go"]');
    expect([...h.app.reviewSel]).toEqual(["defer:a"]);
    expect(h.root.querySelector('[data-testid="m-review-flash"]')!.textContent).toContain("Nothing was approved");
    expect(h.root.querySelector('[data-id="defer:b"] [data-testid="m-review-note"]')!.textContent).toContain("Unticked");
    expect((h.root.querySelector('[data-testid="m-review-tick"][data-id="defer:b"]') as HTMLInputElement).checked).toBe(false);
  });

  it("a decision is answered in place and folds to a receipt where it stood", async () => {
    const h = harness(report({ opens_here: true }));
    h.replies["/m/api/inbox/decisions/d0123456789ab"] = { status: 200, body: { ok: true, note: "Sent to the asking chat." } };
    h.app.start();
    await h.settle();
    const radio = h.root.querySelector<HTMLInputElement>('[data-testid="m-review-option"][value="0"]')!;
    radio.click();
    await h.settle();
    await h.click('[data-testid="m-review-decision-send"]');
    expect(h.posts).toEqual([{ path: "/m/api/inbox/decisions/d0123456789ab", value: { choice: 0 } }]);
    const receipt = h.root.querySelector('[data-testid="m-review-receipt"]')!;
    expect(receipt.textContent).toBe("Sent to the asking chat.");
    expect(receipt.getAttribute("data-id")).toBe("dec:d0123456789ab");
  });
});

describe("the review of #329's fixes, on the phone", () => {
  async function confirmingAandB(h: ReturnType<typeof harness>) {
    h.app.start();
    await h.settle();
    await h.click('[data-testid="m-review-tick"][data-id="defer:a"]');
    await h.click('[data-testid="m-review-tick"][data-id="defer:b"]');
    await h.click('[data-testid="m-review-approve"]');
    expect(h.app.reviewConfirming).toBe(true);
  }

  it("MED 1: a re-read that drops a confirmed item closes the confirmation; nothing is sent", async () => {
    const h = harness(report({ opens_here: true }));
    await confirmingAandB(h);
    const without = report({ opens_here: true });
    (without.overnight as unknown[]).splice(1, 1);
    h.gets["/m/api/review"] = { ok: true, body: without };
    await h.app.loadReview();
    await h.settle();
    expect(h.app.reviewConfirming).toBe(false);
    expect(h.root.querySelector('[data-testid="m-review-confirm"]')).toBeNull();
    expect(h.root.querySelector('[data-testid="m-review-flash"]')!.textContent).toContain("Something you selected changed");
    expect([...h.app.reviewSel]).toEqual(["defer:a"]);
    // A stale tap on the old "Approve with Face ID" approves nothing.
    await h.app.approveSelected();
    expect(h.posts).toEqual([]);
    expect(h.credential).not.toHaveBeenCalled();
  });

  it("#330 LOW 4: a summary that lands under the open confirmation changes nothing it shows", async () => {
    const h = harness(report({ opens_here: true }));
    await confirmingAandB(h);
    const shown = () => Array.from(h.root.querySelectorAll<HTMLElement>('[data-testid="m-review-confirm-item"]'))
      .map((li) => li.textContent);
    const before = shown();
    expect(h.root.querySelector('[data-testid="m-review-confirm-summary"]')).toBeNull();
    const summarized = report({ opens_here: true });
    const b = (summarized.overnight as Record<string, any>[])[1];
    b.row = { ...b.row, detail: { ...b.row.detail, summary: "Deletes everything in /srv." } };   // same tag
    h.gets["/m/api/review"] = { ok: true, body: summarized };
    await h.app.loadReview();
    await h.settle();
    expect(h.app.reviewConfirming).toBe(true);
    expect(shown()).toEqual(before);
    expect(h.root.querySelector('[data-testid="m-review-confirm-summary"]')).toBeNull();
    // The item itself, behind the dialog, does show the new summary.
    expect(h.root.querySelector('[data-id="defer:b"] [data-testid="m-review-as-summary"]')!.textContent).toContain("Deletes everything");
  });

  it("MED 1: a re-read that changes a confirmed item's tag unticks it with why", async () => {
    const h = harness(report({ opens_here: true }));
    await confirmingAandB(h);
    const retagged = report({ opens_here: true });
    (retagged.overnight as Record<string, unknown>[])[1].tag = tag("f");
    h.gets["/m/api/review"] = { ok: true, body: retagged };
    await h.app.loadReview();
    await h.settle();
    expect(h.app.reviewConfirming).toBe(false);
    expect([...h.app.reviewSel]).toEqual(["defer:a"]);
    expect(h.root.querySelector('[data-id="defer:b"] [data-testid="m-review-note"]')!.textContent)
      .toBe("Unticked: answered elsewhere or gone.");
    expect(h.posts).toEqual([]);
  });

  it("MED 2: entering the Review tab, or a notification's tap, starts with nothing ticked", async () => {
    const h = harness(report({ opens_here: true }));
    h.app.start();
    await h.settle();
    await h.click('[data-testid="m-review-tick"][data-id="defer:a"]');
    expect(h.app.reviewSel.size).toBe(1);
    await h.click('[data-testid="m-tab-inbox"]');
    await h.click('[data-testid="m-tab-review"]');
    expect(h.app.reviewSel.size).toBe(0);
    expect(Array.from(h.root.querySelectorAll<HTMLInputElement>('[data-testid="m-review-tick"]')).some((b) => b.checked)).toBe(false);
    await h.click('[data-testid="m-review-tick"][data-id="defer:b"]');
    await h.click('[data-testid="m-review-approve"]');
    await h.app.openFromNotification();
    await h.settle();
    expect(h.app.tab).toBe("review");
    expect(h.app.reviewSel.size).toBe(0);
    expect(h.app.reviewConfirming).toBe(false);
  });

  it("LOW 6: every argument shows beside the command, in the review and the inbox row", async () => {
    const { inboxRow } = await import("./views");
    const { parseRow } = await import("../lib/inbox");
    const args = { command: "make a", timeout: 120000, run_in_background: true, description: "Build " + H };
    const raw = report();
    const first = (raw.overnight as Record<string, any>[])[0];
    first.row.detail.card = { tool: "Bash", command: "make a", args };
    const view = reviewView(state({ report: parseReview(raw) }), actions(), null);
    const shown = view.querySelector('[data-id="defer:a"] [data-testid="m-review-args"]')!.textContent!;
    for (const word of ["timeout", "120000", "run_in_background", "description", "Build"]) expect(shown).toContain(word);
    expect(view.querySelectorAll(MARKUP).length).toBe(0);
    const row = inboxRow(parseRow(first.row)!, () => {});
    const listed = row.querySelector('[data-testid="m-deferred-args"]')!.textContent!;
    expect(listed).toContain("run_in_background");
    expect(listed).toContain("timeout");
  });
});

