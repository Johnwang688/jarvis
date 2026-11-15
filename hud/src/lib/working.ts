// The owner's working set as the sidebar draws it (WS-2, docs/plans/
// 2026-11-14-inbox-working-set-plan.md D-5, D-9, D-10). The daemon decides
// who is in the set and what the rows are (`GET /working`, jarvis/v2/
// working.py); this file only reads the answer and orders it for the eye.
//
// Everything a row carries is text. A title or a row's words are never HTML.

import { oneLine, parseRow, type InboxRow } from "./inbox";
import type { ActivityStatus, ActivityView } from "./activity";

export interface WorkingEntry {
  thread_id: string;
  project_id: string | null;
  since: string;
  /** The last activity, ISO: the order within a label. */
  last: string;
  /** What Done must carry back (a stale one is the daemon's 409). */
  seq: number;
  /** The daemon's sidebar status as of the read; the sidebar draws the live activity store instead. */
  status: string;
  /** How many inbox rows sit on this thread. */
  rows: number;
  /** Why Done would be refused as of the read; null when it would not. A hint: Done checks again. */
  why_not: string | null;
}

export interface WorkingView {
  entries: WorkingEntry[];
  needs_you: InboxRow[];
  count: number;
  pending: boolean;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const idOf = (v: unknown): string | null => (typeof v === "string" && v && v.length <= 128 ? v : null);

export function parseEntry(raw: unknown): WorkingEntry | null {
  if (!isObj(raw)) return null;
  const thread_id = idOf(raw.thread_id);
  const seq = raw.seq;
  if (!thread_id || typeof seq !== "number" || !Number.isInteger(seq)) return null;
  return {
    thread_id,
    project_id: idOf(raw.project_id),
    since: typeof raw.since === "string" ? raw.since : "",
    last: typeof raw.last === "string" ? raw.last : "",
    seq,
    status: typeof raw.status === "string" ? raw.status : "idle",
    rows: Array.isArray(raw.rows) ? raw.rows.length : 0,
    why_not: typeof raw.why_not === "string" && raw.why_not ? oneLine(raw.why_not, 200) : null,
  };
}

/** The daemon's answer, or null when it is not one (the store calls that an error, never an empty set). */
export function parseWorking(raw: unknown): WorkingView | null {
  if (!isObj(raw) || !Array.isArray(raw.entries) || !Array.isArray(raw.needs_you)) return null;
  const entries = raw.entries.map(parseEntry).filter((e): e is WorkingEntry => !!e);
  const needs_you = raw.needs_you.map(parseRow).filter((r): r is InboxRow => !!r);
  return {
    entries, needs_you,
    count: typeof raw.count === "number" && Number.isFinite(raw.count) ? raw.count : 0,
    pending: raw.pending === true,
  };
}

/** D-9: the labels in the order they sit in the Inbox. */
export const LABEL_ORDER: readonly ActivityStatus[] = ["needs_input", "failed", "unread", "working", "idle"];

/** The words a label reads as (the owner's own: Needs you, Failed, Done, Working, your move). */
export const LABEL_TEXT: Record<ActivityStatus, string> = {
  needs_input: "Needs you",
  failed: "Failed",
  unread: "Done, unread",
  working: "Working",
  idle: "Your move",
};

/** D-9: by label (the sidebar's own activity store, so a row matches its project-tree dot),
 * then newest `last` first; ties keep the daemon's order. Does not change its argument. */
export function orderEntries(entries: readonly WorkingEntry[], activity: ActivityView): WorkingEntry[] {
  const rank = (e: WorkingEntry) => LABEL_ORDER.indexOf(activity.threads[e.thread_id] || "idle");
  const at = (e: WorkingEntry) => Date.parse(e.last) || 0;
  return entries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => rank(a.e) - rank(b.e) || at(b.e) - at(a.e) || a.i - b.i)
    .map((x) => x.e);
}

/** What kind of thing a Needs-you row is, in words chosen here, never the served ones. */
const KIND_LABEL: Record<string, string> = {
  approval: "Approval", question: "Question", task_question: "Question", proposal: "Proposal",
  task: "Task", missed: "Missed", decision: "Decision", check: "Check", pr: "PR", pr_unavailable: "PRs",
  deferred: "Deferred",
};

export function kindLabel(row: Pick<InboxRow, "kind">): string {
  return KIND_LABEL[row.kind] || "Item";
}

/** Amber means pending, the same rule as the Inbox count (ranks 1 and 2). */
export const rowPending = (row: Pick<InboxRow, "rank">): boolean => row.rank <= 2;

/** The Inbox section's collapsed/expanded choice survives a reload (localStorage, never required). */
const KEY = "jarvis.hud.workingSection";

export function loadSectionOpen(): boolean {
  try {
    return localStorage.getItem(KEY) !== "0";
  } catch {
    return true;
  }
}

export function saveSectionOpen(open: boolean): void {
  try {
    localStorage.setItem(KEY, open ? "1" : "0");
  } catch {
    /* no storage here: the choice just does not last */
  }
}

/** How long the Undo toast stays (the daemon's own deadline is 10 s). */
export const UNDO_TOAST_MS = 5_000;
