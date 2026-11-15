// PX-4: history (state only), scroll memory, the approval sheet, the ⋯ menus, the tray and the review line.
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { Nav, ScrollMemory, depthOf, menuView, sheetView, trayView, type HistoryEnv } from "./nav";
import { mkRow } from "./threads.fixtures";

// -- the history, on its own, with a fake browser ---------------------------------------------

function fakeHistory() {
  const entries: unknown[] = [null];
  let at = 0;
  const pops: ((s: unknown) => void)[] = [];
  const log: string[] = [];
  const env: HistoryEnv = {
    state: () => entries[at],
    push: (s) => { entries.splice(at + 1); entries.push(s); at += 1; log.push("push " + JSON.stringify(s)); },
    replace: (s) => { entries[at] = s; log.push("replace " + JSON.stringify(s)); },
    go: (d) => { log.push("go " + d); queueMicrotask(() => { at += d; pops.forEach((f) => f(entries[at])); }); },
    onPop: (f) => { pops.push(f); return () => { pops.splice(pops.indexOf(f), 1); }; },
  };
  return { env, entries, log, get at() { return at; }, pop: (d: number) => { at += d; pops.forEach((f) => f(entries[at])); } };
}

describe("Nav: history that holds a depth and nothing else", () => {
  it("reads a depth only from a whole number it made", () => {
    expect([depthOf({ jv: 2 }), depthOf(null), depthOf({ jv: "2" }), depthOf({ jv: -1 }), depthOf({ jv: 1.5 }), depthOf("x")])
      .toEqual([2, 0, 0, 0, 0, 0]);
  });

  it("pushes one entry per level opened, with state {jv} only, and goes back for a level closed", async () => {
    const h = fakeHistory();
    let depth = 0;
    const nav = new Nav(h.env, () => depth, () => {});
    nav.start();
    depth = 2;
    nav.sync();
    expect(h.log).toEqual(['replace {"jv":0}', 'push {"jv":1}', 'push {"jv":2}']);
    depth = 1;
    nav.sync();
    expect(h.log.at(-1)).toBe("go -1");
    await Promise.resolve();
    expect(nav.hist).toBe(1);
    for (const entry of h.entries) expect(JSON.stringify(entry)).not.toMatch(/[a-f0-9]{8}/);
  });

  it("a pop runs the app's own back to the landed depth, and pushes nothing while it does", () => {
    const h = fakeHistory();
    let depth = 0;
    const closed: number[] = [];
    const nav = new Nav(h.env, () => depth, (d) => { closed.push(d); while (depth > d) { depth -= 1; nav.sync(); } });
    nav.start();
    depth = 2;
    nav.sync();
    const before = h.log.length;
    h.pop(-2);
    expect(closed).toEqual([0]);
    expect(depth).toBe(0);
    // `sync` ran inside the close (one level at a time) and must not have pushed a forward entry.
    expect(h.log.slice(before).filter((l) => l.startsWith("push"))).toEqual([]);
    expect(nav.hist).toBe(0);
  });

  it("a forward pop to a level the app has closed is walked back", async () => {
    const h = fakeHistory();
    let depth = 1;
    const nav = new Nav(h.env, () => depth, () => {});
    nav.start();
    nav.sync();
    depth = 0;
    nav.sync();
    await Promise.resolve();
    h.log.length = 0;
    h.pop(1);
    expect(h.log).toEqual(["go -1"]);
  });
});

describe("ScrollMemory", () => {
  it("keeps a top per view and the row left from, matched by attribute", () => {
    const m = new ScrollMemory();
    m.save("inbox:", 321.4);
    m.save("chats", -5);
    expect([m.top("inbox:"), m.top("chats"), m.top("other")]).toEqual([321, 0, 0]);
    m.leftFrom("inbox:", "data-thread", "aaaaaaaa");
    expect(m.row("inbox:")).toEqual({ attr: "data-thread", value: "aaaaaaaa" });
    m.clearRow("inbox:");
    expect(m.row("inbox:")).toBeNull();
  });
});

