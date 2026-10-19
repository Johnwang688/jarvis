// FS4: the directional split and close (docs/plans/2026-10-19-hud-free-split-
// plan.md, the FS4 card with §4, §5, §7, §9 and §10; decisions FS-1..FS-10,
// which win). Written before FS4: on FS3's code every block here fails, because
// the API it names (a per-pane, per-axis verdict and split, close to the
// sibling, `autoSplit`, the FS-3 drag stop) does not exist yet.
//
// ---- The contract these tests assume (one list for the implementer) ---------
//
// lib/workspace.ts
//   type SplitWhy = "" | "dropped" | "across" | "down" | "room" | "max" | "hidden-edit"
//   splitVerdict(ws, fit: WorkspaceFit, source: PaneNo, axis: Axis, dirty?: readonly PaneNo[])
//       : { ok: boolean; why: SplitWhy }
//       first reason wins (§5.1): "dropped" (fit.dropped > 0) → "across" (axis x) / "down"
//       (axis y), counted geometrically over the fitted rects of every drawn pane (§4.1, the
//       whole window; never assuming a node has ≤ 4 children) → "room" (§4.5: the source's
//       fitted px; right needs px.width / 2 ≥ 360, down px.height / 2 ≥ 200; skipped when
//       the px are NaN) → "max" (all twelve ids in the tree) → "hidden-edit" (allocate(ws,
//       dirty) finds no id).
//   splitTitle(why: SplitWhy, axis: Axis, drawn?: number, total?: number): string   (§10)
//   splitPane(ws, fit, source, axis, id, spec, opts?: { focus?: boolean }): Workspace
//       `ws` itself (toBe) when splitVerdict(ws, fit, source, axis) is not ok, when `id` is
//       already in the tree, when `id` is not an id 1–12, or when `source` is not drawn.
//       Otherwise: C3 (every node on root → source takes the sizes it was drawn with, from
//       fit.render), then insertBeside (§5.1 cases A and B); panes[id] = a copy of `spec`;
//       `id` leaves `kept`; focus goes to `id` unless `focus: false`.
//   closePane(ws, id, then?): Workspace
//       removeLeaf (§5.2: the space to the previous sibling, else the next); the pane's
//       spec back to its default; `kept` untouched. Focus: unchanged when the focused pane
//       is not the one closed; else `then` when it is still drawn; else removeLeaf's heir
//       (the heir's leaf nearest the closed pane). `ws` itself for the last pane or a pane
//       not drawn.
//   autoSplit(ws, fit, source, held?: readonly PaneNo[]): { axis: Axis; id: PaneNo } | null
//       right when its verdict is ok, else down, else null; the id is freeId(ws, held)
//       (never allocate's destructive tiers); null when there is none.
//   moveEdge(ws, fit, handle, px): as FS3, plus the FS-3 stop (§4.3): an edge stops where
//       moving it further would make some line cross a fifth pane across or a fourth down,
//       and the handle's min/max (fitWorkspace's edges) already reflect that stop.
//   LADDER, MAX_DRAWN and autoSplitVerdict are gone (not exported).
//
// Choices made where the plan is open (recorded here, reversible by the lead):
//   - splitTitle's "room" for down: the plan writes `(and "…down")`. Right is pinned exactly;
//     down must start "Not enough room to split down" and offer "zoom out (Ctrl+-)" — the
//     rest of the advice (widen / fold a side pane does not add height) is the implementer's.
//   - splitTitle's "dropped" takes the drawn and stored counts as two numbers.
//   - closePane keeps focus where it is when another pane closes (today's rule), and `then`
//     is read only when the focused pane is the one closed.
//   - autoSplit returns null whenever the window is dropping panes (P-6), and splits only
//     from the `source` it is given (devopen picks the source; see devopen.test.ts).
//
// Each test names, in a [bracketed] comment, the mutation it is meant to catch
// where the plan names one.

import { describe, expect, it } from "vitest";
import * as W from "./workspace";
import { DEFAULT_LAYOUT, type PaneLayout } from "./layout";
import {
  allocate, autoSplit, closePane, defaultPane, defaultWorkspace, fitWorkspace, focusPane, freeId, moveEdge, orderOf,
  parseWorkspace, presetOfWs, setPanel, setPreset, splitPane, splitTitle, splitVerdict,
  type PaneNo, type PaneSpec, type Workspace, type WorkspaceFit,
} from "./workspace";
import {
  crossing, insertBeside, isLeaf, leaves, pathOf, presetTree, rectsOf, removeLeaf, storeTree, withSizes,
  type Axis, type LayoutNode, type Path, type Rect, type SplitNode, type StoredNode,
} from "./splittree";

// ---- helpers -------------------------------------------------------------------

const FOLDED: PaneLayout = { ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true };
const chat = (): PaneSpec => ({ view: "chat", projectId: null, terminalId: null });
const P = (n: number) => n as PaneNo;
const X = (sizes: number[], ...children: StoredNode[]): StoredNode => ({ axis: "x", children, sizes });
const Y = (sizes: number[], ...children: StoredNode[]): StoredNode => ({ axis: "y", children, sizes });
const v2 = (tree: StoredNode, rest: Record<string, unknown> = {}): Workspace =>
  parseWorkspace(JSON.stringify({ version: 2, tree, ...rest }));
const halves = [0.5, 0.5];
const quarters = [0.25, 0.25, 0.25, 0.25];
const thirds = [1 / 3, 1 / 3, 1 / 3];

/** No measured window: nothing is dropped and FS-4 is skipped, so only FS-3 decides. */
const unmeasured = (ws: Workspace) => fitWorkspace(DEFAULT_LAYOUT, ws, NaN, NaN);
/** A fit at this room, both side panes folded unless a layout is given. */
const fitAt = (ws: Workspace, available: number, availableH: number, layout: PaneLayout = FOLDED) =>
  fitWorkspace(layout, ws, available, availableH);
