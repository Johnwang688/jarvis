import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";

class Stream implements StreamLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage = null;
  onerror = null;
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach((app) => app.stop()); vi.useRealTimers(); });

function harness() {
  vi.useFakeTimers();
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const streams: Stream[] = [];
  const deadlines: (() => void)[] = [];
  const clearTimer = vi.fn((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const signedOut = vi.fn();
  const posts: Record<string, Posted> = {};
  const me = { value: { ok: true, body: { signed_in: true, secure: { ok: true }, unlock_s: 1800 } } as Got };
  const app = new PhoneApp({ root, onSignedOut: signedOut, signOut: async () => {},
    postJson: async (path) => posts[path] ?? { status: 200, body: {} },
    fetchJson: async (path) => path === "/m/api/me" ? me.value : { ok: true, body: {
      rows: [], tasks: [], open: 0, entries: [], total: 0, more: false } },
    openStream: () => { const stream = new Stream(); streams.push(stream); return stream; },
    setTimer: (fn, ms) => { if (ms === 10_000) deadlines.push(fn); return setTimeout(fn, ms); },
    clearTimer,
  });
  apps.push(app);
  const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
  const word = () => root.querySelector('[data-testid="m-conn"]')!.textContent;
  const pill = () => root.querySelector<HTMLElement>('[data-testid="m-lock-pill"]')!;
  return { app, streams, deadlines, clearTimer, me, posts, signedOut, settle, word, pill, root };
}

describe("orb first-open deadline and session lifecycle", () => {
  it("uses the injected timer at ten seconds without claiming the stream ever opened", async () => {
    const h = harness(); h.app.start(); await h.settle();
    expect(h.deadlines).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(9999);
    expect(h.word()).toBe("Connecting…");
    await vi.advanceTimersByTimeAsync(1);
    expect(h.word()).toBe("Reconnecting…");
    expect(h.app.everOpened).toBe(false);
    h.streams[0].open(); await h.settle();
    expect(h.word()).toBe("Live");
    expect(h.app.everOpened).toBe(true);
  });

  it("keeps offline ahead of reconnecting when the first-open deadline expires", async () => {
    const h = harness(); h.app.start(); await h.settle();
    h.app.setOnline(false, 0);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.word()).toMatch(/^Offline/);
  });

  it("clears the deadline on open and ignores an already queued callback", async () => {
    const h = harness(); h.app.start(); await h.settle();
    const deadline = h.deadlines[0];
    h.streams[0].open(); await h.settle();
    expect(h.clearTimer).toHaveBeenCalledOnce();
    deadline();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.word()).toBe("Live");
  });

  it("keeps the launch deadline across stream replacements and ignores obsolete opens", async () => {
    const h = harness(); h.app.start(); await h.settle();
    await vi.advanceTimersByTimeAsync(6000);
    h.app.connect();
    expect(h.deadlines).toHaveLength(1);
    h.streams[0].open();
    expect(h.word()).toBe("Connecting…");
    await vi.advanceTimersByTimeAsync(4000);
    expect(h.word()).toBe("Reconnecting…");
    expect(h.app.everOpened).toBe(false);
  });

  it("clears on stop and ignores an already queued deadline callback", async () => {
    const h = harness(); h.app.start(); await h.settle();
    h.app.stop();
    expect(h.clearTimer).toHaveBeenCalledOnce();
    h.deadlines[0]();
    expect(h.word()).toBe("Connecting…");
  });

  it.each([502, 503, 504])("records a Lock now %s without relocking the pill; a later network loss is amber", async (status) => {
    const h = harness(); h.app.start(); await h.settle();
    h.streams[0].open(); await h.settle();
    h.posts["/m/api/lock"] = { status, body: {} };
    h.pill().click();
    h.root.querySelector<HTMLElement>('[data-testid="m-lock-now"]')!.click(); await h.settle();
    expect(h.app.failStatus).toBe(status);
    expect(h.app.online).toBe(false);
    expect(h.word()).toBe("Jarvis unreachable");
    expect(h.pill().dataset.state).toBe("unlocked");
    h.posts["/m/api/lock"] = { status: 0, body: {}, offline: true };
    await h.app.unlock.lockNow();
    expect(h.app.failStatus).toBe(0);
    expect(h.word()).toMatch(/^Offline/);
    expect(h.pill().dataset.state).toBe("unlocked");
  });

  it("ends the session exactly once on a stream-open me 401", async () => {
    const h = harness(); h.app.start(); await h.settle();
    h.me.value = { ok: false, why: "signed-out", status: 401 };
    h.streams[0].open(); await h.settle();
    expect(h.word()).toBe("Signed out");
    expect(h.signedOut).toHaveBeenCalledOnce();
    expect(h.streams[0].readyState).toBe(2);
    await h.app.loadMe();
    h.deadlines[0]();
    expect(h.signedOut).toHaveBeenCalledOnce();
    expect(h.word()).toBe("Signed out");
  });
});
