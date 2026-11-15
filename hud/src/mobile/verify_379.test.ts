import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";

// Independent app-level counterexamples; no private state injection.
class Stream implements StreamLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach(app => app.stop()); vi.useRealTimers(); });
const ok = (body: unknown): Got => ({ ok: true, body });
function harness() {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const stream = new Stream();
  const signedOut = vi.fn();
  const gets: Record<string, Got> = {
    "/m/api/me": ok({ signed_in: true, secure: { ok: true }, unlock_s: 1800 }),
    "/m/api/inbox": ok({ rows: [] }), "/m/api/tasks": ok({ tasks: [] }),
    "/m/api/review": ok({ open: 0 }), "/m/api/working": ok({ entries: [], total: 0, more: false }),
  };
  const posts: Record<string, Posted> = {};
  const app = new PhoneApp({ root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async path => gets[path] ?? ok({}),
    postJson: async path => posts[path] ?? { status: 200, body: {} },
    openStream: () => stream,
  });
  apps.push(app);
  const settle = async () => { for (let i = 0; i < 60; ++i) await Promise.resolve(); };
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  const word = () => q("m-conn").textContent;
  return { app, stream, signedOut, gets, posts, settle, q, word };
}

describe("PR 379 independent counterexamples", () => {
  it("a first stream that never opens becomes reconnecting after ten seconds", async () => {
    vi.useFakeTimers();
    const h = harness(); h.app.start(); await h.settle();
    expect(h.word()).toBe("Connecting…");
    await vi.advanceTimersByTimeAsync(10_001);
    expect(h.q("m-conn").dataset.state).toBe("reconnecting");
    expect(h.word()).toBe("Reconnecting…");
  });

  it("a 401 in the me read triggered by stream reopen ends the session", async () => {
    const h = harness(); h.app.start(); await h.settle();
    h.gets["/m/api/me"] = { ok: false, why: "signed-out", status: 401 };
    // The stream open handler starts loadMe, while all list reads still succeed.
    h.stream.open(); await h.settle();
    expect(h.word()).toBe("Signed out");
    expect(h.signedOut).toHaveBeenCalledOnce();
  });

  it.each([502, 503, 504])("a proxy %s on Lock now turns the orb red and preserves the truthful unlock pill", async (status) => {
    const h = harness(); h.app.start(); await h.settle(); h.stream.open(); await h.settle();
    expect(h.word()).toBe("Live");
    expect(h.q("m-lock-pill").dataset.state).toBe("unlocked");
    h.posts["/m/api/lock"] = { status, body: {} };
    h.q("m-lock-pill").click(); h.q("m-lock-now").click(); await h.settle();
    expect(h.q("m-lock-pill").dataset.state).toBe("unlocked");
    expect({ word: h.word(), tone: h.q("m-conn").dataset.tone })
      .toEqual({ word: "Jarvis unreachable", tone: "err" });
  });
});
