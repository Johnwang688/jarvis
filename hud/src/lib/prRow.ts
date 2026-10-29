// PR rows (PM-F of docs/plans/2026-10-28-pr-inbox-merge.md: PM-24 to PM-27,
// the owner's Q1 to Q4), the rules both surfaces share: the HUD's inbox
// (components/InboxPrRow.tsx) and the phone's (mobile/views.ts, mobile/app.ts).
//
// A PR row's `detail` (PM-C's `pr_summary.ENRICH_KEYS`, PM-D's hold and card,
// PM-E's fix) is read here into a view. Pure: strings, numbers and booleans,
// no markup. **Every string is untrusted text**, drawn as React text or
// `textContent`, never HTML:
//   - code's words: the facts parts' `text`, `why`, the summary's state and
//     note and reason, the hold's label, the fix's state; drawn plain;
//   - names (check names, paths, the PR's title, the hold's note): untrusted,
//     each in its own `<bdi>` and box, never joined into code's words;
//   - the model's words (the summary's Does and Blocked, the fix's summary):
//     only inside the "AI SUMMARY" slot, labelled as the model's, never as a
//     line code could have written.
// What an OK binds is the row's `head` and `card` exactly as drawn (PM-13,
// MED 2 of PM-D's review): a surface sends the values it drew, never a value
// read again at click time.

import { oneLine, textBlock, type InboxRow } from "./inbox";

export type PrAction = "merge" | "merge_as_is" | "fix" | "update";
/** The buttons' order on the HUD (left to right). */
export const PR_ACTIONS: readonly PrAction[] = ["merge", "merge_as_is", "fix", "update"];
export const ACTION_LABELS: Record<PrAction, string> = {
  merge: "Merge", merge_as_is: "Merge as is", fix: "Fix", update: "Update",
};
/** What the phone's fixed notice slot says while an action runs (after "Asking for Face ID…"). */
export const ACTION_PROGRESS: Record<PrAction, string> = {
  merge: "Merging…", merge_as_is: "Merging…", fix: "Starting fix…", update: "Updating…",
};
/** The HUD's routes (PM-D, PM-E): `POST /inbox/pr/<route>`. */
export type PrRoute = "merge" | "update" | "hold" | "fix" | "fix/cancel";
export const ROUTE_OF: Record<PrAction, PrRoute> = { merge: "merge", merge_as_is: "merge", fix: "fix", update: "update" };

export const AI_TAG = "AI SUMMARY";
export const PENDING_WORDS = "summarizing…";
export const UNAVAILABLE = "AI summary unavailable";
/** The phone's words when a limited phone is offered nothing on a flagged PR (Q1). */
export const AT_DESK = "touches safety files: answer it at the desk or on a fully checked phone";
export const HOLD_MAX = 200;

const SHA = /^[0-9a-f]{40}$/;
const CARD = /^[0-9a-f]{64}$/;
const TASK_ID = /^[A-Za-z0-9_-]{1,64}$/;
const SCANNER_SUFFIX = " [secret scanner]";
/** PM-10's families, code's words (`pr_facts.FLAG_*`); anything else is not drawn as one. */
const FAMILIES: ReadonlySet<string> = new Set(["self-protected", "agent instructions", "CI", "secret-scanner config",
                                               "credential file"]);
/** The facts parts' keys (`pr_facts.facts_parts`). */
const FACT_KEYS: ReadonlySet<string> = new Set(["pr", "state", "branch", "checks", "conflicts", "merge_state", "review",
                                                "files", "head", "fork", "method", "read"]);

export interface PrName {
  /** An untrusted name (a check, a path), drawn in its own `<bdi>`. */
  name: string;
  /** Code's "[secret scanner]" label: only when the row's own `failing` says this check is a scanner. */
  scanner: boolean;
}

export interface PrFact {
  key: string;
  /** Code's words for the part. */
  text: string;
  items: PrName[];
  more: number;
}

export interface PrFlags {
  /** False only when the daemon says nothing is flagged; true or unknown otherwise (fails closed). */
  flagged: boolean;
  items: { name: string; families: string[] }[];
  more: number;
  incomplete: boolean;
}

export type FixState = "starting" | "running" | "question" | "blocked" | "stopped" | "checking" | "rewritten"
  | "failing" | "pushed" | "ready" | "start_failed";
const FIX_STATES: ReadonlySet<string> = new Set(["starting", "running", "question", "blocked", "stopped", "checking",
                                                 "rewritten", "failing", "pushed", "ready", "start_failed"]);
/** A fix the owner may Cancel: its task is alive (or blocked, which holds the PR). */
const LIVE: ReadonlySet<FixState> = new Set(["starting", "running", "question", "blocked"]);

