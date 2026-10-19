// FS3: the split tree becomes the workspace's model, and the owner sees no
// change (docs/plans/2026-10-19-hud-free-split-plan.md, the FS3 card with
// §3.2, §3.3, §7, §8 and §9; decisions FS-1..FS-10, FS-8 above all). Written
// before FS3: on FS2's code every block here but "today's ladder" fails,
// because the API it names does not exist yet. "Today's ladder" passes now
// and must pass after FS3 without an edit (it reads the order and the preset
// through `ord`/`pre`, which work in either model).
//
// ---- The contract these tests assume (one list for the implementer) ---------
//
// lib/workspace.ts
//   interface Workspace {
//     tree: LayoutNode;                         // which panes are drawn, their arrangement, their sizes
//     kept: PaneNo[]; panes: Panes; focused: PaneNo; panel: { open: boolean; height: number };
//   }                                           // `preset`, `order` and `splits` are gone (P-1)
//   defaultWorkspace(): its tree is { pane: 1 }
//   orderOf(ws: Pick<Workspace, "tree">): PaneNo[]          leaves(ws.tree), reading order
//   presetOfWs(ws: Pick<Workspace, "tree">): Preset | null  presetOf(ws.tree): structure only, sizes ignored
//   paneSequence / inWorkspace / slotOf / paneAt: as today, over orderOf and kept
//   parseWorkspace(raw: unknown): Workspace
//       `version === 2` with a tree parseTree accepts → the v2 parse, field by field (§8.3);
//       anything else (no version, a bad v2 tree, a version that is not the number 2) →
//       today's v1 parse kept verbatim, then §8.2's table. So a bad v2 tree falls back to
//       its own mirror (`preset`, `order`). `kept`, `focused`, `panel`, `panes` are read
//       as today in both; `panes` is padded to 12.
//   workspaceStorage(ws): the stored value, exactly these keys (no `splits`):
//       { version: 2, tree: storeTree(ws.tree), kept, panes (12, StoredPane as today),
//         focused, panel,
//         preset: presetOfWs(ws) ?? LADDER[min(count, 8)],
//         order?: orderOf(ws)  — only when every id is ≤ 8 and count === SHAPES[preset].panes }
//   saveWorkspace / loadWorkspace: as today, through workspaceStorage / parseWorkspace
//   setPreset(ws, p): tree = presetTree(p, paneSequence(ws).slice(0, n)) at the preset's
//       DEFAULT sizes (FS-8: ⊞ keeps no memory of a custom shape); the rest that was in the
//       workspace goes to `kept`; focus stays if drawn, else the first pane
//   resetWorkspace(ws): setPreset(…, "single"), the panel closed (as today)
//   splitPane(ws, source, id, spec, opts?) and closePane(ws, id, then?): today's signatures
//       and results (the ladder, MAX_DRAWN 8, `toBe(ws)` on a refusal); the new tree is
//       presetTree(LADDER[n ± 1], the new order) at default sizes
//       [FS4 replaced these: workspace.split.test.ts holds the directional split and close,
//       and the ladder tests below were ported or removed there, each named in its header.]
//   fitWorkspace(layout, ws, available, availableH, prefer?, preferPanel?, previous?): WorkspaceFit
//   interface WorkspaceFit {                     // §7.4
//     sides: Fitted; render: RenderNode; panes: PaneNo[];   // panes: the render's leaves, reading order
//     boxes: Partial<Record<PaneNo, PaneBox>>;   // one per drawn pane (a Record, never a Map)
//     edges: EdgeHandle[];
//     dropped: number; why: "" | "narrow" | "short"; width: number; height: number;
//     minW: number; minH: number;                // §4.4 of the render: main2 is 720 (§7.1 (i))
//     drawnPreset: Preset | null;                // presetOf(render)
//     panel: { open: boolean; height: number; auto: boolean };
//   }
//   interface PaneBox { rect: Rect; px: { width: number; height: number } }
//       rect: unit fractions of the pane area (the fitted, clamped sizes); px = rect × width/height
//   interface EdgeHandle {
//     testid: string;                            // §9: split-<axis><stored path digits>-<i>
//     axis: Axis;                                // "x": a vertical separator, moving along x
//     members: { path: Path; i: number }[];      // stored node boundaries it moves (several when linked, P-5)
//     at: Rect;
//     value: number; min: number; max: number;  // px from the pane area's left ("x") or top ("y"),
//   }                                            // as today's Separator reads `c * width`; min/max keep
//                                                // each child ≥ its subtree minimum (360·acrossS, 200·downS)
//   moveEdge(ws, fit, handle: EdgeHandle, px: number): Workspace
//       every member boundary to `px` (clamped to [min, max]), starting from the fitted sizes;
//       only the two children beside each member boundary change; every other node's sizes
//       come back value for value (FS-3's stop is FS4's)
//   equalize(ws, paths: readonly Path[]): Workspace      each node at `paths`: sizes 1/k
//
// lib/splittree.ts
//   presetTree(p: Preset, ids: readonly PaneNo[], edges?: LegacyEdges): LayoutNode
//   type LegacyEdges = { cols: readonly number[]; row: number; bottom?: readonly number[] }  (today's Split)
//       §8.2: an edge list e becomes the sizes [e1, e2 − e1, …, 1 − ek]; cols2/cols3 from cols;
//       rows2 [row, 1 − row]; main2 x from cols, its stack y [row, 1 − row]; grid4/6/8 both
//       rows from cols (linked); grid5/grid7 the top row from cols, the bottom row from
//       bottom; the root y of every grid [row, 1 − row]
//
// Each test names, in a [bracketed] comment, the mutation it is meant to catch
// where the plan names one.

import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT } from "./layout";
import {
  PANEL, PANE_NOS, PRESETS, SHAPES, TITLEBAR_H, WORKSPACE_KEY, allocate, closePane, defaultPane,
  defaultWorkspace, equalize, fitWorkspace, focusPane, inWorkspace, loadWorkspace, moveEdge, orderOf, paneAt,
  paneSequence, parseWorkspace, pinPane, presetOfWs, resetWorkspace, saveWorkspace, setBrowserUrl, setPreset,
  setView, show, slotOf, splitPane, workspaceStorage,
  type EdgeHandle, type PaneNo, type PaneSpec, type Preset, type View, type Workspace, type WorkspaceFit,
} from "./workspace";
import {
  isLeaf, leaves, parseTree, presetOf, presetTree, storeTree, type LayoutNode, type SplitNode, type StoredNode,
} from "./splittree";
import LEGACY_FITS from "./workspace.legacy-fits.json";

const memory = (seed: Record<string, string> = {}) => {
  const m = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    map: m,
  };
};
const room = (w: number, h: number, zoom: number) => [w / (zoom / 100), h / (zoom / 100) - TITLEBAR_H] as const;
const fitAt = (w: Workspace, px: number, py: number, zoom = 100) => {
  const [aw, ah] = room(px, py, zoom);
  return fitWorkspace(DEFAULT_LAYOUT, w, aw, ah);
};
const chat = (): PaneSpec => ({ view: "chat", projectId: null, terminalId: null });
const P = (...ids: number[]) => ids as PaneNo[];
const v1 = (raw: Record<string, unknown>) => parseWorkspace(JSON.stringify(raw));
const v2 = (tree: unknown, rest: Record<string, unknown> = {}) => parseWorkspace(JSON.stringify({ version: 2, tree, ...rest }));
const X = (sizes: number[], ...children: StoredNode[]): StoredNode => ({ axis: "x", children, sizes });
const Y = (sizes: number[], ...children: StoredNode[]): StoredNode => ({ axis: "y", children, sizes });
const thirds = [1 / 3, 1 / 3, 1 / 3];
/** The shape the ladder drew for each count: the v1 mirror still names it for a free tree (§8.1). */
const LADDER_SHAPE: Readonly<Record<number, Preset>> = {
  1: "single", 2: "cols2", 3: "cols3", 4: "grid4", 5: "grid5", 6: "grid6", 7: "grid7", 8: "grid8",
};
/** `w` with this tree (read as stored), everything else as it was. */
const withTree = (w: Workspace, tree: StoredNode): Workspace => ({ ...w, tree: parseTree(tree)! });
const quarters = [0.25, 0.25, 0.25, 0.25];
const halves = [0.5, 0.5];

/** `got` has `want`'s structure and ids exactly, and its sizes within 1e-12. */
function expectTree(got: LayoutNode | undefined, want: StoredNode, what: string) {
  expect(got, `${what}: no tree`).toBeDefined();
  const walk = (g: LayoutNode, w: StoredNode, at: string) => {
    if (typeof w === "number") {
      expect(g, at).toEqual({ pane: w });
      return;
    }
    expect(isLeaf(g), `${at}: a leaf where a node was wanted`).toBe(false);
    const n = g as SplitNode;
    expect([n.axis, n.children.length, n.sizes.length], at).toEqual([w.axis, w.children.length, w.children.length]);
    n.sizes.forEach((s, i) => expect(Math.abs(s - w.sizes[i]), `${at} sizes ${n.sizes} vs ${w.sizes}`).toBeLessThan(1e-12));
    n.children.forEach((c, i) => walk(c, w.children[i], `${at}.${i}`));
  };
  walk(got!, want, what);
}

