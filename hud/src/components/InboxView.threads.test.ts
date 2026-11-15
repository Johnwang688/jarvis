// The Needs you page (WS-3): the old Inbox page under its new name, listing every row as before, with the
// rows that sit on a chat gathered under that chat (a link opens it) and the sidebar's hint able to
// scroll the page to one chat's group.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { groupByThread, parseInbox } from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";
import { FOCUS_REQUEST_MS, InboxFocusStore, inboxFocus } from "../state/inboxFocus";
import { InboxView, type InboxViewProps } from "./InboxView";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-10-25T10:00:00+00:00");
const threads = [
  { id: "t1", title: "desk chat", turns: 3 },
  { id: "t2", title: "second chat", turns: 1 },
];

const served = () => [
  { id: "appr:R1", kind: "approval", rank: 1, title: "Codex asks to use run_command", blocks: "chat t1 waits on this",
    thread_id: "t1", expires_at: "2026-10-25T10:02:00+00:00", detail: { req_id: "R1", tool: "run_command", command: "ls" } },
  { id: "q:t2:r9", kind: "question", rank: 2, title: "chat t2 is stopped until you answer", blocks: "chat t2 waits on this",
    thread_id: "t2", detail: { thread_id: "t2", req_id: "r9", text: "Pick one", options: ["left", "right"] } },
  { id: "prop:k", kind: "proposal", rank: 3, title: "task k starts on its own unless you withdraw it", task_id: "k",
    detail: { task_id: "k", brief: "ship it", withdraw_until: new Date(NOW + 30_000).toISOString() } },
  { id: "dec:d1", kind: "decision", rank: 4, title: "which database", thread_id: "t1",
    detail: { decision_id: "d1", question: "which?", options: [{ label: "a" }, { label: "b" }], suggested: 0 } },
  { id: "dec:d2", kind: "decision", rank: 8, title: "an old decision", thread_id: "t1",
    detail: { decision_id: "d2", question: "old?", options: [{ label: "a" }], suggested: 0, answered: "a" } },
];

const snap = (raw: unknown[] = served()): InboxSnapshot => ({ rows: parseInbox(raw)!, status: "ready", message: "", reads: 1 });

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  inboxFocus.clear();
});

function draw(over: Partial<InboxViewProps> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const props: InboxViewProps = {
    snapshot: snap(), approvals: [], blocked: false, threads,
    onOpenThread: vi.fn(), onOpenTask: vi.fn(), onAnswer: vi.fn(async () => ({ ok: true })),
    onDecide: vi.fn(async () => ({ ok: true })), onReread: vi.fn(),
    onMarkCheck: vi.fn(async () => ({ ok: true })), onImportChecks: vi.fn(async () => ({})),
    onMark: vi.fn(async () => ({ ok: true })), copyText: vi.fn(async () => {}), now: () => NOW, ...over,
  };
  act(() => root!.render(createElement(InboxView, props)));
  return props;
}
const q = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const qa = (sel: string) => Array.from(host!.querySelectorAll(sel)) as HTMLElement[];

describe("groupByThread", () => {
  it("gathers a thread's rows at its first row, keeps rows without a thread in place, and loses none", () => {
    const rows = parseInbox(served())!;
    const slots = groupByThread(rows);
    expect(slots.map((s) => (s.kind === "row" ? s.row.id : `${s.threadId}:${s.rows.map((r) => r.id).join(",")}`)))
      .toEqual(["t1:appr:R1,dec:d1,dec:d2", "t2:q:t2:r9", "prop:k"]);
    const all = slots.flatMap((s) => (s.kind === "row" ? [s.row.id] : s.rows.map((r) => r.id)));
    expect(all.sort()).toEqual(rows.map((r) => r.id).sort());
  });
});

