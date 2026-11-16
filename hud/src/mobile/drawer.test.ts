import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { Drawer, drawerSwipe, type DrawerState } from "./drawer";
import { brandButton } from "./orb";
import { createProjectFolds } from "./folds";
import { chatsView } from "./views";
import { CODE_SVG } from "./icons";
import { PhoneApp } from "./app";
import { mkRow } from "./threads.fixtures";
import type { Got } from "./net";
import type { ChatThread } from "./model";
const thread = (id: string, updated = "2026-11-15T10:00:00Z"): ChatThread =>
  ({ id, project_id: "p1", updated, title: "Chat " + id, provider: "claude", running: false, queued: 0, turns: 1, task_id: null });
const base = (): DrawerState => ({ section: "inbox", connection: { state: "live", tone: "ok", word: "Live" },
  lastRead: "10:00 AM", projects: [{ id: "p1", name: "Jarvis", inbox: false }], threads: [thread("aaaaaaaa")],
  needs: new Set(["aaaaaaaa"]), running: new Set(), count: 2, review: 3, unlock: "Locked", unlocked: false, lockNote: "" });
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach((f) => f()); document.body.replaceChildren(); localStorage.clear(); vi.restoreAllMocks(); });
function fixture(state = base()) {
  const host = document.createElement("div"), behind = document.createElement("main"), opener = brandButton();
  behind.append(opener); host.append(behind); document.body.replaceChildren(host);
  const folds = createProjectFolds(null), actions = { section: vi.fn(), thread: vi.fn(), lock: vi.fn() };
  const drawer = new Drawer(host, opener, folds, () => state, actions);
  opener.onclick = () => drawer.open(); cleanup.push(() => drawer.stop());
  const q = (id: string) => host.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  return { host, behind, opener, drawer, state, folds, actions, q };
}
function touch(host: HTMLElement, type: string, x: number, y: number, count = 1, cancelable = true) {
  const event = new Event(type, { bubbles: true, cancelable }), t = { clientX: x, clientY: y, identifier: 1 };
  Object.defineProperties(event, { touches: { value: type === "touchend" ? [] : Array(count).fill(t) }, changedTouches: { value: [t] } });
  host.dispatchEvent(event); return event;
}
const key = (key: string, shiftKey = false) => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true }));