// ---- the golden fits (§7.1's intended differences, by key) ---------------------

/** What a WP-A/WP-B window stored for its own preset: the same values workspace.test.ts uses. */
const LEGACY_CUSTOM: Record<string, { cols: number[]; row: number }> = {
  single: { cols: [], row: 0.5 },
  cols2: { cols: [0.3], row: 0.5 },
  rows2: { cols: [], row: 0.35 },
  cols3: { cols: [0.2, 0.7], row: 0.5 },
  main2: { cols: [0.7], row: 0.3 },
  grid4: { cols: [0.4], row: 0.6 },
};

/**
 * The golden keys FS3 changes, each with its new value and the §7.1 reason:
 *   (i)  main2's centre minimum is 720, not 840: at 1366×768 the side panes are
 *        no longer squeezed for it, and at 1280×800@150 it is drawn whole on
 *        both rails instead of dropping a column;
 *   (ii) a dropped render keeps the drawn panes' own proportions (renormalised,
 *        then clamped), not the smaller preset's default edges: three columns
 *        drawn as two, main2 drawn as one row or one column, 2×2 drawn as one
 *        row or one column, each from its own stored sizes;
 *   (iii) a four-wide row whose other row is dropped stays four wide: no WP-B
 *        preset has a four-wide row, so no golden key changes for it (it is
 *        pinned below, "§7.1 (iii)").
 * Every other key must draw exactly as main's WP-B code did.
 */
