// The centre's panes as a split tree (docs/plans/2026-10-19-hud-free-split-
// plan.md §3.1; decisions FS-1..FS-10): VS Code's layout, where every node
// lays its children side by side (`x`, split right) or stacked (`y`, split
// down), each child holding a fraction of the node. Pure, like lib/layout.ts
// and lib/workspace.ts, so every rule is tested without a browser. Since FS3
// it is the workspace's model (`Workspace.tree`).
//
// **A tree is kept normal.** Every split node has two or more children, none
// of them of its own axis (a same-axis child is flattened into it), and sizes
// that are finite, positive and sum to 1. `normalize` restores that; every
// other function keeps it.
//
// **Sizes are kept value for value wherever nothing asks them to change.**
// A node whose sizes already sum to 1 within `SUM_TOLERANCE` is never
// rescaled, and a split halves its source exactly (case B, §5.1), so a split
// moves no other pane by even one ulp (FS-5, §5.3). `rectsOf` adds offsets
// with a correctly rounded sum for the same reason: s/2 + s/2 regrouped from
// s must give the siblings after it the same offset, bit for bit.
//
// **The 4-across / 3-down rule (FS-3) is geometric** (§4.1): it counts the
// panes a line through the drawn rectangles crosses, not the tree's
// structure. The structural counts (`acrossS`, `downS`) are the minimum sizes
// (§4.4), which fitting and FS-4 use.
//
// Pane ids run 1–12 (FS-1; workspace.ts's `PaneNo`, `MAX_LEAVES`).
//
// Every fraction here is of the pane area (the unit square); every pixel
// count is the HUD's own (unzoomed) pixels, like layout.ts.

import { MAIN_MIN } from "./layout";
import { PANE_MIN_H, PANE_MIN_W, type PaneNo, type Preset } from "./workspace";

/** x: children side by side (split right); y: stacked (split down). */
export type Axis = "x" | "y";
export interface Leaf { pane: PaneNo }
export interface SplitNode {
  axis: Axis;
  /** x: 2–4, y: 2–3; no child has its parent's axis (flattened). */
  children: LayoutNode[];
  /** One per child, finite, > 0, sum 1 (fractions of the node). */
  sizes: number[];
}
export type LayoutNode = Leaf | SplitNode;
/** Child indices from the root. */
export type Path = readonly number[];
/** Unit fractions of the pane area. */
export interface Rect { left: number; top: number; width: number; height: number }
/** What is persisted: leaves as bare numbers (§8.1). */
export type StoredNode = number | { axis: Axis; children: StoredNode[]; sizes: number[] };

/** FS-3: no line crosses more than four panes across or three down. */
export const MAX_ACROSS = 4;
export const MAX_DOWN = 3;
/** FS-1: twelve pane ids. */
export const MAX_LEAVES = 12;
/** Nodes on the longest root-to-leaf path, the leaf counted (§4.4: three x ancestors, two y). */
export const MAX_DEPTH = 6;
/** Edges closer than this are one edge. */
export const SAME_EDGE = 1e-4;

/** A node whose sizes sum to 1 within this is never rescaled. */
const SUM_TOLERANCE = 1e-9;
/** The most nodes a tree of twelve leaves can have: 12 leaves and 11 split nodes. */
const MAX_NODES = 2 * MAX_LEAVES - 1;
/** The most children a node of each axis can have (FS-3 bounds it per node). */
const MAX_CHILDREN: Readonly<Record<Axis, number>> = { x: MAX_ACROSS, y: MAX_DOWN };

export const isLeaf = (n: LayoutNode): n is Leaf => "pane" in n;
const equal = (k: number) => Array.from({ length: k }, () => 1 / k);

// ---- sums ----------------------------------------------------------------------

/**
 * The correctly rounded sum of `xs` (Shewchuk's algorithm, as Python's
 * math.fsum). Its result depends only on the exact sum, never on how the
 * terms are grouped, which is what keeps a halved sibling's neighbours still.
 */
