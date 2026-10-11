import { describe, expect, it } from "vitest";
import { endsTalk, isTalkKey, talkKeyAction, type KeyLike } from "./talkkey";

const key = (over: Partial<KeyLike> = {}): KeyLike => ({
  key: "d", code: "KeyD", ctrlKey: true, metaKey: false, altKey: false, shiftKey: false, ...over,
});

describe("push-to-talk's key", () => {
  it("is Ctrl+D", () => {
    expect(isTalkKey(key())).toBe(true);
    // Playwright and some layouts report the capital with Ctrl held.
    expect(isTalkKey(key({ key: "D" }))).toBe(true);
  });

  it("takes no other modifier: Ctrl+Shift+D (Monaco's next match), Ctrl+Alt+D, AltGr, Meta", () => {
    expect(isTalkKey(key({ shiftKey: true, key: "D" }))).toBe(false);
    expect(isTalkKey(key({ altKey: true }))).toBe(false);
    expect(isTalkKey(key({ getModifierState: (k) => k === "AltGraph" }))).toBe(false);
    expect(isTalkKey(key({ metaKey: true }))).toBe(false);
    expect(isTalkKey(key({ ctrlKey: false, metaKey: true }))).toBe(false);
    expect(isTalkKey(key({ ctrlKey: false }))).toBe(false);
  });

  it("is never Space, and never another letter", () => {
    expect(isTalkKey(key({ key: " ", code: "Space", ctrlKey: false }))).toBe(false);
    expect(isTalkKey(key({ key: " ", code: "Space" }))).toBe(false);
    expect(isTalkKey(key({ key: "b", code: "KeyB" }))).toBe(false);
  });

  it("is ignored mid-composition", () => {
    expect(isTalkKey(key({ isComposing: true }))).toBe(false);
  });

  it("is the letter, not the place: Dvorak's D key types E and is Ctrl+E", () => {
    expect(isTalkKey(key({ key: "e", code: "KeyD" }))).toBe(false);
    expect(isTalkKey(key({ key: "d", code: "KeyH" }))).toBe(true);
  });

  it("falls back to the physical key where it types no Latin letter (Cyrillic, Greek)", () => {
    expect(isTalkKey(key({ key: "в", code: "KeyD" }))).toBe(true);
    expect(isTalkKey(key({ key: "δ", code: "KeyD" }))).toBe(true);
    expect(isTalkKey(key({ key: "в", code: "KeyF" }))).toBe(false);
  });

  it("survives an event whose getModifierState throws", () => {
    expect(isTalkKey(key({ getModifierState: () => { throw new Error("no"); } }))).toBe(true);
  });
});

describe("where the key goes", () => {
  it("is push-to-talk's everywhere but a focused terminal", () => {
    expect(talkKeyAction(key(), { terminal: false, blocked: false })).toBe("talk");
    expect(talkKeyAction(key(), { terminal: true, blocked: false })).toBe("shell");
  });

  it("never reaches a shell under a card: the window takes it (and push-to-talk refuses)", () => {
    expect(talkKeyAction(key(), { terminal: true, blocked: true })).toBe("talk");
    expect(talkKeyAction(key(), { terminal: false, blocked: true })).toBe("talk");
  });

  it("leaves every other key alone, in a terminal or not", () => {
    for (const where of [{ terminal: false, blocked: false }, { terminal: true, blocked: false },
      { terminal: true, blocked: true }]) {
      expect(talkKeyAction(key({ key: " ", code: "Space", ctrlKey: false }), where)).toBe(null);
      expect(talkKeyAction(key({ shiftKey: true }), where)).toBe(null);
    }
  });
});

describe("letting go", () => {
  it("ends on D or Ctrl, in either order", () => {
    expect(endsTalk({ key: "d", code: "KeyD" })).toBe(true);
    expect(endsTalk({ key: "D", code: "KeyD" })).toBe(true);
    expect(endsTalk({ key: "Control", code: "ControlLeft" })).toBe(true);
    expect(endsTalk({ key: "Control", code: "ControlRight" })).toBe(true);
    expect(endsTalk({ key: "в", code: "KeyD" })).toBe(true);
  });

  it("not on any other key", () => {
    expect(endsTalk({ key: " ", code: "Space" })).toBe(false);
    expect(endsTalk({ key: "Shift", code: "ShiftLeft" })).toBe(false);
    expect(endsTalk({ key: "a", code: "KeyA" })).toBe(false);
  });
});
