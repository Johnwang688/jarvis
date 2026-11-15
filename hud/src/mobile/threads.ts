// The phone's thread list, pure (PX-2, plan C §4.2 and §4.3): the join of the
// three reads the list is made of, and the band every card sits in.
//
//   /m/api/working  → entries: the owner's threads, each with a label
//   /m/api/inbox    → rows: what waits on the owner (a row may sit on a thread,
//                     on a task, on both, or on neither)
//   /m/api/tasks    → tasks: running ones have no inbox row and no entry
//                     (working.py leaves task threads out), so they are seeded
//                     here (Sol, finding 4), and rows attach to the same slot
//
// **IN-8 for the rows the phone received: every row lands in exactly one
// slot.** `buildSlots` is total: it checks four rules in order and the last
// one takes whatever is left, so no row can fall through, whatever the
// working read did (null, failed, empty) and whatever ids overlap. `bandOf` is
// total too: every slot shape has a band, with an explicit Recent fallback.
// Ranks 1 to 4 (all of Needs you's reasons but a label) can never be placed
// anywhere but Needs you; nothing here folds or hides a row. The one fold the
// list has (rank 8 row slots, "for your information") is `bands().info`.
//
// Nothing here builds HTML. Every string is untrusted text and stays so.

import { oneLine, type InboxRow } from "../lib/inbox";
import { LABEL_ORDER } from "../lib/working";
import { RUNNING_STATES, type TaskView, type WorkingItem } from "./model";

export type Band = "needs" | "running" | "recent";

/** What kind of card a slot is: a working-set thread, a task, a thread that is not in the set, or a row alone. */
export type SlotKind = "thread" | "task" | "orphan" | "row";

export interface Slot {
  /** Unique in a list, and the key of the view's reuse: `thread:`, `task:`, `orphan:` or `row:` then its id. */
  key: string;
  kind: SlotKind;
  threadId: string | null;
  taskId: string | null;
  entry: WorkingItem | null;
  task: TaskView | null;
  /** In rank order (ties keep the order received). */
  rows: InboxRow[];
  /** The orphan's or task's own name when no entry or task read gives one. */
  title: string;
}

/** The lowest rank that is not "Needs you" (ranks 1 to 5 are: lib/inbox `COUNTED_MAX_RANK`). */
export const NEEDS_MAX_RANK = 5;

/** A row that waits on the owner. A rank that is not a number counts as waiting: never lose a row to a typo. */
export const waiting = (row: Pick<InboxRow, "rank">): boolean => !(row.rank > NEEDS_MAX_RANK);

export function buildSlots(entries: readonly WorkingItem[] | null, rows: readonly InboxRow[],
                           tasks: readonly TaskView[] | null): Slot[] {
  const slots = new Map<string, Slot>();
  const make = (slot: Omit<Slot, "rows" | "title"> & { title?: string }): Slot => {
    const existing = slots.get(slot.key);
    if (existing) return existing;
    const made: Slot = { rows: [], title: "", ...slot };
    slots.set(slot.key, made);
    return made;
  };
  const taskById = new Map<string, TaskView>((tasks ?? []).map((t) => [t.id, t]));
  const entryIds = new Set<string>();
  // The seeds: every entry (with or without rows) and every running or verifying task (Sol 4).
  for (const e of entries ?? []) {
    entryIds.add(e.thread_id);
    make({ key: "thread:" + e.thread_id, kind: "thread", threadId: e.thread_id, taskId: null, entry: e, task: null });
  }
  for (const t of tasks ?? []) {
    if (RUNNING_STATES.has(t.state)) {
      make({ key: "task:" + t.id, kind: "task", threadId: null, taskId: t.id, entry: null, task: t });
    }
  }
  const ordered = rows.map((row, i) => ({ row, i })).sort((a, b) => a.row.rank - b.row.rank || a.i - b.i);
  for (const { row } of ordered) {
    let slot: Slot;
    if (row.thread_id && entryIds.has(row.thread_id)) {
      slot = slots.get("thread:" + row.thread_id)!;                                 // 1. its thread is an entry
    } else if (row.task_id) {
      slot = make({ key: "task:" + row.task_id, kind: "task", threadId: null, taskId: row.task_id, entry: null,
        task: taskById.get(row.task_id) ?? null });                                 // 2. a task, merged with its read
    } else if (row.thread_id) {
      slot = make({ key: "orphan:" + row.thread_id, kind: "orphan", threadId: row.thread_id, taskId: null,
        entry: null, task: null });                                                 // 3. a thread the list does not hold
    } else {
      slot = make({ key: "row:" + row.id, kind: "row", threadId: null, taskId: null, entry: null, task: null }); // 4.
    }
    slot.rows.push(row);
  }
  for (const slot of slots.values()) {
    if (slot.kind === "orphan") slot.title = slot.rows.map((r) => r.origin).find(Boolean) ?? "";
    if (slot.kind === "task") slot.title = slot.task ? oneLine(slot.task.brief, 140) : "";
  }
  return [...slots.values()];
}

