// The centre's panes (design §18; plan docs/plans/2026-10-09-hud-workspace-
// plan.md §2.2): which preset is chosen, what each of the four panes shows,
// where the edges between them are, the bottom panel, and how all of it is
// fitted to the window. Pure, like lib/layout.ts, so every rule is tested
// without a browser; components/Workspace.tsx and the title bar only draw
// what this file decides.
//
// **Presets, not free splitting.** Six shapes cover what the owner asked for
// (2, 3 and 4 panes) without a tree of groups and drop targets.
//
// **All four pane specs are always stored**, so going from four panes to one
// and back brings back what each pane showed. A pane is mounted the first
// time a preset shows it and is then hidden, never unmounted (the side panes'
// rule): an unsaved edit in pane 3 survives switching to one pane.
//
// **Chat is a singleton in this package (WP-A).** At most one pane shows the
// conversation; choosing chat in another pane swaps the two panes' views.
// WP-B lifts this.
//
// **Fitting changes only the render, never what is stored** (`fitWorkspace`,
// the generalisation of `fitLayout`). Across: the side panes give back their
// slack, then fold (right first), and only then are columns dropped, never the
// focused pane. Down: the panel shrinks to its minimum, then folds, and only
// then are rows dropped. Opening a panel the window folded drops rows instead
// (the vertical `prefer`).
//
// Every number here is in the HUD's own (unzoomed) pixels, like layout.ts.

import { MAIN_MIN, fitLayout, type Fitted, type PaneLayout, type Side } from "./layout";

export const WORKSPACE_KEY = "jarvis.hud.workspace";

export type View = "chat" | "task" | "file" | "diff" | "preview" | "terminal";
/** What a pane offers. The terminal view (WP-D) shows one of the owner's
 * terminals, chosen per pane (`terminalId`); one terminal is drawn in one
 * place at a time (lib/terminal.ts, `placeTerminals`). */
export const PANE_VIEWS: readonly View[] = ["chat", "task", "file", "diff", "preview", "terminal"];

export type Preset = "single" | "cols2" | "rows2" | "cols3" | "main2" | "grid4";
export const PRESETS: readonly Preset[] = ["single", "cols2", "rows2", "cols3", "main2", "grid4"];

export type PaneNo = 1 | 2 | 3 | 4;
export const PANE_NOS: readonly PaneNo[] = [1, 2, 3, 4];

/** A pane in a split is never drawn narrower or shorter than this. */
export const PANE_MIN_W = 360;
export const PANE_MIN_H = 200;
/** The bottom panel's height: stored, then fitted. */
export const PANEL = { min: 120, def: 260, max: 1600 } as const;
/** The window-wide title bar above everything (§2.1). */
export const TITLEBAR_H = 28;

export interface ShapeSpec {
  label: string;
  /** How many panes it draws. */
  panes: number;
  /** The centre's minimum width, and the pane area's minimum height. */
  minW: number;
  minH: number;
  cols: number;
  rows: number;
  /** CSS grid areas, by slot (`s1` … `s4`). */
  areas: string[];
}

export const SHAPES: Record<Preset, ShapeSpec> = {
  single: { label: "One pane", panes: 1, minW: MAIN_MIN, minH: PANE_MIN_H, cols: 1, rows: 1, areas: ["s1"] },
  cols2: { label: "Two side by side", panes: 2, minW: 2 * PANE_MIN_W, minH: PANE_MIN_H, cols: 2, rows: 1,
           areas: ["s1 s2"] },
  rows2: { label: "Two stacked", panes: 2, minW: MAIN_MIN, minH: 2 * PANE_MIN_H, cols: 1, rows: 2,
           areas: ["s1", "s2"] },
  cols3: { label: "Three side by side", panes: 3, minW: 3 * PANE_MIN_W, minH: PANE_MIN_H, cols: 3, rows: 1,
           areas: ["s1 s2 s3"] },
  main2: { label: "One large, two stacked", panes: 3, minW: MAIN_MIN + PANE_MIN_W, minH: 2 * PANE_MIN_H,
           cols: 2, rows: 2, areas: ["s1 s2", "s1 s3"] },
  grid4: { label: "Two by two", panes: 4, minW: 2 * PANE_MIN_W, minH: 2 * PANE_MIN_H, cols: 2, rows: 2,
           areas: ["s1 s2", "s3 s4"] },
};

