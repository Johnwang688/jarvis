import { describe, expect, it } from "vitest";
import {
  FRAME_MAX_BYTES, FramePipe, HeldButtons, HeldKeys, LINE_PX, MAX_DEVICE_PIXELS, MOD, OFF_PAGE, PASTE_CHUNK, VIEW_MAX,
  WHEEL_MAX, backingSize, browserTakesKey, cdpKey, cdpMouse, cdpWheel, cleanTitle, clientToPage, clipKey, frameDest,
  isAltGraph, normalizeWheel, pageToCanvas, paneKeyAction, pasteMessages, placeFrame, routeKeyDown, routeMouse,
  viewportFor, virtualKeyOf, type Frame, type InputMsg, type KeyEventLike,
} from "./remote";
import { isTakeover } from "./drive";

/** A keydown as Chrome on Windows reports it. */
const key = (k: string, over: Partial<KeyEventLike> = {}): KeyEventLike => ({
  key: k, code: "", ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...over,
});
const ctrl = (k: string, over: Partial<KeyEventLike> = {}) => key(k, { ctrlKey: true, ...over });
/** AltGr as Chrome on Windows reports it: Ctrl+Alt, with the AltGraph modifier state. */
const altGr = (k: string, code: string, over: Partial<KeyEventLike> = {}) =>
  key(k, { code, ctrlKey: true, altKey: true, getModifierState: (m: string) => m === "AltGraph", ...over });

describe("keys to CDP", () => {
  it("a letter types itself", () => {
    expect(cdpKey(key("a", { code: "KeyA", keyCode: 65 }), "down")).toEqual({
      type: "keyDown", modifiers: 0, key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
      location: 0, text: "a", unmodifiedText: "a",
    });
  });

  it("Shift is part of typing; Ctrl, Alt and Meta type nothing but still reach the page", () => {
    const shifted = cdpKey(key("A", { code: "KeyA", shiftKey: true }), "down");
    expect([shifted.type, shifted.text, shifted.modifiers]).toEqual(["keyDown", "A", MOD.shift]);
    const all = cdpKey(ctrl("a", { code: "KeyA" }), "down");
    expect([all.type, all.text, all.modifiers, all.windowsVirtualKeyCode]).toEqual(["rawKeyDown", undefined, MOD.ctrl, 65]);
    const alt = cdpKey(key("f", { code: "KeyF", altKey: true }), "down");
    expect([alt.type, alt.text, alt.modifiers]).toEqual(["rawKeyDown", undefined, MOD.alt]);
  });

  it("Enter types a carriage return, Ctrl+Enter none; named keys carry their virtual key", () => {
    const enter = cdpKey(key("Enter", { code: "Enter" }), "down");
    expect([enter.type, enter.text, enter.windowsVirtualKeyCode]).toEqual(["keyDown", "\r", 13]);
    expect(cdpKey(ctrl("Enter", { code: "Enter" }), "down").text).toBeUndefined();
    const left = cdpKey(key("ArrowLeft", { code: "ArrowLeft" }), "down");
    expect([left.type, left.windowsVirtualKeyCode]).toEqual(["rawKeyDown", 37]);
    expect(virtualKeyOf({ key: "Backspace", code: "Backspace" })).toBe(8);
    expect(virtualKeyOf({ key: "F12", code: "F12" })).toBe(123);
    expect(virtualKeyOf({ key: "7", code: "Numpad7" })).toBe(103);
    expect(virtualKeyOf({ key: "ß", code: "Minus", keyCode: 219 })).toBe(219);   // the layout's own, first
    expect(virtualKeyOf({ key: "x", code: "", keyCode: 229 })).toBe(88);         // 229 is the IME's, never a key
  });

  it("a keyup types nothing; the numpad and auto-repeat say so", () => {
    expect(cdpKey(key("a", { code: "KeyA" }), "up")).toEqual({
      type: "keyUp", modifiers: 0, key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
      location: 0,
    });
    const pad = cdpKey(key("5", { code: "Numpad5", location: 3, repeat: true }), "down");
    expect([pad.isKeypad, pad.autoRepeat, pad.location]).toEqual([true, true, 3]);
  });

  // The plan's mutation: AltGr sent as Ctrl+Alt (the owner could not type @, { or ę into a page).
  it("AltGr is a character, never Ctrl+Alt", () => {
    for (const [k, code] of [["@", "KeyQ"], ["{", "Digit7"], ["ę", "KeyE"], ["€", "KeyE"], ["\\", "IntlBackslash"]]) {
      const ev = cdpKey(altGr(k, code), "down");
      expect(ev.type).toBe("keyDown");
      expect(ev.text).toBe(k);
      expect(ev.modifiers & (MOD.ctrl | MOD.alt)).toBe(0);
    }
    // Where the system reports no AltGraph state, the character itself says so.
    const flags = cdpKey(key("@", { code: "KeyQ", ctrlKey: true, altKey: true }), "down");
    expect([flags.text, flags.modifiers]).toEqual(["@", 0]);
    // AltGr with Shift keeps Shift.
    expect(cdpKey(altGr("Ę", "KeyE", { shiftKey: true }), "down").modifiers).toBe(MOD.shift);
  });

  it("a real Ctrl+Alt shortcut stays one", () => {
    for (const k of ["k", "K", "3", " "]) {
      const ev = cdpKey(key(k, { ctrlKey: true, altKey: true }), "down");
      expect(isAltGraph(key(k, { ctrlKey: true, altKey: true }))).toBe(false);
      expect([ev.type, ev.text, ev.modifiers]).toEqual(["rawKeyDown", undefined, MOD.ctrl | MOD.alt]);
    }
    expect(isAltGraph(key("ArrowLeft", { ctrlKey: true, altKey: true }))).toBe(false);
    expect(isAltGraph(key("@", { ctrlKey: true, altKey: true, metaKey: true }))).toBe(false);
    expect(isAltGraph(key("@", { getModifierState: () => { throw new Error("no"); } }))).toBe(false);
  });
});

