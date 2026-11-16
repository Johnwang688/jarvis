import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { mkRow } from "./threads.fixtures";

const apps: PhoneApp[] = [];
const settle = async () => { for (let n = 0; n < 80; n++) await Promise.resolve(); };
afterEach(() => { apps.splice(0).forEach(app => app.stop()); document.body.replaceChildren(); });

async function start() {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const stream: StreamLike = { readyState: 1, onopen: null, onmessage: null, onerror: null, close() {} };
  const responses: Record<string, Got> = {
    "/m/api/me": { ok: true, body: { secure: { ok: true }, unlock_s: 1800 } },
    "/m/api/inbox": { ok: true, body: { rows: [1, 2, 3, 4].map(rank => mkRow(`q:aaaaaaaa:${rank}`, rank)) } },
    "/m/api/working": { ok: true, body: { entries: [], total: 0, more: false } },
    "/m/api/tasks": { ok: true, body: { tasks: [] } },
    // The only deferral is outside the report's cap. Its summary must keep the door open at night.
    "/m/api/review": { ok: true, body: { v: 1, open: 61, more: 1, overnight: [], before: [],
      summary: { approvals: 1, questions: 60 }, quiet: { enabled: true, now: true }, opens_here: false } },
  };
  const signOut = vi.fn(async () => {});
  const app = new PhoneApp({ root, signOut, onSignedOut() {},
    fetchJson: async path => responses[path] ?? { ok: false, status: 404, why: "error", body: {} },
    postJson: async () => ({ status: 200, body: {} }), openStream: () => stream,
    pushEnv: { supported: () => false, ios: () => true, standalone: () => true,
      permission: () => "default", register: async () => { throw new Error("unsupported"); }, onOpenInbox() {} },
    setTimer: fn => { queueMicrotask(fn); return 0; } });
  apps.push(app);
  app.start();
  await settle();
  stream.onopen?.(new Event("open"));
  await settle();
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  const click = async (id: string) => { q(id).click(); await settle(); };
  return { app, root, responses, signOut, q, click };
}

describe("independent RD-2 verification", () => {
  it("quiet capped deferrals survive every filter, report refresh and notification opening", async () => {
    const h = await start();
    for (const filter of [null, "needs", "running", "tasks"] as const) {
      h.app.filter = filter;
      h.app.render();
      await h.app.loadReview();
      expect(h.q("m-review-open"), String(filter)).not.toBeNull();
      await h.click("m-review-open");
      expect(h.q("m-review")).not.toBeNull();
      await h.click("m-review-back");
      await h.app.openFromNotification();
      expect(h.q("m-review-open")).not.toBeNull();
      await h.click("m-review-open");
      expect(h.app.reviewSel.size).toBe(0);
      await h.click("m-review-back");
    }
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]')).toHaveLength(4);
    expect(h.q("m-tab-inbox").getAttribute("aria-label")).toBe("Inbox, 4 need you");
    h.responses["/m/api/inbox"] = { ok: true, body: { rows: [] } };
    await h.app.loadInbox();
    expect(h.q("m-tab-inbox").getAttribute("aria-label")).toBe("Inbox");
    expect(h.q("m-tab-inbox").querySelector(".m-tab-badge")).toBeNull();
  });

  it("background inert and cancellation survive a redraw while sign out is open", async () => {
    const h = await start();
    await h.click("m-tab-settings");
    await h.click("m-signout");
    for (const redraw of [false, true]) {
      if (redraw) h.app.render();
      for (const selector of [".top", ".main", ".tabs"]) {
        expect(h.root.querySelector(selector)?.hasAttribute("inert"), selector).toBe(true);
      }
      expect(document.activeElement).toBe(h.q("m-signout-sheet"));
      expect(h.signOut).not.toHaveBeenCalled();
    }
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(h.q("m-signout-sheet")).toBeNull();
    expect(document.activeElement).toBe(h.q("m-signout"));
    expect(h.root.querySelector("[inert]")).toBeNull();
    await h.click("m-signout");
    await h.click("m-signout-confirm");
    expect(h.signOut).toHaveBeenCalledOnce();
  });

  it("returns focus to the Inbox Morning review opener when its new Back closes the level (spec 5)", async () => {
    const h = await start();
    h.q("m-review-open").focus();
    await h.click("m-review-open");
    h.q("m-review-back").focus();
    await h.click("m-review-back");
    expect(document.activeElement?.getAttribute("data-testid")).toBe("m-review-open");
  });
});