export interface PaneSpec {
  view: View;
  /** File and Preview panes: the project they are pinned to, or null to follow the chat. */
  projectId: string | null;
  /** The terminal a terminal pane shows (WP-D), or null to choose one. */
  terminalId: string | null;
  /** Preview: the URL last loaded here, judged again before it is loaded again. */
  previewUrl?: string;
}

/** Where a shape's edges are, as fractions: `cols` the cumulative column edges, `row` the row edge. */
export interface Split {
  cols: number[];
  row: number;
}

export type Panes = [PaneSpec, PaneSpec, PaneSpec, PaneSpec];

export interface Workspace {
  preset: Preset;
  panes: Panes;
  splits: Record<Preset, Split>;
  focused: PaneNo;
  panel: { open: boolean; height: number };
}

const DEFAULT_VIEWS: Record<PaneNo, View> = { 1: "chat", 2: "preview", 3: "file", 4: "task" };

/** Equal columns for a shape (main2's large pane is wider by default). */
function defaultSplit(preset: Preset): Split {
  const n = SHAPES[preset].cols;
  if (preset === "main2") return { cols: [0.6], row: 0.5 };
  return { cols: Array.from({ length: n - 1 }, (_, i) => (i + 1) / n), row: 0.5 };
}

export function defaultWorkspace(): Workspace {
  const splits = {} as Record<Preset, Split>;
  for (const p of PRESETS) splits[p] = defaultSplit(p);
  return {
    preset: "single",
    panes: PANE_NOS.map((n) => ({ view: DEFAULT_VIEWS[n], projectId: null, terminalId: null })) as Panes,
    splits,
    focused: 1,
    panel: { open: false, height: PANEL.def },
  };
}

/** The panes a preset draws, in slot order, before any fitting. */
export function panesOf(preset: Preset): PaneNo[] {
  return PANE_NOS.slice(0, SHAPES[preset].panes);
}

export function isPreset(v: unknown): v is Preset {
  return typeof v === "string" && (PRESETS as readonly string[]).includes(v);
}

function isView(v: unknown): v is View {
  return typeof v === "string" && (PANE_VIEWS as readonly string[]).includes(v);
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

export function clampPanelHeight(h: unknown): number {
  if (typeof h !== "number" || !Number.isFinite(h)) return PANEL.def;
  return Math.min(PANEL.max, Math.max(PANEL.min, Math.round(h)));
}

const ID_MAX = 200;
const URL_MAX = 2048;

function parsePane(raw: unknown, n: PaneNo): PaneSpec {
  const v = isObj(raw) ? raw : {};
  const id = (x: unknown) => (typeof x === "string" && x && x.length <= ID_MAX ? x : null);
  const spec: PaneSpec = {
    view: isView(v.view) ? v.view : DEFAULT_VIEWS[n],
    projectId: id(v.projectId),
    terminalId: id(v.terminalId),
  };
  if (typeof v.previewUrl === "string" && v.previewUrl && v.previewUrl.length <= URL_MAX) {
    spec.previewUrl = v.previewUrl;
  }
  return spec;
}

/** Fractions strictly inside (0, 1), strictly increasing, the shape's count. */
function parseSplit(raw: unknown, preset: Preset): Split {
  const def = defaultSplit(preset);
  const v = isObj(raw) ? raw : {};
  const inside = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x) && x > 0 && x < 1;
  let cols = def.cols;
  if (Array.isArray(v.cols) && v.cols.length === def.cols.length && v.cols.every(inside)
      && v.cols.every((x, i) => i === 0 || x > (v.cols as number[])[i - 1])) {
    cols = (v.cols as number[]).slice();
  }
  return { cols, row: inside(v.row) ? v.row : def.row };
}

/** At most one pane shows the chat: the first keeps it, any later one goes back to its default view. */
function oneChat(panes: Panes): Panes {
  let seen = false;
  return panes.map((p, i) => {
    if (p.view !== "chat") return p;
    if (!seen) {
      seen = true;
      return p;
    }
    return { ...p, view: DEFAULT_VIEWS[(i + 1) as PaneNo] };
  }) as Panes;
}

/**
 * The stored JSON → a workspace. Each field stands on its own (a bad split
 * does not reset the panes), an unknown preset reads as `single`, and the
 * panel is open only on a literal `true`.
 */
