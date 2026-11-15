import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { mkEntry, mkRow, mkTask, T1, T2, K1 } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach(app => app.stop()); vi.useRealTimers(); });
const settle = async () => { for (let i = 0; i < 120; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });
const failed: Got = { ok: false, why: "offline", status: 504 };

async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const log = (text: string) => ok({ rows: [{ seq: 1, role: "Claude", text }], next: 1 });
  const gets: Record<string, Got> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
    "/m/api/inbox": ok({ rows: [mkRow("q:old", 3, { title: "Old question" })] }),
    "/m/api/working": ok({ entries: [mkEntry(T1, "idle", { title: "Old thread" })], total: 1 }),
    "/m/api/tasks": ok({ tasks: [mkTask(K1, "running", { brief: "Old task" })] }),
    "/m/api/review": ok({ v: 1, overnight: [], before: [], open: 1 }),
    [`/m/api/threads/${T1}/log`]: log("Old message"),
    [`/m/api/threads/${T2}/log`]: log("Other message"),
  };
  const app = new PhoneApp({ root, onSignedOut: vi.fn(), signOut: async () => {},
    fetchJson: async path => gets[path.split("?")[0]] ?? ok({}), openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  const warning = (resource: string) => root.querySelector<HTMLElement>(`.m-resource-warning[data-resource="${resource}"]`);
  return { app, gets, root, warning };
}

describe("list freshness belongs to each read", () => {
  const resources = ["inbox", "working", "tasks", "review", "log"] as const;
  it.each(resources.flatMap(resource => [false, true].map(failedFirst => ({ resource, failedFirst }))))(
    "retains $resource data and warns on a malformed 200 (failed first: $failedFirst)", async ({ resource, failedFirst }) => {
      const h = await harness();
      if (resource === "log") await h.app.openThread(T1);
      const name = resource === "log" ? `threads/${T1}/log` : resource;
      const path = `/m/api/${name}`;
      const load = { inbox: () => h.app.loadInbox(), working: () => h.app.loadWorking(true),
        tasks: () => h.app.loadTasks(), review: () => h.app.loadReview(), log: () => h.app.moreLog() }[resource];
      const snapshot = () => ({ inbox: h.app.inbox, inboxMore: h.app.inboxMore,
        entries: h.app.entries, cutoff: h.app.workingCut, tasks: h.app.tasks, tasksMore: h.app.tasksMore,
        review: h.app.review, rows: h.app.thread?.rows, next: h.app.thread?.next });
      const before = snapshot();
      if (failedFirst) {
        h.gets[path] = { ok: false, why: "error", status: 503, body: { error: "read unavailable" } };
        await load();
        expect(h.warning(name)).not.toBeNull();
      }
      // Neither a missing list nor a wrong-shaped list may erase retained rows or clear a warning.
      for (const body of [{}, null, { rows: {}, entries: {}, tasks: {} }]) {
        h.gets[path] = ok(body);
        await load(); await h.app.loadMe(); await settle();
        expect(h.warning(name)?.textContent).toContain("showing the last read");
        expect(snapshot()).toEqual(before);
      }
      const empty = { inbox: { rows: [] }, working: { entries: [], total: 0 }, tasks: { tasks: [] },
        review: { v: 1, overnight: [], before: [], open: 0 }, log: { rows: [], next: 1 } }[resource];
      h.gets[path] = ok(empty);
      await load(); await settle();
      expect(h.warning(name)).toBeNull();
    });

  it("validates review reads from notifications before clearing their warning", async () => {
    const h = await harness();
    const previous = h.app.review;
    h.gets["/m/api/review"] = ok({});
    await h.app.openFromNotification(); await settle();
    expect(h.app.review).toBe(previous);
    expect(h.warning("review")).not.toBeNull();
    h.gets["/m/api/review"] = ok({ v: 1, overnight: [], before: [], open: 0 });
    await h.app.openFromNotification(); await settle();
    expect(h.warning("review")).toBeNull();
  });

  it.each([ok({}), { ok: false, why: "error", status: 503, body: { error: "read unavailable" } } as Got])(
    "a first invalid or refused log read has no successful last read (%j)", async got => {
      const h = await harness();
      const name = `threads/${T2}/log`, path = `/m/api/${name}`;
      h.gets[path] = got;
      await h.app.openThread(T2); await h.app.loadMe(); await settle();
      expect(h.warning(name)?.textContent).toContain("not read yet");
      h.gets[path] = ok({ rows: [], next: 0 });
      await h.app.moreLog();
      expect(h.warning(name)).toBeNull();
      h.gets[path] = got;
      await h.app.moreLog();
      expect(h.warning(name)?.textContent).toContain("showing the last read");
    });

  it.each(["inbox", "working", "tasks", "review"] as const)("keeps %s stale across healthy reads, redraws and 12 seconds, until its own success", async resource => {
    const h = await harness();
    const load = { inbox: () => h.app.loadInbox(), working: () => h.app.loadWorking(true),
      tasks: () => h.app.loadTasks(), review: () => h.app.loadReview() };
    const path = `/m/api/${resource}`, previous = h.gets[path];
    h.gets[path] = failed;
    await load[resource]();
    await h.app.loadMe();
    await vi.advanceTimersByTimeAsync(12_001); await settle();
    expect(h.warning(resource)?.textContent).toContain("couldn't refresh: showing the last read");
    expect(h.warning(resource)?.dataset.stale).toBe("true");
    for (const text of ["Old question", "Old thread", "Old task"]) expect(h.root.textContent).toContain(text);
    // A view switch must reconstruct the same warning from resource state.
    h.app.show("chats"); h.app.show("inbox"); await settle();
    expect(h.warning(resource)).not.toBeNull();
    h.gets[path] = previous;
    await load[resource](); await settle();
    expect(h.warning(resource)).toBeNull();
  });

  it("clears only the recovered resource when several lists fail", async () => {
    const h = await harness();
    h.gets["/m/api/tasks"] = failed;
    h.gets["/m/api/working"] = failed;
    await h.app.loadTasks(); await h.app.loadWorking(true); await h.app.loadMe(); await settle();
    expect(h.warning("tasks")).not.toBeNull();
    expect(h.warning("working")).not.toBeNull();
    h.gets["/m/api/tasks"] = ok({ tasks: [] });
    await h.app.loadTasks(); await settle();
    expect(h.warning("tasks")).toBeNull();
    expect(h.warning("working")).not.toBeNull();
    h.app.show("review"); await settle();
    expect(h.warning("working")).toBeNull(); // hidden resources do not claim this view is stale
  });

  it("retains the open chat log and clears its warning on an empty successful cursor read", async () => {
    const h = await harness();
    await h.app.openThread(T1);
    const path = `/m/api/threads/${T1}/log`, resource = `threads/${T1}/log`;
    h.gets[path] = failed;
    await h.app.moreLog(); await h.app.loadMe();
    await vi.advanceTimersByTimeAsync(12_001); await settle();
    expect(h.root.textContent).toContain("Old message");
    expect(h.warning(resource)?.textContent).toContain("showing the last read");
    h.gets[path] = ok({ rows: [], next: 1 });
    await h.app.moreLog(); await settle();
    expect(h.warning(resource)).toBeNull();
    expect(h.root.textContent).toContain("Old message");
  });

  it("another chat's successful read cannot clear this chat's warning", async () => {
    const h = await harness();
    await h.app.openThread(T1);
    h.gets[`/m/api/threads/${T1}/log`] = failed;
    await h.app.moreLog(); await h.app.loadMe(); await settle();
    await h.app.openThread(T2);
    expect(h.root.textContent).toContain("Other message");
    expect(h.warning(`threads/${T1}/log`)).toBeNull();
    await h.app.openThread(T1); await h.app.loadMe(); await settle();
    expect(h.warning(`threads/${T1}/log`)).not.toBeNull();
  });

  it("a first failed read says not read yet rather than claiming there is a last read", async () => {
    const h = await harness();
    h.gets[`/m/api/threads/${T2}/log`] = failed;
    await h.app.openThread(T2); await h.app.loadMe(); await settle();
    expect(h.warning(`threads/${T2}/log`)?.textContent).toContain("not read yet");
  });
});
