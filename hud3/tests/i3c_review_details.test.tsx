import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Brief } from "../src/components/feed/feedApi";

const brief: Brief = {
  id: "fixture-brief", proposal_id: "fixture-proposal:fixture-hash", kind: "decision", rank: 4,
  ask: "Start the fixture work?", due: null, why_now: "Fixture is ready.", recommend: null,
  options: [], default: { action: "none", at: null }, context: [], feedback: [], state: "open",
};
const source = "javascript:fixture(<b>source</b>)";
const longBrief = "x".repeat(15_999) + "!";
const proposal = {
  kind: "thread.start", level: "L2", why: "Fixture policy", tainted: true, state: "briefed",
  basis: [{ type: "thread", id: source + "\u202e" }],
  action: { tool: "work_start", card: {
    title: "Fixture work", rows: [{ label: "Brief", value: longBrief }],
  } },
};

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const disclosure = () => host.querySelector<HTMLDetailsElement>(".feed-proposal")!;
const shortcut = (key: string) =>
  host.querySelector<HTMLButtonElement>(`button[aria-keyshortcuts="${key}"]`)!;

async function mount(details: () => Promise<Response>, surface: "window" | "phone" = "window") {
  const { setSurface } = await import("../src/api");
  const { FeedPanel } = await import("../src/components/feed/FeedPanel");
  setSurface(surface);
  const prefix = surface === "phone" ? "/m/api" : "/v3/owner";
  const fetch = vi.fn(async (path: string) => {
    if (path === `${prefix}/feed`) return Response.json({ briefs: [brief], recent: [] });
    if (path === `${prefix}/proposal`) return details();
    throw new Error("Unexpected fixture request");
  });
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<FeedPanel />));
  await act(async () => host.querySelector<HTMLButtonElement>(".feed-toggle")!.click());
  return fetch;
}

async function toggleDetails() {
  await act(async () => disclosure().querySelector("summary")!.click());
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}

it.each(["window", "phone"] as const)("keeps actions disabled if details finish hidden on %s", async (surface) => {
  let respond!: (response: Response) => void;
  const fetch = await mount(() => new Promise<Response>((resolve) => { respond = resolve; }), surface);
  expect(shortcut("A")).toHaveProperty("disabled", true);
  expect(shortcut("E")).toHaveProperty("disabled", true);
  await toggleDetails();
  expect(fetch.mock.calls.some(([path]) => path.endsWith("/proposal"))).toBe(true);
  await toggleDetails();
  expect(disclosure().open).toBe(false);
  await act(async () => respond(Response.json({ proposal })));
  expect(disclosure().open).toBe(false);
  expect([shortcut("A").disabled, shortcut("E").disabled]).toEqual([true, true]);
});

it("keeps actions disabled when a decision receives a cardless proposal", async () => {
  await mount(async () => Response.json({ proposal: { ...proposal, action: null } }));
  await toggleDetails();
  expect([shortcut("A").disabled, shortcut("E").disabled]).toEqual([true, true]);
});

it("shows the proposal basis as plain text after the taint notice", async () => {
  await mount(async () => Response.json({ proposal }));
  await toggleDetails();
  expect(disclosure().querySelector("summary + p")?.textContent).toBe("Based on untrusted text");
  expect(disclosure().textContent).toContain(longBrief);
  expect(disclosure().querySelector("a, b")).toBeNull();
  expect(disclosure().textContent).not.toContain("\u202e");
  expect(disclosure().textContent?.includes(source), "The proposal basis should be visible as text").toBe(true);
});
