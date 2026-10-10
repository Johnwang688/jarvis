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
// widths are fitted to the window (`fitLayout`) without touching what is
// stored — shrinking the window and growing it back returns the panes to the
// widths the owner chose. When even the panes' minimums would leave the
// centre under MAIN_MIN, a pane is folded *for this render only* (the right
// one first): a small window at a high zoom gets a usable centre, and the
// owner's own folded flags are never rewritten by it.
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
/**
 * The centre pane (chat, file, diff, Monaco) is kept at least this wide —
 * by shrinking the panes, then by folding them for the render — unless the
 * window is too small even with both panes folded. A split centre needs more
 * (lib/workspace.ts, `SHAPES`): every fitting function takes the centre's
 * minimum as a parameter, defaulting to this one, the single layout's.
 */
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

/** What a render draws: the widths, and which panes are folded for it. */
export interface Fitted {
  left: number;
  right: number;
  leftFolded: boolean;
  rightFolded: boolean;
  /** Folded by the window, not by the owner — not stored, gone when there is room. */
  autoLeft: boolean;
  autoRight: boolean;
}

/**
 * The widths to draw with the given panes folded, in a window `available` px
 * wide (zoomed space). When the panes and the centre's minimum do not fit,
 * the open panes give back their slack above their minimums in proportion to
 * it, so neither is the only one to lose; past that, the minimums are drawn.
 */
function widths(
  layout: PaneLayout, leftFolded: boolean, rightFolded: boolean, available: number, mainMin: number,
) {
  const left = leftFolded ? RAIL : clampWidth("left", layout.left);
  const right = rightFolded ? RAIL : clampWidth("right", layout.right);
  if (!Number.isFinite(available) || available <= 0) return { left, right };
  const over = left + right + mainMin - available;
  if (over <= 0) return { left, right };
  const slackL = leftFolded ? 0 : left - PANE.left.min;
  const slackR = rightFolded ? 0 : right - PANE.right.min;
  const slack = slackL + slackR;
  if (slack <= 0) return { left, right };
  const take = Math.min(over, slack);
  const takeL = Math.round((take * slackL) / slack);
  return { left: left - takeL, right: right - (take - takeL) };
}

/**
 * The layout one render draws. Only the render changes, never what is stored:
 *
 *   1. the owner's folded flags are applied;
 *   2. if the open panes' *minimums* still leave the centre under MAIN_MIN,
 *      a pane is folded for this render — the right one first (the status
 *      pane is the one that can say "something here wants you" from its
 *      rail), then the left;
 *   3. the open panes shrink toward their minimums to give the centre room.
 *
 * `prefer` is a pane the owner opened by hand while the window had folded it:
 * it is never folded by the window, and the other pane folds first instead.
 * `mainMin` is the centre's minimum: MAIN_MIN for one pane, more for a split
 * (lib/workspace.ts decides which, and drops panes when even this fails).
 */
export function fitLayout(
  layout: PaneLayout, available: number, prefer: Side | null = null, mainMin: number = MAIN_MIN,
): Fitted {
  let leftFolded = layout.leftCollapsed;
  let rightFolded = layout.rightCollapsed;
  let autoLeft = false;
  let autoRight = false;
  if (Number.isFinite(available) && available > 0) {
    const fits = () =>
      (leftFolded ? RAIL : PANE.left.min) + (rightFolded ? RAIL : PANE.right.min) + mainMin <= available;
    const order: Side[] = prefer === "left" ? ["right"] : prefer === "right" ? ["left"] : ["right", "left"];
    for (const side of order) {
      if (fits()) break;
      if (side === "right" && !rightFolded) rightFolded = autoRight = true;
      if (side === "left" && !leftFolded) leftFolded = autoLeft = true;
    }
  }
  return {
    ...widths(layout, leftFolded, rightFolded, available, mainMin), leftFolded, rightFolded, autoLeft, autoRight,
  };
}

/**
 * The widest a pane may be dragged to right now: its own maximum, or whatever
 * leaves the centre its minimum beside the other pane as drawn — never below
 * its own minimum.
 */
