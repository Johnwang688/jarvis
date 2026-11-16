import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { mkEntry, mkRow, mkTask, K1, T1, T2 } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach(app => app.stop()); vi.useRealTimers(); });
const settle = async () => { for (let i = 0; i < 160; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });
const line = (seq: number, text = `line ${seq}`) => ({ seq, role: "Claude", text });
const report = (over: Record<string, unknown> = {}) => ({ v: 1, overnight: [], since: [], before: [], live: [], open: 0, ...over });

async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const replies: Record<string, Got | Promise<Got>> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
    "/m/api/inbox": ok({ rows: [] }),
    "/m/api/working": ok({ entries: [], total: 0 }),
    "/m/api/tasks": ok({ tasks: [] }),
    "/m/api/review": ok(report()),
    [`/m/api/threads/${T1}/log`]: ok({ rows: [line(1)], next: 2 }),
    [`/m/api/threads/${T2}/log`]: ok({ rows: [], next: 0 }),
    [`/m/api/tasks/${K1}`]: ok({ task: mkTask(K1, "running", { brief: "Previously read task" }) }),
  };
  const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path] ?? replies[path.split("?")[0]] ?? ok({}));
  const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  const warning = (resource: string) => root.querySelector<HTMLElement>(`.m-resource-warning[data-resource="${resource}"]`);
  return { app, replies, root, fetchJson, warning };
}

describe("PR 381 fresh verifier counterexamples", () => {
  it.each(["since", "live"] as const)("wrong-shaped review %s must not erase its last read and clear freshness", async section => {
    const h = await harness();
    const row = mkRow("appr:retained", 1, { kind: "approval", title: "Previously read review row" });
    h.replies["/m/api/review"] = ok(report({ [section]: [{ row }], open: 1 }));
    await h.app.loadReview(); h.app.show("review"); await settle();
    const retained = h.app.review;
    h.replies["/m/api/review"] = { ok: false, why: "error", status: 503, body: { error: "unavailable" } };
    await h.app.loadReview();
    expect(h.warning("review")).not.toBeNull();
    h.replies["/m/api/review"] = ok(report({ [section]: {} }));
    await h.app.loadReview(); await settle();
    expect.soft(h.app.review, "a present non-array record list is malformed, not an empty list").toBe(retained);
    expect.soft(h.warning("review"), "malformed optional sections cannot establish freshness").not.toBeNull();
  });

  it("a malformed task detail refresh retains the already read task", async () => {
    const h = await harness();
    await h.app.openTask(K1);
    expect(h.root.textContent).toContain("Previously read task");
    h.replies[`/m/api/tasks/${K1}`] = ok({});
    await h.app.openTask(K1); await settle();
    expect(h.root.textContent, "task detail must not disappear into a permanent Loading state after a malformed 200")
      .toContain("Previously read task");
  });

  it("an earlier opening of A cannot overwrite the fresh A after A → B → A", async () => {
    const h = await harness();
    let resolve!: (got: Got) => void;
    h.replies[`/m/api/threads/${T1}/log`] = new Promise<Got>(r => { resolve = r; });
    const oldOpening = h.app.openThread(T1); await settle();
    await h.app.openThread(T2);
    h.replies[`/m/api/threads/${T1}/log`] = ok({ rows: [line(1), line(9, "Newly read answer")], next: 10 });
    await h.app.openThread(T1);
    expect(h.app.thread?.next).toBe(10);
    resolve(ok({ rows: [line(1, "Old snapshot")], next: 2 }));
    await oldOpening; await settle();
    expect.soft(h.root.textContent).toContain("Newly read answer");
    expect.soft(h.app.thread?.next, "the old opening must not rewind the cursor of the current opening").toBe(10);
  });
});

