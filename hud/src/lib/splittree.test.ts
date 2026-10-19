// FS1, the pure split tree (docs/plans/2026-10-19-hud-free-split-plan.md §3.1,
// with §3.3, §4, §5.1–5.3, §7.1–7.3, §8.3 and §9). Written before the module
// exists: until `./splittree` is implemented this file fails on its import.
//
// ---- The contract these tests assume (one list for the implementer) ---------
//
// From §3.1, as the plan spells them:
//   type Axis = "x" | "y";
//   interface Leaf { pane: PaneNo }
//   interface SplitNode { axis: Axis; children: LayoutNode[]; sizes: number[] }
//   type LayoutNode = Leaf | SplitNode;
//   type Path = readonly number[];
//   interface Rect { left: number; top: number; width: number; height: number }
//   type StoredNode = number | { axis: Axis; children: StoredNode[]; sizes: number[] };
//   const MAX_ACROSS = 4, MAX_DOWN = 3, MAX_LEAVES = 12, MAX_DEPTH = 6, SAME_EDGE = 1e-4;
//   leaves(n: LayoutNode): PaneNo[]
//   normalize(n: LayoutNode): LayoutNode
//   pathOf(n: LayoutNode, pane: PaneNo): Path | null
//   rectsOf(n: LayoutNode): Partial<Record<PaneNo, Rect>>
//   acrossS(n: LayoutNode): number
//   downS(n: LayoutNode): number
//   crossing(rects: Partial<Record<PaneNo, Rect>>, axis: Axis, from: number, to: number): number
//       axis "x": the most panes any horizontal line y ∈ (from, to) crosses;
//       axis "y": the same for vertical lines x ∈ (from, to). Open band, strict
//       containment, edges within SAME_EDGE are one edge (§4.1).
//   insertBeside(n: LayoutNode, at: PaneNo, axis: Axis, id: PaneNo): LayoutNode
//   removeLeaf(n: LayoutNode, pane: PaneNo): { tree: LayoutNode; heir: PaneNo } | null
//       heir: the receiving sibling's leaf nearest the closed pane (the last
//       leaf, in reading order, of a previous sibling; the first leaf of a next
//       one). null for the root leaf.
//   withSizes(n: LayoutNode, path: Path, sizes: readonly number[]): LayoutNode
//   presetTree(p: Preset, ids: readonly PaneNo[]): LayoutNode   (the optional
//       `edges?: LegacyEdges` argument is FS3's and is not exercised here)
//   presetOf(n: LayoutNode): Preset | null
//   shapeKey(n: LayoutNode): string
//   parseTree(raw: unknown): LayoutNode | null
//       depth = nodes on the longest root-to-leaf path, the leaf counted (a
//       bare leaf is depth 1), so MAX_DEPTH 6 allows five nested split nodes.
//   storeTree(n: LayoutNode): StoredNode
//
// Not named by §3.1; these tests fix them (keep them as written):
//   minW(n: LayoutNode): number      §4.4: acrossS === 1 ? MAIN_MIN (480) : 360 · acrossS
//   minH(n: LayoutNode): number      §4.4: 200 · downS
//   prune(n: LayoutNode, axis: Axis, t: number, focus: PaneNo): LayoutNode
//       §7.1: a render tree (a LayoutNode, normalised, sizes renormalised in
//       proportion) with acrossS ≤ t for "x" (downS ≤ t for "y"), never
//       without `focus`. A leaf is always kept. Unchanged when already ≤ t.
//   clampSizes(sizes: readonly number[], mins: readonly number[], length: number): number[]
//       §7.3: one node's clamp. `mins` are its children's minimums in px
//       (acrossS(child)·360 or downS(child)·200), `length` its px extent.
//       Edge-push (forward, then backward); returns `sizes` itself, value for
//       value, when every child already meets its minimum (C4); below Σ mins,
//       sizes in proportion to the mins; a non-finite or ≤ 0 length: unchanged.
//   interface TreeEdge {
//     testid: string;                       // §9: split-<axis><path digits>-<i>
//     axis: Axis;                           // the way it moves: "x" is a vertical separator
//     members: { path: Path; i: number }[]; // node boundaries it moves; i as in the testid
//                                           // (the boundary between children i−1 and i)
//     at: Rect;                             // where it is drawn (unit fractions)
//   }
//   edgesOf(n: LayoutNode): TreeEdge[]     §9 with P-5's linking, from the tree's own sizes
//
// Sizes are kept value for value wherever nothing asks them to change:
// normalize, parseTree, prune (a node it keeps whole) and storeTree never
// rescale a node whose sizes already sum to 1 within 1e-9; only other sums
// are divided out. withSizes stores the sizes it is given. (Otherwise every
// pass would move panes by an ulp, and the identities below could not hold.)
//
// Pane ids run 1–12 here although workspace.ts's PaneNo is still 1–8 until
// FS2; ids reach the module through `P()`, a plain cast.
//
// Bit-identity (the insertBeside property) is asked of every other leaf's rect.
// In case B the siblings after the source see a prefix sum regrouped from
// s to s/2 + s/2, so rectsOf's offsets must not depend on how the prefix is
// grouped (for instance a correctly rounded running sum, like Python's
// math.fsum, rather than a naive left-to-right one).
//
// Each test names, in a [bracketed] comment, the mutation it is meant to catch
// where the plan names one.

import { describe, expect, it } from "vitest";
import { PRESETS, type PaneNo, type Preset } from "./workspace";
import {
  MAX_ACROSS, MAX_DEPTH, MAX_DOWN, MAX_LEAVES, SAME_EDGE,
  acrossS, clampSizes, crossing, downS, edgesOf, insertBeside, leaves, minH, minW, normalize, parseTree, pathOf,
  presetOf, presetTree, prune, rectsOf, removeLeaf, shapeKey, storeTree, withSizes,
  type Axis, type LayoutNode, type Path, type Rect, type SplitNode,
} from "./splittree";

// ---- helpers --------------------------------------------------------------------

const P = (n: number) => n as PaneNo;
const ids = (...n: number[]) => n.map(P);
type Kid = number | LayoutNode;
const kid = (c: Kid): LayoutNode => (typeof c === "number" ? { pane: P(c) } : c);
const node = (axis: Axis, kids: Kid[], sizes?: number[]): SplitNode =>
  ({ axis, children: kids.map(kid), sizes: sizes ?? kids.map(() => 1 / kids.length) });
const x = (...k: Kid[]) => node("x", k);
const y = (...k: Kid[]) => node("y", k);
const xs = (sizes: number[], ...k: Kid[]) => node("x", k, sizes);
const ys = (sizes: number[], ...k: Kid[]) => node("y", k, sizes);
const leaf = (n: number): LayoutNode => ({ pane: P(n) });

const isLeaf = (n: LayoutNode): n is { pane: PaneNo } => "pane" in n;
type Shape = number | { axis: Axis; children: Shape[] };
/** Structure and ids, no sizes. */
const shape = (n: LayoutNode): Shape => (isLeaf(n) ? n.pane : { axis: n.axis, children: n.children.map(shape) });

