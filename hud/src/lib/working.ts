// The owner's working set as the sidebar draws it (WS-2, docs/plans/
// 2026-11-14-inbox-working-set-plan.md D-5, D-9, D-10). The daemon decides
// who is in the set and what the rows are (`GET /working`, jarvis/v2/
// working.py); this file only reads the answer and orders it for the eye.
//
// Everything a row carries is text. A title or a row's words are never HTML.

import { canMark, isHidden, oneLine, parseRow, type InboxRow } from "./inbox";
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
  /** The inbox rows that sit on this thread (shown ones only): a hint on its row. */
  attached: { kind: string; rank: number }[];
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
    attached: Array.isArray(raw.rows)
      ? raw.rows.map(parseRow).filter((r): r is InboxRow => !!r && !isHidden(r)).map((r) => ({ kind: r.kind, rank: r.rank }))
      : [],
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

/** The small hint on a thread row that has inbox rows attached: "1 decision", "2 items". */
export function attachedHint(attached: readonly { kind: string; rank: number }[]): string {
  if (!attached.length) return "";
  const kinds = new Set(attached.map((a) => kindLabel(a)));
  const n = attached.length;
  if (kinds.size === 1) return `${n} ${[...kinds][0].toLowerCase()}${n > 1 ? "s" : ""}`;
  return `${n} items`;
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

/** The Needs you group's own fold (owner, 2026-11-15): open unless the owner folded it. Lasts like the section's. */
const NEEDS_KEY = "jarvis.hud.workingNeeds";

export function loadNeedsOpen(): boolean {
  try {
    return localStorage.getItem(NEEDS_KEY) !== "0";
  } catch {
    return true;
  }
}

export function saveNeedsOpen(open: boolean): void {
  try {
    localStorage.setItem(NEEDS_KEY, open ? "1" : "0");
  } catch {
    /* no storage here: the choice just does not last */
  }
}

/** Why a Needs you row's ✓ cannot mark it done (dismiss it), or null when it can. The daemon judges
 * again (IN-8): a live approval, a question, a proposal or a blocked task (ranks 1 to 4), a deferral
 * and "PR list unavailable" are never hidden. */
export function needDoneBlock(row: InboxRow): string | null {
  if (canMark(row)) return null;
  if (row.rank <= 4) return "it still waits on you: answer it on the Needs you page";
  return "this one cannot be hidden";
}

/** How long the "Marked Done" toast stays (owner, 2026-11-15). The daemon's own Undo deadline is
 * 20 s (`working.UNDO_S`), so an Undo pressed at the toast's last moment still lands. */
export const UNDO_TOAST_MS = 15_000;

/** WS-4: the idle limit, as `GET /working/settings` answers it. The bounds are the daemon's. */
export interface WorkingSettings { idle_days: number; min: number; max: number; default: number }

const wholeIn = (v: unknown, lo: number, hi: number): v is number =>
  typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi;

/** The daemon's answer, or null when it is not one: a made-up value is never shown as the setting. */
export function parseWorkingSettings(raw: unknown): WorkingSettings | null {
  if (!isObj(raw)) return null;
  const { idle_days, min, max } = raw;
  if (typeof min !== "number" || typeof max !== "number" || !Number.isInteger(min) || !Number.isInteger(max)
    || min < 1 || max < min || !wholeIn(idle_days, min, max)) return null;
  return { idle_days, min, max, default: wholeIn(raw.default, min, max) ? raw.default : idle_days };
}

/** A typed idle limit: the whole number, or the sentence that says what is wrong with it. */
export function checkIdleDays(text: string, min: number, max: number): { value: number } | { error: string } {
  const t = text.trim();
  const n = /^\d{1,4}$/.test(t) ? Number(t) : NaN;
  return Number.isInteger(n) && n >= min && n <= max ? { value: n } : { error: `Enter a whole number from ${min} to ${max}.` };
}