describe("which keys a Browser pane gives the page", () => {
  // The plan's addition: Ctrl+D handed to the page would take push-to-talk away.
  it("never Ctrl+D: push-to-talk, as everywhere but a shell", () => {
    expect(browserTakesKey(ctrl("d", { code: "KeyD" }))).toBe(false);
    expect(browserTakesKey(ctrl("D", { code: "KeyD" }))).toBe(false);
    expect(browserTakesKey(ctrl("в", { code: "KeyD" }))).toBe(false);   // Cyrillic: the physical key
    expect(routeKeyDown(ctrl("d", { code: "KeyD" }), false)).toEqual({ kind: "hud" });
  });

  it("never the HUD's zoom, pane-focus, fold or panel keys", () => {
    for (const k of ["=", "+", "-", "_", "0"]) expect(browserTakesKey(ctrl(k))).toBe(false);
    for (let n = 1; n <= 8; n++) expect(browserTakesKey(ctrl(String(n), { altKey: true }))).toBe(false);
    expect(browserTakesKey(ctrl("b"))).toBe(false);
    expect(browserTakesKey(ctrl("b", { altKey: true }))).toBe(false);
    expect(browserTakesKey(ctrl("`"))).toBe(false);
  });

  it("everything else, the page's own shortcuts included", () => {
    for (const ev of [
      key("a"), key("Escape"), key("Tab"), key("Enter"), key(" "), key("F1"), ctrl("a"), ctrl("s"), ctrl("f"),
      ctrl("k"), ctrl("p"), ctrl("P", { shiftKey: true }), ctrl("F", { shiftKey: true }),
      ctrl("D", { shiftKey: true }), ctrl("9", { altKey: true }), ctrl("B", { shiftKey: true }),
      altGr("{", "Digit7"), altGr("@", "KeyQ"),
    ]) {
      expect(browserTakesKey(ev)).toBe(true);
    }
  });
});

describe("where a keydown goes", () => {
  it("under a card: held, whatever it is — Escape bubbles to deny", () => {
    for (const ev of [key("Escape"), key("Enter"), key("a"), ctrl("v"), key("F5"), ctrl("d")]) {
      expect(routeKeyDown(ev, true)).toEqual({ kind: "held" });
    }
  });

  it("the pane's own keys", () => {
    expect(paneKeyAction(key("F5"))).toBe("reload");
    expect(paneKeyAction(key("F5", { shiftKey: true }))).toBe("hardReload");
    expect(paneKeyAction(ctrl("F5"))).toBe("hardReload");
    expect(paneKeyAction(ctrl("r"))).toBe("reload");
    expect(paneKeyAction(ctrl("R", { shiftKey: true }))).toBe("hardReload");
    expect(paneKeyAction(ctrl("l"))).toBe("address");
    expect(paneKeyAction(key("ArrowLeft", { altKey: true }))).toBe("back");
    expect(paneKeyAction(key("ArrowRight", { altKey: true }))).toBe("forward");
    expect(paneKeyAction(key("BrowserBack"))).toBe("back");
    expect(paneKeyAction(key("ArrowLeft"))).toBeNull();
    expect(paneKeyAction(key("ArrowLeft", { altKey: true, ctrlKey: true }))).toBeNull();
    expect(paneKeyAction(altGr("r", "KeyR"))).toBeNull();
    expect(routeKeyDown(key("F5"), false)).toEqual({ kind: "pane", action: "reload" });
  });

  it("a paste is never a key; copy and cut are forwarded and marked", () => {
    expect(routeKeyDown(ctrl("v"), false)).toEqual({ kind: "paste" });
    expect(routeKeyDown(ctrl("V", { shiftKey: true }), false)).toEqual({ kind: "paste" });
    expect(routeKeyDown(key("Insert", { shiftKey: true }), false)).toEqual({ kind: "paste" });
    const copy = routeKeyDown(ctrl("c", { code: "KeyC" }), false);
    expect(copy.kind === "forward" && copy.clip).toBe("copy");
    expect(copy.kind === "forward" && copy.event.modifiers).toBe(MOD.ctrl);
    expect(clipKey(ctrl("x"))).toBe("cut");
    expect(clipKey(key("Delete", { shiftKey: true }))).toBe("cut");
    expect(clipKey(ctrl("C", { shiftKey: true }))).toBeNull();
    expect(clipKey(altGr("v", "KeyV"))).toBeNull();
  });

  it("composition, dead and unidentified keys send nothing", () => {
    expect(routeKeyDown(key("a", { isComposing: true }), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("Process"), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("Dead", { code: "Quote" }), false)).toEqual({ kind: "drop" });
    expect(routeKeyDown(key("Unidentified"), false)).toEqual({ kind: "drop" });
  });

  it("anything else is forwarded", () => {
    const r = routeKeyDown(key("é", { code: "Digit2" }), false);
    expect(r.kind === "forward" && [r.event.text, r.clip]).toEqual(["é", null]);
  });
});

