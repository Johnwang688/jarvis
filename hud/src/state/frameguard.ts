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
//     owner's first wheel or click there can be eaten by the shield;
//   - the window losing focus to another program (not to a frame), or being
//     hidden, brings them back too: a pointer that left the window from
//     inside a page was never heard leaving;
//   - **while a HUD text control has focus** (an input, a textarea, Monaco,
//     a terminal, contenteditable; `isTypingTarget`) the shield steps aside
//     only on a `pointerdown` — never on a move or a wheel — and the HUD then
//     puts the keyboard in the page itself (`contentWindow.focus()`). So a
//     pointer crossing a page while the owner types is never "over" it (a
//     page can grab on its first `pointermove`), at a cost: the first click
//     after typing in the HUD only focuses the page, and a wheel there does
//     not scroll it until a click (review of PR #78, M2, the lead's option
//     (a)). Focus arriving on a text control brings every shield back.
//
// **On theft** (synchronously, in the handler): the iframe element is removed
// (the only thing that returns the keyboard); focus goes back to the HUD
// element that lost it **to this theft** (caught in its own `focusout`, which
// comes just before the window's `blur`), its selection kept — never to one
// that had it earlier: a page that grabs from another page the owner was
// typing in must not hand the owner's next keys to the chat box, where an
// Enter sends them (review of PR #78, H1). With no such element, nothing that
// types or sends gets focus: the pane's notice takes it (its container, not
// its Reload button). Under a card the card does. The pane draws "This page
// took the keyboard · Reload", and one console line names the pane slot,
// never the URL. A key already in flight when the focus moved may be lost.
//
// **A Browser iframe is never `.focus()`ed** (H2): an element focus leaves
// the parent's `activeElement` on that frame even after another frame takes
// the keyboard, which blinds every check here. FrameHost replaces each
// frame's `focus` with `contentWindow.focus()`, which updates it.

import {
  BELT_MS, RESTORE_MS, isModifierKey, isTabKey, judgeFocus, loadGrantLive, tabGesture,
  type LoadGrant, type TabMark, type Verdict,
} from "../lib/frameguard";
import { isTypingTarget } from "../lib/talkkey";

interface Entry {
  frame: HTMLIFrameElement;
  shield: HTMLElement;
  host: HTMLElement;
  key: string;
  pane: number;
  slot: () => number;
  /** The pane is drawn and this is its active tab (the pane's own word). */
  drawn: () => boolean;
  /** The guard removed the frame; `focusNotice` when nothing got focus back. */
  onStolen: (focusNotice: boolean) => void;
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
/**
 * The HUD element that just lost focus (its `focusout`), until focus lands on
 * a HUD element again or a frame is let keep it: what a theft gives back to.
 */
let leftFor: HTMLElement | null = null;
/** The latest key in the HUD, when it was Tab or Shift+Tab. */
let lastTab: TabMark | null = null;
/** An element focus can be put on and taken off again, so that nothing is focused. */
let sink: HTMLElement | null = null;
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
  onStolen: (focusNotice: boolean) => void;
}

/** A Browser frame the guard watches, from its creation to its removal. */
export function registerFrame(r: FrameRegistration): () => void {
  const entry: Entry = { ...r, over: false };
  entries.set(r.frame, entry);
  shields.set(r.shield, entry);
  setShield(entry);
  // A press on the shield never moves focus to the pane around it.
  r.shield.addEventListener("mousedown", (e) => e.preventDefault());
  const grant = loads.get(r.key);
  if (grant && grant.frame === null) grant.frame = r.frame;
  r.frame.addEventListener("load", () => frameLoaded(r.frame));
  startBelt();
  return () => unregister(entry);
}