const CHANGED: Record<string, ["(i)" | "(ii)", unknown[]]> = {
  "cols3/1/-/custom/1280x800@150": ["(ii)", ["cols2", "12", 1, "narrow", [0.460751], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "cols3/1/panel/custom/1280x800@150": ["(ii)", ["cols2", "12", 1, "narrow", [0.460751], 0.5, [true, 260, false], [36, 36, true, true, true, true]]],
  "cols3/2/-/custom/1280x800@150": ["(ii)", ["cols2", "12", 1, "narrow", [0.460751], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "cols3/2/panel/custom/1280x800@150": ["(ii)", ["cols2", "12", 1, "narrow", [0.460751], 0.5, [true, 260, false], [36, 36, true, true, true, true]]],
  "cols3/3/-/custom/1280x800@150": ["(ii)", ["cols2", "23", 1, "narrow", [0.539249], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "cols3/3/panel/custom/1280x800@150": ["(ii)", ["cols2", "23", 1, "narrow", [0.539249], 0.5, [true, 260, false], [36, 36, true, true, true, true]]],
  "main2/1/-/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/1/-/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/1/-/-/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.6], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/1/-/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.3, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/1/-/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/1/-/custom/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.656489], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/1/panel/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/1/panel/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/1/panel/-/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.6], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "main2/1/panel/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.416667, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/1/panel/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/1/panel/custom/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.656489], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "main2/2/-/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/2/-/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/2/-/-/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.6], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/2/-/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.3, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/2/-/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/2/-/custom/1024x700@160": ["(ii)", ["rows2", "23", 1, "narrow", [], 0.4884, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/2/-/custom/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.656489], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/2/panel/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/2/panel/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/2/panel/-/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.6], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "main2/2/panel/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.416667, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/2/panel/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/2/panel/custom/1024x700@160": ["(ii)", ["rows2", "23", 1, "narrow", [], 0.4884, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/2/panel/custom/1600x420@100": ["(ii)", ["cols2", "12", 1, "short", [0.656489], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "main2/3/-/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/3/-/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/3/-/-/1600x420@100": ["(ii)", ["cols2", "13", 1, "short", [0.6], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/3/-/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.3, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/3/-/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/3/-/custom/1024x700@160": ["(ii)", ["rows2", "23", 1, "narrow", [], 0.4884, [false, 0, false], [36, 36, true, true, true, true]]],
  "main2/3/-/custom/1600x420@100": ["(ii)", ["cols2", "13", 1, "short", [0.656489], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "main2/3/panel/-/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.5, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/3/panel/-/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.5, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/3/panel/-/1600x420@100": ["(ii)", ["cols2", "13", 1, "short", [0.6], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "main2/3/panel/custom/1366x768@100": ["(i)", ["main2", "123", 0, "", [0.55774], 0.416667, [true, 260, false], [236, 316, false, false, false, false]]],
  "main2/3/panel/custom/1280x800@150": ["(i)", ["main2", "123", 0, "", [0.539249], 0.395778, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/3/panel/custom/1024x700@160": ["(ii)", ["rows2", "23", 1, "narrow", [], 0.4884, [false, 0, true], [36, 36, true, true, true, true]]],
  "main2/3/panel/custom/1600x420@100": ["(ii)", ["cols2", "13", 1, "short", [0.656489], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "grid4/1/-/custom/1024x700@160": ["(ii)", ["rows2", "13", 2, "narrow", [], 0.5116, [false, 0, false], [36, 36, true, true, true, true]]],
  "grid4/1/-/custom/1600x420@100": ["(ii)", ["cols2", "12", 2, "short", [0.4], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "grid4/1/panel/custom/1024x700@160": ["(ii)", ["rows2", "13", 2, "narrow", [], 0.5116, [false, 0, true], [36, 36, true, true, true, true]]],
  "grid4/1/panel/custom/1600x420@100": ["(ii)", ["cols2", "12", 2, "short", [0.4], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "grid4/2/-/custom/1024x700@160": ["(ii)", ["rows2", "24", 2, "narrow", [], 0.5116, [false, 0, false], [36, 36, true, true, true, true]]],
  "grid4/2/-/custom/1600x420@100": ["(ii)", ["cols2", "12", 2, "short", [0.4], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "grid4/2/panel/custom/1024x700@160": ["(ii)", ["rows2", "24", 2, "narrow", [], 0.5116, [false, 0, true], [36, 36, true, true, true, true]]],
  "grid4/2/panel/custom/1600x420@100": ["(ii)", ["cols2", "12", 2, "short", [0.4], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "grid4/3/-/custom/1024x700@160": ["(ii)", ["rows2", "13", 2, "narrow", [], 0.5116, [false, 0, false], [36, 36, true, true, true, true]]],
  "grid4/3/-/custom/1600x420@100": ["(ii)", ["cols2", "34", 2, "short", [0.4], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "grid4/3/panel/custom/1024x700@160": ["(ii)", ["rows2", "13", 2, "narrow", [], 0.5116, [false, 0, true], [36, 36, true, true, true, true]]],
  "grid4/3/panel/custom/1600x420@100": ["(ii)", ["cols2", "34", 2, "short", [0.4], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
  "grid4/4/-/custom/1024x700@160": ["(ii)", ["rows2", "24", 2, "narrow", [], 0.5116, [false, 0, false], [36, 36, true, true, true, true]]],
  "grid4/4/-/custom/1600x420@100": ["(ii)", ["cols2", "34", 2, "short", [0.4], 0.5, [false, 0, false], [236, 316, false, false, false, false]]],
  "grid4/4/panel/custom/1024x700@160": ["(ii)", ["rows2", "24", 2, "narrow", [], 0.5116, [false, 0, true], [36, 36, true, true, true, true]]],
  "grid4/4/panel/custom/1600x420@100": ["(ii)", ["cols2", "34", 2, "short", [0.4], 0.5, [false, 0, true], [236, 316, false, false, false, false]]],
};

const r6 = (x: number) => (Number.isFinite(x) ? Math.round(x * 1e6) / 1e6 : null);
const SAME = 1e-9;

/**
 * A fit read the way the golden recorded WP-B's: `cols` the top row's column
 * edges (main2: the large pane's right edge), `row` the row edge (0.5 with
 * none, as WP-B's one-row shapes read their unused default), and `bottom` the
 * bottom row's own edges when they differ from the top row's, else [] (WP-B
 * drew one set for the six WP-B shapes, so the golden always says []).
 */
function adapt(f: WorkspaceFit) {
  const rects = f.panes.map((n) => f.boxes[n]!.rect);
  const lefts = (rs: typeof rects) => rs.map((r) => r.left).filter((x) => x > SAME).sort((a, b) => a - b).map(r6);
  const cols = lefts(rects.filter((r) => Math.abs(r.top) < SAME));
  const tops = rects.map((r) => r.top).filter((x) => x > SAME);
  const row = tops.length ? Math.min(...tops) : 0.5;
  const low = lefts(rects.filter((r) => tops.length && Math.abs(r.top - row) < SAME));
  const bottom = JSON.stringify(low) === JSON.stringify(cols) || !tops.length ? [] : low;
  return {
    got: [
      f.drawnPreset, f.panes.join(""), f.dropped, f.why, cols, r6(row),
      [f.panel.open, f.panel.height, f.panel.auto],
      [f.sides.left, f.sides.right, f.sides.leftFolded, f.sides.rightFolded, f.sides.autoLeft, f.sides.autoRight],
    ],
    bottom,
  };
}

describe("FS3: the 300 golden fits through the tree (§7.1)", () => {
  const entries = Object.entries(LEGACY_FITS as Record<string, unknown[]>);

  it("draws every golden fit as WP-B did, except the 62 keys §7.1 (i)–(ii) change [migrating grid4 rows with independent sizes]", () => {
    // The bottom row's own edges must equal the top row's for every WP-B shape:
    // a stored 2×2's rows are linked, so migrating its bottom row with sizes of
    // its own (the default ½ under a custom 0.4) fails here, and in the four
    // changed 1600×420 keys a row drop keeps the bottom row at 0.4, never ½.
    for (const [key, want] of entries) {
      const [preset, focused, panel, custom, win] = key.split("/");
      const [w, h, z] = win.match(/^(\d+)x(\d+)@(\d+)$/)!.slice(1).map(Number);
      const stored = JSON.stringify({
        preset, focused: Number(focused), panel: { open: panel === "panel", height: 260 },
        panes: [{ view: "chat" }, { view: "browser" }, { view: "file" }, { view: "task" }],
        ...(custom === "custom" ? { splits: { [preset]: LEGACY_CUSTOM[preset] } } : {}),
      });
      const f = fitWorkspace(DEFAULT_LAYOUT, parseWorkspace(stored), w / (z / 100), h / (z / 100) - TITLEBAR_H);
      const { got, bottom } = adapt(f);
      expect(got, key).toEqual(CHANGED[key]?.[1] ?? want);
      expect(bottom, `${key}: the bottom row's edges are the top row's`).toEqual([]);
    }
  });

  it("lists exactly the keys it changes, each with a reason, and each really changed", () => {
    const keys = Object.keys(CHANGED);
    expect(keys).toHaveLength(62);
    const golden = LEGACY_FITS as Record<string, unknown[]>;
    for (const [key, [why, value]] of Object.entries(CHANGED)) {
      expect(golden[key], key).toBeDefined();
      expect(value, `${key} is not a change`).not.toEqual(golden[key]);
      const [preset, , , , win] = key.split("/");
      if (why === "(i)") {
        // main2 drawn whole where WP-B's 840 squeezed the side panes or dropped a column.
        expect(preset, key).toBe("main2");
        expect(["1366x768@100", "1280x800@150"], key).toContain(win);
        expect(value[0], key).toBe("main2");
      } else {
        // A dropped render: some pane is not drawn, and its own sizes are kept.
        expect(value[2] as number, key).toBeGreaterThan(0);
        expect(["cols3", "main2", "grid4"], key).toContain(preset);
      }
    }
    expect(keys.filter((k) => CHANGED[k][0] === "(i)")).toHaveLength(24);
    expect(keys.filter((k) => CHANGED[k][0] === "(ii)")).toHaveLength(38);
  });

  it("§7.1 (iii): a four-wide row whose other row is dropped stays four wide (no free tile for it)", () => {
    // 1920 wide leaves 1440 for four columns; 300 tall is one row.
    const g8 = focusPane(setPreset(defaultWorkspace(), "grid8"), 6);
    const f8 = fitWorkspace(DEFAULT_LAYOUT, g8, 1920, 300);
    expect([f8.panes, f8.dropped, f8.why, f8.drawnPreset]).toEqual([[5, 6, 7, 8], 4, "short", null]);
    const g7 = focusPane(setPreset(defaultWorkspace(), "grid7"), 2);
    const f7 = fitWorkspace(DEFAULT_LAYOUT, g7, 1920, 300);
    expect([f7.panes, f7.dropped, f7.drawnPreset]).toEqual([[1, 2, 3, 4], 3, null]);
  });

  it("§7.1 (i): main2's minimum is 720, read from the fit (fit.minW replaces SHAPES[drawn].minW)", () => {
    const f = fitAt(setPreset(defaultWorkspace(), "main2"), 1920, 1080);
    expect([f.minW, f.minH, f.drawnPreset]).toEqual([720, 400, "main2"]);
    const table: [Preset, number, number][] = [
      ["single", 480, 200], ["cols2", 720, 200], ["rows2", 480, 400], ["cols3", 1080, 200], ["grid4", 720, 400],
      ["grid5", 1080, 400], ["grid6", 1080, 400], ["grid7", 1440, 400], ["grid8", 1440, 400],
    ];
    for (const [p, w, h] of table) {
      const g = fitAt(setPreset(defaultWorkspace(), p), 1920, 1080);
      expect([g.minW, g.minH, g.drawnPreset], p).toEqual([w, h, p]);
    }
  });
});

// ---- §8.2: v1 → tree ------------------------------------------------------------

/** Each preset's tree at default and at custom edges, from the v1 value WP-B stored for it. */
const MIGRATION: [Preset, Record<string, unknown>, StoredNode, StoredNode][] = [
  ["single", { cols: [], row: 0.3 }, 1, 1],
  ["cols2", { cols: [0.3] }, X(halves, 1, 2), X([0.3, 0.7], 1, 2)],
  ["rows2", { row: 0.35 }, Y(halves, 1, 2), Y([0.35, 0.65], 1, 2)],
  ["cols3", { cols: [0.2, 0.7] }, X(thirds, 1, 2, 3), X([0.2, 0.5, 0.3], 1, 2, 3)],
  ["main2", { cols: [0.7], row: 0.3 }, X([0.6, 0.4], 1, Y(halves, 2, 3)), X([0.7, 0.3], 1, Y([0.3, 0.7], 2, 3))],
  ["grid4", { cols: [0.4], row: 0.6 },
    Y(halves, X(halves, 1, 2), X(halves, 3, 4)),
    Y([0.6, 0.4], X([0.4, 0.6], 1, 2), X([0.4, 0.6], 3, 4))],
  ["grid5", { cols: [0.3, 0.6], bottom: [0.7], row: 0.55 },
    Y(halves, X(thirds, 1, 2, 3), X(halves, 4, 5)),
    Y([0.55, 0.45], X([0.3, 0.3, 0.4], 1, 2, 3), X([0.7, 0.3], 4, 5))],
  ["grid6", { cols: [0.2, 0.5], row: 0.4 },
    Y(halves, X(thirds, 1, 2, 3), X(thirds, 4, 5, 6)),
    Y([0.4, 0.6], X([0.2, 0.3, 0.5], 1, 2, 3), X([0.2, 0.3, 0.5], 4, 5, 6))],
  ["grid7", { cols: [0.1, 0.4, 0.8], bottom: [0.45, 0.55], row: 0.35 },
    Y(halves, X(quarters, 1, 2, 3, 4), X(thirds, 5, 6, 7)),
    Y([0.35, 0.65], X([0.1, 0.3, 0.4, 0.2], 1, 2, 3, 4), X([0.45, 0.1, 0.45], 5, 6, 7))],
  ["grid8", { cols: [0.1, 0.3, 0.6], row: 0.7 },
    Y(halves, X(quarters, 1, 2, 3, 4), X(quarters, 5, 6, 7, 8)),
    Y([0.7, 0.3], X([0.1, 0.2, 0.3, 0.4], 1, 2, 3, 4), X([0.1, 0.2, 0.3, 0.4], 5, 6, 7, 8))],
];

describe("FS3: a v1 workspace becomes a tree (§8.2)", () => {
  it("migrates all ten presets at their default edges", () => {
    for (const [p, , def] of MIGRATION) {
      const w = v1({ preset: p });
      expectTree(w.tree, def, `${p} default`);
      expect(presetOfWs(w), p).toBe(p);
      expect(orderOf(w), p).toEqual(PANE_NOS.slice(0, SHAPES[p].panes));
    }
  });

  it("migrates all ten at custom edges, grid5's and grid7's bottom rows from their own `bottom` [the bottom row reading s.cols]", () => {
    for (const [p, split, , custom] of MIGRATION) {
      expectTree(v1({ preset: p, splits: { [p]: split } }).tree, custom, `${p} custom`);
    }
  });

  it("presetTree takes the legacy edges itself, ids in reading order", () => {
    const t = presetTree("grid5", P(1, 5, 2, 3, 4), { cols: [0.3, 0.6], row: 0.55, bottom: [0.7] });
    expectTree(t, Y([0.55, 0.45], X([0.3, 0.3, 0.4], 1, 5, 2), X([0.7, 0.3], 3, 4)), "presetTree grid5");
    expectTree(presetTree("main2", P(4, 1, 2), { cols: [0.7], row: 0.3 }), X([0.7, 0.3], 4, Y([0.3, 0.7], 1, 2)), "main2");
    // Without edges, the defaults (FS1's behaviour).
    expectTree(presetTree("grid7", P(1, 2, 3, 4, 5, 6, 7)), MIGRATION[8][2], "grid7 default");
  });

  it("places a stored order's ids in reading order, a hidden pane kept", () => {
    const w = v1({ preset: "grid5", order: [1, 5, 2, 3, 4], kept: [6], focused: 5,
                   splits: { grid5: { cols: [0.3, 0.6], bottom: [0.4], row: 0.55 } } });
    expectTree(w.tree, Y([0.55, 0.45], X([0.3, 0.3, 0.4], 1, 5, 2), X([0.4, 0.6], 3, 4)), "grid5 ordered");
    expect([orderOf(w), w.kept, w.focused]).toEqual([[1, 5, 2, 3, 4], [6], 5]);
  });

  it("reads each v1 edge field on its own, as v1 did: a bad one is that preset's default", () => {
    // The wrong count, outside (0, 1), not increasing: that part's default only.
    expectTree(v1({ preset: "grid5", splits: { grid5: { cols: [0.3, 0.6], bottom: [0.4, 0.8], row: 0.55 } } }).tree,
               Y([0.55, 0.45], X([0.3, 0.3, 0.4], 1, 2, 3), X(halves, 4, 5)), "grid5 bad bottom");
    expectTree(v1({ preset: "cols3", splits: { cols3: { cols: [0.6, 0.4] } } }).tree, X(thirds, 1, 2, 3), "cols3 not increasing");
    expectTree(v1({ preset: "main2", splits: { main2: { cols: [1.2], row: 0.3 } } }).tree,
               X([0.6, 0.4], 1, Y([0.3, 0.7], 2, 3)), "main2 cols outside");
    expectTree(v1({ preset: "rows2", splits: "nope" }).tree, Y(halves, 1, 2), "rows2 junk");
  });

  it("carries over only the current preset's edges: ⊞ to another opens at its defaults (FS-8)", () => {
    const w = v1({ preset: "grid4", splits: { grid4: { cols: [0.4], row: 0.6 }, cols2: { cols: [0.3] } } });
    expectTree(w.tree, Y([0.6, 0.4], X([0.4, 0.6], 1, 2), X([0.4, 0.6], 3, 4)), "grid4");
    expectTree(setPreset(w, "cols2").tree, X(halves, 1, 2), "cols2 after ⊞");
    // And back to 2×2: its stored edges are not remembered either.
    expectTree(setPreset(setPreset(w, "cols2"), "grid4").tree, MIGRATION[5][2], "grid4 again");
  });

  it("keeps a WP-A/WP-B value's undrawn panes kept, and an unknown preset is one pane", () => {
    const one = v1({ preset: "single", panes: [{ view: "chat" }, { view: "diff" }, { view: "terminal", terminalId: "t-1" }, { view: "file" }] });
    expect([storeTree(one.tree), one.kept]).toEqual([1, [2, 3, 4]]);
    expect(storeTree(v1({ preset: "grid9", order: [1, 2, 3, 4, 5, 6, 7, 8, 9] }).tree)).toEqual(1);
    expect(defaultWorkspace().tree).toEqual({ pane: 1 });
    expect(Object.keys(defaultWorkspace()).sort()).toEqual(["focused", "kept", "panel", "panes", "tree"]);
  });
});

// ---- §8.1: the v2 value, and its v1 mirror ----------------------------------------

/**
 * Today's parser (FS2, 1–12 ids), frozen: what a HUD rolled back past FS3
 * reads from the stored value. Copied from lib/workspace.ts `parseWorkspace`
 * and its helpers as they stand before FS3; only the pane specs are read
 * minimally (view, project, terminal), since PaneSpec does not change in FS3
 * and the mirror is about the layout. Never edit this to follow FS3.
 */
type FrozenPreset = "single" | "cols2" | "rows2" | "cols3" | "main2" | "grid4" | "grid5" | "grid6" | "grid7" | "grid8";
interface FrozenSplit { cols: number[]; row: number; bottom?: number[] }
interface FrozenWorkspace {
  preset: FrozenPreset; order: number[]; kept: number[];
  panes: { view: string; projectId: string | null; terminalId: string | null }[];
  splits: Record<FrozenPreset, FrozenSplit>; focused: number; panel: { open: boolean; height: number };
}
function parseV1Frozen(raw: unknown): FrozenWorkspace {
  const PRESETS_F: FrozenPreset[] = ["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid5", "grid6", "grid7", "grid8"];
  const ROWS: Record<FrozenPreset, number[] | null> = {
    single: [1], cols2: [2], rows2: [1, 1], cols3: [3], main2: null, grid4: [2, 2], grid5: [3, 2], grid6: [3, 3],
    grid7: [4, 3], grid8: [4, 4],
  };
  const count = (p: FrozenPreset) => ROWS[p]?.reduce((a, b) => a + b, 0) ?? 3;
  const IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  const VIEWS = ["chat", "task", "file", "diff", "browser", "terminal", "vex", "subagent"];
  const DEFAULT_VIEWS = ["chat", "browser", "file", "task", "chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat"];
  const LEGACY_PANES = 4;
  const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
  const isPreset = (v: unknown): v is FrozenPreset => typeof v === "string" && (PRESETS_F as string[]).includes(v);
  const isPaneNo = (v: unknown): v is number => typeof v === "number" && IDS.includes(v);
  const equalEdges = (n: number) => Array.from({ length: n - 1 }, (_, i) => (i + 1) / n);
  const uneven = (p: FrozenPreset) => { const r = ROWS[p]; return !!r && r.length === 2 && r[0] !== r[1]; };
  const defaultSplit = (p: FrozenPreset): FrozenSplit => {
    if (p === "main2") return { cols: [0.6], row: 0.5 };
    const rows = ROWS[p]!;
    const split: FrozenSplit = { cols: equalEdges(rows[0]), row: 0.5 };
    if (uneven(p)) split.bottom = equalEdges(rows[1]);
    return split;
  };
  const inside = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x) && x > 0 && x < 1;
  const parseEdges = (r: unknown, like: number[]) =>
    Array.isArray(r) && r.length === like.length && r.every(inside) && r.every((x, i) => i === 0 || x > (r as number[])[i - 1])
      ? (r as number[]).slice() : like;
  const parseSplit = (r: unknown, p: FrozenPreset): FrozenSplit => {
    const def = defaultSplit(p);
    const s = isObj(r) ? r : {};
    const split: FrozenSplit = { cols: parseEdges(s.cols, def.cols), row: inside(s.row) ? s.row : def.row };
    if (def.bottom) split.bottom = parseEdges(s.bottom, def.bottom);
    return split;
  };
  const parseOrder = (r: unknown, n: number) =>
    !Array.isArray(r) || r.length !== n || !r.every(isPaneNo) || new Set(r).size !== n ? null : (r as number[]).slice();
  const parseKept = (r: unknown, order: number[]) => {
    const out: number[] = [];
    if (Array.isArray(r)) for (const x of r) if (isPaneNo(x) && !order.includes(x) && !out.includes(x)) out.push(x);
    return out;
  };
  const parsePane = (r: unknown, n: number) => {
    const p = isObj(r) ? r : {};
    const id = (x: unknown) => (typeof x === "string" && x && x.length <= 200 ? x : null);
    const view = p.view === "preview" ? "browser" : typeof p.view === "string" && VIEWS.includes(p.view) ? p.view : DEFAULT_VIEWS[n - 1];
    return { view, projectId: id(p.projectId), terminalId: id(p.terminalId) };
  };
  let v: unknown = null;
  if (typeof raw === "string") {
    try { v = JSON.parse(raw); } catch { v = null; }
  }
  const splits0 = {} as Record<FrozenPreset, FrozenSplit>;
  for (const p of PRESETS_F) splits0[p] = defaultSplit(p);
  const ws: FrozenWorkspace = { preset: "single", order: [1], kept: [], panes: IDS.map((n) => parsePane(undefined, n)),
                                splits: splits0, focused: 1, panel: { open: false, height: PANEL.def } };
  if (!isObj(v)) return ws;
  const preset = isPreset(v.preset) ? v.preset : "single";
  const n = count(preset);
  const order = parseOrder(v.order, n) ?? IDS.slice(0, n);
  const rawPanes = Array.isArray(v.panes) ? v.panes : [];
  const legacy = v.order === undefined && v.kept === undefined;
  const kept = !legacy ? parseKept(v.kept, order) : IDS.filter((id) => id <= LEGACY_PANES && id <= rawPanes.length && !order.includes(id));
  const panes = IDS.map((id) => parsePane(rawPanes[id - 1], id));
  const rawSplits = isObj(v.splits) ? v.splits : {};
  const splits = {} as Record<FrozenPreset, FrozenSplit>;
  for (const p of PRESETS_F) splits[p] = parseSplit(rawSplits[p], p);
  const focused = isPaneNo(v.focused) && order.includes(v.focused) ? v.focused : order[0];
  const panel = isObj(v.panel) ? v.panel : {};
  const h = typeof panel.height === "number" && Number.isFinite(panel.height)
    ? Math.min(PANEL.max, Math.max(PANEL.min, Math.round(panel.height))) : PANEL.def;
  return { preset, order, kept, panes, splits, focused, panel: { open: panel.open === true, height: h } };
}

/** The stored JSON for `w`, as saveWorkspace writes it. */
const stored = (w: Workspace): Record<string, unknown> => JSON.parse(JSON.stringify(workspaceStorage(w)));

describe("FS3: the v2 value round-trips (§8.1)", () => {
  const free = X([0.25, 0.5, 0.25], 1, Y([0.3, 0.7], 5, 9), 12);
  const panes = PANE_NOS.map((n) => ({ view: n === 9 ? "file" : n === 12 ? "diff" : "chat" }));

  it("reads a v2 value whole: its tree, kept, focus, panel and panes", () => {
    const w = v2(free, { kept: [2, 3], focused: 9, panel: { open: true, height: 300 }, panes });
    expect(storeTree(w.tree)).toEqual(free);
    expect([orderOf(w), w.kept, w.focused, w.panel]).toEqual([[1, 5, 9, 12], [2, 3], 9, { open: true, height: 300 }]);
    expect([w.panes[8].view, w.panes[11].view, w.panes]).toEqual(["file", "diff", expect.any(Array)]);
    expect(w.panes).toHaveLength(12);
    expect(presetOfWs(w)).toBeNull();
  });

  it("saves version 2, the tree as stored (bare-number leaves), the mirror, and nothing else", () => {
    const w = v2(free, { kept: [2, 3], focused: 9, panel: { open: true, height: 300 }, panes });
    const m = memory();
    saveWorkspace(w, m);
    const s = JSON.parse(m.map.get(WORKSPACE_KEY)!);
    expect(s.version).toBe(2);
    expect(s.tree).toEqual(free);
    expect([s.kept, s.focused, s.panel]).toEqual([[2, 3], 9, { open: true, height: 300 }]);
    expect(s.panes).toHaveLength(12);
    for (const k of Object.keys(s)) expect(["version", "tree", "kept", "panes", "focused", "panel", "preset", "order"]).toContain(k);
    expect(loadWorkspace(m)).toEqual(w);
  });

  it("keeps a dragged size value for value through a save and a load", () => {
    const t = X([0.123456789, 0.876543211], 1, Y([0.61803398875, 0.38196601125], 2, 3));
    const w = v2(t);
    const m = memory();
    saveWorkspace(w, m);
    expect(storeTree(loadWorkspace(m).tree)).toEqual(t);
    expect(presetOfWs(loadWorkspace(m))).toBe("main2");
  });

  it("round-trips every ladder shape and every ⊞ preset", () => {
    for (const p of PRESETS) {
      const w = setView(setPreset(defaultWorkspace(), p), 1, "diff");
      const m = memory();
      saveWorkspace(w, m);
      expect(loadWorkspace(m), p).toEqual(w);
      expect(presetOfWs(loadWorkspace(m)), p).toBe(p);
    }
  });
});

describe("FS3: the v1 mirror, read by a HUD rolled back past FS3 (§8.1)", () => {
  /** What the ladder drew for n panes, splitting pane 1 each time (FS3's ladder; built directly since FS4 removed it). */
  const ladder = (n: number) => {
    const order = [1, ...Array.from({ length: n - 1 }, (_, i) => n - i)] as PaneNo[];
    const panes = defaultWorkspace().panes.map((p, i) => (i > 0 && i < n ? chat() : p));
    return { ...defaultWorkspace(), tree: presetTree(LADDER_SHAPE[n], order), panes, focused: P(n)[0] };
  };
  const views = (w: { panes: { view: string }[] }) => w.panes.map((p) => p.view);

  it("a ladder 2×2 reads back as that 2×2, its order, focus, panel and panes", () => {
    const w = setView(ladder(4), 3, "file");
    expect(orderOf(w)).toEqual([1, 4, 3, 2]);
    const s = stored(w);
    expect([s.preset, s.order]).toEqual(["grid4", [1, 4, 3, 2]]);
    const old = parseV1Frozen(JSON.stringify(s));
    expect([old.preset, old.order, old.kept, old.focused, old.panel]).toEqual(["grid4", [1, 4, 3, 2], w.kept, w.focused, w.panel]);
    expect(views(old)).toEqual(views(w));
  });

  it("a dragged two columns is still two columns to the old parser (sizes are not its business)", () => {
    const w = v2(X([0.3, 0.7], 1, 2));
    const s = stored(w);
    expect([s.preset, s.order]).toEqual(["cols2", [1, 2]]);
    expect(parseV1Frozen(JSON.stringify(s)).preset).toBe("cols2");
  });

  it("a tree no preset has mirrors the ladder's shape for its count", () => {
    const w = v2(X([0.25, 0.25, 0.25, 0.25], 1, Y(halves, 2, 3), 4, 5));
    expect(presetOfWs(w)).toBeNull();
    const s = stored(w);
    expect([s.preset, s.order]).toEqual([LADDER_SHAPE[5], [1, 2, 3, 4, 5]]);
    const old = parseV1Frozen(JSON.stringify(s));
    expect([old.preset, old.order]).toEqual(["grid5", [1, 2, 3, 4, 5]]);
  });

  it("names no order when an id is above 8 or the count is not the preset's; the old parser then reads 1…n", () => {
    const nine = withTree(setPreset(setPreset(defaultWorkspace(), "grid8"), "single"), X(halves, 1, 9));
    expect(orderOf(nine)).toEqual([1, 9]);
    const s = stored(nine);
    expect(s.preset).toBe("cols2");
    expect("order" in s).toBe(false);
    expect(parseV1Frozen(JSON.stringify(s)).preset).toBe("cols2");
    const ten = v2(Y([0.4, 0.3, 0.3], X(quarters, 1, 2, 3, 4), X(thirds, 5, 6, 7), X(thirds, 8, 9, 10)));
    const t = stored(ten);
    expect([t.preset, "order" in t]).toEqual(["grid8", false]);
    expect(parseV1Frozen(JSON.stringify(t)).preset).toBe("grid8");
  });

  it("⊞ to one pane mirrors one pane, the rest kept", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    const old = parseV1Frozen(JSON.stringify(stored(one)));
    expect([old.preset, old.order, old.kept]).toEqual(["single", [1], [2, 3, 4, 5, 6, 7, 8]]);
  });

  it("a bad v2 tree falls back to its own mirror; every other field is still read", () => {
    const w = setPanel4(setView(ladder(4), 3, "file"));
    const s = stored(w);
    for (const bad of [null, "x", [1, 2], { axis: "z", children: [1, 2], sizes: halves }, { axis: "x", children: [1, 1], sizes: halves }]) {
      const back = parseWorkspace(JSON.stringify({ ...s, tree: bad }));
      expect([orderOf(back), presetOfWs(back)], JSON.stringify(bad)).toEqual([[1, 4, 3, 2], "grid4"]);
      expect([back.kept, back.focused, back.panel, views(back)]).toEqual([w.kept, w.focused, w.panel, views(w)]);
    }
  });
});

function setPanel4(w: Workspace): Workspace {
  return { ...w, panel: { open: true, height: 333 } };
}

// ---- §8.3: garbage, field by field -------------------------------------------------

describe("FS3: a stored v2 value read field by field (§8.3)", () => {
  // A good value: main2's shape with pane 5 and 2 in the stack, dragged sizes,
  // two kept panes, focus in the stack, the panel open, and its mirror.
  const tree = { axis: "x" as const, children: [1, Y([0.4, 0.6], 5, 2)], sizes: [0.3, 0.7] };
  const good = {
    version: 2, tree, kept: [3, 9], focused: 5, panel: { open: true, height: 300 },
    panes: [{ view: "file", projectId: "p1" }, { view: "diff" }, { view: "browser" }],
    preset: "main2", order: [1, 5, 2],
  };
  const base = parseWorkspace(JSON.stringify(good));
  const fields = ["tree", "kept", "focused", "panel", "panes"] as const;

  it("reads the good value whole", () => {
    expect(storeTree(base.tree)).toEqual(tree);
    expect([base.kept, base.focused, base.panel]).toEqual([[3, 9], 5, { open: true, height: 300 }]);
    expect([base.panes[0].view, base.panes[0].projectId, base.panes[1].view, base.panes[2].view, base.panes[8].view])
      .toEqual(["file", "p1", "diff", "browser", "chat"]);
  });

  const variants: [string, Record<string, unknown>, (w: Workspace) => void][] = [
    // The tree: any cap broken rejects the whole tree, and the value falls back to its mirror.
    ...([
      ["not an object", "x"], ["null", null], ["a fraction for a leaf", { axis: "x", children: [1, 2.5], sizes: halves }],
      ["a leaf 13", { axis: "x", children: [1, 13], sizes: halves }], ["a leaf 0", { axis: "x", children: [0, 1], sizes: halves }],
      ["a string leaf", { axis: "x", children: [1, "3"], sizes: halves }],
      ["a duplicate leaf", { axis: "x", children: [1, { axis: "y", children: [5, 1], sizes: halves }], sizes: halves }],
      ["an unknown axis", { axis: "z", children: [1, 2], sizes: halves }],
      ["five children across", { axis: "x", children: [1, 2, 3, 4, 5], sizes: [0.2, 0.2, 0.2, 0.2, 0.2] }],
      ["four children down", { axis: "y", children: [1, 2, 3, 4], sizes: quarters }],
      ["depth 7", ((): unknown => { let t: unknown = 1; for (let i = 0; i < 6; i++) t = { axis: i % 2 ? "x" : "y", children: [t, 10 - i], sizes: halves }; return t; })()],
    ] as [string, unknown][]).map(([what, bad]): [string, Record<string, unknown>, (w: Workspace) => void] => [
      `a tree with ${what}`, { tree: bad },
      (w) => expectTree(w.tree, X([0.6, 0.4], 1, Y(halves, 5, 2)), `${what}: the mirror's main2 at default sizes`),
    ]),
    // Sizes: that node only.
    ["root sizes not a list", { tree: { ...tree, sizes: "0.3,0.7" } }, (w) => expectTree(w.tree, X(halves, 1, Y([0.4, 0.6], 5, 2)), "root")],
    ["inner sizes the wrong length", { tree: X([0.3, 0.7], 1, Y([1], 5, 2)) }, (w) => expectTree(w.tree, X([0.3, 0.7], 1, Y(halves, 5, 2)), "inner")],
    ["a size of zero", { tree: X([0.3, 0.7], 1, Y([0, 1], 5, 2)) }, (w) => expectTree(w.tree, X([0.3, 0.7], 1, Y(halves, 5, 2)), "zero")],
    ["a negative size", { tree: X([-0.3, 1.3], 1, Y([0.4, 0.6], 5, 2)) }, (w) => expectTree(w.tree, X(halves, 1, Y([0.4, 0.6], 5, 2)), "negative")],
    ["a null size (NaN in storage)", { tree: X([0.3, 0.7], 1, Y([null as unknown as number, 0.6], 5, 2)) },
      (w) => expectTree(w.tree, X([0.3, 0.7], 1, Y(halves, 5, 2)), "null")],
    ["a string size", { tree: X(["0.3" as unknown as number, 0.7], 1, Y([0.4, 0.6], 5, 2)) },
      (w) => expectTree(w.tree, X(halves, 1, Y([0.4, 0.6], 5, 2)), "string")],
    ["sizes that do not sum to 1 (divided out)", { tree: X([3, 7], 1, Y([0.4, 0.6], 5, 2)) },
      (w) => expectTree(w.tree, X([0.3, 0.7], 1, Y([0.4, 0.6], 5, 2)), "sum")],
    ["sizes whose sum is not finite", { tree: X([1e308, 1e308], 1, Y([0.4, 0.6], 5, 2)) },
      (w) => expectTree(w.tree, X(halves, 1, Y([0.4, 0.6], 5, 2)), "infinite sum")],
    // kept, focused, panel, panes: as v1 reads them.
    ["kept not a list", { kept: 6 }, (w) => expect(w.kept).toEqual([])],
    ["kept holding junk, repeats and drawn ids", { kept: [13, 2, "7", 3, 3, 0, 1, 9, 4.5] }, (w) => expect(w.kept).toEqual([3, 9])],
    ["focus on a pane not drawn", { focused: 3 }, (w) => expect(w.focused).toBe(1)],
    ["focus out of range", { focused: 13 }, (w) => expect(w.focused).toBe(1)],
    ["focus as a string", { focused: "5" }, (w) => expect(w.focused).toBe(1)],
    ["the panel open as a string", { panel: { open: "true", height: 300 } }, (w) => expect(w.panel).toEqual({ open: false, height: 300 })],
    ["the panel's height junk", { panel: { open: true, height: "tall" } }, (w) => expect(w.panel).toEqual({ open: true, height: PANEL.def })],
    ["the panel's height tiny", { panel: { open: true, height: 5 } }, (w) => expect(w.panel).toEqual({ open: true, height: PANEL.min })],
    ["panes not a list", { panes: "junk" }, (w) => expect(w.panes).toEqual(defaultWorkspace().panes)],
    ["a bad pane", { panes: [{ view: "nonsense" }, { view: "diff" }, { view: "browser" }] },
      (w) => expect(w.panes.slice(0, 3).map((p) => p.view)).toEqual(["chat", "diff", "browser"])],
  ];

  for (const [what, bad, only] of variants) {
    it(`falls back on ${what}, and only there`, () => {
      const w = parseWorkspace(JSON.stringify({ ...good, ...bad }));
      only(w);
      const field = Object.keys(bad)[0];
      for (const k of fields) {
        if (k === field) continue;
        expect(k === "tree" ? storeTree(w.tree) : w[k], `${what}: ${k}`).toEqual(k === "tree" ? storeTree(base.tree) : base[k]);
      }
    });
  }

  it("refuses a 100 000-entry children array quickly, as a bad tree", () => {
    const huge = { axis: "x", children: Array.from({ length: 100_000 }, (_, i) => (i % 12) + 1), sizes: [] };
    const t0 = performance.now();
    const w = parseWorkspace(JSON.stringify({ ...good, tree: huge }));
    expect(performance.now() - t0).toBeLessThan(500);
    expect(orderOf(w)).toEqual([1, 5, 2]);
  });

  it("reads a version other than the number 2 with today's parser: the mirror, at default sizes", () => {
    for (const version of [3, "2", 1, null]) {
      const w = parseWorkspace(JSON.stringify({ ...good, version }));
      expectTree(w.tree, X([0.6, 0.4], 1, Y(halves, 5, 2)), `version ${JSON.stringify(version)}`);
      expect([w.kept, w.focused]).toEqual([[3, 9], 5]);
    }
  });

  it("with a bad tree and a bad mirror, opens today's fallback: one pane", () => {
    const w = parseWorkspace(JSON.stringify({ ...good, tree: "x", preset: "nope", order: "1,5,2" }));
    expect([orderOf(w), w.focused]).toEqual([[1], 1]);
  });

  it("ignores unknown keys and never reads an inherited one (__proto__ stays inert)", () => {
    const raw = `{"version":2,"tree":{"axis":"x","children":[1,{"axis":"y","children":[5,2],"sizes":[0.4,0.6],"colour":"red"}],`
      + `"sizes":[0.3,0.7],"__proto__":{"polluted":true}},"kept":[3,9],"focused":5,"panel":{"open":true,"height":300},`
      + `"extra":{"a":1},"preset":"main2","order":[1,5,2]}`;
    const w = parseWorkspace(raw);
    expect(storeTree(w.tree)).toEqual(tree);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect((w.tree as unknown as Record<string, unknown>).polluted).toBeUndefined();
  });

  it("never checks FS-3 on read: a tree over the cap is drawn as it is (and an over-wide flattened node too)", () => {
    // A line across the top crosses five panes: kept, never lost on reload.
    const over = X(quarters, 1, 2, 3, Y(halves, X(halves, 4, 5), 6));
    expect(leaves(v2(over).tree)).toEqual([1, 2, 3, 4, 5, 6]);
    // x[x[1,2,3], x[4,5,6]] flattens into one x node of six children (the FS1 carry-forward note):
    const wide = v2(X(halves, X(thirds, 1, 2, 3), X(thirds, 4, 5, 6)));
    expect(leaves(wide.tree)).toEqual([1, 2, 3, 4, 5, 6]);
    const f = fitWorkspace(DEFAULT_LAYOUT, focusPane(wide, 6), 4000, 1000);
    expect(f.panes).toEqual([1, 2, 3, 4, 5, 6]);
    expect(f.edges.filter((e) => e.axis === "x")).toHaveLength(5);
    expect(fitWorkspace(DEFAULT_LAYOUT, focusPane(wide, 6), 1000, 800).panes).toContain(6);
  });
});

// ---- ⊞ 8 → 1 → 8 -------------------------------------------------------------------

describe("FS3: ⊞ hides and brings back, on the tree (§3.3, FS-8)", () => {
  const eight = () => {
    let w = setPreset(defaultWorkspace(), "grid8");
    const views: View[] = ["chat", "browser", "file", "task", "diff", "terminal", "chat", "file"];
    views.forEach((v, i) => (w = setView(w, (i + 1) as PaneNo, v)));
    return pinPane(setBrowserUrl(w, 2, "http://localhost:5173/"), 8, "p2");
  };

  it("8 → 1 → 8 brings back every pane, in its place, with what it showed", () => {
    const w = eight();
    expectTree(w.tree, MIGRATION[9][2], "grid8");
    const one = setPreset(w, "single");
    expect([storeTree(one.tree), one.kept]).toEqual([1, [2, 3, 4, 5, 6, 7, 8]]);
    const back = setPreset(one, "grid8");
    expectTree(back.tree, MIGRATION[9][2], "grid8 again");
    expect([orderOf(back), back.kept, back.panes]).toEqual([[1, 2, 3, 4, 5, 6, 7, 8], [], w.panes]);
  });

  it("brings back a ladder order where it was, and ids 9–12 too", () => {
    const w = { ...defaultWorkspace(), tree: presetTree("grid8", P(1, 8, 7, 6, 5, 4, 3, 2)) };
    expect(orderOf(w)).toEqual([1, 8, 7, 6, 5, 4, 3, 2]);
    const back = setPreset(setPreset(w, "single"), "grid8");
    expect([orderOf(back), back.kept]).toEqual([[1, 8, 7, 6, 5, 4, 3, 2], []]);
    // FS2's ids: 8 → 1, a split takes 9, then ⊞ 8 takes 1, 9, then the hidden ones.
    const nine = withTree(setPreset(setPreset(defaultWorkspace(), "grid8"), "single"), X(halves, 1, 9));
    const g = setPreset(nine, "grid8");
    expect([orderOf(g), g.kept]).toEqual([[1, 9, 2, 3, 4, 5, 6, 7], [8]]);
  });

  it("keeps no memory of a custom shape: a preset comes back at its default sizes (FS-8)", () => {
    const w = eight();
    // 2400 wide: the columns are wider than the minimum, so the edge can move.
    const f = fitAt(w, 2400, 1080);
    const edge = f.edges.find((e) => e.testid === "split-x0-1")!;
    const dragged = moveEdge(w, f, edge, edge.value + 40);
    expect(storeTree(dragged.tree)).not.toEqual(storeTree(w.tree));
    expect(presetOfWs(dragged)).toBe("grid8");
    const back = setPreset(setPreset(dragged, "single"), "grid8");
    expectTree(back.tree, MIGRATION[9][2], "grid8 at its defaults");
    // A free tree replaced by ⊞: its panes come back in order, its shape does not.
    const freeW = v2(X([0.25, 0.25, 0.25, 0.25], 1, Y(halves, 2, 3), 4, 5));
    const g5 = setPreset(freeW, "grid6");
    expectTree(g5.tree, MIGRATION[7][2], "grid6 from a free tree");
    expect(setPreset(freeW, "cols2").kept).toEqual([3, 4, 5]);
  });

  it("Reset layout keeps the first pane in reading order and hides the rest, the panel closed", () => {
    const w = setPanel4(v2(X(thirds, 5, 1, 2), { focused: 2 }));
    const r = resetWorkspace(w);
    expect([storeTree(r.tree), r.kept, r.focused, r.panel]).toEqual([5, [1, 2], 5, { open: false, height: PANEL.def }]);
  });
});

// ---- edges (§9) ------------------------------------------------------------------

describe("FS3: the edges, from the fit (§9, §3.3's test ids)", () => {
  const at = (p: Preset) => {
    const w = setPreset(defaultWorkspace(), p);
    return { w, f: fitAt(w, 1920, 1080) };
  };

  it("names each preset's separators as §3.3 does, linked where the columns line up (P-5)", () => {
    const want: Record<Preset, [string, number][]> = {
      single: [],
      cols2: [["split-x-1", 1]],
      rows2: [["split-y-1", 1]],
      cols3: [["split-x-1", 1], ["split-x-2", 1]],
      main2: [["split-x-1", 1], ["split-y1-1", 1]],
      grid4: [["split-x0-1", 2], ["split-y-1", 1]],
      grid5: [["split-x0-1", 1], ["split-x0-2", 1], ["split-x1-1", 1], ["split-y-1", 1]],
      grid6: [["split-x0-1", 2], ["split-x0-2", 2], ["split-y-1", 1]],
      grid7: [["split-x0-1", 1], ["split-x0-2", 1], ["split-x0-3", 1], ["split-x1-1", 1], ["split-x1-2", 1], ["split-y-1", 1]],
      grid8: [["split-x0-1", 2], ["split-x0-2", 2], ["split-x0-3", 2], ["split-y-1", 1]],
    };
    for (const p of Object.keys(want) as Preset[]) {
      const { f } = at(p);
      expect(f.edges.map((e): [string, number] => [e.testid, e.members.length]).sort(), p).toEqual(want[p]);
    }
    // 2×2's one column separator moves both rows' boundary.
    const g = at("grid4").f.edges.find((e) => e.testid === "split-x0-1")!;
    expect([g.axis, g.members]).toEqual(["x", [{ path: [0], i: 1 }, { path: [1], i: 1 }]]);
  });

  it("draws each pane from its own box: fractions and px, every box the minimum or more", () => {
    const { f } = at("cols2");
    expect(f.width).toBe(1920 - 236 - 316);
    expect(f.boxes[1]!.rect).toEqual({ left: 0, top: 0, width: 0.5, height: 1 });
    expect(f.boxes[2]!.rect).toEqual({ left: 0.5, top: 0, width: 0.5, height: 1 });
    expect(f.boxes[2]!.px).toEqual({ width: 684, height: f.height });
    const e = f.edges[0];
    expect([e.value, e.min, e.max]).toEqual([684, 360, 1368 - 360]);
    for (const p of ["grid8", "grid7", "main2", "grid5"] as Preset[]) {
      const g = at(p).f;
      expect(Object.keys(g.boxes).map(Number).sort((a, b) => a - b), p).toEqual([...g.panes].sort((a, b) => a - b));
      for (const n of g.panes) expect(g.boxes[n]!.px.width, `${p} pane ${n}`).toBeGreaterThanOrEqual(360 - 1e-6);
    }
  });

  it("in 3 over 2, moving split-x1-1 moves only the bottom row; split-x0-1 only the top", () => {
    const { w, f } = at("grid5");
    const bottom = f.edges.find((e) => e.testid === "split-x1-1")!;
    const moved = moveEdge(w, f, bottom, bottom.value + 120);
    const t = moved.tree as SplitNode;
    const before = w.tree as SplitNode;
    expect(t.sizes).toEqual(before.sizes);
    expect((t.children[0] as SplitNode).sizes).toEqual((before.children[0] as SplitNode).sizes);
    const b = (t.children[1] as SplitNode).sizes;
    expect(b[0] * f.width).toBeCloseTo(bottom.value + 120, 6);
    expect(b[0] + b[1]).toBeCloseTo(1, 12);
    const top = f.edges.find((e) => e.testid === "split-x0-1")!;
    const m2 = moveEdge(w, f, top, top.value - 60);
    expect((m2.tree as SplitNode).children[1]).toEqual(before.children[1]);
    expect(((m2.tree as SplitNode).children[0] as SplitNode).sizes[0] * f.width).toBeCloseTo(top.value - 60, 6);
  });

  it("in 2×2, the linked column edge moves both rows; the row edge only the rows", () => {
    const { w, f } = at("grid4");
    const col = f.edges.find((e) => e.testid === "split-x0-1")!;
    const moved = moveEdge(w, f, col, col.value + 100) as Workspace;
    const t = moved.tree as SplitNode;
    const r0 = (t.children[0] as SplitNode).sizes;
    const r1 = (t.children[1] as SplitNode).sizes;
    expect(r0).toEqual(r1);
    expect(r0[0] * f.width).toBeCloseTo(col.value + 100, 6);
    const row = f.edges.find((e) => e.testid === "split-y-1")!;
    const m2 = moveEdge(w, f, row, row.value + 50);
    expect((m2.tree as SplitNode).sizes[0] * f.height).toBeCloseTo(row.value + 50, 6);
    expect((m2.tree as SplitNode).children).toEqual((w.tree as SplitNode).children);
    // Double-click: equal again, both rows (equalize every member's node).
    const even = equalize(moved, col.members.map((m) => m.path));
    expectTree(even.tree, MIGRATION[5][2], "grid4 equal");
  });

  it("stops a dragged edge at the pane minimum, and a dragged cols2 is still cols2", () => {
    const { w, f } = at("cols2");
    const e = f.edges[0];
    const far = moveEdge(w, f, e, -2000);
    expect((far.tree as SplitNode).sizes[0] * f.width).toBeCloseTo(360, 6);
    const near = moveEdge(w, f, e, 99999);
    expect((near.tree as SplitNode).sizes[1] * f.width).toBeCloseTo(360, 6);
    expect(presetOfWs(far)).toBe("cols2");
    expect(orderOf(far)).toEqual([1, 2]);
    // main2: the stack's edge moves only the stack.
    const m = at("main2");
    const stack = m.f.edges.find((x) => x.testid === "split-y1-1")!;
    const mm = moveEdge(m.w, m.f, stack, stack.value + 80);
    expect((mm.tree as SplitNode).sizes).toEqual((m.w.tree as SplitNode).sizes);
    expect(((mm.tree as SplitNode).children[1] as SplitNode).sizes[0] * m.f.height).toBeCloseTo(stack.value + 80, 6);
  });

  it("an edge handle is plain data the page can hold (no Maps, JSON-safe)", () => {
    const { f } = at("grid4");
    const e: EdgeHandle = f.edges[0];
    expect(JSON.parse(JSON.stringify(e))).toEqual(e);
    expect(JSON.parse(JSON.stringify(f.boxes))).toEqual(f.boxes);
  });
});

// ---- WP-G2's rules, through the derived order and preset ----------------------------
//
// FS3's copy of workspace.test.ts's ladder blocks, read through `ord` and `pre`.
// FS4 replaced the ladder; what is left are WP-G2's rules that still hold, on
// the new signatures. Removed here, each replaced in workspace.split.test.ts:
//   "climbs the ladder 1 → 8 and back"                  → "6. the full 4×3 …", "close gives the space …"
//   "lands on the ladder's shape …"                      → "the split (§5.1) > halves only the pane split …"
//   "refuses … and a ninth pane"                         → "the split (§5.1) > refuses an id already drawn …", "\"max\" with twelve leaves …"
//   "decides the split button from the count and the room" → the "the verdict …" blocks and "the buttons' titles (§10)"

const ord = (w: Workspace): PaneNo[] => orderOf(w);
const pre = (w: Workspace): Preset | null => {
  const v = w as unknown as { tree?: LayoutNode; preset?: Preset };
  return v.tree ? presetOf(v.tree) : v.preset ?? null;
};

describe("WP-G2's rules on the tree (directional since FS4)", () => {
  const unmeasured = (w: Workspace) => fitWorkspace(DEFAULT_LAYOUT, w, NaN, NaN);
  const splitAt = (w: Workspace, slot: number, axis: "x" | "y" = "x") =>
    splitPane(w, unmeasured(w), ord(w)[slot - 1], axis, allocate(w)!, chat());
  const allTwelve = () => {
    let w = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    for (const axis of ["x", "x", "x", "y"] as const) w = splitAt(w, ord(w).length, axis);
    expect([ord(w), w.kept]).toEqual([[1, 9, 10, 11, 12], [2, 3, 4, 5, 6, 7, 8]]);
    return w;
  };

  it("puts the new pane right after its source, focused; × hands focus to the neighbour that takes its space", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const w = splitPane(three, unmeasured(three), 2, "x", 4, chat());
    expect([ord(w), slotOf(w, 4), w.focused]).toEqual([[1, 2, 4, 3], 3, 4]);
    const closed = closePane(w, 4);
    expect([ord(closed), closed.focused]).toEqual([[1, 2, 3], 2]);
    expect(closePane(focusPane(closed, 3), 3).focused).toBe(2);
    expect(closePane(focusPane(closed, 1), 3).focused).toBe(1);
    const grid = setPreset(defaultWorkspace(), "grid4");
    expect([closePane(grid, 1, 3).focused, closePane(grid, 1, 1).focused, closePane(grid, 1, null).focused]).toEqual([3, 2, 2]);
  });

  it("splits without moving focus when asked not to", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "cols2"), 1);
    const quiet = splitPane(w, unmeasured(w), 2, "x", 3, { view: "browser", projectId: null, terminalId: null }, { focus: false });
    expect([ord(quiet), quiet.focused, splitPane(w, unmeasured(w), 2, "x", 3, chat()).focused]).toEqual([[1, 2, 3], 1, 3]);
  });

  it("allocates the lowest free id, else the oldest hidden pane with no unsaved edit, else none", () => {
    expect(allocate(defaultWorkspace())).toBe(2);
    expect(allocate(closePane(setPreset(defaultWorkspace(), "grid4"), 2))).toBe(2);
    expect(allocate(setPreset(setPreset(defaultWorkspace(), "grid8"), "single"))).toBe(9);
    const one = allTwelve();
    expect([allocate(one), allocate(one, [8]), allocate(one, [2, 3, 4, 5, 6, 7, 8])]).toEqual([8, 7, null]);
    expect([allocate(one, [], [8, 7]), allocate(one, [6], [2, 3, 4, 5, 7, 8])]).toEqual([6, 8]);
    const took = splitPane(one, unmeasured(one), 1, "y", 8, chat());
    expect([took.kept, ord(took)]).toEqual([[2, 3, 4, 5, 6, 7], [1, 8, 9, 10, 11, 12]]);
  });

  it("resets only the closed pane's spec and leaves kept alone", () => {
    let w = setPreset(setPreset(defaultWorkspace(), "grid8"), "grid6");
    w = setView(setBrowserUrl(setView(w, 3, "browser"), 3, "http://localhost:5173/"), 4, "file");
    const closed = closePane(w, 3);
    expect(closed.panes[2]).toEqual(defaultPane(3));
    expect(closed.panes.filter((_, i) => i !== 2)).toEqual(w.panes.filter((_, i) => i !== 2));
    // 2 took its space: two over three is no preset (FS4; the ladder drew 3 over 2).
    expect([closed.kept, pre(closed), ord(closed)]).toEqual([w.kept, null, [1, 2, 4, 5, 6]]);
  });

  it("closes and splits ids, never slots, and a reload keeps an order without pane 1", () => {
    const w = closePane(setPreset(defaultWorkspace(), "cols3"), 1);
    expect([ord(w), paneAt(w, 1), slotOf(w, 3)]).toEqual([[2, 3], 2, 2]);
    const back = splitPane(w, unmeasured(w), 3, "x", allocate(w)!, chat());
    expect([ord(back), slotOf(back, 1)]).toEqual([[2, 3, 1], 3]);
    const m = memory();
    saveWorkspace(w, m);
    expect([ord(loadWorkspace(m)), loadWorkspace(m).focused]).toEqual([[2, 3], 2]);
    const only2 = closePane(setPreset(defaultWorkspace(), "cols2"), 1);
    expect([show(only2, "file", []).panes[1].view, show(only2, "file", []).focused]).toEqual(["file", 2]);
  });

  it("⊞: fewer panes hide, more bring them back, most recently hidden first; the same count changes only the shape", () => {
    let w = setPreset(setPreset(setPreset(defaultWorkspace(), "grid4"), "cols2"), "single");
    expect(w.kept).toEqual([2, 3, 4]);
    expect([ord(setPreset(w, "cols2")), ord(setPreset(w, "grid6"))]).toEqual([[1, 2], [1, 2, 3, 4, 5, 6]]);
    const one = setPreset(v1({ preset: "cols3", order: [1, 5, 2] }), "single");
    expect([ord(one), one.kept, ord(setPreset(one, "grid4"))]).toEqual([[1], [5, 2], [1, 5, 2, 3]]);
    w = setPreset(v1({ preset: "cols3", order: [3, 1, 2], kept: [7] }), "main2");
    expect([pre(w), ord(w), w.kept]).toEqual(["main2", [3, 1, 2], [7]]);
    const f = focusPane(setPreset(defaultWorkspace(), "grid8"), 7);
    expect([setPreset(f, "grid6").focused, setPreset(f, "grid7").focused]).toEqual([1, 7]);
    const r = resetWorkspace(v1({ preset: "cols3", order: [5, 1, 2], focused: 2 }));
    expect([pre(r), ord(r), r.kept, r.focused]).toEqual(["single", [5], [1, 2], 5]);
  });

  it("ids are not slots: a stored order numbers its panes by place", () => {
    const w = v1({ preset: "cols3", order: [1, 5, 2], kept: [7] });
    expect([[1, 5, 2].map((n) => slotOf(w, n as PaneNo)), [1, 2, 3].map((s) => paneAt(w, s)), paneAt(w, 4)])
      .toEqual([[1, 2, 3], [1, 5, 2], null]);
    expect([slotOf(w, 7), ord(setPreset(w, "grid4"))[3]]).toEqual([4, 7]);
    expect(paneSequence(w)).toEqual([1, 5, 2, 7, 3, 4, 6, 8, 9, 10, 11, 12]);
    expect([1, 7, 3].map((n) => inWorkspace(w, n as PaneNo))).toEqual([true, true, false]);
  });

  it("FS2's twelve ids: ⊞ 8 → 1, a split takes 9 in slot 2; 9–12 stored and read", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    const two = splitPane(one, unmeasured(one), 1, "x", allocate(one)!, chat());
    expect([pre(two), ord(two), two.kept, two.focused, slotOf(two, 9)]).toEqual(["cols2", [1, 9], [2, 3, 4, 5, 6, 7, 8], 9, 2]);
    let w = one;
    for (const axis of ["x", "x", "x", "y"] as const) w = splitAt(w, ord(w).length, axis);
    const eight = setPreset(w, "grid8");
    expect([ord(eight), eight.kept]).toEqual([[1, 9, 10, 11, 12, 2, 3, 4], [5, 6, 7, 8]]);
    const panes = Array.from({ length: 12 }, () => ({ view: "chat" }));
    panes[9] = { view: "file" };
    const read = v1({ preset: "grid4", order: [1, 10, 2, 12], kept: [11, 9], focused: 10, panes });
    expect([pre(read), ord(read), read.kept, read.focused, read.panes[9].view]).toEqual(["grid4", [1, 10, 2, 12], [11, 9], 10, "file"]);
    const m = memory();
    saveWorkspace(read, m);
    expect(loadWorkspace(m)).toEqual(read);
  });
});
