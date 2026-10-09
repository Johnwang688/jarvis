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
// (window visible) when it finishes — the owner watched it happen. Neither
// the snapshot nor a `/seen` answer may undo a newer record (ActivitySync).

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

/** A request whose answer is replayed over: one `GET /activity` in flight. */
interface Pending {
  n: number;
  records: any[];
}

/** What `POST …/seen` was sent against. */
export interface SeenTicket {
  of: "thread" | "task";
  id: string;
  heard: number;
  begun: number;
}

const keyOf = (record: any): string | null => {
  const data = record?.data || {};
  return (data.of === "thread" || data.of === "task") && typeof data.id === "string" ? `${data.of}:${data.id}` : null;
};

/** The answers that can be older than a record already heard (review,
 * 2026-10-09). `GET /activity` and `POST …/seen` both answer with the status
 * as the daemon worked it out, while SSE records keep arriving; the daemon
 * publishes on change only, so an older answer applied as-is would undo a
 * newer record for good. Every SSE `activity` record goes through `heard`. */
export class ActivitySync {
  private inFlight = new Set<Pending>();
  private counts = new Map<string, number>();
  private begun = 0;
  private landed = 0;

  /** An SSE `activity` record, as it is dispatched: it is replayed over
   * every snapshot still in flight, which may have been taken before it. */
  heard(record: any): void {
    for (const p of this.inFlight) p.records.push(record);
    const key = keyOf(record);
    if (key) this.counts.set(key, (this.counts.get(key) || 0) + 1);
  }

  /** `GET /activity` is sent. */
  begin(): Pending {
    const p = { n: ++this.begun, records: [] };
    this.inFlight.add(p);
    return p;
  }

  /** …and answered: the snapshot, with every record heard since it was sent. */
  land(p: Pending, snapshot: ActivityView): ActivityView {
    this.inFlight.delete(p);
    this.landed = Math.max(this.landed, p.n);
    return p.records.reduce<ActivityView>((view, record) => applyActivity(view, record), snapshot);
  }

  /** …or failed. */
  drop(p: Pending): void {
    this.inFlight.delete(p);
  }

  /** `POST …/seen` is sent. */
  ask(of: "thread" | "task", id: string): SeenTicket {
    return { of, id, heard: this.counts.get(`${of}:${id}`) || 0, begun: this.begun };
  }

  /** …and answered with `status`: the record to dispatch, or null when
   * something newer about that row has arrived since — a record of its own,
   * or a snapshot sent after the ask. */
  answered(t: SeenTicket, status: unknown): any | null {
    const key = `${t.of}:${t.id}`;
    if (!isStatus(status) || (this.counts.get(key) || 0) !== t.heard || this.landed > t.begun) return null;
    const record = { kind: "activity", data: { of: t.of, id: t.id, status } };
    for (const p of this.inFlight) if (p.n <= t.begun) p.records.push(record);
    this.counts.set(key, t.heard + 1);
    return record;
  }
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
