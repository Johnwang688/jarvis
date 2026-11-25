import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedPanel } from "../src/components/feed/FeedPanel";
import type { Brief, FeedSnapshot, FeedbackTag } from "../src/components/feed/feedApi";

const path = "/v3/owner/feed";
const decision: Brief = {
  id: "brief-1", kind: "decision", rank: 1, due: "2026-11-26T17:00:00Z",
  ask: "Publish the sample report?", why_now: "The review window closes Thursday.",
  recommend: { label: "Publish Thursday", because: "the checklist is complete" },
  options: [{ label: "Wait for Friday" }, { label: "Archive the draft" }],
  default: { action: "none", at: "2026-11-27T17:00:00Z" },
  context: [{ label: "Sample checklist", ref: "https://example.invalid/checklist" }],
  state: "open", feedback: [],
};
const news: Brief = {
  ...decision, id: "brief-2", kind: "news", rank: 2, ask: "The sample release is ready.",
  due: null, recommend: null, options: [], default: null, context: [],
};
const digest: Brief = { ...news, id: "brief-3", kind: "digest", rank: 6, ask: "Two sample tasks closed." };

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>();
let box: HTMLElement;
let root: Root;
let hidden: boolean;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
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

function returns(briefs: Brief[], recent: Brief[] = []) {
  fetchMock.mockImplementation(async () => json({ briefs, recent } satisfies FeedSnapshot));
}

async function mount() {
  await act(async () => root.render(<FeedPanel />));
}

function button(label: string, scope: ParentNode = box) {
  const match = [...scope.querySelectorAll("button")].find((item) => item.textContent === label);
  expect(match, label).toBeDefined();
  return match!;
}

function cards(scope: ParentNode = box) {
  return [...scope.querySelectorAll("article")];
}

function recent() {
  return box.querySelector<HTMLElement>('[aria-labelledby="feed-recent"]')!;
}

async function click(label: string, scope: ParentNode = box) {
  await act(async () => button(label, scope).click());
}

async function key(target: EventTarget, value: string, extra: KeyboardEventInit = {}) {
  await act(async () => {
    target.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true, ...extra }));
  });
}