describe("held keys", () => {
  it("a keyup goes only where its keydown went", () => {
    const held = new HeldKeys();
    const a = key("a", { code: "KeyA" });
    held.pressed(a, cdpKey(a, "down"));
    expect(held.released(key("d", { code: "KeyD" }), false)).toBeNull();   // push-to-talk's D
    expect(held.released(a, false)?.type).toBe("keyUp");
    expect(held.released(a, false)).toBeNull();
  });

  it("a lone modifier is never held", () => {
    const held = new HeldKeys();
    const c = key("Control", { code: "ControlLeft", ctrlKey: true });
    held.pressed(c, cdpKey(c, "down"));
    expect(held.size).toBe(0);
    expect(held.released(key("Control", { code: "ControlLeft" }), false)).toBeNull();
  });

  // Review of PR #66 (LOW 2): a card taking focus is a blur, and nothing may reach the page under it.
  it("under a card nothing is released; the keys wait until the card goes", () => {
    const held = new HeldKeys();
    const enter = key("Enter", { code: "Enter" });
    const a = key("a", { code: "KeyA" });
    held.pressed(enter, cdpKey(enter, "down"));
    held.pressed(a, cdpKey(a, "down"));
    expect(held.releaseAll(true)).toEqual([]);                 // the card took focus: a blur
    expect(held.released(a, true)).toBeNull();                 // A came up under the card
    expect(held.size).toBe(2);
    const ups = held.releaseAll(false);                         // the card went
    expect(ups.map((u) => [u.type, u.code, u.modifiers])).toEqual([["keyUp", "Enter", 0], ["keyUp", "KeyA", 0]]);
    expect(held.releaseAll(false)).toEqual([]);
  });

  it("losing focus lets every key go", () => {
    const held = new HeldKeys();
    for (const k of [key("a", { code: "KeyA" }), key("ArrowLeft", { code: "ArrowLeft", shiftKey: true })]) {
      held.pressed(k, cdpKey(k, "down"));
    }
    const ups = held.releaseAll(false);
    expect(ups.map((u) => [u.type, u.code, u.modifiers])).toEqual([
      ["keyUp", "KeyA", 0], ["keyUp", "ArrowLeft", 0],
    ]);
    expect(held.releaseAll(false)).toEqual([]);
  });
});

/**
 * Keys as BR-3b will run them through: each keydown routed, a forwarded one
 * held and sent; each keyup released through `HeldKeys`. What reached the
 * page, and whether any of it took over from a driving agent.
 */
function typeThrough(steps: ["down" | "up", KeyEventLike][], blocked = false) {
  const held = new HeldKeys();
  const sent: InputMsg[] = [];
  for (const [phase, e] of steps) {
    if (phase === "down") {
      const r = routeKeyDown(e, blocked);
      if (r.kind === "forward") {
        held.pressed(e, r.event);
        sent.push({ t: "key", e: r.event });
      }
    } else {
      const up = held.released(e, blocked);
      if (up) sent.push({ t: "key", e: up });
    }
  }
  return { sent, tookOver: sent.some((m) => isTakeover(m)) };
}

const CTRL = (down: boolean, over: Partial<KeyEventLike> = {}) => key("Control", { code: "ControlLeft", ctrlKey: down, ...over });
const ALT = (down: boolean, over: Partial<KeyEventLike> = {}) => key("Alt", { code: "AltLeft", altKey: down, ctrlKey: true, ...over });

describe("a HUD chord pressed the usual way, modifier first (review of PR #66, MED)", () => {
  it("push-to-talk: Ctrl down, D down, D up, Ctrl up — nothing reaches the page, nothing takes over", () => {
    const r = typeThrough([
      ["down", CTRL(true)],
      ["down", ctrl("d", { code: "KeyD" })],
      ["down", ctrl("d", { code: "KeyD", repeat: true })],
      ["up", ctrl("d", { code: "KeyD" })],
      ["up", CTRL(false)],
    ]);
    expect(r.sent).toEqual([]);
    expect(r.tookOver).toBe(false);
  });

  it("Ctrl+Alt+2, Ctrl+= and Ctrl+B: nothing reaches the page either", () => {
    expect(typeThrough([
      ["down", CTRL(true)], ["down", ALT(true)], ["down", ctrl("2", { code: "Digit2", altKey: true })],
      ["up", ctrl("2", { code: "Digit2", altKey: true })], ["up", ALT(false)], ["up", CTRL(false)],
    ]).sent).toEqual([]);
    expect(typeThrough([
      ["down", CTRL(true)], ["down", ctrl("=", { code: "Equal" })], ["up", ctrl("=", { code: "Equal" })], ["up", CTRL(false)],
    ]).sent).toEqual([]);
    expect(typeThrough([
      ["down", CTRL(true)], ["down", ctrl("b", { code: "KeyB" })], ["up", ctrl("b", { code: "KeyB" })], ["up", CTRL(false)],
    ]).sent).toEqual([]);
  });

  it("a lone modifier of any kind is dropped; the key it modifies still goes, carrying it", () => {
    for (const k of ["Control", "Alt", "Shift", "Meta", "AltGraph"]) {
      expect(routeKeyDown(key(k, { ctrlKey: k === "Control", altKey: k === "Alt", shiftKey: k === "Shift" }), false))
        .toEqual({ kind: "drop" });
    }
    const r = typeThrough([
      ["down", key("Shift", { code: "ShiftLeft", shiftKey: true })],
      ["down", key("A", { code: "KeyA", shiftKey: true })],
      ["up", key("A", { code: "KeyA", shiftKey: true })],
      ["up", key("Shift", { code: "ShiftLeft" })],
    ]);
    expect(r.sent.map((m) => m.t === "key" && [m.e.type, m.e.key, m.e.modifiers])).toEqual([
      ["keyDown", "A", MOD.shift], ["keyUp", "A", MOD.shift],
    ]);
    expect(r.tookOver).toBe(true);                               // typing is the owner's input
  });

  it("AltGr: the Ctrl Windows makes up for it and the AltGr key itself go nowhere; the character does", () => {
    const r = typeThrough([
      ["down", CTRL(true)],
      ["down", key("AltGraph", { code: "AltRight", ctrlKey: true, altKey: true })],
      ["down", altGr("@", "KeyQ")],
      ["up", altGr("@", "KeyQ")],
      ["up", key("AltGraph", { code: "AltRight", ctrlKey: true })],
      ["up", CTRL(false)],
    ]);
    expect(r.sent.map((m) => m.t === "key" && [m.e.type, m.e.key, m.e.text ?? null, m.e.modifiers])).toEqual([
      ["keyDown", "@", "@", 0], ["keyUp", "@", null, 0],
    ]);
  });
});