const verdict = (ws: Workspace, source: number, axis: Axis, fit: WorkspaceFit = unmeasured(ws), dirty: PaneNo[] = []) =>
  splitVerdict(ws, fit, P(source), axis, dirty);
/** Split as the owner does: the lowest id `allocate` gives, a chat copy. */
const split = (ws: Workspace, source: number, axis: Axis, fit: WorkspaceFit = unmeasured(ws)) =>
  splitPane(ws, fit, P(source), axis, allocate(ws)!, chat());
/** `got`'s structure and ids exactly, sizes within 1e-12. */
function expectTree(got: LayoutNode, want: StoredNode, what = "tree") {
  const walk = (g: LayoutNode, w: StoredNode, at: string) => {
    if (typeof w === "number") {
      expect(g, at).toEqual({ pane: w });
      return;
    }
    expect(isLeaf(g), `${at}: a leaf where a node was wanted (${JSON.stringify(storeTree(got))})`).toBe(false);
    const n = g as SplitNode;
    expect([n.axis, n.children.length], `${at} in ${JSON.stringify(storeTree(got))}`).toEqual([w.axis, w.children.length]);
    n.sizes.forEach((s, i) => expect(Math.abs(s - w.sizes[i]), `${at} sizes ${n.sizes} vs ${w.sizes}`).toBeLessThan(1e-12));
    n.children.forEach((c, i) => walk(c, w.children[i], `${at}.${i}`));
  };
  walk(got, want, what);
}
/** The most panes any horizontal line crosses ("x") or any vertical line ("y"), over the whole tree. */
const most = (t: LayoutNode, axis: Axis) => crossing(rectsOf(t), axis, 0, 1);
/** The same over a fit's drawn rects. */
const mostDrawn = (fit: WorkspaceFit, axis: Axis) => {
  const rects: Partial<Record<PaneNo, Rect>> = {};
  for (const n of fit.panes) rects[n] = fit.boxes[n]!.rect;
  return crossing(rects, axis, 0, 1);
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

/** FS-3 alone, on a bare tree (the oracle the verdict is checked against; §4.1). */
function fs3(t: LayoutNode, pane: PaneNo, axis: Axis): boolean {
  const rects = rectsOf(t);
  const r = rects[pane]!;
  return axis === "x"
    ? crossing(rects, "x", r.top, r.top + r.height) <= 3
    : crossing(rects, "y", r.left, r.left + r.width) <= 2;
}
function splitPaths(t: LayoutNode, at: number[] = []): number[][] {
  if (isLeaf(t)) return [];
  return [at, ...t.children.flatMap((c, i) => splitPaths(c, [...at, i]))];
}
function nodeAt(t: LayoutNode, path: Path): LayoutNode {
  let n = t;
  for (const i of path) n = (n as SplitNode).children[i];
  return n;
}
/**
 * A tree the owner could reach: splits FS-3 allows (into the lowest free id)
 * and drags (new sizes on a node) that keep FS-3, in random order.
 */
function reachable(rand: () => number, steps: number): LayoutNode {
  let t: LayoutNode = { pane: 1 };
  for (let s = 0; s < steps; s++) {
    const ls = leaves(t);
    if (rand() < 0.65) {
      const id = ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] as PaneNo[]).find((n) => !ls.includes(n));
      const pane = pick(rand, ls);
      const axis: Axis = rand() < 0.5 ? "x" : "y";
      if (id !== undefined && fs3(t, pane, axis)) t = insertBeside(t, pane, axis, id);
    } else {
      const paths = splitPaths(t);
      if (!paths.length) continue;
      const path = pick(rand, paths);
      const k = (nodeAt(t, path) as SplitNode).children.length;
      const raw = Array.from({ length: k }, () => 0.04 + rand());
      const sum = raw.reduce((a, b) => a + b, 0);
      const next = withSizes(t, path, raw.map((v) => v / sum));
      if (most(next, "x") <= 4 && most(next, "y") <= 3) t = next;
    }
  }
  return t;
}
/** The same tree as a workspace, the focus on one of its panes. */
const wsOf = (tree: LayoutNode, focused: PaneNo = leaves(tree)[0]): Workspace =>
  ({ ...defaultWorkspace(), tree, focused });

/**
 * Twelve panes that still pass FS-3 on panes 1 and 11 (found by a seeded
 * search): FS-1's twelve is enforced by the id pool, not by FS-3 (§4.4), so
 * this is where "max" is the first reason.
 */
const TWELVE_ROOM = X(halves,
  Y([0.25, 0.25, 0.5], X(halves, 1, 11), X([0.25, 0.25, 0.5], 8, 12, 10), X(halves, 5, 6)),
  Y([0.5, 0.25, 0.25], 2, X(halves, 3, 9), X(halves, 4, 7)));

// ---- removed ---------------------------------------------------------------------

describe("the ladder is gone (FS4 card)", () => {
  it("LADDER, MAX_DRAWN and autoSplitVerdict are no longer exported", () => {
    const mod = W as Record<string, unknown>;
    expect(mod.LADDER).toBeUndefined();
    expect(mod.MAX_DRAWN).toBeUndefined();
    expect(mod.autoSplitVerdict).toBeUndefined();
    expect(typeof mod.autoSplit).toBe("function");
  });
});

// ---- the verdict: FS-3, geometric (§4.1, §4.2) -------------------------------------

