// The thinking line at the foot of each chat (WP-H; plan
// docs/plans/2026-10-10-hud-split-panes-plan.md §4, decisions S-4): a mini orb,
// a word ("Thinking…", "Running ls…", "Waiting for approval", "Waiting for
// your answer") and the elapsed time, under the last message while that
// chat's turn runs, Claude Code style. Amber for a tool or an approval, red
// on an error, as the big orb is. Pure, like lib/chats.ts.
//
// **The turn state is keyed by thread, window-wide** (H-1, `State.turns`), and
// fed by the turn events *and* the daemon's activity records, so the line
// follows its thread wherever it is shown: across a trade of conversations, a
// pane closed and reopened, a turn started from Discord before any pane
// showed the thread, and a reload (the activity snapshot says the thread is
// working; the thread record's `running_since` says since when).

import type { ChatState } from "./chats";
import type { ActivityStatus, ActivityView } from "./activity";
import type { OrbState } from "../state/store";
import type { Thread, ToolOp } from "../types";

export type TurnError = "" | "during" | "ended";

/** One thread's turn, as far as this window knows it. */
export interface TurnEntry {
  /** When the turn began (epoch ms), for the elapsed time; null when unknown. */
  since: number | null;
  /** "during": an error was said in this turn, which still runs. "ended": the
   * turn is over and ended on an error — a static red line until the next
   * turn or send. */
  error: TurnError;
  /** When this window began tracking it, on this window's clock: which tool
   * calls are this turn's (a call a previous turn left unfinished is not). */
  seen: number;
  /** A send made it and no `turn_started` has confirmed it yet: that send's
   * local id. Its failure takes the entry back; a stale idle record does not. */
  by?: string;
  /** Noticed late (an activity record, not a turn event): `since` is
   * corrected from the thread record's `running_since` when that arrives. */
  late?: boolean;
}

export type Turns = Record<string, TurnEntry>;

export type TurnEvent =
  /** The owner sent in that thread. A steer joins the turn that runs. */
  | { event: "send"; threadId: string; at: number; by: string; steer?: boolean }
  /** That send failed, or was dropped: the entry it made is taken back. */
  | { event: "send_failed"; threadId: string; by: string }
  /** That send was taken (steered or queued): no turn of its own will confirm it. */
  | { event: "sent"; threadId: string; by: string }
  | { event: "started"; threadId: string; at: number }
  | { event: "error"; threadId: string; at: number }
  | { event: "finished"; threadId: string; at: number; stop?: string; next?: number };

const RUNNING: ReadonlySet<ActivityStatus> = new Set(["working", "needs_input"]);

function put(turns: Turns, id: string, entry: TurnEntry | null): Turns {
  const next = { ...turns };
  if (entry) next[id] = entry;
  else delete next[id];
  return next;
}

/** One turn event folded in (the reducer's `turn` action). */
export function applyTurn(turns: Turns, e: TurnEvent): Turns {
  const id = e.threadId;
  if (!id) return turns;
  const had = turns[id];
  switch (e.event) {
    case "send":
      // A message that steers (or queues behind) the running turn changes
      // nothing about it: its time and any error stand. So does one sent
      // into a turn this window knows runs though the pane does not track it
      // yet (a Discord turn, not adopted): the daemon hands it to that turn.
      if (had && had.error !== "ended" && (e.steer || !had.by)) return turns;
      return put(turns, id, { since: e.at, error: "", seen: e.at, by: e.by });
    case "send_failed":
      return had?.by === e.by ? put(turns, id, null) : turns;
    case "sent":
      if (had?.by !== e.by) return turns;
      return put(turns, id, { since: had.since, error: had.error, seen: had.seen });
    case "started":
      // The send that asked for this turn already made its entry: kept (its
      // time is when the owner pressed Enter). Anything else — an ended
      // error, an entry a missed finish left behind — gives way to the new turn.
      if (had?.by && had.error === "") return put(turns, id, { since: had.since, error: "", seen: had.seen });
      return put(turns, id, { since: e.at, error: "", seen: e.at });
    case "error":
      // An error said before any turn event this window heard: no start time.
      if (!had || had.error === "ended") return put(turns, id, { since: null, error: "during", seen: e.at, late: true });
      return put(turns, id, { since: had.since, error: "during", seen: had.seen, ...(had.late ? { late: true } : {}) });
    case "finished":
      // A message waits behind it and runs at once as the next turn.
      if (e.next) return put(turns, id, { since: e.at, error: "", seen: e.at });
      if (e.stop === "error" || had?.error === "during") {
        return put(turns, id, { since: had?.since ?? null, error: "ended", seen: had?.seen ?? e.at });
      }
      return had ? put(turns, id, null) : turns;
  }
}

/** A thread record's `running_since` (H-b), as epoch ms, or null. */
export function runningSince(threads: readonly Thread[], id: string): number | null {
  const t = threads.find((x) => x.id === id);
  if (!t || t.running === false || typeof t.running_since !== "string") return null;
  const at = Date.parse(t.running_since);
  return Number.isFinite(at) ? at : null;
}

/**
 * The activity view moved from `before` to `after` (an SSE record, or the
 * `GET /activity` snapshot): only a **change** of a thread's status counts,
 * because the daemon publishes on change, and a view that merely still says
 * "working" may be older than a finish already heard.
 *   - into working / needs_input, with no entry (or an ended one): a turn
 *     noticed late — started from Discord, or before a reload — tracked from
 *     the record's `running_since`, else from now;
 *   - into failed: the turn ended on an error (its finish was missed);
 *   - into idle / unread: a missed finish — the entry goes. Never one a send
 *     in flight made (a stale record from opening the thread), nor an ended one.
 */