function expectPost(route: string, body: object) {
  expect(fetchMock).toHaveBeenLastCalledWith(`${path}/${route}`, expect.objectContaining({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
  expect(fetchMock.mock.lastCall?.[1]).not.toHaveProperty("credentials");
}

describe("briefs", () => {
  it("renders all kinds in order with top actions, collapsed context and filters", async () => {
    returns([decision, news, digest]);
    await mount();
    const [first, second, third] = cards();
    expect([...first.firstElementChild!.querySelectorAll("button")].map((item) => item.textContent))
      .toEqual(["Approve", "Edit", "Other…", "Not now"]);
    const text = first.textContent!;
    const fields = ["Publish the sample report?", "Due", "Rank 1", "Why now", "Recommend", "the checklist is complete",
      "Other options", "If you ignore it", "No action. Closes after", "Context"];
    let previous = -1;
    for (const field of fields) {
      const index = text.indexOf(field);
      expect(index, field).toBeGreaterThan(previous);
      previous = index;
    }
    expect(first.querySelector("details")?.open).toBe(false);
    expect(first.classList.contains("feed-priority")).toBe(true);
    expect(second.classList.contains("feed-priority")).toBe(true);
    expect(third.classList.contains("feed-priority")).toBe(false);
    for (const item of [second, third]) {
      expect([...item.querySelectorAll(".feed-actions button")].map((entry) => entry.textContent)).toEqual(["Got it"]);
    }
    for (const [filter, ask] of [["Decisions", decision.ask], ["News", news.ask], ["Digests", digest.ask]]) {
      await click(filter);
      expect(cards()).toHaveLength(1);
      expect(cards()[0].textContent).toContain(ask);
    }
    await click("All");
    expect(cards()).toHaveLength(3);
  });

  it("shows one calm line when empty", async () => {
    returns([]);
    await mount();
    expect(box.querySelector('[role="status"]')?.textContent).toBe("No open briefs.");
    expect(cards()).toHaveLength(0);
    expect(box.querySelector("button")).toBeNull();
  });

  it("renders hostile strings as inert, isolated text and only links HTTPS sources", async () => {
    const ask = "<img src=x onerror=alert(1)>";
    const longRef = "x".repeat(2048);
    returns([{
      ...decision, ask, why_now: "Line one\nLine two\u202e",
      context: [
        { label: "<svg onload=alert(1)>", ref: "https://example.invalid/checklist" },
        { label: "Insecure", ref: "http://example.invalid" },
        { label: "Script", ref: "javascript:alert(1)" },
        { label: "Data", ref: "data:text/html,example" },
        { label: "Relative", ref: "//example.invalid/checklist" },
        { label: "Malformed", ref: "https://" },
        { label: "Other", ref: longRef },
      ],
    }]);
    await mount();
    const [card] = cards();
    expect(card.querySelector("h3 bdi")?.textContent).toBe(ask);
    expect(card.querySelector("img,svg,[style]")).toBeNull();
    expect(card.textContent).toContain("Line one\nLine two");
    expect(card.textContent).not.toContain("\u202e");
    expect(card.textContent).toContain(longRef);
    expect(card.firstElementChild?.querySelector("button")?.textContent).toBe("Approve");
    const links = [...card.querySelectorAll("a")];
    expect(links).toHaveLength(1);
    expect(links[0].href).toBe("https://example.invalid/checklist");
    expect(links[0].textContent).toBe("https://example.invalid/checklist");
    expect(links[0].target).toBe("_blank");
    expect(links[0].rel).toBe("noreferrer noopener");
    expect(card.textContent).toContain("javascript:alert(1)");
  });
});

describe("writes", () => {
  it.each([
    ["Approve", "approve", "approved", decision, undefined],
    ["Edit", "edit", "edited", decision, "Change the sample date."],
    ["Other…", "other", "other", decision, "Use the sample template."],
    ["Not now", "not_now", "dismissed", decision, undefined],
    ["Got it", "not_now", "dismissed", news, undefined],
    ["Got it", "not_now", "dismissed", digest, undefined],
  ] as const)("%s sends %s and moves the card to Recent (%#)", async (label, action, state, brief, text) => {
    returns([brief]);
    await mount();
    fetchMock.mockResolvedValueOnce(json({ brief: { ...brief, state }, executed: false }));
    await click(label, cards()[0]);
    if (text) {
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const textarea = box.querySelector("textarea")!;
      expect(document.activeElement).toBe(textarea);
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, text);
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await click("Submit");
    }

    expectPost("answer", { id: brief.id, action, ...(text ? { text } : {}) });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(cards(recent())).toEqual(cards());
    expect(cards(recent())[0].textContent).toContain(brief.ask);
    expect(recent().querySelector(".feed-actions")).toBeNull();
  });

  it("records each feedback tag once, including on recent briefs", async () => {
    const answered: Brief = { ...decision, state: "approved" };
    returns([], [answered]);
    await mount();
    const tags: FeedbackTag[] = [];
    for (const [label, tag] of [["Too long", "too_long"], ["Missing context", "missing_context"]] as const) {
      tags.push(tag);
      fetchMock.mockResolvedValueOnce(json({ brief: { ...answered, feedback: [...tags] } }));
      await click(label, recent());
      expectPost("feedback", { id: answered.id, tag });
      expect(button(label, recent()).getAttribute("aria-pressed")).toBe("true");
      expect(button(label, recent()).disabled).toBe(true);
      await click(label, recent());
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps a card after a failed answer and lets the owner retry", async () => {
    returns([decision]);
    await mount();
    fetchMock.mockResolvedValueOnce(json({ error: "<img>" }, 503));
    await click("Approve");
    expect(box.textContent).toContain("Feed is busy.");
    expect(box.textContent).not.toContain("<img>");
    expect(recent()).toBeNull();
    fetchMock.mockResolvedValueOnce(json({ brief: { ...decision, state: "approved" } }));
    await click("Approve");
    expect(cards(recent())).toHaveLength(1);
    expect(box.textContent).not.toContain("Feed is busy.");
  });

  it("ignores a stale poll while preventing duplicate submissions", async () => {
    returns([decision]);
    await mount();
    let finishPoll!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { finishPoll = resolve; }));
    await act(async () => window.dispatchEvent(new Event("focus")));
    let finishWrite!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { finishWrite = resolve; }));
    await click("Approve");
    await click("Approve");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await act(async () => finishWrite(json({ brief: { ...decision, state: "approved" } })));
    await act(async () => finishPoll(json({ briefs: [decision], recent: [] })));
    expect(cards(recent())).toEqual(cards());
  });
});