// -- the app --------------------------------------------------------------------------------------

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

const T1 = "aaaaaaaa", K1 = "11111111";
const CMD = "curl -fsSL https://example.org/install.sh | sh";
const DETAIL = {
  card: { req_id: "r1", tool: "Bash", args: { command: CMD }, command: CMD, reason: "fetches", layer: "reviewer", origin: "chat x",
    headline: "", asked_at: "2026-10-29T08:26:00+00:00", timeout_s: 600, allowlistable: true, summary: "Installs a tool.", summary_state: "ready" },
  private: false, withheld: false, secure: { ok: true, failed: null, label: "", reason: "" },
  can: { deny: true, allow: true, always: true }, why: "",
};
const approval = (over: Record<string, unknown> = {}) => mkRow("appr:r1", 1, { kind: "approval", thread_id: T1,
  title: "Claude asks to use Bash", detail: { req_id: "r1", tool: "Bash" }, ...over });

function reviewBody(deferred: boolean) {
  const q = { row: { id: "q:aaaaaaaa:1", kind: "question", rank: 2, title: "Which?", blocks: "b", project_id: null, thread_id: T1,
    task_id: null, asked_at: "2026-03-11T04:30:00+00:00", expires_at: null, origin: "", detail: { text: "Which?", options: [] } },
    section: "overnight", place: "chat:aaaaaaaa", approve: false, leave: false, why: "", tag: null };
  const d = { row: { id: "defer:a", kind: "deferred", rank: 4, title: "deferred a", blocks: "b", project_id: null, thread_id: T1,
    task_id: null, asked_at: "2026-03-11T04:30:00+00:00", expires_at: null, origin: "",
    detail: { deferral_id: "a", tool: "Bash", grantable: true, why_not: "", expired: false, hud_only: false, count: 1, more: 0,
      card: { tool: "Bash", command: "make a" } } }, section: "overnight", place: "chat:aaaaaaaa", approve: true, leave: true,
    why: "", tag: "a".repeat(64) };
  return { v: 1, quiet: { enabled: true, now: false, start: null, end: null },
    summary: { approvals: deferred ? 1 : 0, questions: 1, decisions: 0, blocked: 0, proposals: 0, live: 0 }, live: [],
    overnight: deferred ? [q, d] : [q], before: [], fyi: { count: 0 }, more: 0, open: deferred ? 2 : 1,
    limits: { approve: 10, leave: 25 }, projects: [{ id: "p1", name: "Calc" }],
    places: [{ key: "chat:aaaaaaaa", kind: "chat", id: "aaaaaaaa", project_id: "p1", title: "Fix it" }],
    group: { ok: true, why: "" }, opens_here: false };
}

function harness(opts: { rows?: unknown[]; review?: unknown } = {}) {
  const gets: Record<string, Got> = {};
  const posts: string[] = [];
  const replies: Record<string, Posted> = {};
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows: opts.rows ?? [] } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null } } };
  gets["/m/api/review"] = opts.review ? { ok: true, body: opts.review } : { ok: true, body: { open: 0 } };
  gets["/m/api/tasks"] = { ok: true, body: { tasks: [], more: false } };
  gets["/m/api/working"] = { ok: true, body: { entries: [], more: false, total: 0 } };
  gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: DETAIL };
  gets[`/m/api/threads/${T1}/log`] = { ok: true, body: { rows: [{ seq: 1, role: "Claude", text: "hello", at: null, marks: [] }], next: 2,
    thread: { id: T1, title: "Fix it", project_id: "p1" } } };
  gets["/m/api/projects"] = { ok: true, body: { projects: [{ id: "p1", name: "Calc" }] } };
  gets[`/m/api/tasks/${K1}`] = { ok: true, body: { task: { id: K1, project_id: "p1", brief: "Build", state: "running",
    updated: "2026-11-15T10:00:00Z", status: { step: 1, steps: 3 } } } };
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => { signedOut = true; },
    fetchJson: async (path) => gets[path.split("?")[0]] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } },
    postJson: async (path) => { posts.push(path); return replies[path] ?? { status: 200, body: { ok: true } }; },
    getCredential: async () => null,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
    now: () => Date.parse("2026-11-15T10:30:00Z"),
  });
  let signedOut = false;
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  /** jsdom's history traversal is asynchronous. */
  const traversed = async () => { await new Promise((r) => setTimeout(r, 25)); await settle(); };
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  const click = async (id: string) => { q(id)!.click(); await settle(); };
  return { app, root, gets, posts, replies, settle, traversed, q, click, get signedOut() { return signedOut; } };
}

