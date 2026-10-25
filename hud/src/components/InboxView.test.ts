// The inbox view (inbox A2): groups in rank order, empty ones hidden; an
// approval row is never a second card; untrusted text stays text; a click on a
// row answers nothing; Answer and Withdraw post once; a 409 says so and reads
// again; Withdraw goes when its window closes; under a card nothing posts.

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
    onOpenThread: vi.fn(), onOpenTask: vi.fn(), onAnswer: vi.fn(async () => ({ ok: true })), onReread: vi.fn(),
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

  it("Withdraw posts {withdraw: true}, and is gone once withdraw_until passes", async () => {
    vi.useFakeTimers();
    let t = NOW;
    const p = draw({ now: () => t });
    act(() => (rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]') as HTMLButtonElement).click());
    await flush();
    expect(p.onAnswer).toHaveBeenCalledWith("prop:k", { withdraw: true });
    t = NOW + 31_000;
    act(() => { vi.advanceTimersByTime(31_000); });
    expect(rowOf("prop:k").querySelector('[data-testid="inbox-withdraw"]')).toBeNull();
    expect(rowOf("prop:k").querySelector('[data-testid="inbox-withdraw-closed"]')).not.toBeNull();
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

  it("reads the inbox when it is shown, and never touches the title", () => {
    document.title = "J.A.R.V.I.S.";
    const p = draw();
    expect(p.onReread).toHaveBeenCalled();
    expect(document.title).toBe("J.A.R.V.I.S.");
  });
});
