// The inbox view (inbox A2): groups in rank order, empty ones hidden; an
// approval row is never a second card; untrusted text stays text; a click on a
// row answers nothing; Answer and Withdraw post once; a 409 says so and reads
// again; Withdraw goes when its window closes; under a card nothing posts; a
// "denied while you were away" row (A3) is information with its chat link only.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { parseInbox } from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";
import { GONE_NOTICE, InboxView, type InboxViewProps } from "./InboxView";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const HOSTILE = '<img src=x onerror="window.__pwned=1">';
const NOW = Date.parse("2026-10-25T10:00:00+00:00");

const served = () => [
  { id: "task:f", kind: "task", rank: 8, title: "task f failed", blocks: "for your information", task_id: "f",
    detail: { task_id: "f", state: "failed", brief: "the brief " + HOSTILE } },
  { id: "appr:R1", kind: "approval", rank: 1, title: "Codex asks to use run_command", blocks: "chat t1 waits on this",
    thread_id: "t1", origin: "Codex · task " + HOSTILE, expires_at: "2026-10-25T10:02:00+00:00",
    detail: { req_id: "R1", tool: "run_command", command: "rm -rf /" } },
  { id: "q:t2:r9", kind: "question", rank: 2, title: "chat t2 is stopped until you answer", blocks: "chat t2 waits on this",
    thread_id: "t2", detail: { thread_id: "t2", req_id: "r9", text: "Pick one\n" + HOSTILE, options: ["left", "right"] } },
  { id: "prop:k", kind: "proposal", rank: 3, title: "task k starts on its own unless you withdraw it",
    blocks: "nothing waits yet", task_id: "k",
    detail: { task_id: "k", brief: "ship it", withdraw_until: new Date(NOW + 30_000).toISOString() } },
];

const snap = (raw: unknown[] = served()): InboxSnapshot => ({ rows: parseInbox(raw)!, status: "ready", message: "", reads: 1 });

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.useRealTimers();
});

function draw(over: Partial<InboxViewProps> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const props: InboxViewProps = {
    snapshot: snap(), approvals: [{ req_id: "R1" }], blocked: false,
    onOpenThread: vi.fn(), onOpenTask: vi.fn(), onAnswer: vi.fn(async () => ({ ok: true })),
    onDecide: vi.fn(async () => ({ ok: true, delivery: "queued", note: "Sent to the asking chat." })), onReread: vi.fn(),
    onMarkCheck: vi.fn(async () => ({ ok: true, done: false })), onImportChecks: vi.fn(async () => ({})),
    copyText: vi.fn(async () => {}),
    now: () => NOW, ...over,
  };
  act(() => root!.render(createElement(InboxView, props)));
  return props;
}

const q = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const qa = (sel: string) => Array.from(host!.querySelectorAll(sel)) as HTMLElement[];
const rowOf = (id: string) => q(`[data-testid="inbox-row"][data-id="${id}"]`)!;
const type = (input: HTMLInputElement, value: string) => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => { set.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
};
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