function nodeAt(n: LayoutNode, path: Path): LayoutNode {
  let at = n;
  for (const i of path) at = (at as SplitNode).children[i];
  return at;
}
function splitPaths(n: LayoutNode, path: number[] = []): number[][] {
  if (isLeaf(n)) return [];
  return [path, ...n.children.flatMap((c, i) => splitPaths(c, [...path, i]))];
}
/** Every split node: ≥ 2 children, never its parent's axis, sizes positive, summing to 1. */
function expectNormal(n: LayoutNode, parent: Axis | null = null) {
  if (isLeaf(n)) return;
  expect(n.children.length).toBeGreaterThanOrEqual(2);
  expect(n.axis).not.toBe(parent);
  expect(n.sizes.length).toBe(n.children.length);
  for (const s of n.sizes) expect(s).toBeGreaterThan(0);
  expect(n.sizes.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
  for (const c of n.children) expectNormal(c, n.axis);
}
function deepFreeze<T>(v: T): T {
  if (v && typeof v === "object") {
    for (const k of Object.keys(v)) deepFreeze((v as Record<string, unknown>)[k]);
    Object.freeze(v);
  }
  return v;
}
const across = (n: LayoutNode) => crossing(rectsOf(n), "x", 0, 1);
const down = (n: LayoutNode) => crossing(rectsOf(n), "y", 0, 1);
/** The FS-3 verdict of §4.1, built from `crossing`: split right / down of `pane`. */
function canSplit(n: LayoutNode, pane: PaneNo, axis: Axis): boolean {
  const rects = rectsOf(n);
  const r = rects[pane]!;
  return axis === "x"
    ? crossing(rects, "x", r.top, r.top + r.height) <= MAX_ACROSS - 1
    : crossing(rects, "y", r.left, r.left + r.width) <= MAX_DOWN - 1;
}
function freeId(n: LayoutNode): PaneNo | null {
  const used = new Set<number>(leaves(n));
  for (let i = 1; i <= 12; i++) if (!used.has(i)) return P(i);
  return null;
}
/** Split as the owner would: the verdict must allow it, and the new pane takes the lowest free id. */
function split(n: LayoutNode, pane: number, axis: Axis): LayoutNode {
  expect(canSplit(n, P(pane), axis), `split ${axis} of ${pane} in ${JSON.stringify(shape(n))}`).toBe(true);
  return insertBeside(n, P(pane), axis, freeId(n)!);
}
const expectRect = (r: Rect | undefined, want: Rect) => {
  expect(r).toBeDefined();
  expect(r!.left).toBeCloseTo(want.left, 12);
  expect(r!.top).toBeCloseTo(want.top, 12);
  expect(r!.width).toBeCloseTo(want.width, 12);
  expect(r!.height).toBeCloseTo(want.height, 12);
};

/** mulberry32: a small seeded PRNG, so every property run is the same run. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = <T,>(rand: () => number, xs: readonly T[]): T => xs[Math.floor(rand() * xs.length)];
function randomSizes(rand: () => number, k: number): number[] {
  const raw = Array.from({ length: k }, () => 0.05 + rand());
  const sum = raw.reduce((a, b) => a + b, 0);
  return raw.map((v) => v / sum);
}
/**
 * A tree the owner could reach: from one pane, splits the FS-3 verdict allows,
 * drags (new sizes on a node) and closes, in random order.
 */
function reachable(rand: () => number, steps: number): LayoutNode {
  let t: LayoutNode = leaf(1);
  for (let s = 0; s < steps; s++) {
    const r = rand();
    const ls = leaves(t);
    if (r < 0.6) {
      const id = freeId(t);
      const pane = pick(rand, ls);
      const axis: Axis = rand() < 0.5 ? "x" : "y";
      if (id !== null && canSplit(t, pane, axis)) t = insertBeside(t, pane, axis, id);
    } else if (r < 0.85) {
      const paths = splitPaths(t);
      if (!paths.length) continue;
      const path = pick(rand, paths);
      t = withSizes(t, path, randomSizes(rand, (nodeAt(t, path) as SplitNode).children.length));
    } else if (ls.length > 1) {
      t = removeLeaf(t, pick(rand, ls))!.tree;
    }
  }
  return t;
}

// The ten presets as §3.3's table draws them, ids in reading order.
const PRESET_SHAPES: Record<Preset, Shape> = {
  single: 1,
  cols2: { axis: "x", children: [1, 2] },
  rows2: { axis: "y", children: [1, 2] },
  cols3: { axis: "x", children: [1, 2, 3] },
  main2: { axis: "x", children: [1, { axis: "y", children: [2, 3] }] },
  grid4: { axis: "y", children: [{ axis: "x", children: [1, 2] }, { axis: "x", children: [3, 4] }] },
  grid5: { axis: "y", children: [{ axis: "x", children: [1, 2, 3] }, { axis: "x", children: [4, 5] }] },
  grid6: { axis: "y", children: [{ axis: "x", children: [1, 2, 3] }, { axis: "x", children: [4, 5, 6] }] },
  grid7: { axis: "y", children: [{ axis: "x", children: [1, 2, 3, 4] }, { axis: "x", children: [5, 6, 7] }] },
  grid8: { axis: "y", children: [{ axis: "x", children: [1, 2, 3, 4] }, { axis: "x", children: [5, 6, 7, 8] }] },
};
const COUNT: Record<Preset, number> = {
  single: 1, cols2: 2, rows2: 2, cols3: 3, main2: 3, grid4: 4, grid5: 5, grid6: 6, grid7: 7, grid8: 8,
};
const preset = (p: Preset, order?: number[]) =>
  presetTree(p, order ? ids(...order) : ids(...Array.from({ length: COUNT[p] }, (_, i) => i + 1)));

// ---- constants and basics ------------------------------------------------------------

describe("the constants (§3.1)", () => {
  it("pins the caps and the edge tolerance", () => {
    expect([MAX_ACROSS, MAX_DOWN, MAX_LEAVES, MAX_DEPTH, SAME_EDGE]).toEqual([4, 3, 12, 6, 1e-4]);
  });
});

describe("leaves, pathOf and rectsOf", () => {
  it("lists leaves depth first: x children left to right, y children top to bottom (P-2)", () => {
    expect(leaves(leaf(7))).toEqual([7]);
    expect(leaves(x(y(1, 2), 3))).toEqual([1, 2, 3]);
    expect(leaves(y(x(4, y(9, 2)), 12, x(1, 3)))).toEqual([4, 9, 2, 12, 1, 3]);
  });

  it("finds a pane's path, [] for the root leaf, null when absent", () => {
    const t = x(1, y(2, 3));
    expect(pathOf(t, P(3))).toEqual([1, 1]);
    expect(pathOf(t, P(1))).toEqual([0]);
    expect(pathOf(leaf(5), P(5))).toEqual([]);
    expect(pathOf(t, P(4))).toBeNull();
  });

  it("draws every pane from its node's fractions, top down", () => {
    const r = rectsOf(preset("main2"));
    expectRect(r[P(1)], { left: 0, top: 0, width: 0.6, height: 1 });
    expectRect(r[P(2)], { left: 0.6, top: 0, width: 0.4, height: 0.5 });
    expectRect(r[P(3)], { left: 0.6, top: 0.5, width: 0.4, height: 0.5 });
    expectRect(rectsOf(leaf(4))[P(4)], { left: 0, top: 0, width: 1, height: 1 });
    expect(Object.keys(rectsOf(preset("grid5"))).map(Number).sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5]);
  });
});

// ---- normalize ----------------------------------------------------------------------

describe("normalize flattens and collapses", () => {
  it("flattens a child of its parent's axis, scaling its sizes by its share", () => {
    const t = normalize(xs([0.4, 0.6], 1, xs([0.25, 0.75], 2, 3)));
    expect(shape(t)).toEqual({ axis: "x", children: [1, 2, 3] });
    const s = (t as SplitNode).sizes;
    expect(s[0]).toBeCloseTo(0.4, 12);
    expect(s[1]).toBeCloseTo(0.15, 12);
    expect(s[2]).toBeCloseTo(0.45, 12);
  });

  it("collapses one-child nodes, then flattens what that exposes", () => {
    expect(normalize(x(leaf(1)))).toEqual(leaf(1));
    expect(shape(normalize(y(x(1, 2))))).toEqual({ axis: "x", children: [1, 2] });
    // y[x[2,3]] collapses to x[2,3], which now sits in an x parent and is flattened in.
    const t = normalize(xs([0.5, 0.5], 1, node("y", [xs([0.5, 0.5], 2, 3)], [1])));
    expect(shape(t)).toEqual({ axis: "x", children: [1, 2, 3] });
    expect((t as SplitNode).sizes.map((v) => +v.toFixed(12))).toEqual([0.5, 0.25, 0.25]);
    expect(shape(normalize(x(x(x(1, 2), 3), 4)))).toEqual({ axis: "x", children: [1, 2, 3, 4] });
  });

  it("renormalises sizes to sum 1, and leaves a normal tree as it is", () => {
    const t = normalize(xs([2, 6], 1, 2)) as SplitNode;
    expect(t.sizes[0]).toBeCloseTo(0.25, 12);
    expect(t.sizes[1]).toBeCloseTo(0.75, 12);
    for (const p of PRESETS) expect(normalize(preset(p))).toEqual(preset(p));
    const rand = rng(11);
    for (let i = 0; i < 200; i++) {
      const r = reachable(rand, Math.floor(rand() * 20));
      expect(normalize(r)).toEqual(r);
    }
  });
});

// ---- presets ------------------------------------------------------------------------

