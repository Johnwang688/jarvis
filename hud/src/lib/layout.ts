// The window's own geometry: how zoomed in the HUD is, how wide the left and
// right panes are, and whether either is folded away to a rail (design §18,
// 2026-10-08). Pure, so the rules are tested without a browser; App and
// components/Layout.tsx only apply what this file decides.
//
// **Zoom is CSS `zoom` on #root**, not the browser's page zoom (which a page
// cannot set) and not a root font-size (every size in theme.css is px). Three
// consequences, each handled where it bites:
//
//   - viewport units inside #root are multiplied too, so every `vh`/`vw` cap
//     in theme.css is divided by `--ui-zoom` — without that, the approval
//     card's 86vh cap becomes 138vh at 160% and its buttons leave the screen;
//   - `getBoundingClientRect()` and `clientX` are in screen pixels while a
//     `left:` set from them is in the zoomed space, so a position measured on
//     screen is divided by the zoom before it is used (`toCss`);
//   - Monaco already inverts the scale it measures (getBoundingClientRect
//     against offsetWidth), so the editor needs nothing.
//
// **Widths are in the HUD's own (unzoomed) pixels.** A pane keeps its
// proportion to its own text as the zoom changes: zooming in makes the
// sidebar wider on screen along with its words, rather than truncating more
// of them. What zoom *does* change is how much room there is, so the rendered
// widths are fitted to the window (`fitPanes`) without touching what is
// stored — shrinking the window and growing it back returns the panes to the
// widths the owner chose.
//
// Every storage read and write is guarded and falls back to the default: a
// window with storage blocked, or a value written by hand, still opens.

export const ZOOM_KEY = "jarvis.hud.zoom";
export const LAYOUT_KEY = "jarvis.hud.layout";

/** Percent, so the steps never drift the way 0.1 + 0.2 does. */
export const ZOOM_MIN = 70;
export const ZOOM_MAX = 160;
export const ZOOM_STEP = 10;
export const ZOOM_DEFAULT = 100;

export type Side = "left" | "right";

export const PANE: Record<Side, { min: number; max: number; def: number }> = {
  left: { min: 180, max: 480, def: 236 },
  right: { min: 240, max: 560, def: 316 },
};
/** The centre pane (chat, file, diff, Monaco) never gets narrower than this. */
export const MAIN_MIN = 480;
/** A folded pane's rail. */
export const RAIL = 36;

export interface PaneLayout {
  left: number;
  right: number;
  leftCollapsed: boolean;
  rightCollapsed: boolean;
}

export const DEFAULT_LAYOUT: PaneLayout = {
  left: PANE.left.def,
  right: PANE.right.def,
  leftCollapsed: false,
  rightCollapsed: false,
};

type Getter = Pick<Storage, "getItem">;
type Setter = Pick<Storage, "setItem">;

// ---- zoom -------------------------------------------------------------------

/** A level the control can show: in range and on a step. Anything else is not one. */
export function isZoom(value: unknown): value is number {
  return (
    typeof value === "number" && Number.isInteger(value) &&
    value >= ZOOM_MIN && value <= ZOOM_MAX && value % ZOOM_STEP === 0
  );
}

/** The stored string → a level. Garbage, of any kind, is 100%. */
export function parseZoom(raw: unknown): number {
  if (typeof raw !== "string" || !/^\d{1,3}$/.test(raw.trim())) return ZOOM_DEFAULT;
  const n = Number(raw.trim());
  return isZoom(n) ? n : ZOOM_DEFAULT;
}

/** One step in or out, stopping at the ends rather than wrapping. */
export function stepZoom(current: number, dir: 1 | -1): number {
  const base = isZoom(current) ? current : ZOOM_DEFAULT;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, base + dir * ZOOM_STEP));
}

export function loadZoom(storage?: Getter): number {
  try {
    return parseZoom((storage ?? window.localStorage).getItem(ZOOM_KEY));
  } catch {
    return ZOOM_DEFAULT;
  }
}

export function saveZoom(level: number, storage?: Setter) {
  try {
    (storage ?? window.localStorage).setItem(ZOOM_KEY, String(isZoom(level) ? level : ZOOM_DEFAULT));
  } catch {
    /* storage blocked: the window still zooms, it just forgets */
  }
}

/** A length measured on screen (a rect, a clientX), in the zoomed space. */
export function toCss(screenPx: number, level: number): number {
  return screenPx / ((isZoom(level) ? level : ZOOM_DEFAULT) / 100);
}

// ---- pane widths ------------------------------------------------------------

/** Into the pane's own range; a non-number is its default. */
export function clampWidth(side: Side, width: unknown): number {
  const p = PANE[side];
  if (typeof width !== "number" || !Number.isFinite(width)) return p.def;
  return Math.min(p.max, Math.max(p.min, Math.round(width)));
}

/**
 * The stored JSON → a layout. Each field stands on its own: a bad width falls
 * back to its default without resetting the other pane, and anything that is
 * not literally `true` leaves a pane open — a pane that hides itself because
 * a value was mangled is a pane the owner has to go looking for.
 */