describe("modal drawer", () => {
  it("brand opens a named modal, inerts background and focuses the first row", () => {
    const h = fixture(); h.opener.click();
    expect(h.q("m-drawer").getAttribute("role")).toBe("dialog");
    expect(h.q("m-drawer").getAttribute("aria-modal")).toBe("true");
    expect(h.q("m-drawer").getAttribute("aria-label")).toBe("Navigation");
    expect(h.behind.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(h.q("m-drawer-inbox"));
  });
  it.each(["Escape", "m-drawer-dim", "m-drawer-close", "swipe"])("%s closes and returns focus", (method) => {
    const h = fixture(); h.opener.click();
    if (method === "Escape") key("Escape");
    else if (method === "swipe") {
      const p = h.q("m-drawer"); touch(p, "touchstart", 200, 100); touch(p, "touchmove", 140, 101); touch(p, "touchend", 140, 101);
    } else h.q(method).click();
    expect(h.drawer.isOpen).toBe(false); expect(h.behind.hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(h.opener); expect(h.opener.getAttribute("aria-expanded")).toBe("false");
  });
  it("traps Tab in both directions and redirects programmatic focus", () => {
    const h = fixture(); h.drawer.open(); h.q("m-drawer-close").focus(); key("Tab", true);
    expect(document.activeElement).toBe(h.q("m-drawer-lock-now")); key("Tab");
    expect(document.activeElement).toBe(h.q("m-drawer-close")); h.opener.focus();
    expect(document.activeElement).toBe(h.q("m-drawer-inbox"));
  });
  it("preserves prior inert and cleans listeners on stop", () => {
    const h = fixture(); h.behind.setAttribute("inert", ""); h.drawer.open(); h.drawer.stop();
    expect(h.behind.hasAttribute("inert")).toBe(true); h.behind.removeAttribute("inert");
    h.opener.focus(); expect(document.activeElement).toBe(h.opener);
  });
  it.each(["inbox", "chats", "review", "tasks", "settings"] as const)("%s navigates and closes", (section) => {
    const h = fixture(); h.drawer.open(); h.q("m-drawer-" + section).click();
    expect(h.actions.section).toHaveBeenCalledExactlyOnceWith(section); expect(h.drawer.isOpen).toBe(false);
  });
  it("project and recent chats navigate and close", () => {
    const h = fixture();
    for (const id of ["project-chat-aaaaaaaa", "recent-aaaaaaaa"]) {
      h.drawer.open(); h.q("m-drawer-" + id).click();
      expect(h.actions.thread).toHaveBeenLastCalledWith(h.state.threads[0]); expect(h.drawer.isOpen).toBe(false);
    }
  });
  it("shares explicit folds and activity defaults with Chats in both directions", () => {
    const h = fixture(); h.drawer.open();
    const chats = () => chatsView(h.state.projects, h.state.threads, new Set(), {
      open: () => {}, toggle: () => {}, fold: (id, folded) => h.folds.set(id, folded),
    }, { folds: h.folds, needs: h.state.needs, running: h.state.running });
    expect(h.q("m-drawer-project").getAttribute("aria-expanded")).toBe("true"); h.q("m-drawer-project").click();
    expect(h.q("m-drawer-project").getAttribute("aria-expanded")).toBe("false");
    expect(chats().querySelector("[data-testid=m-project-head]")!.getAttribute("aria-expanded")).toBe("false");
    (chats().querySelector("[data-testid=m-project-head]") as HTMLElement).click();
    expect(h.q("m-drawer-project").getAttribute("aria-expanded")).toBe("true");
  });
  it("updates live counts, lock and section, keeping focus, scroll and inert", () => {
    const h = fixture(); h.drawer.open(); h.q("m-drawer-project").focus();
    h.host.querySelector(".m-drawer-body")!.scrollTop = 123;
    h.state.count = 9; h.state.review = 0; h.state.unlocked = true; h.state.unlock = "Unlocked until 14:32"; h.state.section = "chats";
    h.behind.removeAttribute("inert"); h.drawer.refresh();
    expect(h.q("m-drawer-inbox").textContent).toBe("Inbox9"); expect(h.q("m-drawer-review")).toBeNull();
    expect(h.q("m-drawer-chats").getAttribute("aria-current")).toBe("page");
    expect(h.q("m-drawer-lock").textContent).toBe("Unlocked until 14:32");
    expect(document.activeElement).toBe(h.q("m-drawer-project"));
    expect(h.host.querySelector(".m-drawer-body")!.scrollTop).toBe(123); expect(h.behind.hasAttribute("inert")).toBe(true);
    h.q("m-drawer-lock-now").click(); expect(h.actions.lock).toHaveBeenCalledOnce(); expect(h.drawer.isOpen).toBe(false);
  });
  it("caps recent lists at five newest chats and excludes tasks", () => {
    const s = base(); s.threads = Array.from({ length: 8 }, (_, i) => thread(String(i), `2026-11-${10 + i}T10:00:00Z`));
    s.threads.push({ ...thread("task"), task_id: "task" }); const h = fixture(s); h.folds.set("p1", false); h.drawer.open();
    expect(Array.from(h.host.querySelectorAll(".m-drawer-body > .m-drawer-chat")).map((n) => (n as HTMLElement).dataset.id)).toEqual(["7", "6", "5", "4", "3"]);
    expect(h.host.querySelectorAll(".m-drawer-project-body > button").length).toBe(5);
  });
  it("hostile names, ids, connection and lock text stay text; only code icons exist", () => {
    const evil = '<svg class="m-icon" aria-hidden="true" onload="bad()"><path/></svg><img src=x>';
    const s = base(); s.projects![0] = { id: evil, name: evil, inbox: false }; s.threads[0].title = evil; s.threads[0].project_id = evil;
    s.connection.word = evil; s.unlock = evil; s.lockNote = evil; const h = fixture(s); h.drawer.open();
    expect(h.q("m-drawer").textContent).toContain(evil); expect(h.host.querySelectorAll("img, script, iframe").length).toBe(0);
    expect(Array.from(h.host.querySelectorAll("svg")).every((n) => CODE_SVG.has(n))).toBe(true);
  });
  it("shows an honest unread hint", () => {
    const s = base(); s.projects = null; s.threads = []; const h = fixture(s); h.drawer.open();
    expect(h.q("m-drawer").textContent).toContain("Open Chats to read projects.");
  });
  it("follows the finger without inline style and skips motion when reduced", () => {
    const h = fixture(); h.drawer.open();
    const animation = { pause: vi.fn(), cancel: vi.fn(), currentTime: 0 };
    const animate = vi.fn(() => animation);
    Object.defineProperty(h.q("m-drawer"), "animate", { value: animate });
    vi.spyOn(h.q("m-drawer"), "getBoundingClientRect").mockReturnValue({ width: 200 } as DOMRect);
    const media = vi.fn(() => ({ matches: false }));
    vi.stubGlobal("matchMedia", media);
    h.drawer.follow(100); expect(animation.currentTime).toBe(500); expect(animation.pause).toHaveBeenCalledOnce();
    expect(h.q("m-drawer").hasAttribute("style")).toBe(false);
    h.drawer.finishOpening(true); expect(animation.cancel).toHaveBeenCalledOnce();
    media.mockReturnValue({ matches: true }); h.drawer.follow(100); expect(animate).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });
  it("has reduced motion and 48px target CSS", () => {
    const css = readFileSync(__dirname + "/nav.css", "utf8");
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.m-drawer \{ animation: none/);
    expect(css).toMatch(/\.tap\.m-drawer-row[^}]*min-height: var\(--m-tap\)/);
    expect(css).toMatch(/\.tap\.m-drawer-close[^}]*min-height: var\(--m-tap\)/);
  });
});
describe("edge gesture", () => {
  function gesture(enabled = true) {
    const host = document.createElement("div"); document.body.append(host);
    const actions = { begin: vi.fn(), move: vi.fn(), end: vi.fn() };
    cleanup.push(drawerSwipe(host, "open", () => enabled, actions)); return { host, actions };
  }
  it.each([0, 5, 21, 100, 300])("rejects opening at x=%s", (x) => {
    const h = gesture(); touch(h.host, "touchstart", x, 100); touch(h.host, "touchmove", x + 60, 100); touch(h.host, "touchend", x + 60, 100);
    expect(h.actions.begin).not.toHaveBeenCalled();
  });
  it("follows horizontal intent, commits at 40px, leaves pending touches uncanceled", () => {
    const h = gesture(); touch(h.host, "touchstart", 20, 100);
    expect(touch(h.host, "touchmove", 24, 101).defaultPrevented).toBe(false);
    expect(touch(h.host, "touchmove", 60, 102).defaultPrevented).toBe(true); touch(h.host, "touchend", 60, 102);
    expect(h.actions.begin).toHaveBeenCalledOnce(); expect(h.actions.move).toHaveBeenCalledWith(40); expect(h.actions.end).toHaveBeenCalledWith(true);
  });
  it("locks out vertical scrolling even when it later turns horizontal", () => {
    const h = gesture(); touch(h.host, "touchstart", 16, 100); touch(h.host, "touchmove", 18, 120);
    touch(h.host, "touchmove", 100, 120); touch(h.host, "touchend", 100, 120); expect(h.actions.begin).not.toHaveBeenCalled();
  });
  it.each(["short", "cancel", "multitouch", "native"])("cancels %s gestures", (method) => {
    const h = gesture(); touch(h.host, "touchstart", 16, 100); touch(h.host, "touchmove", 40, 100, 1, method !== "native");
    if (method === "multitouch") touch(h.host, "touchmove", 80, 100, 2);
    else touch(h.host, method === "cancel" ? "touchcancel" : "touchend", 40, 100);
    expect(h.actions.end).not.toHaveBeenCalledWith(true);
    if (method !== "native") expect(h.actions.end).toHaveBeenCalledWith(false);
  });
  it("is disabled over nested screens and overlays", () => {
    const h = gesture(false); touch(h.host, "touchstart", 16, 100); touch(h.host, "touchmove", 100, 100); touch(h.host, "touchend", 100, 100);
    expect(h.actions.begin).not.toHaveBeenCalled();
  });
});
async function appFixture() {
  const root = document.createElement("div"); document.body.replaceChildren(root);
  const gets: string[] = [], posts: string[] = []; let rows: unknown[] = [], unlock = 1800;
  const app = new PhoneApp({ root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path): Promise<Got> => {
      gets.push(path);
      const body = path === "/m/api/me" ? { signed_in: true, secure: { ok: true }, unlock_s: unlock }
        : path === "/m/api/inbox" ? { rows } : path === "/m/api/projects" ? { projects: [{ id: "11111111", name: "Jarvis" }] }
        : path === "/m/api/threads" ? { threads: [{ ...thread("aaaaaaaa"), project_id: "11111111" }] } : path === "/m/api/tasks" ? { tasks: [] }
        : path === "/m/api/working" ? { entries: [], more: false, total: 0 } : path === "/m/api/review" ? { open: 0 } : { ops: [] };
      return { ok: true, body };
    }, postJson: async (path) => { posts.push(path); if (path === "/m/api/lock") unlock = 0; return { status: 200, body: { ok: true } }; },
    openStream: () => ({ readyState: 1, onopen: null, onmessage: null, onerror: null, close() {} }),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  cleanup.push(() => app.stop()); app.start();
  const settle = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); }; await settle();
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  return { app, root, q, gets, posts, settle, setRows: (next: unknown[]) => { rows = next; } };
}
describe("app wiring", () => {
  it("opening/folding/closing adds no reads; existing refreshes keep counts live and background inert", async () => {
    const h = await appFixture(); await h.app.loadChats(); const before = [...h.gets];
    h.q("m-brand").click(); h.q("m-drawer-project").click(); expect(h.gets).toEqual(before);
    h.setRows([mkRow("q:1", 2, { thread_id: "aaaaaaaa", detail: { text: "Which?", options: [] } })]);
    await h.app.loadInbox(); await h.settle(); expect(h.q("m-drawer-inbox").textContent).toBe("Inbox1");
    for (const selector of [".top", ".main", ".tabs"]) expect(h.root.querySelector(selector)!.hasAttribute("inert")).toBe(true);
    key("Escape"); expect(document.activeElement).toBe(h.q("m-brand"));
  });
  it("uses the existing Lock now route and truthful mirror", async () => {
    const h = await appFixture(); h.q("m-brand").click(); expect(h.q("m-drawer-lock").textContent).toContain("Unlocked until");
    h.q("m-drawer-lock-now").click(); await h.settle(); expect(h.posts).toEqual(["/m/api/lock"]); expect(h.q("m-drawer")).toBeNull();
    h.q("m-brand").click(); expect(h.q("m-drawer-lock").textContent).toBe("Locked");
  });
  it("Tasks opens the Inbox filter; edge swipes cannot open above a chat", async () => {
    const h = await appFixture(); h.q("m-brand").click(); h.q("m-drawer-tasks").click(); await h.settle();
    expect([h.app.tab, h.app.filter]).toEqual(["inbox", "tasks"]);
    h.q("m-brand").click(); h.q("m-drawer-settings").click();
    h.q("m-brand").click(); h.q("m-drawer-inbox").click(); expect(h.app.filter).toBeNull();
    await h.app.openThread("aaaaaaaa");
    const frame = h.root.querySelector(".app")! as HTMLElement;
    touch(frame, "touchstart", 16, 200); touch(frame, "touchmove", 100, 200); touch(frame, "touchend", 100, 200); expect(h.q("m-drawer")).toBeNull();
    h.q("m-brand").click(); expect(h.q("m-drawer")).not.toBeNull(); key("Escape");
    h.app.show("inbox"); h.q("m-brand").click(); h.q("m-drawer-chats").click();
    expect(h.app.thread).toBeNull(); expect(h.app.tab).toBe("chats");
  });
});
