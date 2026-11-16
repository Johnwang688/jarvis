import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { T1, T2 } from "./threads.fixtures";

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
const line = (seq: number) => ({ seq, role: "Claude", text: `message ${seq}` });
const page = (start: number, end: number, older = false) => ok({
  rows: Array.from({ length: end - start + 1 }, (_, i) => line(start + i)), next: end + 1, older,
});
function deferred() {
  let resolve!: (got: Got) => void;
  const promise = new Promise<Got>(r => { resolve = r; });
  return { promise, resolve };
}
async function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const replies: Record<string, Got | Promise<Got>> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
    "/m/api/inbox": ok({ rows: [] }),
    "/m/api/working": ok({ entries: [], total: 0 }),
    "/m/api/tasks": ok({ tasks: [] }),
    "/m/api/review": ok({ v: 1, overnight: [], before: [], open: 0 }),
    "/m/api/projects": ok({ projects: [{ id: T2, name: "Project" }] }),
    "/m/api/threads": ok({ threads: [] }),
    [`/m/api/threads/${T1}/log`]: page(0, 59),
    "/m/api/thread-models": ok({ providers: {
      fast: { label: "Available provider", available: true, models: [{ id: "test-model", name: "Test model" }] },
    } }),
  };
  const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path] ?? replies[path.split("?")[0]] ?? ok({}));
  const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();
  return { app, root, replies, fetchJson };
}

describe("PR 384 independent counterexamples", () => {
  it("thread-models retains the last provider list and warns after a malformed 200", async () => {
    const h = await harness();
    await h.app.openNewChat();
    const providers = h.root.querySelector('[data-testid="m-new-provider"]')!.textContent;
    expect(providers).toContain("Available provider");
    h.app.goBack(); await settle();
    h.replies["/m/api/thread-models"] = ok({ providers: [] });
    await h.app.openNewChat();
    expect.soft(h.root.querySelector('[data-testid="m-new-provider"]')!.textContent,
      "a resource read is not a write acknowledgement, transport or session path").toBe(providers);
    expect.soft(h.root.textContent).toMatch(/could not|couldn't|incomplete/i);
  });

  it("a tail refresh racing load-more does not skip unread messages between the retained log and its tail", async () => {
    const h = await harness();
    const path = `/m/api/threads/${T1}/log`;
    await h.app.openThread(T1);
    const pending = deferred();
    h.replies[`${path}?after=60`] = pending.promise;
    const incremental = h.app.moreLog(); await settle();
    // The real API's default page is the latest LOG_TAIL=60 records. A busy chat
    // can gain >60 rows while a cursor page is in flight.
    h.replies[path] = page(120, 179, true);
    await h.app.openThread(T1);
    pending.resolve(page(60, 179));
    await incremental;
    // Ask again as a real resync would. The server returns nothing after 180.
    h.replies[`${path}?after=180`] = ok({ rows: [], next: 180 });
    h.replies[`${path}?after=60`] = page(60, 179);
    await h.app.moreLog();
    const seqs = h.app.thread!.rows.map(row => row.seq);
    // Replacing the view with a bounded latest tail (main's behavior) is fine.
    // Retaining the old prefix and silently splicing a disjoint tail onto it
    // makes an internally incomplete transcript look like one continuous log.
    expect(seqs, "the shown log must not have an internal hole at unread 60..119")
      .toEqual(Array.from({ length: 180 - seqs[0] }, (_, i) => seqs[0] + i));
  });

  it("a queued log refresh survives an obsolete incremental response", async () => {
    const h = await harness();
    const path = `/m/api/threads/${T1}/log`;
    await h.app.openThread(T1);
    const pending = deferred(); h.replies[`${path}?after=60`] = pending.promise;
    const incremental = h.app.moreLog(); await settle();
    h.replies[path] = page(1, 60, true);
    await h.app.openThread(T1);
    // A stream event arrives after the new opening's snapshot, while the old
    // incremental request still owns logBusy. This must run once it releases.
    h.replies[`${path}?after=61`] = page(61, 61);
    await h.app.moreLog();
    pending.resolve(page(60, 60)); await incremental; await settle();
    expect(h.app.thread?.rows.at(-1)?.seq).toBe(61);
    expect(h.fetchJson.mock.calls.map(call => call[0])).toContain(`${path}?after=61`);
  });

  it("an unanswered request for A does not indefinitely block incremental reads for B", async () => {
    const h = await harness();
    const pathA = `/m/api/threads/${T1}/log`, pathB = `/m/api/threads/${T2}/log`;
    await h.app.openThread(T1);
    const pending = deferred(); h.replies[`${pathA}?after=60`] = pending.promise;
    const readingA = h.app.moreLog(); await settle();
    h.replies[pathB] = page(0, 0);
    await h.app.openThread(T2);
    h.replies[`${pathB}?after=1`] = page(1, 1);
    await h.app.moreLog();
    // Give any timeout/retry in the app a chance to release the obsolete read.
    // No response for A is supplied until after the assertions are captured.
    await vi.advanceTimersByTimeAsync(120_000); await settle();
    const messages = h.app.thread?.rows.map(row => row.seq);
    const requests = h.fetchJson.mock.calls.map(call => call[0]);
    pending.resolve(page(60, 60)); await readingA;
    expect.soft(messages, "A's pending fetch owns the single logBusy flag for every chat").toEqual([0, 1]);
    expect.soft(requests).toContain(`${pathB}?after=1`);
  });
});
