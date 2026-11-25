import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkPanel } from "../src/components/work/WorkPanel";

const now = Date.UTC(2030, 0, 2, 12);
const thread = {
  thread_id: "fixture-thread", project: "alpha", title: "Fixture title", status: "running",
  waiting_on_owner: false, started_at: now / 1000 - 60, updated_at: now / 1000 - 5,
};
const snapshot = { projects: [{ name: "alpha" }], threads: [thread] };
const fetchWork = vi.fn<() => Promise<Response>>();
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchWork);
  vi.useFakeTimers();
  vi.setSystemTime(now);
  fetchWork.mockReset().mockImplementation(async () => Response.json(snapshot));
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const mount = () => act(async () => { root.render(<WorkPanel />); });
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

it("coalesces visibility changes during a poll into one immediate recheck", async () => {
  let finish!: (response: Response) => void;
  fetchWork.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  await mount();
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(fetchWork).toHaveBeenCalledTimes(1);
  await act(async () => { finish(Response.json(snapshot)); });
  expect(fetchWork).toHaveBeenCalledTimes(2);
  await advance(9_999);
  expect(fetchWork).toHaveBeenCalledTimes(2);
  await advance(1);
  expect(fetchWork).toHaveBeenCalledTimes(3);
});

it("clamps a future update to zero and reports day-old updates in days and hours", async () => {
  fetchWork.mockResolvedValueOnce(Response.json({ ...snapshot, threads: [
    { ...thread, updated_at: now / 1000 + 60 },
    { ...thread, thread_id: "older", started_at: now / 1000 - 100_000, updated_at: now / 1000 - 93_600 },
  ] }));
  await mount();
  expect([...host.querySelectorAll(".work-when")].map((item) => item.textContent)).toEqual(["0 s ago", "1 d 2 h ago"]);
});

it("removes all bidi controls from a title without dropping its row", async () => {
  fetchWork.mockResolvedValueOnce(Response.json({ ...snapshot, threads: [
    { ...thread, title: "\u061c\u200e\u200f\u202e\u2066\u2069" },
  ] }));
  await mount();
  expect(host.querySelectorAll("li")).toHaveLength(1);
  expect(host.querySelector(".work-title")?.textContent).toBe("");
});

it("ignores extra keys in the snapshot and its rows", async () => {
  fetchWork.mockResolvedValueOnce(Response.json({
    extra: "fixture-hidden-snapshot",
    projects: [{ name: "alpha", path: "fixture-hidden-project" }],
    threads: [{ ...thread, branch: "fixture-hidden-thread" }],
  }));
  await mount();
  expect(host.querySelector("h3")?.textContent).toBe("alpha");
  expect(host.querySelector(".work-title")?.textContent).toBe(thread.title);
  expect(host.innerHTML).not.toContain("fixture-hidden");
});

it("clears rows on 503 and recovers on the next successful poll", async () => {
  await mount();
  expect(host.querySelectorAll("li")).toHaveLength(1);
  fetchWork.mockResolvedValueOnce(Response.json({ error: "fixture detail" }, { status: 503 }));
  await advance(10_000);
  expect(host.querySelectorAll("li")).toHaveLength(0);
  expect(host.querySelector("p")?.textContent).toBe("T3 threads could not be read.");
  await advance(10_000);
  expect(host.querySelector(".work-title")?.textContent).toBe(thread.title);
});
