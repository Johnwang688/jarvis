// A brief is a two-line row until the owner opens it.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedPanel } from "../src/components/feed/FeedPanel";
import type { Brief, FeedSnapshot } from "../src/components/feed/feedApi";

const decision: Brief = {
  id: "brief-1", kind: "decision", rank: 4, due: null,
  ask: "Publish the sample report?", why_now: "The review window closes Thursday.",
  recommend: { label: "Publish Thursday", because: "the checklist is complete" },
  options: [{ label: "Wait for Friday" }],
  default: { action: "none", at: null },
  context: [{ label: "Sample checklist", ref: "https://example.invalid/checklist" }],
  state: "open", feedback: [],
};
const news: Brief = {
  ...decision, id: "brief-2", kind: "news", rank: 5, ask: "The sample release is ready.",
  recommend: null, options: [], default: null,
};
const urgent: Brief = { ...decision, id: "brief-3", rank: 2, ask: "Sign the sample form today?" };
const closed: Brief = { ...decision, id: "brief-4", state: "approved", ask: "Order the sample parts?" };

const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>();
let box: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  box = document.body.appendChild(document.createElement("div"));
  root = createRoot(box);
});

afterEach(() => {
  act(() => root.unmount());
  box.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mount(briefs: Brief[], recent: Brief[] = []) {
  fetchMock.mockImplementation(async () => new Response(JSON.stringify({ briefs, recent } satisfies FeedSnapshot)));
  await act(async () => root.render(<FeedPanel />));
}

const card = (ask: string) => [...box.querySelectorAll("article")].find((item) => item.querySelector("h3,h4")?.textContent === ask)!;
const toggle = (ask: string) => card(ask).querySelector<HTMLButtonElement>(".feed-toggle")!;
const shown = (scope: Element, selector: string) =>
  [...scope.querySelectorAll<HTMLElement>(selector)].filter((item) => !item.closest("[hidden]"));
const labels = (scope: Element) => shown(scope, "button").map((item) => item.textContent);

describe("a brief row", () => {
  it("starts closed: the ask, one line of why, and no answers for a decision", async () => {
    await mount([decision]);
    const row = card(decision.ask);
    expect(row.hasAttribute("data-collapsed")).toBe(true);
    expect(toggle(decision.ask).getAttribute("aria-expanded")).toBe("false");
    expect(labels(row)).toEqual([decision.ask]);
    expect(shown(row, ".feed-why")[0].textContent).toContain(decision.why_now);
    expect(shown(row, "details, a, .feed-feedback")).toHaveLength(0);
    expect(row.querySelector(".feed-more")!.hasAttribute("hidden")).toBe(true);
    expect(toggle(decision.ask).getAttribute("aria-controls"))
      .toBe(`${row.querySelector(".feed-actions")!.id} ${row.querySelector(".feed-more")!.id}`);
  });

  it("opens from its title and closes again", async () => {
    await mount([decision]);
    await act(async () => toggle(decision.ask).click());
    const row = card(decision.ask);
    expect(row.hasAttribute("data-collapsed")).toBe(false);
    expect(toggle(decision.ask).getAttribute("aria-expanded")).toBe("true");
    expect(labels(row)).toEqual([decision.ask, "Approve", "Edit", "Other…", "Not now", "Too long", "Missing context"]);
    expect(shown(row, "details")).toHaveLength(1);
    await act(async () => toggle(decision.ask).click());
    expect(row.hasAttribute("data-collapsed")).toBe(true);
    expect(labels(row)).toEqual([decision.ask]);
  });

  it("opens from a press on the row, and a press on an open row does not close it", async () => {
    await mount([decision]);
    const row = card(decision.ask);
    await act(async () => row.querySelector<HTMLElement>(".feed-why")!.click());
    expect(row.hasAttribute("data-collapsed")).toBe(false);
    await act(async () => row.querySelector<HTMLElement>(".feed-why")!.click());
    expect(row.hasAttribute("data-collapsed")).toBe(false);
  });

  it("keeps Got it on a closed news row, and pressing it does not open the row", async () => {
    await mount([news]);
    const row = card(news.ask);
    expect(labels(row)).toEqual([news.ask, "Got it"]);
    await act(async () => shown(row, ".feed-actions button")[0].click());
    expect(fetchMock).toHaveBeenLastCalledWith("/v3/owner/feed/answer", expect.objectContaining({
      body: JSON.stringify({ id: news.id, action: "not_now" }),
    }));
    expect(row.hasAttribute("data-collapsed")).toBe(true);
  });

  it("starts an urgent decision open, and lets the owner close it", async () => {
    await mount([urgent, decision]);
    expect(card(urgent.ask).hasAttribute("data-collapsed")).toBe(false);
    expect(labels(card(urgent.ask)).slice(1, 5)).toEqual(["Approve", "Edit", "Other…", "Not now"]);
    expect(card(decision.ask).hasAttribute("data-collapsed")).toBe(true);
    await act(async () => toggle(urgent.ask).click());
    expect(card(urgent.ask).hasAttribute("data-collapsed")).toBe(true);
  });

  it("stays open while an answer is being written, whatever the title says", async () => {
    await mount([decision]);
    await act(async () => toggle(decision.ask).click());
    await act(async () => shown(card(decision.ask), ".feed-actions button")[1].click());
    expect(box.querySelector("textarea")).not.toBeNull();
    await act(async () => toggle(decision.ask).click());
    expect(card(decision.ask).hasAttribute("data-collapsed")).toBe(false);
    expect(shown(card(decision.ask), "textarea")).toHaveLength(1);
  });

  it("keeps its place across polls", async () => {
    await mount([decision, news]);
    await act(async () => toggle(decision.ask).click());
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(card(decision.ask).hasAttribute("data-collapsed")).toBe(false);
    expect(card(news.ask).hasAttribute("data-collapsed")).toBe(true);
  });

  it("shows a closed brief as a row with its outcome", async () => {
    await mount([], [closed]);
    const row = card(closed.ask);
    expect(row.hasAttribute("data-collapsed")).toBe(true);
    expect(labels(row)).toEqual([closed.ask]);
    expect(shown(row, ".feed-chips span").map((chip) => chip.textContent)).toContain("Approved");
  });

  it("a closed decision takes no answer from the keyboard, an open one does", async () => {
    await mount([decision]);
    const press = async (value: string) => act(async () => {
      toggle(decision.ask).dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true }));
    });
    toggle(decision.ask).focus();
    const before = fetchMock.mock.calls.length;
    for (const value of ["a", "n", "e"]) await press(value);
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    expect(fetchMock.mock.calls.length).toBe(before);
    expect(box.querySelector("textarea")).toBeNull();
    await act(async () => toggle(decision.ask).click());
    await press("a");
    expect(fetchMock).toHaveBeenLastCalledWith("/v3/owner/feed/answer", expect.objectContaining({
      body: JSON.stringify({ id: decision.id, action: "approve" }),
    }));
  });

  it("the second press of a double-click never answers", async () => {
    await mount([decision]);
    await act(async () => toggle(decision.ask).click());
    const approve = shown(card(decision.ask), ".feed-actions button")[0];
    await act(async () => { approve.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 2 })); });
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    await act(async () => { approve.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 })); });
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  });

  it("opening a row moves no focus", async () => {
    const field = document.body.appendChild(document.createElement("input"));
    await mount([urgent, decision]);
    field.focus();
    await act(async () => card(decision.ask).querySelector<HTMLElement>(".feed-why")!.click());
    expect(card(decision.ask).hasAttribute("data-collapsed")).toBe(false);
    expect(document.activeElement).toBe(field);
    field.remove();
  });
});
