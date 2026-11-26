// Orchestrator-owned acceptance tests for A5a's window (implementers: do not edit).
// Contract: hud3/src/App.tsx default-exports <App/>, which needs no provider.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import App from "../src/App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const STATUS = { service: "jarvis-agentd", version: "v3", pid: 4242, started_at: "2026-11-24T05:00:00+00:00", uptime_s: 61, data_dir: "/x/v3" };
let root: Root; let box: HTMLElement; let calls: string[]; let respond: (url?: string) => Promise<Response>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
beforeEach(() => {
  vi.useFakeTimers();
  calls = []; respond = async () => json(STATUS);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => { const url = String(input instanceof Request ? input.url : input); calls.push(url); return respond(url); }));
  vi.stubGlobal("WebSocket", class { constructor(u: string) { calls.push("ws:" + u); } });
  vi.stubGlobal("EventSource", class { constructor(u: string) { calls.push("es:" + u); } });
  box = document.createElement("div"); document.body.append(box);
});
afterEach(() => { act(() => root.unmount()); box.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const mount = async () => { root = createRoot(box); await act(async () => { root.render(createElement(App)); }); await settle(); };
const settle = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(0); }); };
const panel = (id: string) => box.querySelector<HTMLElement>(`[data-panel="${id}"]`)!;

describe("the v3 window", () => {
  it("has the orb and one named slot per panel", async () => {
    await mount();
    expect(box.querySelector("[data-orb]")).not.toBeNull();
    const ids = [...box.querySelectorAll<HTMLElement>("[data-panel]")].map((p) => p.dataset.panel);
    expect(ids.slice().sort()).toEqual(["chat", "cron", "feed", "skills", "status", "t3", "vault", "voice"]);
    for (const [id, name] of [["t3", "T3"], ["chat", "Chat"], ["feed", "Feed"], ["vault", "Vault"], ["skills", "Skills"], ["cron", "Cron"], ["status", "Status"], ["voice", "Voice"]] as const) {
      const p = panel(id);
      const label = document.getElementById(p.getAttribute("aria-labelledby") ?? "");
      expect(label?.textContent).toBe(name);
      expect(p.contains(label!)).toBe(true);
    }
  });

  it("an empty slot says so and offers no control", async () => {
    await mount();
    for (const id of ["t3", "feed", "vault", "skills", "cron"]) {
      expect(panel(id).textContent!.length).toBeGreaterThan(id.length + 3);
      expect(panel(id).querySelectorAll("button,a,input,select,textarea,iframe,[tabindex]")).toHaveLength(0);
    }
    expect(box.querySelectorAll("iframe,button,input,textarea")).toHaveLength(0);
  });

  it("polls the status, chat, feed, Voice and T3 panel routes", async () => {
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(calls.length).toBeGreaterThan(1);
    expect([...new Set(calls)].sort()).toEqual(["/v3/owner/chat", "/v3/owner/feed", "/v3/owner/voice", "/v3/owner/work", "/v3/status"]);
  });

  it("shows the running daemon", async () => {
    await mount();
    expect(panel("status").textContent).toMatch(/running/i);
    expect(panel("status").textContent).toContain("4242");
    expect(panel("status").textContent).not.toContain("/x/v3");
  });

  it.each([
    ["the request fails", async () => { throw new TypeError("no"); }],
    ["an error answer", async () => json({ error: "x" }, 500)],
    ["another service", async () => json({ ...STATUS, service: "other" })],
    ["another version", async () => json({ ...STATUS, version: "v2" })],
    ["not JSON", async () => new Response("<html>", { status: 200 })],
    ["wrong shapes", async () => json({ ...STATUS, pid: "<b>1</b>", uptime_s: null })],
    ["null", async () => json(null)],
  ] as const)("never says running when %s", async (_name, answer) => {
    respond = answer as () => Promise<Response>;
    await mount();
    expect(panel("status").textContent).not.toMatch(/(^|[^t] )running/i);
    expect(panel("status").textContent).toMatch(/not (reachable|running)/i);
    expect(panel("status").querySelector("b")).toBeNull();
  });

  it("goes from running to not reachable, and back", async () => {
    await mount();
    respond = async () => { throw new TypeError("down"); };
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(panel("status").textContent).toMatch(/not (reachable|running)/i);
    respond = async () => json({ ...STATUS, pid: 99 });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(panel("status").textContent).toContain("99");
  });

  it("a slow answer never overwrites a newer one, and polls never pile up", async () => {
    const open: ((r: Response) => void)[] = [];
    respond = (url) => url?.endsWith("/v3/status") ? new Promise<Response>((r) => { open.push(r); }) : Promise.resolve(json({ briefs: [], recent: [] }));
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    const pending = open.length;
    expect(pending).toBeLessThanOrEqual(15); // a hung daemon must time out or be waited on, not stacked forever at full rate
    await act(async () => { open.at(-1)!(json({ ...STATUS, pid: 777 })); await vi.advanceTimersByTimeAsync(0); });
    for (const late of open.slice(0, -1)) await act(async () => { late(json({ ...STATUS, pid: 111 })); await vi.advanceTimersByTimeAsync(0); });
    expect(panel("status").textContent).toContain("777");
    expect(panel("status").textContent).not.toContain("111");
  });

  it("stops asking once it is gone", async () => {
    await mount();
    act(() => root.unmount());
    const n = calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls.length).toBe(n);
    root = createRoot(box);
  });

  it("never takes the keyboard", async () => {
    const field = document.createElement("input"); document.body.append(field); field.focus();
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(document.activeElement).toBe(field);
    field.remove();
  });
});

describe("the page", () => {
  const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");
  it("keeps the title the desktop bridge refuses to drive", () => {
    // windows/uiatree.py FORBIDDEN_TITLES
    expect(html.match(/<title>(.*?)<\/title>/)![1]!.toLowerCase()).toContain("j.a.r.v.i.s.");
  });
  it("is not v2's window", () => expect(html.match(/<title>(.*?)<\/title>/)![1]).not.toBe("J.A.R.V.I.S."));
  it("holds no inline script and no other origin", () => {
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/);
    expect(html).not.toMatch(/(src|href)="(https?:)?\/\//);
    expect(html).not.toMatch(/\son\w+=/);
  });
  it("has an installable manifest of its own", () => {
    const manifest = JSON.parse(readFileSync(join(__dirname, "..", "public", "manifest.webmanifest"), "utf8"));
    expect(manifest.id).toBe("/");
    expect(manifest.name.toLowerCase()).toContain("j.a.r.v.i.s.");
    expect(manifest.name).not.toBe("J.A.R.V.I.S.");
  });
});