function fsum(xs: readonly number[]): number {
  const partials: number[] = [];
  for (let v of xs) {
    let i = 0;
    for (let j = 0; j < partials.length; j++) {
      let y = partials[j];
      if (Math.abs(v) < Math.abs(y)) [v, y] = [y, v];
      const hi = v + y;
      const lo = y - (hi - v);
      if (lo !== 0) partials[i++] = lo;
      v = hi;
    }
    partials.length = i;
    partials.push(v);
  }
  let n = partials.length;
  if (n === 0) return 0;
  let hi = partials[--n];
  let lo = 0;
  while (n > 0) {
    const v = hi;
    const y = partials[--n];
    hi = v + y;
    lo = y - (hi - v);
    if (lo !== 0) break;
  }
  // Round half to even across the remaining partials, as math.fsum does.
  if (n > 0 && ((lo < 0 && partials[n - 1] < 0) || (lo > 0 && partials[n - 1] > 0))) {
    const y = lo * 2;
    const v = hi + y;
    if (y === v - hi) hi = v;
  }
  return hi;
}

/** Each child's offset in its node: the exact prefix sums of `sizes`, correctly rounded. */
function offsets(sizes: readonly number[]): number[] {
  return sizes.map((_, i) => fsum(sizes.slice(0, i)));
}

/** `sizes` summing to 1: as they are when they already do (within tolerance), else divided out. */
function unitSizes(sizes: readonly number[]): number[] {
  const sum = fsum(sizes);
  return Math.abs(sum - 1) <= SUM_TOLERANCE ? sizes.slice() : sizes.map((s) => s / sum);
}

// ---- reading a tree --------------------------------------------------------------

/** The panes, depth first: x children left to right, y children top to bottom (reading order). */
export function leaves(n: LayoutNode): PaneNo[] {
  return isLeaf(n) ? [n.pane] : n.children.flatMap(leaves);
}

/** The child indices from the root to `pane`'s leaf: [] for the root leaf, null when absent. */
export function pathOf(n: LayoutNode, pane: PaneNo): Path | null {
  if (isLeaf(n)) return n.pane === pane ? [] : null;
  for (let i = 0; i < n.children.length; i++) {
    const p = pathOf(n.children[i], pane);
    if (p) return [i, ...p];
  }
  return null;
}

function nodeAt(n: LayoutNode, path: Path): LayoutNode | null {
  let at: LayoutNode = n;
  for (const i of path) {
    if (isLeaf(at) || i < 0 || i >= at.children.length) return null;
    at = at.children[i];
  }
  return at;
}

/** Each child's rect inside its node's rect `r`, from the node's own fractions. */
function childRects(n: SplitNode, r: Rect): Rect[] {
  const at = offsets(n.sizes);
  return n.sizes.map((s, i) =>
    n.axis === "x"
      ? { left: r.left + r.width * at[i], top: r.top, width: r.width * s, height: r.height }
      : { left: r.left, top: r.top + r.height * at[i], width: r.width, height: r.height * s });
}

const UNIT: Rect = { left: 0, top: 0, width: 1, height: 1 };

/** Every pane's rect, computed top down: a pane's rect reads only its ancestors' sizes (C5, §5.3). */
export function rectsOf(n: LayoutNode): Partial<Record<PaneNo, Rect>> {
  const out: Partial<Record<PaneNo, Rect>> = {};
  const walk = (m: LayoutNode, r: Rect) => {
    if (isLeaf(m)) {
      out[m.pane] = r;
      return;
    }
    const rs = childRects(m, r);
    m.children.forEach((c, i) => walk(c, rs[i]));
  };
  walk(n, UNIT);
  return out;
}

// ---- normal form -----------------------------------------------------------------

/**
 * The tree in normal form: one-child nodes collapsed, a child of its parent's
 * axis flattened in (its sizes scaled by its share), sizes summing to 1. A
 * normal tree comes back equal, sizes value for value.
 */
