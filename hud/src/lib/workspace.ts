// The centre's panes (design §18; plans docs/plans/2026-10-09-hud-workspace-
// plan.md §2.2 and 2026-10-10-hud-split-panes-plan.md §3): which preset is
// chosen, what each of the eight panes shows, where the edges between them
// are, the bottom panel, and how all of it is fitted to the window. Pure,
// like lib/layout.ts, so every rule is tested without a browser;
// components/Workspace.tsx and the title bar only draw what this file decides.
//
// **Presets, not free splitting.** Ten shapes, one to eight panes (WP-G1,
// 2026-10-10; decisions S-1): the six of WP-A, plus 3 over 2, 3×2, 4 over 3
// and 4×2. ⊞ offers eight of them (`MENU_PRESETS`); 5 and 7 are reached by
// split and close (WP-G2) or a stored workspace. Every shape but main2 is a
// list of rows (`ShapeSpec.rows`, panes per row), and the CSS grid, the edges
// and the drops are all derived from it.
//
// **A pane's id is not its slot** (plan G-1). A pane keeps its id
// (`pane-<id>`, its React key, `chats[id]`, its dirty flag) for life;
// `ws.order` lists the ids the preset draws, in reading order. Every number
// the owner reads is the slot (`slotOf`). A stored WP-A or WP-B workspace has
// no `order`, so it reads as ids 1…n: the same panes it always drew.
//
// **All eight pane specs are always stored**, so going from eight panes to
// one and back brings back what each pane showed: ⊞ to fewer panes moves the
// rest to `kept`, and growing takes `kept` back first. A pane is mounted the
// first time a preset shows it and is then hidden, never unmounted (the side
// panes' rule): an unsaved edit in pane 3 survives switching to one pane.
//
// **Several panes may show chat (WP-B, 2026-10-09)**, each its own
// conversation (lib/chats.ts). WP-A kept the chat a singleton and swapped two
// panes' views when chat was chosen in a second pane; that is gone, and a
// stored workspace with several chat panes is kept as it is.
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

export type Preset =
  | "single" | "cols2" | "rows2" | "cols3" | "main2" | "grid4" | "grid5" | "grid6" | "grid7" | "grid8";
export const PRESETS: readonly Preset[] =
  ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid5", "grid6", "grid7", "grid8"];
/** ⊞'s tiles, four to a row (decisions S-1: 6 and 8 directly; 5 and 7 by split or close). */
export const MENU_PRESETS: readonly Preset[] = ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8"];
/** The shape a split or a close lands on for each count (plan G-3; decisions S-3: three is three columns). */
export const LADDER: Readonly<Record<number, Preset>> = {
  1: "single", 2: "cols2", 3: "cols3", 4: "grid4", 5: "grid5", 6: "grid6", 7: "grid7", 8: "grid8",
};

/** A pane's id: stable for its life, never its position (that is `slotOf`). */
export type PaneNo = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;
export const PANE_NOS: readonly PaneNo[] = [1, 2, 3, 4, 5, 6, 7, 8];

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
  /** Panes per row, top to bottom, in reading order; null for main2 (one large pane beside a stacked column). */
  rows: number[] | null;
}

/** A shape made of rows: the widest row's minimum across, a pane's minimum per row down. */
function rowShape(label: string, rows: number[]): ShapeSpec {
  const widest = Math.max(...rows);
  return {
    label, rows, panes: rows.reduce((a, b) => a + b, 0),
    minW: widest === 1 ? MAIN_MIN : widest * PANE_MIN_W, minH: rows.length * PANE_MIN_H,
  };
}

export const SHAPES: Record<Preset, ShapeSpec> = {
  single: rowShape("One pane", [1]),
  cols2: rowShape("Two side by side", [2]),
  rows2: rowShape("Two stacked", [1, 1]),
  cols3: rowShape("Three side by side", [3]),
  main2: { label: "One large, two stacked", panes: 3, minW: MAIN_MIN + PANE_MIN_W, minH: 2 * PANE_MIN_H, rows: null },
  grid4: rowShape("Two by two", [2, 2]),
  grid5: rowShape("Three over two", [3, 2]),
  grid6: rowShape("Three by two", [3, 3]),
  grid7: rowShape("Four over three", [4, 3]),
  grid8: rowShape("Four by two", [4, 4]),
};