describe("held mouse buttons (review of PR #66, LOW 2)", () => {
  const m = (over = {}) => ({ button: 0, buttons: 1, detail: 1, ...over });

  // Delta review of PR #66: a press a card cut off must never become a click when the card goes.
  it("a press a card cut off is let go off the page, never as a click where it was pressed", () => {
    const held = new HeldButtons();
    held.sent(cdpMouse("down", m(), { x: 50, y: 60 }));
    held.sent(cdpWheel({ deltaX: 0, deltaY: 100, deltaMode: 0, ...m() }, { x: 99, y: 99 }, 100, 600)!);
    // The card came up: the release was dropped under it, and the blur releases nothing.
    expect(routeMouse("up", m({ buttons: 0 }), { x: 50, y: 60, inside: true }, true)).toEqual({ kind: "held" });
    expect(held.releaseAll(true)).toEqual([]);
    expect(held.size).toBe(1);
    const out = held.releaseAll(false);                         // the card went
    expect(out).toEqual([
      { type: "mouseMoved", x: OFF_PAGE.x, y: OFF_PAGE.y, modifiers: 0, button: "left", buttons: 1, clickCount: 0,
        pointerType: "mouse" },
      { type: "mouseReleased", x: OFF_PAGE.x, y: OFF_PAGE.y, modifiers: 0, button: "left", buttons: 0, clickCount: 0,
        pointerType: "mouse" },
    ]);
    // Off the viewport, and not where it was pressed; no event of it counts as a click.
    expect(OFF_PAGE.x < 0 && OFF_PAGE.y < 0).toBe(true);
    expect(out.some((e) => e.x === 50 && e.y === 60)).toBe(false);
    expect(out.every((e) => e.clickCount === 0)).toBe(true);
    expect(held.releaseAll(false)).toEqual([]);
  });

  it("a release the page got lets the button go; two held buttons move off together, then come up one by one", () => {
    const held = new HeldButtons();
    held.sent(cdpMouse("down", m(), { x: 1, y: 1 }));
    held.sent(cdpMouse("up", m({ buttons: 0 }), { x: 1, y: 1 }));
    expect(held.size).toBe(0);
    expect(held.releaseAll(false)).toEqual([]);                 // nothing held: no move either
    held.sent(cdpMouse("down", m(), { x: 2, y: 2 }));
    held.sent(cdpMouse("down", m({ button: 2, buttons: 3 }), { x: 2, y: 2 }));
    expect(held.releaseAll(false).map((e) => [e.type, e.button, e.buttons, e.clickCount])).toEqual([
      ["mouseMoved", "left", 3, 0], ["mouseReleased", "left", 2, 0], ["mouseReleased", "right", 0, 0],
    ]);
  });
});

describe("pasteMessages (review of PR #66, LOW 3)", () => {
  const bytes = (m: { t: "text"; text: string }) => new TextEncoder().encode(JSON.stringify(m)).length;

  it("strips what a terminal paste strips: ESC and the C1 controls, never tabs or line ends", () => {
    expect(pasteMessages("a\u{1b}[201~b\u{9b}31mc\td\r\ne")).toEqual([{ t: "text", text: "a[201~b31mc\td\r\ne" }]);
    expect(pasteMessages("")).toEqual([]);
    expect(pasteMessages("\u{1b}")).toEqual([]);
    expect(pasteMessages(42)).toEqual([]);
  });

  // Delta review of PR #66: a CRLF is one line break; a chunk never ends between its CR and LF.
  it("never cuts between a CR and its LF", () => {
    const text = "a".repeat(16_382) + "\r\n" + "b".repeat(10);
    const out = pasteMessages(text);
    expect(out.map((m) => m.text).join("")).toBe(text);
    expect(out.length).toBe(2);
    expect(out[0].text).toBe("a".repeat(16_382));
    expect(out[1].text.startsWith("\r\n")).toBe(true);
    for (let i = 0; i + 1 < out.length; i++) {
      expect(out[i].text.endsWith("\r") && out[i + 1].text.startsWith("\n")).toBe(false);
    }
    // A CR that is not before an LF is cut like any other character.
    const lone = pasteMessages("a".repeat(16_382) + "\rx");
    expect(lone.map((m) => m.text)).toEqual(["a".repeat(16_382) + "\r", "x"]);
  });

  it("100 KiB goes as 7 messages, in order, nothing lost", () => {
    const text = "0123456789abcdef".repeat(6400);                  // 100 KiB
    const out = pasteMessages(text);
    expect(out.length).toBe(7);
    expect(out.map((m) => m.text).join("")).toBe(text);
    for (const m of out) expect(m.text.length).toBeLessThanOrEqual(PASTE_CHUNK);
  });

  it("every message stays under the socket's 64 KiB, whatever the text, and no character is cut in half", () => {
    for (const text of ["\u{1}".repeat(40_000), '"\\'.repeat(30_000), "é".repeat(40_000), "😀".repeat(20_000)]) {
      const out = pasteMessages(text);
      expect(out.map((m) => m.text).join("")).toBe(text);
      for (const m of out) {
        expect(bytes(m)).toBeLessThanOrEqual(PASTE_CHUNK + 32);
        expect(m.text).not.toMatch(/^[\u{DC00}-\u{DFFF}]|[\u{D800}-\u{DBFF}]$/u);
      }
    }
  });
});

