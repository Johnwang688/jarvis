import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import type { PushEnv, SubscriptionLike } from "./notify";
import { settingsView, type SettingsState } from "./settings";
import { mkRow } from "./threads.fixtures";

const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach((app) => app.stop()); document.body.replaceChildren(); });
const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
function harness() {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const stream: StreamLike = { readyState: 1, onopen: null, onmessage: null, onerror: null, close() {} };
  let clock = Date.parse("2026-11-15T16:00:00Z");
  const gets: Record<string, Got> = {
    "/m/api/me": { ok: true, body: { secure: { ok: true }, unlock_s: 1800 } },
    "/m/api/inbox": { ok: true, body: { rows: [mkRow("appr:a", 1), mkRow("q:aaaaaaaa:1", 2), mkRow("done:a", 6)] } },
    "/m/api/working": { ok: true, body: { entries: [], more: false, total: 0 } },
    "/m/api/tasks": { ok: true, body: { tasks: [] } },
    "/m/api/review": { ok: true, body: { v: 1, open: 0, overnight: [], before: [], quiet: { enabled: true, start: "22:00", end: "08:00" } } },
  };
  const replies: Record<string, Posted> = {};
  const post = vi.fn(async (path: string): Promise<Posted> => replies[path] ?? { status: 200, body: {} });
  const signOut = vi.fn(async () => {});
  const raw = Uint8Array.from({ length: 65 }, (_, i) => i === 0 ? 4 : i);
  const key = btoa(String.fromCharCode(...raw)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const subscription: SubscriptionLike = { endpoint: "https://example.org/phone", toJSON: () => ({ endpoint: "https://example.org/phone" }),
    unsubscribe: vi.fn(async () => true) };
  const subscribe = vi.fn(async () => subscription);
  const pushEnv: PushEnv = { supported: () => true, ios: () => true, standalone: () => true,
    permission: () => "default", onOpenInbox() {},
    register: async () => ({ pushManager: { getSubscription: async () => subscription, subscribe } }) };
  const pushSend = vi.fn(async () => ({ status: 200, body: { enabled: true, key, subscribed: false } }));
  const app = new PhoneApp({ root, signOut, onSignedOut: vi.fn(), postJson: post,
    fetchJson: async (path) => gets[path] ?? { ok: false, why: "error", status: 404, body: {} },
    openStream: () => stream, now: () => clock, pushEnv, pushSend,
    setTimer: (fn) => { queueMicrotask(fn); return 0; } });
  apps.push(app);
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  const click = async (id: string) => { q(id).click(); await settle(); };
  return { root, app, gets, replies, post, signOut, stream, q, click, subscribe, pushSend, subscription,
    advance: () => { clock += 60_000; } };
}
async function started() { const h = harness(); h.app.start(); await settle(); h.stream.onopen?.(new Event("open")); return h; }

describe("RD-2 icon tabs and Settings", () => {
  it("keeps exactly three icon and label tabs, with the exact live amber Needs-you count and name", async () => {
    const h = await started();
    const tabs = [...h.root.querySelectorAll("nav.tabs button")];
    expect(tabs.map((n) => n.getAttribute("data-testid"))).toEqual(["m-tab-inbox", "m-tab-chats", "m-tab-settings"]);
    for (const tab of tabs) {
      expect(tab.querySelector('svg[aria-hidden="true"][focusable="false"]')).not.toBeNull();
      expect(tab.querySelector(".m-tab-label")?.textContent).toMatch(/^(Inbox|Chats|Settings)$/);
    }
    expect(h.q("m-tab-inbox").getAttribute("aria-label")).toBe("Inbox, 2 need you");
    expect(h.q("m-tab-inbox").querySelector(".m-tab-badge")?.textContent).toBe("2");
    await h.click("m-tab-settings");
    expect(h.q("m-tab-settings").getAttribute("aria-current")).toBe("page");
    expect(h.q("m-tab-inbox").hasAttribute("aria-current")).toBe(false);
    h.gets["/m/api/inbox"] = { ok: true, body: { rows: [] } };
    await h.app.loadInbox();
    expect(h.q("m-tab-inbox").getAttribute("aria-label")).toBe("Inbox");
    expect(h.q("m-tab-inbox").querySelector(".m-tab-badge")).toBeNull();
  });

  it("renders all sections, uses live orb/security/unlock/read/quiet state, and keeps focus across updates", async () => {
    const h = await started();
    await h.click("m-tab-settings");
    expect([...h.q("m-settings").querySelectorAll("h2")].map((n) => n.textContent)).toEqual(["Connection", "Unlock", "Notifications", "Session"]);
    expect(h.q("m-settings-quiet").textContent).toContain("22:00 to 08:00");
    expect(h.q("m-settings-session").textContent).toContain("passkey");
    expect(h.q("m-menu-lastread").textContent).not.toBe("Not read yet");
    expect(h.q("m-settings-connection").textContent).toBe(h.q("m-conn").textContent);
    expect(h.q("m-settings-unlock").textContent).toContain("Unlocked until");
    h.q("m-lock-now").focus();
    h.app.setOnline(false);
    expect(h.q("m-settings-connection").textContent).toMatch(/^Offline/);
    expect(document.activeElement).toBe(h.q("m-lock-now"));
    h.gets["/m/api/me"] = { ok: true, body: { secure: { ok: false, label: "<img src=x onerror=alert(1)>" }, unlock_s: 0 } };
    await h.app.loadMe();
    expect(h.q("m-settings-security").textContent).toContain("<img");
    expect(h.q("m-settings-security").dataset.tone).toBe("err");
    expect(h.q("m-settings-unlock").textContent).toBe("Locked");
    expect(h.q("m-lock-pill").dataset.state).toBe("locked");
    expect(h.q("m-settings").querySelector("img,script,[onerror]")).toBeNull();
  });

  it("locks through the existing server flow, reports refusal truthfully, and exposes only two pill rows", async () => {
    const h = await started();
    await h.click("m-lock-pill");
    expect([...h.q("m-menu-panel").children].map((n) => (n as HTMLElement).dataset.testid)).toEqual(["m-lock-now", "m-menu-settings"]);
    await h.click("m-menu-settings");
    h.replies["/m/api/lock"] = { status: 403, body: {} };
    await h.click("m-lock-now");
    expect(h.q("m-settings-lock-note").textContent).toContain("refused");
    expect(h.q("m-lock-pill").dataset.state).toBe("unlocked");
    delete h.replies["/m/api/lock"];
    await h.click("m-lock-now");
    expect(h.post.mock.calls.map(([path]) => path)).toEqual(["/m/api/lock", "/m/api/lock"]);
    expect(h.q("m-settings-unlock").textContent).toBe("Locked");
    expect(h.q("m-settings-lock-note").textContent).toMatch(/^Locked\./);
    expect(h.q("m-lock-pill").dataset.state).toBe("locked");
    h.gets["/m/api/me"] = { ok: true, body: { secure: { ok: true }, unlock_s: 1800 } };
    await h.app.loadMe();
    expect(h.q("m-settings-lock-note").hidden).toBe(true);
  });

  it("reuses the real notification row for subscribing and unsubscribing in Settings", async () => {
    const h = await started();
    await h.click("m-tab-settings");
    expect(h.q("m-notify-button").textContent).toBe("Turn on");
    await h.click("m-notify-button");
    expect(h.subscribe).toHaveBeenCalledOnce();
    expect(h.q("m-notify-button").textContent).toBe("Turn off");
    await h.click("m-notify-button");
    expect(h.subscription.unsubscribe).toHaveBeenCalledOnce();
    expect(h.pushSend.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(h.q("m-notify-button").textContent).toBe("Turn on");
  });

  it.each(["m-signout-cancel", "m-signout-dim", "Escape", "Back"])("cancels sign out with %s, restores focus and removes inert", async (method) => {
    const h = await started();
    await h.click("m-tab-settings");
    h.q("m-tab-settings").focus(); // iOS taps need not focus the row that opened the sheet
    await h.click("m-signout");
    expect(h.signOut).not.toHaveBeenCalled();
    expect(h.q("m-signout-sheet").getAttribute("role")).toBe("alertdialog");
    expect(document.activeElement).toBe(h.q("m-signout-sheet"));
    for (const selector of [".top", ".main", ".tabs"]) expect(h.root.querySelector(selector)?.hasAttribute("inert")).toBe(true);
    h.app.render(); // live redraw while the sheet is open keeps the background inert
    expect(h.root.querySelector(".top")?.hasAttribute("inert")).toBe(true);
    if (method === "Escape") document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    else if (method === "Back") h.app.goBack();
    else await h.click(method);
    expect(h.q("m-signout-sheet")).toBeNull();
    expect(h.signOut).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(h.q("m-signout"));
    for (const selector of [".top", ".main", ".tabs", ".m-brand"]) expect(h.root.querySelector(selector)?.hasAttribute("inert")).toBe(false);
  });

  it("traps focus both ways and signs out only on the second tap", async () => {
    const h = await started();
    await h.click("m-tab-settings");
    const session = h.q("m-signout").parentElement!;
    expect(session.lastElementChild).toBe(h.q("m-signout"));
    h.q("m-signout").focus();
    await h.click("m-signout");
    expect(h.signOut).not.toHaveBeenCalled();
    const key = (shiftKey = false) => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true }));
    key(true);
    expect(document.activeElement).toBe(h.q("m-signout-cancel"));
    key();
    expect(document.activeElement).toBe(h.q("m-signout-confirm"));
    key(true);
    expect(document.activeElement).toBe(h.q("m-signout-cancel"));
    h.q("m-tab-inbox").focus();
    expect(document.activeElement).toBe(h.q("m-signout-sheet"));
    await h.click("m-signout-confirm");
    expect(h.signOut).toHaveBeenCalledOnce();
    expect(h.app.stopped).toBe(true);
  });

  it("renders hostile state and quiet text only as text", () => {
    const hostile = '<svg onload="alert(1)"><img src=x onerror="alert(1)">';
    const state: SettingsState = { connection: hostile, connectionState: "live", tone: "ok", lastRead: hostile, security: hostile,
      limited: true, unlock: hostile, unlocked: false, lockNote: hostile, quiet: hostile };
    const view = settingsView(state, document.createElement("section"), { lock() {}, signOut() {} });
    expect(view.textContent).toContain(hostile);
    expect(view.querySelector("img,script,[onload],[onerror]")).toBeNull();
  });

  it("keeps Morning review reachable under every Inbox filter for a quiet, capped report with deferrals", async () => {
    const h = await started();
    h.gets["/m/api/review"] = { ok: true, body: { v: 1, open: 61, more: 1, overnight: [], before: [],
      summary: { approvals: 1, questions: 60 }, quiet: { enabled: true, now: true } } };
    await h.app.loadReview();
    for (const filter of [null, "needs", "running", "tasks"] as const) {
      h.app.filter = filter;
      h.app.render();
      expect(h.q("m-review-open"), String(filter)).not.toBeNull();
      await h.click("m-review-open");
      expect(h.q("m-review")).not.toBeNull();
      await h.click("m-review-back");
      expect(h.app.tab).toBe("inbox");
    }
  });
});