/** The shape whose rows are exactly these, if one is a preset. */
function presetOfRows(rows: readonly number[]): Preset | null {
  const key = rows.join(",");
  return PRESETS.find((p) => SHAPES[p].rows?.join(",") === key) ?? null;
}

/** How many rows a shape draws. */
export function rowCount(preset: Preset): number {
  return SHAPES[preset].rows?.length ?? 2;
}

/** A two-row shape whose rows differ (3 over 2, 4 over 3): its bottom row has edges of its own. */
export function uneven(preset: Preset): boolean {
  const r = SHAPES[preset].rows;
  return !!r && r.length === 2 && r[0] !== r[1];
}

/** Columns in the top row (main2: the large pane and the stacked column). */
function topColumns(preset: Preset): number {
  return SHAPES[preset].rows?.[0] ?? 2;
}

export interface PaneSpec {
  view: View;
  /** File and Preview panes: the project they are pinned to, or null to follow the chat. */
  projectId: string | null;
  /** The terminal a terminal pane shows (WP-D), or null to choose one. */
  terminalId: string | null;
  /** Preview: the URL last loaded here, judged again before it is loaded again. */
  previewUrl?: string;
  /** File and Diff: the open file, relative to the pane's project (read and written by WP-G2). */
  filePath?: string;
}

/**
 * Where a shape's edges are, as fractions: `cols` the cumulative column
 * edges, `row` the row edge, and — for 3 over 2 and 4 over 3 only, whose rows
 * cannot share columns — `bottom`, the bottom row's own column edges.
 */
export interface Split {
  cols: number[];
  row: number;
  bottom?: number[];
}

/** One spec per pane id, eight of them, by id (`panes[id - 1]`). */
export type Panes = PaneSpec[];

export interface Workspace {
  preset: Preset;
  /** The ids the preset draws, in reading order: as many as `SHAPES[preset].panes`. */
  order: PaneNo[];
  /** Ids ⊞ hid, most recently hidden first, specs kept to come back; never in `order`. */
  kept: PaneNo[];
  panes: Panes;
  splits: Record<Preset, Split>;
  /** Always one of `order`. */
  focused: PaneNo;
  panel: { open: boolean; height: number };
}

const DEFAULT_VIEWS: Record<PaneNo, View> = {
  1: "chat", 2: "preview", 3: "file", 4: "task", 5: "chat", 6: "chat", 7: "chat", 8: "chat",
};

const equalEdges = (n: number) => Array.from({ length: n - 1 }, (_, i) => (i + 1) / n);

/** Equal columns for a shape (main2's large pane is wider by default). */
function defaultSplit(preset: Preset): Split {
  if (preset === "main2") return { cols: [0.6], row: 0.5 };
  const rows = SHAPES[preset].rows!;
  const split: Split = { cols: equalEdges(rows[0]), row: 0.5 };
  if (uneven(preset)) split.bottom = equalEdges(rows[1]);
  return split;
}

export function defaultPane(n: PaneNo): PaneSpec {
  return { view: DEFAULT_VIEWS[n], projectId: null, terminalId: null };
}

export function defaultWorkspace(): Workspace {
  const splits = {} as Record<Preset, Split>;
  for (const p of PRESETS) splits[p] = defaultSplit(p);
  return {
    preset: "single",
    order: [1],
    kept: [],
    panes: PANE_NOS.map(defaultPane),
    splits,
    focused: 1,
    panel: { open: false, height: PANEL.def },
  };
}

/**
 * Every id, in the order a growing preset takes them: the drawn ones, then
 * the ones ⊞ hid (most recently hidden first), then the free ones, lowest
 * first. ⊞ to n panes draws the first n of these.
 */