describe("the pointer", () => {
  const m = (over = {}) => ({ button: 0, buttons: 0, detail: 1, ...over });

  it("presses, releases and moves as CDP takes them", () => {
    expect(cdpMouse("down", m({ buttons: 1, detail: 2, shiftKey: true }), { x: 10, y: 20 })).toEqual({
      type: "mousePressed", x: 10, y: 20, modifiers: MOD.shift, button: "left", buttons: 1, clickCount: 2,
      pointerType: "mouse",
    });
    expect(cdpMouse("up", m({ button: 2, detail: 0 }), { x: 1, y: 1 })).toMatchObject({ button: "right", clickCount: 1 });
    expect(cdpMouse("down", m({ button: 1, buttons: 4 }), { x: 1, y: 1 }).button).toBe("middle");
    expect(cdpMouse("move", m({ buttons: 1 }), { x: 1, y: 1 })).toMatchObject({ type: "mouseMoved", button: "left", clickCount: 0 });
    expect(cdpMouse("move", m(), { x: 1, y: 1 }).button).toBe("none");
  });

  it("a card holds it; Back and Forward act on release; a press off the frame is dropped", () => {
    const inside = { x: 5, y: 5, inside: true };
    expect(routeMouse("down", m(), inside, true)).toEqual({ kind: "held" });
    expect(routeMouse("down", m({ button: 3 }), inside, false)).toEqual({ kind: "drop" });
    expect(routeMouse("up", m({ button: 3 }), inside, false)).toEqual({ kind: "pane", action: "back" });
    expect(routeMouse("up", m({ button: 4 }), inside, false)).toEqual({ kind: "pane", action: "forward" });
    expect(routeMouse("down", m(), { x: 0, y: 0, inside: false }, false)).toEqual({ kind: "drop" });
    expect(routeMouse("up", m(), { x: 0, y: 0, inside: false }, false).kind).toBe("forward");
    expect(routeMouse("move", m(), { x: 0, y: 0, inside: false }, false).kind).toBe("forward");
  });
});