export function parseWorkspace(raw: unknown): Workspace {
  let v: unknown = null;
  if (typeof raw === "string") {
    try {
      v = JSON.parse(raw);
    } catch {
      v = null;
    }
  }
  const ws = defaultWorkspace();
  if (!isObj(v)) return ws;
  const preset = isPreset(v.preset) ? v.preset : "single";
  const rawPanes = Array.isArray(v.panes) ? v.panes : [];
  const panes = oneChat(PANE_NOS.map((n) => parsePane(rawPanes[n - 1], n)) as Panes);
  const rawSplits = isObj(v.splits) ? v.splits : {};
  const splits = {} as Record<Preset, Split>;
  for (const p of PRESETS) splits[p] = parseSplit(rawSplits[p], p);
  const f = v.focused;
  const focused: PaneNo =
    typeof f === "number" && (PANE_NOS as readonly number[]).includes(f) && panesOf(preset).includes(f as PaneNo)
      ? (f as PaneNo) : 1;
  const panel = isObj(v.panel) ? v.panel : {};
  return { preset, panes, splits, focused, panel: { open: panel.open === true, height: clampPanelHeight(panel.height) } };
}

type Getter = Pick<Storage, "getItem">;
type Setter = Pick<Storage, "setItem">;

export function loadWorkspace(storage?: Getter): Workspace {
  try {
    return parseWorkspace((storage ?? window.localStorage).getItem(WORKSPACE_KEY));
  } catch {
    return defaultWorkspace();
  }
}

export function saveWorkspace(ws: Workspace, storage?: Setter) {
  try {
    const clean = parseWorkspace(JSON.stringify(ws));
    (storage ?? window.localStorage).setItem(WORKSPACE_KEY, JSON.stringify(clean));
  } catch {
    /* storage blocked: the window still splits, it just forgets */
  }
}

// ---- changes ----------------------------------------------------------------

/** Choose a preset. A focused pane the preset does not draw hands focus to pane 1. */
export function setPreset(ws: Workspace, preset: Preset): Workspace {
  if (!isPreset(preset)) return ws;
  const focused = panesOf(preset).includes(ws.focused) ? ws.focused : 1;
  return { ...ws, preset, focused };
}

export function focusPane(ws: Workspace, pane: PaneNo): Workspace {
  return ws.focused === pane ? ws : { ...ws, focused: pane };
}

/**
 * Show `view` in `pane`, and focus it. Chat is a singleton: choosing it here
 * swaps with the pane that had it, whether or not that pane is drawn — the
 * **whole spec** (view, pin, preview URL, terminal), so the pane that takes
 * this one's view takes what it was showing too, never a stale URL or pin of
 * its own from before.
 */
export function setView(ws: Workspace, pane: PaneNo, view: View): Workspace {
  if (!isView(view)) return ws;
  const panes = ws.panes.map((p) => ({ ...p })) as Panes;
  const here = pane - 1;
  if (view === "chat" && panes[here].view !== "chat") {
    const other = panes.findIndex((p) => p.view === "chat");
    if (other >= 0) {
      const mine = panes[here];
      panes[here] = panes[other];
      panes[other] = mine;
      return { ...ws, panes, focused: pane };
    }
  }
  panes[here].view = view;
  return { ...ws, panes, focused: pane };
}

/**
 * Where a sidebar click lands (§2.2, "Where a sidebar click goes"): the pane
 * already showing that kind of thing, and only otherwise the focused pane.
 *
 *   - chat: the drawn pane that shows it gets focus; else the focused pane
 *     switches to chat (swapping with a hidden chat pane, the singleton rule);
 *   - task: a drawn task or diff pane follows the selected task by itself, so
 *     nothing moves; with none drawn the focused pane switches to task — in
 *     the single layout exactly today's behaviour;
 *   - anything else: as chat.
 *
 * `drawn` is what the window draws now (`fitWorkspace(...).panes`).
 */
export function show(ws: Workspace, view: View, drawn: readonly PaneNo[]): Workspace {
  const visible = drawn.length ? drawn : [1 as PaneNo];
  if (view === "task") {
    if (visible.some((n) => ws.panes[n - 1].view === "task" || ws.panes[n - 1].view === "diff")) return ws;
  } else {
    const showing = visible.filter((n) => ws.panes[n - 1].view === view);
    if (showing.length) return showing.includes(ws.focused) ? ws : focusPane(ws, showing[0]);
  }
  const target = visible.includes(ws.focused) ? ws.focused : visible[0];
  return setView(ws, target, view);
}

