// The working-set store (WS-2): one read at a time, exactly one more after an
// event heard mid-read, an error that keeps the last list, an older daemon
// that has no section, and the Done / Undo / 409 flows with the api mocked.

import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { WorkingStore, type WorkingDeps } from "./working";

const entry = (id: string, seq = 1, over: Record<string, unknown> = {}) => ({
  thread_id: id, project_id: "p1", since: "2026-11-14T10:00:00+00:00", last: "2026-11-14T10:00:00+00:00",
  seq, status: "idle", rows: [], why_not: null, ...over,
});
const body = (entries: unknown[] = [], needs_you: unknown[] = []) => ({ entries, needs_you, count: 0, pending: false });

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

const deps = (over: Partial<WorkingDeps> = {}): WorkingDeps => ({
  fetch: async () => body(), done: async () => ({}), undo: async () => ({}), ...over,
});

describe("WorkingStore reads", () => {
  it("reads, parses and tells its listeners", async () => {
    const store = new WorkingStore(deps({ fetch: async () => ({
      entries: [entry("t1", 3, { why_not: "a turn is running" })],
      needs_you: [{ id: "pr:a", kind: "pr", rank: 5, title: "PR 1" }], count: 1, pending: true }) }));
    const heard = vi.fn();
    store.subscribe(heard);
    expect(store.snapshot().status).toBe("loading");
    await store.refresh();
    const s = store.snapshot();
    expect(s).toMatchObject({ status: "ready", count: 1, pending: true });
    expect(s.entries).toHaveLength(1);
    expect(s.entries[0]).toMatchObject({ thread_id: "t1", seq: 3, why_not: "a turn is running" });
    expect(s.needsYou.map((r) => r.id)).toEqual(["pr:a"]);
    expect(heard).toHaveBeenCalled();
  });

  it("one read at a time; events heard during a read ask for exactly one more after it", async () => {
    const reads: ReturnType<typeof deferred<unknown>>[] = [];
    const store = new WorkingStore(deps({ fetch: () => { const d = deferred<unknown>(); reads.push(d); return d.promise; } }));
    const first = store.refresh();
    store.refresh();
    store.refresh();
    store.refresh();
    expect(reads.length).toBe(1);
    reads[0].resolve(body([entry("old")]));
    await new Promise((r) => setTimeout(r, 0));
    expect(reads.length).toBe(2);            // three events meanwhile: one more read, not three
    reads[1].resolve(body([entry("new")]));
    await first;
    expect(reads.length).toBe(2);
    expect(store.snapshot().entries.map((e) => e.thread_id)).toEqual(["new"]);
  });

  it("a read started after the last event is the last one drawn", async () => {
    const reads: ReturnType<typeof deferred<unknown>>[] = [];
    const store = new WorkingStore(deps({ fetch: () => { const d = deferred<unknown>(); reads.push(d); return d.promise; } }));
    const first = store.refresh();
    store.refresh();
    reads[0].resolve(body([entry("older")]));
    await new Promise((r) => setTimeout(r, 0));
    reads[1].resolve(body([entry("newer")]));
    await first;
    expect(store.snapshot().entries[0].thread_id).toBe("newer");
  });

  it("an older daemon (404) or a refused page (403) has no list; a failure keeps what was drawn", async () => {
    let fail: unknown = null;
    const store = new WorkingStore(deps({ fetch: async () => { if (fail) throw fail; return body([entry("t1")]); } }));
    await store.refresh();
    fail = new ApiError(500, "boom");
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "error", message: "boom" });
    expect(store.snapshot().entries).toHaveLength(1);
    fail = new ApiError(404, "route not found");
    await store.refresh();
    expect(store.snapshot().status).toBe("unavailable");
    expect(store.snapshot().entries).toEqual([]);
    fail = new ApiError(403, "only the owner");
    await store.refresh();
    expect(store.snapshot().status).toBe("unavailable");
    fail = new TypeError("Failed to fetch");
    await store.refresh();
    expect(store.snapshot().status).toBe("error");
  });

  it("an answer that is not a list is an error, never an empty set", async () => {
    const store = new WorkingStore(deps({ fetch: async () => ({ nope: 1 }) }));
    await store.refresh();
    expect(store.snapshot().status).toBe("error");
    expect(store.snapshot().entries).toEqual([]);
  });

  it("`loaded` is false until a read lands, whatever an earlier error was; a later error keeps it; a missing route drops it", async () => {
    let fail: unknown = new ApiError(500, "server error");
    const store = new WorkingStore(deps({ fetch: async () => { if (fail) throw fail; return body([entry("t1")]); } }));
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "error", loaded: false });
    fail = new Error("network down");
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "error", loaded: false });
    fail = null;
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "ready", loaded: true });
    fail = new Error("network down");
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "error", loaded: true });
    fail = new ApiError(404, "route not found");
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "unavailable", loaded: false });
  });
});

