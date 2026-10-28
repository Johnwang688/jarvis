// The inbox's deferred rows (QH, jarvis/v2/quiet_defer.py): an approval asked
// during quiet hours, denied at once, shown at rank 4 ("Waiting on you",
// counted, never markable) with the card's words as text, "Approve and retry"
// and "Leave it"; neither is defaulted, each posts once per press, nothing
// posts under a card, and a Browser card or a day-old row never offers the retry.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { canMark, deferredOf, parseInbox, waitingCount } from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";
import { InboxView, type InboxViewProps } from "./InboxView";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const HOSTILE = '<img src=x onerror="window.__pwned=1">‮evil';

const deferredRow = (over: Record<string, unknown> = {}, detail: Record<string, unknown> = {},
                     card: Record<string, unknown> = {}) => ({
  id: "defer:D1", kind: "deferred", rank: 4, title: "Bash is waiting for your morning approval",
  blocks: "chat t1 was told to carry on without it", thread_id: "t1", asked_at: "2026-10-25T04:30:00+00:00",
  origin: "", markable: true,
  detail: { deferral_id: "D1", tool: "Bash", thread_id: "t1", task_id: null, at: "2026-10-25T04:30:00+00:00",
            until: "2026-10-25T12:00:00+00:00", grantable: true, expired: false, hud_only: false,
            card: { req_id: "D1", tool: "Bash", command: "make deploy " + HOSTILE, reason: "ask profile " + HOSTILE,
                    headline: "", args: { command: "make deploy " + HOSTILE }, ...card }, ...detail },
  ...over,
});

const snap = (raw: unknown[]): InboxSnapshot => ({ rows: parseInbox(raw)!, status: "ready", message: "", reads: 1 });

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  delete (window as any).__pwned;
});

function draw(raw: unknown[], over: Partial<InboxViewProps> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const props: InboxViewProps = {
    snapshot: snap(raw), approvals: [], blocked: false,
    onOpenThread: vi.fn(), onOpenTask: vi.fn(),
    onAnswer: vi.fn(async () => ({ ok: true, delivery: "queued", note: "Approved once for 30 minutes." })),
    onDecide: vi.fn(async () => ({ ok: true })), onReread: vi.fn(), now: () => 0,
    onMarkCheck: vi.fn(async () => ({ ok: true })), onImportChecks: vi.fn(async () => ({})),
    onMark: vi.fn(async () => ({ ok: true })), ...over,
  };
  act(() => root!.render(createElement(InboxView, props)));
  return props;
}

const q = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const rowOf = (id: string) => q(`[data-testid="inbox-row"][data-id="${id}"]`)!;
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