describe("the verdict: four across, three down, counted over the whole window (FS-3, §4.2)", () => {
  it("1. one pane: right gives x[1,2], down gives y[1,2]", () => {
    const one = defaultWorkspace();
    expect(verdict(one, 1, "x")).toEqual({ ok: true, why: "" });
    expect(verdict(one, 1, "y")).toEqual({ ok: true, why: "" });
    expectTree(split(one, 1, "x").tree, X(halves, 1, 2), "right");
    expectTree(split(one, 1, "y").tree, Y(halves, 1, 2), "down");
  });

  it("2. x[1,2,3,4]: right is ✗ (across) on every pane; down on 2 gives x[1, y[2,5], 3, 4]", () => {
    const four = v2(X(quarters, 1, 2, 3, 4));
    for (const n of [1, 2, 3, 4]) expect(verdict(four, n, "x"), `right on ${n}`).toEqual({ ok: false, why: "across" });
    expect(verdict(four, 2, "y")).toEqual({ ok: true, why: "" });
    const w = split(four, 2, "y");
    expectTree(w.tree, X(quarters, 1, Y(halves, 2, 5), 3, 4));
    expect([most(w.tree, "x"), most(w.tree, "y")]).toEqual([4, 2]);
  });

  it("3. 4×2: down on 1, then 5 is ✗ (down) and 6 is ok — no vertical line crosses more than three [counting lines on an edge]", () => {
    const grid8 = setPreset(defaultWorkspace(), "grid8");
    for (const n of orderOf(grid8)) expect(verdict(grid8, n, "x"), `right on ${n}`).toEqual({ ok: false, why: "across" });
    expect(verdict(grid8, 1, "y")).toEqual({ ok: true, why: "" });
    const a = split(grid8, 1, "y");
    expect(orderOf(a).slice(0, 2)).toEqual([1, 9]);
    for (const n of [1, 9, 5]) expect(verdict(a, n, "y"), `down on ${n}`).toEqual({ ok: false, why: "down" });
    expect(verdict(a, 6, "y")).toEqual({ ok: true, why: "" });
    const b = split(a, 6, "y");
    expect(leaves(b.tree)).toContain(10);
    expect(most(b.tree, "y")).toBe(3);
    // Column 2 holds 2, 6, 10 now; column 3 still has room.
    for (const n of [2, 6, 10]) expect(verdict(b, n, "y"), `down on ${n}`).toEqual({ ok: false, why: "down" });
    expect(verdict(b, 7, "y")).toEqual({ ok: true, why: "" });
  });

  it("4. 3 over 2: down on 1, then 4 is ✗ (its band reaches column 1), 5 is ok", () => {
    const grid5 = setPreset(defaultWorkspace(), "grid5");
    const a = split(grid5, 1, "y");
    expect(verdict(a, 4, "y")).toEqual({ ok: false, why: "down" });
    expect(verdict(a, 5, "y")).toEqual({ ok: true, why: "" });
  });

  it("5. staggered x[y[x[1,2],3],4]: right on 4 (3 → 4) then right on 3 (2 → 3); then every right is ✗ (erratum: 4/4)", () => {
    const st = v2(X(halves, Y(halves, X(halves, 1, 2), 3), 4));
    expect(verdict(st, 4, "x")).toEqual({ ok: true, why: "" });
    const a = split(st, 4, "x");
    expect(verdict(a, 3, "x")).toEqual({ ok: true, why: "" });
    expect(verdict(a, 1, "x")).toEqual({ ok: false, why: "across" });
    const b = split(a, 3, "x");
    for (const n of orderOf(b)) expect(verdict(b, n, "x"), `right on ${n}`).toEqual({ ok: false, why: "across" });
    expect(verdict(b, 4, "y")).toEqual({ ok: true, why: "" });
  });

  it("6. the full 4×3, rows first and columns first: twelve panes, then every split is ✗ by FS-3", () => {
    let rows = defaultWorkspace();
    rows = split(rows, 1, "y");
    rows = split(rows, 2, "y"); // a sibling insert: y[1,2,3]
    expectTree(rows.tree, Y([0.5, 0.25, 0.25], 1, 2, 3));
    for (const first of [1, 2, 3]) {
      rows = split(rows, first, "x");
      rows = split(rows, first, "x");
      const row = leaves(rows.tree).find((n) => n !== first && verdict(rows, n, "x").ok
        && Math.abs(rectsOf(rows.tree)[n]!.top - rectsOf(rows.tree)[P(first)]!.top) < 1e-9);
      expect(row, `a third right in the row of ${first}`).toBeDefined();
      rows = split(rows, row!, "x");
    }
    let cols = defaultWorkspace();
    cols = split(cols, 1, "x");
    cols = split(cols, 1, "x");
    cols = split(cols, 2, "x"); // x[1,3,2,4]
    for (const top of orderOf(cols).slice()) {
      cols = split(cols, top, "y");
      cols = split(cols, top, "y");
    }
    for (const w of [rows, cols]) {
      expect(orderOf(w)).toHaveLength(12);
      expect([most(w.tree, "x"), most(w.tree, "y")]).toEqual([4, 3]);
      for (const n of orderOf(w)) {
        expect(verdict(w, n, "x"), `right on ${n}`).toEqual({ ok: false, why: "across" });
        expect(verdict(w, n, "y"), `down on ${n}`).toEqual({ ok: false, why: "down" });
      }
    }
  });

  it("never assumes a node has at most four children: a flattened six-wide row refuses right, the pane below it still splits right", () => {
    // parseTree caps children before normalize, so x[x[1,2,3], x[4,5,6]] reads as one six-wide row.
    const six = v2(Y(halves, X(halves, X(thirds, 1, 2, 3), X(thirds, 4, 5, 6)), 7));
    const top = six.tree as SplitNode;
    expect((top.children[0] as SplitNode).children).toHaveLength(6);
    for (const n of [1, 2, 3, 4, 5, 6]) expect(verdict(six, n, "x"), `right on ${n}`).toEqual({ ok: false, why: "across" });
    expect(verdict(six, 7, "x")).toEqual({ ok: true, why: "" });
    expect(verdict(six, 1, "y")).toEqual({ ok: true, why: "" });
    // And a stored tree over the cap is drawn as it is; splits on its lines stay disabled.
    const over = v2(X(halves, X(thirds, 1, 2, 3), X(thirds, 4, 5, 6)));
    expect(orderOf(over)).toEqual([1, 2, 3, 4, 5, 6]);
    for (const n of orderOf(over)) expect(verdict(over, n, "x").why, `right on ${n}`).toBe("across");
  });

  it("counts on what is drawn: the fitted rects, not the stored sizes", () => {
    // Left column: a three-wide row over pane 4, row edge at 0.65. Right
    // column: pane 5 over a row of a two-high stack and pane 7, edge at 0.7.
    // Stored, lines between the edges cross 4 and 5 only. At 1000 px tall the
    // right column's lower row needs 400 px, so its edge is drawn at 0.6 — above
    // the left one — and lines in (0.6, 0.65) cross 1, 2, 3, 6 and 7: five.
    const st = v2(X([0.4, 0.6], Y([0.65, 0.35], X(thirds, 1, 2, 3), 4), Y([0.7, 0.3], 5, X(halves, Y(halves, 6, 8), 7))));
    expect([most(st.tree, "x"), most(st.tree, "y")]).toEqual([4, 3]);
    const fit = fitAt(st, 3000 + 72, 1000);
    expect(fit.dropped).toBe(0);
    expect(fit.boxes[5]!.rect.height).toBeCloseTo(0.6, 9);
    expect(mostDrawn(fit, "x")).toBe(5);
    // Stored rects: right on 7 passes FS-3 (its band meets 4, 6/8 and 7), and 7 is 900 px wide.
    expect(fs3(st.tree, 7, "x")).toBe(true);
    expect(fit.boxes[7]!.px.width).toBeGreaterThanOrEqual(720);
    expect(verdict(st, 7, "x", unmeasured(st))).toEqual({ ok: true, why: "" });
    // Drawn rects: its band reaches the line that already crosses five.
    expect(verdict(st, 7, "x", fit)).toEqual({ ok: false, why: "across" });
  });
});