describe("presetTree and presetOf (§3.3)", () => {
  it("builds each preset's tree, rows first, ids in reading order", () => {
    for (const p of PRESETS) expect(shape(preset(p)), p).toEqual(PRESET_SHAPES[p]);
    expect(leaves(preset("grid5", [5, 3, 1, 2, 4]))).toEqual([5, 3, 1, 2, 4]);
  });

  it("uses equal sizes, except main2's 0.6 / 0.4", () => {
    const m = preset("main2") as SplitNode;
    expect(m.sizes[0]).toBeCloseTo(0.6, 12);
    expect(m.sizes[1]).toBeCloseTo(0.4, 12);
    expect((m.children[1] as SplitNode).sizes).toEqual([0.5, 0.5]);
    const g = preset("grid7") as SplitNode;
    expect(g.sizes).toEqual([0.5, 0.5]);
    expect((g.children[0] as SplitNode).sizes).toEqual([0.25, 0.25, 0.25, 0.25]);
    for (const s of (g.children[1] as SplitNode).sizes) expect(s).toBeCloseTo(1 / 3, 12);
  });

  it("round-trips all ten presets, whatever the ids and the sizes", () => {
    const rand = rng(3);
    for (const p of PRESETS) {
      expect(presetOf(preset(p)), p).toBe(p);
      const reversed = preset(p, Array.from({ length: COUNT[p] }, (_, i) => COUNT[p] - i));
      expect(presetOf(reversed), `${p} reversed`).toBe(p);
      let dragged = preset(p);
      for (const path of splitPaths(dragged)) {
        dragged = withSizes(dragged, path, randomSizes(rand, (nodeAt(dragged, path) as SplitNode).children.length));
      }
      expect(presetOf(dragged), `${p} dragged`).toBe(p);
    }
  });

  it("names no preset for any other structure", () => {
    expect(presetOf(x(1, y(2, 3), 4))).toBeNull();
    expect(presetOf(x(y(1, 2), 3))).toBeNull(); // main2 mirrored
    expect(presetOf(x(y(1, 2), y(3, 4)))).toBeNull(); // 2×2 columns first is not grid4's structure
    expect(presetOf(y(x(1, 2), x(3, 4), x(5, 6)))).toBeNull();
    expect(presetOf(x(1, 2, 3, 4))).toBeNull(); // no four-column preset
    expect(presetOf(y(x(1, 2, 3, 4), x(5, 6, 7, 8), 9))).toBeNull(); // nine panes
    expect(presetOf(y(x(1, 2), 3))).toBeNull();
  });

  it("keys a tree by structure and ids, never by sizes", () => {
    const g = preset("grid4");
    expect(shapeKey(withSizes(g, [0], [0.3, 0.7]))).toBe(shapeKey(g));
    expect(shapeKey(withSizes(g, [], [0.8, 0.2]))).toBe(shapeKey(g));
    expect(shapeKey(preset("grid4", [1, 2, 4, 3]))).not.toBe(shapeKey(g));
    expect(shapeKey(x(1, y(2, 3)))).not.toBe(shapeKey(x(y(1, 2), 3)));
    expect(shapeKey(x(1, 2))).not.toBe(shapeKey(y(1, 2)));
  });
});

// ---- structural minimums ---------------------------------------------------------------

describe("acrossS / downS and minW / minH (§4.4)", () => {
  // Today's SHAPES (workspace.ts) minW / minH, except main2, whose 840 becomes
  // 720 by design (§4.4). Written out so that FS3 removing SHAPES' minimums
  // cannot change what this checks.
  const want: Record<Preset, [number, number, number, number]> = {
    //        acrossS downS minW  minH
    single: [1, 1, 480, 200],
    cols2: [2, 1, 720, 200],
    rows2: [1, 2, 480, 400],
    cols3: [3, 1, 1080, 200],
    main2: [2, 2, 720, 400],
    grid4: [2, 2, 720, 400],
    grid5: [3, 2, 1080, 400],
    grid6: [3, 2, 1080, 400],
    grid7: [4, 2, 1440, 400],
    grid8: [4, 2, 1440, 400],
  };

  it("equals the preset table for every preset", () => {
    for (const p of PRESETS) {
      const t = preset(p);
      expect([acrossS(t), downS(t), minW(t), minH(t)], p).toEqual(want[p]);
    }
  });

  it("sums along a node's axis and takes the max across it", () => {
    const t = x(y(x(1, 2), 3), 4);
    expect(acrossS(t)).toBe(3);
    expect(downS(t)).toBe(2);
    expect(minW(t)).toBe(1080);
    expect(minH(t)).toBe(400);
    const tall = y(1, x(2, y(3, 4)), 5);
    expect(downS(tall)).toBe(4);
    expect(acrossS(tall)).toBe(2);
  });

  it("is never below the geometric count, and equals it on aligned trees", () => {
    for (const p of PRESETS) {
      expect(across(preset(p)), p).toBe(acrossS(preset(p)));
      expect(down(preset(p)), p).toBe(downS(preset(p)));
    }
    const rand = rng(5);
    for (let i = 0; i < 300; i++) {
      const t = reachable(rand, Math.floor(rand() * 25));
      expect(across(t)).toBeLessThanOrEqual(acrossS(t));
      expect(down(t)).toBeLessThanOrEqual(downS(t));
    }
  });
});

// ---- the 4-across / 3-down rule ----------------------------------------------------------