export function normalize(n: LayoutNode): LayoutNode {
  if (isLeaf(n)) return { pane: n.pane };
  if (n.children.length === 0) return n;
  const sizes = unitSizes(n.sizes);
  const children: LayoutNode[] = [];
  const out: number[] = [];
  n.children.forEach((c, i) => {
    const m = normalize(c);
    if (!isLeaf(m) && m.axis === n.axis) {
      m.children.forEach((g, j) => {
        children.push(g);
        out.push(m.sizes[j] * sizes[i]);
      });
    } else {
      children.push(m);
      out.push(sizes[i]);
    }
  });
  if (children.length === 1) return children[0];
  return { axis: n.axis, children, sizes: out };
}

// ---- structural minimums (§4.4) --------------------------------------------------------

/** The most panes side by side any sizes can make: x sums its children, y takes the widest. */
export function acrossS(n: LayoutNode): number {
  if (isLeaf(n)) return 1;
  const k = n.children.map(acrossS);
  return n.axis === "x" ? k.reduce((a, b) => a + b, 0) : Math.max(...k);
}

/** The same down: y sums its children, x takes the tallest. */
export function downS(n: LayoutNode): number {
  if (isLeaf(n)) return 1;
  const k = n.children.map(downS);
  return n.axis === "y" ? k.reduce((a, b) => a + b, 0) : Math.max(...k);
}

/** The least width at which some sizes give every pane its minimum (a lone column: MAIN_MIN). */
export function minW(n: LayoutNode): number {
  const k = acrossS(n);
  return k === 1 ? MAIN_MIN : PANE_MIN_W * k;
}

/** The least height at which some sizes give every pane its minimum. */
export function minH(n: LayoutNode): number {
  return PANE_MIN_H * downS(n);
}

const countOf = (n: LayoutNode, axis: Axis) => (axis === "x" ? acrossS(n) : downS(n));

// ---- the 4-across / 3-down rule (§4.1) ---------------------------------------------------

/**
 * The most panes one line crosses inside the open band (from, to): for "x",
 * horizontal lines at heights in the band; for "y", vertical lines. A line
 * crosses a pane only strictly inside it, so a line on an edge crosses nothing
 * there, and edges within SAME_EDGE are one edge (no sliver between them is
 * counted). Testing the midpoints between consecutive distinct edges is
 * enough, since the count only changes at an edge.
 */
export function crossing(rects: Partial<Record<PaneNo, Rect>>, axis: Axis, from: number, to: number): number {
  const spans: [number, number][] = [];
  for (const r of Object.values(rects) as Rect[]) {
    spans.push(axis === "x" ? [r.top, r.top + r.height] : [r.left, r.left + r.width]);
  }
  // Group the edges (and the band's ends) so that close ones snap to one value.
  const edges = [from, to, ...spans.flat()].filter(Number.isFinite).sort((a, b) => a - b);
  const reps: number[] = [];
  for (const e of edges) if (!reps.length || e - reps[reps.length - 1] >= SAME_EDGE) reps.push(e);
  // A group spans [rep, rep + SAME_EDGE) and the next group starts past it, so
  // a value belongs to the last group starting at or below it.
  const snap = (v: number) => {
    let best = reps[0];
    for (const r of reps) if (r <= v) best = r;
    return best;
  };
  const lo = snap(from), hi = snap(to);
  const snapped = spans.map(([a, b]) => [snap(a), snap(b)] as const);
  let most = 0;
  for (let i = 0; i + 1 < reps.length; i++) {
    const line = (reps[i] + reps[i + 1]) / 2;
    if (line <= lo || line >= hi) continue;
    let n = 0;
    for (const [a, b] of snapped) if (a < line && line < b) n++;
    most = Math.max(most, n);
  }
  return most;
}

// ---- editing (each returns a new tree; inputs are never mutated) ----------------------------

