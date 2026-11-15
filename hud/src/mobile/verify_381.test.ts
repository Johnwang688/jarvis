import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { mkEntry, T1, T2 } from "./threads.fixtures";

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
// Daemon errors isolate resource freshness from offline recovery's automatic retries.
const failure: Got = { ok: false, why: "error", status: 503, body: { error: "read unavailable" } };
const log = (text: string) => ok({ rows: [{ seq: 1, role: "Claude", text }], next: 1 });

async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const replies: Record<string, Got | Promise<Got>> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
    "/m/api/inbox": ok({ rows: [] }),
    "/m/api/working": ok({ entries: [mkEntry(T1, "working", { title: "Retained running chat" })], more: true, total: 250 }),
    "/m/api/tasks": ok({ tasks: [] }),
    "/m/api/review": ok({ v: 1, overnight: [], before: [], open: 0 }),
    [`/m/api/threads/${T1}/log`]: log("First chat message"),
    [`/m/api/threads/${T2}/log`]: log("Second chat message"),
  };
  const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path] ?? replies[path.split("?")[0]] ?? ok({}));
  const postJson = vi.fn(async () => ({ status: 409, body: { error: "conflict" } }));
  const app = new PhoneApp({ root, fetchJson, postJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  const warning = (resource: string) => root.querySelector<HTMLElement>(`.m-resource-warning[data-resource="${resource}"]`);
  return { app, root, replies, fetchJson, postJson, warning };
}

describe("PR 381 independent counterexamples", () => {
  it.each(["review", "log"])("a malformed 200 must not clear the %s failed-read warning", async resource => {
    const h = await harness();
    if (resource === "log") await h.app.openThread(T1);
    else h.app.show("review");
    await settle();
    const name = resource === "log" ? `threads/${T1}/log` : "review";
    const path = `/m/api/${name}`;
    const load = () => resource === "log" ? h.app.moreLog() : h.app.loadReview();
    const previous = h.replies[path];
    h.replies[path] = failure;
    await load();
    expect(h.warning(name)?.textContent).toContain("showing the last read");
    h.replies[path] = ok({}); // parseReview/parseLog explicitly reject this body.
    await load(); await h.app.loadMe(); await settle();
    expect(h.warning(name), "no valid resource data was read; the stale warning must survive").not.toBeNull();
    h.replies[path] = previous;
    await load();
    expect(h.warning(name)).toBeNull();
  });

  it("a first daemon-refused log read must not claim to show a last successful read", async () => {
    const h = await harness();
    h.replies[`/m/api/threads/${T2}/log`] = failure;
    await h.app.openThread(T2); await h.app.loadMe(); await settle();
    expect(h.warning(`threads/${T2}/log`)?.textContent).toContain("not read yet");
  });
});

describe("PR 381 independent passing controls", () => {
  it("late success from the previous chat leaves the current chat's warning alone", async () => {
    const h = await harness();
    let resolve!: (got: Got) => void;
    h.replies[`/m/api/threads/${T1}/log`] = new Promise<Got>(r => { resolve = r; });
    const old = h.app.openThread(T1);
    h.replies[`/m/api/threads/${T2}/log`] = failure;
    await h.app.openThread(T2);
    expect(h.warning(`threads/${T2}/log`)).not.toBeNull();
    resolve(log("late first chat")); await old; await settle();
    expect(h.warning(`threads/${T2}/log`)).not.toBeNull();
    expect(h.root.textContent).not.toContain("late first chat");
    h.replies[`/m/api/threads/${T2}/log`] = ok({ rows: [], next: 0 });
    await h.app.moreLog();
    expect(h.warning(`threads/${T2}/log`)).toBeNull();
  });

  it("a refused write neither creates nor clears list failures and leaves the orb Live", async () => {
    const h = await harness();
    h.replies["/m/api/review"] = failure;
    await h.app.loadReview();
    await h.app.sendMessage(T1, "one message"); await settle();
    expect(h.postJson).toHaveBeenCalledTimes(1);
    expect(h.warning("review")).not.toBeNull();
    expect(h.warning("inbox")).toBeNull();
    expect(h.warning("working")).toBeNull();
    expect(h.root.querySelector('[data-testid="m-conn"]')?.textContent).toMatch(/^Live/);
  });

  it("retains running entries and freshness through repeated unrelated successes and redraws", async () => {
    const h = await harness();
    h.replies["/m/api/working"] = failure;
    await h.app.loadWorking(true);
    for (let i = 0; i < 4; i++) {
      await h.app.loadInbox(); await h.app.loadMe();
      h.app.show("chats"); h.app.show("inbox"); await settle();
      expect(h.warning("working")?.textContent).toContain("showing the last read");
      expect(h.root.textContent).toContain("Retained running chat");
    }
    const count = h.fetchJson.mock.calls.length;
    await vi.advanceTimersByTimeAsync(29_999); await settle();
    expect(h.fetchJson.mock.calls.length).toBe(count);
    await vi.advanceTimersByTimeAsync(1); await settle();
    expect(h.fetchJson.mock.calls.length).toBe(count + 1);
    expect(h.warning("working")).not.toBeNull();
    h.replies["/m/api/working"] = ok({ entries: [], total: 0 });
    await h.app.loadWorking(true);
    expect(h.warning("working")).toBeNull();
    expect(h.root.textContent).not.toContain("Retained running chat");
  });
});