describe("crossing and the FS-3 verdicts: §4.2's seven examples", () => {
  // [counting lines that lie on an edge]: a line on an edge crosses nothing
  // there, and the band is open; counting either way makes these numbers wrong.

  it("1. one pane: every line crosses one; right gives x[1,n], down gives y[1,n]", () => {
    const t = leaf(1);
    expect(across(t)).toBe(1);
    expect(down(t)).toBe(1);
    expect(shape(split(t, 1, "x"))).toEqual({ axis: "x", children: [1, 2] });
    expect(shape(split(t, 1, "y"))).toEqual({ axis: "y", children: [1, 2] });
  });

  it("2. x[1,2,3,4]: right is refused; down on 2 gives x[1,y[2,n],3,4]", () => {
    const t = x(1, 2, 3, 4);
    expect(crossing(rectsOf(t), "x", 0, 1)).toBe(4);
    for (const p of [1, 2, 3, 4]) expect(canSplit(t, P(p), "x")).toBe(false);
    // Vertical lines through pane 2's band (¼, ½) cross pane 2 only; its edges at ¼ and ½ cross nothing.
    expect(crossing(rectsOf(t), "y", 0.25, 0.5)).toBe(1);
    const d = split(t, 2, "y");
    expect(shape(d)).toEqual({ axis: "x", children: [1, { axis: "y", children: [2, 5] }, 3, 4] });
    expect([across(d), down(d)]).toEqual([4, 2]);
  });

  it("3. 4×2: down on 1, then down on 5 refused, down on 6 allowed (geometric, P-3)", () => {
    let t = preset("grid8");
    for (const p of [1, 2, 3, 4, 5, 6, 7, 8]) expect(canSplit(t, P(p), "x"), `right on ${p}`).toBe(false);
    expect(crossing(rectsOf(t), "y", 0, 0.25)).toBe(2);
    t = split(t, 1, "y"); // pane 9
    expect(shape(t)).toEqual({
      axis: "y",
      children: [
        { axis: "x", children: [{ axis: "y", children: [1, 9] }, 2, 3, 4] },
        { axis: "x", children: [5, 6, 7, 8] },
      ],
    });
    expect(crossing(rectsOf(t), "y", 0, 0.25)).toBe(3); // column 1: {1, 9, 5}
    expect(canSplit(t, P(5), "y")).toBe(false);
    expect(crossing(rectsOf(t), "y", 0.25, 0.5)).toBe(2); // {2, 6}: the edge at ¼ is not counted
    t = split(t, 6, "y"); // pane 10
    expect(leaves(t)).toEqual([1, 9, 2, 3, 4, 5, 6, 10, 7, 8]);
    expect(crossing(rectsOf(t), "y", 0.25, 0.5)).toBe(3); // column 2: {2, 6, 10}
    expect([across(t), down(t)]).toEqual([4, 3]);
    expect(downS(t)).toBe(4); // a structural count would have refused the second split
  });

  it("4. 3 over 2: down on 1 allowed, then down on 4 refused, down on 5 allowed", () => {
    let t = preset("grid5");
    expect(canSplit(t, P(1), "y")).toBe(true);
    t = split(t, 1, "y"); // pane 6
    const rects = rectsOf(t);
    // Pane 4's band is (0, ½); its part (0, ⅓) crosses {1, 6, 4}.
    expect(crossing(rects, "y", 0, 0.5)).toBe(3);
    expect(canSplit(t, P(4), "y")).toBe(false);
    // Pane 5's band (½, 1) crosses {2, 5} or {3, 5}.
    expect(crossing(rects, "y", 0.5, 1)).toBe(2);
    expect(canSplit(t, P(5), "y")).toBe(true);
  });

  it("5. staggered x[y[x[a,b], c], d]: top lines cross 3, bottom lines 2; right on d and on c are allowed", () => {
    const t = x(y(x(1, 2), 3), 4);
    expect(crossing(rectsOf(t), "x", 0, 0.5)).toBe(3);
    expect(crossing(rectsOf(t), "x", 0.5, 1)).toBe(2);
    expect(canSplit(t, P(4), "x")).toBe(true); // max 3 over d's band
    expect(canSplit(t, P(3), "x")).toBe(true); // max 2 over c's band
    const d = split(t, 4, "x");
    expect(crossing(rectsOf(d), "x", 0, 0.5)).toBe(4);
    expect(crossing(rectsOf(d), "x", 0.5, 1)).toBe(3);
    // c's band now holds 3, so right on c is still allowed and fills the bottom to 4 too.
    // (The plan's "after both, top = 4 and bottom = 3" holds after the split on d alone.)
    const both = split(d, 3, "x");
    expect(crossing(rectsOf(both), "x", 0, 0.5)).toBe(4);
    expect(crossing(rectsOf(both), "x", 0.5, 1)).toBe(4);
    for (const p of leaves(both)) expect(canSplit(both, p, "x")).toBe(false);
  });

  it("6. the full 4×3 is reachable rows first and columns first; then every split is refused", () => {
    // Rows first: down, down (the second is a sibling insert), then right ×3 in each row.
    let t = split(leaf(1), 1, "y");
    t = split(t, 2, "y");
    expect(shape(t)).toEqual({ axis: "y", children: [1, 2, 3] });
    for (const row of [1, 2, 3]) {
      let last = row;
      for (let k = 0; k < 3; k++) {
        const id = freeId(t)!;
        t = split(t, last, "x");
        last = id;
      }
    }
    const check = (g: LayoutNode) => {
      expect(leaves(g).length).toBe(12);
      expect([across(g), down(g)]).toEqual([4, 3]);
      expect(freeId(g)).toBeNull();
      for (const p of leaves(g)) {
        expect(canSplit(g, p, "x"), `right on ${p}`).toBe(false);
        expect(canSplit(g, p, "y"), `down on ${p}`).toBe(false);
      }
    };
    check(t);
    expect((t as SplitNode).children.every((r) => !isLeaf(r) && r.axis === "x" && r.children.length === 4)).toBe(true);
    // Columns first.
    let c = leaf(1);
    for (const p of [1, 2, 3]) c = split(c, p, "x");
    expect(shape(c)).toEqual({ axis: "x", children: [1, 2, 3, 4] });
    for (const col of [1, 2, 3, 4]) {
      const id = freeId(c)!;
      c = split(c, col, "y");
      c = split(c, id, "y");
    }
    check(c);
  });

  it("7. a line on 2×2's shared column edge crosses nothing; lines either side cross 2", () => {
    const rects = rectsOf(preset("grid4"));
    expect(crossing(rects, "y", 0, 1)).toBe(2);
    expect(crossing(rects, "y", 0.5 - 1e-3, 0.5)).toBe(2);
    expect(crossing(rects, "y", 0.5, 0.5 + 1e-3)).toBe(2);
    // The open band (0, ½) of the top row: its lower end lies on the row edge.
    expect(crossing(rects, "x", 0, 0.5)).toBe(2);
    expect(crossing(rects, "x", 0.5, 1)).toBe(2);
  });
});

describe("crossing at coincident edges (§4.1: edges within SAME_EDGE are one)", () => {
  // Left column: x[1,2] over 3, its row edge at ½ + 5e-5; right column: 4 over
  // x[5,6], its row edge at ½. Counted in the 5e-5 sliver between the two
  // edges, a line would cross {1, 2, 5, 6} = 4; the edges are one, so there is
  // no such line and every line crosses 3.
  const near = xs([0.5, 0.5], ys([0.5 + 5e-5, 0.5 - 5e-5], x(1, 2), 3), y(4, x(5, 6)));
  const apart = xs([0.5, 0.5], ys([0.6, 0.4], x(1, 2), 3), y(4, x(5, 6)));

  it("counts no sliver between coincident edges", () => {
    expect(crossing(rectsOf(near), "x", 0, 1)).toBe(3);
    expect(canSplit(near, P(1), "x")).toBe(true);
    expect(canSplit(near, P(6), "x")).toBe(true);
  });

  it("counts the band between edges that are genuinely apart", () => {
    expect(crossing(rectsOf(apart), "x", 0, 1)).toBe(4);
    expect(crossing(rectsOf(apart), "x", 0.5, 0.6)).toBe(4);
    expect(canSplit(apart, P(1), "x")).toBe(false);
  });

  it("does the same down a window", () => {
    const tall = ys([0.5, 0.5], xs([0.5 + 5e-5, 0.5 - 5e-5], y(1, 2), 3), x(4, y(5, 6)));
    expect(crossing(rectsOf(tall), "y", 0, 1)).toBe(3);
  });
});

// ---- split ---------------------------------------------------------------------------

describe("insertBeside: cases A and B (§5.1)", () => {
  it("case A: the root leaf becomes a new node of the split's axis, halves equal", () => {
    expect(insertBeside(leaf(1), P(1), "x", P(2))).toEqual(xs([0.5, 0.5], 1, 2));
    expect(insertBeside(leaf(3), P(3), "y", P(9))).toEqual(ys([0.5, 0.5], 3, 9));
  });

  it("case A: under a parent of the other axis the leaf is replaced, the parent's sizes untouched", () => {
    expect(insertBeside(xs([0.3, 0.7], 1, 2), P(2), "y", P(3))).toEqual(xs([0.3, 0.7], 1, ys([0.5, 0.5], 2, 3)));
    // 2×2, down on 3: a y node inside the bottom row's x node.
    const g = insertBeside(preset("grid4"), P(3), "y", P(5));
    expect(g).toEqual(ys([0.5, 0.5], xs([0.5, 0.5], 1, 2), xs([0.5, 0.5], ys([0.5, 0.5], 3, 5), 4)));
  });

  it("case B: a same-axis parent gains a sibling right after the source, the source's size halved", () => {
    expect(insertBeside(xs([0.3, 0.7], 1, 2), P(1), "x", P(3))).toEqual(xs([0.3 / 2, 0.3 / 2, 0.7], 1, 3, 2));
    expect(insertBeside(ys([0.2, 0.8], 1, 2), P(2), "y", P(3))).toEqual(ys([0.2, 0.4, 0.4], 1, 2, 3));
    const g5 = preset("grid5") as SplitNode;
    const g = insertBeside(g5, P(4), "x", P(6));
    expect(g).toEqual(ys(g5.sizes, g5.children[0], xs([0.25, 0.25, 0.5], 4, 6, 5)));
  });

  it("never mutates its input", () => {
    const t = deepFreeze(preset("main2"));
    const copy = JSON.parse(JSON.stringify(t));
    insertBeside(t, P(2), "y", P(4));
    insertBeside(t, P(1), "x", P(4));
    expect(t).toEqual(copy);
  });

  it("property: over 2000 random reachable trees every other leaf's rect is bit-identical", () => {
    // [redistributing 1/(k+1) to all siblings]: case B must take the new pane's
    // room from the source only, and nothing outside the source may move by
    // even one ulp (FS-5's === witness, §5.3 (a)).
    const rand = rng(20261019);
    let checked = 0, caseA = 0, caseB = 0;
    while (checked < 2000) {
      const t = reachable(rand, Math.floor(rand() * 24));
      const id = freeId(t);
      if (id === null) continue;
      const pane = pick(rand, leaves(t));
      const axis: Axis = rand() < 0.5 ? "x" : "y";
      if (!canSplit(t, pane, axis)) continue;
      const path = pathOf(t, pane)!;
      const parent = path.length ? (nodeAt(t, path.slice(0, -1)) as SplitNode) : null;
      if (parent && parent.axis === axis) caseB++;
      else caseA++;
      const before = rectsOf(t);
      const after = rectsOf(insertBeside(t, pane, axis, id));
      for (const p of leaves(t)) {
        if (p === pane) continue;
        const a = after[p]!, b = before[p]!;
        const same = a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
        expect(same, `pane ${p} moved when ${pane} split ${axis} in ${JSON.stringify(storeTree(t))}`).toBe(true);
      }
      // The halves share the source's rect.
      const s = before[pane]!, h1 = after[pane]!, h2 = after[id]!;
      const span = axis === "x" ? [s.width / 2, h1.width, h2.width] : [s.height / 2, h1.height, h2.height];
      expect(span[1]).toBeCloseTo(span[0], 12);
      expect(span[2]).toBeCloseTo(span[0], 12);
      checked++;
    }
    expect(caseA).toBeGreaterThan(200);
    expect(caseB).toBeGreaterThan(200);
  }, 60_000);
});