/** `n` with the node at `path` replaced by `make(it)`, every node on the way copied. */
function replaceAt(n: LayoutNode, path: Path, make: (at: LayoutNode) => LayoutNode): LayoutNode {
  if (path.length === 0) return make(n);
  if (isLeaf(n)) return n;
  const [i, ...rest] = path;
  const children = n.children.slice();
  children[i] = replaceAt(children[i], rest, make);
  return { axis: n.axis, children, sizes: n.sizes.slice() };
}

/**
 * Split pane `at` along `axis`, the new pane `id` right of or below it (§5.1).
 * Case B, a parent of the same axis: `id` becomes the next sibling and the two
 * share the source's size, halved exactly. Case A, otherwise: the source's
 * leaf becomes a new node of `axis` holding both, halves equal. Nothing else's
 * size changes. Unchanged when `at` is not in the tree.
 */
export function insertBeside(n: LayoutNode, at: PaneNo, axis: Axis, id: PaneNo): LayoutNode {
  const path = pathOf(n, at);
  if (!path) return n;
  const fresh: LayoutNode = { pane: id };
  if (path.length > 0) {
    const parentPath = path.slice(0, -1);
    const parent = nodeAt(n, parentPath) as SplitNode;
    if (parent.axis === axis) {
      const i = path[path.length - 1];
      return replaceAt(n, parentPath, () => {
        const children = parent.children.slice();
        children.splice(i + 1, 0, fresh);
        const sizes = parent.sizes.slice();
        const half = sizes[i] / 2;
        sizes.splice(i, 1, half, half);
        return { axis, children, sizes };
      });
    }
  }
  return replaceAt(n, path, (leaf) => ({ axis, children: [leaf, fresh], sizes: [0.5, 0.5] }));
}

/** The first leaf of `n` in reading order, or the last. */
const firstLeaf = (n: LayoutNode): PaneNo => (isLeaf(n) ? n.pane : firstLeaf(n.children[0]));
const lastLeaf = (n: LayoutNode): PaneNo => (isLeaf(n) ? n.pane : lastLeaf(n.children[n.children.length - 1]));

/**
 * Close `pane` (§5.2). Its size goes to the previous sibling, or the next one
 * when it was first. A parent left with one child collapses into it, which
 * takes the parent's place and size, its children flattened into the
 * grandparent (scaled by that size) when it has the grandparent's axis. Panes
 * outside the parent keep their rects. `heir` is the receiving sibling's leaf
 * nearest the closed pane: the last leaf of a previous sibling, the first of
 * a next one. Null for the root leaf (the last pane is never closed) or a
 * pane not in the tree.
 */
export function removeLeaf(n: LayoutNode, pane: PaneNo): { tree: LayoutNode; heir: PaneNo } | null {
  const path = pathOf(n, pane);
  if (!path || path.length === 0) return null;
  const parentPath = path.slice(0, -1);
  const parent = nodeAt(n, parentPath) as SplitNode;
  const i = path[path.length - 1];
  const to = i > 0 ? i - 1 : i + 1;
  const heir = to < i ? lastLeaf(parent.children[to]) : firstLeaf(parent.children[to]);

  if (parent.children.length > 2) {
    const tree = replaceAt(n, parentPath, () => {
      const children = parent.children.slice();
      const sizes = parent.sizes.slice();
      sizes[to] = sizes[to] + sizes[i];
      children.splice(i, 1);
      sizes.splice(i, 1);
      return { axis: parent.axis, children, sizes };
    });
    return { tree, heir };
  }

  const survivor = parent.children[to];
  if (parentPath.length === 0) return { tree: survivor, heir };
  const grandPath = parentPath.slice(0, -1);
  const grand = nodeAt(n, grandPath) as SplitNode;
  const p = parentPath[parentPath.length - 1];
  const tree = replaceAt(n, grandPath, () => {
    if (isLeaf(survivor) || survivor.axis !== grand.axis) {
      const children = grand.children.slice();
      children[p] = survivor;
      return { axis: grand.axis, children, sizes: grand.sizes.slice() };
    }
    const share = grand.sizes[p];
    return {
      axis: grand.axis,
      children: [...grand.children.slice(0, p), ...survivor.children, ...grand.children.slice(p + 1)],
      sizes: [...grand.sizes.slice(0, p), ...survivor.sizes.map((s) => s * share), ...grand.sizes.slice(p + 1)],
    };
  });
  return { tree, heir };
}

