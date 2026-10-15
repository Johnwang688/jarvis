// The sidebar's chat list per project (owner request, 2026-10-15): the five
// chats touched most recently, newest first, and "Show N more" for the rest.
//
// "Recently modified" is `Thread.updated`, which the daemon stamps on every
// turn (jarvis/v2/daemon.py) — the last activity, not the creation time. A
// row with no readable stamp sorts after every row with one, and ties keep the
// order the daemon sent, so the list never shuffles for no reason.
//
// A chat the owner is looking at or that wants them is never behind the
// button, however old: the selected chat and any chat drawn in a pane, a
// running turn, an approval or question open (needs_input), an unread finish
// and a failure, and the row being renamed. They are kept in their sorted
// place, so "Show N more" counts only what is really out of sight.

import type { ActivityStatus } from "./activity";
import type { Thread } from "../types";

/** How many chats a project shows before "Show more". */
export const RECENT_CHATS = 5;

/** The localStorage key holding the projects whose full list the owner asked for. */
export const SHOW_MORE_KEY = "jarvis.hud.sidebar.showmore";

export interface RecentChats {
  /** Every chat of the project, newest first. */
  sorted: Thread[];
  /** What to draw: the first five, or all when expanded, plus the kept rows. */
  shown: Thread[];
  /** How many are behind the button (0 when expanded). */
  hidden: number;
  /** Whether the project has more chats than the limit, so there is a button at all. */
  more: boolean;
}

function stamp(t: Thread): number {
  const n = Date.parse(t.updated || "");
  return Number.isNaN(n) ? -Infinity : n;
}

/** Newest first; an unreadable stamp last; ties in the order given. */
export function byRecent(chats: readonly Thread[]): Thread[] {
  return chats
    .map((t, i) => ({ t, i, at: stamp(t) }))
    .sort((a, b) => (a.at === b.at ? a.i - b.i : a.at > b.at ? -1 : 1))
    .map((x) => x.t);
}

/** Whether a chat must stay on screen whatever its age. */
export function mustShow(t: Thread, status: ActivityStatus | undefined, kept: ReadonlySet<string>): boolean {
  return kept.has(t.id) || !!t.running || (!!status && status !== "idle");
}

export function recentChats(
  chats: readonly Thread[],
  opts: {
    expanded: boolean;
    /** Ids that stay visible: the selected chat, chats open in panes, the one being renamed. */
    kept?: ReadonlySet<string>;
    status?: (id: string) => ActivityStatus | undefined;
    limit?: number;
  },
): RecentChats {
  const limit = opts.limit ?? RECENT_CHATS;
  const kept = opts.kept ?? new Set<string>();
  const sorted = byRecent(chats);
  const more = sorted.length > limit;
  if (opts.expanded || !more) return { sorted, shown: sorted, hidden: 0, more };
  const shown = sorted.filter((t, i) => i < limit || mustShow(t, opts.status?.(t.id), kept));
  return { sorted, shown, hidden: sorted.length - shown.length, more };
}

/** The button's words. Collapsed it counts what is hidden; expanded it folds back. */
export function moreLabel(hidden: number, expanded: boolean): string {
  return expanded ? "Show less" : `Show ${hidden} more`;
}

/** The remembered projects, read defensively: a convenience, never a failure. */
export function loadShowMore(storage?: Storage | null): Record<string, boolean> {
  try {
    const raw = (storage ?? window.localStorage).getItem(SHOW_MORE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    const out: Record<string, boolean> = {};
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      for (const [k, v] of Object.entries(parsed)) if (v === true) out[k] = true;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveShowMore(map: Record<string, boolean>, storage?: Storage | null): void {
  try {
    const on = Object.keys(map).filter((k) => map[k]);
    (storage ?? window.localStorage).setItem(SHOW_MORE_KEY, JSON.stringify(Object.fromEntries(on.map((k) => [k, true]))));
  } catch {
    /* the list still works, it just forgets */
  }
}