/** Pin a File or Preview pane to a project (null: follow the chat again). */
export function pinPane(ws: Workspace, pane: PaneNo, projectId: string | null): Workspace {
  if (ws.panes[pane - 1].projectId === projectId) return ws;
  const panes = ws.panes.map((p, i) => (i === pane - 1 ? { ...p, projectId } : p)) as Panes;
  return { ...ws, panes };
}

/**
 * A project went away: every pane pinned to it follows the chat again —
 * except the panes in `keep` (a File pane holding an unsaved edit), which
 * stay pinned until the owner says to discard it.
 */
export function unpinProject(ws: Workspace, projectId: string, keep: readonly PaneNo[] = []): Workspace {
  const hit = (p: PaneSpec, i: number) => p.projectId === projectId && !keep.includes((i + 1) as PaneNo);
  if (!ws.panes.some(hit)) return ws;
  return { ...ws, panes: ws.panes.map((p, i) => (hit(p, i) ? { ...p, projectId: null } : p)) as Panes };
}

/**
 * Show terminal `id` in `pane` (null: the pane chooses again). A terminal is
 * drawn in one place at a time, so any *other* pane holding it lets it go —
 * its view stays "terminal" and it offers the choice again.
 */
export function setPaneTerminal(ws: Workspace, pane: PaneNo, id: string | null): Workspace {
  const here = ws.panes[pane - 1];
  const elsewhere = id !== null && ws.panes.some((p, i) => i !== pane - 1 && p.terminalId === id);
  if (here.terminalId === id && !elsewhere) return ws;
  const panes = ws.panes.map((p, i) => {
    if (i === pane - 1) return { ...p, terminalId: id };
    return id !== null && p.terminalId === id ? { ...p, terminalId: null } : p;
  }) as Panes;
  return { ...ws, panes };
}

/** A terminal was closed or ended: no pane holds it any more. */
export function forgetTerminal(ws: Workspace, id: string): Workspace {
  if (!ws.panes.some((p) => p.terminalId === id)) return ws;
  return { ...ws, panes: ws.panes.map((p) => (p.terminalId === id ? { ...p, terminalId: null } : p)) as Panes };
}

export function setPreviewUrl(ws: Workspace, pane: PaneNo, url: string): Workspace {
  const cur = ws.panes[pane - 1].previewUrl ?? "";
  if (cur === url || url.length > URL_MAX) return ws;
  const panes = ws.panes.map((p, i) => {
    if (i !== pane - 1) return p;
    const next = { ...p };
    if (url) next.previewUrl = url;
    else delete next.previewUrl;
    return next;
  }) as Panes;
  return { ...ws, panes };
}

export function setSplit(ws: Workspace, preset: Preset, split: Partial<Split>): Workspace {
  const next = parseSplit({ ...ws.splits[preset], ...split }, preset);
  return { ...ws, splits: { ...ws.splits, [preset]: next } };
}

/** Double-click on an edge: the shape's panes equal. */
export function equalSplit(ws: Workspace, preset: Preset): Workspace {
  const n = SHAPES[preset].cols;
  return setSplit(ws, preset, { cols: Array.from({ length: n - 1 }, (_, i) => (i + 1) / n), row: 0.5 });
}

export function setPanel(ws: Workspace, panel: Partial<Workspace["panel"]>): Workspace {
  const open = panel.open ?? ws.panel.open;
  const height = panel.height === undefined ? ws.panel.height : clampPanelHeight(panel.height);
  if (open === ws.panel.open && height === ws.panel.height) return ws;
  return { ...ws, panel: { open, height } };
}

/** Reset layout (§2.1): one pane, default edges, the panel closed. What each pane showed is kept. */
export function resetWorkspace(ws: Workspace): Workspace {
  const d = defaultWorkspace();
  return { ...ws, preset: "single", splits: d.splits, focused: 1, panel: d.panel };
}

// ---- edges --------------------------------------------------------------------

/**
 * Column edges fitted to a centre `width` px wide: every column at least
 * `min` wide, as close to the stored fractions as that allows. A centre too
 * narrow for that (only a window too small for anything) gets equal columns.
 */
