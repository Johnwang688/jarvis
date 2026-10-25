// The phone app's data, as the read API sends it (phone design §5 and §6,
// P-3; jarvis/v2/remote_api.py). Pure: parsing, ordering and grouping only,
// so every rule here is tested without a browser (model.test.ts).
//
// **Everything a row, a thread, a task or a log line carries is untrusted
// text** (a model wrote most of it). Nothing here builds HTML; the views set
// it with textContent, and only an assistant's reply goes through the
// sanitized Markdown renderer (./markdown.ts).

import { oneLine, textBlock } from "../lib/inbox";

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strOrNull = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const ID = /^[0-9a-f]{8}$/;
const idOrNull = (v: unknown): string | null => (typeof v === "string" && ID.test(v) ? v : null);

export interface Project { id: string; name: string; inbox: boolean }

export interface ChatThread {
  id: string;
  project_id: string;
  task_id: string | null;
  title: string;
  provider: string;
  updated: string;
  running: boolean;
  queued: number;
  turns: number;
}

export interface LogRow { seq: number; role: string; text: string; at: string | null; marks: string[]; cut: boolean }

export interface LogPage { rows: LogRow[]; next: number; more: boolean; older: boolean; thread: ChatThread | null }

export interface Report { done: string[]; changed: string[]; verified: string; open: string[]; next: string }

export interface TaskQuestion { text: string; options: string[]; blocking: boolean; answer: string | null }

export interface TaskView {
  id: string;
  project_id: string;
  brief: string;
  state: string;
  updated: string;
  step: number;
  steps: number;
  openQuestion: string;
  plan: string[];
  report: Report | null;
  questions: TaskQuestion[];
  branch: string;
}

export interface DiffFile { path: string; status: string; additions: number; deletions: number; patch: string | null; tooLarge: boolean }

export interface DiffView { files: DiffFile[]; truncated: boolean; note: string }

export function parseProjects(raw: unknown): Project[] {
  const list = isObj(raw) && Array.isArray(raw.projects) ? raw.projects : [];
  const out: Project[] = [];
  for (const p of list) {
    const id = isObj(p) ? idOrNull(p.id) : null;
    if (!id || !isObj(p)) continue;
    out.push({ id, name: oneLine(p.name, 80) || "(unnamed)", inbox: p.inbox === true });
  }
  return out;
}

export function parseThread(t: unknown): ChatThread | null {
  if (!isObj(t)) return null;
  const id = idOrNull(t.id), project = idOrNull(t.project_id);
  if (!id || !project) return null;
  return {
    id, project_id: project, task_id: idOrNull(t.task_id),
    title: oneLine(t.title, 120), provider: oneLine(t.provider, 20),
    updated: str(t.updated), running: t.running === true, queued: num(t.queued), turns: num(t.turns),
  };
}

export function parseThreads(raw: unknown): ChatThread[] {
  const list = isObj(raw) && Array.isArray(raw.threads) ? raw.threads : [];
  return list.map(parseThread).filter((t): t is ChatThread => t !== null);
}

export function parseLog(raw: unknown): LogPage | null {
  if (!isObj(raw) || !Array.isArray(raw.rows) || typeof raw.next !== "number") return null;
  const rows: LogRow[] = [];
  for (const r of raw.rows) {
    if (!isObj(r) || typeof r.seq !== "number") continue;
    rows.push({
      // The text as sent (already scrubbed at the source, invisible
      // characters out): its spacing is Markdown's, so it is not collapsed.
      seq: r.seq, role: oneLine(r.role, 120) || "system", text: str(r.text).slice(0, 40000),
      at: strOrNull(r.at), marks: Array.isArray(r.marks) ? r.marks.map((m) => oneLine(m, 40)).filter(Boolean) : [],
      cut: typeof r.cut === "number",
    });
  }
  return { rows, next: raw.next, more: raw.more === true, older: raw.older === true, thread: parseThread(raw.thread) };
}

/** The roles whose text is an assistant's reply: rendered as Markdown. Everything else is text. */
export const ASSISTANT_ROLES: ReadonlySet<string> = new Set(["Jarvis", "Claude", "Codex", "assistant"]);

function strings(v: unknown, cap = 400): string[] {
  return Array.isArray(v) ? v.map((s) => textBlock(s, cap)).filter(Boolean) : [];
}