export interface PrFix {
  state: FixState;
  taskId: string | null;
  /** Code's words for the state (never READY's, which carries the model's summary). */
  note: string;
  commits: number | null;
  /** Code's CI check of the fix: true changed CI, false did not, null not every file compared. */
  ciChanged: boolean | null;
  /** The model's one line about the fix (display only, in the AI slot). */
  summary: string;
  summaryState: "pending" | "ready" | "failed" | "off" | null;
  count: number;
  max: number;
  live: boolean;
}

export interface PrRowView {
  id: string;
  repo: string;
  number: number;
  /** The PR's own title: the author's words. */
  title: string;
  /** The head the row binds (40 hex), or null: no action then. */
  head: string | null;
  /** The card key the row binds (64 hex), or null: no action then. */
  card: string | null;
  facts: PrFact[] | null;
  flags: PrFlags | null;
  summary: { does: string; blocked: string } | null;
  /** "none": the row carries no summary state (a daemon without summaries). */
  summaryState: "pending" | "ready" | "failed" | "off" | "none";
  summaryNote: string;
  summaryReason: string;
  actions: PrAction[];
  why: string;
  method: "merge" | "squash" | null;
  /** Q3: the hold's note ("" for a hold without one), or null when not held. */
  hold: string | null;
  fix: PrFix | null;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): number => (typeof v === "number" && Number.isInteger(v) && v > 0 ? Math.min(v, 1e6) : 0);

function scannerNames(failing: unknown): Set<string> {
  const out = new Set<string>();
  if (!Array.isArray(failing)) return out;
  for (const f of failing) if (isObj(f) && f.scanner === true && typeof f.name === "string") out.add(oneLine(f.name, 120));
  return out;
}

function factsOf(raw: unknown, scanners: Set<string>): PrFact[] | null {
  if (!Array.isArray(raw)) return null;
  const out: PrFact[] = [];
  for (const part of raw.slice(0, 16)) {
    if (!isObj(part)) continue;
    const text = oneLine(part.text, 200);
    const key = typeof part.key === "string" && FACT_KEYS.has(part.key) ? part.key : "";
    if (!text || !key) continue;
    const items: PrName[] = (Array.isArray(part.items) ? part.items : []).slice(0, 20).map((x) => {
      const name = oneLine(x, 140);
      // The daemon appends " [secret scanner]" to a scanner's name; the label is
      // drawn as code's only when the row's `failing` (code's own field) agrees.
      if (key === "checks" && name.endsWith(SCANNER_SUFFIX)) {
        const base = name.slice(0, -SCANNER_SUFFIX.length);
        if (scanners.has(base)) return { name: base, scanner: true };
      }
      return { name, scanner: false };
    }).filter((n) => n.name);
    out.push({ key, text, items, more: count(part.more) });
  }
  return out;
}

function flagsOf(raw: unknown): PrFlags | null {
  if (!isObj(raw)) return null;
  const items = (Array.isArray(raw.items) ? raw.items : []).filter(isObj).slice(0, 20).map((f) => ({
    name: oneLine(f.name, 200),
    families: (Array.isArray(f.families) ? f.families : []).filter((x): x is string => typeof x === "string" && FAMILIES.has(x)),
  })).filter((f) => f.name);
  return { flagged: raw.flagged !== false, items, more: count(raw.more), incomplete: raw.incomplete === true };
}

function fixOf(raw: unknown): PrFix | null {
  if (!isObj(raw) || typeof raw.state !== "string" || !FIX_STATES.has(raw.state)) return null;
  const state = raw.state as FixState;
  const s = raw.summary_state;
  return {
    state,
    taskId: typeof raw.task_id === "string" && TASK_ID.test(raw.task_id) ? raw.task_id : null,
    // READY's note is "fixed: <model text> — merge?": never drawn as code's.
    note: state === "ready" ? "" : oneLine(raw.note, 300),
    commits: typeof raw.commits === "number" && Number.isInteger(raw.commits) && raw.commits >= 0 ? raw.commits : null,
    ciChanged: raw.ci_changed === true ? true : raw.ci_changed === false ? false : null,
    summary: oneLine(raw.summary, 300),
    summaryState: s === "pending" || s === "ready" || s === "failed" || s === "off" ? s : null,
    count: count(raw.count),
    max: count(raw.max),
    live: LIVE.has(state),
  };
}