describe("coordinates under the HUD's zoom and the window's DPR", () => {
  it("the remote viewport is the canvas's CSS size, at DPR × zoom", () => {
    expect(viewportFor({ width: 800, height: 600 }, 160, 1.5)).toEqual({ width: 800, height: 600, deviceScaleFactor: 2.4 });
    expect(viewportFor({ width: 800.7, height: 600.2 }, 100, 1)).toEqual({ width: 800, height: 600, deviceScaleFactor: 1 });
    expect(viewportFor({ width: 800, height: 600 }, 70, 2)).toEqual({ width: 800, height: 600, deviceScaleFactor: 1.4 });
    expect(viewportFor({ width: 0, height: NaN }, 999, NaN)).toEqual({ width: 1, height: 1, deviceScaleFactor: 1 });
    const huge = viewportFor({ width: 99999, height: 99999 }, 160, 3);
    expect([huge.width, huge.height]).toEqual([VIEW_MAX, VIEW_MAX]);
    expect(huge.width * huge.height * huge.deviceScaleFactor ** 2).toBeLessThanOrEqual(MAX_DEVICE_PIXELS * 1.01);
  });

  it("a frame of the viewport fills the backing store exactly, one device pixel to one", () => {
    for (const [zoom, dpr] of [[100, 1], [160, 1.5], [70, 2], [130, 1.25]]) {
      const css = { width: 640, height: 400 };
      const view = viewportFor(css, zoom, dpr);
      const back = backingSize(css, zoom, dpr);
      const dest = frameDest(placeFrame(css, view), view, zoom, dpr);
      expect([dest.x, dest.y]).toEqual([0, 0]);
      expect(dest.width).toBeCloseTo(back.width, 6);
      expect(dest.height).toBeCloseTo(back.height, 6);
      expect(view.width * view.deviceScaleFactor).toBeCloseTo(back.width, 0);
    }
  });

  it("a frame is never stretched; an old, larger one shrinks to fit", () => {
    expect(placeFrame({ width: 800, height: 600 }, { width: 800, height: 600 })).toEqual({ x: 0, y: 0, scale: 1 });
    expect(placeFrame({ width: 800, height: 600 }, { width: 400, height: 300 })).toEqual({ x: 0, y: 0, scale: 1 });
    expect(placeFrame({ width: 400, height: 600 }, { width: 800, height: 600 }).scale).toBe(0.5);
  });

  // The plan's mutation: the zoom not divided out of a pointer position.
  it("a pointer position is divided by the zoom before it is a page point", () => {
    const view = { width: 800, height: 600 };
    const place = placeFrame(view, view);
    const box = { left: 100, top: 50 };
    // At 160% a page point (100, 50) is drawn 160 × 80 screen px into the canvas.
    expect(clientToPage({ x: 260, y: 130 }, box, 160, place, view)).toEqual({ x: 100, y: 50, inside: true });
    expect(clientToPage({ x: 170, y: 85 }, box, 70, place, view)).toEqual({ x: 100, y: 50, inside: true });
    expect(clientToPage({ x: 200, y: 100 }, box, 100, place, view)).toEqual({ x: 100, y: 50, inside: true });
    // A frame shrunk to half: twice as far into the page.
    expect(clientToPage({ x: 260, y: 130 }, box, 160, { x: 0, y: 0, scale: 0.5 }, view))
      .toEqual({ x: 200, y: 100, inside: true });
  });

  // The daemon lays a page with a min-width out wider than the pane (host.py
  // `_fit`): an 800 × 960 frame at the pane's device pixels in a 500 × 600
  // canvas. It is drawn at 500/800 and fills the backing store; a pointer
  // anywhere on it, its right edge included, is a point of the wider page.
  it("a fitted frame wider than the canvas fills it and maps input to the wider page", () => {
    const css = { width: 500, height: 600 };
    const view = { width: 800, height: 960 };
    const place = placeFrame(css, view);
    expect(place).toEqual({ x: 0, y: 0, scale: 0.625 });
    for (const [zoom, dpr] of [[100, 1], [160, 1.5]]) {
      const dest = frameDest(place, view, zoom, dpr);
      const back = backingSize(css, zoom, dpr);
      expect(dest.width).toBeCloseTo(back.width, 6);
      expect(dest.height).toBeCloseTo(back.height, 6);
    }
    const box = { left: 10, top: 20 };
    expect(clientToPage({ x: 10 + 487.5, y: 20 + 12.5 }, box, 100, place, view)).toEqual({ x: 780, y: 20, inside: true });
    expect(clientToPage({ x: 10 + 250, y: 20 + 300 }, box, 100, place, view)).toEqual({ x: 400, y: 480, inside: true });
    // At 160% the same canvas point is 1.6 × as far into the screen.
    expect(clientToPage({ x: 10 + 487.5 * 1.6, y: 20 + 12.5 * 1.6 }, box, 160, place, view))
      .toEqual({ x: 780, y: 20, inside: true });
    // Past the canvas's right edge: clamped to the page's, not the pane's.
    expect(clientToPage({ x: 10 + 520, y: 20 }, box, 100, place, view)).toEqual({ x: 800, y: 0, inside: false });
  });

  it("off the frame is clamped and marked", () => {
    const view = { width: 400, height: 300 };
    const place = placeFrame({ width: 800, height: 600 }, view);
    expect(clientToPage({ x: 600, y: 10 }, { left: 0, top: 0 }, 100, place, view)).toEqual({ x: 400, y: 10, inside: false });
    expect(clientToPage({ x: -5, y: -5 }, { left: 0, top: 0 }, 100, place, view)).toEqual({ x: 0, y: 0, inside: false });
  });

  it("an agent's target maps back onto the canvas where the pointer would hit it", () => {
    const view = { width: 800, height: 600 };
    const place = { x: 0, y: 0, scale: 0.75 };
    const r = pageToCanvas({ x: 100, y: 40, width: 80, height: 20 }, place);
    expect(r).toEqual({ x: 75, y: 30, width: 60, height: 15 });
    // Its centre, on screen at 160%, is the page point it came from.
    const centre = { x: (r.x + r.width / 2) * 1.6, y: (r.y + r.height / 2) * 1.6 };
    expect(clientToPage(centre, { left: 0, top: 0 }, 160, place, view)).toEqual({ x: 140, y: 50, inside: true });
  });
});

