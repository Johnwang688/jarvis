// The centre's panes (design §18; plans docs/plans/2026-10-09-hud-workspace-
// plan.md §2.2, 2026-10-10-hud-split-panes-plan.md §3 and 2026-10-19-hud-free-
// split-plan.md §3, §7–§9): how the panes are arranged, what each of the twelve
// pane ids shows, the bottom panel, and how all of it is fitted to the window.
// Pure, like lib/layout.ts, so every rule is tested without a browser;
// components/Workspace.tsx and the title bar only draw what this file decides.
//
// **The split tree is the model (FS3, plan P-1).** `ws.tree` (lib/splittree.ts)
// says which panes are drawn, how they are arranged and their sizes. The order
// (`orderOf`: the tree's leaves, depth first) and the preset (`presetOfWs`: the
// preset whose structure the tree has, sizes ignored) are derived, never
// stored as truth. ⊞ lays a preset out at its default sizes (decisions FS-8:
// no memory of a custom shape). A split (FS4) halves one pane, right or down,
// and moves nothing else (FS-5); × gives the closed pane's space to its
// neighbour in the same split. Four across and three down are the most, counted
// over the drawn window (FS-3), and up to twelve panes (FS-1).
//
// **A pane's id is not its slot** (plan G-1). A pane keeps its id
// (`pane-<id>`, its React key, `chats[id]`, its dirty flag) for life; its slot
// is its place in reading order (`slotOf`), and every number the owner reads
// is the slot. A stored WP-A or WP-B workspace has no `order`, so it reads as
// ids 1…n: the same panes it always drew.
//
// **Every pane spec is always stored**, so going from eight panes to one and
// back brings back what each pane showed: ⊞ to fewer panes moves the rest to
// `kept`, and growing takes `kept` back first. A pane is mounted the first time
// it is drawn and is then hidden, never unmounted (the side panes' rule): an
// unsaved edit in pane 3 survives switching to one pane.
//
// **Several panes may show chat (WP-B, 2026-10-09)**, each its own
// conversation (lib/chats.ts).
//
// **Fitting changes only the render, never what is stored** (`fitWorkspace`,
// the generalisation of `fitLayout`, §7). Across: the side panes give back
// their slack, then fold (right first), and only then are panes dropped
// (`prune`), never the focused one. Down: the panel shrinks to its minimum,
// then folds, and only then are panes dropped. Opening a panel the window
// folded drops panes instead (the vertical `prefer`). Each node is then
// clamped so every pane gets its minimum, and each pane is drawn from its own
// box (C5): moving one never moves another by a pixel.
//
// **Stored as version 2** (§8): the tree, with a v1 mirror (`preset`,
// `order`) beside it that a HUD rolled back past FS3 reads; a v1 value (or a
// bad v2 tree) is read by today's parser kept verbatim (`parseV1`) and
// migrated (§8.2).
//
// Every number here is in the HUD's own (unzoomed) pixels, like layout.ts.

import { fitLayout, type Fitted, type PaneLayout, type Side } from "./layout";
import { originOf, storableKeepOrigin } from "./browser";
import { parseTabs, sameTabs, type Tabs } from "./buffers";
import { cleanTitle } from "./remote";
import { parseRef, type SubagentRef } from "./subagents";
import {
  MAX_ACROSS, MAX_DOWN, acrossS, clampSizes, crossing, downS, edgesOf, insertBeside, isLeaf, leaves,
  minH as treeMinH, minW as treeMinW, parseTree, pathOf, presetOf, presetTree, prune, rectsOf, removeLeaf, shapeKey,
  storeTree, withSizes,
  type Axis, type LayoutNode, type Path, type Rect, type StoredNode,
} from "./splittree";

export const WORKSPACE_KEY = "jarvis.hud.workspace";

export type View = "chat" | "task" | "file" | "diff" | "browser" | "terminal" | "vex" | "subagent";
/** What a pane offers. The terminal view (WP-D) shows one of the owner's
 * terminals, chosen per pane (`terminalId`); one terminal is drawn in one
 * place at a time (lib/terminal.ts, `placeTerminals`). The VEX view (VX-2)
 * is offered in the strip only when the pane's project is a VEX project
 * (lib/vex.ts `offersVex`); it shows its pinned project. The sub-agent view
 * (SV-3) is never offered: a pane shows it only when a sub-agent's row or
 * card opened it there (`PaneSpec.subagent`), and keeps its button while it
 * does, so the owner can see where they are. */
export const PANE_VIEWS: readonly View[] = ["chat", "task", "file", "diff", "browser", "terminal", "vex", "subagent"];

/**
 * What the owner reads for each view: the strip's buttons, its compact
 * menu and a pane's `aria-label`. The ids are not labels. The editor's id
 * stays `"file"`, so a stored `"file"` pane, the test ids (`tab-file`) and
 * the editor suites need nothing; Preview became Browser in name and id
 * both (BR-3a). Drawn capitalised by the strip's CSS.
 */
export const VIEW_LABEL: Readonly<Record<View, string>> = {
  chat: "chat", task: "task", file: "editor", diff: "diff", browser: "browser", terminal: "terminal", vex: "vex",
  subagent: "sub-agent",
};

export type Preset =
  | "single" | "cols2" | "rows2" | "cols3" | "main2" | "grid4" | "grid5" | "grid6" | "grid7" | "grid8";
export const PRESETS: readonly Preset[] =
  ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid5", "grid6", "grid7", "grid8"];
/** ⊞'s tiles, four to a row (decisions S-1: 6 and 8 directly; 5 and 7 by split or close). */
export const MENU_PRESETS: readonly Preset[] = ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8"];
/**
 * The shape WP-G2's ladder drew for each count, kept only for the v1 mirror
 * (§8.1): a HUD rolled back past FS3 reads a free tree as this preset. FS4's
 * split no longer climbs it.
 */
const MIRROR_SHAPE: Readonly<Record<number, Preset>> = {
  1: "single", 2: "cols2", 3: "cols3", 4: "grid4", 5: "grid5", 6: "grid6", 7: "grid7", 8: "grid8",
};

/** A pane's id: stable for its life, never its position (that is `slotOf`). */
export type PaneNo = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12;
export const PANE_NOS: readonly PaneNo[] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

/** A pane in a split is never drawn narrower or shorter than this. */
export const PANE_MIN_W = 360;
export const PANE_MIN_H = 200;
/** The bottom panel's height: stored, then fitted. */
export const PANEL = { min: 120, def: 260, max: 1600 } as const;
/** The window-wide title bar above everything (§2.1). */
export const TITLEBAR_H = 28;

/**
 * A preset's name and pane count. Its arrangement is `presetTree` (lib/
 * splittree.ts) and its minimum size comes from the tree (§4.4: `fit.minW`,
 * `fit.minH`), so main2's centre minimum is 720 (§7.1 (i)).
 */
export interface ShapeSpec {
  label: string;
  /** How many panes it draws. */
  panes: number;
}

export const SHAPES: Record<Preset, ShapeSpec> = {
  single: { label: "One pane", panes: 1 },
  cols2: { label: "Two side by side", panes: 2 },
  rows2: { label: "Two stacked", panes: 2 },
  cols3: { label: "Three side by side", panes: 3 },
  main2: { label: "One large, two stacked", panes: 3 },
  grid4: { label: "Two by two", panes: 4 },
  grid5: { label: "Three over two", panes: 5 },
  grid6: { label: "Three by two", panes: 6 },
  grid7: { label: "Four over three", panes: 7 },
  grid8: { label: "Four by two", panes: 8 },
};

/** v1's rows per preset (WP-A/WP-B), read only by the v1 parse (`parseV1`). */
const V1_ROWS: Readonly<Record<Preset, readonly number[] | null>> = {
  single: [1], cols2: [2], rows2: [1, 1], cols3: [3], main2: null,
  grid4: [2, 2], grid5: [3, 2], grid6: [3, 3], grid7: [4, 3], grid8: [4, 4],
};

/** A two-row v1 shape whose rows differ (3 over 2, 4 over 3): its bottom row had edges of its own. */
function uneven(preset: Preset): boolean {
  const r = V1_ROWS[preset];
  return !!r && r.length === 2 && r[0] !== r[1];
}

export const BROWSER_TABS_MAX = 12;
/** Privacy provenance, distinct from the optional auto-open display label.
 * Missing/invalid legacy provenance is unknown, never an owner disclosure. */
export type BrowserSource = { kind: "dev_server"; entryId: string } | { kind: "unknown" };
function parseBrowserSource(raw: unknown): BrowserSource {
  return isObj(raw) && raw.kind === "dev_server" && typeof raw.entryId === "string"
    && /^ds_[0-9a-f]{8}$/.test(raw.entryId)
    ? { kind: "dev_server", entryId: raw.entryId } : { kind: "unknown" };
}

export interface BrowserTabSpec {
  id: string;
  url: string;
  mode?: "direct" | "remote";
  remoteTabId?: string;
  title?: string;
  keepOrigin?: string;
  auto?: { kind: "thread" | "task" | "terminal"; label: string };
  source?: BrowserSource;
}
export interface BrowserTabs { tabs: BrowserTabSpec[]; active: string }
export interface BrowserTabOptions { activate?: boolean; auto?: BrowserTabSpec["auto"]; source?: BrowserSource; id?: string; mode?: "direct" | "remote" }