/** The node at `path` with these sizes, stored as given. Unchanged when `path` is not a split node. */
export function withSizes(n: LayoutNode, path: Path, sizes: readonly number[]): LayoutNode {
  const at = nodeAt(n, path);
  if (!at || isLeaf(at)) return n;
  return replaceAt(n, path, () => ({ axis: at.axis, children: at.children.slice(), sizes: sizes.slice() }));
}

// ---- presets (§3.3) ----------------------------------------------------------------

/** Panes per row, top to bottom; null for main2 (one large pane beside a stacked column). */
const PRESET_ROWS: Readonly<Record<Preset, readonly number[] | null>> = {
  single: [1], cols2: [2], rows2: [1, 1], cols3: [3], main2: null,
  grid4: [2, 2], grid5: [3, 2], grid6: [3, 3], grid7: [4, 3], grid8: [4, 4],
};

/**
 * A workspace's edges as WP-A/WP-B stored them for one preset (workspace.ts's
 * v1 `Split`): `cols` the cumulative column edges of the top row (main2: the
 * large pane's right edge), `row` the row edge, and `bottom` the bottom row's
 * own column edges (3 over 2 and 4 over 3 only).
 */
export interface LegacyEdges { cols: readonly number[]; row: number; bottom?: readonly number[] }

/** Cumulative edges as sizes, `[e1, e2 − e1, …, 1 − ek]`; null when they are not `k − 1` increasing fractions. */
function edgeSizes(edges: readonly number[] | undefined, k: number): number[] | null {
  if (!edges || edges.length !== k - 1) return null;
  const at = [0, ...edges, 1];
  const sizes = at.slice(1).map((e, i) => e - at[i]);
  return sizes.every((s) => Number.isFinite(s) && s > 0) ? sizes : null;
}

/**
 * A preset's tree, `ids` in reading order. The grids are rows first, each
 * row its own x node, so depth-first order is the reading order. Equal
 * sizes, except main2's 0.6 / 0.4.
 *
 * With `edges` (§8.2, a v1 workspace migrating) the sizes come from them
 * instead: cols2/cols3 and every grid's top row from `cols`; the bottom row
 * from `bottom` in 3 over 2 and 4 over 3, else from `cols` too (the rows were
 * linked); the root y of every two-row shape and main2's stack `[row, 1 −
 * row]`; main2's x from `cols`. An edge list of the wrong shape leaves that
 * node at its default.
 */
export function presetTree(p: Preset, ids: readonly PaneNo[], edges?: LegacyEdges): LayoutNode {
  let next = 0;
  const take = (): LayoutNode => ({ pane: ids[next++] });
  const row = (k: number, own?: readonly number[]): LayoutNode => {
    if (k === 1) return take();
    return { axis: "x", children: Array.from({ length: k }, take), sizes: edgeSizes(own, k) ?? equal(k) };
  };
  const rowEdge = edges && Number.isFinite(edges.row) && edges.row > 0 && edges.row < 1 ? [edges.row] : undefined;
  const rows = PRESET_ROWS[p];
  if (!rows) {
    const main = take();
    const stack: LayoutNode = { axis: "y", children: [take(), take()], sizes: edgeSizes(rowEdge, 2) ?? [0.5, 0.5] };
    return { axis: "x", children: [main, stack], sizes: edgeSizes(edges?.cols, 2) ?? [0.6, 0.4] };
  }
  if (rows.length === 1) return row(rows[0], edges?.cols);
  const bottom = rows[1] !== rows[0] ? edges?.bottom : edges?.cols;
  return {
    axis: "y",
    children: rows.map((k, i) => row(k, i === 0 ? edges?.cols : bottom)),
    sizes: (rows.length === 2 ? edgeSizes(rowEdge, 2) : null) ?? equal(rows.length),
  };
}