// ---- FS-4, dropped, max, hidden-edit and the order of reasons ----------------------------

describe("the verdict's other reasons (FS-4, P-6, FS-1), first reason wins (§5.1)", () => {
  it("FS-4 at 719/720 px wide and 399/400 px tall, on the fitted size", () => {
    const one = defaultWorkspace();
    expect(fitAt(one, 792, 1000).boxes[1]!.px.width).toBe(720);
    expect(verdict(one, 1, "x", fitAt(one, 792, 1000))).toEqual({ ok: true, why: "" });
    expect(verdict(one, 1, "x", fitAt(one, 791, 1000))).toEqual({ ok: false, why: "room" });
    expect(verdict(one, 1, "y", fitAt(one, 791, 400))).toEqual({ ok: true, why: "" });
    expect(verdict(one, 1, "y", fitAt(one, 791, 399))).toEqual({ ok: false, why: "room" });
    // A nested pane: two columns at 1440 wide are 720 each; at 1439, 719.5.
    const two = setPreset(one, "cols2");
    expect(verdict(two, 2, "x", fitAt(two, 72 + 1440, 1000)).ok).toBe(true);
    expect(verdict(two, 2, "x", fitAt(two, 72 + 1439, 1000)).why).toBe("room");
    // Unmeasured: FS-4 is skipped.
    expect(verdict(one, 1, "x", unmeasured(one)).ok).toBe(true);
  });

  it("FS-7: 1920 px with both side panes open draws 684 px columns; a third is disabled, never borrowing from the side panes", () => {
    const two = split(defaultWorkspace(), 1, "x", fitWorkspace(DEFAULT_LAYOUT, defaultWorkspace(), 1920, 1052));
    const fit = fitWorkspace(DEFAULT_LAYOUT, two, 1920, 1052);
    expect([fit.sides.leftFolded, fit.sides.rightFolded, fit.width]).toEqual([false, false, 1368]);
    expect(fit.boxes[1]!.px.width).toBe(684);
    for (const n of [1, 2]) expect(verdict(two, n, "x", fit), `right on ${n}`).toEqual({ ok: false, why: "room" });
    expect(verdict(two, 1, "y", fit)).toEqual({ ok: true, why: "" });
    // Folded, the same two columns are 924 wide and split again.
    expect(verdict(two, 1, "x", fitAt(two, 1920, 1052)).ok).toBe(true);
  });

  it("\"dropped\" while the window drops panes, before anything else (P-6)", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const fit = fitWorkspace(DEFAULT_LAYOUT, three, 900, 700);
    expect(fit.dropped).toBe(1);
    for (const n of fit.panes) for (const axis of ["x", "y"] as const) {
      expect(verdict(three, n, axis, fit), `${axis} on ${n}`).toEqual({ ok: false, why: "dropped" });
    }
    // 4×2 drawn narrow: dropped wins over across.
    const eight = setPreset(defaultWorkspace(), "grid8");
    const narrow = fitWorkspace(DEFAULT_LAYOUT, eight, 1280, 772);
    expect(narrow.dropped).toBeGreaterThan(0);
    expect(verdict(eight, narrow.panes[0], "x", narrow).why).toBe("dropped");
  });

  it("across before room: four columns in a narrow window say across, not room", () => {
    const four = v2(X(quarters, 1, 2, 3, 4));
    const fit = fitAt(four, 72 + 1500, 800);
    expect(fit.dropped).toBe(0);
    expect(verdict(four, 1, "x", fit).why).toBe("across");
    expect(verdict(four, 1, "y", fit).ok).toBe(true);
  });

  it("\"max\" with twelve leaves, where FS-3 and FS-4 would allow the split", () => {
    const twelve = v2(TWELVE_ROOM);
    expect(orderOf(twelve)).toHaveLength(12);
    const fit = fitAt(twelve, 3000 + 72, 1700);
    expect(fit.dropped).toBe(0);
    expect(fs3(twelve.tree, 1, "x") && fs3(twelve.tree, 11, "x")).toBe(true);
    expect(fit.boxes[1]!.px.width).toBeGreaterThanOrEqual(720);
    expect(verdict(twelve, 1, "x", fit)).toEqual({ ok: false, why: "max" });
    expect(verdict(twelve, 11, "x", fit)).toEqual({ ok: false, why: "max" });
    // room comes before max: the same window, too narrow for pane 1.
    expect(verdict(twelve, 1, "x", fitAt(twelve, 2000 + 72, 1700)).why).toBe("room");
    // And a twelfth-pane split is refused outright, whatever id is passed.
    for (const id of [1, 5, 12]) expect(splitPane(twelve, fit, P(1), "x", P(id), chat())).toBe(twelve);
  });

  it("\"hidden-edit\" when every id not drawn is hidden with an unsaved edit", () => {
    // 1 drawn, the other eleven hidden by ⊞.
    const one = parseWorkspace(JSON.stringify({
      version: 2, tree: 1, kept: [9, 10, 11, 12, 2, 3, 4, 5, 6, 7, 8],
      panes: Array.from({ length: 12 }, () => ({ view: "chat" })),
    }));
    const fit = fitAt(one, 1920, 1052);
    const all = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] as PaneNo[];
    expect(verdict(one, 1, "x", fit, all)).toEqual({ ok: false, why: "hidden-edit" });
    expect(verdict(one, 1, "y", fit, all)).toEqual({ ok: false, why: "hidden-edit" });
    expect(verdict(one, 1, "x", fit, all.filter((n) => n !== 2))).toEqual({ ok: true, why: "" });
    // room before hidden-edit.
    expect(verdict(one, 1, "x", fitAt(one, 791, 1052), all).why).toBe("room");
  });
});