describe("WorkingStore publishing", () => {
  it("a read that finds the list as it was publishes nothing; a changed one does", async () => {
    let list = [entry("t1")];
    const store = new WorkingStore(deps({ fetch: async () => body(list) }));
    const heard = vi.fn();
    await store.refresh();
    store.subscribe(heard);
    const before = store.snapshot();
    await store.refresh();
    await store.refresh();
    await store.refresh();
    expect(heard).not.toHaveBeenCalled();
    expect(store.snapshot()).toBe(before);
    list = [entry("t1", 2)];
    await store.refresh();
    expect(heard).toHaveBeenCalledTimes(1);
  });
});

describe("WorkingStore Done and Undo", () => {
  it("Done posts the seq the entry was drawn with, then reads again", async () => {
    const done = vi.fn(async () => ({ thread_id: "t1", done: true, token: "tok123", undo_s: 20 }));
    const fetch = vi.fn(async () => body([entry("t1", 7)]));
    const store = new WorkingStore(deps({ fetch, done }));
    await store.refresh();
    const r = await store.done("t1");
    expect(done).toHaveBeenCalledWith("t1", 7);
    expect(r).toEqual({ kind: "done", token: "tok123" });
    await store.refresh();
    expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("Done on a thread the list does not hold posts nothing", async () => {
    const done = vi.fn();
    const store = new WorkingStore(deps({ done }));
    await store.refresh();
    expect(await store.done("nope")).toEqual({ kind: "gone" });
    expect(done).not.toHaveBeenCalled();
  });

  it("a 409 is silent: it reads again, and is not an error", async () => {
    const fetch = vi.fn(async () => body([entry("t1", 2)]));
    const done = vi.fn(async () => { throw new ApiError(409, "it moved on", { error: "it moved on", entry: {} }); });
    const store = new WorkingStore(deps({ fetch, done }));
    await store.refresh();
    const before = fetch.mock.calls.length;
    const r = await store.done("t1");
    expect(r).toEqual({ kind: "stale" });
    await store.refresh();
    expect(fetch.mock.calls.length).toBeGreaterThan(before);
  });

  it("any other failure is a short message, not a re-read", async () => {
    const fetch = vi.fn(async () => body([entry("t1")]));
    const store = new WorkingStore(deps({ fetch, done: async () => { throw new ApiError(503, "could not save"); } }));
    await store.refresh();
    expect(await store.done("t1")).toEqual({ kind: "error", message: "could not save" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("Undo posts the token it was given, then reads again; a 409 is stale and silent", async () => {
    const undo = vi.fn(async () => ({}));
    const fetch = vi.fn(async () => body([]));
    const store = new WorkingStore(deps({ fetch, undo }));
    expect(await store.undo("t1", "tok")).toEqual({ kind: "undone" });
    expect(undo).toHaveBeenCalledWith("t1", "tok");
    await store.refresh();
    expect(fetch).toHaveBeenCalled();
    const late = new WorkingStore(deps({ undo: async () => { throw new ApiError(409, "late"); } }));
    expect(await late.undo("t1", "tok")).toEqual({ kind: "stale" });
    const bad = new WorkingStore(deps({ undo: async () => { throw new ApiError(500, "down"); } }));
    expect(await bad.undo("t1", "tok")).toEqual({ kind: "error", message: "down" });
  });
});