/** Stable identity local to a pane: closing a sibling cannot change it. */
export function browserTabId(taken: readonly string[] = []): string {
  let id: string;
  do { id = Math.floor(Math.random() * 36 ** 6).toString(36).padStart(6, "0"); } while (taken.includes(id));
  return id;
}

export interface PaneSpec {
  view: View;
  /** File and Browser panes: the project they are pinned to, or null to follow the chat. */
  projectId: string | null;
  /** The terminal a terminal pane shows (WP-D), or null to choose one. */
  terminalId: string | null;
  /** Browser: the URL last loaded here, judged again before it is loaded again (`previewUrl` until BR-3a). */
  browserUrl?: string;
  /** Canonical tabs; legacy fields mirror the active tab for one release. */
  browser?: BrowserTabs;
  browserProfile?: string;
  browserCtx?: string;
  /** Stored only when off. Navigation/profile/copy never enable seeing. */
  see?: false;
  /** Named profile preference: an explicit off persists; Private opt-in never does. */
  drive?: false;
  /** A stored Private session is reattached, never implicitly recreated. */
  browserPrivateEnded?: true;
  /**
   * Browser (WP-E, decisions W-4): the origin this pane lets its page keep
   * (`allow-same-origin`), off when absent. Stored as the origin it was given
   * for, so a URL on another port, host or scheme is never covered by it;
   * judged again on every load (lib/browser.ts, `keptOrigin`).
   */
  keepOrigin?: string;
  /** File and Diff: the open file, relative to the pane's project (read and written by WP-G2). */
  filePath?: string;
  /**
   * A File pane's tabs (ED-2): paths relative to its pinned project, the
   * active one and the preview tab. Only with a pinned project; dropped when
   * the pin moves (the paths were the old project's). Re-read from disk on
   * load — unsaved text is never stored.
   */
  editors?: Tabs;
  /**
   * The sub-agent view (SV-3): which child of which chat it shows — ids
   * only, never its words, which the pane reads again from the owner's
   * route. Only with `view: "subagent"`; a bad value shows the default view.
   */
  subagent?: SubagentRef;
}

/**
 * Where a v1 shape's edges were, as fractions (WP-A/WP-B's `splits`, read
 * only to migrate them, §8.2): `cols` the cumulative column edges, `row` the
 * row edge, and — for 3 over 2 and 4 over 3 only — `bottom`, the bottom row's
 * own column edges.
 */
interface Split {
  cols: number[];
  row: number;
  bottom?: number[];
}

/** One spec per pane id, twelve of them, by id (`panes[id - 1]`). */
export type Panes = PaneSpec[];

export interface Workspace {
  /** Which panes are drawn, how they are arranged, and their sizes (lib/splittree.ts). */
  tree: LayoutNode;
  /** Ids ⊞ hid, most recently hidden first, specs kept to come back; never in the tree. */
  kept: PaneNo[];
  panes: Panes;
  /** Always a leaf of the tree. */
  focused: PaneNo;
  panel: { open: boolean; height: number };
}

const DEFAULT_VIEWS: Record<PaneNo, View> = {
  1: "chat", 2: "browser", 3: "file", 4: "task", 5: "chat", 6: "chat", 7: "chat", 8: "chat",
  9: "chat", 10: "chat", 11: "chat", 12: "chat",
};

const equalEdges = (n: number) => Array.from({ length: n - 1 }, (_, i) => (i + 1) / n);

/** A v1 shape's default edges: equal columns (main2's large pane is wider by default). */
function defaultSplit(preset: Preset): Split {
  if (preset === "main2") return { cols: [0.6], row: 0.5 };
  const rows = V1_ROWS[preset]!;
  const split: Split = { cols: equalEdges(rows[0]), row: 0.5 };
  if (uneven(preset)) split.bottom = equalEdges(rows[1]);
  return split;
}

export function defaultPane(n: PaneNo): PaneSpec {
  return { view: DEFAULT_VIEWS[n], projectId: null, terminalId: null };
}

export function defaultWorkspace(): Workspace {
  return {
    tree: { pane: 1 },
    kept: [],
    panes: PANE_NOS.map(defaultPane),
    focused: 1,
    panel: { open: false, height: PANEL.def },
  };
}

/** The ids the tree draws, in reading order (its leaves, depth first; plan P-2). */
export function orderOf(ws: Pick<Workspace, "tree">): PaneNo[] {
  return leaves(ws.tree);
}

/** The preset whose structure the tree has, sizes ignored (a dragged cols2 is still cols2); null for any other tree. */
export function presetOfWs(ws: Pick<Workspace, "tree">): Preset | null {
  return presetOf(ws.tree);
}

/**
 * Every id, in the order a growing preset takes them: the drawn ones, then
 * the ones ⊞ hid (most recently hidden first), then the free ones, lowest
 * first. ⊞ to n panes draws the first n of these.
 */
export function paneSequence(ws: Pick<Workspace, "tree" | "kept">): PaneNo[] {
  const order = orderOf(ws);
  const free = PANE_NOS.filter((n) => !order.includes(n) && !ws.kept.includes(n));
  return [...order, ...ws.kept, ...free];
}

/** Id `n` is drawn, or hidden by ⊞ to come back — not free (closed by ×, or never drawn). */
export function inWorkspace(ws: Pick<Workspace, "tree" | "kept">, n: PaneNo): boolean {
  return orderOf(ws).includes(n) || ws.kept.includes(n);
}

/**
 * The number the owner reads for pane `id` (plan G-1): its slot, 1-based in
 * reading order. A pane the tree does not draw reads as the slot a grow
 * would bring it back in.
 */
export function slotOf(ws: Pick<Workspace, "tree" | "kept">, id: PaneNo): number {
  const drawn = orderOf(ws);
  const at = drawn.indexOf(id);
  if (at >= 0) return at + 1;
  return [...drawn, ...paneSequence(ws).filter((n) => !drawn.includes(n))].indexOf(id) + 1;
}