describe("the buttons' titles (§10)", () => {
  it("say what each does, or why it cannot", () => {
    expect(splitTitle("", "x")).toBe("Split right: a copy of this pane beside it");
    expect(splitTitle("", "y")).toBe("Split down: a copy of this pane below it");
    expect(splitTitle("across", "x")).toBe("Four panes across the window is the most");
    expect(splitTitle("down", "y")).toBe("Three panes down the window is the most");
    expect(splitTitle("room", "x"))
      .toBe("Not enough room to split right: widen the window, fold a side pane or zoom out (Ctrl+-)");
    const down = splitTitle("room", "y");
    expect(down.startsWith("Not enough room to split down")).toBe(true);
    expect(down).toContain("zoom out (Ctrl+-)");
    expect(splitTitle("dropped", "x", 2, 3)).toBe("Showing 2 of 3 panes: widen the window or close a pane to split");
    expect(splitTitle("max", "x")).toBe("Twelve panes is the most");
    expect(splitTitle("max", "y")).toBe("Twelve panes is the most");
    expect(splitTitle("hidden-edit", "y")).toBe("A hidden pane has an unsaved edit: show it from ⊞ first");
  });
});

// ---- the split ----------------------------------------------------------------------------

describe("the split (§5.1)", () => {
  it("halves only the pane split: case B a sibling in a same-direction parent, case A a new node", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    expectTree(split(three, 2, "x").tree, X([1 / 3, 1 / 6, 1 / 6, 1 / 3], 1, 2, 4, 3), "case B");
    expectTree(split(three, 2, "y").tree, X(thirds, 1, Y(halves, 2, 4), 3), "case A");
    // The new pane comes right after its source in reading order, and takes focus.
    const w = split(three, 2, "y");
    expect([orderOf(w), w.focused]).toEqual([[1, 2, 4, 3], 4]);
  });

  it("takes focus unless asked not to; the id leaves kept and shows a copy of the spec", () => {
    const w = focusPane(setPreset(setPreset(defaultWorkspace(), "grid8"), "cols2"), 1);
    expect(w.kept).toEqual([3, 4, 5, 6, 7, 8]);
    const spec: PaneSpec = { view: "browser", projectId: "p1", terminalId: null };
    const quiet = splitPane(w, unmeasured(w), 2, "y", 8, spec, { focus: false });
    expect([orderOf(quiet), quiet.focused, quiet.kept]).toEqual([[1, 2, 8], 1, [3, 4, 5, 6, 7]]);
    expect(quiet.panes[7]).toEqual(spec);
    expect(quiet.panes[7]).not.toBe(spec);
    expect(splitPane(w, unmeasured(w), 2, "y", 8, spec).focused).toBe(8);
  });

  it("refuses an id already drawn (insertBeside would duplicate it), a source not drawn, a bad id, and any verdict that is not ok", () => {
    const w = setPreset(defaultWorkspace(), "cols2");
    const fit = unmeasured(w);
    expect(splitPane(w, fit, 1, "x", 2, chat())).toBe(w);
    expect(splitPane(w, fit, 1, "y", 1, chat())).toBe(w);
    expect(splitPane(w, fit, 5, "x", 3, chat())).toBe(w);
    expect(splitPane(w, fit, 1, "x", 13 as PaneNo, chat())).toBe(w);
    expect(splitPane(w, fit, 1, "x", 0 as PaneNo, chat())).toBe(w);
    const four = v2(X(quarters, 1, 2, 3, 4));
    expect(splitPane(four, unmeasured(four), 1, "x", 5, chat())).toBe(four); // across
    const three = setPreset(defaultWorkspace(), "cols3");
    expect(splitPane(three, fitWorkspace(DEFAULT_LAYOUT, three, 900, 700), 1, "y", 4, chat())).toBe(three); // dropped
    const one = defaultWorkspace();
    expect(splitPane(one, fitAt(one, 791, 1000), 1, "x", 2, chat())).toBe(one); // room
    // Not vacuous: the same calls with a free id, a drawn source and a verdict that is ok do split.
    expect(orderOf(splitPane(w, fit, 1, "y", 3, chat()))).toEqual([1, 3, 2]);
    expect(orderOf(splitPane(one, fitAt(one, 792, 1000), 1, "x", 2, chat()))).toEqual([1, 2]);
  });

  it("never draws one id twice, and stops at twelve with \"max\" (property, carry-forward from FS1)", () => {
    const rand = rng(0xf54);
    for (let run = 0; run < 150; run++) {
      let w = defaultWorkspace();
      for (let s = 0; s < 40; s++) {
        const fit = unmeasured(w);
        const source = pick(rand, orderOf(w));
        const axis: Axis = rand() < 0.5 ? "x" : "y";
        // Any id at all, drawn ones included: the split itself must refuse a duplicate.
        const id = P(1 + Math.floor(rand() * 12));
        const next = splitPane(w, fit, source, axis, id, chat());
        if (orderOf(w).includes(id)) expect(next, `id ${id} is already drawn`).toBe(w);
        w = next;
        const ids = orderOf(w);
        expect(new Set(ids).size, JSON.stringify(storeTree(w.tree))).toBe(ids.length);
        expect(ids.length).toBeLessThanOrEqual(12);
      }
    }
    const twelve = v2(TWELVE_ROOM);
    const fit = fitAt(twelve, 3072, 1700);
    for (const n of orderOf(twelve)) for (const axis of ["x", "y"] as const) {
      expect(verdict(twelve, n, axis, fit).ok, `${axis} on ${n}`).toBe(false);
    }
  });

  it("commits the fitted sizes along the source's path first (C3): a clamped parent's halves are equal on screen", () => {
    // x[1, 2] stored 0.9 / 0.1; at 1500 px pane 2 is clamped up to 360, pane 1 drawn 1140.
    const w = v2(X([0.9, 0.1], 1, 2));
    const fit = fitAt(w, 1500 + 72, 1000);
    expect(fit.boxes[1]!.px.width).toBeCloseTo(1140, 9);
    expect(fit.boxes[2]!.px.width).toBeCloseTo(360, 9);
    const next = splitPane(w, fit, 1, "x", 3, chat());
    expectTree(next.tree, X([0.38, 0.38, 0.24], 1, 3, 2));
    const after = fitAt(next, 1500 + 72, 1000);
    expect(after.boxes[1]!.px.width).toBeCloseTo(570, 9);
    expect(after.boxes[3]!.px.width).toBeCloseTo(570, 9);
    expect(after.boxes[2]!.px.width).toBeCloseTo(360, 9);
    // Nodes off the path keep their stored sizes.
    const off = v2(Y(halves, X([0.9, 0.1], 1, 2), X([0.9, 0.1], 3, 4)));
    const fitOff = fitAt(off, 1500 + 72, 1000);
    const nextOff = splitPane(off, fitOff, 1, "x", 5, chat());
    expectTree(nextOff.tree, Y(halves, X([0.38, 0.38, 0.24], 1, 5, 2), X([0.9, 0.1], 3, 4)));
  });
});