export function clampCols(cols: readonly number[], width: number, min: number = PANE_MIN_W): number[] {
  const n = cols.length + 1;
  if (n === 1) return [];
  if (!Number.isFinite(width) || width <= 0) return cols.slice();
  if (width < n * min) return Array.from({ length: n - 1 }, (_, i) => (i + 1) / n);
  const px = cols.map((c) => c * width);
  for (let i = 0; i < px.length; i++) px[i] = Math.max(px[i], (i === 0 ? 0 : px[i - 1]) + min);
  for (let i = px.length - 1; i >= 0; i--) px[i] = Math.min(px[i], (i === px.length - 1 ? width : px[i + 1]) - min);
  return px.map((x) => x / width);
}

/** The row edge fitted to a pane area `height` px tall. */
export function clampRow(row: number, height: number, min: number = PANE_MIN_H): number {
  if (!Number.isFinite(height) || height <= 0) return row;
  if (height < 2 * min) return 0.5;
  return Math.min(1 - min / height, Math.max(min / height, row));
}

/** Move column edge `i` to `frac`, stopped `min` px short of its neighbours. */
export function moveColEdge(
  cols: readonly number[], i: number, frac: number, width: number, min: number = PANE_MIN_W,
): number[] {
  if (i < 0 || i >= cols.length || !Number.isFinite(frac)) return cols.slice();
  const gap = Number.isFinite(width) && width > 0 ? min / width : 0;
  const lo = (i === 0 ? 0 : cols[i - 1]) + gap;
  const hi = (i === cols.length - 1 ? 1 : cols[i + 1]) - gap;
  const out = cols.slice();
  out[i] = lo > hi ? cols[i] : Math.min(hi, Math.max(lo, frac));
  return out;
}

/** Each column's width in px, from the fitted edges. */
export function columnWidths(cols: readonly number[], width: number): number[] {
  const edges = [0, ...cols, 1];
  return edges.slice(1).map((e, i) => (e - edges[i]) * width);
}

// ---- fitting ------------------------------------------------------------------

/**
 * One column fewer, keeping the focused pane's column: three columns become
 * two, two become one, one large plus two stacked becomes the large pane or
 * the stacked column, and 2×2 becomes the focused column. Null when there is
 * no column to drop.
 */
export function dropColumn(shape: Preset, panes: readonly PaneNo[], focused: PaneNo): { shape: Preset; panes: PaneNo[] } | null {
  const [a, b, c, d] = panes;
  switch (shape) {
    case "cols3":
      return { shape: "cols2", panes: focused === c ? [b, c] : [a, b] };
    case "cols2":
      return { shape: "single", panes: [panes.includes(focused) ? focused : a] };
    case "main2":
      return focused === b || focused === c ? { shape: "rows2", panes: [b, c] } : { shape: "single", panes: [a] };
    case "grid4":
      return focused === b || focused === d ? { shape: "rows2", panes: [b, d] } : { shape: "rows2", panes: [a, c] };
    default:
      return null;
  }
}

/** One row fewer, keeping the focused pane's row. Null when there is no row to drop. */
export function dropRow(shape: Preset, panes: readonly PaneNo[], focused: PaneNo): { shape: Preset; panes: PaneNo[] } | null {
  const [a, b, c, d] = panes;
  switch (shape) {
    case "rows2":
      return { shape: "single", panes: [panes.includes(focused) ? focused : a] };
    case "grid4":
      return focused === c || focused === d ? { shape: "cols2", panes: [c, d] } : { shape: "cols2", panes: [a, b] };
    case "main2":
      return { shape: "cols2", panes: [a, focused === c ? c : b] };
    default:
      return null;
  }
}

/** What one render drew, handed to the next as `fitWorkspace`'s `previous`. */
export interface DrawnSet {
  preset: Preset;
  drawn: Preset;
  panes: readonly PaneNo[];
}

export interface WorkspaceFit {
  /** The side panes, as `fitLayout` draws them for the drawn shape. */
  sides: Fitted;
  /** The shape this render draws, and the panes in its slots. */
  drawn: Preset;
  panes: PaneNo[];
  /** Panes the chosen preset has that this render leaves out, and why. */
  dropped: number;
  why: "" | "narrow" | "short";
  /** The centre's width and the panes' area height (NaN with no window to fit). */
  width: number;
  height: number;
  /** The drawn shape's edges, fitted to the room. */
  cols: number[];
  row: number;
  /** The bottom panel as drawn; `auto` when the window folded it, not the owner. */
  panel: { open: boolean; height: number; auto: boolean };
}