export function paneSequence(ws: Pick<Workspace, "order" | "kept">): PaneNo[] {
  const free = PANE_NOS.filter((n) => !ws.order.includes(n) && !ws.kept.includes(n));
  return [...ws.order, ...ws.kept, ...free];
}

/**
 * The ids the preset draws, in reading order. `ws.order` always has the
 * preset's count; a workspace put together by hand that does not is read the
 * way a grow would read it.
 */
export function orderOf(ws: Pick<Workspace, "preset" | "order" | "kept">): PaneNo[] {
  const n = SHAPES[ws.preset].panes;
  return ws.order.length === n ? ws.order.slice() : paneSequence(ws).slice(0, n);
}

/**
 * The number the owner reads for pane `id` (plan G-1): its slot, 1-based in
 * reading order. A pane the preset does not draw reads as the slot a grow
 * would bring it back in.
 */
export function slotOf(ws: Pick<Workspace, "preset" | "order" | "kept">, id: PaneNo): number {
  const drawn = orderOf(ws);
  const at = drawn.indexOf(id);
  if (at >= 0) return at + 1;
  return [...drawn, ...paneSequence(ws).filter((n) => !drawn.includes(n))].indexOf(id) + 1;
}

/** The id in slot `slot` (1-based), if the preset has that many. */
export function paneAt(ws: Pick<Workspace, "preset" | "order" | "kept">, slot: number): PaneNo | null {
  return orderOf(ws)[slot - 1] ?? null;
}

export function isPreset(v: unknown): v is Preset {
  return typeof v === "string" && (PRESETS as readonly string[]).includes(v);
}

function isPaneNo(v: unknown): v is PaneNo {
  return typeof v === "number" && (PANE_NOS as readonly number[]).includes(v);
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
export const PATH_MAX = 1024;

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
  if (typeof v.filePath === "string" && v.filePath && v.filePath.length <= PATH_MAX) {
    spec.filePath = v.filePath;
  }
  return spec;
}

/** Distinct pane ids, exactly `n` of them, else null. */
function parseOrder(raw: unknown, n: number): PaneNo[] | null {
  if (!Array.isArray(raw) || raw.length !== n || !raw.every(isPaneNo)) return null;
  return new Set(raw).size === n ? (raw as PaneNo[]).slice() : null;
}

/** The distinct pane ids that are not drawn; anything else is dropped. */
function parseKept(raw: unknown, order: readonly PaneNo[]): PaneNo[] {
  if (!Array.isArray(raw)) return [];
  const out: PaneNo[] = [];
  for (const x of raw) if (isPaneNo(x) && !order.includes(x) && !out.includes(x)) out.push(x);
  return out;
}

const inside = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x) && x > 0 && x < 1;

/** Fractions strictly inside (0, 1), strictly increasing, `like`'s count; else `like`. */
function parseEdges(raw: unknown, like: number[]): number[] {
  if (Array.isArray(raw) && raw.length === like.length && raw.every(inside)
      && raw.every((x, i) => i === 0 || x > (raw as number[])[i - 1])) {
    return (raw as number[]).slice();
  }
  return like;
}

/** Each part on its own: the top edges, the row edge and (3 over 2, 4 over 3) the bottom row's edges. */
function parseSplit(raw: unknown, preset: Preset): Split {
  const def = defaultSplit(preset);
  const v = isObj(raw) ? raw : {};
  const split: Split = { cols: parseEdges(v.cols, def.cols), row: inside(v.row) ? v.row : def.row };
  if (def.bottom) split.bottom = parseEdges(v.bottom, def.bottom);
  return split;
}