describe("the wheel", () => {
  it("pixels are screen pixels, divided by the zoom", () => {
    expect(normalizeWheel({ deltaX: 0, deltaY: 100, deltaMode: 0 }, 100, 600)).toEqual({ deltaX: 0, deltaY: 100 });
    expect(normalizeWheel({ deltaX: 0, deltaY: 160, deltaMode: 0 }, 160, 600)).toEqual({ deltaX: 0, deltaY: 100 });
    expect(normalizeWheel({ deltaX: -70, deltaY: 0, deltaMode: 0 }, 70, 600)).toEqual({ deltaX: -100, deltaY: 0 });
  });

  it("lines and pages are the page's own units", () => {
    expect(normalizeWheel({ deltaX: 0, deltaY: 3, deltaMode: 1 }, 160, 600)).toEqual({ deltaX: 0, deltaY: 3 * LINE_PX });
    expect(normalizeWheel({ deltaX: 0, deltaY: -1, deltaMode: 2 }, 160, 600)).toEqual({ deltaX: 0, deltaY: -600 });
  });

  it("Shift scrolls sideways; nothing, NaN or a fling are handled", () => {
    expect(normalizeWheel({ deltaX: 0, deltaY: 100, deltaMode: 0, shiftKey: true }, 100, 600)).toEqual({ deltaX: 100, deltaY: 0 });
    expect(normalizeWheel({ deltaX: 30, deltaY: 100, deltaMode: 0, shiftKey: true }, 100, 600)).toEqual({ deltaX: 30, deltaY: 100 });
    expect(normalizeWheel({ deltaX: 0, deltaY: 0, deltaMode: 0 }, 100, 600)).toBeNull();
    expect(normalizeWheel({ deltaX: NaN, deltaY: NaN, deltaMode: 0 }, 100, 600)).toBeNull();
    expect(normalizeWheel({ deltaX: 0, deltaY: 1e9, deltaMode: 0 }, 100, 600)).toEqual({ deltaX: 0, deltaY: WHEEL_MAX });
  });

  // A fitted page (host.py `_fit`) is drawn at its placement scale: pixels
  // and lines are divided by it, so a notch moves the picture as far on
  // screen as on a page drawn 1:1; a page is the fitted view's own height.
  it("a frame drawn smaller scrolls by its placement scale", () => {
    expect(normalizeWheel({ deltaX: 0, deltaY: 100, deltaMode: 0 }, 100, 960, 0.625)).toEqual({ deltaX: 0, deltaY: 160 });
    expect(normalizeWheel({ deltaX: 0, deltaY: 160, deltaMode: 0 }, 160, 960, 0.625)).toEqual({ deltaX: 0, deltaY: 160 });
    expect(normalizeWheel({ deltaX: 0, deltaY: 100, deltaMode: 0, shiftKey: true }, 100, 960, 0.625)).toEqual({ deltaX: 160, deltaY: 0 });
    expect(normalizeWheel({ deltaX: 0, deltaY: 3, deltaMode: 1 }, 100, 960, 0.625)).toEqual({ deltaX: 0, deltaY: 3 * LINE_PX * 1.6 });
    expect(normalizeWheel({ deltaX: 0, deltaY: -1, deltaMode: 2 }, 100, 960, 0.625)).toEqual({ deltaX: 0, deltaY: -960 });
    // 1 changes nothing; zero, negative or nonfinite reads as 1.
    for (const scale of [1, 0, -2, NaN, Infinity]) {
      expect(normalizeWheel({ deltaX: 0, deltaY: 100, deltaMode: 0 }, 100, 600, scale)).toEqual({ deltaX: 0, deltaY: 100 });
      expect(normalizeWheel({ deltaX: 0, deltaY: 3, deltaMode: 1 }, 100, 600, scale)).toEqual({ deltaX: 0, deltaY: 3 * LINE_PX });
    }
    // Still clamped.
    expect(normalizeWheel({ deltaX: 0, deltaY: 4000, deltaMode: 0 }, 100, 960, 0.5)).toEqual({ deltaX: 0, deltaY: WHEEL_MAX });
    expect(cdpWheel({ deltaX: 0, deltaY: 100, deltaMode: 0, button: 0, buttons: 0 }, { x: 780, y: 20 }, 100, 960, 0.625)!.deltaY)
      .toBe(160);
  });

  it("goes as a mouseWheel event at the pointer", () => {
    expect(cdpWheel({ deltaX: 0, deltaY: 160, deltaMode: 0, button: 0, buttons: 0, ctrlKey: true }, { x: 3, y: 4 }, 160, 600))
      .toEqual({
        type: "mouseWheel", x: 3, y: 4, modifiers: MOD.ctrl, button: "none", buttons: 0, clickCount: 0,
        deltaX: 0, deltaY: 100, pointerType: "mouse",
      });
    expect(cdpWheel({ deltaX: 0, deltaY: 0, deltaMode: 0, button: 0, buttons: 0 }, { x: 0, y: 0 }, 100, 600)).toBeNull();
  });
});

/** A decoder whose results the test releases, and a canvas that records what it drew. */
function stage() {
  const pending: { data: Uint8Array; ok: (b: string) => void; fail: (e: Error) => void }[] = [];
  const drawn: string[] = [];
  const acked: [number, number][] = [];
  const disposed: string[] = [];
  const pipe = new FramePipe<string>(
    (data) => new Promise<string>((ok, fail) => pending.push({ data, ok, fail })),
    (bitmap) => {
      if (bitmap === "throws") throw new Error("draw");
      drawn.push(bitmap);
    },
    (gen, id) => acked.push([gen, id]),
    (bitmap) => disposed.push(bitmap),
  );
  const settle = async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  };
  const finish = async (name: string) => {
    pending.shift()!.ok(name);
    await settle();
  };
  const frame = (id: number): Frame => ({ id, data: new Uint8Array([id]), meta: { width: 800, height: 600 } });
  return { pipe, pending, drawn, acked, disposed, finish, settle, frame };
}