// ---- FS-5: nothing else moves (§5.3) ----------------------------------------------------------

describe("FS-5: a split halves its pane and moves nothing else, to the pixel (§5.3)", () => {
  /** A window to fit in: the side panes open, folded or one of each; the panel open or not. */
  const windows = (rand: () => number) => {
    const layouts: PaneLayout[] = [DEFAULT_LAYOUT, FOLDED, { ...DEFAULT_LAYOUT, leftCollapsed: true }];
    return {
      layout: pick(rand, layouts),
      available: 900 + Math.floor(rand() * 3200),
      availableH: 500 + Math.floor(rand() * 1700),
      panel: rand() < 0.3 ? { open: true, height: 120 + Math.floor(rand() * 400) } : null,
    };
  };

  it("over seeded random reachable trees × windows, clamping ones included: every other box, the side panes and the panel are identical (===) [skipping C3; splitting from stored rather than fitted sizes]", () => {
    const rand = rng(0x5f5);
    let checked = 0;
    let clamped = 0;
    let caseB = 0;
    for (let run = 0; run < 400; run++) {
      const tree = reachable(rand, 4 + Math.floor(rand() * 30));
      for (let k = 0; k < 3; k++) {
        const win = windows(rand);
        let ws = wsOf(tree, pick(rand, leaves(tree)));
        if (win.panel) ws = setPanel(ws, win.panel);
        const before = fitWorkspace(win.layout, ws, win.available, win.availableH);
        if (before.dropped > 0) continue;
        const id = allocate(ws);
        if (id === null) continue;
        for (const source of before.panes) for (const axis of ["x", "y"] as const) {
          if (!splitVerdict(ws, before, source, axis).ok) continue;
          const next = splitPane(ws, before, source, axis, id, chat());
          expect(next).not.toBe(ws);
          const after = fitWorkspace(win.layout, next, win.available, win.availableH);
          const what = `${axis} on ${source} in ${JSON.stringify(storeTree(ws.tree))} at ${win.available}×${win.availableH}`;
          expect(after.dropped, what).toBe(0);
          expect(after.sides, what).toEqual(before.sides);
          expect(after.panel, what).toEqual(before.panel);
          for (const n of before.panes) {
            if (n === source) continue;
            const a = after.boxes[n]!.px;
            const b = before.boxes[n]!.px;
            expect(Object.is(a.width, b.width) && Object.is(a.height, b.height), `${what}: pane ${n} ${JSON.stringify(b)} → ${JSON.stringify(a)}`).toBe(true);
          }
          const w = before.boxes[source]!.px;
          for (const half of [source, id]) {
            const got = after.boxes[half]!.px;
            const want = axis === "x" ? { width: w.width / 2, height: w.height } : { width: w.width, height: w.height / 2 };
            expect(Math.abs(got.width - want.width), `${what}: half ${half} width`).toBeLessThan(1e-6);
            expect(Math.abs(got.height - want.height), `${what}: half ${half} height`).toBeLessThan(1e-6);
          }
          checked++;
          // How often the window clamps the source, and how often the split is a sibling insert there (case B).
          const stored = rectsOf(ws.tree)[source]!;
          const drawn = before.boxes[source]!.rect;
          const moved = Math.abs(stored.width - drawn.width) > 1e-6 || Math.abs(stored.height - drawn.height) > 1e-6;
          if (moved) clamped++;
          const path = pathOf(ws.tree, source)!;
          if (moved && path.length > 0 && (nodeAt(ws.tree, path.slice(0, -1)) as SplitNode).axis === axis) caseB++;
        }
      }
    }
    // The property is not vacuous: many splits, and many where the window clamps.
    expect(checked).toBeGreaterThan(2000);
    expect(clamped).toBeGreaterThan(500);
    expect(caseB).toBeGreaterThan(200);
  });
});

