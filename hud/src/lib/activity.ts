// The dot beside each sidebar row (2026-10-08): what a thread or task is
// doing. The daemon decides every status (jarvis/v2/activity.py) and this
// window only draws it — `GET /activity` once, then an SSE `activity` record
// per change.
//
//   idle         the plain `·`
//   working      a turn is running (a task: a phase that is not over)
//   needs_input  yellow: an approval, an open question, a blocked task
//   unread       blue: finished since the owner last opened it
//   failed       a red ⚠: ended in an error since the owner last opened it
//
// Opening a thread or task marks it read, and so does having it on screen
// (window visible) when it finishes — the owner watched it happen.

export type ActivityStatus = "idle" | "working" | "needs_input" | "unread" | "failed";

export interface ActivityView {
  threads: Record<string, ActivityStatus>;
  tasks: Record<string, ActivityStatus>;
}

export const NO_ACTIVITY: ActivityView = { threads: {}, tasks: {} };

const STATUSES: ActivityStatus[] = ["idle", "working", "needs_input", "unread", "failed"];

/** Most urgent first: what a collapsed project shows for everything in it. */
const URGENCY: ActivityStatus[] = ["failed", "needs_input", "working", "unread", "idle"];

export function isStatus(value: unknown): value is ActivityStatus {
  return typeof value === "string" && (STATUSES as string[]).includes(value);
}

export function mostUrgent(statuses: Iterable<ActivityStatus>): ActivityStatus {
  let best = URGENCY.length - 1;
  for (const s of statuses) best = Math.min(best, URGENCY.indexOf(s) < 0 ? best : URGENCY.indexOf(s));
  return URGENCY[best];
}

/** A collapsed project's dot: its chat threads and its tasks (a task already
 * carries its own threads' approvals and questions). */
export function projectActivity(
  view: ActivityView,
  threadIds: Iterable<string>,
  taskIds: Iterable<string>,
): ActivityStatus {
  const all: ActivityStatus[] = [];
  for (const id of threadIds) all.push(view.threads[id] || "idle");
  for (const id of taskIds) all.push(view.tasks[id] || "idle");
  return mostUrgent(all);
}

/** One SSE `activity` record folded in; anything malformed changes nothing. */
export function applyActivity(view: ActivityView, record: any): ActivityView {
  const data = record?.data || {};
  const of = data.of === "task" ? "tasks" : data.of === "thread" ? "threads" : null;
  const id = data.id;
  if (!of || typeof id !== "string" || !isStatus(data.status)) return view;
  if ((view[of][id] || "idle") === data.status) return view;
  const next = { ...view[of] };
  if (data.status === "idle") delete next[id];
  else next[id] = data.status;
  return { ...view, [of]: next };
}

/** `GET /activity`, kept to what this window can draw. */
export function parseActivity(body: any): ActivityView {
  const pick = (m: any) => {
    const out: Record<string, ActivityStatus> = {};
    if (m && typeof m === "object") for (const [k, v] of Object.entries(m)) if (isStatus(v) && v !== "idle") out[k] = v;
    return out;
  };
  return { threads: pick(body?.threads), tasks: pick(body?.tasks) };
}

/** Whether a status is one that reading clears. */
export function clearsOnRead(status: ActivityStatus | undefined): boolean {
  return status === "unread" || status === "failed";
}

export const ACTIVITY_LABEL: Record<ActivityStatus, string> = {
  idle: "",
  working: "working",
  needs_input: "waiting for you",
  unread: "finished · unread",
  failed: "failed",
};
