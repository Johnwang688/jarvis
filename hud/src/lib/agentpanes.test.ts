// A chat's ask for panes as one batch (2026-11-13, docs/plans/2026-11-13-
// agent-pane-grid-decisions.md D1/D3): free drawn panes first, then the
// window laid out once as a balanced grid (`gridTree`), never by halving the
// chat's pane until it is too small.

import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT, type PaneLayout } from "./layout";
import { AGENT_OPEN_MAX, agentRequestId, gridFor, placeAgentPanes, type AgentAsk } from "./agentpanes";
import {
  balancedRows, gridTree, isLeaf, leaves, MAX_ACROSS, MAX_DOWN, normalize, parseTree, rectsOf, storeTree,
  type LayoutNode, type StoredNode,
} from "./splittree";
import {
  defaultWorkspace, fitWorkspace, orderOf, parseWorkspace, PANE_MIN_H, PANE_MIN_W, PANE_NOS, setPreset, showSubagent,
  subagentSpec, type PaneNo, type Workspace,
} from "./workspace";

const FOLDED: PaneLayout = { ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true };
const P = (n: number) => n as PaneNo;
const ids = (k: number) => PANE_NOS.slice(0, k);
const fitAt = (ws: Workspace, w: number, h: number, layout: PaneLayout = FOLDED) => fitWorkspace(layout, ws, w, h);
const v2 = (tree: StoredNode, rest: Record<string, unknown> = {}): Workspace =>
  parseWorkspace(JSON.stringify({ version: 2, tree, ...rest }));
const sid = (n: number) => `cl:toolu_${String(n).padStart(4, "0")}`;
const ask = (n: number, threadId = "c1"): AgentAsk => {
  const ref = { threadId, sid: sid(n) };
  return { show: (w, pane) => showSubagent(w, pane, ref, false), spec: subagentSpec(ref) };
};
const asks = (k: number) => Array.from({ length: k }, (_, i) => ask(i + 1));
/** Rows of a grid tree, top to bottom, each its panes left to right. */
const rowsOf = (n: LayoutNode): PaneNo[][] => {
  if (isLeaf(n)) return [[n.pane]];
  if (n.axis === "x") return [leaves(n)];
  return n.children.map((c) => leaves(c));
};

describe("gridTree and balancedRows (lib/splittree)", () => {
  it("builds rows first, reading order depth first, equal sizes, already normal", () => {
    const t = gridTree(ids(11), [4, 4, 3])!;
    expect(rowsOf(t)).toEqual([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]]);
    expect(leaves(t)).toEqual(ids(11));
    expect(normalize(t)).toEqual(t);
    expect(parseTree(storeTree(t))).toEqual(t);
    expect(!isLeaf(t) && t.sizes).toEqual([1 / 3, 1 / 3, 1 / 3]);
    const r = rectsOf(t);
    expect(r[1]).toEqual({ left: 0, top: 0, width: 0.25, height: 1 / 3 });
    expect(r[11]!.width).toBeCloseTo(1 / 3);
  });

  it("a row of one is a leaf, one row is the x node, one pane is a leaf", () => {
    expect(gridTree([P(3)], [1])).toEqual({ pane: 3 });
    expect(gridTree(ids(3), [3])).toEqual({ axis: "x", children: [{ pane: 1 }, { pane: 2 }, { pane: 3 }], sizes: [1 / 3, 1 / 3, 1 / 3] });
    expect(gridTree(ids(2), [1, 1])).toEqual({ axis: "y", children: [{ pane: 1 }, { pane: 2 }], sizes: [0.5, 0.5] });
    expect(rowsOf(gridTree(ids(3), [2, 1])!)).toEqual([[1, 2], [3]]);
  });

  it("refuses what FS-3 or the ids refuse", () => {
    expect(gridTree(ids(5), [5])).toBeNull();
    expect(gridTree(ids(4), [1, 1, 1, 1])).toBeNull();
    expect(gridTree(ids(4), [2, 1])).toBeNull();
    expect(gridTree([P(1), P(1)], [2])).toBeNull();
    expect(gridTree([], [])).toBeNull();
    expect(gridTree(ids(2), [2, 0])).toBeNull();
  });

  it("balances rows longer first: 7 = 4 over 3, 11 = 4/4/3, 5 = 3 over 2", () => {
    expect(balancedRows(7, 2)).toEqual([4, 3]);
    expect(balancedRows(11, 3)).toEqual([4, 4, 3]);
    expect(balancedRows(5, 2)).toEqual([3, 2]);
    expect(balancedRows(12, 3)).toEqual([4, 4, 4]);
    expect(balancedRows(9, 2)).toBeNull();
    expect(balancedRows(2, 3)).toBeNull();
  });

  it("every count 1..12 has a grid within 4 across and 3 down", () => {
    for (let t = 1; t <= 12; t++) {
      const rows = gridFor(t, NaN, NaN)!;
      expect(rows.length).toBeLessThanOrEqual(MAX_DOWN);
      expect(Math.max(...rows)).toBeLessThanOrEqual(MAX_ACROSS);
      expect(rows.reduce((a, b) => a + b, 0)).toBe(t);
      expect(gridTree(ids(t), rows)).not.toBeNull();
    }
  });
});

