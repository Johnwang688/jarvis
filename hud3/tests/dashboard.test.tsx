// The dashboard shell: one status poll shared by the top bar and the job list.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import App from "../src/App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const NOW = Date.UTC(2030, 0, 2, 12, 0, 0);
const STATUS = { service: "jarvis-agentd", version: "v3", pid: 4242, uptime_s: 7384, data_dir: "/x/v3" };
const job = (ago: number | null, outcome: string | null = "ok", failures = 0) =>
  ({ last_run: ago === null ? null : NOW / 1000 - ago, last_outcome: outcome, failures });
let root: Root; let box: HTMLElement; let calls: string[]; let status: unknown;
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  calls = []; status = STATUS;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input instanceof Request ? input.url : input);
    calls.push(url);
    if (url === "/v3/owner/work") return json({ projects: [], threads: [] });
    if (url === "/v3/owner/voice") return json({
      state: "resting", helper: { connected: true, canceller: true },
      transcriber: { name: "fixture-asr" }, voice: { name: "fixture-voice" }, turns: [],
      settings: { speed: { value: 1, min: 0.8, max: 2 }, follow_up_s: { value: 8, min: 0, max: 30 } },
    });
    return json(url === "/v3/status" ? status : { briefs: [], recent: [] });
  }));
  box = document.createElement("div"); document.body.append(box);
});
afterEach(() => { act(() => root.unmount()); box.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const mount = async () => {
  root = createRoot(box);
  await act(async () => { root.render(createElement(App)); });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
};
const panel = (id: string) => box.querySelector<HTMLElement>(`[data-panel="${id}"]`)!;
const rows = () => [...panel("cron").querySelectorAll("li")].map((li) =>
  [...li.querySelectorAll("span")].map((s) => s.textContent));

describe("the job list", () => {
  it("shows each job, when it last ran and how it ended", async () => {
    status = { ...STATUS, jobs: { gmail: job(42), "morning-brief": job(7260), github: job(600, "error", 3), "evening-wrap": job(null, null) } };
    await mount();
    expect(rows()).toEqual([
      ["gmail", "42 s ago", "OK"],
      ["morning-brief", "2 h 1 min ago", "OK"],
      ["github", "10 min ago", "error · 3 failed"],
      ["evening-wrap", "", "Not run yet"],
    ]);
    expect([...panel("cron").querySelectorAll("li")].map((li) => li.dataset.state)).toEqual(["ok", "ok", "failed", "idle"]);
    expect(panel("cron").querySelectorAll("button,a,input,select,textarea,iframe,[tabindex]")).toHaveLength(0);
  });

  it("an ended-in-error job is never shown as OK, with or without a count", async () => {
    status = { ...STATUS, jobs: {
      a: job(5, "error", 0), b: job(5, "weird"), c: job(5, null), d: job(null, "error", 1), e: job(5, "ok", 2), f: job(null, null, 1),
    } };
    await mount();
    expect(rows().map((r) => r[2])).toEqual(["error", "weird", "Failed", "error · 1 failed", "Failed · 2 failed", "Failed · 1 failed"]);
    expect(panel("cron").querySelectorAll('li[data-state="failed"]')).toHaveLength(6);
  });

  it.each([
    ["no table", undefined], ["null", null], ["a list", [job(1)]], ["text", "<b>x</b>"], ["a number", 3],
  ])("says there are no jobs for %s, and still says running", async (_name, jobs) => {
    status = { ...STATUS, jobs };
    await mount();
    expect(panel("cron").textContent).toContain("No scheduled jobs reported.");
    expect(panel("status").textContent).toMatch(/Running/);
  });

  it("drops odd rows and names, keeps the rest, and creates no element from them", async () => {
    status = { ...STATUS, jobs: {
      "<img src=x onerror=alert(1)>": job(1), "UPPER": job(1), "": job(1), ["x".repeat(41)]: job(1),
      bad: "text", worse: null, gmail: { last_run: "<b>1</b>", last_outcome: { toString: "x" }, failures: "<i>9</i>" },
      feed: { last_run: 1e999, last_outcome: "ok".repeat(40), failures: -4 },
    } };
    await mount();
    expect(rows()).toEqual([["gmail", "", "Not run yet"], ["feed", "", "ok".repeat(12)]]);
    expect(panel("cron").querySelector("img,b,i")).toBeNull();
    expect(panel("status").textContent).toMatch(/Running/);
  });

  it("shows at most forty jobs", async () => {
    status = { ...STATUS, jobs: Object.fromEntries(Array.from({ length: 60 }, (_, n) => [`job-${n}`, job(n)])) };
    await mount();
    expect(rows()).toHaveLength(40);
  });

  it("says so when the daemon is not reachable, and shows no stale rows", async () => {
    status = { ...STATUS, jobs: { gmail: job(1) } };
    await mount();
    expect(rows()).toHaveLength(1);
    status = { error: "x" };
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(rows()).toHaveLength(0);
    expect(panel("cron").textContent).toContain("not reachable");
    expect(panel("status").querySelector(".agent-down")?.textContent).toBe("Not reachable");
  });
});

describe("the top bar", () => {
  it("shares one status poll between the bar and the job list", async () => {
    await mount();
    expect(calls.filter((url) => url === "/v3/status")).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    const polls = calls.filter((url) => url === "/v3/status").length;
    expect(polls).toBeGreaterThan(5);
    expect(polls).toBeLessThanOrEqual(11);
  });

  it("shows uptime in units a person reads", async () => {
    await mount();
    expect(panel("status").textContent).toContain("2 h 3 min");
    expect(panel("status").textContent).not.toContain("7384");
  });

  it("puts the feed, the chat and the rail in one grid, with the status in the bar", async () => {
    await mount();
    const grid = box.querySelector(".agent-grid")!;
    expect([...grid.children].map((el) => (el as HTMLElement).dataset.panel ?? el.className)).toEqual(["feed", "chat", "agent-rail"]);
    expect([...grid.querySelectorAll<HTMLElement>(".agent-rail > [data-panel]")].map((p) => p.dataset.panel)).toEqual(["voice", "cron", "t3", "vault", "skills"]);
    expect(box.querySelector(".agent-top")!.contains(panel("status"))).toBe(true);
    expect(box.querySelector(".agent-top")!.contains(box.querySelector("[data-orb]"))).toBe(true);
  });

  it("keeps Voice status outside the closed details and settings disclosure", async () => {
    await mount();
    const voice = panel("voice");
    const details = voice.querySelector<HTMLDetailsElement>(":scope > details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector(":scope > summary")?.textContent).toBe("Details and settings");
    expect([...details.querySelector(".voice-details-body")!.children].map((el) => el.className))
      .toEqual(["voice-engines", "voice-recent", "voice-settings", "voice-mic"]);
    expect(voice.querySelector(":scope > .voice-top .voice-state")?.textContent).toBe("Resting");
    expect(voice.querySelector(":scope > .voice-helper")?.textContent).toContain("echo cancelling on");
    expect(voice.querySelector(":scope > .voice-last h3")?.textContent).toBe("Last heard");
  });
});
