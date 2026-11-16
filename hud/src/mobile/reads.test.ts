// PR 381: every list read goes through one validated apply step (reads.ts `readVerdict`,
// PhoneApp.applyRead). The table: each resource x each outcome, from a clean list and from one
// already warned, asserting the warning, the data kept or replaced, and the chat log's cursor.
import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { decodeLog, readVerdict } from "./reads";
import { mkEntry, mkRow, mkTask, K1, K2, T1, T2 } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach((app) => app.stop()); vi.useRealTimers(); });
const settle = async () => { for (let i = 0; i < 160; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });
const daemonRefusal: Got = { ok: false, why: "error", status: 503, body: { error: "read unavailable" } };

type Resource = "inbox" | "working" | "tasks" | "review" | "log";
type Outcome = "valid" | "empty-valid" | "{}" | "all-invalid" | "partial" | "network failure" | "proxy 5xx";
const RESOURCES: Resource[] = ["inbox", "working", "tasks", "review", "log"];
const OUTCOMES: Outcome[] = ["valid", "empty-valid", "{}", "all-invalid", "partial", "network failure", "proxy 5xx"];

const review = (overnight: unknown[]) => ({ v: 1, overnight, before: [], open: overnight.length });
const logRow = (seq: number, text: string) => ({ seq, role: "Claude", text });

/** What each resource holds before the outcome: one record, "kept". */
const RETAINED: Record<Resource, unknown> = {
  inbox: { rows: [mkRow("q:kept", 3, { title: "Kept question" })] },
  working: { entries: [mkEntry(T1, "working", { title: "Kept chat" })], total: 1 },
  tasks: { tasks: [mkTask(K1, "running", { brief: "Kept task" })] },
  review: review([{ row: mkRow("q:kept-review", 3, { title: "Kept review" }) }]),
  log: { rows: [logRow(1, "Kept message")], next: 2 },
};

/** The 200 bodies per outcome. A partial log has a valid line, an invalid one, then a valid one after the gap. */
const BODIES: Record<Resource, Record<"valid" | "empty-valid" | "all-invalid" | "partial", unknown>> = {
  inbox: {
    valid: { rows: [mkRow("q:fresh", 3, { title: "Fresh question" })] },
    "empty-valid": { rows: [] },
    "all-invalid": { rows: [null, { id: 7 }] },
    partial: { rows: [mkRow("q:fresh", 3, { title: "Fresh question" }), null] },
  },
  working: {
    valid: { entries: [mkEntry(T2, "working", { title: "Fresh chat" })], total: 1 },
    "empty-valid": { entries: [], total: 0 },
    "all-invalid": { entries: [null, { thread_id: "not-an-id" }], total: 2 },
    partial: { entries: [mkEntry(T2, "working", { title: "Fresh chat" }), null], total: 2 },
  },
  tasks: {
    valid: { tasks: [mkTask(K2, "running", { brief: "Fresh task" })] },
    "empty-valid": { tasks: [] },
    "all-invalid": { tasks: [null, { id: "x" }] },
    partial: { tasks: [mkTask(K2, "running", { brief: "Fresh task" }), null] },
  },
  review: {
    valid: review([{ row: mkRow("q:fresh-review", 3, { title: "Fresh review" }) }]),
    "empty-valid": review([]),
    "all-invalid": review([null, { row: null }]),
    partial: review([{ row: mkRow("q:fresh-review", 3, { title: "Fresh review" }) }, null]),
  },
  log: {
    valid: { rows: [logRow(2, "Fresh message")], next: 3 },
    "empty-valid": { rows: [], next: 2 },
    "all-invalid": { rows: [null, { seq: "3" }], next: 4 },
    partial: { rows: [logRow(2, "Fresh message"), null, logRow(4, "After the gap")], next: 5 },
  },
};

function reply(resource: Resource, outcome: Outcome): Got {
  if (outcome === "network failure") return { ok: false, why: "offline", status: 0 };
  if (outcome === "proxy 5xx") return { ok: false, why: "offline", status: 502 };   // the proxy's plain 502: no daemon words
  if (outcome === "{}") return ok({});
  return ok(BODIES[resource][outcome]);
}

async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const replies: Record<string, Got> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
    "/m/api/inbox": ok(RETAINED.inbox), "/m/api/working": ok(RETAINED.working),
    "/m/api/tasks": ok(RETAINED.tasks), "/m/api/review": ok(RETAINED.review),
    [`/m/api/threads/${T1}/log`]: ok(RETAINED.log),
  };
  const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path.split("?")[0]] ?? ok({}));
  const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  await app.loadReview(); await settle();
  const warning = (resource: string) => root.querySelector<HTMLElement>(`.m-resource-warning[data-resource="${resource}"]`);
  return { app, root, replies, fetchJson, warning };
}