describe("gridFor: fewest rows that fit the measured centre", () => {
  it("a wide window gets columns; a narrow one more rows; none when nothing fits", () => {
    expect(gridFor(4, 1500, 900)).toEqual([4]);
    expect(gridFor(4, 1000, 900)).toEqual([2, 2]);
    expect(gridFor(12, 4 * PANE_MIN_W, 3 * PANE_MIN_H)).toEqual([4, 4, 4]);
    expect(gridFor(12, 4 * PANE_MIN_W - 1, 900)).toBeNull();
    expect(gridFor(12, 1500, 3 * PANE_MIN_H - 1)).toBeNull();
    expect(gridFor(7, 1500, 300)).toBeNull();
    expect(gridFor(3, 1200, 300)).toEqual([3]);
    expect(gridFor(13, NaN, NaN)).toBeNull();
  });
});

describe("placeAgentPanes", () => {
  it("11 asks from a one-pane window: one 4x3 grid of 12, the chat at slot 1, focus unchanged", () => {
    const one = defaultWorkspace();
    const fit = fitAt(one, 1920, 1052);
    const out = placeAgentPanes(one, fit, [], asks(11));
    expect(out.grid).toEqual([4, 4, 4]);
    expect(out.placed).toEqual(ids(12).slice(1));
    expect(orderOf(out.ws)).toEqual(ids(12));
    expect(rowsOf(out.ws.tree)).toEqual([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]);
    expect(out.ws.focused).toBe(1);
    expect(out.ws.panes[0]).toEqual(one.panes[0]);
    for (let n = 2; n <= 12; n++) expect(out.ws.panes[n - 1].subagent).toEqual({ threadId: "c1", sid: sid(n - 1) });
    const after = fitAt(out.ws, 1920, 1052);
    expect(after.dropped).toBe(0);
    expect(after.sides).toEqual(fit.sides);
    expect(after.panel).toEqual(fit.panel);
    for (const n of after.panes) {
      expect(after.boxes[n]!.px.width).toBeGreaterThanOrEqual(PANE_MIN_W);
      expect(after.boxes[n]!.px.height).toBeGreaterThanOrEqual(PANE_MIN_H);
    }
  });

  it("a narrow window places what fits and counts the rest as not placed", () => {
    const one = defaultWorkspace();
    // 1200 - 72 (two rails) = 1128 wide: three columns; 700 - 28... three rows fit in 652.
    const fit = fitAt(one, 1200, 652);
    const out = placeAgentPanes(one, fit, [], asks(11));
    expect(out.grid).toEqual([3, 3, 3]);
    expect(out.placed.filter((n) => n !== null)).toHaveLength(8);
    expect(out.placed.slice(8)).toEqual([null, null, null]);
    expect(fitAt(out.ws, 1200, 652).dropped).toBe(0);
    // Too small for even two panes: nothing changes.
    const tiny = fitAt(one, 700, 380);
    const none = placeAgentPanes(one, tiny, [], asks(3));
    expect(none.ws).toBe(one);
    expect(none.placed).toEqual([null, null, null]);
  });

  it("free panes first (in the order given), keeping their spec; the grid only for the rest", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const fit = fitAt(three, 1920, 1052);
    const out = placeAgentPanes(three, fit, [P(3), P(2)], asks(4));
    expect(out.placed.slice(0, 2)).toEqual([3, 2]);
    expect(out.ws.panes[2]).toMatchObject({ ...three.panes[2], view: "subagent" });
    expect(out.ws.panes[1].browserUrl ?? null).toBe(three.panes[1].browserUrl ?? null);
    expect(out.grid).toEqual([3, 2]);
    expect(orderOf(out.ws)).toEqual([1, 2, 3, 4, 5]);
    expect(rowsOf(out.ws.tree)).toEqual([[1, 2, 3], [4, 5]]);
    // Only free asks: the tree does not move.
    const fits = placeAgentPanes(three, fit, [P(2), P(3)], asks(2));
    expect(fits.ws.tree).toBe(three.tree);
    expect(fits.grid).toBeNull();
  });

  it("a pane not in `free` is never taken: the window's rules (dirty, kept-own-work, focused) are its own", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const fit = fitAt(three, 1920, 1052);
    // Pane 2 is "dirty" and pane 3 a Task view: the window leaves them out of `free`.
    const out = placeAgentPanes(three, fit, [], asks(2));
    for (const n of [1, 2, 3] as PaneNo[]) expect(out.ws.panes[n - 1]).toEqual(three.panes[n - 1]);
    expect(out.placed).toEqual([4, 5]);
    // A pane listed but not drawn is ignored.
    const ghost = placeAgentPanes(three, fit, [P(9)], asks(1));
    expect(ghost.placed).toEqual([4]);
    expect(ghost.ws.panes[8]).toEqual(three.panes[8]);
  });

  it("never takes an id ⊞ hid, never drops or reorders a drawn pane", () => {
    const eight = setPreset(defaultWorkspace(), "grid8");
    const hid = setPreset(eight, "cols2"); // 3..8 kept
    expect(hid.kept.length).toBe(6);
    const fit = fitAt(hid, 1920, 1052);
    const out = placeAgentPanes(hid, fit, [], asks(11));
    expect(out.ws.kept).toEqual(hid.kept);
    for (const n of hid.kept) expect(out.ws.panes[n - 1]).toEqual(hid.panes[n - 1]);
    // Free ids: 9..12 only, so 2 + 4 = 6 panes.
    expect(orderOf(out.ws)).toEqual([1, 2, 9, 10, 11, 12]);
    expect(out.placed.filter((n) => n !== null)).toEqual([9, 10, 11, 12]);
  });

  it("keeps the drawn reading order: the chat stays first even when it was not top-left in the tree's ids", () => {
    const ws = v2({ axis: "y", children: [{ axis: "x", children: [5, 2], sizes: [0.5, 0.5] }, 7], sizes: [0.5, 0.5] },
      { focused: 5 });
    const fit = fitAt(ws, 1920, 1052);
    const out = placeAgentPanes(ws, fit, [], asks(3));
    expect(orderOf(out.ws).slice(0, 3)).toEqual([5, 2, 7]);
    expect(out.ws.focused).toBe(5);
    expect(rowsOf(out.ws.tree)).toEqual([[5, 2, 7], [1, 3, 4]]);
  });

  it("a window already dropping panes, or a stale fit, takes free panes only", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const dropping = fitWorkspace(DEFAULT_LAYOUT, three, 900, 700);
    expect(dropping.dropped).toBeGreaterThan(0);
    const out = placeAgentPanes(three, dropping, [], asks(2));
    expect(out.ws).toBe(three);
    const stale = fitAt(defaultWorkspace(), 1920, 1052);
    expect(placeAgentPanes(three, stale, [], asks(1)).ws).toBe(three);
  });

  it("twelve drawn: nothing more", () => {
    const twelve = v2(storeTree(gridTree(ids(12), [4, 4, 4])!));
    const out = placeAgentPanes(twelve, fitAt(twelve, 1920, 1052), [], asks(2));
    expect(out.ws).toBe(twelve);
    expect(out.placed).toEqual([null, null]);
  });

  it("at most eleven asks are placed", () => {
    expect(AGENT_OPEN_MAX).toBe(11);
    const out = placeAgentPanes(defaultWorkspace(), fitAt(defaultWorkspace(), 1920, 1052), [], asks(13));
    expect(out.placed.filter((n) => n !== null)).toHaveLength(11);
    expect(out.placed.slice(11)).toEqual([null, null]);
  });

  it("every result fits the window it was planned for (property)", () => {
    let seed = 0x5eed;
    const rand = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    for (let run = 0; run < 300; run++) {
      const start = Math.floor(rand() * 8);
      const presets = ["single", "cols2", "cols3", "grid4", "grid5", "grid6", "grid7", "grid8"] as const;
      const ws = setPreset(defaultWorkspace(), presets[start]);
      const w = 700 + Math.floor(rand() * 2400), h = 400 + Math.floor(rand() * 1100);
      const fit = fitAt(ws, w, h);
      const k = 1 + Math.floor(rand() * 11);
      const out = placeAgentPanes(ws, fit, [], asks(k));
      const after = fitAt(out.ws, w, h);
      if (out.ws === ws) continue;
      expect(after.dropped).toBe(0);
      expect(after.sides).toEqual(fit.sides);
      expect(out.ws.focused).toBe(ws.focused);
      expect(orderOf(out.ws).slice(0, orderOf(ws).length)).toEqual(orderOf(ws));
      for (const n of after.panes) {
        expect(after.boxes[n]!.px.width).toBeGreaterThanOrEqual(PANE_MIN_W - 1e-6);
        expect(after.boxes[n]!.px.height).toBeGreaterThanOrEqual(PANE_MIN_H - 1e-6);
      }
    }
  });
});

describe("agentRequestId", () => {
  it("is 32 lowercase hex characters or null", () => {
    expect(agentRequestId("a".repeat(32))).toBe("a".repeat(32));
    for (const bad of ["A".repeat(32), "a".repeat(31), "a".repeat(33), 5, null, undefined, "zz".repeat(16)]) {
      expect(agentRequestId(bad)).toBeNull();
    }
  });
});
