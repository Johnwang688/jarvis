import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp } from "./app";
import { Drawer, drawerSwipe, type DrawerState } from "./drawer";
import { brandButton } from "./orb";
import { createProjectFolds } from "./folds";
import { mkRow } from "./threads.fixtures";
import type { Got } from "./net";

// Independent RD-6 verifier cases. No production code modifications.
const disposals: (() => void)[] = [];
afterEach(() => {
  disposals.splice(0).reverse().forEach((f) => f());
  document.body.replaceChildren(); localStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks();
});
const settle = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
async function appFixture(unlock = 1800) {
  vi.useFakeTimers();
  const root = document.createElement("main"); document.body.replaceChildren(root);
  const gets: string[] = [];
  const approval = mkRow("appr:test", 1, { kind: "approval", thread_id: "aaaaaaaa" });
  const app = new PhoneApp({ root, onSignedOut() {}, signOut: async () => {},
    fetchJson: async (path): Promise<Got> => {
      gets.push(path);
      const body = path === "/m/api/me" ? { signed_in: true, secure: { ok: true }, unlock_s: unlock }
        : path === "/m/api/inbox" ? { rows: [approval] }
        : path === "/m/api/projects" ? { projects: [{ id: "11111111", name: "Project" }] }
        : path === "/m/api/threads" ? { threads: [{ id: "aaaaaaaa", project_id: "11111111", title: "Chat", role: "chat", updated: "2026-11-15T10:00:00Z", provider: "claude", task_id: null }] }
        : path === "/m/api/working" ? { entries: [], more: false, total: 0 }
        : path === "/m/api/tasks" ? { tasks: [] }
        : path === "/m/api/review" ? { v: 1, open: 0, overnight: [], before: [], summary: {}, quiet: { enabled: true, now: false } }
        : { ops: [] };
      return { ok: true, body };
    },
    postJson: async () => ({ status: 200, body: { ok: true } }),
    openStream: () => ({ readyState: 1, onopen: null, onmessage: null, onerror: null, close() {} }),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  disposals.push(() => app.stop()); app.start(); await settle();
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  return { app, root, approval, gets, q };
}
function drawerFixture() {
  const host = document.createElement("div"), background = document.createElement("main"), opener = brandButton();
  background.append(opener); host.append(background); document.body.replaceChildren(host);
  const state: DrawerState = { section: "inbox", connection: { state: "live", tone: "ok", word: "Live" },
    lastRead: "10:00", projects: [{ id: "p", name: "Project", inbox: false }],
    threads: [{ id: "a", project_id: "p", title: "Deploy", updated: "2026-11-15T10:00:00Z", provider: "claude", running: false, queued: 0, turns: 1, task_id: null }],
    needs: new Set(["a"]), running: new Set(), count: 2, review: 0, unlock: "Locked", unlocked: false, lockNote: "" };
  const drawer = new Drawer(host, opener, createProjectFolds(null), () => state, { section() {}, thread() {}, lock() {} });
  disposals.push(() => drawer.stop());
  return { host, background, opener, state, drawer };
}
function touch(host: HTMLElement, type: string, x: number, y: number, cancelable = true) {
  const event = new Event(type, { bubbles: true, cancelable });
  const t = { clientX: x, clientY: y, identifier: 42 };
  Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : [t] }, changedTouches: { value: [t] } });
  host.dispatchEvent(event); return event;
}

describe("RD-6 independent counterexamples", () => {
  it("a sheet opened first refuses the drawer, including a pending edge gesture", async () => {
    const h = await appFixture(); h.app.openCard(h.approval); await settle();
    h.q("m-brand")!.click();
    const frame = h.root.querySelector<HTMLElement>(".app")!;
    touch(frame, "touchstart", 16, 200); touch(frame, "touchmove", 80, 201); touch(frame, "touchend", 80, 201);
    expect(h.q("m-drawer")).toBeNull(); expect(h.q("m-sheet")).not.toBeNull();
    h.app.goBack(); await settle();
    expect(h.q("m-main")!.hasAttribute("inert")).toBe(false);
  });

  it("a sheet opened second keeps the background inert when the drawer closes", async () => {
    const h = await appFixture(); h.q("m-brand")!.click();
    h.app.openCard(h.approval); await settle();
    // A modal coordinator may replace the drawer or stack above it. Either is valid.
    if (h.q("m-drawer")) h.q("m-drawer-close")!.click();
    expect(h.q("m-sheet")).not.toBeNull();
    expect(h.q("m-main")!.hasAttribute("inert")).toBe(true);
    expect(h.q("m-brand")!.closest("[inert]")).not.toBeNull();
  });

  it("announces what the amber project badge counts", () => {
    const h = drawerFixture(); h.drawer.open();
    const project = h.host.querySelector<HTMLElement>('[data-testid="m-drawer-project"]')!;
    // §6: phrase-form counts, as already implemented in Chats. "Project11" is ambiguous.
    expect(project.getAttribute("aria-label") ?? project.textContent).toMatch(/1\s+need(s)? you/i);
  });

  it.each(["needs", "running"])("%s chat status is available without seeing its colored dot", (status) => {
    const h = drawerFixture();
    if (status === "running") { h.state.needs = new Set(); h.state.running = new Set(["a"]); }
    h.drawer.open();
    const chat = h.host.querySelector<HTMLElement>('[data-testid="m-drawer-recent-a"]')!;
    const words = chat.getAttribute("aria-label") ?? chat.textContent;
    expect(words).toMatch(status === "needs" ? /needs? you/i : /working|running/i);
  });

  it("closing restores focus and adds no reads even after a background redraw", async () => {
    const h = await appFixture(); const before = [...h.gets];
    h.q("m-brand")!.click(); h.app.render();
    expect(h.q("m-main")!.hasAttribute("inert")).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(h.q("m-drawer")).toBeNull(); expect(document.activeElement).toBe(h.q("m-brand"));
    expect(h.gets).toEqual(before);
  });

  it("rejects vertical intent, center starts and the reserved native edge", () => {
    const host = document.createElement("div"); document.body.append(host);
    const begin = vi.fn(), end = vi.fn();
    disposals.push(drawerSwipe(host, "open", () => true, { begin, move() {}, end }));
    for (const x of [0, 3, 7, 21, 180]) {
      touch(host, "touchstart", x, 100); touch(host, "touchmove", x + 80, 101); touch(host, "touchend", x + 80, 101);
    }
    touch(host, "touchstart", 16, 100); touch(host, "touchmove", 17, 130);
    touch(host, "touchmove", 90, 130); touch(host, "touchend", 90, 130);
    expect(begin).not.toHaveBeenCalled(); expect(end).not.toHaveBeenCalled();
    touch(host, "touchstart", 16, 100); touch(host, "touchmove", 90, 101, false); touch(host, "touchend", 90, 101);
    expect(begin).not.toHaveBeenCalled();
  });
});