/** The ids each resource holds now (the chat log: its seqs). */
function held(app: PhoneApp, resource: Resource): (string | number)[] | null {
  switch (resource) {
    case "inbox": return app.inbox?.map((r) => r.id) ?? null;
    case "working": return app.entries?.map((e) => e.thread_id) ?? null;
    case "tasks": return app.tasks?.map((t) => t.id) ?? null;
    case "review": return app.review?.overnight.map((i) => i.row.id) ?? null;
    case "log": return app.thread?.rows.map((r) => r.seq) ?? null;
  }
}

const KEPT: Record<Resource, (string | number)[]> = { inbox: ["q:kept"], working: [T1], tasks: [K1], review: ["q:kept-review"], log: [1] };
const FRESH: Record<Resource, (string | number)[]> = { inbox: ["q:fresh"], working: [T2], tasks: [K2], review: ["q:fresh-review"], log: [1, 2] };
const EMPTY: Record<Resource, (string | number)[]> = { inbox: [], working: [], tasks: [], review: [], log: [1] };

/** What each outcome must leave: the warning (null: none), the data, the log's cursor. */
const EXPECT: Record<Outcome, { warning: null | "failed" | "partial"; data: Record<Resource, (string | number)[]>; next: number }> = {
  valid: { warning: null, data: FRESH, next: 3 },
  "empty-valid": { warning: null, data: EMPTY, next: 2 },
  "{}": { warning: "failed", data: KEPT, next: 2 },
  "all-invalid": { warning: "failed", data: KEPT, next: 2 },
  // The lists apply what parsed; the log applies only the line before the gap, and its cursor stops after it.
  partial: { warning: "partial", data: FRESH, next: 3 },
  "network failure": { warning: "failed", data: KEPT, next: 2 },
  "proxy 5xx": { warning: "failed", data: KEPT, next: 2 },
};