/** A PR row's view, or null for any other row or a detail that is not a PR's. */
export function prRowOf(row: InboxRow): PrRowView | null {
  if (row.kind !== "pr") return null;
  const d = row.detail;
  const repo = typeof d.repo === "string" ? oneLine(d.repo, 201) : "";
  const number = d.number;
  if (!repo || typeof number !== "number" || !Number.isInteger(number) || number < 1) return null;
  const sum = isObj(d.summary) ? d.summary : null;
  const does = sum ? oneLine(sum.does, 300) : "";
  const state = d.summary_state;
  const summaryState = state === "pending" || state === "ready" || state === "failed" || state === "off" ? state : "none";
  const hold = isObj(d.hold) ? oneLine(d.hold.note, HOLD_MAX) : typeof d.hold === "string" ? oneLine(d.hold, HOLD_MAX) : null;
  const method = d.method === "merge" || d.method === "squash" ? d.method : null;
  const actions = Array.isArray(d.actions)
    ? PR_ACTIONS.filter((a) => (d.actions as unknown[]).includes(a)) : [];
  return {
    id: row.id, repo, number,
    title: oneLine(d.title, 300),
    head: typeof d.head === "string" && SHA.test(d.head) ? d.head : null,
    card: typeof d.card === "string" && CARD.test(d.card) ? d.card : null,
    facts: factsOf(d.facts, scannerNames(d.failing)),
    flags: flagsOf(d.flags),
    summary: does ? { does, blocked: sum ? oneLine(sum.blocked, 360) : "" } : null,
    summaryState,
    summaryNote: oneLine(d.summary_note, 300),
    summaryReason: oneLine(d.summary_reason, 300),
    actions,
    why: textBlock(d.why, 600).replace(/\n+/g, " "),
    method,
    hold,
    fix: fixOf(d.fix),
  };
}

export interface PrButtons {
  /** The actions offered, in the HUD's order. */
  actions: PrAction[];
  /** A limited phone and a flagged PR (Q1): nothing is offered, and the row says so. */
  desk: boolean;
}

/**
 * The buttons a surface draws. None while held (Q3), or without the head and
 * card an OK binds. A limited phone gets nothing on a PR the daemon does not
 * say is unflagged (Q1; the server refuses it anyway, `pr_merge.phone_options`).
 */
export function prButtons(v: PrRowView, opts: { limited?: boolean } = {}): PrButtons {
  if (v.hold !== null || !v.head || !v.card) return { actions: [], desk: false };
  if (opts.limited && v.actions.length && v.flags?.flagged !== false) return { actions: [], desk: true };
  return { actions: [...v.actions], desk: false };
}

/** A button's label: a Fix after a fix that ended is "Fix again". */
export function actionLabel(v: PrRowView, action: PrAction): string {
  if (action === "fix" && v.fix && !v.fix.live) return "Fix again";
  return ACTION_LABELS[action];
}

/** Q4: the merge method, named before the OK when it is a squash ("" otherwise, or with no Merge). */
export function methodWords(v: PrRowView, actions: readonly PrAction[]): string {
  if (v.method !== "squash" || !actions.some((a) => a === "merge" || a === "merge_as_is")) return "";
  return "squash merge: merge commits are off for this repository";
}

export interface PrSummaryWords {
  state: "pending" | "ready" | "none";
  /** Ready: the model's Does and Blocked lines. */
  lines: { label: "Does" | "Blocked"; text: string }[];
  /** Pending or none: code's words for the slot. */
  text: string;
}

/** The AI slot's contents (PM-7): the two lines, "summarizing…", or why there is none. */
export function prSummaryWords(v: PrRowView): PrSummaryWords {
  if (v.summaryState === "ready" && v.summary) {
    const lines: PrSummaryWords["lines"] = [{ label: "Does", text: v.summary.does }];
    if (v.summary.blocked) lines.push({ label: "Blocked", text: v.summary.blocked });
    return { state: "ready", lines, text: "" };
  }
  if (v.summaryState === "pending") return { state: "pending", lines: [], text: PENDING_WORDS };
  const reason = v.summaryReason
    || (v.summaryState === "none" ? "this daemon does not summarize pull requests" : v.summaryState === "ready"
      ? "the summary could not be shown" : "no reason was given");
  return { state: "none", lines: [], text: `${UNAVAILABLE}: ${reason}` };
}

export interface PrFixWords {
  /** Code's line for the fix. */
  text: string;
  tone: "plain" | "amber";
  /** The fix's task can be cancelled. */
  cancel: boolean;
  /** The fix waits on the owner's answer to its task's question. */
  question: boolean;
  /** The fix card: "Fixed — merge?", the AI slot for the fix's summary, its counts and ambers. */
  card: boolean;
}

const short = (id: string | null) => (id ? id.slice(0, 8) : "");

