import { describe, expect, it } from "vitest";
import { PANE_MIN_MS, TOTAL_MAX, TOTAL_WINDOW_MS, createChatActivity, paneRect } from "./activity";

function rig(rects: Record<number, { left: number; top: number; width: number; height: number } | null> = {}) {
  let t = 0;
  const made: { left: number }[] = [];
  const a = createChatActivity({ now: () => t, rectOf: (p) => (p in rects ? rects[p]! : { left: p, top: 0, width: 10, height: 10 }) });
  a.setSink((r) => { made.push(r); return true; });
  return { a, made, at: (ms: number) => { t = ms; } };
}

describe("chat activity ripples", () => {
  it("makes nothing without a sink, and nothing for a pane that is not drawn", () => {
    const h = rig({ 3: null });
    expect(h.a.chatStreaming(3)).toBe(false);
    h.a.setSink(null);
    expect(h.a.chatStreaming(1)).toBe(false);
    expect(h.made.length).toBe(0);
  });

  it("rippleAt makes one ripple from a point, and none without a sink (the boot's handoff)", () => {
    const h = rig();
    expect(h.a.rippleAt(120, 80)).toBe(true);
    expect(h.made).toEqual([{ left: 119, top: 79, width: 2, height: 2 }]);
    h.a.setSink(null);
    expect(h.a.rippleAt(1, 1)).toBe(false);
  });

  it("limits one pane to a ripple per PANE_MIN_MS", () => {
    const h = rig();
    for (let t = 0; t < 3 * PANE_MIN_MS; t += 50) { h.at(t); h.a.chatStreaming(1); }
    expect(h.made.length).toBe(3);
  });

  it("limits all panes together to TOTAL_MAX per window", () => {
    const h = rig();
    h.at(0);
    for (let p = 1; p <= 12; p++) h.a.chatStreaming(p);
    expect(h.made.length).toBe(TOTAL_MAX);
    h.at(TOTAL_WINDOW_MS + 1);
    h.a.chatStreaming(9);
    expect(h.made.length).toBe(TOTAL_MAX + 1);
  });

  it("a refused ripple (the aurora cannot stir) does not use the budget", () => {
    const h = rig();
    h.a.setSink(() => false);
    h.at(0);
    for (let p = 1; p <= 5; p++) expect(h.a.chatStreaming(p)).toBe(false);
    h.at(PANE_MIN_MS + 1);
    h.a.setSink((r) => { h.made.push(r); return true; });
    expect(h.a.chatStreaming(1)).toBe(true);
  });
});

describe("a rejected ripple costs no layout read", () => {
  function counted(eligible: () => boolean) {
    let t = 0;
    let reads = 0;
    const a = createChatActivity({ now: () => t, rectOf: () => { reads++; return { left: 0, top: 0, width: 10, height: 10 }; } });
    a.setSink(() => true, eligible);
    return { a, reads: () => reads, at: (ms: number) => { t = ms; } };
  }
  it("100 deltas with ripples impossible (activity off, Flow, Still) read no rect at all", () => {
    const h = counted(() => false);
    for (let i = 0; i < 100; i++) { h.at(i * 10); h.a.chatStreaming(1); }
    expect(h.reads()).toBe(0);
  });
  it("100 deltas in Fluid read at most one rect per pane window", () => {
    const h = counted(() => true);
    for (let i = 0; i < 100; i++) { h.at(i * 10); h.a.chatStreaming(1); } // 1 s of deltas
    expect(h.reads()).toBeLessThanOrEqual(2);
  });
  it("a pane with nothing to ripple from (not shown) is measured once per window, not per delta", () => {
    let t = 0;
    let reads = 0;
    const a = createChatActivity({ now: () => t, rectOf: () => { reads++; return null; } });
    a.setSink(() => true, () => true);
    for (let i = 0; i < 100; i++) { t = i * 10; a.chatStreaming(2); }
    expect(reads).toBeLessThanOrEqual(2);
  });
});

describe("paneRect", () => {
  it("prefers the pane's input bar and is null for a pane that is not drawn or has no size", () => {
    document.body.innerHTML = '<div data-testid="pane-2"><div data-testid="input"></div></div>';
    const pane = document.querySelector<HTMLElement>('[data-testid="pane-2"]')!;
    const input = document.querySelector<HTMLElement>('[data-testid="input"]')!;
    const box = (l: number, w: number) => () => ({ left: l, top: 5, width: w, height: 20, right: l + w, bottom: 25, x: l, y: 5, toJSON() {} }) as DOMRect;
    pane.getBoundingClientRect = box(0, 500);
    input.getBoundingClientRect = box(10, 300);
    expect(paneRect(2)).toEqual({ left: 10, top: 5, width: 300, height: 20 });
    expect(paneRect(7)).toBeNull();
    input.getBoundingClientRect = box(10, 0);
    expect(paneRect(2)).toBeNull();
    document.body.innerHTML = "";
  });

  it("is null when the pane shows another view, is inert (or inside something inert), or is off screen", () => {
    const box = (l: number, t: number) => () => ({ left: l, top: t, width: 300, height: 20, right: l + 300, bottom: t + 20, x: l, y: t, toJSON() {} }) as DOMRect;
    document.body.innerHTML = '<div id="wrap"><div data-testid="pane-4"><div data-testid="input"></div></div></div><div data-testid="pane-5"></div>';
    const input = document.querySelector<HTMLElement>('[data-testid="input"]')!;
    input.getBoundingClientRect = box(10, 10);
    expect(paneRect(4)).not.toBeNull();
    expect(paneRect(5)).toBeNull(); // no message box: not showing the chat
    document.getElementById("wrap")!.setAttribute("inert", "");
    expect(paneRect(4)).toBeNull();
    document.getElementById("wrap")!.removeAttribute("inert");
    input.getBoundingClientRect = box(-5000, 10);
    expect(paneRect(4)).toBeNull();
    input.getBoundingClientRect = box(10, 10);
    const veil = document.createElement("div");
    document.body.appendChild(veil);
    (document as unknown as { elementFromPoint: () => Element }).elementFromPoint = () => veil;
    expect(paneRect(4)).toBeNull(); // a point there hits the veil
    delete (document as unknown as { elementFromPoint?: unknown }).elementFromPoint;
    document.body.innerHTML = "";
  });
});