// ---- close -----------------------------------------------------------------------------

describe("removeLeaf (§5.2)", () => {
  it("gives the closed pane's size to the previous sibling, the next one when it was first", () => {
    const t = xs([0.2, 0.3, 0.5], 1, 2, 3);
    const mid = removeLeaf(t, P(2))!;
    expect(shape(mid.tree)).toEqual({ axis: "x", children: [1, 3] });
    expect((mid.tree as SplitNode).sizes[0]).toBeCloseTo(0.5, 12);
    expect((mid.tree as SplitNode).sizes[1]).toBeCloseTo(0.5, 12);
    expect(mid.heir).toBe(1);
    const last = removeLeaf(t, P(3))!;
    expect((last.tree as SplitNode).sizes[1]).toBeCloseTo(0.8, 12);
    expect((last.tree as SplitNode).sizes[0]).toBeCloseTo(0.2, 12);
    expect(last.heir).toBe(2);
    const first = removeLeaf(t, P(1))!;
    expect(shape(first.tree)).toEqual({ axis: "x", children: [2, 3] });
    expect((first.tree as SplitNode).sizes[0]).toBeCloseTo(0.5, 12);
    expect(first.heir).toBe(2);
  });

  it("collapses a two-child parent into the survivor, which takes its place and size", () => {
    const r = removeLeaf(ys([0.3, 0.7], 1, xs([0.4, 0.6], 2, 3)), P(1))!;
    expect(r.tree).toEqual(xs([0.4, 0.6], 2, 3));
    expect(r.heir).toBe(2); // the first leaf of a next heir
    const g = removeLeaf(ys([0.3, 0.7], x(1, 2), x(3, 4)), P(2))!;
    expect(shape(g.tree)).toEqual({ axis: "y", children: [1, { axis: "x", children: [3, 4] }] });
    expect((g.tree as SplitNode).sizes).toEqual([0.3, 0.7]);
    expect(g.heir).toBe(1);
  });

  it("flattens a survivor of the grandparent's axis, scaled by the parent's size", () => {
    const r = removeLeaf(xs([0.4, 0.6], 1, ys([0.5, 0.5], 2, xs([0.25, 0.75], 3, 4))), P(2))!;
    expect(shape(r.tree)).toEqual({ axis: "x", children: [1, 3, 4] });
    const s = (r.tree as SplitNode).sizes;
    expect(s[0]).toBeCloseTo(0.4, 12);
    expect(s[1]).toBeCloseTo(0.15, 12);
    expect(s[2]).toBeCloseTo(0.45, 12);
    expect(r.heir).toBe(3);
    const prev = removeLeaf(x(y(1, 2), 3), P(3))!;
    expect(prev.tree).toEqual(y(1, 2));
    expect(prev.heir).toBe(2); // the last leaf of a previous heir
  });

  it("never closes the root leaf", () => {
    expect(removeLeaf(leaf(1), P(1))).toBeNull();
  });

  it("leaves the panes outside the parent where they were, and the tree normal", () => {
    const rand = rng(77);
    for (let i = 0; i < 500; i++) {
      const t = reachable(rand, 1 + Math.floor(rand() * 24));
      const ls = leaves(t);
      if (ls.length < 2) continue;
      const pane = pick(rand, ls);
      const path = pathOf(t, pane)!;
      const inside = new Set<number>(leaves(nodeAt(t, path.slice(0, -1))));
      const r = removeLeaf(t, pane)!;
      expectNormal(r.tree);
      expect(leaves(r.tree)).toEqual(ls.filter((p) => p !== pane));
      expect(inside.has(r.heir)).toBe(true);
      const before = rectsOf(t), after = rectsOf(r.tree);
      for (const p of ls) if (!inside.has(p)) expectRect(after[p], before[p]!);
    }
  });

  it("never mutates its input", () => {
    const t = deepFreeze(preset("grid5"));
    const copy = JSON.parse(JSON.stringify(t));
    removeLeaf(t, P(4));
    removeLeaf(t, P(2));
    expect(t).toEqual(copy);
  });

  it("property: split then close of the new pane is the identity, and the heir is the source", () => {
    // [giving to the next sibling]: in case B the new pane's half must go back
    // to the source before it, or a later sibling grows.
    const rand = rng(424242);
    let checked = 0;
    while (checked < 2000) {
      const t = reachable(rand, Math.floor(rand() * 24));
      const id = freeId(t);
      if (id === null) continue;
      const pane = pick(rand, leaves(t));
      const axis: Axis = rand() < 0.5 ? "x" : "y";
      if (!canSplit(t, pane, axis)) continue;
      const r = removeLeaf(insertBeside(t, pane, axis, id), id)!;
      expect(r.tree).toEqual(t);
      expect(r.heir).toBe(pane);
      checked++;
    }
  }, 60_000);
});

// ---- drop (render only) ------------------------------------------------------------------

