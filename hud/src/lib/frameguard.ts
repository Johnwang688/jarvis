// No Browser frame takes the keyboard on its own (R-1; amendment 2 §E.8).
//
// A page in a Browser pane runs in WP-E's sandboxed frame. It can still call
// `el.focus()` or `window.focus()` with no user activation, and Chrome moves
// keyboard focus out of the HUD into the frame: every key after that goes to
// the page, which can send it to its own server. Measured in Chromium 149,
// headless and headed: `inert`, `display: none`, `visibility: hidden`,
// re-focusing the box and `contentWindow.blur()` do not stop it. Removing the
// iframe element does.
//
// **The rule:** a Browser frame holds the keyboard only when the owner put it
// there. A frame that gains focus without the owner's gesture is removed at
// once (state/frameguard.ts) and the pane says "This page took the keyboard".
//
// This file is the pure half: the gestures and the decision. The DOM half —
// the listeners, the removal, putting focus back — is state/frameguard.ts.

/** A Tab or Shift+Tab in the HUD this recently moves focus into a frame. */
export const TAB_MS = 300;
/** The owner's own load lasts until this long after the frame's `load` event. */
export const LOAD_GRACE_MS = 2000;
/** …or, if the frame never fires `load`, this long after the owner's action. */
export const LOAD_WAIT_MS = 20000;
/** The belt: how often `document.activeElement` is checked while a frame is mounted. */
export const BELT_MS = 250;
/** How long the HUD's own giving-back of focus (after a card) stays valid. */
export const RESTORE_MS = 300;
/**
 * After the layout moves (a split, a close, ⊞; FS4), how long a press on a
 * Browser frame's shield does not step it aside: the second click of a
 * double-click on × lands on whatever grew under the pointer, and that is no
 * gesture toward the page. The × guard in components/Workspace.tsx closes
 * nothing for as long, for the same reason.
 */
export const CLOSE_GUARD_MS = 400;

/** `data-browser-frame` on every Browser iframe: the pane id and the tab index. */
export function frameKey(pane: number, tab: number | string): string {
  return `${pane}:${tab}`;
}

/**
 * The latest key pressed in the HUD, when it was Tab or Shift+Tab (review of
 * PR #78, M1). A Tab counts only if it moved focus — a keydown something
 * cancelled (Monaco indents, a card cycles its own buttons) moved none — and
 * only for the frame focus landed in, read just after the key's own task.
 */
export interface TabMark {
  at: number;
  /** The keydown was cancelled: it moved no focus. Read when judged (final once dispatched). */
  prevented: () => boolean;
  /** Where focus landed: undefined until read, then that frame, or null for anything else. */
  landed?: unknown;
}

/** Keys that only modify another: the one list, in lib/modifierkeys.ts. */
export { isModifierKey } from "./modifierkeys";

/** Tab or Shift+Tab, with no Ctrl, Alt or Meta. */
export function isTabKey(e: { key: string; ctrlKey: boolean; altKey: boolean; metaKey: boolean }): boolean {
  return e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey;
}

/**
 * Did the owner's Tab put the keyboard in `frame`? A Tab at most TAB_MS ago,
 * not cancelled, and either still in its own task (where it landed is not
 * read yet, so the frame gaining focus now is where it landed) or landed in
 * exactly this frame.
 */
export function tabGesture(t: TabMark | null, frame: unknown, now: number): boolean {
  if (!t || now - t.at < 0 || now - t.at > TAB_MS) return false;
  if (t.prevented()) return false;
  return t.landed === undefined || t.landed === frame;
}

/** The owner's own load of one frame: when they asked, and when that frame fired `load`. */
export interface LoadGrant {
  at: number;
  loadedAt: number | null;
}

/** Still live: until LOAD_GRACE_MS after `load`, or LOAD_WAIT_MS after the action if it never came. */
export function loadGrantLive(g: LoadGrant | null | undefined, now: number): boolean {
  if (!g) return false;
  if (g.loadedAt !== null) return now - g.loadedAt <= LOAD_GRACE_MS;
  return now - g.at <= LOAD_WAIT_MS;
}

/** Everything the decision reads about one frame at the moment it gained focus. */
export interface FocusFacts {
  /** An authorization card is up. */
  cardUp: boolean;
  /** The frame is the active tab of a drawn pane, and is drawn. */
  drawn: boolean;
  /** The owner's pointer is over the frame (state/frameguard.ts says how that is known). */
  pointerOver: boolean;
  /** The latest key in the HUD was Tab or Shift+Tab, within TAB_MS. */
  tab: boolean;
  /** The owner's own load of this frame is still live. */
  ownLoad: boolean;
  /** The HUD itself is giving back focus a card took from this frame. */
  restore: boolean;
}

export type Verdict =
  | { ok: true; by: "restore" | "pointer" | "tab" | "load" }
  | { ok: false; why: "card" | "hidden" | "no-gesture" };

/**
 * May this frame have the keyboard? Never under a card and never when hidden,
 * whatever the pointer says; otherwise only with one of the owner's gestures.
 */
export function judgeFocus(f: FocusFacts): Verdict {
  if (f.cardUp) return { ok: false, why: "card" };
  if (!f.drawn) return { ok: false, why: "hidden" };
  if (f.restore) return { ok: true, by: "restore" };
  if (f.pointerOver) return { ok: true, by: "pointer" };
  if (f.tab) return { ok: true, by: "tab" };
  if (f.ownLoad) return { ok: true, by: "load" };
  return { ok: false, why: "no-gesture" };
}
