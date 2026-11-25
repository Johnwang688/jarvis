// Orchestrator-owned acceptance tests for W3's T3 panel (implementers: do not edit; if one is wrong, stop and say why).
// Contract: <App/>'s `[data-panel="t3"]` (heading "T3", in the rail) lists the coding threads Jarvis started, from GET /v3/owner/work.
//  Poll: one poller for the whole window. First request at mount, then 10 s after each poll settles; a poll is abandoned after 5 s
//    (abort + treated as failed; a late answer is ignored). Becoming visible polls at once when none is in flight. Unmount stops it.
//    fetch("/v3/owner/work", { cache: "no-store", redirect: "error", signal }) — a GET, nothing else is requested by the panel.
//  Answer, validated whole before anything is shown (one bad item rejects the answer):
//    an object; projects: array of at most 32 { name } with name matching /^[a-z0-9][a-z0-9-]{0,31}$/, no duplicates;
//    threads: array of at most 50 { thread_id: string 1–256 units, project: a name as above, title: string 1–200 units,
//    status: /^[a-z_]{1,32}$/, waiting_on_owner: boolean, started_at, updated_at: finite numbers }, no duplicate thread_id.
//    Extra keys are ignored. Server order is kept (newest first).
//  Messages (each the panel's only text besides its heading; no rows with them):
//    before the first answer "Checking…"; network failure, timeout or a non-2xx other than 503: "T3 threads are not reachable.";
//    503, a 200 that is not JSON, or a rejected answer: "T3 threads could not be read."; a failed poll clears earlier rows.
//    No projects and no threads: a line naming no registered project and the command `jarvis work add`. Projects, no threads: "No threads yet."
//  Rows: grouped by project — one element [data-work-project="<name>"] per project that has threads, holding an <h3> with the name and
//    one <li> per thread in server order. Group order: registered projects in the answer's order, then unregistered names by first appearance.
//    Each <li> has data-state and three children: .work-title (the title with \p{Bidi_Control} removed), .work-status (the word),
//    .work-when ("<span> ago", Status.tsx's units: "42 s", "10 min", "2 h 1 min", "1 d 2 h", from updated_at).
//    waiting_on_owner true or status "waiting" -> "waiting on you" / waiting (amber); started|queued|preparing|starting|running ->
//    "running" / running (accent); failed|interrupted|cancelled -> the same word / error (red); completed|idle|exported -> the same word,
//    rolled_back -> "rolled back" / done (muted); any other status -> "unknown" / unknown (muted).
//  Text only: server text is never markup; no button, link, input, select, textarea, iframe, details or [tabindex]; nothing takes focus.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import App from "../src/App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const NOW = Date.UTC(2030, 0, 2, 12, 0, 0);
const STATUS = { service: "jarvis-agentd", version: "v3", pid: 4242, uptime_s: 61 };
const WORK = "/v3/owner/work";
const thread = (id: string, project: string, ago: number, extra: Record<string, unknown> = {}) => ({
  thread_id: id, project, title: `Title ${id}`, status: "running", waiting_on_owner: false,
  started_at: NOW / 1000 - ago - 5, updated_at: NOW / 1000 - ago, ...extra,
});
const ANSWER = { projects: [{ name: "alpha" }], threads: [thread("t-1", "alpha", 42)] };
type Answer = () => Promise<Response>;
let root: Root; let box: HTMLElement; let calls: { url: string; init?: RequestInit }[]; let work: Answer;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const answer = (body: unknown) => { work = async () => json(body); };
const omit = (value: Record<string, unknown>, key: string) => { const copy = { ...value }; delete copy[key]; return copy; };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  calls = []; answer(ANSWER);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    calls.push({ url, init });
    if (url === WORK) return work();
    if (url === "/v3/status") return json(STATUS);
    if (url === "/v3/owner/feed") return json({ briefs: [], recent: [] });
    return json({ error: "x" }, 404);
  }));
  box = document.createElement("div"); document.body.append(box);
});
afterEach(() => { act(() => root.unmount()); box.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const settle = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const mount = async () => { root = createRoot(box); await act(async () => { root.render(createElement(App)); }); await settle(); };
const t3 = () => box.querySelector<HTMLElement>('[data-panel="t3"]')!;
const items = () => [...t3().querySelectorAll<HTMLElement>("li")];
const row = (li: HTMLElement) => [".work-title", ".work-status", ".work-when"].map((s) => li.querySelector(s)?.textContent);
const workCalls = () => calls.filter((c) => c.url === WORK);
const text = () => t3().textContent!.replace(/^T3/, "").trim();

describe("the T3 panel", () => {
  it("lists threads grouped by project with the title, status and when it last changed", async () => {
    answer({ projects: [{ name: "alpha" }, { name: "beta" }, { name: "idle-one" }], threads: [
      thread("t-1", "alpha", 42), thread("t-2", "beta", 600, { waiting_on_owner: true }),
      thread("t-3", "alpha", 7260, { status: "completed" }), thread("t-4", "gone", 93_600, { status: "failed" }),
    ] });
    await mount();
    const groups = [...t3().querySelectorAll<HTMLElement>("[data-work-project]")];
    expect(groups.map((g) => g.dataset.workProject)).toEqual(["alpha", "beta", "gone"]);
    expect(groups.map((g) => g.querySelector("h3")?.textContent)).toEqual(["alpha", "beta", "gone"]);
    expect(groups.map((g) => [...g.querySelectorAll<HTMLElement>("li")].map(row))).toEqual([
      [["Title t-1", "running", "42 s ago"], ["Title t-3", "completed", "2 h 1 min ago"]],
      [["Title t-2", "waiting on you", "10 min ago"]],
      [["Title t-4", "failed", "1 d 2 h ago"]],
    ]);
    expect(items().map((li) => li.dataset.state)).toEqual(["running", "done", "waiting", "error"]);
  });

  it.each([
    ["started", false, "running", "running"], ["queued", false, "running", "running"], ["preparing", false, "running", "running"],
    ["starting", false, "running", "running"], ["running", false, "running", "running"], ["running", true, "waiting on you", "waiting"],
    ["waiting", false, "waiting on you", "waiting"], ["failed", false, "failed", "error"], ["interrupted", false, "interrupted", "error"],
    ["cancelled", false, "cancelled", "error"], ["completed", false, "completed", "done"], ["idle", false, "idle", "done"],
    ["exported", false, "exported", "done"], ["rolled_back", false, "rolled back", "done"], ["brand_new", false, "unknown", "unknown"],
  ])("status %s (waiting %s) reads %s with state %s", async (status, waiting, word, state) => {
    answer({ projects: [{ name: "alpha" }], threads: [thread("t-1", "alpha", 5, { status, waiting_on_owner: waiting })] });
    await mount();
    expect(items()).toHaveLength(1);
    expect(items()[0].dataset.state).toBe(state);
    expect(items()[0].querySelector(".work-status")?.textContent).toBe(word);
  });

  it("shows server text as text and strips bidi controls", async () => {
    const hostile = '<img src=x onerror="alert(1)"><b>bold</b><a href="http://example.invalid">x</a>';
    answer({ projects: [{ name: "alpha" }], threads: [thread("t-1", "alpha", 5, { title: hostile }), thread("t-2", "alpha", 6, { title: "safe‮gnp.exe⁦x⁩" })] });
    await mount();
    expect(items().map((li) => li.querySelector(".work-title")?.textContent)).toEqual([hostile, "safegnp.exex"]);
    expect(t3().querySelector("img,b,a,script")).toBeNull();
  });

  it.each([
    ["null", null], ["a list", []], ["no threads", { projects: [] }], ["no projects", { threads: [] }],
    ["threads not a list", { projects: [], threads: {} }],
    ["a hostile project name", { projects: [{ name: "<img src=x onerror=alert(1)>" }], threads: [] }],
    ["an upper-case project", { projects: [{ name: "Alpha" }], threads: [] }],
    ["duplicate projects", { projects: [{ name: "alpha" }, { name: "alpha" }], threads: [] }],
    ["33 projects", { projects: Array.from({ length: 33 }, (_, n) => ({ name: `p${n}` })), threads: [] }],
    ["a thread of a hostile project", { projects: [], threads: [thread("t-1", "<b>x</b>", 5)] }],
    ["a missing field", { projects: [], threads: [omit(thread("t-1", "alpha", 5), "status")] }],
    ["a string flag", { projects: [], threads: [thread("t-1", "alpha", 5, { waiting_on_owner: "true" })] }],
    ["a text time", { projects: [], threads: [thread("t-1", "alpha", 5, { updated_at: "<i>1</i>" })] }],
    ["an odd status", { projects: [], threads: [thread("t-1", "alpha", 5, { status: "Running<b>" })] }],
    ["an empty title", { projects: [], threads: [thread("t-1", "alpha", 5, { title: "" })] }],
    ["a long title", { projects: [], threads: [thread("t-1", "alpha", 5, { title: "x".repeat(201) })] }],
    ["an empty id", { projects: [], threads: [thread("", "alpha", 5)] }],
    ["duplicate ids", { projects: [], threads: [thread("t-1", "alpha", 5), thread("t-1", "alpha", 6)] }],
    ["51 threads", { projects: [], threads: Array.from({ length: 51 }, (_, n) => thread(`t-${n}`, "alpha", n)) }],
  ])("shows no rows for %s", async (_name, body) => {
    answer(body);
    await mount();
    expect(items()).toHaveLength(0);
    expect(text()).toBe("T3 threads could not be read.");
    expect(t3().querySelector("img,b,i")).toBeNull();
  });

  it("a failed poll after a good one clears the rows, and a good one brings them back", async () => {
    await mount();
    expect(items()).toHaveLength(1);
    work = async () => { throw new TypeError("down"); };
    await settle(10_000);
    expect(items()).toHaveLength(0);
    expect(text()).toBe("T3 threads are not reachable.");
    answer(ANSWER);
    await settle(10_000);
    expect(items()).toHaveLength(1);
    work = async () => json({ error: "x" }, 500);
    await settle(10_000);
    expect([items().length, text()]).toEqual([0, "T3 threads are not reachable."]);
    answer(ANSWER);
    await settle(10_000);
    work = async () => json({ error: "Service Unavailable" }, 503);
    await settle(10_000);
    expect([items().length, text()]).toEqual([0, "T3 threads could not be read."]);
    answer(ANSWER);
    await settle(10_000);
    work = async () => new Response("<html>", { status: 200 });
    await settle(10_000);
    expect([items().length, text()]).toEqual([0, "T3 threads could not be read."]);
  });

  it("the empty states", async () => {
    answer({ projects: [], threads: [] });
    await mount();
    expect(items()).toHaveLength(0);
    expect(text()).toMatch(/no project/i);
    expect(text()).toContain("jarvis work add");
    answer({ projects: [{ name: "alpha" }], threads: [] });
    await settle(10_000);
    expect([items().length, text()]).toEqual([0, "No threads yet."]);
  });

  it("is one poller that asks only for the work route, as a plain GET", async () => {
    await mount();
    await settle(60_000);
    expect(workCalls().length).toBeGreaterThanOrEqual(6);
    expect(workCalls().length).toBeLessThanOrEqual(8);
    for (const { init } of workCalls()) {
      expect(init?.method ?? "GET").toBe("GET");
      expect(init?.redirect).toBe("error");
      expect(init?.cache).toBe("no-store");
      expect(init?.body).toBeUndefined();
    }
  });

  it("a hung daemon times out, polls do not pile up, and a late answer is ignored", async () => {
    const open: ((r: Response) => void)[] = [];
    work = () => new Promise<Response>((resolve) => { open.push(resolve); });
    await mount();
    expect(text()).toBe("Checking…");
    await settle(60_000);
    expect(open.length).toBeGreaterThanOrEqual(3);
    expect(open.length).toBeLessThanOrEqual(6);
    expect(text()).toBe("T3 threads are not reachable.");
    for (const late of open.slice(0, -1)) await act(async () => { late(json(ANSWER)); await vi.advanceTimersByTimeAsync(0); });
    expect(items()).toHaveLength(0);
  });

  it("polls again at once when the window becomes visible", async () => {
    await mount();
    await settle(1_000);
    const before = workCalls().length;
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(workCalls().length).toBe(before + 1);
  });

  it("stops asking once it is gone", async () => {
    await mount();
    act(() => root.unmount());
    const n = calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls.length).toBe(n);
    root = createRoot(box);
  });

  it("offers no control and never takes the keyboard", async () => {
    answer({ projects: [{ name: "alpha" }], threads: [thread("t-1", "alpha", 5), thread("t-2", "alpha", 9, { waiting_on_owner: true })] });
    const field = document.createElement("input"); document.body.append(field); field.focus();
    await mount();
    await settle(30_000);
    expect(items()).toHaveLength(2);
    expect(t3().querySelectorAll("button,a,input,select,textarea,iframe,details,summary,[tabindex],[contenteditable]")).toHaveLength(0);
    expect(document.activeElement).toBe(field);
    field.remove();
  });
});

describe("the panel's colours", () => {
  const css = (dir: string): string => readdirSync(dir, { withFileTypes: true }).map((e) =>
    e.isDirectory() ? css(join(dir, e.name)) : e.name.endsWith(".css") ? readFileSync(join(dir, e.name), "utf8") : "").join("\n");
  const all = css(join(__dirname, "..", "src")).replace(/\/\*[\s\S]*?\*\//g, "");
  it.each([["running", "--accent"], ["waiting", "--amber"], ["error", "--red"], ["done", "--text-muted"]])(
    "state %s uses %s", (state, token) => {
      const rule = new RegExp(`[^{}]*work[^{}]*\\[data-state="${state}"\\][^{}]*\\{[^}]*var\\(${token}`);
      expect(all).toMatch(rule);
    });
});
