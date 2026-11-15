import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { mkRow } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage = null;
  onerror = null;
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach(app => app.stop()); vi.useRealTimers(); });
const settle = async () => { for (let i = 0; i < 120; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });

function harness() {
  vi.useFakeTimers();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const stream = new Stream();
  const signedOut = vi.fn();
  const gets: Record<string, Got> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true }, unlock_s: 1800 }),
    "/m/api/inbox": ok({ rows: [mkRow("q:old", 3, { title: "Old waiting question" })] }),
    "/m/api/tasks": ok({ tasks: [] }), "/m/api/review": ok({ open: 0 }),
    "/m/api/working": ok({ entries: [], total: 0, more: false }),
  };
  const posts: Record<string, Posted> = {};
  const app = new PhoneApp({ root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async path => gets[path] ?? ok({}),
    postJson: async path => posts[path] ?? { status: 200, body: {} },
    openStream: () => stream,
  });
  apps.push(app);
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  return { root, stream, gets, posts, app, signedOut, q };
}

describe("PR 379c: persistent route failure must retain a visible freshness warning", () => {
  it("a successful me cannot certify a still-failing visible Inbox as current", async () => {
    const h = harness(); h.app.start(); await settle(); h.stream.open(); await settle();
    expect(h.q("m-conn").textContent).toBe("Live");
    expect(h.root.textContent).toContain("Old waiting question");
    h.gets["/m/api/inbox"] = { ok: false, why: "offline", status: 504 };
    await h.app.loadInbox();
    expect(h.q("m-offline").hidden).toBe(false);
    // Same steady partial outage: the session and every other route still answer.
    await h.app.loadMe(); await settle();
    await vi.advanceTimersByTimeAsync(12_001); await settle();
    expect(h.root.textContent).toContain("Old waiting question");
    // Permit either a retained stale banner or a specific degraded/error notice. A "Live" orb can
    // coexist with a local warning; unrelated successes must not silently bless the old list.
    const hasFreshnessWarning = !h.q("m-offline").hidden
      || !!h.root.querySelector('[data-testid="m-list-error"]')
      || /degraded|inbox.*(?:unavailable|could not|stale)/i.test(h.q("m-conn").textContent ?? "");
    expect(hasFreshnessWarning).toBe(true);
  });
});

describe("PR 379c independent mutation controls and rule 9", () => {
  it("a daemon refusal leaves Live alone while an unworded proxy failure turns red", async () => {
    const h = harness(); h.app.start(); await settle(); h.stream.open(); await settle();
    h.gets["/m/api/working"] = { ok: false, why: "error", status: 503,
      body: { error: "the In progress list is not running" } };
    await h.app.loadWorking(true);
    expect(h.q("m-conn").textContent).toBe("Live");
    h.gets["/m/api/working"] = { ok: false, why: "offline", status: 503 };
    await h.app.loadWorking(true);
    expect(h.q("m-conn").textContent).toBe("Jarvis unreachable");
  });

  it("a phone without a network outranks Limited and preserves the unlock pill", async () => {
    const h = harness(); h.app.start(); await settle(); h.stream.open(); await settle();
    h.gets["/m/api/me"] = ok({ signed_in: true, secure: { ok: false }, unlock_s: 1800 });
    await h.app.loadMe();
    expect(h.q("m-conn").textContent).toBe("Limited connection");
    h.gets["/m/api/me"] = { ok: false, why: "offline", status: 0 };
    await h.app.loadMe();
    expect(h.q("m-conn").textContent).toMatch(/^Offline/);
    expect(h.q("m-lock-pill").dataset.state).toBe("unlocked");
  });

  it.each([false, true])("a 401 write asks me before deciding signed out (%s)", async expired => {
    const h = harness(); h.app.start(); await settle(); h.stream.open(); await settle();
    h.posts["/m/api/threads/aaaaaaaa/messages"] = { status: 401, body: { error: "unauthorized" } };
    if (expired) h.gets["/m/api/me"] = { ok: false, why: "signed-out", status: 401 };
    await h.app.sendMessage("aaaaaaaa", "hello"); await settle();
    expect(h.signedOut).toHaveBeenCalledTimes(expired ? 1 : 0);
    expect(h.q("m-conn").textContent).toBe(expired ? "Signed out" : "Live");
    expect(h.q("m-lock-pill").dataset.state).toBe("unlocked");
  });
});