/**
 * The stored JSON → a workspace. Each field stands on its own (a bad split
 * does not reset the panes, a bad `order` does not reset the preset), an
 * unknown preset reads as `single`, and the panel is open only on a literal
 * `true`. A WP-A or WP-B value has no `order`: it draws ids 1…n, the panes it
 * always drew. A rolled-back HUD ignores `order` and `kept`, and reads a
 * preset it does not know as single, so that direction is safe too.
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
  const n = SHAPES[preset].panes;
  const order = parseOrder(v.order, n) ?? PANE_NOS.slice(0, n);
  const kept = parseKept(v.kept, order);
  const rawPanes = Array.isArray(v.panes) ? v.panes : [];
  const panes = PANE_NOS.map((id) => parsePane(rawPanes[id - 1], id));
  const rawSplits = isObj(v.splits) ? v.splits : {};
  const splits = {} as Record<Preset, Split>;
  for (const p of PRESETS) splits[p] = parseSplit(rawSplits[p], p);
  const focused: PaneNo = isPaneNo(v.focused) && order.includes(v.focused) ? v.focused : order[0];
  const panel = isObj(v.panel) ? v.panel : {};
  return {
    preset, order, kept, panes, splits, focused,
    panel: { open: panel.open === true, height: clampPanelHeight(panel.height) },
  };
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

/**
 * Choose a preset (⊞). Fewer panes keep the first ones in reading order and
 * hide the rest (`kept`, their specs as they were); more panes take back the
 * hidden ones first, most recently hidden first, then free ids; the same
 * count changes only the shape. A focused pane the preset no longer draws
 * hands focus to the first one. So 8 → 1 → 8 brings back what each pane
 * showed, where it was (plan G-2: ⊞ hides, it never closes).
 */
export function setPreset(ws: Workspace, preset: Preset): Workspace {
  if (!isPreset(preset)) return ws;
  const seq = paneSequence(ws);
  const n = SHAPES[preset].panes;
  const order = seq.slice(0, n);
  const kept = seq.slice(n).filter((id) => ws.order.includes(id) || ws.kept.includes(id));
  const focused = order.includes(ws.focused) ? ws.focused : order[0];
  return { ...ws, preset, order, kept, focused };
}

export function focusPane(ws: Workspace, pane: PaneNo): Workspace {
  return ws.focused === pane ? ws : { ...ws, focused: pane };
}

/**
 * Show `view` in `pane`, and focus it. Only that pane changes: several panes
 * may show chat, each its own conversation (WP-B), so choosing chat in a
 * second pane no longer swaps it with the first.
 */
export function setView(ws: Workspace, pane: PaneNo, view: View): Workspace {
  if (!isView(view)) return ws;
  const panes = ws.panes.map((p, i) => (i === pane - 1 ? { ...p, view } : p)) as Panes;
  return { ...ws, panes, focused: pane };
}

/**
 * Where a sidebar click lands (§2.2, "Where a sidebar click goes"): the pane
 * already showing that kind of thing, and only otherwise the focused pane.
 *
 *   - chat: a drawn chat pane gets focus (the focused one if it is one);
 *     else the focused pane switches to chat. Which chat pane a *thread*
 *     lands in — the one showing it, the focused chat pane, the selected chat
 *     — is lib/chats.ts `chatTarget`, which the window uses for threads;
 *   - task: a drawn task or diff pane follows the selected task by itself, so
 *     nothing moves; with none drawn the focused pane switches to task — in
 *     the single layout exactly today's behaviour;
 *   - anything else: as chat.
 *
 * `drawn` is what the window draws now (`fitWorkspace(...).panes`).
 */
