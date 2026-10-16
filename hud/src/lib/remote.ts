// Remote mode's input and output, pure (BR-2; browser plan §2 "The stream",
// §8 and §9, amendment 1 §B.9). A Remote Browser pane is a canvas: the page
// runs in a Chrome the daemon owns, its frames arrive as JPEG over a ticketed
// socket, and the owner's keys, pointer and wheel go back as JSON that the
// daemon turns into CDP `Input.dispatch*` calls. Everything that decides what
// goes where lives here, so it is tested without a browser; BR-3b's
// `RemoteView.tsx` only applies it. Nothing here is wired in yet.
//
// **Keys.** A keydown in the pane is one of:
//   - the HUD's (`browserTakesKey` false): Ctrl+D (push-to-talk, taken by the
//     window's capture listener), the zoom keys, Ctrl+Alt+1–8, Ctrl+B and
//     Ctrl+Alt+B (the pane folds) and Ctrl+` (the panel). They never reach the
//     page and never count as taking over from an agent (amendment §B.9);
//   - the pane's own (§9): F5 and Ctrl+R reload the tab, Ctrl+L goes to the
//     address bar, Alt+←/→ and the browser keys go back and forward;
//   - a paste (Ctrl+V, Shift+Insert): never forwarded as a key — the native
//     paste event's text goes as `insertText`, so a page never reads the
//     owner's clipboard and the remote Chrome's own clipboard is never pasted;
//   - a lone modifier (Ctrl, Alt, Shift, Meta, AltGr): never sent. The Ctrl
//     of push-to-talk, pressed before its D, would otherwise reach the page
//     and pause a driving agent; every key and pointer event carries the
//     modifiers' state anyway. **The limit, stated**: a page that reacts to
//     a modifier pressed on its own — a double-Shift search, an overlay shown
//     while Alt is held, a game's Shift to run — never sees it here;
//   - everything else, forwarded as CDP key events. Copy and cut are
//     forwarded too (a page's own handlers run) and marked, so the pane can
//     ask the daemon for the selection (§8.7).
// **BR-3b must give `browserTakesKey` its exception in `useLayout`'s capture
// handler** (components/Layout.tsx), beside `terminalTakesKey`'s: today that
// handler takes Ctrl+P, Ctrl+Shift+P, F1 and Ctrl+Shift+F for the HUD
// everywhere but a terminal, so without it they would never reach the page.
// **AltGr is a character, never Ctrl+Alt.** Windows reports AltGr as Ctrl+Alt
// (with `getModifierState("AltGraph")` on Chrome), so `@` on a German layout
// arrives as Ctrl+Alt+@. Sent as such, with no text, the owner could not type
// `@`, `{` or `ę` into a page; it goes as the character it typed instead.
//
// **Coordinates.** The pane renders at the window's DPR times the HUD zoom
// (§2): the remote viewport is the canvas's own CSS size, at a device scale of
// DPR × zoom, so its frames map one to one onto device pixels and text stays
// sharp. `clientX` and `getBoundingClientRect()` are in screen pixels under
// the HUD's CSS zoom (lib/layout.ts), so a pointer position is divided by the
// zoom (`toCss`) before it becomes a page coordinate.
//
// **Frames.** `FramePipe` decodes one frame at a time and keeps only the
// newest waiting one; each socket is a generation, and a frame from an older
// socket — still decoding when the pane reattached — is never drawn.

import { ZOOM_DEFAULT, isZoom, shortcutFor, toCss, type Shortcut } from "./layout";
import { isTalkKey, type KeyLike } from "./talkkey";
import { CHUNK, cleanPaste } from "./terminal";

// ---- keys ---------------------------------------------------------------------

/** CDP's modifier bits (`Input.dispatchKeyEvent` / `dispatchMouseEvent`). */
export const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 } as const;

/** A keydown or keyup: a KeyboardEvent, or a plain object in tests. */
export interface KeyEventLike extends KeyLike {
  /** The legacy key code (Chrome on Windows: the layout's virtual key). */
  keyCode?: number;
  /** 0 standard, 1 left, 2 right, 3 numpad. */
  location?: number;
  repeat?: boolean;
}

/** `Input.dispatchKeyEvent`'s parameters, as the daemon passes them on. */
export interface CdpKeyEvent {
  type: "keyDown" | "rawKeyDown" | "keyUp";
  modifiers: number;
  key: string;
  code: string;
  windowsVirtualKeyCode: number;
  nativeVirtualKeyCode: number;
  /** The character the key types; absent for a key that types none. */
  text?: string;
  unmodifiedText?: string;
  autoRepeat?: boolean;
  isKeypad?: boolean;
  location?: number;
}

/** One printable character (a code point, not a key name like "Enter"). */
function isCharKey(key: string): boolean {
  const cps = Array.from(key || "");
  if (cps.length !== 1) return false;
  const c = cps[0].codePointAt(0)!;
  return c >= 0x20 && !(c >= 0x7f && c <= 0x9f);
}

/**
 * Whether a key was typed with AltGr: `getModifierState("AltGraph")`, or —
 * where a system does not report it — Ctrl+Alt with a key that is a character
 * but not an ASCII letter, digit or space. A real Ctrl+Alt shortcut reports
 * its letter (`k`); AltGr reports what it typed (`@`, `{`, `ę`).
 */