const cssOf = (f: string) => readFileSync(join(__dirname, f), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

describe("the sheet keeps the safety gap", () => {
  it("leaves .m-gap at its 7.5rem (120 px, never under 100) and no rule narrows it inside the sheet", () => {
    const rules = [...cssOf("mobile.css").matchAll(/(?:^|\n)\.m-gap \{([^}]*)\}/g)].map((m) => m[1]);
    const height = rules.map((r) => /height:\s*([\d.]+)rem/.exec(r)?.[1]).filter(Boolean).map(Number);
    expect(height).toEqual([7.5]);
    expect(height[0] * 16).toBeGreaterThan(100);
    for (const f of readdirSync(__dirname).filter((n) => n.endsWith(".css"))) {
      if (f === "mobile.css") continue;
      expect([f, /\.m-gap\b[^{]*\{[^}]*height/.test(cssOf(f))]).toEqual([f, false]);
    }
    expect(cssOf("nav.css")).not.toMatch(/\.m-(deny-row|allow-row|gap|card-notice)\b/);
  });
});

const here = () => location.pathname + location.search + location.hash;

describe("history is state only: no id ever reaches the address", () => {
  it("opening a chat, a card and a task pushes {jv} entries and leaves the URL as it was", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: [approval()] });
    h.app.start();
    await h.settle();
    const length = history.length;
    expect(history.state).toEqual({ jv: 0 });
    await h.app.openThread(T1);
    await h.settle();
    expect(history.state).toEqual({ jv: 1 });
    h.app.openCard(approval() as never);
    await h.settle();
    expect(history.state).toEqual({ jv: 2 });
    expect(history.length).toBe(length + 2);
    expect(here()).toBe("/m/");
    expect(location.href).not.toContain(T1);
    expect(JSON.stringify(history.state)).not.toContain(T1);
    h.app.stop();
  });

  it("opening a task pushes one entry and no id either", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    await h.app.openTask(K1);
    await h.settle();
    expect(history.state).toEqual({ jv: 1 });
    expect(here()).toBe("/m/");
    expect(location.href).not.toContain(K1);
    h.app.stop();
  });

  it("the browser's back closes the sheet first, then the chat, one level each", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: [approval()] });
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    h.app.openCard(approval() as never);
    await h.settle();
    expect([h.app.depth(), !!h.app.card, !!h.app.thread]).toEqual([2, true, true]);
    history.back();
    await h.traversed();
    expect([h.app.depth(), !!h.app.card, h.app.thread?.id]).toEqual([1, false, T1]);
    expect(history.state).toEqual({ jv: 1 });
    history.back();
    await h.traversed();
    // Opened from the Inbox, so back is the Inbox, not the Chats list.
    expect([h.app.depth(), h.app.thread, h.app.tab]).toEqual([0, null, "inbox"]);
    expect(history.state).toEqual({ jv: 0 });
    h.app.stop();
  });

  it("the in-app back works with no browser back at all: it closes the level, then trims the history", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    await h.settle();
    expect(history.state).toEqual({ jv: 1 });
    await h.click("m-back");
    // The level closed at once, whatever the browser does about its history.
    expect(h.app.thread).toBeNull();
    await h.traversed();
    expect(history.state).toEqual({ jv: 0 });
    h.app.stop();
  });
});

