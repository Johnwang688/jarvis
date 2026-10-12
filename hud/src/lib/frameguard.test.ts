// The frame guard's rules (R-1; amendment 2 §E.8), pure.

import { describe, expect, it } from "vitest";
import {
  LOAD_GRACE_MS, LOAD_WAIT_MS, TAB_MS, frameKey, isModifierKey, isTabKey, judgeFocus, loadGrantLive, tabGesture,
  type FocusFacts, type TabMark,
} from "./frameguard";

const none: FocusFacts = { cardUp: false, drawn: true, pointerOver: false, tab: false, ownLoad: false, restore: false };

describe("judgeFocus", () => {
  it("refuses a frame that gained focus with no gesture", () => {
    expect(judgeFocus(none)).toEqual({ ok: false, why: "no-gesture" });
  });

  it("allows each of the owner's gestures", () => {
    expect(judgeFocus({ ...none, pointerOver: true })).toEqual({ ok: true, by: "pointer" });
    expect(judgeFocus({ ...none, tab: true })).toEqual({ ok: true, by: "tab" });
    expect(judgeFocus({ ...none, ownLoad: true })).toEqual({ ok: true, by: "load" });
    expect(judgeFocus({ ...none, restore: true })).toEqual({ ok: true, by: "restore" });
  });

  it("never allows focus under a card, whatever the gesture", () => {
    const all = { ...none, pointerOver: true, tab: true, ownLoad: true, restore: true };
    expect(judgeFocus({ ...all, cardUp: true })).toEqual({ ok: false, why: "card" });
  });

  it("never allows a hidden or background frame, whatever the gesture", () => {
    const all = { ...none, pointerOver: true, tab: true, ownLoad: true, restore: true };
    expect(judgeFocus({ ...all, drawn: false })).toEqual({ ok: false, why: "hidden" });
  });

  it("puts the card before hidden", () => {
    expect(judgeFocus({ ...none, cardUp: true, drawn: false })).toEqual({ ok: false, why: "card" });
  });
});

describe("Tab", () => {
  const ev = (key: string, mods: Partial<{ ctrlKey: boolean; altKey: boolean; metaKey: boolean }> = {}) =>
    ({ key, ctrlKey: false, altKey: false, metaKey: false, ...mods });
  const A = { name: "A" };
  const B = { name: "B" };
  const mark = (at: number, prevented = false, landed?: unknown): TabMark => ({ at, prevented: () => prevented, landed });

  it("counts a Tab within TAB_MS, and nothing older", () => {
    const m = mark(1000);
    expect(tabGesture(m, A, 1000)).toBe(true);
    expect(tabGesture(m, A, 1000 + TAB_MS)).toBe(true);
    expect(tabGesture(m, A, 1000 + TAB_MS + 1)).toBe(false);
    expect(tabGesture(m, A, 999)).toBe(false);
    expect(tabGesture(null, A, 0)).toBe(false);
  });

  it("never counts a Tab something cancelled (Monaco indenting): it moved no focus (M1)", () => {
    expect(tabGesture(mark(0, true), A, 10)).toBe(false);
  });

  it("counts only for the frame the Tab landed in, once that is known (M1)", () => {
    expect(tabGesture(mark(0, false, A), A, 10)).toBe(true);
    expect(tabGesture(mark(0, false, A), B, 10)).toBe(false);
    expect(tabGesture(mark(0, false, null), A, 10)).toBe(false); // it landed on a HUD control
  });

  it("is Tab or Shift+Tab only: never with Ctrl, Alt or Meta, nor any other key", () => {
    expect(isTabKey(ev("Tab"))).toBe(true);
    expect(isTabKey(ev("Tab", { ctrlKey: true }))).toBe(false);
    expect(isTabKey(ev("Tab", { altKey: true }))).toBe(false);
    expect(isTabKey(ev("Tab", { metaKey: true }))).toBe(false);
    expect(isTabKey(ev("a"))).toBe(false);
  });

  it("a lone modifier is not a key (Shift before Tab)", () => {
    expect(isModifierKey("Shift")).toBe(true);
    expect(isModifierKey("Control")).toBe(true);
    expect(isModifierKey("Tab")).toBe(false);
  });
});

describe("the owner's own load", () => {
  it("lasts until LOAD_GRACE_MS after load", () => {
    const g = { at: 0, loadedAt: 500 };
    expect(loadGrantLive(g, 500 + LOAD_GRACE_MS)).toBe(true);
    expect(loadGrantLive(g, 500 + LOAD_GRACE_MS + 1)).toBe(false);
  });

  it("or LOAD_WAIT_MS after the action when load never comes", () => {
    const g = { at: 100, loadedAt: null };
    expect(loadGrantLive(g, 100 + LOAD_WAIT_MS)).toBe(true);
    expect(loadGrantLive(g, 100 + LOAD_WAIT_MS + 1)).toBe(false);
    expect(loadGrantLive(null, 0)).toBe(false);
  });
});

describe("frameKey", () => {
  it("is pane:tab", () => {
    expect(frameKey(3, 0)).toBe("3:0");
  });
});