/** What the row says about its fix (PM-21 to PM-23), in code's words only. */
export function prFixWords(fix: PrFix): PrFixWords {
  const task = fix.taskId ? ` (task ${short(fix.taskId)})` : "";
  const base = { tone: "plain" as const, cancel: false, question: false, card: false };
  switch (fix.state) {
    case "starting": return { ...base, text: `fix starting${task}`, cancel: !!fix.taskId };
    case "running": return { ...base, text: `fix running${task}`, cancel: !!fix.taskId };
    case "question": return { ...base, text: `fix waits on your answer${task}`, cancel: !!fix.taskId, question: true };
    case "blocked": return { ...base, text: (fix.note || "fix stopped: blocked: cancel it to Fix again") + task,
                             cancel: !!fix.taskId };
    case "rewritten": return { ...base, tone: "amber", text: "branch rewritten since your Fix" };
    case "ready": return { ...base, text: "Fixed — merge?", card: true };
    case "stopped": return { ...base, text: fix.note || "fix stopped" };
    case "start_failed": return { ...base, text: fix.note || "fix could not start" };
    case "failing": return { ...base, text: fix.note || "fix pushed; checks still fail" };
    case "pushed": return { ...base, text: fix.note || "fix pushed; checks running" };
    default: return { ...base, text: fix.note || "checking the fix" };
  }
}

/** The fix card's code lines: commits, and CI (amber). */
export function fixCardLines(fix: PrFix): { commits: string; ci: string } {
  const commits = fix.commits === null ? "" : `${fix.commits} commit${fix.commits === 1 ? "" : "s"} since your Fix`;
  const ci = fix.ciChanged === true ? "the fix changed CI configuration"
    : fix.ciChanged === null ? "not every file of the fix could be checked for CI changes" : "";
  return { commits, ci };
}

/** The fix's summary in its AI slot: the model's line, "summarizing…", or why there is none. */
export function fixSummaryWords(fix: PrFix): { state: "pending" | "ready" | "none"; text: string } {
  if (fix.summaryState === "ready" && fix.summary) return { state: "ready", text: fix.summary };
  if (fix.summaryState === "pending" || fix.summaryState === null) return { state: "pending", text: PENDING_WORDS };
  return { state: "none", text: `${UNAVAILABLE}: the fix's summary could not be written` };
}

/** The `tq:` row of the fix's task question, when it is in the inbox. */
export function questionRow(fix: PrFix, rows: readonly InboxRow[]): string | null {
  if (!fix.taskId) return null;
  const found = rows.find((r) => r.kind === "task_question" && r.id.startsWith(`tq:${fix.taskId}:`));
  return found ? found.id : null;
}

/**
 * AS-13 on the HUD: what re-arms the buttons. Everything the row shows that an
 * OK answers or that changes what the row says around the buttons: the whole
 * view (title, head, card, facts, flags, summary and its state, note and reason,
 * actions, why, method, hold, the fix), so a field added to the view re-arms too.
 */
export function prArmKey(v: PrRowView): string {
  return JSON.stringify(v);
}

/** The body an action sends: exactly the head and card the row drew (never read again). */
export function actionBody(v: Pick<PrRowView, "id" | "head" | "card">, action: PrAction): Record<string, unknown> | null {
  if (!v.head || !v.card) return null;
  if (action === "merge_as_is") return { id: v.id, head: v.head, card: v.card, action };
  return { id: v.id, head: v.head, card: v.card };
}

/** What an action's 200 says, in code's words (the daemon's `result` is a fixed word). */
export function resultWords(action: PrAction | "cancel", body: unknown): string {
  const b = isObj(body) ? body : {};
  const word = oneLine(b.result, 60);
  if (action === "cancel") return "Fix cancelled.";
  if (action === "fix") {
    const id = typeof b.task_id === "string" && TASK_ID.test(b.task_id) ? b.task_id : "";
    return id ? `Fix started (task ${short(id)}).` : "Fix started.";
  }
  if (action === "update") {
    if (word === "update requested") return "Update requested: GitHub merges the base in; Merge again once it has.";
    if (word === "updated") return "Updated: Merge again for the new head.";
    return word ? `Not updated: ${word}.` : "Not updated.";
  }
  if (word === "merged") return b.method === "squash" ? "Merged (squash merge)." : "Merged.";
  if (word === "queued") return "Queued: GitHub will merge it.";
  return word ? `Not merged: ${word}.` : "Not merged.";
}

/** Whether an action's 200 did what was asked (a "head moved" or "not mergeable" reads amber). */
export function resultOk(action: PrAction | "cancel", body: unknown): boolean {
  if (action === "cancel" || action === "fix") return true;
  const word = isObj(body) ? body.result : null;
  return word === "merged" || word === "queued" || word === "updated" || word === "update requested";
}