function unregister(entry: Entry) {
  if (entries.get(entry.frame) === entry) entries.delete(entry.frame);
  if (shields.get(entry.shield) === entry) shields.delete(entry.shield);
  if (holding === entry.frame) {
    holding = null;
  }
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

function decide(entry: Entry): Verdict {
  const t = now();
  const grant = loads.get(entry.key);
  return judgeFocus({
    cardUp: cardUp(),
    drawn: drawnNow(entry),
    pointerOver: entry.over,
    tab: tabGesture(lastTab, entry.frame, t),
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
    // window shrank, the layout dropped its pane) loses it — to nothing that
    // types or sends (H1).
    if (!drawnNow(entry)) {
      holding = null;
      focusNothing();
      return;
    }
    startRaf();
    return;
  }
  if (lastTab) tabLanded(lastTab, entry.frame);
  const v = decide(entry);
  if (v.ok) {
    if (v.by === "restore") restoreGrant = null;
    holding = entry.frame;
    leftFor = null; // the keyboard went where the owner put it
    startRaf();
    return;
  }
  steal(entry);
}

function steal(entry: Entry) {
  // 1. The element goes: the only thing that returns the keyboard.
  entry.frame.remove();
  unregister(entry);
  // 2. Focus back to what lost it to this theft, else to nothing that types.
  const back = refocus();
  // 3. The pane marks the tab and draws the notice (and takes focus when
  //    nothing else did).
  try {
    entry.onStolen(!back);
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

/**
 * Focus back after a theft: the card when one is up, else the element that
 * lost focus to this theft (H1). False when nothing got it: the keyboard was
 * taken from another page, or that element is gone.
 */
function refocus(): boolean {
  const card = document.querySelector<HTMLElement>('#authveil [data-testid="approval-card"]');
  if (card) {
    card.focus({ preventScroll: true });
    return true;
  }
  const t = leftFor;
  leftFor = null;
  if (focusable(t) && focusKeeping(t)) return true;
  focusNothing();
  return false;
}

/** Take the keyboard to nothing that types or sends: focus lands on no element. */
function focusNothing() {
  if (!sink || !sink.isConnected) {
    sink = document.createElement("div");
    sink.tabIndex = -1;
    sink.setAttribute("aria-hidden", "true");
    sink.setAttribute("data-frameguard-sink", "");
    sink.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;";
    document.body.append(sink);
  }
  sink.focus({ preventScroll: true });
  sink.blur();
}

function isSink(t: EventTarget | null): boolean {
  return !!sink && t === sink;
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
  if (e.target instanceof Node) return; // an element's own blur; only the window's counts
  // The window lost focus to another program, not to a frame: the owner left
  // it, so a pointer last seen over a page no longer counts (headed, the HUD
  // never hears the pointer leave a frame).
  const a = document.activeElement;
  if (!(a instanceof HTMLIFrameElement && entries.has(a))) {
    for (const entry of Array.from(entries.values())) setOver(entry, false);
  }
  check();
}

function onFocusIn(e: FocusEvent) {
  const t = e.target;
  if (t instanceof HTMLIFrameElement && entries.has(t)) {
    check();
    return;
  }
  if (!(t instanceof HTMLElement) || t === document.body || isSink(t)) return;
  leftFor = null;
  if (lastTab) tabLanded(lastTab, null);
  holding = null;
  // The owner moved on to something outside that pane: their load no longer covers it.
  for (const key of Array.from(loads.keys())) {
    const pane = paneOf(key);
    if (!pane || !pane.contains(t)) loads.delete(key);
  }
  // A text control has the keyboard now: a pointer resting over a page stops counting (M2).
  if (isTypingTarget(t)) for (const entry of Array.from(entries.values())) setOver(entry, false);
}

function onFocusOut(e: FocusEvent) {
  const t = e.target;
  if (!(t instanceof HTMLElement) || t instanceof HTMLIFrameElement || isSink(t)) return;
  leftFor = t;
}

function onKeyDown(e: KeyboardEvent) {
  if (isModifierKey(e.key)) return;
  if (isTabKey(e)) {
    const mark: TabMark = { at: now(), prevented: () => e.defaultPrevented };
    lastTab = mark;
    // Where it landed, read once the key's own task is over (M1).
    setTimeout(() => tabLanded(mark), 0);
  } else {
    lastTab = null;
  }
  // A key typed in the HUD: a pointer resting over a page no longer counts,
  // and the owner's load of a page no longer covers it.
  for (const entry of Array.from(entries.values())) setOver(entry, false);
  loads.clear();
}

/**
 * Where a Tab landed is the first place focus goes after it (its own default
 * action moves focus inside the key's task, before any other page can act):
 * a HUD element (`focusin`) or a frame (`check`). Read once; a Tab whose
 * landing was never seen by the end of its task landed nowhere.
 */
function tabLanded(mark: TabMark, where?: HTMLElement | null) {
  if (lastTab !== mark || mark.landed !== undefined) return;
  mark.landed = where ?? null;
}

function onPointer(e: Event) {
  if (!entries.size) return;
  const t = e.target as Node | null;
  const mine = t instanceof Element ? shields.get(t) : undefined;
  if (mine) {
    // The keyboard is the owner's where they type: a HUD text control, or
    // another page they put it in (PR #78 delta review: crossing page B while
    // typing in page A let B take the keys).
    const active = document.activeElement;
    const typing = isTypingTarget(active)
      || (active instanceof HTMLIFrameElement && entries.has(active) && active !== mine.frame);
    if (e.type === "pointerdown") {
      setOver(mine, true);
      if (typing) {
        // The owner was typing in the HUD: this press only puts the keyboard
        // in the page (M2), and it is the HUD that puts it there.
        e.preventDefault();
        try {
          mine.frame.contentWindow?.focus();
        } catch {
          // A frame that has gone: nothing to focus.
        }
      }
    } else if (e.type !== "pointerover" && !typing) {
      // A move or a wheel counts only while no text control has the keyboard (M2).
      setOver(mine, true);
    }
  }
  for (const entry of Array.from(entries.values())) {
    if (entry === mine) continue;
    if (entry.over && !(t && entry.host.contains(t))) setOver(entry, false);
  }
}

function onVisibility() {
  if (document.visibilityState === "hidden") {
    for (const entry of Array.from(entries.values())) setOver(entry, false);
  }
  check();
}

/** Install the window-level listeners (App, once). Returns the uninstall. */
export function installFrameGuard(): () => void {
  installs += 1;
  if (installs === 1) {
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onVisibility);
    document.addEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusout", onFocusOut, true);
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
    document.removeEventListener("focusout", onFocusOut, true);
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
  leftFor = null;
  lastTab = null;
  sink?.remove();
  sink = null;
  restoreGrant = null;
  stopBelt();
}