export function isAltGraph(e: KeyLike): boolean {
  try {
    if (e.getModifierState?.("AltGraph")) return true;
  } catch {
    /* an event that cannot say is judged on its flags */
  }
  if (!e.ctrlKey || !e.altKey || e.metaKey) return false;
  return isCharKey(e.key) && !/^[A-Za-z0-9 ]$/.test(e.key);
}

/** CDP modifier bits for an event. AltGr is not Ctrl+Alt: only Shift survives it. */
export function modifiersOf(
  e: { ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean; shiftKey?: boolean },
  altGraph = false,
): number {
  if (altGraph) return e.shiftKey ? MOD.shift : 0;
  return (e.altKey ? MOD.alt : 0) | (e.ctrlKey ? MOD.ctrl : 0) | (e.metaKey ? MOD.meta : 0)
    | (e.shiftKey ? MOD.shift : 0);
}

const VK_BY_CODE: Record<string, number> = {
  Backspace: 8, Tab: 9, Enter: 13, NumpadEnter: 13, ShiftLeft: 16, ShiftRight: 16, ControlLeft: 17,
  ControlRight: 17, AltLeft: 18, AltRight: 18, Pause: 19, CapsLock: 20, Escape: 27, Space: 32, PageUp: 33,
  PageDown: 34, End: 35, Home: 36, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  PrintScreen: 44, Insert: 45, Delete: 46, MetaLeft: 91, MetaRight: 92, ContextMenu: 93,
  NumpadMultiply: 106, NumpadAdd: 107, NumpadSubtract: 109, NumpadDecimal: 110, NumpadDivide: 111,
  NumLock: 144, ScrollLock: 145, Semicolon: 186, Equal: 187, Comma: 188, Minus: 189, Period: 190,
  Slash: 191, Backquote: 192, BracketLeft: 219, Backslash: 220, BracketRight: 221, Quote: 222,
  IntlBackslash: 226,
};

const VK_BY_KEY: Record<string, number> = {
  Backspace: 8, Tab: 9, Enter: 13, Shift: 16, Control: 17, Alt: 18, Pause: 19, CapsLock: 20, Escape: 27,
  " ": 32, PageUp: 33, PageDown: 34, End: 35, Home: 36, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39,
  ArrowDown: 40, Insert: 45, Delete: 46, Meta: 91, ContextMenu: 93,
};

/**
 * The Windows virtual key code Chrome's editing commands read: the event's own
 * `keyCode` where it has one (the layout's, on Windows), else from the
 * physical key, else from the key's name.
 */
export function virtualKeyOf(e: { key: string; code?: string; keyCode?: number }): number {
  const kc = e.keyCode;
  if (typeof kc === "number" && Number.isInteger(kc) && kc > 0 && kc !== 229) return kc;
  const code = e.code || "";
  let m = /^Key([A-Z])$/.exec(code);
  if (m) return m[1].charCodeAt(0);
  m = /^Digit([0-9])$/.exec(code);
  if (m) return 48 + Number(m[1]);
  m = /^Numpad([0-9])$/.exec(code);
  if (m) return 96 + Number(m[1]);
  m = /^F([0-9]{1,2})$/.exec(code) ?? /^F([0-9]{1,2})$/.exec(e.key);
  if (m && Number(m[1]) >= 1 && Number(m[1]) <= 24) return 111 + Number(m[1]);
  if (VK_BY_CODE[code] !== undefined) return VK_BY_CODE[code];
  if (VK_BY_KEY[e.key] !== undefined) return VK_BY_KEY[e.key];
  if (/^[a-z]$/i.test(e.key)) return e.key.toUpperCase().charCodeAt(0);
  if (/^[0-9]$/.test(e.key)) return e.key.charCodeAt(0);
  return 0;
}

/**
 * What a keydown types, if anything. A character with no Ctrl, Alt or Meta
 * (Shift is part of typing); a character typed with AltGr; Enter's carriage
 * return. Ctrl+A, Alt+F and the like type nothing: their keydown still
 * reaches the page, so its own shortcuts run.
 */
export function textOf(e: KeyLike, altGraph = isAltGraph(e)): string {
  if (e.key === "Enter") return e.ctrlKey || e.altKey || e.metaKey ? "" : "\r";
  if (!isCharKey(e.key)) return "";
  if (altGraph) return e.key;
  return e.ctrlKey || e.altKey || e.metaKey ? "" : e.key;
}

/** One keydown or keyup as `Input.dispatchKeyEvent` takes it. */
export function cdpKey(e: KeyEventLike, phase: "down" | "up"): CdpKeyEvent {
  const altGraph = isAltGraph(e);
  const vk = virtualKeyOf(e);
  const location = typeof e.location === "number" && e.location >= 0 && e.location <= 3 ? e.location : 0;
  const out: CdpKeyEvent = {
    type: "keyUp",
    modifiers: modifiersOf(e, altGraph),
    key: e.key,
    code: e.code ?? "",
    windowsVirtualKeyCode: vk,
    nativeVirtualKeyCode: vk,
    location,
  };
  if (location === 3) out.isKeypad = true;
  if (phase === "up") return out;
  const text = textOf(e, altGraph);
  out.type = text ? "keyDown" : "rawKeyDown";
  if (text) {
    out.text = text;
    out.unmodifiedText = text;
  }
  if (e.repeat) out.autoRepeat = true;
  return out;
}