export function parseTask(t: unknown): TaskView | null {
  if (!isObj(t)) return null;
  const id = idOrNull(t.id);
  if (!id) return null;
  const status = isObj(t.status) ? t.status : {};
  const report = isObj(t.report) ? {
    done: strings(t.report.done), changed: strings(t.report.changed), verified: textBlock(t.report.verified, 2000),
    open: strings(t.report.open), next: textBlock(t.report.next, 2000),
  } : null;
  const spec = isObj(t.spec) ? t.spec : {};
  const questions = Array.isArray(spec.questions) ? spec.questions.filter(isObj).map((q) => ({
    text: textBlock(q.text, 2000), options: strings(q.options, 200), blocking: q.blocking === true,
    answer: typeof q.answer === "string" ? textBlock(q.answer, 2000) : null,
  })) : [];
  return {
    id, project_id: idOrNull(t.project_id) || "", brief: textBlock(t.brief, 20000), state: oneLine(t.state, 20),
    updated: str(t.updated), step: num(status.step), steps: num(status.steps),
    openQuestion: textBlock(status.open_question, 2000), plan: strings(t.plan, 2000), report, questions,
    branch: oneLine(t.branch, 200),
  };
}

export function parseTasks(raw: unknown): TaskView[] {
  const list = isObj(raw) && Array.isArray(raw.tasks) ? raw.tasks : [];
  return list.map(parseTask).filter((t): t is TaskView => t !== null);
}

export function parseDiff(raw: unknown): DiffView | null {
  if (!isObj(raw) || !Array.isArray(raw.files)) return null;
  const files: DiffFile[] = [];
  for (const f of raw.files) {
    if (!isObj(f) || typeof f.path !== "string") continue;
    files.push({
      path: oneLine(f.path, 400), status: oneLine(f.status, 2) || "M", additions: num(f.additions),
      deletions: num(f.deletions), patch: typeof f.patch === "string" ? f.patch : null, tooLarge: f.too_large === true,
    });
  }
  return { files, truncated: raw.truncated === true, note: oneLine(raw.note, 200) };
}

/** Tasks tab order (design §6): running first, then blocked, then the rest, each newest first. */
export const RUNNING_STATES: ReadonlySet<string> = new Set(["running", "verifying"]);
export const BLOCKED_STATES: ReadonlySet<string> = new Set(["blocked"]);

export function taskGroup(state: string): 0 | 1 | 2 {
  return RUNNING_STATES.has(state) ? 0 : BLOCKED_STATES.has(state) ? 1 : 2;
}

export function orderTasks(tasks: readonly TaskView[]): { label: string; tasks: TaskView[] }[] {
  const stamp = (t: TaskView) => { const n = Date.parse(t.updated); return Number.isNaN(n) ? -Infinity : n; };
  const sorted = tasks.map((t, i) => ({ t, i })).sort((a, b) =>
    taskGroup(a.t.state) - taskGroup(b.t.state) || stamp(b.t) - stamp(a.t) || a.i - b.i).map((x) => x.t);
  const labels = ["Running", "Blocked", "Everything else"];
  const out: { label: string; tasks: TaskView[] }[] = [];
  for (const g of [0, 1, 2] as const) {
    const mine = sorted.filter((t) => taskGroup(t.state) === g);
    if (mine.length) out.push({ label: labels[g], tasks: mine });
  }
  return out;
}

/** How a patch line is drawn: added, removed, a hunk header, a file header, or context. */
export function lineKind(line: string): "add" | "del" | "hunk" | "file" | "ctx" {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git")
      || line.startsWith("index ") || line.startsWith("new file") || line.startsWith("deleted file")
      || line.startsWith("rename ") || line.startsWith("similarity ")) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

/** The stream's records (remote_api.stream_record): anything else is dropped. */
export type StreamEvent =
  | { kind: "inbox_changed"; ids: string[] }
  | { kind: "thread_changed"; threadId: string; projectId: string | null }
  | { kind: "task_changed"; taskId: string; projectId: string | null }
  | { kind: "resync" };

export function parseEvent(text: string): StreamEvent | null {
  let raw: unknown;
  try { raw = JSON.parse(text); } catch { return null; }
  if (!isObj(raw) || typeof raw.kind !== "string") return null;
  const data = isObj(raw.data) ? raw.data : {};
  if (raw.kind === "inbox_changed") {
    return { kind: "inbox_changed", ids: Array.isArray(data.ids) ? data.ids.filter((i): i is string => typeof i === "string") : [] };
  }
  if (raw.kind === "thread_changed") {
    const id = idOrNull(data.thread_id);
    return id ? { kind: "thread_changed", threadId: id, projectId: idOrNull(data.project_id) } : null;
  }
  if (raw.kind === "task_changed") {
    const id = idOrNull(data.task_id);
    return id ? { kind: "task_changed", taskId: id, projectId: idOrNull(data.project_id) } : null;
  }
  if (raw.kind === "resync") return { kind: "resync" };
  return null;
}

/** The kinds a phone row is answered at the desk in P-3, and what the note says. */
export function deskNote(kind: string): string | null {
  if (kind === "approval" || kind === "question" || kind === "task_question") return "Answer at the desk";
  if (kind === "proposal") return "Withdraw at the desk";
  if (kind === "task") return "Resume or cancel at the desk";
  return null;
}