describe("PR 381: one validated apply step for every list read", () => {
  const cases = RESOURCES.flatMap((resource) => OUTCOMES.flatMap((outcome) =>
    [false, true].map((warnedFirst) => ({ resource, outcome, warnedFirst }))));
  it.each(cases)("$resource × $outcome (warned first: $warnedFirst)", async ({ resource, outcome, warnedFirst }) => {
    const h = await harness();
    if (resource === "log") { await h.app.openThread(T1); await settle(); }
    const name = resource === "log" ? `threads/${T1}/log` : resource;
    const path = `/m/api/${name}`;
    const load = { inbox: () => h.app.loadInbox(), working: () => h.app.loadWorking(true),
      tasks: () => h.app.loadTasks(), review: () => h.app.loadReview(), log: () => h.app.moreLog() }[resource];
    expect(held(h.app, resource)).toEqual(KEPT[resource]);
    expect(h.warning(name)).toBeNull();
    if (resource === "log") expect(h.app.thread?.next).toBe(2);
    if (warnedFirst) {
      h.replies[path] = daemonRefusal;
      await load(); await settle();
      expect(h.warning(name)?.textContent).toContain("couldn't refresh: showing the last read");
      expect(held(h.app, resource)).toEqual(KEPT[resource]);
    }
    h.replies[path] = reply(resource, outcome);
    await load(); await settle();
    const want = EXPECT[outcome];
    const note = h.warning(name);
    if (want.warning === null) expect(note, "only a valid read clears the warning").toBeNull();
    else if (want.warning === "failed") {
      expect(note?.textContent).toContain("couldn't refresh: showing the last read");
      expect(note?.dataset.read).toBe("failed");
    } else {
      expect(note?.textContent).toContain("is incomplete: some items couldn't be read");
      expect(note?.dataset.read).toBe("partial");
    }
    expect(held(h.app, resource), "data is replaced only by an applied read").toEqual(want.data[resource]);
    if (resource === "log") {
      expect(h.app.thread?.next, "the cursor passes only lines that were applied").toBe(want.next);
      expect(h.root.textContent).not.toContain("After the gap");
    }
    // Other resources' warnings are their own: none of them was touched.
    for (const other of RESOURCES.filter((r) => r !== resource && r !== "log")) expect(h.warning(other)).toBeNull();
  });

  it("a partial log page asks again for the dropped line: the next read starts at it", async () => {
    const h = await harness();
    await h.app.openThread(T1); await settle();
    const path = `/m/api/threads/${T1}/log`;
    h.replies[path] = reply("log", "partial");
    await h.app.moreLog(); await settle();
    expect(h.app.thread?.next).toBe(3);
    // The daemon now sends the line it could not before, and the one after it.
    h.replies[path] = ok({ rows: [logRow(3, "The missing line"), logRow(4, "After the gap")], next: 5 });
    await h.app.moreLog(); await settle();
    const after = h.fetchJson.mock.calls.map(([p]) => p).filter((p) => p.startsWith(path + "?"));
    expect(after.at(-1)).toBe(`${path}?after=3`);
    expect(h.app.thread?.rows.map((r) => r.seq)).toEqual([1, 2, 3, 4]);
    expect(h.app.thread?.next).toBe(5);
    expect(h.warning(`threads/${T1}/log`)).toBeNull();
  });

  it("the warning clears only after the read was applied: an apply that throws leaves it up", async () => {
    const h = await harness();
    h.replies["/m/api/inbox"] = daemonRefusal;
    await h.app.loadInbox(); await settle();
    expect(h.warning("inbox")).not.toBeNull();
    h.replies["/m/api/inbox"] = reply("inbox", "valid");
    const proto = PhoneApp.prototype as unknown as { renderBadge: () => void };
    const badge = vi.spyOn(proto, "renderBadge").mockImplementationOnce(() => { throw new Error("draw failed"); });
    await h.app.loadInbox().catch(() => {}); await settle();
    badge.mockRestore();
    await h.app.loadMe(); await settle();   // redraws the Inbox without reading it
    expect(h.warning("inbox"), "a read that did not finish applying is not a fresh read").not.toBeNull();
    await h.app.loadInbox(); await settle();
    expect(h.warning("inbox")).toBeNull();
  });

  it("an incomplete Inbox read does not call the open card gone (its row may be the dropped one)", async () => {
    const h = await harness();
    h.app.openCard(h.app.inbox![0]); await settle();
    expect(h.app.card?.rowId).toBe("q:kept");
    h.replies["/m/api/inbox"] = ok({ rows: [mkRow("q:fresh", 3, { title: "Fresh question" }), null] });
    await h.app.loadInbox(); await settle();
    expect(h.app.inbox?.map((r) => r.id)).toEqual(["q:fresh"]);
    expect(h.app.card?.refused ?? "").not.toContain("no longer waiting");
    h.replies["/m/api/inbox"] = ok({ rows: [mkRow("q:fresh", 3, { title: "Fresh question" })] });
    await h.app.loadInbox(); await settle();
    expect(h.app.card?.refused).toContain("no longer waiting");
  });

  it("a first malformed log read says Not read yet, never No messages yet, and a later valid read replaces it", async () => {
    const h = await harness();
    h.replies[`/m/api/threads/${T2}/log`] = ok({ rows: [null], next: 1 });
    await h.app.openThread(T2); await settle();
    const transcript = () => h.root.querySelector('[data-testid="m-transcript"]')?.textContent ?? "";
    expect(h.warning(`threads/${T2}/log`)?.textContent).toContain("not read yet");
    expect(transcript()).toContain("Not read yet.");
    expect(transcript()).not.toContain("No messages yet");
    expect(h.app.thread?.next).toBe(0);
    h.replies[`/m/api/threads/${T2}/log`] = ok({ rows: [], next: 0 });
    await h.app.moreLog(); await settle();
    expect(h.warning(`threads/${T2}/log`)).toBeNull();
    expect(transcript()).toContain("No messages yet.");
  });
});

describe("readVerdict and decodeLog (pure)", () => {
  it("judges shape and consistency", () => {
    expect(readVerdict(null)).toBe("malformed");
    expect(readVerdict([])).toBe("valid");
    expect(readVerdict([{ sent: 0, valid: 0 }])).toBe("valid");
    expect(readVerdict([{ sent: 2, valid: 2 }])).toBe("valid");
    expect(readVerdict([{ sent: 2, valid: 1 }])).toBe("partial");
    expect(readVerdict([{ sent: 1, valid: 0 }])).toBe("malformed");
    // One non-empty list with nothing valid makes the whole read malformed, whatever the others hold.
    expect(readVerdict([{ sent: 3, valid: 3 }, { sent: 1, valid: 0 }])).toBe("malformed");
  });

  it("cuts a log page before its first invalid line and stops the cursor there", () => {
    const cut = decodeLog({ rows: [logRow(5, "a"), logRow(6, "b"), null, logRow(8, "c")], next: 9, more: true });
    expect(cut?.value.rows.map((r) => r.seq)).toEqual([5, 6]);
    expect(cut?.value.next).toBe(7);
    expect(cut?.value.more).toBe(false);
    expect(readVerdict(cut!.lists)).toBe("partial");
    expect(readVerdict(decodeLog({ rows: [null, logRow(8, "c")], next: 9 })!.lists)).toBe("malformed");
    expect(decodeLog({ rows: [logRow(5, "a")], next: 6, more: true })?.value).toMatchObject({ next: 6, more: true });
    expect(decodeLog({ rows: {}, next: 1 })).toBeNull();
  });
});