describe("prune reproduces dropColumn / dropRow (§7.1)", () => {
  // The expectation tables of workspace.test.ts ("keeps the focused pane's
  // column and row", "the generic drop (§3.3)", "drops by position"), copied.
  // prune(tree, axis, acrossS − 1 | downS − 1, focus) is one dropColumn /
  // dropRow. `shape` is presetOf of the result (null: no preset has it).
  const cases: [string, Axis, Preset, number[], number, Preset | null, number[]][] = [
    ["dropColumn", "x", "cols3", [1, 2, 3], 3, "cols2", [2, 3]],
    ["dropColumn", "x", "cols3", [1, 2, 3], 1, "cols2", [1, 2]],
    ["dropColumn", "x", "cols2", [2, 3], 3, "single", [3]],
    ["dropColumn", "x", "main2", [1, 2, 3], 3, "rows2", [2, 3]],
    ["dropColumn", "x", "main2", [1, 2, 3], 2, "rows2", [2, 3]],
    ["dropColumn", "x", "main2", [1, 2, 3], 1, "single", [1]],
    ["dropColumn", "x", "grid4", [1, 2, 3, 4], 4, "rows2", [2, 4]],
    ["dropRow", "y", "grid4", [1, 2, 3, 4], 3, "cols2", [3, 4]],
    ["dropRow", "y", "main2", [1, 2, 3], 3, "cols2", [1, 3]],
    ["dropRow", "y", "main2", [1, 2, 3], 1, "cols2", [1, 2]], // a overlaps both; the topmost wins
    ["dropRow", "y", "rows2", [1, 3], 3, "single", [3]],
    ["dropColumn", "x", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 7, "grid6", [1, 2, 3, 5, 6, 7]],
    ["dropColumn", "x", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 8, "grid6", [2, 3, 4, 6, 7, 8]],
    ["dropColumn", "x", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 4, "grid6", [2, 3, 4, 6, 7, 8]],
    ["dropColumn", "x", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 1, "grid6", [1, 2, 3, 5, 6, 7]],
    ["dropColumn", "x", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 3, "grid6", [1, 2, 3, 5, 6, 7]], // §7.1 by hand
    ["dropColumn", "x", "grid7", [1, 2, 3, 4, 5, 6, 7], 4, "grid6", [2, 3, 4, 5, 6, 7]],
    ["dropColumn", "x", "grid7", [1, 2, 3, 4, 5, 6, 7], 6, "grid6", [1, 2, 3, 5, 6, 7]],
    ["dropColumn", "x", "grid6", [1, 2, 3, 4, 5, 6], 6, "grid4", [2, 3, 5, 6]],
    ["dropColumn", "x", "grid6", [1, 2, 3, 4, 5, 6], 4, "grid4", [1, 2, 4, 5]],
    ["dropColumn", "x", "grid5", [1, 2, 3, 4, 5], 3, "grid4", [2, 3, 4, 5]],
    ["dropColumn", "x", "grid5", [1, 2, 3, 4, 5], 5, "grid4", [1, 2, 4, 5]],
    // Intended difference (iii) of §7.1: a four-wide row whose other row is
    // dropped stays four wide (today: cols3 [5,6,7], [6,7,8], [1,2,3]).
    ["dropRow", "y", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 6, null, [5, 6, 7, 8]],
    ["dropRow", "y", "grid8", [1, 2, 3, 4, 5, 6, 7, 8], 8, null, [5, 6, 7, 8]],
    ["dropRow", "y", "grid7", [1, 2, 3, 4, 5, 6, 7], 2, null, [1, 2, 3, 4]],
    ["dropRow", "y", "grid7", [1, 2, 3, 4, 5, 6, 7], 7, "cols3", [5, 6, 7]],
    ["dropRow", "y", "grid6", [1, 2, 3, 4, 5, 6], 2, "cols3", [1, 2, 3]],
    ["dropRow", "y", "grid5", [1, 2, 3, 4, 5], 5, "cols2", [4, 5]],
    ["dropRow", "y", "grid5", [1, 2, 3, 4, 5], 1, "cols3", [1, 2, 3]],
    // By position, whatever the ids.
    ["dropColumn", "x", "cols3", [4, 8, 1], 1, "cols2", [8, 1]],
    ["dropColumn", "x", "grid8", [8, 7, 6, 5, 4, 3, 2, 1], 5, "grid6", [7, 6, 5, 3, 2, 1]],
  ];

  it.each(cases)("%s %s %s %j focused %i → %s %j", (_old, axis, p, order, focus, want, panes) => {
    // [anchoring on the first child]: the focus path and the overlap rule pick the window.
    const t = presetTree(p, ids(...order));
    const s = axis === "x" ? acrossS(t) : downS(t);
    const r = prune(t, axis, s - 1, P(focus));
    expect(leaves(r)).toEqual(panes);
    expect(presetOf(r)).toBe(want);
    expectNormal(r);
  });

  it("has nothing to drop where today's drop returned null", () => {
    // dropColumn("single"), dropColumn("rows2"), dropRow("cols3") were null: already 1.
    expect(acrossS(preset("single"))).toBe(1);
    expect(acrossS(preset("rows2"))).toBe(1);
    expect(downS(preset("cols3"))).toBe(1);
    expect(prune(preset("cols3"), "y", 1, P(2))).toEqual(preset("cols3"));
    expect(prune(leaf(4), "x", 1, P(4))).toEqual(leaf(4));
  });

  it("keeps the drawn panes' own proportions (§7.1 (ii))", () => {
    const r = prune(xs([0.2, 0.3, 0.5], 1, 2, 3), "x", 2, P(3)) as SplitNode;
    expect(leaves(r)).toEqual([2, 3]);
    expect(r.sizes[0]).toBeCloseTo(0.375, 12);
    expect(r.sizes[1]).toBeCloseTo(0.625, 12);
  });

  it("never drops the focused pane: every shape × every focus × every t, both axes", () => {
    // [anchoring on the first child]
    const rand = rng(99);
    const trees: LayoutNode[] = PRESETS.map((p) => preset(p));
    for (let i = 0; i < 300; i++) trees.push(reachable(rand, 4 + Math.floor(rand() * 24)));
    let pruned = 0;
    for (const t of trees) {
      const ls = leaves(t);
      for (const axis of ["x", "y"] as Axis[]) {
        const s = axis === "x" ? acrossS(t) : downS(t);
        for (const f of ls) {
          for (let k = 1; k <= s; k++) {
            const r = prune(t, axis, k, f);
            const what = `${JSON.stringify(storeTree(t))} ${axis} t=${k} focus ${f}`;
            expect(leaves(r), what).toContain(f);
            expect(axis === "x" ? acrossS(r) : downS(r), what).toBeLessThanOrEqual(k);
            const kept = leaves(r);
            expect(kept.every((p) => ls.includes(p)), what).toBe(true);
            expect(kept.map((p) => ls.indexOf(p)), what).toEqual([...kept.map((p) => ls.indexOf(p))].sort((a, b) => a - b));
            if (k === s) expect(r, what).toEqual(t);
            else pruned++;
            expectNormal(r);
          }
        }
      }
    }
    expect(pruned).toBeGreaterThan(1000);
  }, 60_000);

  it("never mutates its input", () => {
    const t = deepFreeze(preset("grid8"));
    const copy = JSON.parse(JSON.stringify(t));
    prune(t, "x", 3, P(6));
    prune(t, "y", 1, P(6));
    expect(t).toEqual(copy);
  });
});

// ---- the clamp ---------------------------------------------------------------------------

/** workspace.ts's clampCols as it is at b79eb8e, frozen here so later packages cannot move the reference. */
function oldClampCols(cols: readonly number[], width: number, min: number = 360): number[] {
  const n = cols.length + 1;
  if (n === 1) return [];
  if (!Number.isFinite(width) || width <= 0) return cols.slice();
  if (width < n * min) return Array.from({ length: n - 1 }, (_, i) => (i + 1) / n);
  const px = cols.map((c) => c * width);
  for (let i = 0; i < px.length; i++) px[i] = Math.max(px[i], (i === 0 ? 0 : px[i - 1]) + min);
  for (let i = px.length - 1; i >= 0; i--) px[i] = Math.min(px[i], (i === px.length - 1 ? width : px[i + 1]) - min);
  return px.map((x) => x / width);
}
const edgesOfSizes = (s: readonly number[]) => s.slice(0, -1).map((_, i) => s.slice(0, i + 1).reduce((a, b) => a + b, 0));
const sizesOfEdges = (e: readonly number[]) => [...e, 1].map((v, i) => v - (i === 0 ? 0 : e[i - 1]));

describe("the per-node clamp (§7.3)", () => {
  it("equals clampCols on one-level nodes", () => {
    // [a water-filling clamp moving untouched siblings]
    const rand = rng(1150);
    for (let i = 0; i < 3000; i++) {
      const k = 2 + Math.floor(rand() * 3);
      // Some children well under the minimum, so the push is exercised.
      const sizes = randomSizes(rand, k).map((v) => (rand() < 0.3 ? v / 8 : v));
      const sum = sizes.reduce((a, b) => a + b, 0);
      const s = sizes.map((v) => v / sum);
      const width = 200 + rand() * 1800;
      const got = clampSizes(s, s.map(() => 360), width);
      const want = sizesOfEdges(oldClampCols(edgesOfSizes(s), width));
      expect(got.length).toBe(k);
      for (let j = 0; j < k; j++) expect(got[j], `${JSON.stringify(s)} at ${width}`).toBeCloseTo(want[j], 9);
    }
  });

  it("pushes edges, so a child beyond the push keeps its size", () => {
    // [a water-filling clamp moving untouched siblings]: 120 px is pushed to 360
    // by moving only the first edge; the third child keeps its 360 exactly.
    const got = clampSizes([0.1, 0.6, 0.3], [360, 360, 360], 1200);
    expect(got[0] * 1200).toBeCloseTo(360, 9);
    expect(got[1] * 1200).toBeCloseTo(480, 9);
    expect(got[2] * 1200).toBeCloseTo(360, 9);
    const both = clampSizes([0.1, 0.45, 0.45], [360, 360, 360], 1200);
    expect(both.map((v) => +(v * 1200).toFixed(6))).toEqual([360, 360, 480]);
  });

  it("is the identity, value for value, when every child meets its minimum (C4)", () => {
    const rand = rng(4);
    for (let i = 0; i < 2000; i++) {
      const k = 2 + Math.floor(rand() * 3);
      const mins = Array.from({ length: k }, () => pick(rand, [200, 360, 400, 600, 720, 1080]));
      const total = mins.reduce((a, b) => a + b, 0);
      const length = total * (1.01 + rand() * 2);
      // Sizes that give every child at least its minimum.
      const slack = randomSizes(rand, k).map((v) => v * (length - total));
      const s = mins.map((m, j) => (m + slack[j]) / length);
      if (s.some((v, j) => v * length < mins[j])) continue; // rounding put one a hair under
      expect(clampSizes(s, mins, length)).toEqual(s);
    }
    expect(clampSizes([0.3, 0.7], [360, 360], 1200)).toEqual([0.3, 0.7]);
  });

  it("gives each child its own minimum, a subtree's being larger", () => {
    const got = clampSizes([0.5, 0.5], [360, 720], 1200);
    expect(got[0]).toBeCloseTo(0.4, 12);
    expect(got[1]).toBeCloseTo(0.6, 12);
  });

  it("falls back to sizes in proportion to the minimums when they cannot all be met", () => {
    const got = clampSizes([0.5, 0.5], [360, 720], 900);
    expect(got[0]).toBeCloseTo(1 / 3, 12);
    expect(got[1]).toBeCloseTo(2 / 3, 12);
    const eq = clampSizes([0.2, 0.3, 0.5], [360, 360, 360], 700);
    for (const v of eq) expect(v).toBeCloseTo(1 / 3, 12);
  });

  it("leaves sizes alone with no measured length", () => {
    expect(clampSizes([0.1, 0.9], [360, 360], NaN)).toEqual([0.1, 0.9]);
    expect(clampSizes([0.1, 0.9], [360, 360], 0)).toEqual([0.1, 0.9]);
  });
});

