import { describe, expect, it } from "vitest";
import {
  SPACE_HINT, endsTalk, isTalkKey, isTypingTarget, noPointerFocus, talkKeyAction, wantsSpaceHint, type KeyLike,
} from "./talkkey";

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

describe("where Space types", () => {
  const make = (html: string) => {
    const host = document.createElement("div");
    host.innerHTML = html;
    document.body.appendChild(host);
    return host;
  };

  it("is a typing target in a text field, a select, an editable element, Monaco and a terminal", () => {
    const host = make(`<textarea id="a"></textarea><input id="b"><input id="c" type="search"><select id="d"></select>
      <div contenteditable="true"><span id="e"></span></div><div class="monaco-editor"><div id="f"></div></div>
      <div class="termslot"><div class="xterm"><textarea id="g"></textarea></div></div>`);
    for (const id of ["a", "b", "c", "d", "e", "f", "g"]) expect(isTypingTarget(host.querySelector(`#${id}`))).toBe(true);
    host.remove();
  });

  it("is not one on a button, a checkbox, the page or nothing", () => {
    const host = make(`<button id="a"></button><input id="b" type="checkbox"><div id="c" tabindex="0"></div>`);
    for (const id of ["a", "b", "c"]) expect(isTypingTarget(host.querySelector(`#${id}`))).toBe(false);
    expect(isTypingTarget(document.body)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    host.remove();
  });
});

describe("the one-time Space hint", () => {
  const space = { key: " ", code: "Space" };
  it("is wanted on a fresh Space outside a typing target, with no card up", () => {
    expect(wantsSpaceHint(space, { typing: false, blocked: false })).toBe(true);
    expect(SPACE_HINT).toContain("CTRL+D");
  });

  it("is not wanted while typing, under a card, on auto-repeat, with a modifier, or for another key", () => {
    expect(wantsSpaceHint(space, { typing: true, blocked: false })).toBe(false);
    expect(wantsSpaceHint(space, { typing: false, blocked: true })).toBe(false);
    expect(wantsSpaceHint({ ...space, repeat: true }, { typing: false, blocked: false })).toBe(false);
    expect(wantsSpaceHint({ ...space, ctrlKey: true }, { typing: false, blocked: false })).toBe(false);
    expect(wantsSpaceHint({ key: "d", code: "KeyD" }, { typing: false, blocked: false })).toBe(false);
  });
});

describe("a button a pointer must not focus", () => {
  it("keeps focus where it was and still clicks", () => {
    const box = document.createElement("input");
    const button = document.createElement("button");
    let clicked = 0;
    button.addEventListener("mousedown", noPointerFocus);
    button.addEventListener("click", () => clicked++);
    document.body.append(box, button);
    box.focus();
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    button.dispatchEvent(down);
    button.click();
    expect(down.defaultPrevented).toBe(true);
    expect(clicked).toBe(1);
    expect(document.activeElement).toBe(box);
    box.remove();
    button.remove();
  });
});
