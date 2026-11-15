// The phone's list reads, decoded and judged in one place (PR 381). Pure.
//
// Every resource the app keeps a stale warning for (the Inbox, In progress,
// Tasks, the morning review and an open chat's log, its "load more" pages
// included) is decoded here into the value the app applies plus a tally of
// each record list: how many records the daemon sent and how many of them
// parsed. The record parsers are the existing ones (lib/inbox `parseRow`,
// lib/review `parseItem`, ./model's `parseTask`, `parseWorkingItem`,
// `parseLogRow`), so a record is dropped here exactly when the list parsers
// drop it.
//
// `readVerdict` is the one rule (PhoneApp.applyRead is the one place it is
// acted on):
//
// - **malformed**: the container is the wrong shape (`{}`, `null`, a list
//   that is not an array), or any non-empty record list yielded **zero**
//   valid records (`{rows: [null]}`). Nothing was read: the app keeps the
//   shown data, keeps (or raises) the warning and moves no cursor.
// - **partial**: the container is right and every list yielded something,
//   but some records were dropped. The app applies what did parse (it is
//   newer than what is shown: an answered card goes, a new question shows)
//   and keeps a warning that says the list is incomplete. It draws no
//   conclusion from a record's absence (the Inbox does not call the open
//   card "no longer waiting", since its row may be the one that was dropped).
//   A chat log is ordered and has one cursor, and a dropped line has no seq
//   that can be read, so a log applies only the valid lines *before* the
//   first dropped one and its cursor stops there: the next read asks for the
//   dropped line again rather than skipping it. A log page whose first line
//   is invalid has nothing to apply and is malformed.
// - **valid**: everything parsed (an empty list included). Only this clears
//   the warning.
//
// A repeat of a valid record (dropped by the parsers' de-duplication) counts
// as valid: it was read, it is just already shown.

import { parseInbox, parseRow, type InboxRow } from "../lib/inbox";
import { parseItem, parseReview, type ReviewReport } from "../lib/review";
import { parseLog, parseLogRow, parseTask, parseTasks, parseWorkingItem, parseWorkingList,
  type LogPage, type TaskView, type WorkingList } from "./model";

/** One record list of a read: how many records came, and how many of them parsed. */
export interface Tally { sent: number; valid: number }

export type ReadVerdict = "valid" | "partial" | "malformed";

/** A read decoded: the value to apply, and one tally per record list it carried. */
export interface Decoded<T> { value: T; lists: Tally[] }

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

export function tally(list: unknown, valid: (record: unknown) => boolean): Tally {
  const records = Array.isArray(list) ? list : [];
  return { sent: records.length, valid: records.filter(valid).length };
}

/** The one rule: see the header. `null` (no decoded value) is malformed. */
export function readVerdict(lists: readonly Tally[] | null): ReadVerdict {
  if (!lists) return "malformed";
  if (lists.some((l) => l.sent > 0 && l.valid === 0)) return "malformed";
  return lists.some((l) => l.valid < l.sent) ? "partial" : "valid";
}

export function decodeInbox(body: unknown): Decoded<{ rows: InboxRow[]; more: boolean }> | null {
  if (!isObj(body)) return null;
  const rows = parseInbox(body.rows);
  if (!rows) return null;
  return { value: { rows, more: body.more === true }, lists: [tally(body.rows, (r) => parseRow(r) !== null)] };
}

export function decodeWorking(body: unknown): Decoded<WorkingList> | null {
  const list = parseWorkingList(body);
  if (!list || !isObj(body)) return null;
  return { value: list, lists: [tally(body.entries, (e) => parseWorkingItem(e) !== null)] };
}

export function decodeTasks(body: unknown): Decoded<{ tasks: TaskView[]; more: boolean }> | null {
  // parseTasks reads an absent list as empty for other callers; a refresh must carry an actual list.
  if (!isObj(body) || !Array.isArray(body.tasks)) return null;
  return { value: { tasks: parseTasks(body), more: body.more === true },
    lists: [tally(body.tasks, (t) => parseTask(t) !== null)] };
}

export function decodeReview(body: unknown): Decoded<ReviewReport> | null {
  const report = parseReview(body);
  if (!report || !isObj(body)) return null;
  // Every list of records the review shows; `projects` and `places` are lookups, not records.
  return { value: report, lists: [
    tally(body.overnight, (x) => parseItem(x, "overnight") !== null),
    tally(body.since, (x) => parseItem(x, "since") !== null),
    tally(body.before, (x) => parseItem(x, "before") !== null),
    // The kind filter (approvals only) is a choice, not a dropped record: any parsed row counts.
    tally(body.live, (x) => parseRow(isObj(x) ? x.row : null) !== null),
  ] };
}

/** A log page cut to the lines before the first invalid one; `next` never passes a line not applied. */
export function decodeLog(body: unknown): Decoded<LogPage> | null {
  const page = parseLog(body);
  if (!page || !isObj(body) || !Array.isArray(body.rows)) return null;
  const sent = body.rows.length;
  const firstBad = body.rows.findIndex((r) => parseLogRow(r) === null);
  if (firstBad < 0) return { value: page, lists: [{ sent, valid: sent }] };
  // Every line before firstBad parsed, so the page's first firstBad rows are exactly those lines.
  const rows = page.rows.slice(0, firstBad);
  const last = rows[rows.length - 1];
  const next = last ? Math.min(page.next, last.seq + 1) : page.next;
  return { value: { ...page, rows, next, more: false }, lists: [{ sent, valid: firstBad }] };
}