/** Structure only: no ids, no sizes. */
const structureKey = (n: LayoutNode): string =>
  isLeaf(n) ? "." : `${n.axis}[${n.children.map(structureKey).join(",")}]`;

let presetKeys: Map<string, Preset> | null = null;

/** The preset whose structure this is, sizes and ids ignored; null for any other tree. */
export function presetOf(n: LayoutNode): Preset | null {
  if (!presetKeys) {
    presetKeys = new Map();
    for (const p of Object.keys(PRESET_ROWS) as Preset[]) {
      const count = PRESET_ROWS[p]?.reduce((a, b) => a + b, 0) ?? 3;
      const ids = Array.from({ length: count }, (_, i) => (i + 1) as PaneNo);
      presetKeys.set(structureKey(presetTree(p, ids)), p);
    }
  }
  return presetKeys.get(structureKey(n)) ?? null;
}

/** Structure and ids, never sizes: what the sticky drawn set is keyed by (§7.2). */
export function shapeKey(n: LayoutNode): string {
  return isLeaf(n) ? String(n.pane) : `${n.axis}[${n.children.map(shapeKey).join(",")}]`;
}

// ---- dropping, render only (§7.1) ----------------------------------------------------------

/** Where an interval lies along one axis. */
type Span = readonly [number, number];
const spanOf = (r: Rect, axis: Axis): Span =>
  axis === "x" ? [r.left, r.left + r.width] : [r.top, r.top + r.height];
const overlaps = (a: Span, b: Span) => a[0] < b[1] - SAME_EDGE && b[0] < a[1] - SAME_EDGE;
const gap = (a: Span, b: Span) => Math.max(0, a[0] - b[1], b[0] - a[1]);

/**
 * A render tree with at most `t` panes side by side (`axis` "x", acrossS) or
 * stacked ("y", downS), never without `focus`. A node across the axis prunes
 * every child; a node along it keeps the leftmost (topmost) maximal run of
 * children that fits, holding its anchor: the child with the focus when the
 * focus is inside it, else the first child overlapping the focused pane along
 * the axis, else the nearest. A child too big alone is kept and pruned
 * itself. Kept sizes keep their proportions. Unchanged when it already fits.
 */
export function prune(n: LayoutNode, axis: Axis, t: number, focus: PaneNo): LayoutNode {
  const focused = rectsOf(n)[focus];
  const want = focused ? spanOf(focused, axis) : null;

  const walk = (m: LayoutNode, r: Rect): LayoutNode => {
    if (isLeaf(m) || countOf(m, axis) <= t) return m;
    const rs = childRects(m, r);
    if (m.axis !== axis) {
      return normalize({ axis: m.axis, children: m.children.map((c, i) => walk(c, rs[i])), sizes: m.sizes.slice() });
    }
    const counts = m.children.map((c) => countOf(c, axis));
    let j = m.children.findIndex((c) => pathOf(c, focus) !== null);
    if (j < 0 && want) j = rs.findIndex((cr) => overlaps(spanOf(cr, axis), want));
    if (j < 0) {
      j = 0;
      if (want) rs.forEach((cr, i) => { if (gap(spanOf(cr, axis), want) < gap(spanOf(rs[j], axis), want)) j = i; });
    }
    if (counts[j] > t) return walk(m.children[j], rs[j]);
    // The leftmost start whose run up to j fits, then as far right as still fits.
    let a = j;
    let sum = counts[j];
    while (a > 0 && sum + counts[a - 1] <= t) sum += counts[--a];
    let b = j;
    while (b + 1 < counts.length && sum + counts[b + 1] <= t) sum += counts[++b];
    if (a === b) return m.children[a];
    const kept = m.sizes.slice(a, b + 1);
    const total = fsum(kept);
    return normalize({ axis: m.axis, children: m.children.slice(a, b + 1), sizes: kept.map((s) => s / total) });
  };
  return walk(n, UNIT);
}