/**
 * The HUD's own keys that stay the HUD's in a Browser pane. Like
 * `terminalTakesKey` (lib/terminal.ts), but the other way round: a page binds
 * far more keys than a shell, so the page gets every key but these.
 *   - the zoom keys: let one through and it is Chrome's page zoom, which the
 *     HUD's control cannot see (and a page that ate it would strand the owner
 *     at the wrong zoom);
 *   - Ctrl+Alt+1–8 (the plan's "Ctrl+Alt+N"): focus the pane in that slot,
 *     the only way out of the pane by key;
 *   - Ctrl+B and Ctrl+Alt+B (the pane folds) and Ctrl+` (the panel): the
 *     window's layout, which no page may change.
 * Quick open, the palette and find in files (Ctrl+P, Ctrl+Shift+P, F1,
 * Ctrl+Shift+F) are the page's here, as Ctrl+P is the shell's in a terminal:
 * web apps bind them (vscode.dev, Notion), and the HUD's are a click on any
 * other pane away.
 */
const HUD_KEEPS: ReadonlySet<Shortcut> = new Set<Shortcut>([
  "zoomIn", "zoomOut", "zoomReset", "toggleLeft", "toggleRight", "togglePanel",
  "focus1", "focus2", "focus3", "focus4", "focus5", "focus6", "focus7", "focus8",
]);

/**
 * Whether a keydown in a Browser pane goes to the page. Never Ctrl+D: it is
 * push-to-talk in a Browser pane, as everywhere but a shell (App.tsx's
 * capture listener takes it, and a page never sees it). Never the HUD's own
 * keys above. Anything else, yes.
 */
export function browserTakesKey(e: KeyLike): boolean {
  if (isTalkKey(e)) return false;
  const what = shortcutFor(e);
  return what === null || !HUD_KEEPS.has(what);
}

/** What the pane itself does with a key (§9) instead of forwarding it. */
export type PaneAction = "reload" | "hardReload" | "address" | "back" | "forward";

/**
 * The pane's own keys: F5 and Ctrl+R reload the tab (Shift or Ctrl: without
 * the cache) — never the HUD; Ctrl+L goes to the address bar; Alt+←/→ and the
 * keyboard's Back/Forward keys move through the tab's history. Never under
 * AltGr (which arrives as Ctrl+Alt).
 */
export function paneKeyAction(e: KeyLike): PaneAction | null {
  if (e.metaKey || isAltGraph(e)) return null;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "F5" && !e.altKey) return e.ctrlKey || e.shiftKey ? "hardReload" : "reload";
  if (k === "BrowserRefresh") return "reload";
  if (e.ctrlKey && !e.altKey && k === "r") return e.shiftKey ? "hardReload" : "reload";
  if (e.ctrlKey && !e.altKey && !e.shiftKey && k === "l") return "address";
  if (k === "BrowserBack") return "back";
  if (k === "BrowserForward") return "forward";
  if (e.altKey && !e.ctrlKey && !e.shiftKey) {
    if (k === "ArrowLeft") return "back";
    if (k === "ArrowRight") return "forward";
  }
  return null;
}

/** The clipboard keys (§8.7): a page never reads or writes the owner's clipboard. */
export type ClipKey = "copy" | "cut" | "paste";

export function clipKey(e: KeyLike): ClipKey | null {
  if (e.altKey || e.metaKey || isAltGraph(e)) return null;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (e.ctrlKey) {
    if (k === "v") return "paste";                // Ctrl+V, and Ctrl+Shift+V
    if (e.shiftKey) return null;
    if (k === "c" || k === "Insert") return "copy";
    if (k === "x") return "cut";
    return null;
  }
  if (e.shiftKey && k === "Insert") return "paste";
  if (e.shiftKey && k === "Delete") return "cut";
  return null;
}

/** Where a keydown in a Browser pane goes. */
export type KeyRoute =
  /** A card is up: dropped. Its default is left alone and it bubbles — Escape denies. */
  | { kind: "held" }
  /** The HUD's own key: never reaches the page, never counts as taking over. */
  | { kind: "hud" }
  | { kind: "pane"; action: PaneAction }
  /** Let the native paste event fire; its text goes to the page as `insertText`. */
  | { kind: "paste" }
  /**
   * A lone modifier (its state rides on the next key or pointer event), or
   * mid-composition, an IME's key (keyCode 229, whatever its `key`), a dead
   * key or an unidentified key: nothing is sent. What the IME or dead key
   * types reaches the page as text (lib/remoteime.ts), so its keys never do.
   */
  | { kind: "drop" }
  /** Sent to the page; the pane prevents its default. Copy and cut also ask for the selection. */
  | { kind: "forward"; event: CdpKeyEvent; clip: "copy" | "cut" | null };

/**
 * The modifier keys. A lone press or release of one is never sent (review of
 * PR #66): pressed the usual way — Ctrl first, then D — push-to-talk's Ctrl
 * is a keydown of its own, which is neither the talk key nor a HUD shortcut,
 * so it would reach the page and count as taking over from a driving agent
 * (amendment §B.9: the HUD's keys never reach the page and never count). The
 * page still learns them: every key and pointer event carries `modifiers`.
 */
export const MODIFIER_KEYS: ReadonlySet<string> = new Set([
  "Control", "Alt", "Shift", "Meta", "AltGraph", "OS", "Super", "Hyper", "Fn", "FnLock",
]);