// ---- edges ---------------------------------------------------------------------------------

describe("edges and linked edges (§9, P-5)", () => {
  const edgeIds = (n: LayoutNode) => edgesOf(n).map((e) => e.testid).sort();
  const byId = (n: LayoutNode) => Object.fromEntries(edgesOf(n).map((e) => [e.testid, e]));

  it("names every preset's separators as §3.3's table does", () => {
    const want: Record<Preset, string[]> = {
      single: [],
      cols2: ["split-x-1"],
      rows2: ["split-y-1"],
      cols3: ["split-x-1", "split-x-2"],
      main2: ["split-x-1", "split-y1-1"],
      grid4: ["split-x0-1", "split-y-1"],
      grid5: ["split-x0-1", "split-x0-2", "split-x1-1", "split-y-1"],
      grid6: ["split-x0-1", "split-x0-2", "split-y-1"],
      grid7: ["split-x0-1", "split-x0-2", "split-x0-3", "split-x1-1", "split-x1-2", "split-y-1"],
      grid8: ["split-x0-1", "split-x0-2", "split-x0-3", "split-y-1"],
    };
    for (const p of PRESETS) expect(edgeIds(preset(p)), p).toEqual(want[p].slice().sort());
    expect(edgeIds(y(x(1, y(2, 3)), 4))).toEqual(["split-x0-1", "split-y-1", "split-y01-1"]);
  });

  it("grid4: one column separator for both rows, plus one row separator", () => {
    const e = edgesOf(preset("grid4"));
    expect(e.length).toBe(2);
    const col = e.find((h) => h.axis === "x")!;
    expect(col.testid).toBe("split-x0-1");
    expect(col.members).toEqual([{ path: [0], i: 1 }, { path: [1], i: 1 }]);
    expect(col.at.left).toBeCloseTo(0.5, 12);
    expect(col.at.top).toBeCloseTo(0, 12);
    expect(col.at.top + col.at.height).toBeCloseTo(1, 12);
    const row = e.find((h) => h.axis === "y")!;
    expect(row.testid).toBe("split-y-1");
    expect(row.members).toEqual([{ path: [], i: 1 }]);
    expect(row.at.top).toBeCloseTo(0.5, 12);
    expect(row.at.left).toBeCloseTo(0, 12);
    expect(row.at.left + row.at.width).toBeCloseTo(1, 12);
  });

  it("grid5: three column separators, none linked", () => {
    const e = edgesOf(preset("grid5")).filter((h) => h.axis === "x");
    expect(e.length).toBe(3);
    for (const h of e) expect(h.members.length).toBe(1);
    const b = byId(preset("grid5"));
    expect(b["split-x1-1"].at.left).toBeCloseTo(0.5, 12);
    expect(b["split-x1-1"].at.top).toBeCloseTo(0.5, 12);
    expect(b["split-x1-1"].at.height).toBeCloseTo(0.5, 12);
  });

  it("grid6 and grid8 link every column edge across both rows", () => {
    for (const p of ["grid6", "grid8"] as Preset[]) {
      for (const h of edgesOf(preset(p)).filter((e) => e.axis === "x")) expect(h.members.length, `${p} ${h.testid}`).toBe(2);
    }
  });

  it("links by position, within SAME_EDGE, carrying the first member's id", () => {
    // grid5 with the bottom edge dragged onto the top row's second edge.
    const b = byId(ys([0.5, 0.5], x(1, 2, 3), xs([2 / 3, 1 / 3], 4, 5)));
    expect(Object.keys(b).sort()).toEqual(["split-x0-1", "split-x0-2", "split-y-1"]);
    expect(b["split-x0-2"].members).toEqual([{ path: [0], i: 2 }, { path: [1], i: 1 }]);
    expect(b["split-x0-1"].members).toEqual([{ path: [0], i: 1 }]);
    // Within SAME_EDGE is one edge; beyond it, two.
    expect(edgeIds(ys([0.5, 0.5], x(1, 2), xs([0.5 + 5e-5, 0.5 - 5e-5], 3, 4)))).toEqual(["split-x0-1", "split-y-1"]);
    expect(edgeIds(ys([0.5, 0.5], x(1, 2), xs([0.5 + 2e-4, 0.5 - 2e-4], 3, 4)))).toEqual(["split-x0-1", "split-x1-1", "split-y-1"]);
    expect(edgeIds(ys([0.5, 0.5], x(1, 2), xs([0.6, 0.4], 3, 4)))).toEqual(["split-x0-1", "split-x1-1", "split-y-1"]);
  });

  it("links only consecutive children of one node", () => {
    const three = byId(y(x(1, 2), x(3, 4), x(5, 6)));
    expect(three["split-x0-1"].members.length).toBe(3);
    expect(Object.keys(three).sort()).toEqual(["split-x0-1", "split-y-1", "split-y-2"]);
    // The middle row's edge is elsewhere: the top and bottom rows are not consecutive.
    expect(edgeIds(y(x(1, 2), xs([0.6, 0.4], 3, 4), x(5, 6)))).toEqual(
      ["split-x0-1", "split-x1-1", "split-x2-1", "split-y-1", "split-y-2"]);
    // A full-width leaf between two rows breaks the run too.
    expect(edgeIds(y(x(1, 2), 3, x(4, 5)))).toEqual(["split-x0-1", "split-x2-1", "split-y-1", "split-y-2"]);
  });
});

// ---- parseTree / storeTree --------------------------------------------------------------------