// ---- the clamp (§7.3) ------------------------------------------------------------------

/**
 * One node's sizes fitted to its `length` px: every child at least its own
 * minimum (`mins`, px), as close to the stored fractions as that allows, by
 * pushing edges (forward, then backward), as clampCols does with one minimum.
 * Minimal: when every child already meets its minimum the sizes come back
 * value for value (C4, §5.3), and a child beyond the push keeps its size.
 * Below the minimums' sum, sizes in proportion to the minimums. No measured
 * length: unchanged.
 */
export function clampSizes(sizes: readonly number[], mins: readonly number[], length: number): number[] {
  if (!Number.isFinite(length) || length <= 0) return sizes.slice();
  const need = fsum(mins);
  if (length < need) return mins.map((m) => m / need);
  if (sizes.every((s, i) => s * length >= mins[i])) return sizes.slice();
  const px = offsets(sizes).slice(1).map((o) => o * length);
  for (let i = 0; i < px.length; i++) px[i] = Math.max(px[i], (i === 0 ? 0 : px[i - 1]) + mins[i]);
  for (let i = px.length - 1; i >= 0; i--) px[i] = Math.min(px[i], (i === px.length - 1 ? length : px[i + 1]) - mins[i + 1]);
  const at = [0, ...px, length];
  return sizes.map((_, i) => (at[i + 1] - at[i]) / length);
}

// ---- edges (§9) ------------------------------------------------------------------------

export interface TreeEdge {
  /** `split-<axis><path digits>-<i>`, the first member's. */
  testid: string;
  /** The way it moves: "x" is a vertical separator. */
  axis: Axis;
  /** The node boundaries it moves: between children i−1 and i of the node at `path`. */
  members: { path: Path; i: number }[];
  /** Where it is drawn, in unit fractions: zero wide (x) or zero tall (y). */
  at: Rect;
}

/**
 * Every separator, from the tree's own sizes: one per boundary between
 * adjacent children of a node, spanning the node's cross extent. Boundaries
 * of consecutive children of one node that coincide (within SAME_EDGE) are
 * one linked separator (P-5), such as grid4's shared column edge; it carries
 * the first member's test id and spans every member.
 */
export function edgesOf(n: LayoutNode): TreeEdge[] {
  const out: TreeEdge[] = [];
  /** The separators each node's boundaries landed in, by path, with their positions. */
  const placed = new Map<string, { pos: number; edge: TreeEdge }[]>();

  const own = (m: SplitNode, path: number[], r: Rect, prev: { pos: number; edge: TreeEdge }[] | null) => {
    const at = offsets(m.sizes);
    const mine: { pos: number; edge: TreeEdge }[] = [];
    const taken = new Set<TreeEdge>();
    for (let i = 1; i < m.children.length; i++) {
      const pos = m.axis === "x" ? r.left + r.width * at[i] : r.top + r.height * at[i];
      const link = prev?.find((p) => !taken.has(p.edge) && Math.abs(p.pos - pos) < SAME_EDGE);
      if (link) {
        taken.add(link.edge);
        link.edge.members.push({ path, i });
        const a = link.edge.at;
        link.edge.at = m.axis === "x"
          ? { ...a, height: r.top + r.height - a.top }
          : { ...a, width: r.left + r.width - a.left };
        mine.push({ pos, edge: link.edge });
        continue;
      }
      const edge: TreeEdge = {
        testid: `split-${m.axis}${path.join("")}-${i}`,
        axis: m.axis,
        members: [{ path, i }],
        at: m.axis === "x"
          ? { left: pos, top: r.top, width: 0, height: r.height }
          : { left: r.left, top: pos, width: r.width, height: 0 },
      };
      out.push(edge);
      mine.push({ pos, edge });
    }
    placed.set(path.join(","), mine);
  };

  const walk = (m: LayoutNode, path: number[], r: Rect, prev: { pos: number; edge: TreeEdge }[] | null) => {
    if (isLeaf(m)) return;
    own(m, path, r, prev);
    const rs = childRects(m, r);
    let before: { pos: number; edge: TreeEdge }[] | null = null;
    m.children.forEach((c, i) => {
      const p = [...path, i];
      walk(c, p, rs[i], before);
      // Only the next child links to this one's boundaries; a leaf breaks the run.
      before = isLeaf(c) ? null : placed.get(p.join(",")) ?? null;
    });
  };
  walk(n, [], UNIT, null);
  return out;
}

