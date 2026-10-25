// The inbox store (inbox A2): one read at a time, a read after the last event,
// the badge only when the count changes, and an older daemon is "unavailable".

import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { InboxStore } from "./inbox";

const row = (id: string, rank: number) => ({ id, kind: "question", rank, title: "t", blocks: "b", detail: {} });

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

describe("InboxStore", () => {
  it("reads, parses and tells its listeners", async () => {
    const store = new InboxStore({ fetch: async () => [row("q:a", 2), row("task:b", 8)], badge: () => {} });
    const heard = vi.fn();
    store.subscribe(heard);
    expect(store.snapshot().status).toBe("loading");
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "ready", reads: 1 });
    expect(store.snapshot().rows.map((r) => r.id)).toEqual(["q:a", "task:b"]);
    expect(heard).toHaveBeenCalled();
  });

  it("one read at a time; an event during a read asks for exactly one more after it", async () => {
    const reads: ReturnType<typeof deferred<unknown>>[] = [];
    const store = new InboxStore({ fetch: () => { const d = deferred<unknown>(); reads.push(d); return d.promise; },
      badge: () => {} });
    const first = store.refresh();
    store.refresh();
    store.refresh();
    expect(reads.length).toBe(1);
    reads[0].resolve([row("q:old", 2)]);
    await new Promise((r) => setTimeout(r, 0));
    expect(reads.length).toBe(2);            // the events heard meanwhile: one more read, not two
    reads[1].resolve([row("q:new", 2)]);
    await first;
    expect(reads.length).toBe(2);
    expect(store.snapshot().rows.map((r) => r.id)).toEqual(["q:new"]);
  });

  it("sets the badge to the count of ranks 1 to 5, only when it changes, and clears it at 0", async () => {
    let body: unknown[] = [row("appr:a", 1), row("prop:b", 3), row("task:c", 8)];
    const badge = vi.fn();
    const store = new InboxStore({ fetch: async () => body, badge });
    await store.refresh();
    expect(badge).toHaveBeenLastCalledWith(2);
    await store.refresh();
    expect(badge).toHaveBeenCalledTimes(1);
    body = [row("task:c", 8)];
    await store.refresh();
    expect(badge).toHaveBeenLastCalledWith(0);
    expect(badge).toHaveBeenCalledTimes(2);
  });

  it("an older daemon (404) or a refused page (403) is unavailable; a failure keeps the rows it had", async () => {
    let fail: unknown = new ApiError(404, "route not found");
    let ok = false;
    const store = new InboxStore({ fetch: async () => { if (ok) return [row("q:a", 2)]; throw fail; }, badge: () => {} });
    await store.refresh();
    expect(store.snapshot()).toMatchObject({ status: "unavailable", message: "route not found" });
    fail = new ApiError(403, "only the owner");
    await store.refresh();
    expect(store.snapshot().status).toBe("unavailable");
    ok = true;
    await store.refresh();
    ok = false;
    fail = new TypeError("Failed to fetch");
    await store.refresh();
    expect(store.snapshot().status).toBe("error");
    expect(store.snapshot().rows.map((r) => r.id)).toEqual(["q:a"]);
  });

  it("a body that is not a list is an error, never an empty inbox", async () => {
    const store = new InboxStore({ fetch: async () => ({ rows: [] }), badge: () => {} });
    await store.refresh();
    expect(store.snapshot().status).toBe("error");
    expect(store.snapshot().rows).toEqual([]);
  });

  it("a read that fails clears a badge it had set: unavailable, an error or a body that is not a list "
    + "(review of PR #305)", async () => {
    for (const failure of [() => { throw new ApiError(404, "route not found"); },
      () => { throw new TypeError("Failed to fetch"); }, () => ({ rows: [] })]) {
      let ok = true;
      const badge = vi.fn();
      const store = new InboxStore({ fetch: async () => (ok ? [row("appr:a", 1), row("q:b", 2)] : failure()), badge });
      await store.refresh();
      expect(badge).toHaveBeenLastCalledWith(2);
      ok = false;
      await store.refresh();
      expect(badge).toHaveBeenLastCalledWith(0);
      await store.refresh();
      expect(badge).toHaveBeenCalledTimes(2);       // cleared once, not again on every failed read
      ok = true;
      await store.refresh();
      expect(badge).toHaveBeenLastCalledWith(2);    // and back once a read works
    }
  });
});