export function isModifierKey(key: unknown): boolean {
  return typeof key === "string" && MODIFIER_KEYS.has(key);
}

export function routeKeyDown(e: KeyEventLike, blocked: boolean): KeyRoute {
  if (blocked) return { kind: "held" };
  if (!browserTakesKey(e)) return { kind: "hud" };
  if (isModifierKey(e.key)) return { kind: "drop" };
  if (e.isComposing || e.keyCode === 229 || e.key === "Process" || e.key === "Dead" || e.key === "Unidentified" || !e.key) {
    return { kind: "drop" };
  }
  const action = paneKeyAction(e);
  if (action) return { kind: "pane", action };
  const clip = clipKey(e);
  if (clip === "paste") return { kind: "paste" };
  return { kind: "forward", event: cdpKey(e, "down"), clip };
}

/**
 * The keys whose keydown reached the page, so only their keyup does (a keyup
 * whose keydown was the HUD's — the D of a push-to-talk — is never sent, nor
 * a lone modifier's), and so a window that loses focus mid-press can let them
 * all go: a page that tracks held keys must not keep a key down for ever.
 *
 * **Nothing is sent under a card** (review of PR #66): a card taking focus is
 * itself a blur, so BR-3b's blur handler calls `releaseAll(blocked)` with a
 * card up — which sends nothing and keeps every key, as `released` does for a
 * keyup under a card. BR-3b calls `releaseAll(false)` once the card goes (and
 * on a later blur), and the page's keys come up then.
 */
export class HeldKeys {
  private down = new Map<string, CdpKeyEvent>();

  private static id(e: { key: string; code?: string }): string {
    return e.code || `key:${e.key}`;
  }

  pressed(e: KeyEventLike, sent: CdpKeyEvent) {
    if (isModifierKey(e.key)) return;
    this.down.set(HeldKeys.id(e), sent);
  }

  /**
   * The keyup to send, or null when its keydown never reached the page. Under
   * a card: null, and the key is kept for `releaseAll` once the card goes.
   */
  released(e: KeyEventLike, blocked: boolean): CdpKeyEvent | null {
    const id = HeldKeys.id(e);
    if (isModifierKey(e.key) || !this.down.has(id) || blocked) return null;
    this.down.delete(id);
    return cdpKey(e, "up");
  }

  /** A keyup for every key still down, and forget them — or, under a card, nothing (they wait). */
  releaseAll(blocked: boolean): CdpKeyEvent[] {
    if (blocked) return [];
    const out = [...this.down.values()].map((d) => ({
      type: "keyUp" as const, modifiers: 0, key: d.key, code: d.code,
      windowsVirtualKeyCode: d.windowsVirtualKeyCode, nativeVirtualKeyCode: d.nativeVirtualKeyCode,
      location: d.location ?? 0,
    }));
    this.down.clear();
    return out;
  }

  get size(): number {
    return this.down.size;
  }
}

// ---- the pointer --------------------------------------------------------------

/** `Input.dispatchMouseEvent`'s parameters. */
export interface CdpMouseEvent {
  type: "mousePressed" | "mouseReleased" | "mouseMoved" | "mouseWheel";
  x: number;
  y: number;
  modifiers: number;
  button: "none" | "left" | "middle" | "right" | "back" | "forward";
  /** Left 1, right 2, middle 4, back 8, forward 16 (the DOM's own bits). */
  buttons: number;
  clickCount: number;
  deltaX?: number;
  deltaY?: number;
  pointerType: "mouse";
}

/** A MouseEvent, or a plain object in tests. Use mouse events: a pointer event's `detail` is 0. */
export interface MouseLike {
  button: number;
  buttons: number;
  /** The click count (2 on a double click's second press). */
  detail?: number;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  metaKey?: boolean;
}

const BUTTONS = ["left", "middle", "right", "back", "forward"] as const;

function heldButton(buttons: number): CdpMouseEvent["button"] {
  if (buttons & 1) return "left";
  if (buttons & 2) return "right";
  if (buttons & 4) return "middle";
  return "none";
}

/** A press, release or move at a page point, as `Input.dispatchMouseEvent` takes it. */
export function cdpMouse(kind: "down" | "up" | "move", e: MouseLike, at: { x: number; y: number }): CdpMouseEvent {
  const buttons = Number.isInteger(e.buttons) && e.buttons >= 0 ? e.buttons & 31 : 0;
  const pressed = kind !== "move";
  const button = pressed ? (BUTTONS[e.button] ?? "none") : heldButton(buttons);
  const count = typeof e.detail === "number" && Number.isInteger(e.detail) && e.detail > 0 ? Math.min(e.detail, 3) : 1;
  return {
    type: kind === "down" ? "mousePressed" : kind === "up" ? "mouseReleased" : "mouseMoved",
    x: at.x,
    y: at.y,
    modifiers: modifiersOf(e),
    button,
    buttons,
    clickCount: pressed ? count : 0,
    pointerType: "mouse",
  };
}

/** Where a mouse event in a Browser pane goes. */
export type MouseRoute =
  | { kind: "held" }
  /** Outside the frame (its letterbox), or a back/forward press: nothing is sent. */
  | { kind: "drop" }
  /** The mouse's own Back and Forward buttons, on release (when Chrome would act). */
  | { kind: "pane"; action: "back" | "forward" }
  | { kind: "forward"; event: CdpMouseEvent };

