import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "../src/components/chat/ChatPanel";

const path = "/v3/owner/chat";
const idle = { state: "idle", session_id: "sample", seq: 0, history: [], draft: "", tools: [] };
const running = { ...idle, state: "running" };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const batch = (state: string, seq: number, events: object[] = []) => json({ state, seq, gap: false, events });
const user = (text: string, seq = 1) => ({ kind: "user", text, seq });
const fetchMock = vi.fn<(url: string, options: RequestInit) => Promise<Response>>();

let box: HTMLElement;
let root: Root;
let hidden: boolean;
let snapshot: object;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  hidden = false;
  snapshot = idle;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    if (url === path) return json(snapshot);
    throw new Error("Unexpected request");
  });
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

const mount = () => act(async () => root.render(<ChatPanel />));
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const field = () => box.querySelector("textarea")!;
const transcript = () => [...box.querySelectorAll(".chat-text")].map((item) => item.textContent);
const button = (label: string) => [...box.querySelectorAll("button")].find((item) => item.textContent === label)!;
const click = (label: string) => act(async () => button(label).click());

async function type(text: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(field(), text);
    field().dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function key(extra: KeyboardEventInit = {}) {
  const event = new KeyboardEvent("keydown", { key: "Enter", cancelable: true, bubbles: true, ...extra });
  await act(async () => { field().dispatchEvent(event); });
  return event.defaultPrevented;
}

async function visibility(value: boolean) {
  hidden = value;
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
}

function requests(route = "") {
  return fetchMock.mock.calls.filter(([url]) => url === path + route).map(([, options]) => options);
}

function expectPost(route: string, body: object) {
  expect(requests(route)).toEqual([expect.objectContaining({
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    redirect: "error", cache: "no-store",
  })]);
}

describe("chat actions", () => {
  it("loads idle and sends exactly once with Enter, preserving newlines", async () => {
    fetchMock.mockResolvedValueOnce(json(idle)).mockResolvedValueOnce(json({ state: "running", steered: false }, 202));
    await mount();
    expect(box.textContent).toContain("No messages yet.");
    expect(button("New chat").disabled).toBe(false);
    expect(button("Send").disabled).toBe(true);
    expect(button("Stop")).toBeUndefined();
    expect(requests()).toHaveLength(1);
    await type("First line\nSecond line");
    expect(await key({ shiftKey: true })).toBe(false);
    expect(await key({ isComposing: true })).toBe(false);
    expect(await key({ keyCode: 229 } as KeyboardEventInit)).toBe(false);
    await act(async () => { field().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })); });
    expect(await key()).toBe(false);
    await act(async () => { field().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })); });
    expect(requests("/send")).toHaveLength(0);
    expect(await key()).toBe(true);
    expectPost("/send", { text: "First line\nSecond line" });
    expect(field().value).toBe("");
    expect(box.textContent).toContain("Jarvis is working");
    expect(button("New chat").disabled).toBe(true);
  });

  it("steers a running turn and displays the event in the transcript", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ ...running, seq: 1, history: [{ role: "user", text: "Check the sample." }] }))
      .mockResolvedValueOnce(json({ state: "running", steered: true }, 202))
      .mockResolvedValueOnce(batch("running", 2, [{ seq: 2, kind: "steer", text: "Use the small example." }]))
      .mockResolvedValueOnce(json({ ...running, seq: 2 }));
    await mount();
    await type("Use the small example.");
    await click("Send");
    expectPost("/send", { text: "Use the small example." });
    expect(box.textContent).toContain("Message added to this turn.");
    await advance(700);
    expectPost("/events", { after: 1 });
    expect(transcript()).toEqual(["Check the sample.", "Use the small example."]);
    expect(box.querySelectorAll("article")[1].textContent).toContain("Added to this turn");
  });

  it("stops once and shows the stopped event even when Stop returns idle", async () => {
    fetchMock.mockResolvedValueOnce(json(running)).mockResolvedValueOnce(json({ state: "idle" }))
      .mockResolvedValueOnce(batch("idle", 1, [{ seq: 1, kind: "stopped" }]));
    await mount();
    await click("Stop");
    expectPost("/stop", {});
    await advance(700);
    expectPost("/events", { after: 0 });
    expect(box.textContent).toContain("Turn stopped.");
    expect(button("Stop")).toBeUndefined();
    expect(button("New chat").disabled).toBe(false);
  });

  it("shows stopping while waiting for the turn to finish", async () => {
    fetchMock.mockResolvedValueOnce(json(running)).mockResolvedValueOnce(json({ state: "stopping" }));
    await mount();
    await click("Stop");
    expect(box.textContent).toContain("Jarvis is stopping");
    expect(button("Stop").disabled).toBe(true);
    expect(button("New chat").disabled).toBe(true);
  });

  it("starts a new chat once and clears the transcript", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...idle, history: [{ role: "assistant", text: "Old reply" }] }))
      .mockResolvedValueOnce(json({ session_id: "new-sample" }));
    await mount();
    await click("New chat");
    expectPost("/new", {});
    expect(transcript()).toEqual([]);
    expect(box.textContent).toContain("No messages yet.");
  });

  it.each([400, 409, 429, 503])("keeps text after a %i send failure", async (status) => {
    fetchMock.mockResolvedValueOnce(json(idle)).mockResolvedValueOnce(json({ error: "private error" }, status));
    await mount();
    await type("Keep this text");
    await click("Send");
    expectPost("/send", { text: "Keep this text" });
    expect(field().value).toBe("Keep this text");
    expect(box.querySelector(".chat-error")).not.toBeNull();
    expect(box.textContent).not.toContain("private error");
  });

  it("shows the limit near 16,000 and counts Unicode characters", async () => {
    await mount();
    const allowed = "a".repeat(15_999) + "🌱";
    await type(allowed);
    expect(box.textContent).toContain("16,000 / 16,000 characters");
    expect(button("Send").disabled).toBe(false);
    await type(allowed + "a");
    expect(button("Send").disabled).toBe(true);
    await key();
    expect(requests("/send")).toHaveLength(0);
  });
});