describe("the Needs you page", () => {
  it("is titled Needs you and still lists every row, in its rank groups", () => {
    draw();
    expect(q('[data-testid="inbox-page-title"]')!.textContent).toBe("Needs you");
    expect(qa('[data-testid="inbox-row"]').map((r) => r.dataset.id).sort()).toEqual(
      ["appr:R1", "dec:d1", "dec:d2", "prop:k", "q:t2:r9"]);
    expect(qa('[data-testid="inbox-group-head"]').length).toBeGreaterThan(1);
  });

  it("groups a thread's rows under its name; rows without a thread stay outside any thread group", () => {
    draw();
    const t1 = q('[data-testid="inbox-thread"][data-thread-id="t1"]')!;
    expect(t1).toBeTruthy();
    expect(t1.querySelector('[data-testid="inbox-thread-name"]')!.textContent).toBe("desk chat");
    // t1 has rows in three rank groups (1-2, 3-5, 8): it is gathered inside each group, never across them.
    expect(qa('[data-testid="inbox-thread"][data-thread-id="t1"]').length).toBe(3);
    expect(Array.from(t1.querySelectorAll('[data-testid="inbox-row"]')).map((r) => (r as HTMLElement).dataset.id))
      .toEqual(["appr:R1"]);
    const prop = q('[data-testid="inbox-row"][data-id="prop:k"]')!;
    expect(prop.closest('[data-testid="inbox-thread"]')).toBeNull();
  });

  it("opens the thread from the group's link, with the sidebar's own handler; a task row has no thread link", () => {
    const props = draw();
    act(() => q('[data-testid="inbox-thread"][data-thread-id="t2"] [data-testid="inbox-thread-open"]')!.click());
    expect(props.onOpenThread).toHaveBeenCalledWith("t2");
    expect(q('[data-testid="inbox-row"][data-id="prop:k"] [data-testid="inbox-thread-open"]')).toBeNull();
  });

  it("is inert under a card: the thread link is disabled", () => {
    draw({ blocked: true });
    expect((q('[data-testid="inbox-thread-open"]') as HTMLButtonElement).disabled).toBe(true);
  });

  it("scrolls to and focuses the asked-for thread once its rows are drawn, then forgets the request", async () => {
    const scroll = vi.fn();
    (HTMLElement.prototype as any).scrollIntoView = scroll;
    try {
      act(() => inboxFocus.request("t2"));
      draw();
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(q('[data-testid="inbox-thread"][data-thread-id="t2"]'));
      expect(inboxFocus.get()).toBeNull();
    } finally {
      delete (HTMLElement.prototype as any).scrollIntoView;
    }
  });

  it("drops a request for a thread with no group instead of keeping it", async () => {
    act(() => inboxFocus.request("nope"));
    draw();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(inboxFocus.get()).toBeNull();
  });

  it("waits for the opening read before judging the target absent (a hint can be newer than the cached rows)", async () => {
    const scroll = vi.fn();
    (HTMLElement.prototype as any).scrollIntoView = scroll;
    try {
      let release!: () => void;
      const gate = new Promise<void>((r) => { release = r; });
      const stale = snap(served().filter((r) => r.thread_id !== "t2"));          // the cached rows lack t2
      const props = draw({ snapshot: stale, onReread: vi.fn(() => gate) });
      act(() => inboxFocus.request("t2"));
      await act(async () => { await Promise.resolve(); });
      expect(inboxFocus.get()).toBe("t2");                                       // not thrown away yet
      expect(scroll).not.toHaveBeenCalled();
      await act(async () => {                                                    // the read lands with t2 in it
        root!.render(createElement(InboxView, { ...props, snapshot: snap() }));
        release();
        await gate;
      });
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(q('[data-testid="inbox-thread"][data-thread-id="t2"]'));
      expect(inboxFocus.get()).toBeNull();
    } finally {
      delete (HTMLElement.prototype as any).scrollIntoView;
    }
  });

  it("a second hint for the same thread while the page stays mounted waits for its own read", async () => {
    const scroll = vi.fn();
    (HTMLElement.prototype as any).scrollIntoView = scroll;
    try {
      let release!: () => void;
      let gate = Promise.resolve();
      const reread = vi.fn(() => gate);
      const props = draw({ onReread: reread });                                  // t2 is in the rows
      act(() => inboxFocus.request("t2"));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(scroll).toHaveBeenCalledTimes(1);                                   // the first hint landed
      expect(inboxFocus.get()).toBeNull();
      // The inbox moves on without t2 (cached rows), the working list still has it: a new hint, a new read.
      gate = new Promise<void>((r) => { release = r; });
      await act(async () => { root!.render(createElement(InboxView, { ...props, snapshot: snap(served().filter((r) => r.thread_id !== "t2")) })); });
      act(() => inboxFocus.request("t2"));
      await act(async () => { await Promise.resolve(); });
      expect(inboxFocus.get()).toBe("t2");                                       // not judged against the cached rows
      await act(async () => {
        root!.render(createElement(InboxView, { ...props, snapshot: snap() }));
        release();
        await gate;
      });
      expect(scroll).toHaveBeenCalledTimes(2);
      expect(document.activeElement).toBe(q('[data-testid="inbox-thread"][data-thread-id="t2"]'));
      expect(inboxFocus.get()).toBeNull();
    } finally {
      delete (HTMLElement.prototype as any).scrollIntoView;
    }
  });

  it("a claimed request outlives the expiry through a slow read", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const scroll = vi.fn();
    (HTMLElement.prototype as any).scrollIntoView = scroll;
    try {
      let release!: () => void;
      const gate = new Promise<void>((r) => { release = r; });
      const props = draw({ snapshot: snap(served().filter((r) => r.thread_id !== "t2")), onReread: vi.fn(() => gate) });
      act(() => inboxFocus.request("t2"));
      await act(async () => { await Promise.resolve(); });
      vi.setSystemTime(Date.now() + FOCUS_REQUEST_MS + 1_400);                   // the read is still on its way
      expect(inboxFocus.get()).toBe("t2");
      await act(async () => {
        root!.render(createElement(InboxView, { ...props, snapshot: snap() }));
        release();
        await gate;
      });
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(q('[data-testid="inbox-thread"][data-thread-id="t2"]'));
    } finally {
      vi.useRealTimers();
      delete (HTMLElement.prototype as any).scrollIntoView;
    }
  });

  it("keeps the request while the inbox cannot be read, and the page going away drops it", async () => {
    const failed: InboxSnapshot = { rows: [], status: "error", message: "boom", reads: 1 };
    draw({ snapshot: failed, onReread: vi.fn(async () => {}) });
    act(() => inboxFocus.request("t2"));
    await act(async () => { await Promise.resolve(); });
    expect(inboxFocus.get()).toBe("t2");
    act(() => root!.unmount());
    root = null;
    expect(inboxFocus.get()).toBeNull();                                         // abandoned: nothing left to misfire
  });
});

describe("the focus request's lifetime", () => {
  it("an unanswered request expires, so a later ordinary open does not meet it", () => {
    let t = 1_000;
    const store = new InboxFocusStore(() => t);
    store.request("t1");
    expect(store.get()).toBe("t1");
    t += FOCUS_REQUEST_MS + 1;
    expect(store.get()).toBeNull();
  });

  it("a claimed request does not expire, an unclaimed one does, and a newer request replaces the older", () => {
    let t = 1_000;
    const store = new InboxFocusStore(() => t);
    const a = store.request("t1");
    store.claim(a.id);
    t += FOCUS_REQUEST_MS * 4;
    expect(store.get()).toBe("t1");
    const b = store.request("t1");                                               // the same thread, a new request
    expect(b.id).not.toBe(a.id);
    expect(store.current()).toBe(b);
    store.clearRequest(a.id);                                                    // the old one's read landing clears nothing
    expect(store.get()).toBe("t1");
    t += FOCUS_REQUEST_MS + 1;                                                   // b was never claimed
    expect(store.get()).toBeNull();
  });

  it("an ordinary open clears it", () => {
    const store = new InboxFocusStore();
    store.request("t1");
    store.clear();
    expect(store.get()).toBeNull();
  });
});