/** The id in slot `slot` (1-based), if the tree has that many. */
export function paneAt(ws: Pick<Workspace, "tree" | "kept">, slot: number): PaneNo | null {
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

/**
 * A stored view: a known id, and Preview's old one read as the Browser
 * (BR-3a's migration, under the same key). Null for anything else.
 */
function storedView(v: unknown): View | null {
  if (v === "preview") return "browser";
  return isView(v) ? v : null;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

export function clampPanelHeight(h: unknown): number {
  if (typeof h !== "number" || !Number.isFinite(h)) return PANEL.def;
  return Math.min(PANEL.max, Math.max(PANEL.min, Math.round(h)));
}

const ID_MAX = 200;
/** How many panes a workspace stored before WP-G1 held (`Panes` was a 4-tuple). */
const LEGACY_PANES = 4;
const URL_MAX = 2048;
export const PATH_MAX = 1024;

function parsePane(raw: unknown, n: PaneNo): PaneSpec {
  const v = isObj(raw) ? raw : {};
  const id = (x: unknown) => (typeof x === "string" && x && x.length <= ID_MAX ? x : null);
  const spec: PaneSpec = {
    view: storedView(v.view) ?? DEFAULT_VIEWS[n],
    projectId: id(v.projectId),
    terminalId: id(v.terminalId),
  };
  // Explicit empty tabs beat stale mirrors: the final close stays empty.
  const browser = parseBrowserTabs(v.browser);
  if (browser) Object.assign(spec, mirrorBrowser(spec, browser));
  else {
    const url = (x: unknown) => typeof x === "string" && x && x.length <= URL_MAX ? x : null;
    const legacy = url(v.browserUrl) ?? url(v.previewUrl);
    if (legacy) {
      const tab: BrowserTabSpec = { id: "legacy", url: legacy };
      const keep = storableKeepOrigin(v.keepOrigin, legacy);
      if (keep) tab.keepOrigin = keep;
      Object.assign(spec, mirrorBrowser(spec, { tabs: [tab], active: tab.id }));
    }
  }
  if (v.browserProfile === "private" || (typeof v.browserProfile === "string" && /^p-[0-9a-f]{8}$/.test(v.browserProfile))) {
    spec.browserProfile = v.browserProfile;
  }
  if (v.see === false) spec.see = false;
  if (v.drive === false || spec.see === false) spec.drive = false;
  if (spec.browserProfile === "private") {
    if (typeof v.browserCtx === "string" && /^[0-9a-f]{8}$/.test(v.browserCtx)) spec.browserCtx = v.browserCtx;
    if (v.browserPrivateEnded === true || (v.browserCtx != null && !spec.browserCtx)) spec.browserPrivateEnded = true;
    if (spec.browser) {
      const tabs = spec.browser.tabs.map((t): BrowserTabSpec => {
        const next = { ...t, mode: "remote" as const };
        delete next.keepOrigin;
        return next;
      });
      Object.assign(spec, mirrorBrowser(spec, { ...spec.browser, tabs }));
    }
    delete spec.keepOrigin;
  }
  if (typeof v.filePath === "string" && v.filePath && v.filePath.length <= PATH_MAX) {
    spec.filePath = v.filePath;
  }
  // Tabs belong to a pinned project; a WP-G2 `filePath` with none stored is
  // one tab.
  const tabs = spec.projectId
    ? parseTabs(v.editors) ?? (spec.filePath && spec.view === "file" ? parseTabs({ paths: [spec.filePath] }) : null)
    : null;
  if (tabs) spec.editors = tabs;
  if (spec.view === "subagent") {
    const ref = parseRef(v.subagent);
    if (ref) spec.subagent = ref;
    else spec.view = DEFAULT_VIEWS[n];
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

/** What today's (WP-A … FS2) workspace value holds: the v1 model, read only to migrate it (§8.2). */
interface V1Workspace {
  preset: Preset;
  order: PaneNo[];
  kept: PaneNo[];
  panes: Panes;
  splits: Record<Preset, Split>;
  focused: PaneNo;
  panel: { open: boolean; height: number };
}

/**
 * Today's parser, kept verbatim (§8.1): a value with no `version` (WP-A …
 * FS2), a bad v2 tree's mirror, or a version this HUD does not know. Each
 * field stands on its own (a bad split does not reset the panes, a bad
 * `order` does not reset the preset), an unknown preset reads as `single`,
 * and the panel is open only on a literal `true`. A WP-A or WP-B value has no
 * `order`: it draws ids 1…n, the panes it always drew.
 */
function parseV1(v: Record<string, unknown>): V1Workspace {
  const preset = isPreset(v.preset) ? v.preset : "single";
  const n = SHAPES[preset].panes;
  const order = parseOrder(v.order, n) ?? PANE_NOS.slice(0, n);
  const rawPanes = Array.isArray(v.panes) ? v.panes : [];
  // A WP-A or WP-B value has no `order` and no `kept`: its four panes were
  // positions, and the ones its preset did not draw were hidden, specs kept.
  // They are `kept` here (review of PR #47), so a split takes a free id rather
  // than writing over what pane 3 showed, and a grow still brings them back
  // first, in the order they always came back.
  const legacy = v.order === undefined && v.kept === undefined;
  const kept = !legacy ? parseKept(v.kept, order)
    : PANE_NOS.filter((id) => id <= LEGACY_PANES && id <= rawPanes.length && !order.includes(id));
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

const sameSplit = (a: Split, b: Split) => JSON.stringify(a) === JSON.stringify(b);

/**
 * §8.2: a v1 workspace as a tree. Its preset laid out with its order's ids in
 * reading order, at the edges it stored for that preset only (a grid's two
 * rows from `cols` together, 3 over 2's and 4 over 3's bottom row from
 * `bottom`); the other presets' edges are not carried over (FS-8). Default
 * edges give the preset's exact default sizes.
 */
function migrateV1(w: V1Workspace): Workspace {
  const split = w.splits[w.preset];
  const tree = sameSplit(split, defaultSplit(w.preset))
    ? presetTree(w.preset, w.order) : presetTree(w.preset, w.order, split);
  return { tree, kept: w.kept, panes: w.panes, focused: w.focused, panel: w.panel };
}

/**
 * A v2 value (§8.1) whose tree `parseTree` accepted, field by field: `kept`
 * the distinct ids not in the tree, `focused` a leaf else the first,
 * `panel` and `panes` as v1 reads them, `panes` padded to twelve.
 */
function parseV2(v: Record<string, unknown>, tree: LayoutNode): Workspace {
  const order = leaves(tree);
  const rawPanes = Array.isArray(v.panes) ? v.panes : [];
  const panel = isObj(v.panel) ? v.panel : {};
  return {
    tree,
    kept: parseKept(v.kept, order),
    panes: PANE_NOS.map((id) => parsePane(rawPanes[id - 1], id)),
    focused: isPaneNo(v.focused) && order.includes(v.focused) ? v.focused : order[0],
    panel: { open: panel.open === true, height: clampPanelHeight(panel.height) },
  };
}

/**
 * The stored JSON → a workspace. `version === 2` with a tree `parseTree`
 * accepts is read as v2; anything else (no version, a bad v2 tree, a version
 * that is not the number 2) by today's parser (`parseV1`) and migrated, so a
 * bad v2 tree falls back to its own mirror (`preset`, `order`). FS-3 is never
 * checked on read: a stored layout is never lost on reload.
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
  if (!isObj(v)) return defaultWorkspace();
  const tree = v.version === 2 ? parseTree(v.tree) : null;
  return tree ? parseV2(v, tree) : migrateV1(parseV1(v));
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

/** Storage projection is separate from runtime parsing: saving Private state
 * must not blank a live page. Only opaque handles and structural state persist. */
type StoredBrowserTab = Omit<BrowserTabSpec, "url"> & { url?: string };
type StoredPane = Omit<PaneSpec, "browser"> & { browser?: { tabs: StoredBrowserTab[]; active: string } };
/**
 * The stored value (§8.1): version 2, the tree with bare-number leaves, and
 * beside it the v1 mirror a HUD rolled back past FS3 reads (its parser
 * ignores the rest): `preset` the tree's own, else the ladder's shape for its
 * count (at most eight), and `order` only when every id is ≤ 8 and the count
 * is that preset's.
 */
export interface StoredWorkspace {
  version: 2;
  tree: StoredNode;
  kept: PaneNo[];
  panes: StoredPane[];
  focused: PaneNo;
  panel: { open: boolean; height: number };
  preset: Preset;
  order?: PaneNo[];
}
export function workspaceStorage(ws: Workspace): StoredWorkspace {
  const clean = parseWorkspace(JSON.stringify({
    version: 2, tree: storeTree(ws.tree), kept: ws.kept, panes: ws.panes, focused: ws.focused, panel: ws.panel,
  }));
  const order = orderOf(clean);
  const preset = presetOfWs(clean) ?? MIRROR_SHAPE[Math.min(order.length, 8)];
  const mirror = order.every((n) => n <= 8) && order.length === SHAPES[preset].panes ? { order } : {};
  return { version: 2, tree: storeTree(clean.tree), kept: clean.kept, focused: clean.focused, panel: clean.panel,
           preset, ...mirror, panes: clean.panes.map((pane): StoredPane => {
    if (pane.browserProfile !== "private") return pane;
    const out: StoredPane = { ...pane, browserPrivateEnded: true };
    delete out.browserUrl;
    delete out.keepOrigin;
    if (pane.browser) out.browser = { ...pane.browser, tabs: pane.browser.tabs.map((tab) => ({
      id: tab.id, mode: "remote" as const,
      ...(tab.remoteTabId ? { remoteTabId: tab.remoteTabId } : {}),
      ...(tab.source ? { source: { ...tab.source } } : {}),
    })) };
    return out;
  }) };
}

export function saveWorkspace(ws: Workspace, storage?: Setter) {
  try {
    const clean = workspaceStorage(ws);
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
  const kept = seq.slice(n).filter((id) => inWorkspace(ws, id));
  const focused = order.includes(ws.focused) ? ws.focused : order[0];
  // At the preset's default sizes: ⊞ keeps no memory of a shape (FS-8).
  return { ...ws, tree: presetTree(preset, order), kept, focused };
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
  const cur = ws.panes[pane - 1];
  // A sub-agent pane is opened by its row (`setSubagent`), never by a view
  // switch that has no sub-agent to show; leaving the view drops which one.
  if (view === "subagent" && !cur.subagent) return ws;
  const panes = ws.panes.map((p, i) => {
    if (i !== pane - 1) return p;
    const next: PaneSpec = { ...p, view };
    if (view !== "subagent") delete next.subagent;
    return next;
  }) as Panes;
  return { ...ws, panes, focused: pane };
}

/** Pane `pane` shows sub-agent `ref` (SV-3): a pane already showing one shows this one instead, focused. */
export function setSubagent(ws: Workspace, pane: PaneNo, ref: SubagentRef): Workspace {
  const clean = parseRef(ref);
  if (!clean || !isPaneNo(pane)) return ws;
  const panes = ws.panes.map((p, i) => (i === pane - 1
    ? { view: "subagent" as View, projectId: null, terminalId: null, subagent: clean } : p)) as Panes;
  return { ...ws, panes, focused: pane };
}

/** The spec a split opens for sub-agent `ref`. */
export function subagentSpec(ref: SubagentRef): PaneSpec {
  return { view: "subagent", projectId: null, terminalId: null, subagent: { threadId: ref.threadId, sid: ref.sid } };
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

/** A spec moved to another project: its tabs (ED-2) and its open file (WP-G2) were the old project's, so they go. */
function repinned(p: PaneSpec, projectId: string | null): PaneSpec {
  const next = { ...p, projectId };
  delete next.editors;
  delete next.filePath;
  return next;
}

/** Pin a File or Browser pane to a project (null: follow the chat again). Its tabs were the old project's: they go. */
export function pinPane(ws: Workspace, pane: PaneNo, projectId: string | null): Workspace {
  if (ws.panes[pane - 1].projectId === projectId) return ws;
  const panes = ws.panes.map((p, i) => (i === pane - 1 ? repinned(p, projectId) : p)) as Panes;
  return { ...ws, panes };
}

/**
 * A File pane's tabs (ED-2), and the project they are in: pinning a pane that
 * follows the chat to the project its first file came from, in one step with
 * the tab. Null or no paths clears the tabs and keeps the pin.
 */
export function setEditors(ws: Workspace, pane: PaneNo, projectId: string, tabs: Tabs | null): Workspace {
  const cur = ws.panes[pane - 1];
  const next = tabs && tabs.paths.length ? parseTabs(tabs) : null;
  if (cur.projectId === projectId && sameTabs(cur.editors ?? null, next)) return ws;
  const spec: PaneSpec = cur.projectId === projectId ? { ...cur } : repinned(cur, projectId);
  if (next) spec.editors = next;
  else delete spec.editors;
  const panes = ws.panes.map((p, i) => (i === pane - 1 ? spec : p)) as Panes;
  return { ...ws, panes };
}

/**
 * The file a Diff pane has open (WP-G2), relative to its task's diff; empty
 * or null forgets it. It is read again when the pane mounts (a reload, a
 * split's copy). Too long a path is not kept. A File pane's files are its
 * tabs (`setEditors`, ED-2): a stored `filePath` on one is read as one tab.
 */
export function setFilePath(ws: Workspace, pane: PaneNo, path: string | null): Workspace {
  const cur = ws.panes[pane - 1].filePath ?? "";
  const next = path && path.length <= PATH_MAX ? path : "";
  if (cur === next) return ws;
  const panes = ws.panes.map((p, i) => {
    if (i !== pane - 1) return p;
    const spec = { ...p };
    if (next) spec.filePath = next;
    else delete spec.filePath;
    return spec;
  }) as Panes;
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
  return { ...ws, panes: ws.panes.map((p, i) => (hit(p, i) ? repinned(p, null) : p)) as Panes };
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

/**
 * A terminal continued after a restart under a new id (components/Terminal.tsx,
 * `continueHere`): every pane holding the old one holds the new one, shown or not.
 */
export function renameTerminal(ws: Workspace, from: string, to: string): Workspace {
  if (from === to || !ws.panes.some((p) => p.terminalId === from)) return ws;
  return { ...ws, panes: ws.panes.map((p) => (p.terminalId === from ? { ...p, terminalId: to } : p)) as Panes };
}

/** Storage shape only; URLs and grants face the live port judge before loading. */
export function parseBrowserTabs(raw: unknown): BrowserTabs | null {
  if (!isObj(raw) || !Array.isArray(raw.tabs)) return null;
  const tabs: BrowserTabSpec[] = [];
  const ids = new Set<string>();
  for (const v of raw.tabs) {
    if (tabs.length >= BROWSER_TABS_MAX) break;
    if (!isObj(v) || typeof v.id !== "string" || !/^[a-z0-9]{6}$/.test(v.id) || ids.has(v.id)
        || (v.mode !== undefined && v.mode !== "direct" && v.mode !== "remote")) continue;
    const url = v.url === undefined && v.mode === "remote" ? "" : v.url;
    if (typeof url !== "string" || url.length > URL_MAX) continue;
    const tab: BrowserTabSpec = { id: v.id, url };
    if (v.mode === "remote") {
      tab.mode = "remote";
      if (typeof v.remoteTabId === "string" && /^[0-9a-f]{8}$/.test(v.remoteTabId)) tab.remoteTabId = v.remoteTabId;
      if (typeof v.title === "string") tab.title = cleanTitle(v.title);
    }
    const keep = tab.mode !== "remote" ? storableKeepOrigin(v.keepOrigin, url) : null;
    if (keep) tab.keepOrigin = keep;
    if (isObj(v.auto) && ["thread", "task", "terminal"].includes(String(v.auto.kind))
        && typeof v.auto.label === "string") {
      tab.auto = { kind: v.auto.kind as NonNullable<BrowserTabSpec["auto"]>["kind"],
                   label: cleanTitle(v.auto.label, 60) };
    }
    if ("source" in v || "auto" in v) tab.source = parseBrowserSource(v.source);
    ids.add(tab.id);
    tabs.push(tab);
  }
  if (raw.tabs.length && !tabs.length) return null;
  return { tabs, active: typeof raw.active === "string" && ids.has(raw.active) ? raw.active : tabs[0]?.id ?? "" };
}

function mirrorBrowser(spec: PaneSpec, browser: BrowserTabs): PaneSpec {
  const next = { ...spec, browser };
  const tab = browser.tabs.find((t) => t.id === browser.active);
  delete next.browserUrl;
  delete next.keepOrigin;
  if (tab?.url) next.browserUrl = tab.url;
  if (tab?.keepOrigin) next.keepOrigin = tab.keepOrigin;
  return next;
}

function changeBrowser(ws: Workspace, pane: PaneNo, browser: BrowserTabs): Workspace {
  return { ...ws, panes: ws.panes.map((p, i) => i === pane - 1 ? mirrorBrowser(p, browser) : p) as Panes };
}

export function addBrowserTab(ws: Workspace, pane: PaneNo, url: string, opts: BrowserTabOptions = {}): Workspace {
  const browser = ws.panes[pane - 1].browser ?? { tabs: [], active: "" };
  if (browser.tabs.length >= BROWSER_TABS_MAX || url.length > URL_MAX) return ws;
  const used = browser.tabs.map((t) => t.id);
  const id = opts.id && /^[a-z0-9]{6}$/.test(opts.id) && !used.includes(opts.id) ? opts.id : browserTabId(used);
  const tab: BrowserTabSpec = { id, url };
  if (opts.mode === "remote" || ws.panes[pane - 1].browserProfile === "private") tab.mode = "remote";
  if (opts.auto) tab.auto = { ...opts.auto, label: cleanTitle(opts.auto.label, 60) };
  if ("source" in opts || opts.auto) tab.source = parseBrowserSource(opts.source);
  return changeBrowser(ws, pane, { tabs: [...browser.tabs, tab], active: opts.activate === false && browser.active ? browser.active : id });
}

export function closeBrowserTab(ws: Workspace, pane: PaneNo, id: string): Workspace {
  const browser = ws.panes[pane - 1].browser;
  if (!browser || !browser.tabs.some((t) => t.id === id)) return ws;
  const at = browser.tabs.findIndex((t) => t.id === id);
  const tabs = browser.tabs.filter((t) => t.id !== id);
  return changeBrowser(ws, pane, { tabs, active: browser.active === id ? tabs[Math.min(at, tabs.length - 1)]?.id ?? "" : browser.active });
}

export function activateBrowserTab(ws: Workspace, pane: PaneNo, id: string): Workspace {
  const browser = ws.panes[pane - 1].browser;
  return !browser || browser.active === id || !browser.tabs.some((t) => t.id === id) ? ws
    : changeBrowser(ws, pane, { ...browser, active: id });
}

/** An explicit id also addresses a dormant tab without altering the active one. */
export function setBrowserUrl(ws: Workspace, pane: PaneNo, url: string, id?: string): Workspace {
  const browser = ws.panes[pane - 1].browser;
  if (url.length > URL_MAX) return ws;
  if (!browser?.tabs.length) return url ? addBrowserTab(ws, pane, url, { id }) : ws;
  const target = id ?? browser.active;
  const tab = browser.tabs.find((t) => t.id === target);
  if (!tab || tab.url === url) return ws;
  const next = { ...tab, url };
  if (next.keepOrigin && originOf(url) !== next.keepOrigin) delete next.keepOrigin;
  return changeBrowser(ws, pane, { ...browser, tabs: browser.tabs.map((t) => t.id === target ? next : t) });
}

/**
 * Only the owner's address-field Enter/Load action calls this, after URL
 * judging. Passive navigation, reload, project-root and programmatic URL
 * updates use setBrowserUrl and preserve provenance. Same-URL submission is
 * still an explicit disclosure. Drop the stale auto label too, so a later
 * storage parse cannot mistake this owner-entered tab for a legacy auto tab.
 */
export function submitBrowserUrl(ws: Workspace, pane: PaneNo, url: string, id?: string): Workspace {
  if (url.length > URL_MAX) return ws;
  const updated = setBrowserUrl(ws, pane, url, id);
  const browser = updated.panes[pane - 1].browser;
  const target = id ?? browser?.active;
  const tab = browser?.tabs.find((t) => t.id === target);
  if (!browser || !tab || (!tab.source && !tab.auto)) return updated;
  const next = { ...tab };
  delete next.source;
  delete next.auto;
  return changeBrowser(updated, pane, { ...browser, tabs: browser.tabs.map((t) => t.id === target ? next : t) });
}

/** Host-acknowledged runtime metadata. It never clears provenance or see. */
export function setBrowserRemote(ws: Workspace, pane: PaneNo, id: string,
  value: { mode: "direct" | "remote"; remoteTabId?: string; url?: string; title?: string; ctx?: string }): Workspace {
  const spec = ws.panes[pane - 1];
  const browser = spec.browser;
  const tab = browser?.tabs.find((t) => t.id === id);
  if (!browser || !tab || (spec.browserProfile === "private" && value.mode !== "remote")) return ws;
  if (value.remoteTabId && !/^[0-9a-f]{8}$/.test(value.remoteTabId)) return ws;
  if (value.ctx && (!/^[0-9a-f]{8}$/.test(value.ctx) || spec.browserProfile !== "private")) return ws;
  if (value.url !== undefined && value.url.length > URL_MAX) return ws;
  const next = { ...tab, mode: value.mode };
  delete next.remoteTabId;
  delete next.title;
  if (value.mode === "remote") {
    delete next.keepOrigin;
    if (value.remoteTabId) next.remoteTabId = value.remoteTabId;
    if (value.title) next.title = cleanTitle(value.title);
  }
  if (value.url !== undefined) next.url = value.url;
  let updated = changeBrowser(ws, pane, { ...browser, tabs: browser.tabs.map((t) => t.id === id ? next : t) });
  if (value.ctx) updated = { ...updated, panes: updated.panes.map((p, i) => {
    if (i !== pane - 1) return p;
    const live = { ...p, browserCtx: value.ctx }; delete live.browserPrivateEnded; return live;
  }) as Panes };
  return updated;
}

export function setBrowserProfile(ws: Workspace, pane: PaneNo, profile: string, initialOnly = false): Workspace {
  if (profile !== "private" && !/^p-[0-9a-f]{8}$/.test(profile)) return ws;
  const spec = ws.panes[pane - 1];
  // Discovering the default is metadata, not an owner profile conversion.
  // Keep existing handles until ensure() validates them against host metadata.
  if (initialOnly) {
    if (spec.browserProfile || profile === "private") return ws;
    return { ...ws, panes: ws.panes.map((p, i) => i === pane - 1 ? { ...p, browserProfile: profile } : p) as Panes };
  }
  if (spec.browserProfile === profile && !spec.browserPrivateEnded) return ws;
  const next = { ...spec, browserProfile: profile };
  delete next.browserCtx;
  delete next.browserPrivateEnded;
  if (next.browser) next.browser = { ...next.browser, tabs: next.browser.tabs.map((t) => {
    const tab = { ...t };
    delete tab.remoteTabId;
    delete tab.title;
    if (profile === "private") { tab.mode = "remote"; delete tab.keepOrigin; }
    return tab;
  }) };
  const mirrored = next.browser ? mirrorBrowser(next, next.browser) : next;
  return { ...ws, panes: ws.panes.map((p, i) => i === pane - 1 ? mirrored : p) as Panes };
}

/** Call only after the host acknowledges the pane permission mutation. */
export function setBrowserSee(ws: Workspace, pane: PaneNo, see: boolean): Workspace {
  const spec = ws.panes[pane - 1];
  if ((spec.see !== false) === see) return ws;
  const next = { ...spec };
  if (see) delete next.see; else { next.see = false; next.drive = false; }
  return { ...ws, panes: ws.panes.map((p, i) => i === pane - 1 ? next : p) as Panes };
}

/** Named-profile preference only. Private consent belongs to the live controller. */
export function setBrowserDrive(ws: Workspace, pane: PaneNo, drive: boolean): Workspace {
  const spec = ws.panes[pane - 1];
  if (spec.browserProfile === "private" || (drive && spec.see === false)) return ws;
  const next = { ...spec };
  if (drive) delete next.drive; else next.drive = false;
  return { ...ws, panes: ws.panes.map((p, i) => i === pane - 1 ? next : p) as Panes };
}

export function setTabKeepOrigin(ws: Workspace, pane: PaneNo, id: string, origin: string | null): Workspace {
  const browser = ws.panes[pane - 1].browser;
  const tab = browser?.tabs.find((t) => t.id === id);
  if (!browser || !tab || tab.mode === "remote") return ws;
  const keep = origin === null ? null : storableKeepOrigin(origin, tab.url);
  if (origin !== null && !keep || (tab.keepOrigin ?? null) === keep) return ws;
  const next = { ...tab };
  if (keep) next.keepOrigin = keep;
  else delete next.keepOrigin;
  return changeBrowser(ws, pane, { ...browser, tabs: browser.tabs.map((t) => t.id === id ? next : t) });
}

export function setKeepOrigin(ws: Workspace, pane: PaneNo, origin: string | null): Workspace {
  return setTabKeepOrigin(ws, pane, ws.panes[pane - 1].browser?.active ?? "", origin);
}

export function setPanel(ws: Workspace, panel: Partial<Workspace["panel"]>): Workspace {
  const open = panel.open ?? ws.panel.open;
  const height = panel.height === undefined ? ws.panel.height : clampPanelHeight(panel.height);
  if (open === ws.panel.open && height === ws.panel.height) return ws;
  return { ...ws, panel: { open, height } };
}

/**
 * Reset layout (§2.1): one pane (the first in reading order; ⊞ keeps the
 * rest), the panel closed. What each pane showed is kept.
 */
export function resetWorkspace(ws: Workspace): Workspace {
  return { ...setPreset(ws, "single"), panel: defaultWorkspace().panel };
}

// ---- split and close (WP-G2; directional since FS4) --------------------------------
//
// A split (plan §5.1) halves one pane, right ("x") or down ("y"), the copy
// right after its source in reading order; nothing else moves (FS-5, §5.3).
// × (§5.2) removes exactly that pane and gives its space to its neighbour in
// the same split. ⊞ hides panes (`kept`, specs kept to come back); × discards
// one (its spec back to the default), and the window mounts that id fresh if a
// later split reuses it (the incarnation in useLayout).

/**
 * The id a split takes (plan §3.4): the lowest id neither drawn nor hidden;
 * else the oldest hidden (`kept`) pane that holds nothing; else the oldest
 * one whose only content can be kept elsewhere (`holding`: a compose row's
 * unsent words, which the caller parks for the next new thread — review of
 * PR #47); else none — every id is drawn, or hidden with an unsaved edit
 * (`dirty`, by id) that a split must never throw away. The caller closes
 * the hidden pane it takes first.
 */
export function allocate(
  ws: Pick<Workspace, "tree" | "kept">, dirty: readonly PaneNo[] = [], holding: readonly PaneNo[] = [],
): PaneNo | null {
  const order = orderOf(ws);
  const free = PANE_NOS.find((n) => !order.includes(n) && !ws.kept.includes(n));
  if (free !== undefined) return free;
  // `kept` is most recently hidden first: the oldest is the last.
  const oldest = (ok: (n: PaneNo) => boolean) => [...ws.kept].reverse().find(ok) ?? null;
  return oldest((n) => !dirty.includes(n) && !holding.includes(n)) ?? oldest((n) => !dirty.includes(n));
}

/**
 * Pane `id` shows `spec`, beside `source`: right of it (`axis` "x") or below
 * it ("y"), right after it in reading order (§5.1). Only `source` is halved:
 * first every node on the root → source path takes the sizes `fit` drew it
 * with (C3, so a node the window clamps keeps its other children where they
 * are), then the source's leaf becomes two (`insertBeside`: a sibling in a
 * parent of the same axis, else a new node). It takes focus (G-4) unless
 * `focus` is false — a split the owner did not ask for (a dev server the
 * window opens itself) never moves where they are working. `id` leaves
 * `kept` if ⊞ had hidden it.
 *
 * Nothing changes when the split is not allowed (`splitVerdict`), for an id
 * already drawn or not an id at all, for a source that is not drawn, or for a
 * fit of another tree (a stale one: its sizes are not this tree's).
 */
export function splitPane(
  ws: Workspace, fit: WorkspaceFit, source: PaneNo, axis: Axis, id: PaneNo, spec: PaneSpec,
  opts: { focus?: boolean } = {},
): Workspace {
  const order = orderOf(ws);
  if (!isPaneNo(id) || order.includes(id) || !order.includes(source) || (axis !== "x" && axis !== "y")) return ws;
  if (shapeKey(fit.render) !== shapeKey(ws.tree)) return ws;
  if (!splitVerdict(ws, fit, source, axis).ok) return ws;
  // C3: the path's nodes as drawn. With nothing dropped the render has the
  // stored tree's structure, so a path names the same node in both.
  let tree = ws.tree;
  const path = pathOf(ws.tree, source) ?? [];
  for (let k = 0; k < path.length; k++) {
    const drawn = nodeAt(fit.render, path.slice(0, k));
    if (drawn && !isLeaf(drawn)) tree = withSizes(tree, path.slice(0, k), drawn.sizes);
  }
  tree = insertBeside(tree, source, axis, id);
  const panes = ws.panes.map((p, i) => (i === id - 1 ? { ...spec } : p)) as Panes;
  return {
    ...ws,
    tree,
    kept: ws.kept.filter((n) => n !== id),
    panes,
    focused: opts.focus === false && order.includes(ws.focused) ? ws.focused : id,
  };
}

/**
 * × on pane `id` (§5.2): it leaves the tree and its space goes to its
 * neighbour in the same split — the one before it (left or above), else the
 * one after (`removeLeaf`); every other pane keeps its place. Its spec goes
 * back to the default; `kept` is untouched. Focus, when it was on the closed
 * pane, goes to `then` when that is still drawn — the window passes the chat
 * selected before the closed one, so the owner's last choice keeps the
 * microphone (decisions W-6) — else to the pane that took the space (its leaf
 * nearest the closed pane). The last pane is never closed, and neither is
 * one that is not drawn.
 */
export function closePane(ws: Workspace, id: PaneNo, then: PaneNo | null = null): Workspace {
  const order = orderOf(ws);
  if (!order.includes(id) || order.length <= 1) return ws;
  const removed = removeLeaf(ws.tree, id);
  if (!removed) return ws;
  const next = leaves(removed.tree);
  const panes = ws.panes.map((p, i) => (i === id - 1 ? defaultPane(id) : p)) as Panes;
  const focused = ws.focused !== id && next.includes(ws.focused) ? ws.focused
    : then !== null && next.includes(then) ? then : removed.heir;
  return { ...ws, tree: removed.tree, kept: ws.kept.filter((n) => n !== id), panes, focused };
}

/**
 * What a split shows in the new pane (decisions S-2, plan §3.5): a copy of
 * the pane split from. A file keeps its project and its tabs — on the same
 * buffers (ED-2) — and a diff its project and the open file;
 * a Browser its URL and project — never the keep-origin grant, which is the
 * owner's choice for one pane (WP-E); a task view becomes that task's diff
 * (S-7); a chat or a terminal pane starts empty here and the window fills it
 * (a new compose row in the source's project, a new terminal in the folder
 * the source was opened in).
 */
export function copySpec(spec: PaneSpec): PaneSpec {
  const out: PaneSpec = { view: spec.view, projectId: null, terminalId: null };
  switch (spec.view) {
    case "file":
      // Its tabs, on the same buffers (ED-2): the copy shares each file's
      // text, dirty dot and undo stack, and the preview tab is pinned there.
      out.projectId = spec.projectId;
      if (spec.editors) out.editors = { ...spec.editors, paths: [...spec.editors.paths], preview: null };
      break;
    case "diff":
      out.projectId = spec.projectId;
      if (spec.filePath) out.filePath = spec.filePath;
      break;
    case "browser":
      out.projectId = spec.projectId;
      if (spec.browserProfile) out.browserProfile = spec.browserProfile;
      if (spec.see === false) out.see = false;
      if (spec.drive === false) out.drive = false;
      if (spec.browser) {
        const ids = spec.browser.tabs.map((t) => t.id);
        const tabs = spec.browser.tabs.map((t) => {
          const id = browserTabId(ids);
          ids.push(id);
          return { id, url: t.url,
            ...(t.mode === "remote" || spec.browserProfile === "private" ? { mode: "remote" as const } : {}),
            ...(t.source || t.auto ? { source: parseBrowserSource(t.source) } : {}) };
        });
        const active = tabs[spec.browser.tabs.findIndex((t) => t.id === spec.browser!.active)]?.id ?? tabs[0]?.id ?? "";
        return mirrorBrowser(out, { tabs, active });
      }
      if (spec.browserUrl) {
        const id = browserTabId();
        return mirrorBrowser(out, { tabs: [{ id, url: spec.browserUrl }], active: id });
      }
      break;
    case "vex":
      out.projectId = spec.projectId;
      break;
    case "task":
      out.view = "diff";
      break;
    case "subagent":
      // One sub-agent per pane: a copy is a new chat, as a chat's copy is.
      out.view = "chat";
      break;
    default:
      break;
  }
  return out;
}

/** Only an untouched default spec is safe for an unsolicited split to reclaim. */
export function blankKeptPane(ws: Workspace, id: PaneNo): boolean {
  const spec = ws.panes[id - 1];
  const def = defaultPane(id);
  return ws.kept.includes(id) && spec.view === def.view && spec.projectId === null && spec.terminalId === null
    && spec.browser === undefined && spec.browserUrl === undefined && spec.keepOrigin === undefined
    && spec.filePath === undefined && spec.editors === undefined
    && Object.keys(spec).every(key => key in def);
}

/** Automatic placement never falls through to allocate's owner-only destructive tiers. */
export function freeId(ws: Workspace, held: readonly PaneNo[] = []): PaneNo | null {
  const free = PANE_NOS.find(id => !inWorkspace(ws, id) && !held.includes(id));
  if (free !== undefined) return free;
  return [...ws.kept].reverse().find(id => !held.includes(id) && blankKeptPane(ws, id)) ?? null;
}

/**
 * Where a split the owner did not ask for goes (a dev server the window opens
 * itself, plan §5.1): right of `source` when that split is allowed, else
 * below it, else nowhere (null) — never folding a side pane or the panel,
 * never dropping a pane (a split never does, FS-5), and never taking an id
 * that holds something: a free id, else a blank hidden one (`freeId`, never
 * `allocate`'s destructive tiers; `held` ids skipped).
 */
export function autoSplit(
  ws: Workspace, fit: WorkspaceFit, source: PaneNo, held: readonly PaneNo[] = [],
): { axis: Axis; id: PaneNo } | null {
  const id = freeId(ws, held);
  if (id === null) return null;
  for (const axis of ["x", "y"] as const) {
    if (splitVerdict(ws, fit, source, axis).ok) return { axis, id };
  }
  return null;
}

/**
 * Why a split is refused, first reason wins (§5.1): the window is dropping
 * panes (P-6); a fifth pane across or a fourth down (FS-3); halves under the
 * minimum pane (FS-4); all twelve ids drawn (FS-1); every free id a hidden
 * pane with an unsaved edit.
 */
export type SplitWhy = "" | "dropped" | "across" | "down" | "room" | "max" | "hidden-edit";

/**
 * Whether pane `source` can be split along `axis` in the window `fit` drew
 * (plan §4, §5.1), per pane and per direction:
 *  - **dropped**: the window is already leaving panes out (P-6): a new pane
 *    would only add drops, so its result could not be seen.
 *  - **across / down** (FS-3, §4.1): counted over the drawn rects of every
 *    pane, the whole window. A split right adds one pane to every horizontal
 *    line through the source and changes no other line, so it is allowed
 *    while every such line crosses at most three; a split down, while every
 *    vertical line through it crosses at most two.
 *  - **room** (FS-4, §4.5): each half at least 360 wide (right) or 200 tall
 *    (down), at the source's drawn px. Never by borrowing from the side
 *    panes (FS-7). With no measured window it is skipped.
 *  - **max**: all twelve ids are drawn (FS-1).
 *  - **hidden-edit**: the only ids left are hidden panes with an unsaved
 *    edit (`dirty`), which a split must never throw away.
 */
export function splitVerdict(
  ws: Workspace, fit: WorkspaceFit, source: PaneNo, axis: Axis, dirty: readonly PaneNo[] = [],
): { ok: boolean; why: SplitWhy } {
  if (fit.dropped > 0) return { ok: false, why: "dropped" };
  const box = fit.boxes[source];
  if (!box) return { ok: false, why: "dropped" };
  const rects: Partial<Record<PaneNo, Rect>> = {};
  for (const n of fit.panes) if (fit.boxes[n]) rects[n] = fit.boxes[n]!.rect;
  const r = box.rect;
  if (axis === "x" ? crossing(rects, "x", r.top, r.top + r.height) > MAX_ACROSS - 1
    : crossing(rects, "y", r.left, r.left + r.width) > MAX_DOWN - 1) {
    return { ok: false, why: axis === "x" ? "across" : "down" };
  }
  const half = (axis === "x" ? box.px.width : box.px.height) / 2;
  if (Number.isFinite(half) && half < (axis === "x" ? PANE_MIN_W : PANE_MIN_H)) return { ok: false, why: "room" };
  if (orderOf(ws).length >= PANE_NOS.length) return { ok: false, why: "max" };
  // Words a hidden compose row holds are parked, never lost: only an unsaved edit blocks.
  if (allocate(ws, dirty) === null) return { ok: false, why: "hidden-edit" };
  return { ok: true, why: "" };
}

/**
 * A split button's title (§10): what it does, or why it cannot. `drawn` and
 * `total` are the panes the window draws and the panes the layout has (the
 * "dropped" reason names both).
 */
export function splitTitle(why: SplitWhy, axis: Axis, drawn = 0, total = 0): string {
  const right = axis === "x";
  switch (why) {
    case "dropped":
      return `Showing ${drawn} of ${total} panes: widen the window or close a pane to split`;
    case "across":
      return "Four panes across the window is the most";
    case "down":
      return "Three panes down the window is the most";
    case "room":
      return right
        ? "Not enough room to split right: widen the window, fold a side pane or zoom out (Ctrl+-)"
        : "Not enough room to split down: make the window taller, close the panel or zoom out (Ctrl+-)";
    case "max":
      return "Twelve panes is the most";
    case "hidden-edit":
      return "A hidden pane has an unsaved edit: show it from ⊞ first";
    default:
      return right ? "Split right: a copy of this pane beside it" : "Split down: a copy of this pane below it";
  }
}

// ---- the tree, read --------------------------------------------------------------

function nodeAt(n: LayoutNode, path: Path): LayoutNode | null {
  let at: LayoutNode = n;
  for (const i of path) {
    if (isLeaf(at) || i < 0 || i >= at.children.length) return null;
    at = at.children[i];
  }
  return at;
}

const firstLeaf = (n: LayoutNode): PaneNo => (isLeaf(n) ? n.pane : firstLeaf(n.children[0]));
const lastLeaf = (n: LayoutNode): PaneNo => (isLeaf(n) ? n.pane : lastLeaf(n.children[n.children.length - 1]));

/** Structure only: no ids, no sizes. */
const structure = (n: LayoutNode): string => (isLeaf(n) ? "." : `${n.axis}[${n.children.map(structure).join(",")}]`);

/** A child's least size along `axis` (§4.4): 360 per pane across, 200 per pane down. */
const minAlong = (n: LayoutNode, axis: Axis) => (axis === "x" ? acrossS(n) * PANE_MIN_W : downS(n) * PANE_MIN_H);

/** Where `n`'s drawn panes lie along `axis`, in px of `length` (null when none of them is drawn). */
function extent(n: LayoutNode, rects: Partial<Record<PaneNo, Rect>>, axis: Axis, length: number): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of leaves(n)) {
    const r = rects[p];
    if (!r) continue;
    const a = axis === "x" ? r.left : r.top;
    lo = Math.min(lo, a);
    hi = Math.max(hi, a + (axis === "x" ? r.width : r.height));
  }
  return lo <= hi ? [lo * length, hi * length] : null;
}

// ---- edges (§9) --------------------------------------------------------------------

/**
 * One separator between panes (§9), from the fit. `members` are the stored
 * node boundaries it moves — between children i − 1 and i of the node at
 * `path` in `ws.tree` — several when collinear boundaries of one node's
 * children are linked (P-5: 2×2's column edge through both rows). Its test id
 * is `split-<axis><the first member's path digits>-<i>`. `value`, `min` and
 * `max` are px from the pane area's left ("x") or top ("y"); min and max keep
 * every child beside every member at least its subtree minimum, and stop it
 * where some line would cross a fifth pane across or a fourth down (FS-3,
 * §4.3; `capStop`). Plain data: the page may hold it.
 */
export interface EdgeHandle {
  testid: string;
  /** The way it moves: "x" is a vertical separator. */
  axis: Axis;
  members: { path: Path; i: number }[];
  /** Where it is drawn, in unit fractions: zero wide (x) or zero tall (y). */
  at: Rect;
  value: number;
  min: number;
  max: number;
}

/**
 * The stored boundary a drawn one is. Pruning drops children and flattens
 * nodes but never reorders, so the drawn children either side meet where the
 * stored paths of their nearest leaves part: that node, and the child index
 * the second leaf's branch has there.
 */
function storedBoundary(tree: LayoutNode, render: LayoutNode, path: Path, i: number): { path: number[]; i: number } | null {
  const r = nodeAt(render, path);
  if (!r || isLeaf(r) || i < 1 || i >= r.children.length) return null;
  const a = pathOf(tree, lastLeaf(r.children[i - 1]));
  const b = pathOf(tree, firstLeaf(r.children[i]));
  if (!a || !b) return null;
  let k = 0;
  while (k < a.length && k < b.length && a[k] === b[k]) k++;
  return k < b.length ? { path: b.slice(0, k), i: b[k] } : null;
}

/**
 * One member boundary of an edge being dragged, in px along its axis: the
 * panes before it (from `from` to `at`) and after it (from `at` to `to`).
 */
interface Moving {
  before: PaneNo[];
  after: PaneNo[];
  from: number;
  at: number;
  to: number;
}

/** A coordinate along the drag's axis as the edge moves to p: `a + b·p` px. */
interface Lin { a: number; b: number }

/**
 * Where a drag of an edge must stop for FS-3 (§4.3), as px bounds on its
 * position. A drag rescales the two children beside each member boundary
 * (moveEdge), so every coordinate inside them moves linearly with the edge's
 * position p and every other pane stays put; the most panes a line crosses
 * changes only where a moving pane edge meets another pane edge. The edge
 * stops at the first such meeting, each way from where it is, past which some
 * line would cross more than the cap — or more than it already does, for a
 * stored tree already over the cap (drags never make it worse).
 */
function capStop(
  rects: Partial<Record<PaneNo, Rect>>, axis: Axis, moving: readonly Moving[], L: number,
  value: number, min: number, max: number,
): { min: number; max: number } {
  const lines: Axis = axis === "y" ? "x" : "y";
  const cap = axis === "y" ? MAX_ACROSS : MAX_DOWN;
  const span = (r: Rect): [number, number] =>
    axis === "x" ? [r.left * L, (r.left + r.width) * L] : [r.top * L, (r.top + r.height) * L];
  const ends = new Map<PaneNo, [Lin, Lin]>();
  for (const n of PANE_NOS) {
    const r = rects[n];
    if (!r) continue;
    const [s, t] = span(r);
    ends.set(n, [{ a: s, b: 0 }, { a: t, b: 0 }]);
  }
  for (const m of moving) {
    // Before the boundary: [from, at] scaled to [from, p]. After it: [at, to] to [p, to].
    const scaleBefore = (x: number): Lin => {
      const k = m.at > m.from ? (x - m.from) / (m.at - m.from) : 1;
      return { a: m.from * (1 - k), b: k };
    };
    const scaleAfter = (x: number): Lin => {
      const k = m.to > m.at ? (x - m.at) / (m.to - m.at) : 0;
      return { a: k * m.to, b: 1 - k };
    };
    for (const n of m.before) if (rects[n]) ends.set(n, span(rects[n]!).map(scaleBefore) as [Lin, Lin]);
    for (const n of m.after) if (rects[n]) ends.set(n, span(rects[n]!).map(scaleAfter) as [Lin, Lin]);
  }
  const at = (p: number) => {
    const out: Partial<Record<PaneNo, Rect>> = {};
    for (const [n, [s, t]] of ends) {
      const r = rects[n]!;
      const lo = (s.a + s.b * p) / L;
      const hi = (t.a + t.b * p) / L;
      out[n] = axis === "x" ? { ...r, left: lo, width: hi - lo } : { ...r, top: lo, height: hi - lo };
    }
    return crossing(out, lines, 0, 1);
  };
  const limit = Math.max(cap, at(value));
  // Where two pane edges meet as p moves: one of them moving, at a different rate.
  const coords = [...ends.values()].flat();
  const events: number[] = [];
  for (let i = 0; i < coords.length; i++) {
    for (let j = i + 1; j < coords.length; j++) {
      const db = coords[i].b - coords[j].b;
      if (Math.abs(db) < 1e-12) continue;
      const p = (coords[j].a - coords[i].a) / db;
      if (Number.isFinite(p) && p >= min && p <= max) events.push(p);
    }
  }
  const walk = (stops: number[], end: number): number => {
    let prev = value;
    for (const e of [...stops, end]) {
      if (Math.abs(e - prev) <= 1e-9) continue;
      if (at((prev + e) / 2) > limit) return prev;
      prev = e;
    }
    return end;
  };
  const up = walk(events.filter((p) => p > value).sort((a, b) => a - b), max);
  const down = walk(events.filter((p) => p < value).sort((a, b) => b - a), min);
  return { min: down, max: up };
}

/** Every separator of the fitted render, named and moved by the stored nodes its boundaries belong to. */
function edgeHandles(tree: LayoutNode, render: LayoutNode, width: number, height: number): EdgeHandle[] {
  const rects = rectsOf(render);
  const out: EdgeHandle[] = [];
  for (const e of edgesOf(render)) {
    const length = e.axis === "x" ? width : height;
    const L = Number.isFinite(length) ? length : 0;
    const members: { path: number[]; i: number }[] = [];
    const moving: Moving[] = [];
    let value = NaN;
    let min = -Infinity;
    let max = Infinity;
    for (const m of e.members) {
      const stored = storedBoundary(tree, render, m.path, m.i);
      const r = nodeAt(render, m.path);
      if (!stored || !r || isLeaf(r)) continue;
      const before = extent(r.children[m.i - 1], rects, e.axis, L);
      const after = extent(r.children[m.i], rects, e.axis, L);
      if (!before || !after) continue;
      members.push(stored);
      moving.push({ before: leaves(r.children[m.i - 1]), after: leaves(r.children[m.i]),
                    from: before[0], at: after[0], to: after[1] });
      if (Number.isNaN(value)) value = after[0];
      min = Math.max(min, before[0] + minAlong(r.children[m.i - 1], e.axis));
      max = Math.min(max, after[1] - minAlong(r.children[m.i], e.axis));
    }
    if (!members.length) continue;
    // The FS-3 stop (§4.3): never past where some line would cross a fifth
    // pane across (a row edge moving) or a fourth down (a column edge).
    // Geometric counts never exceed the structural ones, so a render whose
    // structure stays within the cap needs no search.
    const capped = e.axis === "y" ? acrossS(render) > MAX_ACROSS : downS(render) > MAX_DOWN;
    if (capped && L > 0 && max > min) {
      const stop = capStop(rects, e.axis, moving, L, value, min, max);
      min = Math.max(min, stop.min);
      max = Math.min(max, stop.max);
    }
    out.push({
      testid: `split-${e.axis}${members[0].path.join("")}-${members[0].i}`,
      axis: e.axis, members, at: e.at, value, min, max: Math.max(min, max),
    });
  }
  return out;
}

/** Sizes this close to a node's own are its own, value for value. */
const SAME_SIZE = 1e-9;

/**
 * Drag an edge (§9): every member boundary to `px` (clamped to the handle's
 * range), starting from the fitted sizes as today's drag started from the
 * fitted columns. Only the two children beside a member boundary move; the
 * node's other drawn children keep what they were drawn with (their stored
 * size, value for value, when the window did not clamp them), and children a
 * dropped render left out keep their stored sizes, the drawn window's share
 * of the node unchanged. Every other node comes back as it was.
 */
export function moveEdge(
  ws: Workspace, fit: Pick<WorkspaceFit, "boxes" | "width" | "height">, handle: EdgeHandle, px: number,
): Workspace {
  const length = handle.axis === "x" ? fit.width : fit.height;
  if (!Number.isFinite(px) || !Number.isFinite(length) || length <= 0) return ws;
  const to = Math.min(Math.max(handle.min, handle.max), Math.max(handle.min, px));
  const rects: Partial<Record<PaneNo, Rect>> = {};
  for (const n of PANE_NOS) if (fit.boxes[n]) rects[n] = fit.boxes[n]!.rect;
  let tree = ws.tree;
  for (const m of handle.members) {
    const node = nodeAt(tree, m.path);
    if (!node || isLeaf(node) || node.axis !== handle.axis || m.i < 1 || m.i >= node.children.length) continue;
    const ext = node.children.map((c) => extent(c, rects, handle.axis, length));
    const left = ext[m.i - 1];
    const right = ext[m.i];
    if (!left || !right || right[1] - left[0] <= 0) continue;
    const drawn = ext.flatMap((x, k) => (x ? [k] : []));
    const total = drawn.reduce((a, k) => a + (ext[k]![1] - ext[k]![0]), 0);
    const share = drawn.reduce((a, k) => a + node.sizes[k], 0);
    const sizes = node.sizes.slice();
    let rest = share;
    for (const k of drawn) {
      if (k === m.i - 1 || k === m.i) continue;
      const fitted = share * (ext[k]![1] - ext[k]![0]) / total;
      sizes[k] = Math.abs(fitted - node.sizes[k]) <= SAME_SIZE ? node.sizes[k] : fitted;
      rest -= sizes[k];
    }
    const p = Math.min(right[1], Math.max(left[0], to));
    sizes[m.i - 1] = rest * (p - left[0]) / (right[1] - left[0]);
    sizes[m.i] = rest - sizes[m.i - 1];
    if (!(sizes[m.i - 1] > 0 && sizes[m.i] > 0)) continue;
    tree = withSizes(tree, m.path, sizes);
  }
  return tree === ws.tree ? ws : { ...ws, tree };
}

/** Double-click on an edge: each node at `paths` (a linked edge's every member) with equal sizes. */
export function equalize(ws: Workspace, paths: readonly Path[]): Workspace {
  let tree = ws.tree;
  for (const path of paths) {
    const n = nodeAt(tree, path);
    if (!n || isLeaf(n)) continue;
    const k = n.children.length;
    if (n.sizes.every((s) => s === 1 / k)) continue;
    tree = withSizes(tree, path, Array.from({ length: k }, () => 1 / k));
  }
  return tree === ws.tree ? ws : { ...ws, tree };
}

// ---- fitting (§7) ------------------------------------------------------------------

/**
 * What one render drew, handed to the next as `fitWorkspace`'s `previous`
 * (§7.2): the stored tree's shape and the drawn tree's (structure and ids,
 * never sizes), and the panes it drew.
 */
export interface DrawnSet {
  shape: string;
  drawn: string;
  panes: readonly PaneNo[];
}

/** The set `fit` drew, for the next render's `previous`. */
export function drawnSet(ws: Pick<Workspace, "tree">, fit: Pick<WorkspaceFit, "render" | "panes">): DrawnSet {
  return { shape: shapeKey(ws.tree), drawn: shapeKey(fit.render), panes: fit.panes.slice() };
}

/** Where one drawn pane is: unit fractions of the pane area, and its size in px (NaN with no window to fit). */
export interface PaneBox {
  rect: Rect;
  px: { width: number; height: number };
}

/** The drawn tree: the stored one less what the window dropped, its sizes fitted (clamped) to the window. */
export type RenderNode = LayoutNode;

export interface WorkspaceFit {
  /** The side panes, as `fitLayout` draws them for the drawn tree. */
  sides: Fitted;
  render: RenderNode;
  /** The drawn panes: the render's leaves, reading order. */
  panes: PaneNo[];
  /** One per drawn pane (a Record, never a Map: the page reads it through JSON). */
  boxes: Partial<Record<PaneNo, PaneBox>>;
  edges: EdgeHandle[];
  /** Panes the tree has that this render leaves out, and why. */
  dropped: number;
  why: "" | "narrow" | "short";
  /** The centre's width and the panes' area height (NaN with no window to fit). */
  width: number;
  height: number;
  /** The drawn tree's least centre width and pane-area height (§4.4: main2 720). */
  minW: number;
  minH: number;
  /** The preset the drawn tree's structure is, else null (a four-wide row of a dropped 4×2). */
  drawnPreset: Preset | null;
  /** The bottom panel as drawn; `auto` when the window folded it, not the owner. */
  panel: { open: boolean; height: number; auto: boolean };
}

/** §7.3: each node's sizes clamped top down to its own px, every child at least its subtree minimum. */
function clampTree(n: LayoutNode, w: number, h: number): LayoutNode {
  if (isLeaf(n)) return n;
  const sizes = clampSizes(n.sizes, n.children.map((c) => minAlong(c, n.axis)), n.axis === "x" ? w : h);
  return {
    axis: n.axis,
    children: n.children.map((c, i) => clampTree(c, n.axis === "x" ? w * sizes[i] : w, n.axis === "y" ? h * sizes[i] : h)),
    sizes,
  };
}

/**
 * What one render draws (§7). Only the render changes, never what is stored.
 *
 * Across (`available` wide): the side panes give back their slack and fold
 * for the render (`fitLayout`, with the drawn tree's own centre minimum and
 * the owner's `prefer`); only when even both rails leave the centre too
 * narrow are panes dropped, one column of panes at a time (`prune`, never the
 * focused pane).
 *
 * Down (`availableH` tall, the room under the title bar): an open panel
 * shrinks toward its minimum, then folds for the render, and only then are
 * rows of panes dropped. With `preferPanel` (the owner opened a panel the
 * window had folded) rows drop first and the panel is never folded, as
 * `prefer` does for the side panes.
 *
 * **A dropped set is sticky** (`previous`, what the last render drew, §7.2).
 * Which panes survive a drop is chosen from the focused pane, and focus
 * follows every click — so without this, clicking into the *other* drawn pane
 * of three-columns-drawn-as-two redrew a different pair, and the pane under
 * the pointer jumped. The last render's set is kept while the stored tree has
 * the same shape, the drawn tree the same structure (the same room), it
 * still holds the focused pane, and the same drops from some focused pane
 * give exactly it (so a reordered workspace is never drawn from a stale set);
 * focus moving to a pane it does not draw (Ctrl+Alt+N) draws a set that
 * includes it.
 *
 * Then each node is clamped (§7.3) and every drawn pane gets its own box,
 * computed from its own fractions (C5), and every edge its handle (§9). Pure:
 * `previous` is read, never written, and nothing here is stored.
 */
export function fitWorkspace(
  layout: PaneLayout, ws: Workspace, available: number, availableH: number,
  prefer: Side | null = null, preferPanel = false, previous: DrawnSet | null = null,
): WorkspaceFit {
  const order = orderOf(ws);
  const focused = order.includes(ws.focused) ? ws.focused : order[0];
  let render: LayoutNode = ws.tree;
  let why: WorkspaceFit["why"] = "";
  const wide = Number.isFinite(available) && available > 0;
  const tall = Number.isFinite(availableH) && availableH > 0;
  // The drops made, in order, so a sticky set can be checked against them.
  const drops: [Axis, number][] = [];
  const drop = (axis: Axis): boolean => {
    const t = (axis === "x" ? acrossS(render) : downS(render)) - 1;
    if (t < 1) return false;
    render = prune(render, axis, t, focused);
    drops.push([axis, t]);
    return true;
  };

  const across = () => {
    for (;;) {
      const sides = fitLayout(layout, available, prefer, treeMinW(render));
      if (!wide || available - sides.left - sides.right >= treeMinW(render)) return sides;
      if (!drop("x")) return sides;
      why = "narrow";
    }
  };
  let sides = across();

  const before = render;
  let panel = { open: false, height: 0, auto: false };
  if (ws.panel.open && !tall) {
    panel = { open: true, height: ws.panel.height, auto: false };
  } else if (ws.panel.open) {
    for (;;) {
      const room = availableH - treeMinH(render);
      if (room >= PANEL.min) {
        panel = { open: true, height: Math.min(ws.panel.height, room), auto: false };
        break;
      }
      if (preferPanel && drop("y")) {
        why = why || "short";
        continue;
      }
      panel = preferPanel ? { open: true, height: PANEL.min, auto: false } : { open: false, height: 0, auto: true };
      break;
    }
  }
  if (tall) {
    while (treeMinH(render) > availableH - panel.height && drop("y")) why = why || "short";
  }
  // A row dropped can need less width: give the side panes back what it frees.
  if (render !== before) sides = fitLayout(layout, available, prefer, treeMinW(render));
  // Sticky: the same stored shape and the same drawn structure as last
  // render, focus still inside what it drew, and a set these drops can draw.
  if (previous && drops.length && previous.shape === shapeKey(ws.tree) && previous.panes.includes(focused)
      && previous.drawn !== shapeKey(render)) {
    const want = structure(render);
    for (const f of order) {
      let r: LayoutNode = ws.tree;
      for (const [axis, t] of drops) r = prune(r, axis, t, f);
      if (shapeKey(r) === previous.drawn && structure(r) === want) {
        render = r;
        break;
      }
    }
  }

  const width = wide ? available - sides.left - sides.right : NaN;
  const height = tall ? availableH - panel.height : NaN;
  const fitted = clampTree(render, width, height);
  const panes = leaves(fitted);
  const rects = rectsOf(fitted);
  const boxes: Partial<Record<PaneNo, PaneBox>> = {};
  for (const n of panes) {
    const rect = rects[n]!;
    boxes[n] = { rect, px: { width: rect.width * width, height: rect.height * height } };
  }
  return {
    sides, render: fitted, panes, boxes, edges: edgeHandles(ws.tree, fitted, width, height),
    dropped: order.length - panes.length, why,
    width, height, minW: treeMinW(render), minH: treeMinH(render),
    drawnPreset: presetOf(render),
    panel,
  };
}

/** The tallest the panel may be dragged to: whatever leaves the drawn panes their minimum height. */
export function maxPanelHeight(fit: Pick<WorkspaceFit, "minH">, availableH: number): number {
  if (!Number.isFinite(availableH) || availableH <= 0) return PANEL.max;
  return Math.max(PANEL.min, Math.min(PANEL.max, Math.floor(availableH - fit.minH)));
}