describe("parseTree (§8.3)", () => {
  const ok = (raw: unknown) => {
    const t = parseTree(raw);
    expect(t, JSON.stringify(raw)).not.toBeNull();
    return t!;
  };
  const two = (b: unknown) => ({ axis: "x", children: [1, b], sizes: [0.5, 0.5] });
  /** Alternating x / y, `levels` nested split nodes, each with one leaf beside the next level. */
  function nested(levels: number): unknown {
    let n: unknown = { axis: levels % 2 ? "x" : "y", children: [levels, levels + 1], sizes: [0.5, 0.5] };
    for (let i = levels - 1; i >= 1; i--) n = { axis: i % 2 ? "x" : "y", children: [i, n], sizes: [0.5, 0.5] };
    return n;
  }

  it("reads leaves as bare numbers, nodes as axis, children and sizes", () => {
    expect(ok(3)).toEqual(leaf(3));
    expect(ok(12)).toEqual(leaf(12));
    expect(ok({ axis: "x", children: [1, 2], sizes: [0.5, 0.5] })).toEqual(xs([0.5, 0.5], 1, 2));
    expect(ok(storeTree(preset("main2")))).toEqual(preset("main2"));
  });

  it("round-trips storeTree for every preset and random reachable trees", () => {
    expect(storeTree(preset("grid4"))).toEqual(
      { axis: "y", children: [{ axis: "x", children: [1, 2], sizes: [0.5, 0.5] }, { axis: "x", children: [3, 4], sizes: [0.5, 0.5] }], sizes: [0.5, 0.5] });
    for (const p of PRESETS) expect(parseTree(JSON.parse(JSON.stringify(storeTree(preset(p)))))).toEqual(preset(p));
    const rand = rng(8);
    for (let i = 0; i < 300; i++) {
      const t = reachable(rand, Math.floor(rand() * 25));
      expect(parseTree(JSON.parse(JSON.stringify(storeTree(t))))).toEqual(t);
    }
  });

  it("rejects what is not a tree", () => {
    for (const raw of [null, undefined, "x", "3", true, [], [1, 2], {}, { children: [1, 2] }, { axis: "x" },
      { axis: "x", children: "12", sizes: [0.5, 0.5] }, { axis: "x", children: { 0: 1, 1: 2, length: 2 } }]) {
      expect(parseTree(raw), JSON.stringify(raw) ?? String(raw)).toBeNull();
    }
  });

  it("rejects a leaf that is not an integer 1–12, at the root or inside", () => {
    for (const bad of [0, 13, -1, "3", 3.5, NaN, Infinity, null, true, [3]]) {
      expect(parseTree(bad), String(bad)).toBeNull();
      expect(parseTree(two(bad)), `inside: ${String(bad)}`).toBeNull();
    }
  });

  it("rejects a duplicate leaf, however deep", () => {
    expect(parseTree({ axis: "x", children: [1, 1], sizes: [0.5, 0.5] })).toBeNull();
    expect(parseTree({
      axis: "y", sizes: [0.5, 0.5],
      children: [{ axis: "x", children: [1, 2], sizes: [0.5, 0.5] }, { axis: "x", children: [3, 1], sizes: [0.5, 0.5] }],
    })).toBeNull();
    // A shared child object is a duplicate too.
    const c = { axis: "y", children: [2, 3], sizes: [0.5, 0.5] };
    expect(parseTree({ axis: "x", children: [c, c], sizes: [0.5, 0.5] })).toBeNull();
  });

  it("rejects an axis other than \"x\" or \"y\"", () => {
    for (const axis of ["z", "X", "", 1, null, undefined, ["x"]]) {
      expect(parseTree({ axis, children: [1, 2], sizes: [0.5, 0.5] }), String(axis)).toBeNull();
    }
  });

  it("caps children per node: 4 across, 3 down", () => {
    expect(parseTree({ axis: "x", children: [1, 2, 3, 4], sizes: [0.25, 0.25, 0.25, 0.25] })).not.toBeNull();
    expect(parseTree({ axis: "x", children: [1, 2, 3, 4, 5], sizes: [0.2, 0.2, 0.2, 0.2, 0.2] })).toBeNull();
    expect(parseTree({ axis: "y", children: [1, 2, 3], sizes: [0.3, 0.3, 0.4] })).not.toBeNull();
    expect(parseTree({ axis: "y", children: [1, 2, 3, 4], sizes: [0.25, 0.25, 0.25, 0.25] })).toBeNull();
  });

  it("refuses a 100 000-entry children array before reading one entry, and quickly", () => {
    let reads = 0;
    const big = new Proxy(Array.from({ length: 100_000 }, (_, i) => (i % 12) + 1), {
      get(target, key, recv) {
        if (typeof key === "string" && /^\d+$/.test(key)) reads++;
        return Reflect.get(target, key, recv);
      },
    });
    const t0 = performance.now();
    expect(parseTree({ axis: "x", children: big, sizes: [] })).toBeNull();
    expect(parseTree({ axis: "y", children: Array(100_000).fill(1), sizes: Array(100_000).fill(1e-5) })).toBeNull();
    expect(performance.now() - t0).toBeLessThan(100);
    expect(reads).toBe(0);
  }, 2_000);

  it("caps depth at six with a counter: five nested split nodes read, six do not", () => {
    const five = ok(nested(5));
    expect(leaves(five)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(parseTree(nested(6))).toBeNull(); // depth 7
    // Far deeper, and a cycle: refused without exhausting the stack.
    let deep: unknown = 1;
    for (let i = 0; i < 100_000; i++) deep = { axis: i % 2 ? "x" : "y", children: [deep, 2], sizes: [0.5, 0.5] };
    const cycle: { axis: string; children: unknown[]; sizes: number[] } = { axis: "x", children: [1], sizes: [0.5, 0.5] };
    cycle.children.push(cycle);
    const t0 = performance.now();
    expect(parseTree(deep)).toBeNull();
    expect(parseTree(cycle)).toBeNull();
    expect(performance.now() - t0).toBeLessThan(100);
  }, 2_000);

  it("caps the node count at 23", () => {
    // Unary wrappers add nodes without leaves: 1 + 4 + 12 + 7 = 24 nodes.
    const wrap = (n: unknown, w: boolean) => (w ? { axis: "x", children: [n], sizes: [1] } : n);
    let id = 0, wraps = 7;
    const col = () => ({ axis: "y", children: [0, 1, 2].map(() => wrap(++id, wraps-- > 0)), sizes: [0.3, 0.3, 0.4] });
    const raw = { axis: "x", children: [col(), col(), col(), col()], sizes: [0.25, 0.25, 0.25, 0.25] };
    expect(id).toBe(12);
    expect(parseTree(raw)).toBeNull();
  });

  it("caps leaves at twelve: the full 4×3 reads, a thirteenth cannot", () => {
    let g: LayoutNode = leaf(1);
    for (const p of [1, 2, 3]) g = insertBeside(g, P(p), "x", P(p + 1));
    for (const c of [1, 2, 3, 4]) {
      g = insertBeside(g, P(c), "y", P(c + 4));
      g = insertBeside(g, P(c + 4), "y", P(c + 8));
    }
    expect(leaves(ok(storeTree(g))).length).toBe(12);
    const stored = storeTree(g) as { axis: Axis; children: unknown[]; sizes: number[] };
    const col = stored.children[0] as { axis: Axis; children: unknown[]; sizes: number[] };
    const thirteen = { ...stored, children: [{ ...col, children: [{ axis: "x", children: [1, 13], sizes: [0.5, 0.5] }, ...col.children.slice(1)] }, ...stored.children.slice(1)] };
    expect(parseTree(thirteen)).toBeNull();
  });

  it("falls back to equal sizes for a node with bad sizes, that node only", () => {
    for (const sizes of [undefined, null, "0.5,0.5", [0.5], [0.3, 0.3, 0.4], ["0.5", 0.5], [NaN, 0.5], [Infinity, 0.5],
      [-Infinity, 0.5], [0, 1], [-0.5, 1.5], [1e308, 1e308], { 0: 0.5, 1: 0.5, length: 2 }]) {
      const t = ok({
        axis: "y", sizes: [0.3, 0.7],
        children: [{ axis: "x", children: [1, 2], sizes }, { axis: "x", children: [3, 4], sizes: [0.2, 0.8] }],
      }) as SplitNode;
      const what = JSON.stringify(sizes) ?? String(sizes);
      expect(t.sizes, what).toEqual([0.3, 0.7]);
      expect((t.children[0] as SplitNode).sizes, what).toEqual([0.5, 0.5]);
      expect((t.children[1] as SplitNode).sizes, what).toEqual([0.2, 0.8]);
    }
  });

  it("renormalises good sizes, then flattens and collapses", () => {
    const t = ok({ axis: "x", children: [1, 2], sizes: [1, 3] }) as SplitNode;
    expect(t.sizes[0]).toBeCloseTo(0.25, 12);
    expect(t.sizes[1]).toBeCloseTo(0.75, 12);
    const f = ok({ axis: "x", children: [1, { axis: "x", children: [2, 3], sizes: [0.5, 0.5] }], sizes: [0.5, 0.5] });
    expect(shape(f)).toEqual({ axis: "x", children: [1, 2, 3] });
    expectNormal(f);
  });

  it("does not check FS-3 on read: a layout over the cap is kept", () => {
    // Five across on the top line (x[1, 2, 3, y[x[4, 5], 6]]).
    const t = ok(storeTree(x(1, 2, 3, y(x(4, 5), 6))));
    expect(across(t)).toBe(5);
  });

  it("ignores unknown keys and never spreads a raw object, so __proto__ is inert", () => {
    const t = ok({ axis: "x", children: [1, 2], sizes: [0.5, 0.5], extra: 5, label: "hi" });
    expect(Object.keys(t).sort()).toEqual(["axis", "children", "sizes"]);
    const leafKeys = Object.keys((t as SplitNode).children[0]);
    expect(leafKeys).toEqual(["pane"]);
    // An own "__proto__" key (JSON.parse makes one) is just an unknown key.
    const own = ok(JSON.parse('{"axis":"x","children":[1,2],"sizes":[0.5,0.5],"__proto__":{"axis":"y","polluted":true}}'));
    expect((own as SplitNode).axis).toBe("x");
    expect((own as unknown as Record<string, unknown>).polluted).toBeUndefined();
    expect(Object.keys(own).sort()).toEqual(["axis", "children", "sizes"]);
    // A node made only of "__proto__" is not a node: Object.assign would have set its prototype.
    expect(parseTree(JSON.parse('{"__proto__":{"axis":"x","children":[1,2],"sizes":[0.5,0.5]}}'))).toBeNull();
    expect(parseTree(JSON.parse('{"axis":"x","children":[1,{"__proto__":{"axis":"y","children":[2,3],"sizes":[0.5,0.5]}}],"sizes":[0.5,0.5]}'))).toBeNull();
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect(({} as Record<string, unknown>).axis).toBeUndefined();
  });
});