describe("FramePipe", () => {
  it("decodes one frame at a time and draws them in order, acking each", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, s.frame(1));
    expect(s.pending.length).toBe(1);
    await s.finish("f1");
    s.pipe.frame(g, s.frame(2));
    await s.finish("f2");
    expect(s.drawn).toEqual(["f1", "f2"]);
    expect(s.acked).toEqual([[g, 1], [g, 2]]);
    expect(s.disposed).toEqual(["f1", "f2"]);
  });

  // B-PERF: the daemon's next frame crosses the socket while this one decodes.
  it("acks a frame when its decode starts, before it is drawn", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, s.frame(1));
    expect(s.pending.length).toBe(1);
    expect(s.acked).toEqual([[g, 1]]);
    expect(s.drawn).toEqual([]);
    await s.finish("f1");
    expect(s.drawn).toEqual(["f1"]);
    expect(s.acked).toEqual([[g, 1]]);
  });

  // The bound that replaces "acked after the draw": however fast frames come,
  // one decodes and one waits, and the waiting one is not acked until its
  // own decode starts, so the daemon (one frame in flight) sends nothing more.
  it("holds one frame decoding and one waiting, acking the waiting one only when it starts", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, s.frame(1));
    for (let id = 2; id <= 6; id++) s.pipe.frame(g, s.frame(id));
    expect(s.pending.length).toBe(1);
    expect(s.acked).toEqual([[g, 1]]);
    await s.finish("f1");
    expect(s.pending.map((p) => p.data[0])).toEqual([6]);
    expect(s.acked).toEqual([[g, 1], [g, 6]]);
    await s.finish("f6");
    expect(s.drawn).toEqual(["f1", "f6"]);
    expect(s.acked).toEqual([[g, 1], [g, 6]]);
    expect(s.pipe.stats).toEqual({ drawn: 2, skipped: 4 });
  });

  it("an ack that throws never stalls it", async () => {
    let first = true;
    const drawn: string[] = [];
    const pending: ((b: string) => void)[] = [];
    const pipe = new FramePipe<string>(
      () => new Promise<string>((ok) => pending.push(ok)),
      (bitmap) => drawn.push(bitmap),
      () => {
        if (first) { first = false; throw new Error("socket gone"); }
      },
    );
    const g = pipe.next();
    pipe.frame(g, { id: 1, data: new Uint8Array([1]), meta: { width: 1, height: 1 } });
    pending.shift()!("f1");
    for (let i = 0; i < 5; i++) await Promise.resolve();
    pipe.frame(g, { id: 2, data: new Uint8Array([2]), meta: { width: 1, height: 1 } });
    pending.shift()!("f2");
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(drawn).toEqual(["f1", "f2"]);
  });

  it("keeps only the newest frame waiting behind a decode", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, s.frame(1));
    s.pipe.frame(g, s.frame(2));
    s.pipe.frame(g, s.frame(3));
    expect(s.pending.length).toBe(1);
    await s.finish("f1");
    expect(s.pending.map((p) => p.data[0])).toEqual([3]);
    await s.finish("f3");
    expect(s.drawn).toEqual(["f1", "f3"]);
    expect(s.pipe.stats).toEqual({ drawn: 2, skipped: 1 });
  });

  // The plan's mutation: a frame from an older socket drawn.
  it("never draws a frame from an older socket, even one still decoding, nor acks one after the reattach", async () => {
    const s = stage();
    const g1 = s.pipe.next();
    s.pipe.frame(g1, s.frame(1));                   // acked on its own socket as its decode starts
    s.pipe.frame(g1, s.frame(2));                   // waiting behind the decode
    const g2 = s.pipe.next();                       // the socket dropped; a new one attached
    s.pipe.frame(g1, s.frame(3));                   // late, from the old socket
    s.pipe.frame(g2, s.frame(10));
    await s.finish("old");                          // the old decode finishes after the reattach
    expect(s.drawn).toEqual([]);
    expect(s.disposed).toEqual(["old"]);
    expect(s.acked).toEqual([[g1, 1], [g2, 10]]);     // the new socket's frame, as its decode starts
    expect(s.pending.map((p) => p.data[0])).toEqual([10]);
    await s.finish("new");
    expect(s.drawn).toEqual(["new"]);
    expect(s.acked).toEqual([[g1, 1], [g2, 10]]);
  });

  it("a failed decode or a throwing draw never stalls it", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, s.frame(1));
    s.pending.shift()!.fail(new Error("not a jpeg"));
    await s.settle();
    expect(s.acked).toEqual([[g, 1]]);
    s.pipe.frame(g, s.frame(2));
    await s.finish("throws");
    expect(s.disposed).toEqual(["throws"]);
    s.pipe.frame(g, s.frame(3));
    await s.finish("f3");
    expect(s.drawn).toEqual(["f3"]);
    expect(s.acked.map(([, id]) => id)).toEqual([1, 2, 3]);
  });

  it("an oversized frame is acked and never decoded; a cleared pipe draws nothing", async () => {
    const s = stage();
    const g = s.pipe.next();
    s.pipe.frame(g, { id: 7, data: new Uint8Array(FRAME_MAX_BYTES + 1), meta: { width: 1, height: 1 } });
    expect(s.pending.length).toBe(0);
    expect(s.acked).toEqual([[g, 7]]);
    s.pipe.frame(g, s.frame(8));
    s.pipe.clear();
    await s.finish("late");
    expect(s.drawn).toEqual([]);
  });
});

describe("cleanTitle", () => {
  it("is one line of text, with nothing hidden or reordered", () => {
    expect(cleanTitle("Inbox\n(3)\tunread")).toBe("Inbox (3) unread");
    expect(cleanTitle("Pay\u200BPal")).toBe("PayPal");
    expect(cleanTitle("invoice\u202Efdp.exe")).toBe("invoicefdp.exe");
    expect(cleanTitle("a\u2066b\u2069c\u061Cd\uFEFFe")).toBe("abcde");
    expect(cleanTitle("tag\u{E0041}\u{E0042}s")).toBe("tags");
    expect(cleanTitle("x\u2028y\u2029z\u0085w")).toBe("x y z w");
    expect(cleanTitle("\u3164\u115F")).toBe("");
    expect(cleanTitle("  many    spaces  ")).toBe("many spaces");
    expect(cleanTitle("Pay\u{34F}Pal")).toBe("PayPal");                // the combining grapheme joiner (review of PR #66)
  });

  it("is capped without cutting a character in half", () => {
    expect(cleanTitle("x".repeat(500)).length).toBe(120);
    expect(cleanTitle("abcdef", 4)).toBe("abc…");
    expect(cleanTitle("😀😀😀😀😀", 3)).toBe("😀😀…");
    expect(cleanTitle("abc", 3)).toBe("abc");
  });

  it("is empty for anything that is not a string", () => {
    for (const v of [null, undefined, 3, {}, ["a"]]) expect(cleanTitle(v)).toBe("");
  });
});
