import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatPanel } from "../src/components/chat/ChatPanel";

const path = "/v3/owner/chat";
const fetchMock = vi.fn<(url: string, options: RequestInit) => Promise<Response>>();
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const user = (seq: number, text: string) => ({ seq, t: seq, kind: "user", text });
const reply = (seq: number, text: string) => ({ seq, t: seq, kind: "reply", text });
const steer = { seq: 3, t: 3, kind: "steer", text: "Use the shorter list." };
const start = { seq: 2, t: 2, kind: "tool_start", name: "sample_lookup" };
const end = { seq: 3, t: 3, kind: "tool_end", name: start.name, ok: true, summary: "Found two rows" };
const first = { role: "user", text: "Look up the sample.", t: 1 };
const answer = { role: "assistant", text: "Two rows.", t: 4 };

let box: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  fetchMock.mockReset().mockImplementation(async (url) => {
    throw new Error(`Unexpected request to ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
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

const snapshot = (state: string, seq: number, history: object[] = [], tools: object[] = []) =>
  json({ state, session_id: "sample", seq, history, tools, draft: "" });
const batch = (state: string, seq: number, events: object[], gap = false) =>
  json({ state, seq, events, gap });
const mount = () => act(async () => root.render(<ChatPanel />));
const advance = (ms = 700) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const button = (label: string) => [...box.querySelectorAll("button")].find((el) => el.textContent === label)!;
const rows = () => [...box.querySelectorAll('[aria-label="Chat transcript"] article p, [aria-label="Chat transcript"] li')]
  .map((el) => el.textContent);

function type(text: string) {
  const field = box.querySelector("textarea")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return field;
}

it("MED keeps the tool result before the reply when the turn finishes", async () => {
  fetchMock.mockResolvedValueOnce(snapshot("running", 1, [first]))
    .mockResolvedValueOnce(batch("idle", 4, [start, end, reply(4, answer.text)]));
  await mount();
  await advance();
  expect(rows()).toEqual(["Look up the sample.", "sample_lookup · OK · Found two rows", "Two rows."]);
});

it("MED keeps the first turn's finished tool when the next turn starts", async () => {
  fetchMock.mockResolvedValueOnce(snapshot("running", 1, [first]))
    .mockResolvedValueOnce(batch("idle", 4, [start, end, reply(4, answer.text)]))
    .mockResolvedValueOnce(json({ state: "running", steered: false }, 202))
    .mockResolvedValueOnce(batch("running", 5, [user(5, "Now check the other sample.")]))
    .mockResolvedValueOnce(snapshot("running", 5, [
      first, answer, { role: "user", text: "Now check the other sample.", t: 5 },
    ]));
  await mount();
  await advance();
  expect(rows()).toContain("sample_lookup · OK · Found two rows");
  type("Now check the other sample.");
  await act(async () => button("Send").click());
  await advance();
  expect(rows()).toEqual(["Look up the sample.", "sample_lookup · OK · Found two rows", "Two rows.", "Now check the other sample."]);
});

it("MED places a steer after the tool that was already running when it was sent", async () => {
  fetchMock.mockResolvedValueOnce(snapshot("running", 1, [first]))
    .mockResolvedValueOnce(batch("running", 2, [start]))
    .mockResolvedValueOnce(snapshot("running", 2, [first], [{ name: start.name }]))
    .mockResolvedValueOnce(json({ state: "running", steered: true }, 202))
    .mockResolvedValueOnce(batch("running", 3, [steer]))
    .mockResolvedValueOnce(snapshot("running", 3, [
      first,
      { role: "user", text: steer.text, t: steer.t },
    ], [{ name: start.name }]));
  await mount();
  await advance();
  expect(rows()).toEqual(["Look up the sample.", "sample_lookup · Running"]);
  type(steer.text);
  await act(async () => button("Send").click());
  await advance();
  expect(rows()).toEqual(["Look up the sample.", "sample_lookup · Running", steer.text]);
});

it("LOW keeps a known steer labeled exactly once across gap recovery", async () => {
  const history = [
    first,
    { role: "user", text: steer.text, t: steer.t },
  ];
  fetchMock.mockResolvedValueOnce(snapshot("running", 1, history.slice(0, 1)))
    .mockResolvedValueOnce(batch("running", 2, [{ ...steer, seq: 2 }]))
    .mockResolvedValueOnce(snapshot("running", 2, history))
    .mockResolvedValueOnce(batch("idle", 80, [], true))
    .mockResolvedValueOnce(snapshot("idle", 80, [...history, { role: "assistant", text: "Two rows.", t: 80 }]));
  await mount();
  await advance();
  expect(box.querySelectorAll("article")[1].textContent).toContain("Added to this turn");
  await advance();
  expect(rows()).toEqual(["Look up the sample.", steer.text, "Two rows."]);
  expect(box.querySelectorAll("article")[1].textContent).toContain("Added to this turn");
});

it.each(["ctrlKey", "altKey", "metaKey"] as const)("MED keeps the draft on Enter with %s", async (modifier) => {
  fetchMock.mockResolvedValueOnce(snapshot("idle", 0))
    .mockResolvedValueOnce(json({ state: "running", steered: false }, 202));
  await mount();
  const field = type("Still editing this draft.");
  await act(async () => {
    field.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter", bubbles: true, cancelable: true, [modifier]: true,
    }));
  });
  expect(fetchMock.mock.calls.filter(([url]) => url === `${path}/send`)).toHaveLength(0);
  expect(field.value).toBe("Still editing this draft.");
});
