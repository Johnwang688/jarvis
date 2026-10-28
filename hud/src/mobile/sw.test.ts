// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The service worker (./sw.ts) imported into a fake worker scope: its own
// handlers, run as the browser would run them.
type Handler = (event: unknown) => void;

function scope(clients: { url: string; focus: ReturnType<typeof vi.fn>; postMessage: ReturnType<typeof vi.fn> }[] = []) {
  const handlers = new Map<string, Handler>();
  const shown: { title: string; options: Record<string, unknown> }[] = [];
  const opened: string[] = [];
  return {
    handlers, shown, opened,
    self: {
      addEventListener: (type: string, fn: Handler) => handlers.set(type, fn),
      skipWaiting: vi.fn(async () => {}),
      registration: { showNotification: vi.fn(async (title: string, options: Record<string, unknown>) => { shown.push({ title, options }); }) },
      clients: {
        claim: vi.fn(async () => {}),
        matchAll: vi.fn(async () => clients),
        openWindow: vi.fn(async (url: string) => { opened.push(url); }),
      },
      location: { origin: "https://jarvis-box.tail-test.ts.net" },
    },
  };
}

async function load(s: ReturnType<typeof scope>) {
  vi.stubGlobal("self", s.self);
  vi.resetModules();
  await import("./sw");
  return s;
}

async function fire(s: ReturnType<typeof scope>, type: string, event: Record<string, unknown>) {
  const waits: Promise<unknown>[] = [];
  s.handlers.get(type)!({ ...event, waitUntil: (p: Promise<unknown>) => waits.push(p) });
  await Promise.all(waits);
}

function push(data: unknown) {
  return { data: data === undefined ? null : { json: () => (typeof data === "string" ? JSON.parse(data) : data), text: () => "" } };
}

describe("the phone's service worker (P-6)", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("registers install, activate, push and notificationclick, and nothing else (no fetch, no cache)", async () => {
    const s = await load(scope());
    expect([...s.handlers.keys()].sort()).toEqual(["activate", "install", "notificationclick", "push"]);
  });

  it("shows 'Jarvis' with 'n waiting', and the rank-1 line", async () => {
    const s = await load(scope());
    await fire(s, "push", push({ count: 2, rank: 3 }));
    await fire(s, "push", push({ count: 1, rank: 1 }));
    await fire(s, "push", push({ count: 0, rank: 0 }));
    expect(s.shown.map((n) => [n.title, n.options.body])).toEqual([
      ["Jarvis", "2 waiting"], ["Jarvis", "1 waiting - an approval expires soon"], ["Jarvis", "Nothing is waiting now"]]);
    expect(s.shown[0].options.tag).toBe("jarvis-inbox");
    expect(s.shown[0].options.icon).toBe("/m/icons/orb-192.png");
  });

  it("shows only its own words, whatever a push carries", async () => {
    const s = await load(scope());
    await fire(s, "push", push({ count: 1, rank: 1, title: "rm -rf /", text: "<b>x</b>" }));
    await fire(s, "push", push({ count: "<img>", rank: 1 }));
    await fire(s, "push", push(undefined));
    await fire(s, "push", { data: { json: () => { throw new Error("bad"); }, text: () => "" } });
    expect(s.shown.map((n) => n.options.body)).toEqual(["1 waiting - an approval expires soon",
      "Something is waiting", "Something is waiting", "Something is waiting"]);
    for (const n of s.shown) expect(Object.keys(n.options).sort()).toEqual(["body", "icon", "renotify", "tag"]);
  });

  it("a tap focuses the open app on the Inbox", async () => {
    const app = { url: "https://jarvis-box.tail-test.ts.net/m/", focus: vi.fn(async () => {}), postMessage: vi.fn() };
    const other = { url: "https://jarvis-box.tail-test.ts.net/elsewhere", focus: vi.fn(async () => {}), postMessage: vi.fn() };
    const s = await load(scope([other, app]));
    const close = vi.fn();
    await fire(s, "notificationclick", { notification: { close } });
    expect(close).toHaveBeenCalled();
    expect(app.postMessage).toHaveBeenCalledWith({ kind: "open-inbox" });
    expect(app.focus).toHaveBeenCalled();
    expect(other.focus).not.toHaveBeenCalled();
    expect(s.opened).toEqual([]);
  });

  it("a tap with no app open opens /m/ on the Inbox", async () => {
    const s = await load(scope());
    await fire(s, "notificationclick", { notification: { close: vi.fn() } });
    expect(s.opened).toEqual(["/m/#inbox"]);
  });
});