/**
 * A press outside the frame sends nothing; a move or release outside it is
 * clamped to its edge (a drag that left the pane still ends). The pane must
 * prevent the default of Back/Forward releases, or Chrome takes the HUD's own
 * window back a page.
 */
export function routeMouse(kind: "down" | "up" | "move", e: MouseLike, at: PagePoint, blocked: boolean): MouseRoute {
  if (blocked) return { kind: "held" };
  if (kind !== "move" && (e.button === 3 || e.button === 4)) {
    return kind === "up" ? { kind: "pane", action: e.button === 3 ? "back" : "forward" } : { kind: "drop" };
  }
  if (kind === "down" && !at.inside) return { kind: "drop" };
  return { kind: "forward", event: cdpMouse(kind, e, at) };
}

/**
 * Where the page is told a cut-off gesture ended: just outside its viewport,
 * above and left of it (CSS px of the remote viewport, as every page point).
 */
export const OFF_PAGE = { x: -1, y: -1 } as const;

/**
 * The mouse buttons the page holds down (review of PR #66): a press it got and
 * no release yet. A drag a card cut off — its release dropped under the card —
 * or one whose release landed outside the window would leave the page's
 * button down. `releaseAll(false)` lets each go; under a card it sends nothing
 * and keeps them, as `HeldKeys` does. BR-3b calls it when the card goes, and
 * on blur.
 *
 * **The release it sends can never finish a click** (delta review of PR #66).
 * The owner pressed and never let go — the card came, and input under a card
 * is dropped, never delivered later (plan §8.3) — so the press must not become
 * a click on whatever was under it. Released where it was pressed, with
 * `clickCount: 1`, Chrome would fire `click` there: a button the owner never
 * finished pressing would act. So the gesture ends the way a drag that leaves
 * the window ends in any browser: first the pointer moves, button still held,
 * to `OFF_PAGE`, then each button is released there. A click needs its press
 * and its release on the same element, and nothing is under a point outside
 * the viewport; and every one of these events carries `clickCount: 0`, which
 * Chrome does not count as a click either — two independent reasons, since
 * neither can be checked without a real Chrome (BR-1b's fixture must assert
 * that no `click` fires from a press a card cut off). The cost, stated: a
 * page tracking the drag sees it end at the corner (a slider dragged when the
 * card came may jump toward its minimum), exactly as if the owner had dragged
 * out of the window — a visible, ordinary outcome, where a click is an action.
 */
export class HeldButtons {
  private down = new Set<CdpMouseEvent["button"]>();

  /** Every mouse event the page was sent. */
  sent(ev: CdpMouseEvent) {
    if (ev.type === "mouseWheel" || ev.button === "none") return;
    if (ev.type === "mousePressed") this.down.add(ev.button);
    else if (ev.type === "mouseReleased") this.down.delete(ev.button);
  }

  /**
   * A move off the page with every held button, then a release of each there,
   * none of them a click — and forget them. Under a card: nothing (they wait).
   */
  releaseAll(blocked: boolean): CdpMouseEvent[] {
    if (blocked || this.down.size === 0) return [];
    const bit = { none: 0, left: 1, right: 2, middle: 4, back: 8, forward: 16 } as const;
    const held = [...this.down];
    let buttons = held.reduce((n, b) => n | bit[b], 0);
    const out: CdpMouseEvent[] = [{
      type: "mouseMoved", x: OFF_PAGE.x, y: OFF_PAGE.y, modifiers: 0, button: heldButton(buttons), buttons,
      clickCount: 0, pointerType: "mouse",
    }];
    for (const button of held) {
      buttons &= ~bit[button];
      out.push({
        type: "mouseReleased", x: OFF_PAGE.x, y: OFF_PAGE.y, modifiers: 0, button, buttons,
        clickCount: 0, pointerType: "mouse",
      });
    }
    this.down.clear();
    return out;
  }

  get size(): number {
    return this.down.size;
  }
}

/**
 * The owner's input as the pane sends it on its socket (BR-1b turns each into
 * one CDP call): a key, a pointer or wheel event, or text — a paste, or an
 * IME's commit — for `Input.insertText`. The socket's other messages (a frame
 * ack, a resize, Resume, Stop) are not input; `drive.isTakeover` tells them
 * apart.
 */
export type InputMsg =
  | { t: "key"; e: CdpKeyEvent }
  | { t: "mouse"; e: CdpMouseEvent }
  | { t: "text"; text: string };

/** One paste message's text, at most, as UTF-8 once JSON-escaped (ws.py takes 64 KiB a message). */
export const PASTE_CHUNK = CHUNK;

/** A code point's size inside a JSON string, as UTF-8: its escape's length, or its encoding's. */
function jsonBytes(cp: string): number {
  const esc = JSON.stringify(cp).slice(1, -1);
  if (esc !== cp) return esc.length;                    // an ASCII escape: \n, \", \\, \u001f, a lone surrogate
  const c = cp.codePointAt(0)!;
  return c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
}

/**
 * The owner's paste, as the `{t:"text"}` messages `Input.insertText` takes
 * (§8.7: the text comes from the HUD's own paste event, never the page's
 * clipboard). Made inert as a control stream the way a terminal paste is
 * (`cleanPaste`, lib/terminal.ts: ESC and the C1 controls go; tabs and line
 * ends stay), then cut at code points into chunks of at most 16 KiB each once
 * JSON-escaped — well under the socket's 64 KiB message cap, whatever the text
 * holds. An empty paste is no message.
 */