describe("chat polling", () => {
  it("shows draft and tool updates, replaces the draft with the final reply, and leaves focus alone", async () => {
    const outside = document.body.appendChild(document.createElement("button"));
    outside.focus();
    fetchMock.mockResolvedValueOnce(json(running))
      .mockResolvedValueOnce(batch("running", 2, [
        user("Summarize the sample."),
        { seq: 2, kind: "tool_start", name: "sample_lookup" },
      ]))
      .mockResolvedValueOnce(json({ ...running, seq: 2, draft: "Checking the sample…" }))
      .mockResolvedValueOnce(batch("running", 4, [
        { seq: 3, kind: "tool_end", name: "sample_lookup", ok: true, summary: "Found two rows" },
        { seq: 4, kind: "tool_end", name: "sample_optional", ok: false, summary: "No optional rows" },
      ]))
      .mockResolvedValueOnce(json({ ...running, seq: 4, draft: "Both rows look ready." }))
      .mockResolvedValueOnce(batch("idle", 5, [{ seq: 5, kind: "reply", text: "Two rows are ready." }]));
    try {
      await mount();
      expect(document.activeElement).toBe(outside);
      await advance(699);
      expect(requests("/events")).toHaveLength(0);
      await advance(1);
      expect(transcript()).toEqual(["Summarize the sample.", "Checking the sample…"]);
      expect(box.querySelector(".chat-tools")?.textContent).toContain("sample_lookup · Running");
      field().focus();
      await advance(700);
      expect(document.activeElement).toBe(field());
      expect(transcript()).toEqual(["Summarize the sample.", "Both rows look ready."]);
      expect(box.querySelector(".chat-tools")?.textContent).toContain("sample_lookup · OK · Found two rows");
      expect(box.querySelector(".chat-tools")?.textContent).toContain("sample_optional · Failed · No optional rows");
      outside.focus();
      await advance(700);
      expect(document.activeElement).toBe(outside);
      expect(box.querySelector("[data-draft]")).toBeNull();
      expect(transcript()).toEqual(["Summarize the sample.", "Two rows are ready."]);
      expect(requests("/events").map(({ body }) => body)).toEqual(['{"after":0}', '{"after":2}', '{"after":4}']);
    } finally {
      outside.remove();
    }
  });

  it("renders hostile messages, draft and tools as text only", async () => {
    const hostile = '<img src="https://example.invalid/pixel" onerror="alert(1)">\nhttps://example.invalid/\u202e' + "x".repeat(500);
    snapshot = { ...running, draft: hostile, tools: [{ name: "<script>", ok: false, summary: hostile }],
      history: [{ role: "user", text: hostile }, { role: "assistant", text: hostile }] };
    await mount();
    expect(transcript()).toEqual([hostile, hostile, hostile]);
    expect(box.querySelector(".chat-tools")?.textContent).toContain(hostile);
    expect(box.querySelectorAll("img, a, iframe, script, [onerror]")).toHaveLength(0);
  });

  it("shows a generic turn error and leaves the composer ready to retry", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...running, draft: "Unfinished" }))
      .mockResolvedValueOnce(batch("error", 1, [{ seq: 1, kind: "error", error: "secret details" }]));
    await mount();
    await advance(700);
    expect(box.textContent).toContain("The turn failed. Try sending a message again.");
    expect(box.textContent).not.toContain("secret details");
    expect(transcript()).toEqual([]);
    expect(button("New chat").disabled).toBe(true);
    await type("Try again");
    expect(button("Send").disabled).toBe(false);
  });

  it("polls every five seconds while idle and pauses when hidden", async () => {
    await mount();
    expect(requests()).toHaveLength(1);
    await advance(4_999);
    expect(requests()).toHaveLength(1);
    await advance(1);
    expect(requests()).toHaveLength(2);
    await visibility(true);
    await advance(25_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await visibility(false);
    await advance(0);
    expect(requests()).toHaveLength(3);
  });

  it("does not pile up running polls and takes no draft snapshot while hidden", async () => {
    let finish!: (response: Response) => void;
    fetchMock.mockResolvedValueOnce(json(running)).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await mount();
    await advance(700);
    await advance(4_000);
    expectPost("/events", { after: 0 });
    await visibility(true);
    expect(requests("/events")[0].signal?.aborted).toBe(true);
    await act(async () => { finish(batch("running", 1, [user("Hidden result")])); });
    await advance(5_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(transcript()).toEqual([]);
  });

  it("discards a slow idle snapshot that started before a send", async () => {
    let finish!: (response: Response) => void;
    fetchMock.mockResolvedValueOnce(json(idle))
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce(json({ state: "running", steered: false }, 202))
      .mockResolvedValueOnce(batch("idle", 2, [
        user("Check the sample."), { seq: 2, kind: "reply", text: "The sample is ready." },
      ]));
    await mount();
    await advance(5_000);
    await type("Check the sample.");
    await click("Send");
    expectPost("/send", { text: "Check the sample." });
    expect(requests()[1].signal?.aborted).toBe(true);
    await act(async () => { finish(json(idle)); });
    expect(box.textContent).toContain("Jarvis is working");
    await advance(700);
    expectPost("/events", { after: 0 });
    expect(transcript()).toEqual(["Check the sample.", "The sample is ready."]);
  });

  it("backs off on a failed read and reconciles an event gap from a snapshot", async () => {
    fetchMock.mockResolvedValueOnce(json(running))
      .mockRejectedValueOnce(new Error("private connection detail"))
      .mockResolvedValueOnce(json({ state: "running", seq: 80, gap: true, events: [] }))
      .mockResolvedValueOnce(json({ ...running, seq: 80, draft: "Resumed", history: [{ role: "user", text: "Recovered message" }] }));
    await mount();
    await advance(700);
    expect(box.textContent).toContain("Chat unavailable. Retrying");
    expect(box.textContent).not.toContain("private connection detail");
    await advance(4_999);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(requests()).toHaveLength(2);
    expect(transcript()).toEqual(["Recovered message", "Resumed"]);
    expect(box.textContent).not.toContain("Chat unavailable");
  });

  it("recovers a known tool in place and labels only the known steer after a gap", async () => {
    const first = { role: "user", text: "Look up the sample.", t: 1 };
    const steer = { role: "user", text: "Use the short list.", t: 3 };
    const tool = { name: "sample_lookup" };
    const start = { ...running, seq: 1, history: [first] };
    fetchMock.mockResolvedValueOnce(json(start))
      .mockResolvedValueOnce(batch("running", 3, [
        { ...tool, kind: "tool_start", seq: 2, t: 2 },
        { text: steer.text, kind: "steer", seq: 3, t: steer.t },
      ]))
      .mockResolvedValueOnce(json({ ...start, seq: 3, history: [first, steer], tools: [tool] }))
      .mockResolvedValueOnce(json({ state: "idle", seq: 80, gap: true, events: [] }))
      .mockResolvedValueOnce(json({
        ...start, state: "idle", seq: 80,
        history: [first, steer, { ...steer, t: 79 }, { role: "assistant", text: "Two rows.", t: 80 }],
        tools: [{ ...tool, ok: true, summary: "Found two rows" }],
      }));

    await mount();
    await advance(700);
    await advance(700);

    const log = box.querySelector('[aria-label="Chat transcript"]')!;
    expect([...log.querySelectorAll("article p, li")].map((row) => row.textContent)).toEqual([
      first.text, "sample_lookup · OK · Found two rows", steer.text, steer.text, "Two rows.",
    ]);
    const articles = log.querySelectorAll("article");
    expect(articles[1].textContent).toContain("Added to this turn");
    expect(articles[2].textContent).not.toContain("Added to this turn");
  });

  it("leaves a newer snapshot's messages to the next event poll", async () => {
    fetchMock.mockResolvedValueOnce(json(running))
      .mockResolvedValueOnce(batch("running", 1, [user("Start")]))
      .mockResolvedValueOnce(json({ ...idle, seq: 2, draft: "Old draft", history: [
        { role: "user", text: "Start" }, { role: "assistant", text: "Final answer" },
      ] }))
      .mockResolvedValueOnce(batch("idle", 2, [{ seq: 2, kind: "reply", text: "Final answer" }]));
    await mount();
    await advance(700);
    expect(transcript()).toEqual(["Start"]);
    await advance(700);
    expect(transcript()).toEqual(["Start", "Final answer"]);
  });
});