describe("deferred rows (QH)", () => {
  it("sits at rank 4 under 'Waiting on you', counted, never markable, the card's words as text", () => {
    draw([deferredRow()]);
    const r = rowOf("defer:D1");
    expect(r.dataset.rank).toBe("4");
    expect(q('[data-testid="inbox-group-you"]')!.contains(r)).toBe(true);
    expect(q('[data-testid="inbox-summary"]')!.textContent).toBe("1 waiting on you");
    expect(r.querySelector('[data-testid="inbox-title"]')!.textContent).toBe("Bash is waiting for your morning approval");
    expect(r.querySelector('[data-testid="inbox-deferred-command"]')!.textContent).toBe("make deploy " + HOSTILE.replace("‮", ""));
    expect(r.querySelector('[data-testid="inbox-deferred-reason"]')!.textContent).toContain("ask profile");
    expect(r.querySelector('[data-testid="inbox-deferred-args"]')!.textContent).toContain('"command"');
    expect(host!.querySelectorAll("img").length).toBe(0);
    expect((window as any).__pwned).toBeUndefined();
    // No snooze or dismiss, whatever the daemon's `markable` says.
    expect(r.querySelector('[data-testid="inbox-marks"]')).toBeNull();
    expect(canMark(parseInbox([deferredRow()])![0])).toBe(false);
    expect(canMark(parseInbox([deferredRow({ rank: 8 })])![0])).toBe(false);
    const buttons = Array.from(r.querySelectorAll("button")).map((b) => (b as HTMLElement).dataset.testid);
    expect(buttons).toEqual(["inbox-deferred-retry", "inbox-deferred-leave", "inbox-link"]);
    for (const b of Array.from(r.querySelectorAll("button"))) {
      expect(b.getAttribute("type")).toBe("button");
      expect(b.hasAttribute("autofocus")).toBe(false);
    }
    expect(document.activeElement).not.toBe(r.querySelector('[data-testid="inbox-deferred-retry"]'));
  });

  it("Approve and retry posts {action: retry} once per press and shows the daemon's note", async () => {
    let finish: (v: unknown) => void = () => {};
    const p = draw([deferredRow()], { onAnswer: vi.fn(() => new Promise((ok) => { finish = ok; })) });
    const retry = rowOf("defer:D1").querySelector('[data-testid="inbox-deferred-retry"]') as HTMLButtonElement;
    act(() => { retry.click(); retry.click(); });
    expect(p.onAnswer).toHaveBeenCalledTimes(1);
    expect(p.onAnswer).toHaveBeenCalledWith("defer:D1", { action: "retry" });
    await act(async () => { finish({ ok: true, note: "Approved once for 30 minutes." }); });
    await flush();
    expect(q('[data-testid="inbox-notice"]')!.textContent).toBe("Approved once for 30 minutes.");
    expect(p.onReread).toHaveBeenCalled();
  });

  it("Leave it posts {action: leave}; a click on the row posts nothing", async () => {
    const p = draw([deferredRow()]);
    act(() => rowOf("defer:D1").click());
    expect(p.onAnswer).not.toHaveBeenCalled();
    act(() => (rowOf("defer:D1").querySelector('[data-testid="inbox-deferred-leave"]') as HTMLButtonElement).click());
    await flush();
    expect(p.onAnswer).toHaveBeenCalledWith("defer:D1", { action: "leave" });
  });

  it("a Browser card, a day-old row, a cut card or one the daemon calls ungrantable offers Leave it only", () => {
    const cases = [deferredRow({}, { hud_only: true, grantable: true }),
                   deferredRow({}, { grantable: true }, { hud_only: true }),
                   deferredRow({ rank: 8 }, { grantable: true }),
                   deferredRow({}, { expired: true, grantable: true }),
                   deferredRow({}, { grantable: true }, { cut: true }),
                   deferredRow({}, { grantable: false })];
    for (const raw of cases) {
      draw([raw]);
      const r = rowOf("defer:D1");
      expect(r.querySelector('[data-testid="inbox-deferred-retry"]')).toBeNull();
      expect(r.querySelector('[data-testid="inbox-deferred-leave"]')).not.toBeNull();
      expect(deferredOf(parseInbox([raw])![0])!.grantable).toBe(false);
      act(() => root?.unmount());
      host?.remove();
    }
    root = null;
    host = null;
  });

  it("says how often it was asked, and why a row takes no retry, in the HUD's own words", () => {
    draw([deferredRow({ title: "Bash is waiting for your morning approval (asked 3 times)" }, { count: 3 })]);
    expect(rowOf("defer:D1").querySelector('[data-testid="inbox-deferred-count"]')!.textContent)
      .toBe("Asked 3 times while it waited.");
    act(() => root?.unmount());
    host?.remove();
    for (const [code, words] of [["codex", "A Codex request"], ["owner-only", "asked every time"],
                                 ["too-large", "too long to show"], ["no-chat", "Nothing is waiting"],
                                 ["anything " + HOSTILE, "cannot be approved from here"]]) {
      draw([deferredRow({}, { why_not: code, grantable: true })]);
      const r = rowOf("defer:D1");
      expect(r.querySelector('[data-testid="inbox-deferred-retry"]')).toBeNull();
      expect(r.querySelector('[data-testid="inbox-deferred-note"]')!.textContent).toContain(words);
      expect(r.textContent).not.toContain(HOSTILE);
      act(() => root?.unmount());
      host?.remove();
    }
    root = null;
    host = null;
  });

  it("an 'and N more' row offers Leave it only and says how many", async () => {
    const more = deferredRow({ id: "defer:more.t1", title: "And 31 more asks from chat t1 were deferred overnight" },
      { deferral_id: null, tool: "", card: {}, more: 31, count: 31, grantable: true });
    const p = draw([more]);
    const r = rowOf("defer:more.t1");
    expect(r.querySelector('[data-testid="inbox-deferred-retry"]')).toBeNull();
    expect(r.querySelector('[data-testid="inbox-deferred-note"]')!.textContent).toContain("31 more asks");
    act(() => (r.querySelector('[data-testid="inbox-deferred-leave"]') as HTMLButtonElement).click());
    await flush();
    expect(p.onAnswer).toHaveBeenCalledWith("defer:more.t1", { action: "leave" });
  });

  it("under a card everything is off and nothing posts", () => {
    const p = draw([deferredRow()], { blocked: true });
    const r = rowOf("defer:D1");
    for (const id of ["inbox-deferred-retry", "inbox-deferred-leave"]) {
      const b = r.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement;
      expect(b.disabled).toBe(true);
      act(() => b.click());
    }
    expect(p.onAnswer).not.toHaveBeenCalled();
  });

  it("is counted with the other waiting rows; a day-old one is not", () => {
    expect(waitingCount(parseInbox([deferredRow()])!)).toBe(1);
    expect(waitingCount(parseInbox([deferredRow({ rank: 8 })])!)).toBe(0);
  });
});