/** The band of any slot: total. Needs you wins over Running; anything unknown is Recent. */
export function bandOf(slot: Slot): Band {
  if (slot.rows.some(waiting) || slot.entry?.status === "needs_input") return "needs";
  if (slot.entry?.status === "working") return "running";
  if (slot.task && RUNNING_STATES.has(slot.task.state)) return "running";
  return "recent";
}

const stamp = (iso: string | null | undefined): number => {
  const n = Date.parse(iso ?? "");
  return Number.isNaN(n) ? 0 : n;
};
const newestRow = (slot: Slot): number => slot.rows.reduce((m, r) => Math.max(m, stamp(r.asked_at)), 0);
/** The slot's recency: its entry's last activity, else its task's update, else its newest row. */
const lastOf = (slot: Slot): number =>
  slot.entry ? stamp(slot.entry.last) : slot.task ? Math.max(stamp(slot.task.updated), newestRow(slot)) : newestRow(slot);
const minRank = (slot: Slot): number => slot.rows.length ? Math.min(...slot.rows.map((r) => (r.rank >= 1 ? r.rank : 1))) : NEEDS_MAX_RANK;
const soonest = (slot: Slot): number =>
  slot.rows.reduce((m, r) => { const t = Date.parse(r.expires_at ?? ""); return Number.isNaN(t) ? m : Math.min(m, t); }, Infinity);
const labelIndex = (slot: Slot): number => slot.entry ? LABEL_ORDER.indexOf(slot.entry.status) : LABEL_ORDER.length;

/** Rank 8 row slots only: a thread, task or orphan card is never folded. */
export const informational = (slot: Slot): boolean => slot.kind === "row" && slot.rows.length > 0 && slot.rows.every((r) => r.rank === 8);

export interface Bands { needs: Slot[]; running: Slot[]; recent: Slot[]; info: Slot[] }

export function bands(slots: readonly Slot[]): Bands {
  const out: Bands = { needs: [], running: [], recent: [], info: [] };
  slots.forEach((s) => (bandOf(s) === "recent" && informational(s) ? out.info : out[bandOf(s)]).push(s));
  const sort = (list: Slot[], cmp: (a: Slot, b: Slot) => number) => {
    const at = new Map(slots.map((s, i) => [s, i]));
    list.sort((a, b) => cmp(a, b) || at.get(a)! - at.get(b)!);
  };
  // Needs you: lowest rank, soonest expiry, label order, newest.
  sort(out.needs, (a, b) => minRank(a) - minRank(b) || soonest(a) - soonest(b) || labelIndex(a) - labelIndex(b) || lastOf(b) - lastOf(a));
  sort(out.running, (a, b) => lastOf(b) - lastOf(a));
  // Recent: failed, unread, idle, then the slots with no label, each newest first.
  const recentGroup = (s: Slot) => (s.entry ? ["failed", "unread", "idle"].indexOf(s.entry.status) : 3);
  sort(out.recent, (a, b) => recentGroup(a) - recentGroup(b) || lastOf(b) - lastOf(a));
  sort(out.info, (a, b) => lastOf(b) - lastOf(a));
  return out;
}