// ---- close (§5.2) -------------------------------------------------------------------------------

describe("close gives the space to the neighbour in the same split (§5.2)", () => {
  it("the previous sibling takes it, else the next; panes outside the parent keep their boxes", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    expectTree(closePane(three, 2).tree, X([2 / 3, 1 / 3], 1, 3), "× on the middle column: the left one takes it");
    expectTree(closePane(three, 1).tree, X([2 / 3, 1 / 3], 2, 3), "× on the first: the next one takes it");
    const grid = setPreset(defaultWorkspace(), "grid6");
    const fit = fitAt(grid, 1920, 1052);
    const closed = closePane(grid, 5);
    const after = fitAt(closed, 1920, 1052);
    for (const n of [1, 2, 3, 6] as PaneNo[]) expect(after.boxes[n]!.px, `pane ${n}`).toEqual(fit.boxes[n]!.px);
    expect(after.boxes[4]!.px.width).toBeCloseTo(fit.boxes[4]!.px.width + fit.boxes[5]!.px.width, 9);
    expect(after.sides).toEqual(fit.sides);
  });

  it("a two-child parent collapses into the survivor, flattened into a same-axis grandparent", () => {
    const w = v2(X(halves, 1, Y(halves, 2, X(halves, 3, 4))));
    expectTree(closePane(w, 2).tree, X([0.5, 0.25, 0.25], 1, 3, 4));
  });

  it("focus goes to `then` when drawn, else the heir's leaf nearest the closed pane — not the pane in its slot [the old slot rule]", () => {
    // x[y[1,2], 3]: × on 2 gives its space to 1. The old slot rule put focus on 3.
    const w = focusPane(v2(X(halves, Y(halves, 1, 2), 3)), 2);
    expect(closePane(w, 2).focused).toBe(1);
    expect(closePane(w, 2, null).focused).toBe(1);
    expect(closePane(w, 2, 3).focused).toBe(3);
    expect(closePane(w, 2, 2).focused).toBe(1); // `then` is the closed pane: not drawn after it
    // A heir that is a subtree: its leaf nearest the closed pane.
    const deep = focusPane(v2(X(halves, Y(halves, 1, 2), Y(halves, 3, 4))), 3);
    expect(closePane(deep, 3).focused).toBe(4); // y[3,4]: 3 first, so 4 takes it
    const left = focusPane(v2(X([0.25, 0.25, 0.5], Y(halves, 1, 2), 5, Y(halves, 3, 4))), 5);
    expect(closePane(left, 5).focused).toBe(2); // the previous sibling's last leaf
    // Focus on another pane stays where it is.
    expect(closePane(focusPane(w, 3), 2).focused).toBe(3);
  });

  it("split, then × on the new pane, gives back the same tree (property)", () => {
    const rand = rng(0xc105e);
    let checked = 0;
    for (let run = 0; run < 600; run++) {
      const tree = reachable(rand, 4 + Math.floor(rand() * 25));
      const ws = wsOf(tree, pick(rand, leaves(tree)));
      const fit = unmeasured(ws);
      const id = allocate(ws);
      if (id === null) continue;
      const source = pick(rand, leaves(tree));
      const axis: Axis = rand() < 0.5 ? "x" : "y";
      if (!splitVerdict(ws, fit, source, axis).ok) continue;
      const back = closePane(splitPane(ws, fit, source, axis, id, chat()), id);
      expect(storeTree(back.tree), `${axis} on ${source} of ${JSON.stringify(storeTree(tree))}`).toEqual(storeTree(tree));
      expect(back.focused).toBe(source);
      checked++;
    }
    expect(checked).toBeGreaterThan(150);
  });

  it("removeLeaf is what close does to the tree", () => {
    const rand = rng(0xc1);
    for (let run = 0; run < 100; run++) {
      const tree = reachable(rand, 6 + Math.floor(rand() * 20));
      if (isLeaf(tree)) continue;
      const pane = pick(rand, leaves(tree));
      const ws = wsOf(tree, pane);
      expect(storeTree(closePane(ws, pane).tree)).toEqual(storeTree(removeLeaf(tree, pane)!.tree));
      expect(closePane(ws, pane).focused).toBe(removeLeaf(tree, pane)!.heir);
    }
  });
});

// ---- autoSplit (dev servers) ------------------------------------------------------------------------