export function maxWidth(side: Side, otherDrawn: number, available: number, mainMin: number = MAIN_MIN): number {
  const p = PANE[side];
  if (!Number.isFinite(available) || available <= 0) return p.max;
  return Math.max(p.min, Math.min(p.max, Math.floor(available - mainMin - otherDrawn)));
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
  return keyStep("x", current, key, shift, PANE[side].min, max, side === "left" ? 1 : -1);
}

/**
 * A value from a key on any separator: the side panes', a split's, the
 * panel's. `axis` is the way the edge moves: "x" for a vertical separator
 * (Left/Right), "y" for a horizontal one (Up/Down). `edge` is +1 when moving
 * the edge right or down grows the value and -1 when it shrinks it (the
 * status pane's edge, the panel's top edge). Home and End go to the ends,
 * Shift takes the bigger step, and any other key is null.
 */
export function keyStep(
  axis: "x" | "y", current: number, key: string, shift: boolean, min: number, max: number, edge: 1 | -1 = 1,
): number | null {
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  const fwd = axis === "x" ? "ArrowRight" : "ArrowDown";
  const back = axis === "x" ? "ArrowLeft" : "ArrowUp";
  let v: number;
  switch (key) {
    case fwd: v = current + step * edge; break;
    case back: v = current - step * edge; break;
    case "Home": v = min; break;
    case "End": v = max; break;
    default: return null;
  }
  return Math.min(max, Math.max(min, Math.round(v)));
}

// ---- shortcuts --------------------------------------------------------------

export type Shortcut =
  | "zoomIn" | "zoomOut" | "zoomReset" | "toggleLeft" | "toggleRight" | "togglePanel"
  | "focus1" | "focus2" | "focus3" | "focus4";

/**
 * Ctrl+= / Ctrl+- / Ctrl+0 zoom (with the numpad and shifted spellings), Ctrl+B
 * folds the left pane and Ctrl+Alt+B the right. Ctrl+Shift+B is left alone:
 * it is the browser's bookmarks bar. Matched on `key`, not `code`: AltGr
 * arrives as Ctrl+Alt, and AltGr+B on a layout that types a letter there
 * reports that letter, so it can never fold a pane mid-word.
 *
 * The workspace's keys (2026-10-09). Ctrl+` shows or hides the bottom panel
 * (VS Code's key). It takes no Alt, because AltGr+7 *is* a backtick on French
 * layouts; where the backtick is a dead key `key` reads "Dead", so there, and
 * only there, the physical key (`code` Backquote) is accepted. Ctrl+Alt+1 … 4
 * focus a pane: AltGr+digit types `{[]}` on European layouts, which reports
 * the symbol and not the digit, so it never fires mid-word. There is no cycle
 * key, because every bracket is a character on some AltGr layout.
 */
export function shortcutFor(e: {
  key: string; code?: string; ctrlKey: boolean; metaKey?: boolean; altKey: boolean; shiftKey?: boolean;
}): Shortcut | null {
  if (!(e.ctrlKey || e.metaKey)) return null;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "b") {
    if (e.shiftKey) return null;
    return e.altKey ? "toggleRight" : "toggleLeft";
  }
  if (e.altKey) {
    if (!e.shiftKey && (k === "1" || k === "2" || k === "3" || k === "4")) return `focus${k}` as Shortcut;
    return null;
  }
  if (k === "`" || (k === "Dead" && e.code === "Backquote")) return e.shiftKey ? null : "togglePanel";
  if (k === "=" || k === "+") return "zoomIn";
  if (k === "-" || k === "_") return "zoomOut";
  if (k === "0") return "zoomReset";
  return null;
}

/**
 * Whether keys are going into Monaco. Ctrl+B stands aside there: it is the
 * second half of Monaco's Ctrl+K Ctrl+B chord. The zoom keys do not — neither
 * Monaco nor the input bar binds them, and letting them through would be
 * Chrome's page zoom, which the HUD's control cannot see and the browser
 * remembers per site.
 */
export function inMonaco(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof (el as any).closest !== "function") return false;
  return !!el.closest(".monaco-editor");
}