describe("PR 381 fresh verifier passing controls and mutation targets", () => {
  it("keeps an open approval on a partial absence and refuses it only after a complete absence", async () => {
    const h = await harness();
    const approval = mkRow("appr:kept", 1, { kind: "approval" });
    h.replies["/m/api/inbox/appr%3Akept/detail"] = ok({
      card: { tool: "Bash", command: "echo test", timeout_s: 600 },
      can: { allow: true, always: false }, secure: { ok: true },
    });
    h.replies["/m/api/inbox"] = ok({ rows: [approval] });
    await h.app.loadInbox();
    h.app.openCard(h.app.inbox![0]); await settle();
    const opened = h.app.card;
    expect(opened?.detail).not.toBeNull();
    expect(opened?.refused).toBeNull();
    h.replies["/m/api/inbox"] = ok({ rows: [mkRow("q:new", 2), null] });
    await h.app.loadInbox(); await settle();
    expect(h.app.card).toBe(opened);
    expect(h.app.card?.refused).toBeNull();
    expect(h.warning("inbox")?.dataset.read).toBe("partial");
    h.replies["/m/api/inbox"] = ok({ rows: [mkRow("q:new", 2)] });
    await h.app.loadInbox(); await settle();
    expect(h.app.card?.refused).toContain("no longer waiting");
  });

  it("retries a dropped log line across sparse physical seqs, applies it once, and never spins on a persistent bad line", async () => {
    const h = await harness();
    await h.app.openThread(T1);
    const path = `/m/api/threads/${T1}/log`;
    h.replies[path] = ok({ rows: [line(5), null, line(20)], next: 24, more: true });
    await h.app.moreLog(); await settle();
    expect(h.app.thread?.rows.map(r => r.seq)).toEqual([1, 5]);
    expect(h.app.thread?.next).toBe(6);
    expect(h.warning(`threads/${T1}/log`)?.dataset.read).toBe("partial");
    let calls = h.fetchJson.mock.calls.length;
    h.replies[path] = ok({ rows: [null, line(20)], next: 24, more: true });
    for (let i = 0; i < 3; i++) {
      await h.app.moreLog(); await settle();
      expect(h.fetchJson.mock.calls.length).toBe(++calls);
      expect(h.fetchJson.mock.calls.at(-1)?.[0]).toBe(`${path}?after=6`);
      expect(h.app.thread?.next).toBe(6);
    }
    await vi.advanceTimersByTimeAsync(12_000); await settle();
    expect(h.fetchJson.mock.calls.length).toBe(calls);
    h.replies[path] = ok({ rows: [line(12, "Recovered line"), line(20)], next: 24 });
    await h.app.moreLog(); await h.app.moreLog(); await settle();
    expect(h.app.thread?.rows.map(r => r.seq)).toEqual([1, 5, 12, 20]);
    expect(h.warning(`threads/${T1}/log`)).toBeNull();
  });

  it("draws every valid rank 1–4 row in a partial inbox, across absent or partial working/tasks reads", async () => {
    const h = await harness();
    const rows = [1, 2, 3, 4].map(rank => mkRow(`q:rank${rank}`, rank, {
      thread_id: rank < 3 ? T1 : T2, task_id: rank === 4 ? K1 : null,
      hidden: { dismissedAt: "2026-11-15T00:00:00Z", snoozedUntil: null }, markable: true,
    }));
    h.replies["/m/api/inbox"] = ok({ rows: [...rows, null] });
    h.replies["/m/api/working"] = ok({ entries: [mkEntry(T1), null], total: 2 });
    h.replies["/m/api/tasks"] = ok({ tasks: [mkTask(K1, "running"), null] });
    await h.app.loadWorking(true); await h.app.loadTasks(); await h.app.loadInbox(); await settle();
    for (const row of rows) {
      const nodes = h.root.querySelectorAll<HTMLElement>(`[data-testid="m-inbox-row"][data-id="${row.id}"]`);
      expect(nodes).toHaveLength(1);
      expect(nodes[0].closest("[hidden]"), row.id).toBeNull();
      expect(nodes[0].closest("[data-band]")?.getAttribute("data-band")).toBe("needs");
    }
    expect(h.warning("inbox")?.textContent).toContain("incomplete");
  });

  it("notification review applies a partial report, warns, and opens with an empty selection", async () => {
    const h = await harness();
    h.replies["/m/api/review"] = ok(report({ overnight: [{ row: mkRow("q:new", 3) }, null], opens_here: true, open: 2 }));
    await h.app.openFromNotification(); await settle();
    expect(h.app.review?.overnight.map(i => i.row.id)).toEqual(["q:new"]);
    expect(h.warning("review")?.dataset.read).toBe("partial");
    expect(h.root.querySelectorAll('input[type="checkbox"]:checked')).toHaveLength(0);
  });
});