export function pasteMessages(text: unknown): { t: "text"; text: string }[] {
  if (typeof text !== "string") return [];
  const clean = cleanPaste(text);
  const out: { t: "text"; text: string }[] = [];
  let cur = "";
  let size = 0;
  for (const cp of clean) {
    const n = jsonBytes(cp);
    if (size + n > PASTE_CHUNK && cur) {
      // Never between a CR and its LF (delta review of PR #66): a page that
      // reads each insertText as it lands would see a lone CR and then a lone
      // LF — two line breaks where the owner pasted one. The CR goes on to
      // the next chunk with its LF.
      if (cp === "\n" && cur.endsWith("\r") && cur.length > 1) {
        out.push({ t: "text", text: cur.slice(0, -1) });
        cur = "\r";
        size = jsonBytes("\r");
      } else {
        out.push({ t: "text", text: cur });
        cur = "";
        size = 0;
      }
    }
    cur += cp;
    size += n;
  }
  if (cur) out.push({ t: "text", text: cur });
  return out;
}

// ---- coordinates ----------------------------------------------------------------

export interface Size {
  width: number;
  height: number;
}

/** The largest remote viewport, in CSS px each way. */
export const VIEW_MAX = 4096;
/** The most device pixels a frame may hold; past it the device scale is lowered. */
export const MAX_DEVICE_PIXELS = 4096 * 4096;

export interface Viewport extends Size {
  deviceScaleFactor: number;
}

/** The HUD zoom as a factor (1.6 at 160%); anything that is not a level is 100%. */
const zoomFactor = (zoomPct: number) => (isZoom(zoomPct) ? zoomPct : ZOOM_DEFAULT) / 100;

const finitePos = (n: number, d: number) => (Number.isFinite(n) && n > 0 ? n : d);

/**
 * The remote viewport for a canvas `css` (its own CSS size, `offsetWidth` ×
 * `offsetHeight`) in a window at `dpr` with the HUD at `zoomPct`: that many
 * CSS px, at a device scale of DPR × zoom — so the page is zoomed with the
 * HUD and its frames are exactly the canvas's device pixels. A huge pane at a
 * high DPR is drawn at a lower scale rather than a frame past MAX_DEVICE_PIXELS.
 */
export function viewportFor(css: Size, zoomPct: number, dpr: number): Viewport {
  const width = Math.min(VIEW_MAX, Math.max(1, Math.floor(finitePos(css.width, 1))));
  const height = Math.min(VIEW_MAX, Math.max(1, Math.floor(finitePos(css.height, 1))));
  let scale = finitePos(dpr, 1) * zoomFactor(zoomPct);
  const cap = Math.sqrt(MAX_DEVICE_PIXELS / (width * height));
  if (scale > cap) scale = cap;
  return { width, height, deviceScaleFactor: Math.round(scale * 1000) / 1000 };
}

/** The canvas's backing store: its CSS size in device pixels (× zoom × DPR). */
export function backingSize(css: Size, zoomPct: number, dpr: number): Size {
  const k = zoomFactor(zoomPct) * finitePos(dpr, 1);
  return {
    width: Math.max(1, Math.round(finitePos(css.width, 1) * k)),
    height: Math.max(1, Math.round(finitePos(css.height, 1) * k)),
  };
}

/** Where a frame sits in the canvas, in the canvas's CSS px: its top-left and CSS px per page px. */
export interface Placement {
  x: number;
  y: number;
  scale: number;
}

/**
 * A frame of `view` (its viewport, page CSS px: the screencast metadata's
 * `deviceWidth` × `deviceHeight`) in a canvas of `css`: top-left, at 1:1 when
 * it fits, shrunk to fit when the pane has just narrowed and the next frame
 * has not come yet — never stretched past 1:1.
 */
export function placeFrame(css: Size, view: Size): Placement {
  const vw = finitePos(view.width, 1);
  const vh = finitePos(view.height, 1);
  const scale = Math.min(1, finitePos(css.width, vw) / vw, finitePos(css.height, vh) / vh);
  return { x: 0, y: 0, scale };
}

/** The frame's rectangle in the canvas's backing store (device px), for `drawImage`. */
export function frameDest(place: Placement, view: Size, zoomPct: number, dpr: number) {
  const k = zoomFactor(zoomPct) * finitePos(dpr, 1);
  return {
    x: place.x * k,
    y: place.y * k,
    width: finitePos(view.width, 1) * place.scale * k,
    height: finitePos(view.height, 1) * place.scale * k,
  };
}

/** A frame's JPEG as a bitmap (the Remote pane's `FramePipe` decode). */
export function decodeFrame(bytes: Uint8Array): Promise<ImageBitmap> {
  return createImageBitmap(new Blob([bytes.slice().buffer], { type: "image/jpeg" }));
}

/**
 * Draw a decoded frame showing viewport `meta` into `target`, a canvas of CSS
 * size `css`: its backing store sized to the device pixels, the frame placed
 * by `placeFrame`. Returns the placement, and whether anything was drawn.
 */
