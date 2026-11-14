// Where the panes a chat asks for go (2026-11-13, docs/plans/2026-11-13-
// agent-pane-grid-decisions.md D1/D3): `hud_open_subagents` asks for up to
// eleven at once, and placing them one per layout pass by halving the chat's
// own pane stopped at three (PANE_MIN_W, MAX_DOWN) and lopsided. So one ask is
// placed as a batch:
//
//  1. existing drawn panes the window found free (`free`, in reading order:
//     the window's rules — never the chat's own pane, the focused or keyboard
//     pane, a pane an agent drives, a pane with unsaved work, a view that keeps
//     its own work, a sub-agent pane) each take one, keeping their spec;
//  2. the rest: the window is laid out once as a balanced grid of every drawn
//     pane, in its reading order, then the new ones (`gridTree`), at most
//     twelve, at most three rows of at most four, each cell at least
//     PANE_MIN_W x PANE_MIN_H at the px the window draws the centre now — never
//     by borrowing from a side pane or the panel, and never on a window that
//     already drops panes. Fewest rows first, so a wide window gets columns.
//     When even the largest grid that fits holds fewer, what fits is placed.
//
// Pure. Only free ids are used (never one ⊞ hid: hidden panes and their specs
// are untouched, FS-5), focus never moves, and a grid that cannot hold one
// more pane changes nothing.

import { balancedRows, gridTree, minH as treeMinH, minW as treeMinW, MAX_ACROSS, MAX_DOWN, MAX_LEAVES, shapeKey } from "./splittree";
import { orderOf, PANE_NOS, type PaneNo, type PaneSpec, type Workspace, type WorkspaceFit } from "./workspace";
import type { SubagentRef } from "./subagents";

/** The most panes one ask may open: the HUD draws twelve and one shows the chat. */
export const AGENT_OPEN_MAX = 11;
/** The most asks a window holds waiting (behind a card, Settings or a busy pointer). */
export const AGENT_BATCHES_MAX = 8;

/** One thing a chat asked to show in a pane. */
export type AgentItem = { kind: "subagent"; ref: SubagentRef };

/** One bus record's ask: placed together, reported together. */
export interface AgentBatch {
  /** The daemon's request id, the window's report is keyed by; null from an older daemon (nothing is reported). */
  requestId: string | null;
  threadId: string;
  items: AgentItem[];
  at: number;
}

/** A request id as the daemon makes them (32 hex characters), else null. */
export function agentRequestId(raw: unknown): string | null {
  return typeof raw === "string" && /^[0-9a-f]{32}$/.test(raw) ? raw : null;
}

/** One pane asked for: how an existing pane shows it, and the spec a new pane takes. */
export interface AgentAsk {
  show: (ws: Workspace, pane: PaneNo) => Workspace;
  spec: PaneSpec;
}

export interface AgentPlacement {
  ws: Workspace;
  /** Per ask, the pane it went to, or null (no room). */
  placed: (PaneNo | null)[];
  /** Panes per row when the window was laid out again, else null. */
  grid: number[] | null;
}

/**
 * Panes per row for `t` panes that fit `width` x `height` (the centre's px;
 * NaN, an unmeasured window, is not checked), fewest rows first; null when no
 * grid of `t` fits.
 */
export function gridFor(t: number, width: number, height: number): number[] | null {
  if (!Number.isInteger(t) || t < 1 || t > MAX_LEAVES) return null;
  for (let r = Math.ceil(t / MAX_ACROSS); r <= MAX_DOWN; r++) {
    const rows = balancedRows(t, r);
    const tree = rows && gridTree(PANE_NOS.slice(0, t), rows);
    if (!rows || !tree) continue;
    const wideEnough = !Number.isFinite(width) || treeMinW(tree) <= width;
    const tallEnough = !Number.isFinite(height) || treeMinH(tree) <= height;
    if (wideEnough && tallEnough) return rows;
  }
  return null;
}

/**
 * Place `asks` (see the module note). `free` is the window's list of drawn
 * panes an ask may take; anything in it that is not drawn is ignored. A fit
 * of another tree (stale) or of a window dropping panes places only into
 * `free`.
 */
export function placeAgentPanes(
  ws: Workspace, fit: WorkspaceFit, free: readonly PaneNo[], asks: readonly AgentAsk[],
): AgentPlacement {
  const placed: (PaneNo | null)[] = asks.map(() => null);
  const asksN = Math.min(asks.length, AGENT_OPEN_MAX);
  const usable = free.filter((n, i) => fit.panes.includes(n) && free.indexOf(n) === i);
  let next = ws;
  let k = 0;
  for (; k < asksN && k < usable.length; k++) {
    next = asks[k].show(next, usable[k]);
    placed[k] = usable[k];
  }
  const rest = asksN - k;
  if (rest <= 0 || fit.dropped > 0 || shapeKey(fit.render) !== shapeKey(ws.tree)) return { ws: next, placed, grid: null };
  const order = orderOf(next);
  const ids = PANE_NOS.filter((n) => !order.includes(n) && !next.kept.includes(n));
  const most = Math.min(MAX_LEAVES, order.length + rest, order.length + ids.length);
  for (let t = most; t > order.length; t--) {
    const rows = gridFor(t, fit.width, fit.height);
    if (!rows) continue;
    const fresh = ids.slice(0, t - order.length);
    const tree = gridTree([...order, ...fresh], rows);
    if (!tree) continue;
    const panes = next.panes.slice();
    fresh.forEach((id, i) => {
      panes[id - 1] = { ...asks[k + i].spec };
      placed[k + i] = id;
    });
    return { ws: { ...next, tree, panes }, placed, grid: rows };
  }
  return { ws: next, placed, grid: null };
}