export function parseLayout(raw: unknown): PaneLayout {
  let v: any = null;
  if (typeof raw === "string") {
    try {
      v = JSON.parse(raw);
    } catch {
      v = null;
    }
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return { ...DEFAULT_LAYOUT };
  return {
    left: clampWidth("left", v.left),
    right: clampWidth("right", v.right),
    leftCollapsed: v.leftCollapsed === true,
    rightCollapsed: v.rightCollapsed === true,
  };
}

export function loadLayout(storage?: Getter): PaneLayout {
  try {
    return parseLayout((storage ?? window.localStorage).getItem(LAYOUT_KEY));
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

export function saveLayout(layout: PaneLayout, storage?: Setter) {
  try {
    const clean = parseLayout(JSON.stringify(layout));
    (storage ?? window.localStorage).setItem(LAYOUT_KEY, JSON.stringify(clean));
  } catch {
    /* ditto */
  }
}

/**
 * The widths to draw in a window `available` px wide (zoomed space). Only the
 * render changes: when the panes and the centre's minimum do not fit, the open
 * panes give back their slack above their minimums in proportion to it, so
 * neither one is the only one to lose. If even the minimums do not fit, the
 * minimums are drawn and the centre takes what is left.
 */
export function fitPanes(layout: PaneLayout, available: number): { left: number; right: number } {
  const left = layout.leftCollapsed ? RAIL : clampWidth("left", layout.left);
  const right = layout.rightCollapsed ? RAIL : clampWidth("right", layout.right);
  if (!Number.isFinite(available) || available <= 0) return { left, right };
  const over = left + right + MAIN_MIN - available;
  if (over <= 0) return { left, right };
  const slackL = layout.leftCollapsed ? 0 : left - PANE.left.min;
  const slackR = layout.rightCollapsed ? 0 : right - PANE.right.min;
  const slack = slackL + slackR;
  if (slack <= 0) return { left, right };
  const take = Math.min(over, slack);
  const takeL = Math.round((take * slackL) / slack);
  return { left: left - takeL, right: right - (take - takeL) };
}

/**
 * The widest a pane may be dragged to right now: its own maximum, or whatever
 * leaves the centre its minimum beside the other pane as drawn — never below
 * its own minimum.
 */
export function maxWidth(side: Side, otherDrawn: number, available: number): number {
  const p = PANE[side];
  if (!Number.isFinite(available) || available <= 0) return p.max;
  return Math.max(p.min, Math.min(p.max, Math.floor(available - MAIN_MIN - otherDrawn)));
}

/** A width from a drag: `dx` is the pointer's travel in the zoomed space. */
export function dragWidth(side: Side, start: number, dx: number, max: number): number {
  const w = side === "left" ? start + dx : start - dx;
  return Math.min(max, Math.max(PANE[side].min, Math.round(w)));
}

export const KEY_STEP = 16;
export const KEY_STEP_BIG = 64;

/**
 * A width from a key on the separator. The arrows move the *edge*, so on the
 * right pane ArrowLeft widens it; Home and End go to the ends. Null for a key
 * the separator does not use.
 */
export function keyWidth(
  side: Side, current: number, key: string, shift: boolean, max: number,
): number | null {
  const p = PANE[side];
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  const edge = side === "left" ? 1 : -1;
  let w: number;
  switch (key) {
    case "ArrowRight": w = current + step * edge; break;
    case "ArrowLeft": w = current - step * edge; break;
    case "Home": w = p.min; break;
    case "End": w = max; break;
    default: return null;
  }
  return Math.min(max, Math.max(p.min, Math.round(w)));
}

// ---- shortcuts --------------------------------------------------------------

export type Shortcut = "zoomIn" | "zoomOut" | "zoomReset" | "toggleLeft" | "toggleRight";

/**
 * Ctrl+= / Ctrl+- / Ctrl+0 zoom (with the numpad and shifted spellings), Ctrl+B
 * folds the left pane and Ctrl+Alt+B the right. Matched on `key`, not `code`:
 * AltGr arrives as Ctrl+Alt, and AltGr+B on a layout that types a letter
 * there reports that letter, so it can never fold a pane mid-word.
 */
export function shortcutFor(e: {
  key: string; ctrlKey: boolean; metaKey?: boolean; altKey: boolean; shiftKey?: boolean;
}): Shortcut | null {
  if (!(e.ctrlKey || e.metaKey)) return null;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "b") return e.altKey ? "toggleRight" : "toggleLeft";
  if (e.altKey) return null;
  if (k === "=" || k === "+") return "zoomIn";
  if (k === "-" || k === "_") return "zoomOut";
  if (k === "0") return "zoomReset";
  return null;
}

/**
 * Whether keystrokes are going into something the owner is typing in: a text
 * input, a textarea, a select, anything contenteditable, or Monaco. The zoom
 * shortcuts stand aside there and the browser keeps its own.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof (el as any).closest !== "function") return false;
  if (el.closest(".monaco-editor")) return true;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = ((el as HTMLInputElement).type || "text").toLowerCase();
    return !["button", "checkbox", "radio", "range", "color", "file", "submit", "reset", "image"].includes(type);
  }
  return false;
}