/**
 * What one render draws. Only the render changes, never what is stored.
 *
 * Across (`available` wide): the side panes give back their slack and fold
 * for the render (`fitLayout`, with the shape's own centre minimum and the
 * owner's `prefer`); only when even both rails leave the centre too narrow is
 * a column dropped, and the focused pane's column is always the one kept.
 *
 * Down (`availableH` tall, the room under the title bar): an open panel
 * shrinks toward its minimum, then folds for the render, and only then are
 * rows dropped. With `preferPanel` (the owner opened a panel the window had
 * folded) rows drop first and the panel is never folded, as `prefer` does
 * for the side panes.
 *
 * **A dropped set is sticky** (`previous`, what the last render drew). Which
 * panes survive a drop is chosen from the focused pane, and focus follows
 * every click — so without this, clicking into the *other* drawn pane of
 * three-columns-drawn-as-two redrew a different pair, and the pane under the
 * pointer jumped. The last render's panes are kept while the preset and the
 * drawn shape are the same (the same shape fits the same room) and they
 * still hold the focused pane; focus moving to a pane they do not draw
 * (Ctrl+Alt+N) draws a set that includes it. Pure: `previous` is read, never
 * written, and nothing here is stored.
 */
export function fitWorkspace(
  layout: PaneLayout, ws: Workspace, available: number, availableH: number,
  prefer: Side | null = null, preferPanel = false, previous: DrawnSet | null = null,
): WorkspaceFit {
  let shape = ws.preset;
  let panes = panesOf(shape);
  const focused = panes.includes(ws.focused) ? ws.focused : panes[0];
  let why: WorkspaceFit["why"] = "";
  const wide = Number.isFinite(available) && available > 0;
  const tall = Number.isFinite(availableH) && availableH > 0;

  const across = () => {
    for (;;) {
      const sides = fitLayout(layout, available, prefer, SHAPES[shape].minW);
      if (!wide || available - sides.left - sides.right >= SHAPES[shape].minW) return sides;
      const next = dropColumn(shape, panes, focused);
      if (!next) return sides;
      ({ shape, panes } = next);
      why = "narrow";
    }
  };
  let sides = across();

  const before = shape;
  let panel = { open: false, height: 0, auto: false };
  if (ws.panel.open && !tall) {
    panel = { open: true, height: ws.panel.height, auto: false };
  } else if (ws.panel.open) {
    for (;;) {
      const room = availableH - SHAPES[shape].minH;
      if (room >= PANEL.min) {
        panel = { open: true, height: Math.min(ws.panel.height, room), auto: false };
        break;
      }
      const next = preferPanel ? dropRow(shape, panes, focused) : null;
      if (next) {
        ({ shape, panes } = next);
        why = why || "short";
        continue;
      }
      panel = preferPanel ? { open: true, height: PANEL.min, auto: false } : { open: false, height: 0, auto: true };
      break;
    }
  }
  if (tall) {
    for (;;) {
      if (SHAPES[shape].minH <= availableH - panel.height) break;
      const next = dropRow(shape, panes, focused);
      if (!next) break;
      ({ shape, panes } = next);
      why = why || "short";
    }
  }
  // A row dropped can need less width: give the side panes back what it frees.
  if (shape !== before) sides = fitLayout(layout, available, prefer, SHAPES[shape].minW);
  // Sticky: the same shape as last render, and focus still inside what it drew.
  if (previous && previous.preset === ws.preset && previous.drawn === shape && shape !== ws.preset
      && previous.panes.length === panes.length && previous.panes.includes(focused)
      && previous.panes.every((n) => panesOf(ws.preset).includes(n))) {
    panes = previous.panes.slice();
  }

  const width = wide ? available - sides.left - sides.right : NaN;
  const height = tall ? availableH - panel.height : NaN;
  const split = ws.splits[shape];
  return {
    sides, drawn: shape, panes,
    dropped: SHAPES[ws.preset].panes - panes.length, why,
    width, height,
    cols: clampCols(split.cols, width), row: clampRow(split.row, height),
    panel,
  };
}

/** The tallest the panel may be dragged to: whatever leaves the drawn panes their minimum height. */
export function maxPanelHeight(fit: WorkspaceFit, availableH: number): number {
  if (!Number.isFinite(availableH) || availableH <= 0) return PANEL.max;
  return Math.max(PANEL.min, Math.min(PANEL.max, Math.floor(availableH - SHAPES[fit.drawn].minH)));
}