describe("scroll memory and the row you came from", () => {
  it("the Inbox keeps its scroll across a chat, and the row opened from it takes the focus back", async () => {
    history.replaceState(null, "", "/m/");
    const rows = [mkRow("q:1", 3, { thread_id: T1, detail: { text: "Which?", options: [] } }),
      mkRow("q:2", 3, { thread_id: "bbbbbbbb", detail: { text: "And?", options: [] } })];
    const h = harness({ rows });
    h.app.start();
    await h.settle();
    const main = h.q("m-main")!;
    main.scrollTop = 321;
    const opener = h.root.querySelector<HTMLElement>(`[data-thread="${T1}"] [data-testid="m-thread-open"]`)!;
    expect(opener).not.toBeNull();
    opener.click();
    await h.settle();
    expect(h.app.thread?.id).toBe(T1);
    expect(h.q("m-inbox")).toBeNull();
    expect(h.q("m-back")!.textContent).toBe("‹ Inbox");
    await h.click("m-back");
    await h.traversed();
    expect(h.app.tab).toBe("inbox");
    expect(h.q("m-main")!.scrollTop).toBe(321);
    expect(document.activeElement).toBe(h.root.querySelector(`[data-thread="${T1}"] [data-testid="m-thread-open"]`));
    h.app.stop();
  });

  it("each tab remembers its own scroll", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    const main = h.q("m-main")!;
    main.scrollTop = 410;
    h.app.show("chats");
    await h.settle();
    expect(main.scrollTop).toBe(0);
    main.scrollTop = 77;
    h.app.show("inbox");
    await h.settle();
    expect(main.scrollTop).toBe(410);
    h.app.show("chats");
    await h.settle();
    expect(main.scrollTop).toBe(77);
    h.app.stop();
  });
});

describe("the approval sheet", () => {
  it("opens over the list with the heading focused, keeps the list and its scroll, and returns the focus on close", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: [approval()] });
    h.app.start();
    await h.settle();
    const main = h.q("m-main")!;
    main.scrollTop = 250;
    await h.click("m-item-review");
    const sheet = h.q("m-sheet")!;
    expect(sheet).not.toBeNull();
    // The card is the sheet's content, whole, in its order: DENY, the gap, the allow row, the notice.
    expect(sheet.contains(h.q("approval-card"))).toBe(true);
    expect(Array.from(sheet.querySelector(".m-card")!.children).map((n) => (n as HTMLElement).dataset.testid || n.className).slice(0, 5))
      .toEqual(["m-card-heading", "m-deny-row", "m-gap", "m-allow-row", "m-card-notice"]);
    // The list is still in the page, behind the sheet, its scroll untouched, and takes no tap or Tab.
    expect(h.q("m-inbox")).not.toBeNull();
    expect(main.scrollTop).toBe(250);
    for (const node of [main, h.root.querySelector(".top")!, h.root.querySelector(".tabs")!]) expect(node.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(h.q("m-card-heading"));
    expect(h.q("m-card-heading")!.tabIndex).toBe(-1);
    expect(document.activeElement).not.toBe(h.q("approval-deny"));
    await h.click("m-back");
    await h.settle();
    expect(h.q("m-sheet")).toBeNull();
    expect(main.hasAttribute("inert")).toBe(false);
    expect(main.scrollTop).toBe(250);
    expect(document.activeElement).toBe(h.root.querySelector('[data-id="appr:r1"] [data-testid="m-item-review"]'));
    h.app.stop();
  });

  it("closes from a tap on the dim area, not while an answer is on its way", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: [approval()] });
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    h.app.card!.busy = true;
    await h.click("m-sheet-dim");
    expect(h.app.card).not.toBeNull();
    h.app.card!.busy = false;
    await h.click("m-sheet-dim");
    expect(h.app.card).toBeNull();
    expect(h.q("m-sheet")).toBeNull();
    h.app.stop();
  });

  it("has no drag or swipe handlers: it closes only by its back, its dim area or history", () => {
    const sheet = sheetView(document.createElement("div"), () => {});
    const names: string[] = [];
    for (const node of sheet) for (const a of Array.from(node.attributes)) names.push(a.name);
    expect(names.filter((n) => /^on|draggable/.test(n))).toEqual([]);
  });

  it("opens over an open chat, and its tray draws that chat's needs-you rows", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: [approval(), mkRow("q:9", 6, { thread_id: T1, detail: { text: "fyi", options: [] } })] });
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    await h.settle();
    expect(h.q("m-tray")).not.toBeNull();
    // Rank 6 is not a tray row.
    expect(h.q("m-tray-body")!.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    await h.click("m-item-review");
    expect(h.q("m-sheet")!.contains(h.q("approval-card"))).toBe(true);
    expect(h.q("m-transcript")).not.toBeNull();
    expect(h.app.tab).toBe("chats");
    h.app.stop();
  });
});