export function turnsAfterActivity(
  turns: Turns, before: ActivityView, after: ActivityView, threads: readonly Thread[], now: number,
): Turns {
  if (before.threads === after.threads) return turns;
  let next = turns;
  const ids = new Set([...Object.keys(before.threads), ...Object.keys(after.threads)]);
  for (const id of ids) {
    const was = before.threads[id] ?? "idle";
    const is = after.threads[id] ?? "idle";
    if (was === is) continue;
    const had = next[id];
    if (RUNNING.has(is)) {
      if (RUNNING.has(was) || (had && had.error !== "ended")) continue;
      const since = runningSince(threads, id);
      next = put(next, id, { since: since ?? now, error: "", seen: now, ...(since === null ? { late: true } : {}) });
    } else if (had && had.error !== "ended" && !had.by) {
      next = put(next, id, is === "failed" ? { since: had.since, error: "ended", seen: had.seen } : null);
    }
  }
  return next;
}

/** The thread records arrived: an entry noticed late takes its true start. */
export function turnsAfterThreads(turns: Turns, threads: readonly Thread[]): Turns {
  let next = turns;
  for (const [id, entry] of Object.entries(turns)) {
    if (!entry.late) continue;
    const since = runningSince(threads, id);
    if (since === null) continue;
    next = put(next, id, { since, error: entry.error, seen: entry.seen });
  }
  return next;
}

// ---- the line --------------------------------------------------------------

export interface ThinkingLine {
  /** Which row of the precedence table: error, ended, approval, answer, tool, thinking. */
  word: "error" | "ended" | "approval" | "answer" | "tool" | "thinking";
  /** What it says. */
  text: string;
  /** The palette key (lib/orbpalette): thinking takes the avatar's accent; tool,
   * approval and error never do. */
  orb: OrbState;
  /** The turn's start, for the elapsed time; null shows no time. */
  since: number | null;
  /** False for the static "Ended on an error": nothing spins. */
  moving: boolean;
}

/** A tool name drawn on one line: no control or format characters, capped. */
export const TOOL_NAME_CAP = 40;
export function toolName(raw: unknown): string {
  const flat = String(raw ?? "").replace(/[\s\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Co}\p{Cn}]+/gu, " ").trim();
  const chars = Array.from(flat || "tool");
  return chars.length <= TOOL_NAME_CAP ? chars.join("") : chars.slice(0, TOOL_NAME_CAP - 1).join("").trimEnd() + "…";
}

/** The elapsed time: `12s`, `3m 07s`, `1h 02m`. */
export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** The newest tool call still running in this turn, if any. */
function runningOp(ops: readonly ToolOp[], turn: TurnEntry | undefined): ToolOp | null {
  for (let i = ops.length - 1; i >= 0; i--) {
    const o = ops[i];
    if (o.finished) continue;
    // A call a previous turn left unfinished (interrupted, its finish never
    // sent) is not this turn's.
    if (turn && o.started < turn.seen) continue;
    return o;
  }
  return null;
}

/**
 * What a chat pane's foot says, or null for nothing. First match wins:
 *   an error in the running turn · the turn ended on an error (static) ·
 *   an approval for this thread · a question (needs_input with no approval) ·
 *   a tool call running in this pane · the turn running (the pane tracks it,
 *   a first send is on its way, or the thread has a live entry — which the
 *   daemon's "working" makes).
 */
export function thinkingLine(a: {
  chat: ChatState;
  /** The thread the pane shows (or is opening for a first send); null while composing. */
  threadId: string | null;
  turn?: TurnEntry;
  activity?: ActivityStatus;
  approvalHere: boolean;
}): ThinkingLine | null {
  const { chat, threadId } = a;
  const turn = threadId ? a.turn : undefined;
  const activity = threadId ? a.activity : undefined;
  const since = turn?.since ?? null;
  if (turn?.error === "during") return { word: "error", text: "Error", orb: "error", since, moving: true };
  if (turn?.error === "ended") {
    return { word: "ended", text: "Ended on an error", orb: "error", since: null, moving: false };
  }
  if (threadId && a.approvalHere) {
    return { word: "approval", text: "Waiting for approval", orb: "approval", since, moving: true };
  }
  if (activity === "needs_input") {
    return { word: "answer", text: "Waiting for your answer", orb: "approval", since, moving: true };
  }
  // The turn runs: this pane tracks it (or its first send is on the way), or
  // the window holds a live entry for its thread. The daemon's "working"
  // reaches the line through the entry it makes (`turnsAfterActivity`), never
  // directly: the record that turns a thread idle follows its `turn_finished`,
  // so a view still saying "working" for those few milliseconds must not
  // outlive the finish that already deleted the entry.
  const tracks = chat.busy && !!chat.turnThreadId
    && ((!!threadId && chat.turnThreadId === threadId) || chat.turnThreadId === chat.compose?.sending);
  if (!tracks && !turn) return null;
  const op = runningOp(chat.ops, turn);
  if (op) return { word: "tool", text: `Running ${toolName(op.name)}…`, orb: "tool", since, moving: true };
  return { word: "thinking", text: "Thinking…", orb: "thinking", since, moving: true };
}