describe("InboxView", () => {
  it("groups rows under their headers in rank order, hiding the empty ones", () => {
    draw();
    expect(qa('[data-testid="inbox-group-head"]').map((h) => h.textContent))
      .toEqual(["Waiting now", "Waiting on you", "For your information"]);
    expect(qa('[data-testid="inbox-row"]').map((r) => r.dataset.id)).toEqual(["appr:R1", "q:t2:r9", "prop:k", "task:f"]);
    expect(q('[data-testid="inbox-group-time"]')).toBeNull();
  });

  it("an approval row is never a second card: it points at the veil's", () => {
    draw();
    expect(qa('[data-testid="approval-card"]').length).toBe(0);
    expect(rowOf("appr:R1").textContent).not.toContain("rm -rf");
    expect(rowOf("appr:R1").querySelector('[data-testid="inbox-card-note"]')!.textContent).toBe("On the card now: answer it there.");
    act(() => root!.unmount());
    root = null;
    host!.remove();
    draw({ approvals: [{ req_id: "R0" }, { req_id: "R1" }] });
    expect(rowOf("appr:R1").querySelector('[data-testid="inbox-card-note"]')!.textContent).toContain("Queued");
    expect(rowOf("appr:R1").querySelectorAll("button:not([data-testid='inbox-link'])").length).toBe(0);
  });

  it("untrusted text renders as text: no element is made from origin, a question or a brief", () => {
    draw();
    expect(host!.querySelectorAll("img").length).toBe(0);
    expect((window as any).__pwned).toBeUndefined();
    expect(rowOf("appr:R1").querySelector('[data-testid="inbox-origin"]')!.textContent).toBe("Codex · task " + HOSTILE);
    expect(rowOf("q:t2:r9").querySelector('[data-testid="inbox-question"]')!.textContent).toBe("Pick one\n" + HOSTILE);
    expect(rowOf("task:f").querySelector('[data-testid="inbox-brief"]')!.textContent).toContain(HOSTILE);
    expect(rowOf("q:t2:r9").querySelector('[data-testid="inbox-options"]')!.textContent).toBe("Options: left / right");
  });

  it("a click anywhere on a row answers nothing", () => {
    const p = draw();
    for (const r of qa('[data-testid="inbox-row"]')) {
      act(() => r.click());
      for (const el of Array.from(r.querySelectorAll("[data-testid='inbox-title'], [data-testid='inbox-blocks'], .ir-text"))) {
        act(() => (el as HTMLElement).click());
      }
    }
    expect(p.onAnswer).not.toHaveBeenCalled();
    expect(p.onOpenThread).not.toHaveBeenCalled();
    expect(p.onOpenTask).not.toHaveBeenCalled();
  });

  it("Answer posts the typed text once; an empty box posts nothing; Enter answers too", async () => {
    const p = draw();
    const box = rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer"]') as HTMLInputElement;
    const send = rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer-send"]') as HTMLButtonElement;
    act(() => send.click());
    expect(p.onAnswer).not.toHaveBeenCalled();
    type(box, "  left  ");
    act(() => { send.click(); send.click(); });
    await flush();
    expect(p.onAnswer).toHaveBeenCalledTimes(1);
    expect(p.onAnswer).toHaveBeenCalledWith("q:t2:r9", { text: "left" });
    expect(p.onReread).toHaveBeenCalled();
    type(box, "right");
    act(() => { box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await flush();
    expect(p.onAnswer).toHaveBeenLastCalledWith("q:t2:r9", { text: "right" });
  });

  it("a 409 says 'already answered or gone' and reads the inbox again", async () => {
    const p = draw({ onAnswer: vi.fn(async () => { throw new ApiError(409, "this item is no longer waiting"); }) });
    const rereads = (p.onReread as any).mock.calls.length;
    act(() => (rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]') as HTMLButtonElement).click());
    await flush();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe(GONE_NOTICE);
    expect((p.onReread as any).mock.calls.length).toBeGreaterThan(rereads);
  });

  it("another refusal is said under its row", async () => {
    draw({ onAnswer: vi.fn(async () => { throw new ApiError(400, "text must be a non-empty string"); }) });
    const box = rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer"]') as HTMLInputElement;
    type(box, "x");
    act(() => (rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer-send"]') as HTMLButtonElement).click());
    await flush();
    expect(rowOf("q:t2:r9").querySelector('[data-testid="inbox-row-error"]')!.textContent).toContain("text must be");
    expect(q('[data-testid="inbox-notice"]')).toBeNull();
  });

  it("Withdraw posts {withdraw: true}; past withdraw_until by this clock it stays, only hinted 'closing'", async () => {
    vi.useFakeTimers();
    let t = NOW;
    const p = draw({ now: () => t });
    const withdraw = () => rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]') as HTMLButtonElement | null;
    const hint = () => rowOf("prop:k").querySelector('[data-testid="inbox-withdraw-hint"]')!;
    expect(withdraw()!.dataset.hint).toBe("open");
    expect(hint().textContent).toBe("Starts on its own when its window closes.");
    act(() => withdraw()!.click());
    await flush();
    expect(p.onAnswer).toHaveBeenCalledWith("prop:k", { withdraw: true });
    // 16 s on: inside CLOSING_MS of the window's end, the timer flips the hint.
    t = NOW + 16_000;
    act(() => { vi.advanceTimersByTime(16_000); });
    expect(withdraw()!.dataset.hint).toBe("closing");
    t = NOW + 31_000;
    act(() => { vi.advanceTimersByTime(15_000); });
    expect(withdraw()).not.toBeNull();
    expect(withdraw()!.disabled).toBe(false);
    expect(hint().textContent).not.toMatch(/closed/i);
  });

  // withdraw_until is the daemon's clock; the window's is the browser's. The
  // daemon's window here is open for 30 s more by the daemon's own clock.
  const skewed = (skewMs: number) => served().map((r) => r.kind === "proposal"
    ? { ...r, detail: { ...r.detail, withdraw_until: new Date(NOW + skewMs + 30_000).toISOString() } } : r);

  it("skew: a daemon 90 s behind still gets Withdraw offered (hinted closing), and the press posts", async () => {
    const p = draw({ snapshot: snap(skewed(-90_000)) });
    const w = rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]') as HTMLButtonElement;
    expect(w).not.toBeNull();
    expect(w.disabled).toBe(false);
    expect(w.dataset.hint).toBe("closing");
    expect(rowOf("prop:k").textContent).not.toMatch(/has closed/i);
    act(() => w.click());
    await flush();
    expect(p.onAnswer).toHaveBeenCalledWith("prop:k", { withdraw: true });
    expect(q('[data-testid="inbox-notice"]')).toBeNull();
  });

  it("skew: a daemon 90 s ahead shows the window open; a press after the daemon closed it is its 409, said, re-read",
    async () => {
      const p = draw({ snapshot: snap(skewed(90_000)),
        onAnswer: vi.fn(async () => { throw new ApiError(409, "this item is no longer waiting"); }) });
      const w = rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]') as HTMLButtonElement;
      expect(w.dataset.hint).toBe("open");
      const rereads = (p.onReread as any).mock.calls.length;
      act(() => w.click());
      await flush();
      expect(q('[data-testid="inbox-notice"]')!.textContent).toBe(GONE_NOTICE);
      expect((p.onReread as any).mock.calls.length).toBeGreaterThan(rereads);
    });

  it("the answer box is read-only while its answer is on its way: nothing typed then is lost", async () => {
    let settle!: () => void;
    const p = draw({ onAnswer: vi.fn(() => new Promise<unknown>((r) => { settle = () => r({ ok: true }); })) });
    const box = rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer"]') as HTMLInputElement;
    type(box, "left");
    act(() => (rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer-send"]') as HTMLButtonElement).click());
    expect(box.readOnly).toBe(true);
    type(box, "left and more");                     // a change while sending is refused
    expect(box.value).toBe("left");
    await act(async () => { settle(); await Promise.resolve(); await Promise.resolve(); });
    expect(p.onAnswer).toHaveBeenCalledWith("q:t2:r9", { text: "left" });
    expect(box.readOnly).toBe(false);
    expect(box.value).toBe("");
    type(box, "next");
    expect(box.value).toBe("next");
  });

  it("links open the row's thread, else its task", () => {
    const p = draw();
    act(() => (rowOf("q:t2:r9").querySelector('[data-testid="inbox-link"]') as HTMLButtonElement).click());
    act(() => (rowOf("prop:k").querySelector('[data-testid="inbox-link"]') as HTMLButtonElement).click());
    expect(p.onOpenThread).toHaveBeenCalledWith("t2");
    expect(p.onOpenTask).toHaveBeenCalledWith("k");
    expect(p.onAnswer).not.toHaveBeenCalled();
  });

  it("under a card every control is off and a script's click posts nothing", async () => {
    const p = draw({ blocked: true });
    for (const b of qa("button")) expect((b as HTMLButtonElement).disabled).toBe(true);
    const box = rowOf("q:t2:r9").querySelector('[data-testid="inbox-answer"]') as HTMLInputElement;
    type(box, "left");
    act(() => { box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    for (const b of qa("button")) {
      b.removeAttribute("disabled");
      act(() => b.click());
    }
    await flush();
    expect(p.onAnswer).not.toHaveBeenCalled();
    expect(p.onOpenThread).not.toHaveBeenCalled();
    expect(p.onOpenTask).not.toHaveBeenCalled();
  });

  it("says when nothing waits, and when the inbox is not there", () => {
    draw({ snapshot: { rows: [], status: "ready", message: "", reads: 1 } });
    expect(q('[data-testid="inbox-empty"]')).not.toBeNull();
    act(() => root!.unmount());
    root = null;
    host!.remove();
    draw({ snapshot: { rows: [], status: "unavailable", message: "route not found", reads: 1 } });
    expect(q('[data-testid="inbox-unavailable"]')!.textContent).toContain("route not found");
  });

  // Inbox A3 (IN-3): "denied while you were away", rank 8, information only.
  const missedRow = (over: Record<string, unknown> = {}) => ({
    id: "missed:M1", kind: "missed", rank: 8, title: "Denied while you were away: run_command",
    blocks: "task k7 was denied it: nobody answered in time", thread_id: "t9", task_id: "k7",
    asked_at: "2026-10-25T09:00:00+00:00", origin: "Codex · task " + HOSTILE,
    detail: { req_id: "M1", tool: "run_command", origin: "Codex · task " + HOSTILE, thread_id: "t9",
              task_id: "k7", at: "2026-10-25T09:00:00+00:00", resolution: "timeout" },
    ...over,
  });

  it("a missed row sits under 'For your information', says why in code's words, and offers only its chat", () => {
    const p = draw({ snapshot: snap([...served(), missedRow()]) });
    const fyi = q('[data-testid="inbox-group-fyi"]')!;
    expect(Array.from(fyi.querySelectorAll('[data-testid="inbox-row"]')).map((r) => (r as HTMLElement).dataset.id))
      .toEqual(["task:f", "missed:M1"]);
    const r = rowOf("missed:M1");
    expect(r.dataset.tone).toBe("plain");
    expect(r.querySelector('[data-testid="inbox-title"]')!.textContent).toBe("Denied while you were away: run_command");
    expect(r.querySelector('[data-testid="inbox-blocks"]')!.textContent).toBe("task k7 was denied it: nobody answered in time");
    expect(r.querySelector('[data-testid="inbox-missed-why"]')!.textContent).toBe("Nobody answered in time, so it was denied.");
    expect(r.querySelector('[data-testid="inbox-missed-next"]')!.textContent).toBe("To try it again, ask from its chat.");
    // No answer box, no Withdraw, no "ask again": its one control is the link.
    expect(Array.from(r.querySelectorAll("button, input, textarea")).map((b) => (b as HTMLElement).dataset.testid))
      .toEqual(["inbox-link"]);
    expect(r.textContent).not.toMatch(/ask again|retry/i);
    // Not counted: the summary still counts the three waiting rows only.
    expect(q('[data-testid="inbox-summary"]')!.textContent).toBe("3 waiting on you");
    const link = r.querySelector('[data-testid="inbox-link"]') as HTMLButtonElement;
    expect(link.textContent).toBe("Open its chat");
    act(() => link.click());
    expect(p.onOpenThread).toHaveBeenCalledWith("t9");
    expect(p.onOpenTask).not.toHaveBeenCalled();
    expect(p.onAnswer).not.toHaveBeenCalled();
    act(() => r.click());
    expect(p.onAnswer).not.toHaveBeenCalled();
  });

  it("a missed row's untrusted text is text, and its words never come from the served detail", () => {
    draw({ snapshot: snap([missedRow({ title: "Denied while you were away: " + HOSTILE,
      detail: { ...missedRow().detail, resolution: HOSTILE, tool: HOSTILE } })]) });
    const r = rowOf("missed:M1");
    expect(host!.querySelectorAll("img").length).toBe(0);
    expect((window as any).__pwned).toBeUndefined();
    expect(r.querySelector('[data-testid="inbox-origin"]')!.textContent).toBe("Codex · task " + HOSTILE);
    expect(r.querySelector('[data-testid="inbox-title"]')!.textContent).toBe("Denied while you were away: " + HOSTILE);
    expect(r.querySelector('[data-testid="inbox-missed-why"]')!.textContent).toBe("It was denied without an answer.");
  });

  it("a missed row with no chat has no link, not even to its task", () => {
    draw({ snapshot: snap([missedRow({ thread_id: null })]) });
    const r = rowOf("missed:M1");
    expect(r.querySelector('[data-testid="inbox-link"]')).toBeNull();
    expect(r.querySelectorAll("button").length).toBe(0);
    expect(r.querySelector('[data-testid="inbox-missed-next"]')!.textContent).toBe("Its chat is not known.");
  });

  // Inbox B1 (IN-5): decisions, rank 6, answered with an option or the owner's words.
  const decisionRow = (over: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) => ({
    id: "dec:d0123456789ab", kind: "decision", rank: 6, title: "chat t3 asks for your decision",
    blocks: "nothing waits; chat t3 hears your answer as a message", thread_id: "t3",
    asked_at: "2026-10-25T09:30:00+00:00", origin: "Claude · chat ‹" + HOSTILE + "› (t3)",
    detail: { decision_id: "d0123456789ab", question: "Which store?\n" + HOSTILE, options: ["sqlite", "postgres " + HOSTILE, "files"],
              default: 1, why: "It decides the schema " + HOSTILE, answer: null, delivery: null, ...detail },
    ...over,
  });
  const options = (r: HTMLElement) => Array.from(r.querySelectorAll('[data-testid="inbox-decision-option"]')) as HTMLInputElement[];
  const decisionText = (r: HTMLElement) => r.querySelector('[data-testid="inbox-decision-text"]') as HTMLInputElement;
  const decisionSend = (r: HTMLElement) => r.querySelector('[data-testid="inbox-decision-send"]') as HTMLButtonElement;

  it("a decision sits under 'When you have time' with its options as radios, the suggestion preselected and nothing posted", async () => {
    const p = draw({ snapshot: snap([...served(), decisionRow()]) });
    const time = q('[data-testid="inbox-group-time"]')!;
    expect(time.querySelector('[data-testid="inbox-group-head"]')!.textContent).toBe("When you have time");
    const r = rowOf("dec:d0123456789ab");
    expect(time.contains(r)).toBe(true);
    expect(r.dataset.tone).toBe("plain");
    const radios = options(r);
    expect(radios.map((o) => o.type)).toEqual(["radio", "radio", "radio"]);
    expect(radios.map((o) => o.checked)).toEqual([false, true, false]);
    const marker = r.querySelector('[data-testid="inbox-decision-suggested"]')!;
    expect(marker.closest("label")!.contains(radios[1])).toBe(true);
    // The HUD's own element, before the option's words (review of PR #309).
    const text = marker.closest("label")!.querySelector(".ir-option-text")!;
    expect(marker.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(text.textContent).toBe("postgres " + HOSTILE);
    expect(r.querySelectorAll('[data-testid="inbox-decision-suggested"]').length).toBe(1);
    await flush();
    expect(p.onDecide).not.toHaveBeenCalled();
    expect(p.onAnswer).not.toHaveBeenCalled();
    // Not counted: ranks 1 to 5 only.
    expect(q('[data-testid="inbox-summary"]')!.textContent).toBe("3 waiting on you");
  });

  it("a decision's untrusted text is text: question, options, why and origin make no element", () => {
    draw({ snapshot: snap([decisionRow()]) });
    const r = rowOf("dec:d0123456789ab");
    expect(host!.querySelectorAll("img").length).toBe(0);
    expect((window as any).__pwned).toBeUndefined();
    expect(r.querySelector('[data-testid="inbox-decision-question"]')!.textContent).toBe("Which store?\n" + HOSTILE);
    expect(r.querySelector('[data-testid="inbox-decision-why"]')!.textContent).toBe("It decides the schema " + HOSTILE);
    expect(r.querySelector('[data-testid="inbox-origin"]')!.textContent).toBe("Claude · chat ‹" + HOSTILE + "› (t3)");
    expect(Array.from(r.querySelectorAll(".ir-option-text")).map((e) => e.textContent)).toContain("postgres " + HOSTILE);
  });

  it("Answer posts the picked option's index once, to its own id; the reply's note is said", async () => {
    const p = draw({ snapshot: snap([decisionRow()]) });
    const r = rowOf("dec:d0123456789ab");
    act(() => options(r)[2].click());
    expect(options(r).map((o) => o.checked)).toEqual([false, false, true]);
    expect(p.onDecide).not.toHaveBeenCalled();
    act(() => { decisionSend(r).click(); decisionSend(r).click(); });
    await flush();
    expect(p.onDecide).toHaveBeenCalledTimes(1);
    expect(p.onDecide).toHaveBeenCalledWith("d0123456789ab", { choice: 2 });
    expect(p.onAnswer).not.toHaveBeenCalled();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe("Sent to the asking chat.");
    expect(p.onReread).toHaveBeenCalled();
  });

  it("the preselected suggestion is posted only when Answer is pressed", async () => {
    const p = draw({ snapshot: snap([decisionRow()]) });
    act(() => decisionSend(rowOf("dec:d0123456789ab")).click());
    await flush();
    expect(p.onDecide).toHaveBeenCalledWith("d0123456789ab", { choice: 1 });
  });

  it("with no suggestion and nothing picked or typed, Answer is off and posts nothing", async () => {
    const p = draw({ snapshot: snap([decisionRow({}, { default: null })]) });
    const r = rowOf("dec:d0123456789ab");
    expect(options(r).some((o) => o.checked)).toBe(false);
    expect(decisionSend(r).disabled).toBe(true);
    decisionSend(r).removeAttribute("disabled");
    act(() => decisionSend(r).click());
    await flush();
    expect(p.onDecide).not.toHaveBeenCalled();
  });

  it("typed words win over the option and are posted as {text}; Enter answers only from the focused box", async () => {
    const p = draw({ snapshot: snap([decisionRow()]) });
    const r = rowOf("dec:d0123456789ab");
    // Enter on a radio answers nothing.
    act(() => { options(r)[0].dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await flush();
    expect(p.onDecide).not.toHaveBeenCalled();
    // Enter in an empty box answers nothing either.
    act(() => { decisionText(r).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await flush();
    expect(p.onDecide).not.toHaveBeenCalled();
    type(decisionText(r), "  use the files, for now  ");
    expect(r.querySelector('[data-testid="inbox-decision-hint"]')).not.toBeNull();
    act(() => { decisionText(r).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await flush();
    expect(p.onDecide).toHaveBeenCalledTimes(1);
    expect(p.onDecide).toHaveBeenCalledWith("d0123456789ab", { text: "use the files, for now" });
  });

  it("a 409 on a decision says 'already answered or gone' and reads again", async () => {
    const p = draw({ snapshot: snap([decisionRow()]),
      onDecide: vi.fn(async () => { throw new ApiError(409, "this decision was already answered"); }) });
    const rereads = (p.onReread as any).mock.calls.length;
    act(() => decisionSend(rowOf("dec:d0123456789ab")).click());
    await flush();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe(GONE_NOTICE);
    expect((p.onReread as any).mock.calls.length).toBeGreaterThan(rereads);
  });

  it("under a card a decision's controls post nothing: Answer is off, a pick and Enter do nothing", async () => {
    const p = draw({ blocked: true, snapshot: snap([decisionRow()]) });
    const r = rowOf("dec:d0123456789ab");
    expect(decisionSend(r).disabled).toBe(true);
    act(() => options(r)[0].click());
    type(decisionText(r), "words");
    act(() => { decisionText(r).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    decisionSend(r).removeAttribute("disabled");
    act(() => decisionSend(r).click());
    await flush();
    expect(p.onDecide).not.toHaveBeenCalled();
    expect(p.onAnswer).not.toHaveBeenCalled();
  });

  it("a decision recorded for a closed chat (rank 8) shows the answer and offers nothing to press but its link", () => {
    const p = draw({ snapshot: snap([decisionRow({ rank: 8, title: "Your decision for chat t3",
      blocks: "recorded; the asking chat is closed" },
      { answer: { choice: 0, text: null, at: "2026-10-25T09:40:00+00:00" }, delivery: "closed" })]) });
    const r = rowOf("dec:d0123456789ab");
    expect(q('[data-testid="inbox-group-fyi"]')!.contains(r)).toBe(true);
    expect(r.querySelector('[data-testid="inbox-blocks"]')!.textContent).toBe("recorded; the asking chat is closed");
    expect(r.querySelector('[data-testid="inbox-decision-answer"]')!.textContent).toBe("Your answer: sqlite");
    expect(Array.from(r.querySelectorAll("button, input")).map((b) => (b as HTMLElement).dataset.testid)).toEqual(["inbox-link"]);
    expect(p.onDecide).not.toHaveBeenCalled();
  });

  it("a decision whose detail is not one is shown but never answerable", () => {
    draw({ snapshot: snap([decisionRow({}, { decision_id: "../x", options: ["only one"] })]) });
    const r = rowOf("dec:d0123456789ab");
    expect(r.querySelectorAll('[data-testid="inbox-decision-option"]').length).toBe(0);
    expect(r.querySelector('[data-testid="inbox-decision-send"]')).toBeNull();
  });

  it("reads the inbox when it is shown, and never touches the title", () => {
    document.title = "J.A.R.V.I.S.";
    const p = draw();
    expect(p.onReread).toHaveBeenCalled();
    expect(document.title).toBe("J.A.R.V.I.S.");
  });
});

describe("InboxView: live checks (inbox B2, IN-6)", () => {
  const SHA = "f".repeat(40);
  const checkRow = (over: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) => ({
    id: "chk:c0123456789ab", kind: "check", rank: 7, title: "Live check c0123456789ab from chat t4: 0 of 2 steps marked",
    blocks: "nothing waits; your marks are the receipt", thread_id: "t4", origin: "Claude · chat ‹x› (t4)",
    detail: {
      check_id: "c0123456789ab", title: "Quick review " + HOSTILE, prs: [], done: false,
      steps: [{ do: "Press it " + HOSTILE, expect: "A spinner" }, { do: "rm -rf ~", expect: "" }],
      results: [], deploy: { state: "no prs", text: "", markable: true }, merged: false, deployed_sha: null,
      created_at: "2026-10-25T09:00:00+00:00", imported_from: null, ...detail,
    },
    ...over,
  });
  const steps = (r: HTMLElement) => Array.from(r.querySelectorAll('[data-testid="inbox-check-step"]')) as HTMLElement[];
  const btn = (el: HTMLElement, id: string) => el.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement;

  it("shows the steps as text in the 'When you have time' group; nothing runs and Copy only copies", async () => {
    const p = draw({ snapshot: snap([checkRow()]) });
    const r = rowOf("chk:c0123456789ab");
    expect(q('[data-testid="inbox-group-time"]')!.contains(r)).toBe(true);
    expect(r.querySelector('[data-testid="inbox-check-title"]')!.textContent).toBe("Quick review " + HOSTILE);
    expect(r.querySelector("img")).toBeNull();
    expect((window as any).__pwned).toBeUndefined();
    const [first, second] = steps(r);
    expect(first.querySelector('[data-testid="inbox-check-do"]')!.textContent).toBe("Press it " + HOSTILE);
    expect(first.querySelector('[data-testid="inbox-check-expect"]')!.textContent).toBe("Expect: A spinner");
    expect(second.querySelector('[data-testid="inbox-check-expect"]')).toBeNull();
    act(() => btn(second, "inbox-check-copy").click());
    await flush();
    expect(p.copyText).toHaveBeenCalledWith("rm -rf ~");
    expect(p.onMarkCheck).not.toHaveBeenCalled();
    expect(p.onAnswer).not.toHaveBeenCalled();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe("Copied step 2's words.");
  });

  it("a verdict posts once with its note; Enter in the note marks nothing", async () => {
    let release!: () => void;
    const p = draw({ snapshot: snap([checkRow()]),
      onMarkCheck: vi.fn(() => new Promise((ok) => { release = () => ok({ ok: true, done: false }); })) });
    const [first] = steps(rowOf("chk:c0123456789ab"));
    const note = first.querySelector('[data-testid="inbox-check-note"]') as HTMLInputElement;
    type(note, "  looked fine  ");
    act(() => { note.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(p.onMarkCheck).not.toHaveBeenCalled();
    act(() => btn(first, "inbox-check-fail").click());
    act(() => btn(first, "inbox-check-fail").click());
    expect(p.onMarkCheck).toHaveBeenCalledTimes(1);
    expect(p.onMarkCheck).toHaveBeenCalledWith("c0123456789ab", 0, { verdict: "fail", note: "looked fine" });
    release();
    await flush();
    expect(p.onReread).toHaveBeenCalled();
  });

  it("not confirmed deployed: the line shows and every mark is off", () => {
    const text = "Not confirmed deployed: no merge commit for #301 in this checkout.";
    const p = draw({ snapshot: snap([checkRow({ blocks: text }, { prs: [301],
      deploy: { state: "not confirmed deployed", text, markable: false } })]) });
    const r = rowOf("chk:c0123456789ab");
    expect(r.querySelector('[data-testid="inbox-check-deploy"]')!.textContent).toBe(text);
    for (const step of steps(r)) {
      for (const v of ["pass", "fail", "skip"]) expect(btn(step, `inbox-check-${v}`).disabled).toBe(true);
    }
    const pass = btn(steps(r)[0], "inbox-check-pass");
    pass.removeAttribute("disabled");
    act(() => pass.click());
    expect(p.onMarkCheck).not.toHaveBeenCalled();
  });

  it("a 409 shows the daemon's own line", async () => {
    const line = "Not deployed yet: #301 is merged but not in the running daemon or installed HUD.";
    draw({ snapshot: snap([checkRow()]), onMarkCheck: vi.fn(async () => { throw new ApiError(409, line); }) });
    act(() => btn(steps(rowOf("chk:c0123456789ab"))[0], "inbox-check-pass").click());
    await flush();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe(line);
  });

  it("under a card nothing marks, copies or imports", async () => {
    const p = draw({ blocked: true, snapshot: snap([checkRow()]) });
    const [first] = steps(rowOf("chk:c0123456789ab"));
    for (const id of ["inbox-check-pass", "inbox-check-copy"]) {
      const b = btn(first, id);
      expect(b.disabled).toBe(true);
      b.removeAttribute("disabled");
      act(() => b.click());
    }
    await flush();
    expect(p.onMarkCheck).not.toHaveBeenCalled();
    expect(p.copyText).not.toHaveBeenCalled();
    expect((q('[data-testid="inbox-import-toggle"]') as HTMLButtonElement).disabled).toBe(true);
  });

  it("done: rank 8 shows the verdicts and copies the receipt as Markdown, with nothing left to mark", async () => {
    const results = [{ step: 0, verdict: "pass", note: "ok", at: "2026-10-25T09:30:00+00:00" },
                     { step: 1, verdict: "fail", note: "## not a heading\n- not a list", at: "2026-10-25T09:31:00+00:00" }];
    const p = draw({ snapshot: snap([checkRow({ rank: 8, title: "Live check c0123456789ab done: 1 passed, 1 failed, 0 skipped" },
      { done: true, results, prs: [301], deployed_sha: SHA, deploy: { state: "done", text: "", markable: false } })]) });
    const r = rowOf("chk:c0123456789ab");
    expect(q('[data-testid="inbox-group-fyi"]')!.contains(r)).toBe(true);
    expect(steps(r).map((s) => s.dataset.verdict)).toEqual(["pass", "fail"]);
    expect(r.querySelector('[data-testid="inbox-check-pass"]')).toBeNull();
    expect(r.querySelector('[data-testid="inbox-check-note"]')).toBeNull();
    act(() => btn(r, "inbox-check-receipt").click());
    await flush();
    const md = (p.copyText as any).mock.calls[0][0] as string;
    expect(md.startsWith("## Live check c0123456789ab: Quick review")).toBe(true);
    expect(md).toContain("- Result: 1 passed, 1 failed, 0 skipped, 0 not marked");
    expect(md).toContain("- Pull requests: #301");
    expect(md).toContain(`- Build: ${SHA.slice(0, 12)}`);
    expect(md).toContain("1. **PASS** Press it");
    expect(md).toContain("   - Note: \\#\\# not a heading - not a list");
    expect(md.split("\n").filter((l) => l.startsWith("#")).length).toBe(1);
  });

  it("imports a runbook as unsaved drafts; only Save stores one", async () => {
    const reply = { path: "/p/docs/plans/x.md", digest: "d1", drafts: [
      { index: 0, title: "First (#5)", prs: [5], steps: [{ do: "a", expect: "b" }], savable: true, reason: null },
      { index: 1, title: "VEX view", prs: [], steps: [], savable: false, reason: "it has no numbered steps" }] };
    const onImportChecks = vi.fn(async (body: any) => ("save" in body ? { ok: true, check_id: "c00000000000a" } : reply));
    const p = draw({ snapshot: snap([]), onImportChecks });
    act(() => (q('[data-testid="inbox-import-toggle"]') as HTMLButtonElement).click());
    type(q('[data-testid="inbox-import-path"]') as HTMLInputElement, "/p/docs/plans/x.md");
    act(() => (q('[data-testid="inbox-import-read"]') as HTMLButtonElement).click());
    await flush();
    expect(onImportChecks).toHaveBeenCalledWith({ path: "/p/docs/plans/x.md" });
    const drafts = qa('[data-testid="inbox-import-draft"]');
    expect(drafts.map((d) => d.dataset.savable)).toEqual(["1", "0"]);
    expect(drafts[1].querySelector('[data-testid="inbox-import-reason"]')!.textContent).toContain("it has no numbered steps");
    expect(onImportChecks).toHaveBeenCalledTimes(1);
    act(() => btn(drafts[0], "inbox-import-save").click());
    await flush();
    expect(onImportChecks).toHaveBeenLastCalledWith({ path: "/p/docs/plans/x.md", digest: "d1", save: 0 });
    expect(q('[data-testid="inbox-import-saved"]')!.textContent).toContain("c00000000000a");
    expect(p.onReread).toHaveBeenCalled();
  });
});