// ---- persistence (§8.3) -------------------------------------------------------------------

/** An own property only, so nothing inherited (or a `__proto__` key) is ever read as a field. */
const own = (o: object, k: string): unknown =>
  Object.prototype.hasOwnProperty.call(o, k) ? (o as Record<string, unknown>)[k] : undefined;

/**
 * A stored tree, or null when it is not one. Caps reject the whole tree:
 * depth over MAX_DEPTH (a counter, so a deep or cyclic value cannot exhaust
 * the stack), more than 23 nodes, a children array longer than its axis
 * allows (checked before it is read), a leaf not an integer 1–12, a duplicate
 * leaf, an axis not "x"/"y". Bad sizes fall back to equal sizes for that node
 * only. Unknown keys are ignored and raw objects are never spread. FS-3 is
 * not checked: a layout over the cap is drawn as it is, never lost on reload.
 */
export function parseTree(raw: unknown): LayoutNode | null {
  const seen = new Set<number>();
  let nodes = 0;
  const read = (v: unknown, depth: number): LayoutNode | null => {
    if (depth > MAX_DEPTH || ++nodes > MAX_NODES) return null;
    if (typeof v === "number") {
      if (!Number.isInteger(v) || v < 1 || v > MAX_LEAVES || seen.has(v)) return null;
      seen.add(v);
      return { pane: v as PaneNo };
    }
    if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
    const axis = own(v, "axis");
    if (axis !== "x" && axis !== "y") return null;
    const kids = own(v, "children");
    if (!Array.isArray(kids)) return null;
    const k = kids.length;
    if (k < 1 || k > MAX_CHILDREN[axis]) return null;
    const children: LayoutNode[] = [];
    for (let i = 0; i < k; i++) {
      const c = read(kids[i], depth + 1);
      if (!c) return null;
      children.push(c);
    }
    return { axis, children, sizes: readSizes(own(v, "sizes"), k) };
  };
  try {
    const t = read(raw, 1);
    return t ? normalize(t) : null;
  } catch {
    return null; // a throwing getter or proxy is not a tree
  }
}

/** A node's stored sizes, summing to 1; equal sizes when they are not `k` finite positive numbers. */
function readSizes(raw: unknown, k: number): number[] {
  if (!Array.isArray(raw) || raw.length !== k) return equal(k);
  const sizes: number[] = [];
  for (let i = 0; i < k; i++) {
    const s: unknown = raw[i];
    if (typeof s !== "number" || !Number.isFinite(s) || s <= 0) return equal(k);
    sizes.push(s);
  }
  const sum = sizes.reduce((a, b) => a + b, 0);
  return Number.isFinite(sum) ? unitSizes(sizes) : equal(k);
}

/** What is persisted: leaves as bare numbers, sizes as they are. */
export function storeTree(n: LayoutNode): StoredNode {
  return isLeaf(n) ? n.pane : { axis: n.axis, children: n.children.map(storeTree), sizes: n.sizes.slice() };
}
