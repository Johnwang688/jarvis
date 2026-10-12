// The DOM half of "no Browser frame takes the keyboard on its own" (R-1;
// amendment 2 §E.8; the rules are lib/frameguard.ts).
//
// **What Chrome does** (measured in Chromium 149, headless and headed under
// WSLg, a sandboxed frame on another loopback port calling `el.focus()` or
// `window.focus()` with no user activation):
//   1. `blur` on the HUD element that had focus (activeElement is <body>);
//   2. `focusout` on it, `relatedTarget` null;
//   3. the window's own `blur`, with `document.activeElement` already the
//      iframe. No `focus` or `focusin` fires on the iframe element.
// All three are dispatched as the focus moves, before any key reaches the
// page, so the guard decides in (3), synchronously, and a theft is removed
// before the next key event. A frame stealing from **another frame** (the
// keyboard already in a page the owner clicked) fires nothing at all in the
// parent: `focusin` is listened for as the plan says, but what catches that
// case is the per-frame check (`requestAnimationFrame`) that runs while the
// keyboard is in a frame, with the 250 ms check as the belt.
//
// **The pointer** (also measured): headless, the parent sees `pointerover`
// and `pointerout` on the iframe element; headed, where the frame runs in a
// process of its own, it sees **nothing** while the pointer is over the frame
// and the iframe never matches `:hover`. So "the pointer is over it" is read
// from a transparent **shield** laid over every frame (plan §E.8's fallback):
//   - a `pointermove`, `pointerdown` or `wheel` on the shield means the
//     owner's pointer is over that frame: the shield steps aside
//     (`pointer-events: none`) and the page gets the pointer from then on;
//   - a pointer event anywhere else in the HUD means it left: the shield
//     comes back;
//   - **a key typed in the HUD** brings every shield back too: a pointer
//     parked over a page while the owner types elsewhere stops counting at
//     the first key (narrower than the plan's stated limit). A pointer that
//     has not moved since never drops the shield again (raising or dropping
//     it under a still pointer fires no `pointermove`, measured), so the
//     owner's first wheel or click there can be eaten by the shield.
//
// **On theft** (synchronously, in the handler): the iframe element is removed
// (the only thing that returns the keyboard), focus goes back to the HUD
// element that last had it (its selection kept), else the selected chat's
// box, or, under a card, to the card; the pane is told (it draws "This page
// took the keyboard · Reload"); and one console line names the pane slot,
// never the URL. A key already in flight when the focus moved may be lost.

import {
  BELT_MS, RESTORE_MS, judgeFocus, keyMark, loadGrantLive, tabGesture,
  type KeyMark, type LoadGrant, type Verdict,
} from "../lib/frameguard";

interface Entry {
  frame: HTMLIFrameElement;
  shield: HTMLElement;
  host: HTMLElement;
  key: string;
  pane: number;
  slot: () => number;
  /** The pane is drawn and this is its active tab (the pane's own word). */
  drawn: () => boolean;
  onStolen: () => void;
  /** The owner's pointer is over the frame. */
  over: boolean;
}

interface Grant extends LoadGrant {
  /** The frame this load made (the first one registered for the key after it). */
  frame: HTMLIFrameElement | null;
}

const entries = new Map<HTMLIFrameElement, Entry>();
const shields = new Map<Element, Entry>();
const loads = new Map<string, Grant>();
/** The frame the owner put the keyboard in, while it still has it. */
let holding: HTMLIFrameElement | null = null;
/** The HUD element that last had focus, and where its selection was. */
let lastHud: HTMLElement | null = null;
let lastKey: KeyMark | null = null;
let restoreGrant: { el: Element; until: number } | null = null;
let belt: ReturnType<typeof setInterval> | null = null;
let raf: number | null = null;
let installs = 0;
let now: () => number = () => performance.now();

/** Tests only: the guard's clock. */
export function setClock(fn: () => number) {
  now = fn;
}

// ---- frames ---------------------------------------------------------------

export interface FrameRegistration {
  frame: HTMLIFrameElement;
  shield: HTMLElement;
  host: HTMLElement;
  key: string;
  pane: number;
  slot: () => number;
  drawn: () => boolean;
  onStolen: () => void;
}

/** A Browser frame the guard watches, from its creation to its removal. */
export function registerFrame(r: FrameRegistration): () => void {
  const entry: Entry = { ...r, over: false };
  entries.set(r.frame, entry);
  shields.set(r.shield, entry);
  setShield(entry);
  const grant = loads.get(r.key);
  if (grant && grant.frame === null) grant.frame = r.frame;
  r.frame.addEventListener("load", () => frameLoaded(r.frame));
  startBelt();
  return () => unregister(entry);
}