describe("autoSplit: right, else down, never folding, dropping or taking a pane that holds something", () => {
  it("prefers right; down when right has no room; null when neither has", () => {
    const one = defaultWorkspace();
    expect(autoSplit(one, fitAt(one, 1920, 1052), 1)).toEqual({ axis: "x", id: 2 });
    expect(autoSplit(one, fitAt(one, 791, 1052), 1)).toEqual({ axis: "y", id: 2 });
    expect(autoSplit(one, fitAt(one, 791, 399), 1)).toBeNull();
    const four = v2(X(quarters, 1, 2, 3, 4));
    expect(autoSplit(four, unmeasured(four), 3)).toEqual({ axis: "y", id: 5 });
  });

  it("takes a free id, or a blank hidden one, never allocate's destructive tiers (held ids skipped)", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    const fit = fitAt(one, 1920, 1052);
    expect(autoSplit(one, fit, 1)).toEqual({ axis: "x", id: 9 });
    expect(autoSplit(one, fit, 1, [9, 10])).toEqual({ axis: "x", id: 11 });
    // Every id used, and every hidden pane holds something: nothing is taken.
    const used = parseWorkspace(JSON.stringify({
      version: 2, tree: 1, kept: [9, 10, 11, 12, 2, 3, 4, 5, 6, 7, 8],
      panes: Array.from({ length: 12 }, (_, i) => ({ view: "file", projectId: `p${i}` })),
    }));
    expect(freeId(used)).toBeNull();
    expect(allocate(used)).not.toBeNull();
    expect(autoSplit(used, fitAt(used, 1920, 1052), 1)).toBeNull();
  });

  it("is null while the window drops panes, and for a twelfth pane", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    expect(autoSplit(three, fitWorkspace(DEFAULT_LAYOUT, three, 900, 700), 1)).toBeNull();
    const twelve = v2(TWELVE_ROOM);
    expect(autoSplit(twelve, fitAt(twelve, 3072, 1700), 1)).toBeNull();
  });

  it("whatever it returns splits without moving the side panes, the panel or any other pane, and keeps focus (property)", () => {
    const rand = rng(0xa070);
    let checked = 0;
    for (let run = 0; run < 500; run++) {
      const tree = reachable(rand, 3 + Math.floor(rand() * 20));
      const ws = wsOf(tree, pick(rand, leaves(tree)));
      const layout = pick(rand, [DEFAULT_LAYOUT, FOLDED]);
      const aw = 900 + Math.floor(rand() * 2400);
      const ah = 500 + Math.floor(rand() * 1200);
      const before = fitWorkspace(layout, ws, aw, ah);
      const source = pick(rand, before.panes);
      const got = autoSplit(ws, before, source);
      if (!got) continue;
      expect(splitVerdict(ws, before, source, got.axis).ok).toBe(true);
      if (got.axis === "y") expect(splitVerdict(ws, before, source, "x").ok, "right first").toBe(false);
      const next = splitPane(ws, before, source, got.axis, got.id, defaultPane(got.id), { focus: false });
      const after = fitWorkspace(layout, next, aw, ah);
      expect(after.dropped).toBe(0);
      expect(after.sides).toEqual(before.sides);
      expect(after.panel).toEqual(before.panel);
      expect(next.focused).toBe(ws.focused);
      for (const n of before.panes) if (n !== source) expect(after.boxes[n]!.px).toEqual(before.boxes[n]!.px);
      checked++;
    }
    expect(checked).toBeGreaterThan(150);
  });
});

// ---- the FS-3 drag stop (§4.3) ------------------------------------------------------------------------

describe("a drag stops at the cap (§4.3)", () => {
  // Left column: a three-wide row over pane 4, edge at 0.3. Right column: pane 5
  // over a two-wide row, edge at 0.6. Lines between the two edges cross 1 + 1;
  // if the left edge passed the right one, lines between them would cross 3 + 2.
  const stagger = () => v2(X(halves, Y([0.3, 0.7], X(thirds, 1, 2, 3), 4), Y([0.6, 0.4], 5, X(halves, 6, 7))));
  const H = 1000;

  it("the left column's row edge stops at the right column's, and its range says so", () => {
    const ws = stagger();
    const fit = fitAt(ws, 2928 + 72, H);
    expect([mostDrawn(fit, "x"), mostDrawn(fit, "y")]).toEqual([4, 2]);
    const handle = fit.edges.find((e) => e.testid === "split-y0-1")!;
    expect(handle).toBeDefined();
    expect(handle.value).toBeCloseTo(300, 6);
    expect(handle.max).toBeCloseTo(600, 3);
    const moved = moveEdge(ws, fit, handle, 800);
    const after = fitAt(moved, 2928 + 72, H);
    expect(mostDrawn(after, "x")).toBeLessThanOrEqual(4);
    expect(after.boxes[1]!.rect.height).toBeGreaterThan(0.6 - 1e-3);
    expect(after.boxes[1]!.rect.height).toBeLessThan(0.6 + 1e-3);
  });

  it("the right column's row edge stops at the left column's, coming up", () => {
    const ws = stagger();
    const fit = fitAt(ws, 2928 + 72, H);
    const handle = fit.edges.find((e) => e.testid === "split-y1-1")!;
    expect(handle.min).toBeCloseTo(300, 3);
    const moved = moveEdge(ws, fit, handle, 100);
    const after = fitAt(moved, 2928 + 72, H);
    expect(mostDrawn(after, "x")).toBeLessThanOrEqual(4);
    expect(after.boxes[5]!.rect.height).toBeGreaterThan(0.3 - 1e-3);
    // The stop never narrows a drag that cannot break the cap (2×2's linked column edge).
    const grid = setPreset(defaultWorkspace(), "grid4");
    const gfit = fitAt(grid, 1920, 1052);
    const col = gfit.edges.find((e) => e.testid === "split-x0-1")!;
    expect([col.min, col.max]).toEqual([360, gfit.width - 360]);
  });

});

// ---- ⊞ after a free layout (FS-8) -----------------------------------------------------------------

describe("⊞ keeps no memory of a custom shape (FS-8)", () => {
  it("a preset replaces a free tree at its default sizes; the free tree is no preset", () => {
    let w = split(defaultWorkspace(), 1, "y");
    w = split(w, 2, "x");
    expect(presetOfWs(w)).toBeNull();
    const g = setPreset(w, "grid4");
    expect(storeTree(g.tree)).toEqual(storeTree(presetTree("grid4", orderOf(g))));
    const back = setPreset(setPreset(g, "single"), "cols3");
    expect(storeTree(back.tree)).toEqual(storeTree(presetTree("cols3", orderOf(back))));
    expect(orderOf(back)).toEqual(orderOf(w));
  });
});
