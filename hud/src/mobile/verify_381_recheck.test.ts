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

describe("PR 381 fresh recheck: malformed records are not a successful empty read", () => {
  it.each(["inbox", "working", "tasks", "review", "log"] as const)(
    "%s retains data and its warning when every returned record is invalid", async resource => {
      const h = await harness();
      h.replies["/m/api/inbox"] = ok({ rows: [{ id: "q:kept", kind: "question", rank: 3, title: "Retained question", detail: { text: "Choose a branch" } }] });
      h.replies["/m/api/tasks"] = ok({ tasks: [{ id: "11111111", brief: "Retained task", state: "running" }] });
      h.replies["/m/api/review"] = ok({ v: 1, overnight: [{ row: { id: "q:review", kind: "question", rank: 3, title: "Retained review question" } }], before: [], open: 1 });
      await h.app.loadInbox(); await h.app.loadTasks(); await h.app.loadReview();
      if (resource === "log") await h.app.openThread(T1);
      if (resource === "review") h.app.show("review");
      await settle();
      const name = resource === "log" ? `threads/${T1}/log` : resource;
      const path = `/m/api/${name}`;
      const load = { inbox: () => h.app.loadInbox(), working: () => h.app.loadWorking(true),
        tasks: () => h.app.loadTasks(), review: () => h.app.loadReview(), log: () => h.app.moreLog() }[resource];
      const retained = { inbox: "Retained question", working: "Retained running chat", tasks: "Retained task",
        review: "Retained review question", log: "First chat message" }[resource];
      expect(h.root.textContent).toContain(retained);
      h.replies[path] = failure;
      await load();
      expect(h.warning(name)?.textContent).toContain("showing the last read");
      const malformed = { inbox: { rows: [null] }, working: { entries: [null], total: 1 },
        tasks: { tasks: [null] }, review: { v: 1, overnight: [null], before: [], open: 1 },
        log: { rows: [null], next: 2 } }[resource];
      h.replies[path] = ok(malformed);
      await load(); await settle();
      expect.soft(h.warning(name), "invalid records cannot establish freshness").not.toBeNull();
      expect.soft(h.root.textContent, "a malformed response must keep the shown data").toContain(retained);
      if (resource === "log") expect.soft(h.app.thread?.next, "an unread record must not be skipped by advancing the cursor").toBe(1);
    });

  it("a first refused log read must not also announce that the chat has no messages", async () => {
    const h = await harness();
    h.replies[`/m/api/threads/${T2}/log`] = failure;
    await h.app.openThread(T2); await settle();
    expect(h.warning(`threads/${T2}/log`)?.textContent).toContain("not read yet");
    expect(h.root.querySelector('[data-testid="m-transcript"]')?.textContent).not.toContain("No messages yet");
  });
});