export function paintFrame(
  target: HTMLCanvasElement, bitmap: CanvasImageSource, meta: Size, css: Size, zoomPct: number, dpr: number,
): { place: Placement; drawn: boolean } {
  const size = backingSize(css, zoomPct, dpr);
  if (target.width !== size.width) target.width = size.width;
  if (target.height !== size.height) target.height = size.height;
  const place = placeFrame(css, meta);
  const d = frameDest(place, meta, zoomPct, dpr);
  const ctx = target.getContext("2d");
  if (!ctx) return { place, drawn: false };
  ctx.clearRect(0, 0, target.width, target.height);
  ctx.drawImage(bitmap, d.x, d.y, d.width, d.height);
  return { place, drawn: true };
}

/** A page point, and whether the pointer was over the frame at all. */
export interface PagePoint {
  x: number;
  y: number;
  inside: boolean;
}

/**
 * A pointer position (`clientX`/`clientY`: screen px) → a page point (CSS px
 * of the remote viewport). `box` is the canvas's `getBoundingClientRect()`
 * (screen px), so the offset into it is divided by the zoom before it meets
 * the placement (canvas CSS px). Outside the frame the point is clamped to its
 * edge and marked.
 */
export function clientToPage(
  client: { x: number; y: number }, box: { left: number; top: number }, zoomPct: number,
  place: Placement, view: Size,
): PagePoint {
  const cx = toCss(client.x - box.left, zoomPct);
  const cy = toCss(client.y - box.top, zoomPct);
  const s = finitePos(place.scale, 1);
  const px = (cx - place.x) / s;
  const py = (cy - place.y) / s;
  const vw = finitePos(view.width, 1);
  const vh = finitePos(view.height, 1);
  const inside = px >= 0 && py >= 0 && px < vw && py < vh;
  const round = (n: number) => Math.round(n * 100) / 100;
  return { x: round(Math.min(vw, Math.max(0, px))), y: round(Math.min(vh, Math.max(0, py))), inside };
}

/** A page rectangle (an agent's target, from the daemon) → the canvas's CSS px, for the outline. */
export function pageToCanvas(rect: { x: number; y: number; width: number; height: number }, place: Placement) {
  return {
    x: place.x + rect.x * place.scale,
    y: place.y + rect.y * place.scale,
    width: rect.width * place.scale,
    height: rect.height * place.scale,
  };
}

// ---- the wheel ------------------------------------------------------------------

export interface WheelLike {
  deltaX: number;
  deltaY: number;
  /** 0 pixels, 1 lines, 2 pages. */
  deltaMode: number;
  shiftKey?: boolean;
}

/** Page px per line (Chrome's own step). */
export const LINE_PX = 40;
/** The most one wheel event scrolls, in page px each way. */
export const WHEEL_MAX = 5000;

/**
 * A wheel event → page CSS px. Pixels are screen pixels, divided by the zoom,
 * so the page moves as far on screen as the owner's Chrome would move it;
 * lines are 40 page px, pages the viewport's height. Shift turns a vertical
 * wheel sideways (Chrome does this before an event reaches the page; the
 * remote Chrome never sees the owner's device). Null when it moves nothing.
 * `placeScale` is the frame's placement (`placeFrame`): a page the daemon
 * laid out wider than the pane (host.py `_fit`) is drawn at W/C, so pixels
 * and lines are divided by it and a notch moves the picture as far on
 * screen as before; a page is already the fitted view's height.
 */
export function normalizeWheel(
  e: WheelLike, zoomPct: number, viewHeight: number, placeScale = 1,
): { deltaX: number; deltaY: number } | null {
  let dx = Number.isFinite(e.deltaX) ? e.deltaX : 0;
  let dy = Number.isFinite(e.deltaY) ? e.deltaY : 0;
  if (e.shiftKey && dx === 0) {
    dx = dy;
    dy = 0;
  }
  const s = finitePos(placeScale, 1);
  if (e.deltaMode === 1) {
    dx *= LINE_PX / s;
    dy *= LINE_PX / s;
  } else if (e.deltaMode === 2) {
    const page = finitePos(viewHeight, 800);
    dx *= page;
    dy *= page;
  } else {
    dx = toCss(dx, zoomPct) / s;
    dy = toCss(dy, zoomPct) / s;
  }
  const clamp = (n: number) => Math.round(Math.min(WHEEL_MAX, Math.max(-WHEEL_MAX, n)) * 100) / 100;
  dx = clamp(dx);
  dy = clamp(dy);
  if (dx === 0 && dy === 0) return null;
  return { deltaX: dx + 0, deltaY: dy + 0 };
}

/** A wheel event at a page point, as `Input.dispatchMouseEvent` takes it. */
export function cdpWheel(
  e: WheelLike & MouseLike, at: { x: number; y: number }, zoomPct: number, viewHeight: number, placeScale = 1,
): CdpMouseEvent | null {
  const d = normalizeWheel(e, zoomPct, viewHeight, placeScale);
  if (!d) return null;
  return {
    type: "mouseWheel", x: at.x, y: at.y, modifiers: modifiersOf(e), button: "none",
    buttons: Number.isInteger(e.buttons) && e.buttons >= 0 ? e.buttons & 31 : 0, clickCount: 0,
    deltaX: d.deltaX, deltaY: d.deltaY, pointerType: "mouse",
  };
}

// ---- frames ---------------------------------------------------------------------

/** The screencast's metadata the pane needs: the viewport the frame shows. */
export interface FrameMeta {
  /** Page CSS px (CDP `deviceWidth` / `deviceHeight`). */
  width: number;
  height: number;
}

export interface Frame {
  /** The daemon's number for it, echoed in the ack. */
  id: number;
  data: Uint8Array;
  meta: FrameMeta;
}

