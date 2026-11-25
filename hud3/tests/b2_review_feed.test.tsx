import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FeedPanel } from "../src/components/feed/FeedPanel";
import type { Brief } from "../src/components/feed/feedApi";

const path = "/v3/owner/feed";
const brief: Brief = {
  id: "brief-1", kind: "decision", rank: 3, due: null,
  ask: "Publish the sample report?", why_now: "The review window is closing.",
  recommend: null, options: [], default: { action: "none", at: null },
  context: [], state: "open", feedback: [],
};
const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>();
let box: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  fetchMock.mockReset();
  box = document.body.appendChild(document.createElement("div"));
  root = createRoot(box);
});

afterEach(() => {
  act(() => root.unmount());
  box.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const json = (body: unknown, status = 200) => Response.json(body, { status });

function mount(open: Brief) {
  fetchMock.mockResolvedValueOnce(json({ briefs: [open], recent: [] }));
  return act(async () => root.render(<FeedPanel />));
}

function button(label: string) {
  return [...box.querySelectorAll("button")].find((item) => item.textContent === label);
}

function refresh() {
  window.dispatchEvent(new Event("blur"));
  window.dispatchEvent(new Event("focus"));
}

it("MED: leaves a credentialed context reference as text", async () => {
  const ref = "https://user:***@example.invalid/source";
  const plain = "https://example.invalid/source";
  await mount({ ...brief, context: [{ label: "Imported source", ref }, { label: "Public source", ref: plain }] });
  const context = box.querySelector("details")!;
  await act(async () => { context.open = true; });
  expect(context.textContent).toContain(ref);
  expect([...context.querySelectorAll("a")].map((link) => link.href)).toEqual([plain]);
});

it("MED: stops offering Approve when a refresh sees an answer during feedback", async () => {
  const closed = { ...brief, state: "dismissed", feedback: ["too_long"] };
  let finishFeedback!: (response: Response) => void;
  fetchMock.mockImplementation((url) => {
    if (url === `${path}/feedback`) {
      return new Promise((resolve) => { finishFeedback = resolve; });
    }
    if (url === path) return Promise.resolve(json({ briefs: [], recent: [closed] }));
    return Promise.resolve(json({ error: "Feed operation refused." }, 400));
  });
  await mount(brief);
  await act(async () => button("Too long")!.click());
  await act(async () => refresh());
  await act(async () => finishFeedback(json({ brief: { ...brief, feedback: ["too_long"] } })));
  await act(async () => button("Approve")?.click());
  const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(posts.map(([url, init]) => [url, JSON.parse(String(init?.body))]))
    .toEqual([[`${path}/feedback`, { id: brief.id, tag: "too_long" }]]);
  expect(box.textContent).toContain("Not now");
});

it("MED: keeps an unsent answer available to copy when the brief expires during editing", async () => {
  await mount(brief);
  await act(async () => button("Other…")!.click());
  const editor = box.querySelector("textarea")!;
  const draft = "Use the revised sample after the final review.";
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(editor, draft);
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(document.activeElement).toBe(editor);
  expect(button("Submit")?.disabled).toBe(false);

  fetchMock.mockResolvedValueOnce(json({ briefs: [], recent: [{ ...brief, state: "expired" }] }));
  await act(async () => refresh());
  expect(box.textContent).toContain("Expired");
  expect(button("Submit")?.disabled ?? true).toBe(true);
  expect(box.querySelector("textarea")?.value ?? box.textContent).toContain(draft);
});

it("LOW: limits rapid focus-triggered retries after a 503", async () => {
  fetchMock.mockImplementation(async () => json({}, 503));
  await act(async () => root.render(<FeedPanel />));
  expect(box.textContent).toContain("Feed is busy");

  for (let second = 0; second < 5; second++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
      refresh();
    });
  }
  expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2);
});