function unregister(entry: Entry) {
  if (entries.get(entry.frame) === entry) entries.delete(entry.frame);
  if (shields.get(entry.shield) === entry) shields.delete(entry.shield);
  if (holding === entry.frame) holding = null;
  const grant = loads.get(entry.key);
  if (grant?.frame === entry.frame) loads.delete(entry.key);
  if (!entries.size) stopBelt();
}

function setShield(entry: Entry) {
  entry.shield.style.pointerEvents = entry.over ? "none" : "auto";
  entry.shield.dataset.up = entry.over ? "false" : "true";
}

function setOver(entry: Entry, over: boolean) {
  if (entry.over === over) return;
  entry.over = over;
  setShield(entry);
}

/** The owner loaded this tab themselves (Load, Enter, Project root, Reload, the keep-origin switch). */
export function ownLoad(key: string) {
  loads.set(key, { at: now(), loadedAt: null, frame: null });
}

/** A load that is not the owner's (a stored URL restored, a terminal's "Open in Browser"): no gesture carries over. */
export function cancelLoad(key: string) {
  loads.delete(key);
}

function frameLoaded(frame: HTMLIFrameElement) {
  const entry = entries.get(frame);
  if (!entry) return;
  const grant = loads.get(entry.key);
  if (grant && grant.frame === frame && grant.loadedAt === null) grant.loadedAt = now();
}

/**
 * The HUD itself giving the keyboard back to a frame a card took it from
 * (ApprovalVeil's restore). Valid for that element, once, for RESTORE_MS.
 */
export function allowRestore(el: Element) {
  if (el instanceof HTMLIFrameElement && entries.has(el)) restoreGrant = { el, until: now() + RESTORE_MS };
}

// ---- the decision ---------------------------------------------------------

function cardUp(): boolean {
  return !!document.querySelector('#authveil [data-testid="approval-card"]');
}

function drawnNow(entry: Entry): boolean {
  const f = entry.frame;
  if (!f.isConnected || !entry.drawn()) return false;
  if (f.getClientRects().length === 0) return false;
  const cv = (f as HTMLElement & { checkVisibility?: (o?: object) => boolean }).checkVisibility;
  return cv ? cv.call(f, { visibilityProperty: true }) : true;
}

/** Tests only: what the guard would decide for a frame now, without acting. */
export function verdictFor(frame: HTMLIFrameElement): Verdict | null {
  const entry = entries.get(frame);
  return entry ? decide(entry) : null;
}

function decide(entry: Entry): Verdict {
  const t = now();
  const grant = loads.get(entry.key);
  return judgeFocus({
    cardUp: cardUp(),
    drawn: drawnNow(entry),
    pointerOver: entry.over,
    tab: tabGesture(lastKey, t),
    ownLoad: !!grant && grant.frame === entry.frame && loadGrantLive(grant, t),
    restore: !!restoreGrant && restoreGrant.el === entry.frame && t <= restoreGrant.until,
  });
}

/** Run on every trigger: is the keyboard in a Browser frame the owner did not put it in? */
export function check() {
  const a = document.activeElement;
  const entry = a instanceof HTMLIFrameElement ? entries.get(a) : undefined;
  if (!entry) {
    holding = null;
    stopRaf();
    return;
  }
  if (a === holding) {
    // A card takes focus off a frame as it comes up (ApprovalVeil); one that
    // still has the keyboard under a card goes.
    if (cardUp()) {
      steal(entry);
      return;
    }
    // A frame the owner put the keyboard in that stops being drawn (the
    // window shrank, the layout dropped its pane) gives it back.
    if (!drawnNow(entry)) {
      holding = null;
      refocus();
      return;
    }
    startRaf();
    return;
  }
  const v = decide(entry);
  if (v.ok) {
    if (v.by === "restore") restoreGrant = null;
    holding = entry.frame;
    startRaf();
    return;
  }
  steal(entry);
}

function steal(entry: Entry) {
  // 1. The element goes: the only thing that returns the keyboard.
  entry.frame.remove();
  unregister(entry);
  // 2. Focus back where the owner had it.
  refocus();
  // 3. The pane marks the tab and draws the notice.
  try {
    entry.onStolen();
  } catch {
    // The removal and the refocus stand whatever the pane does.
  }
  // 4. One line, the pane slot only: never the URL.
  let slot = 0;
  try {
    slot = entry.slot();
  } catch {
    slot = 0;
  }
  console.warn(`frame guard: a Browser page in pane ${slot} took the keyboard; it was closed`);
}

function focusable(el: HTMLElement | null): el is HTMLElement {
  if (!el || !el.isConnected || el === document.body || el === document.documentElement) return false;
  if (el.closest("[inert]")) return false;
  if ((el as HTMLButtonElement).disabled) return false;
  return true;
}

function textSelection(el: HTMLElement): [number, number, "forward" | "backward" | "none"] | null {
  if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return null;
  try {
    if (el.selectionStart === null || el.selectionEnd === null) return null;
    return [el.selectionStart, el.selectionEnd, (el.selectionDirection || "none") as "forward" | "backward" | "none"];
  } catch {
    return null;
  }
}