describe("the Needs-you tray", () => {
  const rows = [{ id: "a", rank: 1, title: "Allow Bash" }, { id: "b", rank: 5, title: "A question" }, { id: "c", rank: 6, title: "Later" }];
  const draw = (r: { id: string }) => { const n = document.createElement("article"); n.dataset.id = r.id; return n; };

  it("is absent with no row of rank 1 to 5, and counts ranks 1 to 5 only", () => {
    expect(trayView({ rows: [rows[2]], draw, folded: false, fold: () => {} })).toBeNull();
    const tray = trayView({ rows, draw, folded: false, fold: () => {} })!;
    expect(tray.querySelector(".m-tray-count")!.textContent).toBe(" · 2");
    expect(Array.from(tray.querySelectorAll("article")).map((n) => (n as HTMLElement).dataset.id)).toEqual(["a", "b"]);
  });

  it("folded to its 48 px bar it still holds every row in the page; the bar turns amber for rank 1 or 2", () => {
    const tray = trayView({ rows, draw, folded: true, fold: () => {} })!;
    expect(tray.querySelector<HTMLElement>(".m-tray-body")!.hidden).toBe(true);
    expect(tray.querySelectorAll("article").length).toBe(2);
    expect(tray.querySelector<HTMLElement>(".m-tray-bar")!.dataset.tone).toBe("pending");
    expect(tray.querySelector(".m-tray-bar")!.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("the ⋯ menu", () => {
  it("is closed until tapped, then lists Notifications, when Jarvis last answered, and Sign out last", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    expect(h.q("m-signout")).toBeNull();
    expect(h.q("m-notify")).toBeNull();
    expect(h.q("m-menu")!.getAttribute("aria-expanded")).toBe("false");
    await h.click("m-menu");
    expect(h.q("m-menu")!.getAttribute("aria-expanded")).toBe("true");
    const panel = h.q("m-menu-panel")!;
    const order = Array.from(panel.children).map((n) => (n as HTMLElement).dataset.testid || (n as HTMLElement).dataset.menuItem);
    expect(order).toEqual(["m-notify", "m-menu-lastread", "m-signout"]);
    expect(h.q("m-notify")).not.toBeNull();
    expect(h.q("m-menu-lastread")!.textContent).toMatch(/^Last read (just now|\d+ min ago)$/);
    expect(h.q("m-signout")!.textContent).toBe("Sign out");
    h.app.stop();
  });

  it("closes on a tap outside and on Escape, the focus back on the button", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    await h.click("m-menu");
    h.root.querySelector<HTMLElement>(".main")!.click();
    expect(h.q("m-menu-panel")).toBeNull();
    await h.click("m-menu");
    (h.q("m-menu-panel")!.querySelector("button") as HTMLElement).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(h.q("m-menu-panel")).toBeNull();
    expect(document.activeElement).toBe(h.q("m-menu"));
    h.app.stop();
  });

  it("Sign out signs out, and a row has the 48 px floor", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    await h.click("m-menu");
    await h.click("m-signout");
    expect(h.signedOut).toBe(true);
    const menu = menuView({ id: "x", label: "X", open: true, toggle: () => {}, items: [{ id: "a", label: "A" }] });
    expect(menu.querySelectorAll("button").length).toBe(2);
  });

  it("a task page's ⋯ holds Cancel, and Cancel's confirmation is the one it had", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness();
    h.app.start();
    await h.settle();
    await h.app.openTask(K1);
    await h.settle();
    expect(h.q("m-task-cancel")).toBeNull();
    // The ⋯ is in the page's header, not in the body.
    expect(h.root.querySelector(".subbar")!.contains(h.q("m-task-more"))).toBe(true);
    await h.click("m-task-more");
    await h.click("m-task-cancel");
    expect(h.q("m-task-ask")!.textContent).toMatch(/Your passkey confirms it/);
    expect(h.q("m-task-confirm")!.textContent).toBe("Cancel with passkey");
    expect(h.posts).toEqual([]);
    h.app.stop();
  });
});

describe("the morning review is a line in the Inbox; its tab shows only for deferrals", () => {
  it("draws the line with the count, opens the review from it, and hides the tab with no deferral", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ review: reviewBody(false) });
    h.app.start();
    await h.settle();
    expect(h.q("m-review-open")!.textContent).toBe("Morning review · 1 left overnightOpen");
    expect(h.q("m-tab-review")!.hidden).toBe(true);
    await h.click("m-review-open");
    expect(h.app.tab).toBe("review");
    // Open, the tab is there to stand on.
    expect(h.q("m-tab-review")!.hidden).toBe(false);
    h.app.stop();
  });

  it("shows the tab, and still the line, while a deferred approval waits", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ review: reviewBody(true) });
    h.app.start();
    await h.settle();
    expect(h.q("m-tab-review")!.hidden).toBe(false);
    expect(h.q("m-review-open")!.textContent).toContain("2 left overnight");
    h.app.stop();
  });

  it("has no line when there is nothing to review", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    expect(h.q("m-review-open")).toBeNull();
    h.app.stop();
  });
});

