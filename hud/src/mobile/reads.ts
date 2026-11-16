// The phone's list reads, decoded and judged in one place (PR 381). Pure.
//
// Every resource the app keeps a stale warning for (the Inbox, In progress,
// Tasks, the morning review and an open chat's log, its "load more" pages
// included, and the details and lists added since: a task, its diff, the
// projects, chats and the new-chat sheet's providers) is decoded here into
// the value the app applies plus a tally of each record list: how many
// records the daemon sent and how many of them parsed. The record parsers
// are the existing ones (lib/inbox `parseRow`, lib/review `parseItem`,
// ./model's `parseTask`, `parseWorkingItem`, `parseLogRow`, ./write's
// `parseModels`), so a record is dropped here exactly when the list parsers
// drop it. How an applied log page joins the transcript is ./chatLog.ts.
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
import { parseDiff, parseLog, parseLogRow, parseProjects, parseTask, parseTasks, parseThreads,
  parseWorkingItem, parseWorkingList, type ChatThread, type DiffView, type LogPage, type Project,
  type TaskView, type WorkingList } from "./model";
import { parseModels, PROVIDERS, type ProviderChoice } from "./write";

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

/** A single parsed record uses the same malformed/valid apply rule as a list. */
export function decodeValue<T>(value: T | null): Decoded<T> | null {
  return value === null ? null : { value, lists: [] };
}

export function decodeTask(body: unknown, id: string): Decoded<TaskView> | null {
  const task = isObj(body) ? parseTask(body.task) : null;
  return decodeValue(task?.id === id ? task : null);
}

export function decodeProjects(body: unknown): Decoded<Project[]> | null {
  if (!isObj(body) || !Array.isArray(body.projects)) return null;
  const value = parseProjects(body);
  return { value, lists: [{ sent: body.projects.length, valid: value.length }] };
}

export function decodeThreads(body: unknown): Decoded<ChatThread[]> | null {
  if (!isObj(body) || !Array.isArray(body.threads)) return null;
  const value = parseThreads(body);
  return { value, lists: [{ sent: body.threads.length, valid: value.length }] };
}

export function decodeDiff(body: unknown): Decoded<DiffView> | null {
  const value = parseDiff(body);
  if (!value || !isObj(body) || !Array.isArray(body.files)) return null;
  return { value, lists: [{ sent: body.files.length, valid: value.files.length }] };
}

export function decodeReview(body: unknown): Decoded<ReviewReport> | null {
  // Optional means absent, not present with an unreadable list (including null).
  if (!isObj(body) || ["since", "live"].some((key) => key in body && !Array.isArray(body[key]))) return null;
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

/**
 * The new-chat sheet's providers (`GET /m/api/thread-models`, remote_write.models). The daemon names every
 * provider it knows (thread_model.describe), so a reply naming none of them is no reading of the roster: it
 * is malformed like a wrong shape (`{providers: []}` included), and the last providers stay. Each named
 * provider is a record, and so is each model row a provider lists.
 */
export function decodeModels(body: unknown): Decoded<ProviderChoice[]> | null {
  if (!isObj(body) || !isObj(body.providers)) return null;
  const providers = body.providers;
  const named = PROVIDERS.filter((name) => name in providers);
  if (!named.length) return null;
  const rows = named.flatMap((name) => {
    const p = providers[name];
    return isObj(p) && Array.isArray(p.models) ? p.models : [];
  });
  return { value: parseModels(body), lists: [
    tally(named.map((name) => providers[name]), isObj),
    tally(rows, (m) => isObj(m) && typeof m.id === "string" && m.id !== ""),
  ] };
}