function focusKeeping(el: HTMLElement): boolean {
  const sel = textSelection(el);
  el.focus({ preventScroll: true });
  if (document.activeElement !== el) return false;
  if (sel) {
    const now2 = textSelection(el);
    if (!now2 || now2[0] !== sel[0] || now2[1] !== sel[1]) {
      try {
        (el as HTMLInputElement).setSelectionRange(sel[0], sel[1], sel[2]);
      } catch {
        // An input type with no selection: focus is what matters.
      }
    }
  }
  return true;
}

/** Focus back on the HUD: the card when one is up, else what last had it, else the selected chat's box. */
function refocus() {
  const card = document.querySelector<HTMLElement>('#authveil [data-testid="approval-card"]');
  if (card) {
    card.focus({ preventScroll: true });
    return;
  }
  if (focusable(lastHud) && focusKeeping(lastHud)) return;
  const box = document.querySelector<HTMLElement>('[data-selected="true"] [data-testid="input"]');
  if (focusable(box)) focusKeeping(box);
}

// ---- the triggers ---------------------------------------------------------

function startBelt() {
  if (belt === null && installs > 0) belt = setInterval(check, BELT_MS);
}

function stopBelt() {
  if (belt !== null) clearInterval(belt);
  belt = null;
  stopRaf();
}

function startRaf() {
  if (raf !== null || installs === 0 || typeof requestAnimationFrame !== "function") return;
  const step = () => {
    raf = null;
    check();
  };
  raf = requestAnimationFrame(step);
}

function stopRaf() {
  if (raf !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(raf);
  raf = null;
}

function paneOf(key: string): Element | null {
  const pane = key.split(":")[0];
  return document.querySelector(`[data-testid="pane-${pane}"]`);
}

function onBlur(e: FocusEvent) {
  if (e.target === window) check();
}

function onFocusIn(e: FocusEvent) {
  const t = e.target;
  if (t instanceof HTMLIFrameElement && entries.has(t)) {
    check();
    return;
  }
  if (!(t instanceof HTMLElement) || t === document.body) return;
  lastHud = t;
  holding = null;
  // The owner moved on to something outside that pane: their load no longer covers it.
  for (const key of Array.from(loads.keys())) {
    const pane = paneOf(key);
    if (!pane || !pane.contains(t)) loads.delete(key);
  }
}

function onKeyDown(e: KeyboardEvent) {
  const mark = keyMark(e, now());
  if (!mark) return;
  lastKey = mark;
  // A key typed in the HUD: a pointer resting over a page no longer counts,
  // and the owner's load of a page no longer covers it.
  for (const entry of Array.from(entries.values())) setOver(entry, false);
  loads.clear();
}

function onPointer(e: Event) {
  if (!entries.size) return;
  const t = e.target as Node | null;
  const mine = t instanceof Element ? shields.get(t) : undefined;
  if (mine && e.type !== "pointerover") setOver(mine, true);
  for (const entry of Array.from(entries.values())) {
    if (entry === mine) continue;
    if (entry.over && !(t && entry.host.contains(t))) setOver(entry, false);
  }
}

function onVisibility() {
  check();
}

/** Install the window-level listeners (App, once). Returns the uninstall. */
export function installFrameGuard(): () => void {
  installs += 1;
  if (installs === 1) {
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onVisibility);
    document.addEventListener("focusin", onFocusIn, true);
    window.addEventListener("keydown", onKeyDown, true);
    for (const type of ["pointermove", "pointerdown", "pointerover"]) document.addEventListener(type, onPointer, true);
    document.addEventListener("wheel", onPointer, { capture: true, passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    if (entries.size) startBelt();
  }
  return () => {
    installs -= 1;
    if (installs > 0) return;
    window.removeEventListener("blur", onBlur);
    window.removeEventListener("focus", onVisibility);
    document.removeEventListener("focusin", onFocusIn, true);
    window.removeEventListener("keydown", onKeyDown, true);
    for (const type of ["pointermove", "pointerdown", "pointerover"]) document.removeEventListener(type, onPointer, true);
    document.removeEventListener("wheel", onPointer, { capture: true } as EventListenerOptions);
    document.removeEventListener("visibilitychange", onVisibility);
    stopBelt();
  };
}

/** Read-only, for tests: what the guard holds. Never a URL. */
export function frameGuardProbe() {
  return {
    frames: Array.from(entries.values()).map((e) => ({ key: e.key, over: e.over })),
    holding: holding ? holding.getAttribute("data-browser-frame") : null,
    installed: installs > 0,
  };
}

/** Tests only: forget everything. */
export function resetFrameGuardForTests() {
  entries.clear();
  shields.clear();
  loads.clear();
  holding = null;
  lastHud = null;
  lastKey = null;
  restoreGrant = null;
  stopBelt();
}
