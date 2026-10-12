// The frame guard's rules (R-1; amendment 2 §E.8), pure.

import { describe, expect, it } from "vitest";
import {
  LOAD_GRACE_MS, LOAD_WAIT_MS, TAB_MS, frameKey, isModifierKey, judgeFocus, keyMark, loadGrantLive, tabGesture,
  type FocusFacts,
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

  it("counts Tab and Shift+Tab within TAB_MS, and nothing older", () => {
    const m = keyMark(ev("Tab"), 1000);
    expect(tabGesture(m, 1000)).toBe(true);
    expect(tabGesture(m, 1000 + TAB_MS)).toBe(true);
    expect(tabGesture(m, 1000 + TAB_MS + 1)).toBe(false);
    expect(tabGesture(m, 999)).toBe(false);
  });

  it("is not Tab with Ctrl, Alt or Meta, nor any other key", () => {
    expect(tabGesture(keyMark(ev("Tab", { ctrlKey: true }), 0), 0)).toBe(false);
    expect(tabGesture(keyMark(ev("Tab", { altKey: true }), 0), 0)).toBe(false);
    expect(tabGesture(keyMark(ev("Tab", { metaKey: true }), 0), 0)).toBe(false);
    expect(tabGesture(keyMark(ev("a"), 0), 0)).toBe(false);
    expect(tabGesture(null, 0)).toBe(false);
  });

  it("leaves the mark as it was for a lone modifier (Shift before Tab)", () => {
    expect(keyMark(ev("Shift"), 5)).toBeNull();
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