export function show(ws: Workspace, view: View, drawn: readonly PaneNo[]): Workspace {
  const visible = drawn.length ? drawn : [orderOf(ws)[0]];
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

/** Double-click on an edge: the shape's panes equal (each row's own, for 3 over 2 and 4 over 3). */
export function equalSplit(ws: Workspace, preset: Preset): Workspace {
  const rows = SHAPES[preset].rows;
  const even: Split = { cols: equalEdges(topColumns(preset)), row: 0.5 };
  if (rows && uneven(preset)) even.bottom = equalEdges(rows[1]);
  return setSplit(ws, preset, even);
}

export function setPanel(ws: Workspace, panel: Partial<Workspace["panel"]>): Workspace {
  const open = panel.open ?? ws.panel.open;
  const height = panel.height === undefined ? ws.panel.height : clampPanelHeight(panel.height);
  if (open === ws.panel.open && height === ws.panel.height) return ws;
  return { ...ws, panel: { open, height } };
}

/**
 * Reset layout (§2.1): one pane (the first in reading order; ⊞ keeps the
 * rest), default edges, the panel closed. What each pane showed is kept.
 */
export function resetWorkspace(ws: Workspace): Workspace {
  const d = defaultWorkspace();
  return { ...setPreset({ ...ws, order: orderOf(ws) }, "single"), splits: d.splits, panel: d.panel };
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

// ---- the grid -----------------------------------------------------------------

/** One pane's place in its shape, as fractions of the pane area: slot order, reading order. */
export interface Cell {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** The edges a shape is drawn with: its top-row columns, its row edge, its bottom row's own columns. */
export type Edges = Pick<Split, "cols" | "row"> & { bottom?: readonly number[] };

/**
 * Where each slot of `shape` sits, given its (fitted) edges. A uniform shape
 * shares one set of column edges between its rows; 3 over 2 and 4 over 3
 * read the bottom row from `bottom`; main2 is one large pane beside a stacked
 * column.
 */
export function cellsOf(shape: Preset, edges: Edges): Cell[] {
  const rows = SHAPES[shape].rows;
  const r = edges.row;
  // Edges of the wrong count (a hand-made value) read as equal columns.
  const want = (e: readonly number[] | undefined, n: number) => (e && e.length === n - 1 ? e : equalEdges(n));
  if (!rows) {
    const c = want(edges.cols, 2)[0];
    return [
      { left: 0, right: c, top: 0, bottom: 1 },
      { left: c, right: 1, top: 0, bottom: r },
      { left: c, right: 1, top: r, bottom: 1 },
    ];
  }
  const out: Cell[] = [];
  rows.forEach((n, i) => {
    const xs = [0, ...want(i === 1 && uneven(shape) ? edges.bottom : edges.cols, n), 1];
    const top = rows.length === 1 ? 0 : i === 0 ? 0 : r;
    const bottom = rows.length === 1 ? 1 : i === 0 ? r : 1;
    for (let j = 0; j < n; j++) out.push({ left: xs[j], right: xs[j + 1], top, bottom });
  });
  return out;
}

/** Coincident edges are one (a drag that lines a bottom edge up with a top one). */
const SAME_EDGE = 1e-4;

function unionEdges(xs: number[]): number[] {
  const out: number[] = [];
  for (const x of [...xs].sort((a, b) => a - b)) {
    if (!out.length || x - out[out.length - 1] > SAME_EDGE) out.push(x);
  }
  out[0] = 0;
  out[out.length - 1] = 1;
  return out;
}

const nearest = (edges: readonly number[], x: number) =>
  edges.reduce((best, e, i) => (Math.abs(e - x) < Math.abs(edges[best] - x) ? i : best), 0);

/**
 * The CSS grid for a shape: its unit columns and rows (as fractions) and its
 * areas, slots named `s1` … `s8`. The unit columns are the union of every
 * row's edges, so a row whose edges are not the top row's (3 over 2: ⅓, ⅔
 * over ½ gives ⅓ ⅙ ⅙ ⅓) spans the units between its own edges: "s1 s2 s2 s3"
 * over "s4 s4 s5 s5". Coincident edges merge.
 */
export function gridTemplate(shape: Preset, edges: Edges): { columns: number[]; rows: number[]; areas: string[] } {
  const cells = cellsOf(shape, edges);
  const xs = unionEdges([0, 1, ...cells.flatMap((c) => [c.left, c.right])]);
  const ys = unionEdges([0, 1, ...cells.flatMap((c) => [c.top, c.bottom])]);
  const spans = cells.map((c) => ({
    x0: nearest(xs, c.left), x1: nearest(xs, c.right), y0: nearest(ys, c.top), y1: nearest(ys, c.bottom),
  }));
  const areas: string[] = [];
  for (let y = 0; y < ys.length - 1; y++) {
    const row: string[] = [];
    for (let x = 0; x < xs.length - 1; x++) {
      const at = spans.findIndex((s) => s.x0 <= x && x < s.x1 && s.y0 <= y && y < s.y1);
      row.push(`s${at + 1}`);
    }
    areas.push(row.join(" "));
  }
  const diffs = (e: number[]) => e.slice(1).map((v, i) => v - e[i]);
  return { columns: diffs(xs), rows: diffs(ys), areas };
}

// ---- fitting ------------------------------------------------------------------

/** A shape's panes, row by row. */
function byRows(rows: readonly number[], panes: readonly PaneNo[]): PaneNo[][] {
  const out: PaneNo[][] = [];
  let at = 0;
  for (const n of rows) {
    out.push(panes.slice(at, at + n));
    at += n;
  }
  return out;
}

/** Where the focused pane is: its row and its place in it (the first pane's when it is not drawn). */
function locate(grid: readonly PaneNo[][], focused: PaneNo): { row: number; at: number } {
  const row = grid.findIndex((r) => r.includes(focused));
  return row < 0 ? { row: 0, at: 0 } : { row, at: grid[row].indexOf(focused) };
}

/** The leftmost `w` panes of a row that still hold place `at`. */
const windowStart = (at: number, w: number) => Math.max(0, at - w + 1);

/**
 * One column fewer, keeping the focused pane's column: every row keeps a
 * contiguous window as wide as the widest row less one — the focused pane's
 * row the leftmost window that holds it, the other rows the window at the
 * same start, clamped. So 4×2 and 4 over 3 become 3×2, 3×2 and 3 over 2
 * become 2×2, 2×2 the focused column, three columns two, two one. One large
 * plus two stacked becomes the large pane or the stacked column. Null when
 * there is no column to drop.
 */
export function dropColumn(shape: Preset, panes: readonly PaneNo[], focused: PaneNo): { shape: Preset; panes: PaneNo[] } | null {
  const rows = SHAPES[shape].rows;
  if (!rows) {
    const [a, b, c] = panes;
    return focused === b || focused === c ? { shape: "rows2", panes: [b, c] } : { shape: "single", panes: [a] };
  }
  const t = Math.max(...rows) - 1;
  if (t < 1) return null;
  const grid = byRows(rows, panes);
  const f = locate(grid, focused);
  const start = windowStart(f.at, Math.min(grid[f.row].length, t));
  const kept = grid.map((r) => {
    const w = Math.min(r.length, t);
    const s = Math.min(start, r.length - w);
    return r.slice(s, s + w);
  });
  const next = presetOfRows(kept.map((r) => r.length));
  return next ? { shape: next, panes: kept.flat() } : null;
}

/**
 * One row fewer, keeping the focused pane's row (one large plus two stacked:
 * the large pane and the focused one of the stack). A row four wide also
 * loses a column — there is no four-column shape — keeping the leftmost
 * three that hold the focused pane. Null when there is no row to drop.
 */
export function dropRow(shape: Preset, panes: readonly PaneNo[], focused: PaneNo): { shape: Preset; panes: PaneNo[] } | null {
  const rows = SHAPES[shape].rows;
  if (!rows) {
    const [a, b, c] = panes;
    return { shape: "cols2", panes: [a, focused === c ? c : b] };
  }
  if (rows.length < 2) return null;
  const grid = byRows(rows, panes);
  const f = locate(grid, focused);
  let keep = grid[f.row];
  if (keep.length > 3) keep = keep.slice(windowStart(f.at, 3), windowStart(f.at, 3) + 3);
  const next = presetOfRows([keep.length]);
  return next ? { shape: next, panes: keep.slice() } : null;
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
  /** The shape this render draws, and the panes in its slots (ids, reading order). */
  drawn: Preset;
  panes: PaneNo[];
  /** Panes the chosen preset has that this render leaves out, and why. */
  dropped: number;
  why: "" | "narrow" | "short";
  /** The centre's width and the panes' area height (NaN with no window to fit). */
  width: number;
  height: number;
  /** The drawn shape's edges, fitted to the room: the top row's columns, the row edge, the bottom row's own columns (3 over 2, 4 over 3; else empty). */
  cols: number[];
  row: number;
  bottom: number[];
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
 * drawn shape are the same (the same shape fits the same room), they still
 * hold the focused pane, and they are a set these panes in this order can
 * draw in this shape — the same drops from some focused pane give exactly
 * them (so a reordered workspace is never drawn from a stale set); focus
 * moving to a pane they do not draw (Ctrl+Alt+N) draws a set that includes
 * it. Pure: `previous` is read, never written, and nothing here is stored.
 */
export function fitWorkspace(
  layout: PaneLayout, ws: Workspace, available: number, availableH: number,
  prefer: Side | null = null, preferPanel = false, previous: DrawnSet | null = null,
): WorkspaceFit {
  let shape = ws.preset;
  const order = orderOf(ws);
  let panes = order.slice();
  const focused = panes.includes(ws.focused) ? ws.focused : panes[0];
  let why: WorkspaceFit["why"] = "";
  const wide = Number.isFinite(available) && available > 0;
  const tall = Number.isFinite(availableH) && availableH > 0;
  // The drops made, in order, so a sticky set can be checked against them.
  const drops: ("column" | "row")[] = [];

  const across = () => {
    for (;;) {
      const sides = fitLayout(layout, available, prefer, SHAPES[shape].minW);
      if (!wide || available - sides.left - sides.right >= SHAPES[shape].minW) return sides;
      const next = dropColumn(shape, panes, focused);
      if (!next) return sides;
      ({ shape, panes } = next);
      drops.push("column");
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
        drops.push("row");
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
      drops.push("row");
      why = why || "short";
    }
  }
  // A row dropped can need less width: give the side panes back what it frees.
  if (shape !== before) sides = fitLayout(layout, available, prefer, SHAPES[shape].minW);
  // Sticky: the same shape as last render, and focus still inside what it drew.
  const drawable = (set: readonly PaneNo[]) => order.some((f) => {
    let s = ws.preset;
    let p = order;
    for (const d of drops) {
      const next = d === "column" ? dropColumn(s, p, f) : dropRow(s, p, f);
      if (!next) return false;
      ({ shape: s, panes: p } = next);
    }
    return s === shape && p.length === set.length && p.every((x, i) => x === set[i]);
  });
  if (previous && previous.preset === ws.preset && previous.drawn === shape && shape !== ws.preset
      && previous.panes.length === panes.length && previous.panes.includes(focused) && drawable(previous.panes)) {
    panes = previous.panes.slice();
  }

  const width = wide ? available - sides.left - sides.right : NaN;
  const height = tall ? availableH - panel.height : NaN;
  const split = ws.splits[shape] ?? defaultSplit(shape);
  return {
    sides, drawn: shape, panes,
    dropped: SHAPES[ws.preset].panes - panes.length, why,
    width, height,
    cols: clampCols(split.cols, width), row: clampRow(split.row, height),
    bottom: split.bottom ? clampCols(split.bottom, width) : [],
    panel,
  };
}

/** Each drawn pane's width in px, by slot, from the fit (its own row's edges). */
export function slotWidths(fit: Pick<WorkspaceFit, "drawn" | "cols" | "row" | "bottom" | "width">): number[] {
  return cellsOf(fit.drawn, fit).map((c) => (c.right - c.left) * fit.width);
}

/** The tallest the panel may be dragged to: whatever leaves the drawn panes their minimum height. */
export function maxPanelHeight(fit: WorkspaceFit, availableH: number): number {
  if (!Number.isFinite(availableH) || availableH <= 0) return PANEL.max;
  return Math.max(PANEL.min, Math.min(PANEL.max, Math.floor(availableH - SHAPES[fit.drawn].minH)));
}