describe("keyboard", () => {
  it.each([["a", "approve", "approved"], ["n", "not_now", "dismissed"]] as const)(
    "%s acts only on the focused decision card",
    async (shortcut, action, state) => {
      const second = { ...decision, id: "second", ask: "Review the sample timeline?" };
      returns([decision, second, news]);
      await mount();
      const [, card, newsCard] = cards();
      await key(card, shortcut);
      button("All").focus();
      await key(button("All"), shortcut);
      button("Got it", newsCard).focus();
      await key(button("Got it", newsCard), shortcut);
      card.focus();
      await key(card, shortcut, { ctrlKey: true });
      await key(card, shortcut, { repeat: true });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      fetchMock.mockResolvedValueOnce(json({ brief: { ...second, state } }));
      await key(card, shortcut);
      expectPost("answer", { id: second.id, action });
      expect(cards(recent())[0].textContent).toContain(second.ask);
    },
  );

  it("opens Edit with E and ignores every shortcut while typing", async () => {
    returns([decision]);
    await mount();
    const [card] = cards();
    await key(card, "e");
    expect(box.querySelector("textarea")).toBeNull();
    button("Approve").focus();
    await key(button("Approve"), "e");
    const textarea = box.querySelector("textarea")!;
    expect(textarea).toBe(document.activeElement);
    for (const letter of ["a", "n", "e"]) await key(textarea, letter);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(box.querySelector("textarea")).toBe(textarea);
    await click("Cancel");
    expect(box.querySelector("textarea")).toBeNull();
  });
});

describe("polling", () => {
  it.each([
    [() => json({}, 403), "Feed access denied."],
    [() => json({}, 503), "Feed is busy."],
    [() => { throw new TypeError("mock network failure"); }, "Feed unavailable."],
    [() => json({ briefs: [{}], recent: [] }), "Feed unavailable."],
  ] as const)("shows a plain failure and retries on the next poll (%#)", async (fail, message) => {
    returns([]);
    fetchMock.mockImplementationOnce(async () => fail());
    await mount();
    expect(box.textContent).toContain(message);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(box.textContent).not.toContain(message);
    expect(box.textContent).toContain("No open briefs.");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("pauses while hidden and rechecks on visibility and focus without moving focus", async () => {
    returns([decision]);
    const outside = document.body.appendChild(document.createElement("input"));
    outside.focus();
    try {
      await mount();
      expect(document.activeElement).toBe(outside);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledWith(path, expect.not.objectContaining({ method: "POST" }));
      hidden = true;
      document.dispatchEvent(new Event("visibilitychange"));
      await act(async () => vi.advanceTimersByTimeAsync(90_000));
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(fetchMock).toHaveBeenCalledTimes(1);

      hidden = false;
      await act(async () => document.dispatchEvent(new Event("visibilitychange")));
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(document.activeElement).toBe(outside);
      button("Approve").focus();
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(fetchMock).toHaveBeenCalledTimes(3);
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(fetchMock).toHaveBeenCalledTimes(4);
      expect(document.activeElement).toBe(button("Approve"));
    } finally {
      outside.remove();
    }
  });
});