/** A frame larger than this is never decoded (a 4K JPEG at quality 80 is ~1–2 MB). */
export const FRAME_MAX_BYTES = 8 * 1024 * 1024;

/**
 * Frames, decoded and drawn **one at a time, latest only, each tagged with the
 * socket it came from** (the terminal's `OutputPipe` rule, PR #28 review).
 *
 *   - `next()` starts a generation: a new socket. Whatever an older one left
 *     waiting is dropped, and a frame of it still decoding is **never drawn**
 *     when it finishes — a reattached pane must not flash the old page;
 *   - while one frame decodes, a newer arrival replaces any frame waiting
 *     behind it: a slow window shows the newest picture, never a backlog;
 *   - each frame is acked on its own generation **when its decode starts**
 *     (B-PERF, 2026-10-15; it was after the draw), so the daemon's next frame
 *     crosses the socket while this one decodes instead of after it: at most
 *     one frame decoding and one waiting here, and the daemon still sends
 *     the next only after an ack. One from an older generation is never
 *     acked, and **neither is one replaced while another decoded**: an ack
 *     names the newest frame taken, so the daemon must read it as "up to
 *     here", never wait on each id it sent;
 *   - a decode or a draw that throws never stalls it, and every bitmap is
 *     disposed, drawn or not.
 */
export class FramePipe<B> {
  private current = 0;
  private waiting: { gen: number; frame: Frame } | null = null;
  private busy = false;
  private drawn = 0;
  private skipped = 0;

  constructor(
    private decode: (data: Uint8Array) => Promise<B>,
    private draw: (bitmap: B, meta: FrameMeta) => void,
    private ack: (gen: number, id: number) => void,
    private dispose: (bitmap: B) => void = () => {},
  ) {}

  get gen(): number {
    return this.current;
  }

  /** A new socket: a new generation, and every older frame dropped. */
  next(): number {
    this.current += 1;
    this.waiting = null;
    return this.current;
  }

  /** The pane is gone: nothing more is drawn. */
  clear() {
    this.current += 1;
    this.waiting = null;
  }

  frame(gen: number, frame: Frame) {
    if (gen !== this.current) return;
    if (!frame || !(frame.data instanceof Uint8Array) || frame.data.length > FRAME_MAX_BYTES) {
      if (frame && Number.isInteger(frame.id)) this.ack(gen, frame.id);
      return;
    }
    if (this.waiting) this.skipped += 1;
    this.waiting = { gen, frame };
    void this.pump();
  }

  private async pump() {
    if (this.busy) return;
    while (this.waiting) {
      const { gen, frame } = this.waiting;
      this.waiting = null;
      if (gen !== this.current) continue;
      this.busy = true;
      try {
        this.ack(gen, frame.id);
      } catch {
        /* an ack that throws never stalls the pipe */
      }
      let bitmap: B | null = null;
      try {
        bitmap = await this.decode(frame.data);
      } catch {
        bitmap = null;
      }
      try {
        // An older socket's frame, finished after a reattach: never drawn.
        if (gen !== this.current) continue;
        if (bitmap !== null) {
          try {
            this.draw(bitmap, frame.meta);
            this.drawn += 1;
          } catch {
            /* a draw that throws loses that frame, never the pipe */
          }
        }
      } finally {
        if (bitmap !== null) {
          try {
            this.dispose(bitmap);
          } catch {
            /* ditto */
          }
        }
        this.busy = false;
      }
    }
  }

  /** Frames drawn, and frames replaced before they were drawn (for the pane's own stats). */
  get stats(): { drawn: number; skipped: number } {
    return { drawn: this.drawn, skipped: this.skipped };
  }
}

// ---- text from the page -----------------------------------------------------------

/** A title's cap; an address or a file name takes its own. */
export const TITLE_CAP = 120;

// What is never drawn from a page's text: C0/C1 controls, format characters
// (zero-width, bidi embeddings, overrides and isolates, tags, the BOM) and the
// line and paragraph separators — `untrusted.UNSEEN_CATEGORIES`, Cc Cf Zl Zp —
// plus the variation selectors, the combining grapheme joiner (U+034F) and
// the Hangul fillers, which draw nothing.
const UNSEEN = /[\p{Cc}\p{Zl}\p{Zp}]/gu;
const VANISH = /[\p{Cf}\u{E0000}-\u{E007F}\uFE00-\uFE0F\u{E0100}-\u{E01EF}\u{34F}\u115F\u1160\u3164\uFFA0]/gu;

/**
 * Text a page chose — a title, an address, a dialog's words, a file name —
 * made safe to draw as one line of React text (§8.6): controls and line
 * breaks become spaces, characters that hide or reorder text (zero-width,
 * bidi controls, tags) are removed, runs of space collapse, and it is capped
 * at `cap` characters (never cutting a character in half). It is only ever
 * text: never `innerHTML` and never `document.title`.
 */
export function cleanTitle(raw: unknown, cap: number = TITLE_CAP): string {
  if (typeof raw !== "string") return "";
  const one = raw.replace(UNSEEN, " ").replace(VANISH, "").replace(/\s+/gu, " ").trim();
  const cps = Array.from(one);
  const n = Math.max(1, Math.floor(cap));
  return cps.length <= n ? one : cps.slice(0, n - 1).join("").trimEnd() + "…";
}