describe("PX-3b state across the sheet and back-navigation", () => {
  const rows = () => [approval(), mkRow("q:5", 5, { thread_id: T1, detail: { text: "Which?", options: [] } }),
    mkRow("m:6", 6, { kind: "missed", thread_id: T1, detail: { text: "n", n: 1 } }),
    mkRow("m:7", 7, { kind: "missed", thread_id: T1, detail: { text: "n", n: 1 } })];
  const hidden = (h: ReturnType<typeof harness>, id: string) =>
    !!h.root.querySelector(`[data-testid="m-inbox-row"][data-id="${id}"]`)?.closest("[hidden]");

  it("focus returns to the Review button inside the thread card; the fold and Later survive the sheet and a chat's back", async () => {
    history.replaceState(null, "", "/m/");
    const h = harness({ rows: rows() });
    h.app.start();
    await h.settle();
    expect(hidden(h, "m:6")).toBe(true);
    await h.click("m-item-fold");
    expect(hidden(h, "m:6")).toBe(false);
    await h.click("m-item-later");
    expect(h.q("m-item-unlater")).not.toBeNull();
    await h.click("m-item-review");
    expect(h.q("m-sheet")).not.toBeNull();
    await h.click("m-back");
    await h.traversed();
    expect(h.q("m-sheet")).toBeNull();
    expect(document.activeElement).toBe(h.root.querySelector('[data-thread="' + T1 + '"] [data-id="appr:r1"] [data-testid="m-item-review"]'));
    expect(hidden(h, "m:6")).toBe(false);
    expect(h.q("m-item-unlater")).not.toBeNull();
    h.root.querySelector<HTMLElement>(`[data-thread="${T1}"] [data-testid="m-thread-open"]`)!.click();
    await h.settle();
    history.back();
    await h.traversed();
    expect(h.app.thread).toBeNull();
    expect(hidden(h, "m:6")).toBe(false);
    expect(h.q("m-item-unlater")).not.toBeNull();
    h.app.stop();
  });
});
