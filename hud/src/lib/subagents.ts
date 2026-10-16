// Native sub-agents in the HUD (SV-3, plan docs/plans/2026-10-13-subagent-view.md
// §D5). Pure: what the daemon says about a chat's own Claude/Codex children,
// cleaned; how a bus record and a list fetch fold together; where each row
// sits in the parent's transcript; and what a row and a pane say.
//
// Two sources, deliberately unequal (docs/hud-api.md "Native sub-agents"):
//
//  - the bus's `subagent` record carries ids, a status, times and counts —
//    never a name, a word a child wrote or a tool's arguments, since
//    `/events` is open to any local client;
//  - the owner-only routes (`GET /threads/{id}/subagents`, and
//    `…/subagents/{sid}/transcript`) carry the rest: the name and kind the
//    model chose, tokens, the last tool, the child's words and tool labels,
//    and its brief.
//
// So a bus record moves a row's status at once, and the list is read again
// (throttled, `state/subagents.ts`) for the words. Everything shown is text:
// names, kinds and labels are re-cleaned with `toolLabel` here, whatever the
// daemon already did.

import type { ChatMessage, ToolCall } from "../types";
import { outcome, toolLabel } from "./toolCalls";
import { elapsed, toolName } from "./thinking";
import { isTrustedPress } from "./vex";

export type SubStatus = "running" | "backgrounded" | "done" | "failed" | "stopped";
export const SUB_STATUSES: readonly SubStatus[] = ["running", "backgrounded", "done", "failed", "stopped"];
export const SID = /^(cl|cx):[A-Za-z0-9_-]{1,128}$/;
/** The daemon keeps 64 children per thread (`subagents.CHILD_LIMIT`); so does the window. */
export const CHILD_CAP = 64;
/** More rows than this at one spot collapse into one line (owner's open question 1). */
export const COLLAPSE_OVER = 6;
/** The daemon's cap on a child's words and brief (`subagents.TEXT_CAP`). */
export const TEXT_CAP = 4000;
const NAME_CAP = 40;
const REASON_CAP = 80;
const STOP_WHY_CAP = 160;

export type SubProvider = "claude" | "codex";

export interface Subagent {
  sid: string;
  status: SubStatus;
  depth: number;
  background: boolean;
  /** Model-chosen (the Agent call's description, Codex's `agentPath`): text only, at most 40 characters. */
  name: string;
  kind: string;
  started_at: string;
  ended_at?: string;
  turn_id?: string;
  /** Transcript entries ever made: never goes back (a stale record has fewer). */
  entries: number;
  task_id?: string;
  parent_sid?: string;
  stop_reason?: string;
  tokens?: number;
  tool_uses?: number;
  duration_ms?: number;
  last_tool?: string;
  declined?: number;
  /** The owner's Stop (SV-4): on its way to the CLI, or sent and not confirmed. Only while live. */
  stop?: StopState;
  stop_requested_at?: string;
  /** The list route's word (SV-4): whether Stop can be sent to this live child now… */
  stoppable?: boolean;
  /** …and, when it cannot, why (one plain sentence). */
  stop_unavailable?: string;
}

export type StopState = "stopping" | "unconfirmed";

/** What one bus `subagent` record may say: ids, a status, times and counts. */
export interface SubBus {
  sid: string;
  op: "started" | "progress" | "finished";
  status: SubStatus;
  depth: number;
  background: boolean;
  started_at: string;
  entries: number;
  task_id?: string;
  parent_sid?: string;
  ended_at?: string;
  declined?: number;
  stop?: StopState;
  stop_requested_at?: string;
}

export const validSid = (v: unknown): v is string => typeof v === "string" && SID.test(v);
export const providerOf = (sid: string): SubProvider => (sid.startsWith("cx:") ? "codex" : "claude");
export const PROVIDER_WORD: Readonly<Record<SubProvider, string>> = { claude: "Claude", codex: "Codex" };
export const isLive = (s: SubStatus) => s === "running" || s === "backgrounded";

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isInteger(v) && v >= 0 && v < 2 ** 53 ? v : undefined;
const when = (v: unknown): string | undefined =>
  typeof v === "string" && v.length <= 64 && Number.isFinite(Date.parse(v)) ? v : undefined;
const anId = (v: unknown): string | undefined =>
  typeof v === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v) ? v : undefined;
const status = (v: unknown): SubStatus | null => (SUB_STATUSES as readonly unknown[]).includes(v) ? v as SubStatus : null;
const stopState = (v: unknown): StopState | undefined => (v === "stopping" || v === "unconfirmed" ? v : undefined);

/** One line of model-chosen text, as the window shows it: cleaned, capped by characters. */
export function line(v: unknown, cap: number): string {
  if (typeof v !== "string") return "";
  const flat = toolLabel(v);
  const chars = Array.from(flat);
  return chars.length <= cap ? flat : chars.slice(0, cap - 1).join("").trimEnd() + "…";
}

/** A row of `GET /threads/{id}/subagents`, or null for anything that is not one. */
export function cleanRow(raw: unknown): Subagent | null {
  if (!isObj(raw) || !validSid(raw.sid)) return null;
  const s = status(raw.status) ?? "stopped";
  const row: Subagent = {
    sid: raw.sid, status: s, depth: Math.min(16, Math.max(1, count(raw.depth) ?? 1)),
    background: raw.background === true, name: line(raw.name, NAME_CAP), kind: line(raw.kind, NAME_CAP),
    started_at: when(raw.started_at) ?? "", entries: count(raw.entries) ?? 0,
  };
  const ended = when(raw.ended_at);
  if (ended) row.ended_at = ended;
  if (typeof raw.turn_id === "string" && raw.turn_id.length <= 128) row.turn_id = raw.turn_id;
  const task = anId(raw.task_id);
  if (task) row.task_id = task;
  if (validSid(raw.parent_sid) && raw.parent_sid !== raw.sid) row.parent_sid = raw.parent_sid;
  const reason = line(raw.stop_reason, REASON_CAP);
  if (reason) row.stop_reason = reason;
  for (const key of ["tokens", "tool_uses", "duration_ms", "declined"] as const) {
    const n = count(raw[key]);
    if (n !== undefined) row[key] = n;
  }
  const tool = line(raw.last_tool, NAME_CAP);
  if (tool) row.last_tool = tool;
  if (isLive(s)) {
    const stop = stopState(raw.stop);
    if (stop) {
      row.stop = stop;
      const asked = when(raw.stop_requested_at);
      if (asked) row.stop_requested_at = asked;
    }
    if (typeof raw.stoppable === "boolean") row.stoppable = raw.stoppable;
    const why = line(raw.stop_unavailable, STOP_WHY_CAP);
    if (raw.stoppable === false && why) row.stop_unavailable = why;
  }
  return row;
}

/** A bus record's `data`, or null for anything that is not one. */
export function cleanBus(raw: unknown): SubBus | null {
  if (!isObj(raw) || !validSid(raw.sid)) return null;
  const s = status(raw.status);
  if (!s) return null;
  const op = raw.op === "started" || raw.op === "finished" ? raw.op : "progress";
  const rec: SubBus = {
    sid: raw.sid, op, status: s, depth: Math.min(16, Math.max(1, count(raw.depth) ?? 1)),
    background: raw.background === true, started_at: when(raw.started_at) ?? "", entries: count(raw.entries) ?? 0,
  };
  const task = anId(raw.task_id);
  if (task) rec.task_id = task;
  if (validSid(raw.parent_sid) && raw.parent_sid !== raw.sid) rec.parent_sid = raw.parent_sid;
  const ended = when(raw.ended_at);
  if (ended) rec.ended_at = ended;
  const declined = count(raw.declined);
  if (declined !== undefined) rec.declined = declined;
  const stop = stopState(raw.stop);
  if (stop && isLive(s)) {
    rec.stop = stop;
    const asked = when(raw.stop_requested_at);
    if (asked) rec.stop_requested_at = asked;
  }
  return rec;
}

/** The list route's body, cleaned, oldest first, at most `CHILD_CAP`. */
export function cleanList(raw: unknown): Subagent[] {
  const rows = isObj(raw) && Array.isArray(raw.subagents) ? raw.subagents : [];
  const seen = new Set<string>();
  const out: Subagent[] = [];
  for (const item of rows) {
    const row = cleanRow(item);
    if (row && !seen.has(row.sid)) { seen.add(row.sid); out.push(row); }
  }
  return bound(out);
}

/** At most `CHILD_CAP` rows: the oldest ended one makes room first, else the oldest. */
export function bound(rows: Subagent[]): Subagent[] {
  if (rows.length <= CHILD_CAP) return rows;
  const out = rows.slice();
  while (out.length > CHILD_CAP) {
    const ended = out.findIndex((r) => !isLive(r.status));
    out.splice(ended >= 0 ? ended : 0, 1);
  }
  return out;
}

/**
 * One bus record onto the rows (a new array when anything changed, else the
 * same one). A record is the daemon's state after one change, so it wins —
 * except a stale one, heard late (a reconnect's list read can be newer):
 * fewer `entries` than the row already has, or a live word for an ended row
 * that is not Codex's own `started` (a child given another turn, SV-2; the
 * daemon itself revives nothing else).
 */
export function applyBus(rows: Subagent[], rec: SubBus): Subagent[] {
  const at = rows.findIndex((r) => r.sid === rec.sid);
  if (at < 0) {
    const row: Subagent = {
      sid: rec.sid, status: rec.status, depth: rec.depth, background: rec.background, name: "", kind: "",
      started_at: rec.started_at, entries: rec.entries,
      ...(rec.task_id ? { task_id: rec.task_id } : {}), ...(rec.parent_sid ? { parent_sid: rec.parent_sid } : {}),
      ...(rec.ended_at && !isLive(rec.status) ? { ended_at: rec.ended_at } : {}),
      ...(rec.declined ? { declined: rec.declined } : {}),
      ...(rec.stop ? { stop: rec.stop } : {}),
      ...(rec.stop && rec.stop_requested_at ? { stop_requested_at: rec.stop_requested_at } : {}),
    };
    return bound([...rows, row]);
  }
  const was = rows[at];
  if (rec.entries < was.entries) return rows;
  const revive = isLive(rec.status) && !isLive(was.status);
  if (revive && !(rec.op === "started" && rec.sid.startsWith("cx:"))) return rows;
  const next: Subagent = { ...was, status: rec.status, depth: rec.depth, background: rec.background, entries: rec.entries };
  if (!next.started_at && rec.started_at) next.started_at = rec.started_at;
  if (rec.task_id) next.task_id = rec.task_id;
  if (rec.parent_sid) next.parent_sid = rec.parent_sid;
  if (rec.declined !== undefined) next.declined = rec.declined;
  if (isLive(rec.status)) {
    delete next.ended_at;
    delete next.stop_reason;
  } else if (rec.ended_at) next.ended_at = rec.ended_at;
  // The owner's Stop is the daemon's state after this change: said, or not.
  if (rec.stop && isLive(rec.status)) {
    next.stop = rec.stop;
    if (rec.stop_requested_at) next.stop_requested_at = rec.stop_requested_at;
    else delete next.stop_requested_at;
  } else {
    delete next.stop;
    delete next.stop_requested_at;
  }
  if (!isLive(rec.status)) {
    delete next.stoppable;
    delete next.stop_unavailable;
  }
  const same = (Object.keys(next) as (keyof Subagent)[]).every((k) => next[k] === was[k])
    && Object.keys(next).length === Object.keys(was).length;
  return same ? rows : rows.map((r, i) => (i === at ? next : r));
}

/** A list read, with the bus records heard while it was on its way laid over it (as `toolCalls.fold` does). */
export function foldList(list: Subagent[], replay: readonly SubBus[]): Subagent[] {
  let rows = list;
  for (const rec of replay) rows = applyBus(rows, rec);
  return rows;
}

// ---- what a row says --------------------------------------------------------

export type StatusTone = "live" | "done" | "failed" | "stopped";
/** Always a word; the tone picks its Graphite colour (amber live, green done, red failed, muted stopped). */
export function statusMark(s: SubStatus): { word: string; tone: StatusTone } {
  switch (s) {
    case "running": return { word: "running", tone: "live" };
    case "backgrounded": return { word: "backgrounded", tone: "live" };
    case "done": return { word: "done", tone: "done" };
    case "failed": return { word: "failed", tone: "failed" };
    default: return { word: "stopped", tone: "stopped" };
  }
}

export function tokensText(n: number | undefined): string {
  if (n === undefined) return "";
  if (n < 1000) return `${n} tokens`;
  if (n < 1e6) return `${(n / 1000).toFixed(n < 1e4 ? 1 : 0).replace(/\.0$/, "")}k tokens`;
  return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M tokens`;
}

export const toolsText = (n: number | undefined) => (n === undefined ? "" : `${n} ${n === 1 ? "tool" : "tools"}`);

/** How long it ran: to its end, or to `now` while live; "" with no start. */
export function elapsedOf(row: Pick<Subagent, "started_at" | "ended_at" | "status" | "duration_ms">, now: number): string {
  const start = Date.parse(row.started_at);
  if (!Number.isFinite(start)) return row.duration_ms !== undefined ? elapsed(row.duration_ms) : "";
  const end = isLive(row.status) ? now : Date.parse(row.ended_at ?? "");
  if (!Number.isFinite(end)) return row.duration_ms !== undefined ? elapsed(row.duration_ms) : "";
  return elapsed(end - start);
}

export const displayName = (row: Pick<Subagent, "name"> | undefined) => row?.name || "unnamed";

/**
 * A child's name on an approval card: the daemon's card-label rule
 * (`permissions.subagent_origin`, Claude's `child_label`): whitespace runs to
 * one space, every run of anything outside `[A-Za-z0-9 _./-]` to "_", spaces
 * and "_" trimmed, at most 40 characters ("…" last), "unnamed" when nothing
 * is left. So a name cannot draw the card's own ‹ ›, · or words in another
 * script on the surface that gates approvals (review of #212, MED-1).
 */
export function cardLabel(name: unknown): string {
  if (typeof name !== "string") return "unnamed";
  const spaced = name.split(/\s+/).filter(Boolean).join(" ");
  let label = spaced.replace(/[^A-Za-z0-9 _./-]+/g, "_").split(" ").filter(Boolean).join(" ")
    .replace(/^[ _]+|[ _]+$/g, "");
  if (label.length > NAME_CAP) label = label.slice(0, NAME_CAP - 1).trimEnd() + "…";
  return label || "unnamed";
}

/** The collapsed line for a run of rows: "Sub-agents · 8 (5 running)". */
export function blockTitle(rows: readonly Pick<Subagent, "status">[]): string {
  const live = rows.filter((r) => isLive(r.status)).length;
  return `Sub-agents · ${rows.length}` + (live ? ` (${live} running)` : "");
}

// ---- the owner's Stop (SV-4) --------------------------------------------------

/**
 * What the Stop button's hover help says, by provider (the owner's decisions
 * of 2026-10-15). Codex's states its caveat: `turn/interrupt` on the child's
 * thread ends its turn, but a command it is already running runs on, and the
 * orchestrator's model is not told (SV-0 finding 12).
 */
export const STOP_HELP: Readonly<Record<SubProvider, string>> = {
  claude: "Stop this sub-agent. The orchestrator's turn and its other sub-agents go on.",
  codex: "Stop this sub-agent: it ends its turn. A command it is already running may still finish, "
    + "and the orchestrator's model is not told.",
};
/** Sent, and the CLI has said nothing within the daemon's bound (`subagents.STOP_CONFIRM_S`). */
export const STOP_UNCONFIRMED_HELP = "Stop was sent, but the CLI has not confirmed it; this sub-agent may "
  + "still be running. Between messages Jarvis does not read the CLI, so its answer may come only with "
  + "your next message. Press to send Stop again.";
/** The thread's own Stop on a Claude chat: Claude Code's interrupt also ends its background sub-agents (SV-0 finding 6; kept, owner 2026-10-15). */
export const MAIN_STOP_CLAUDE_HELP = "Stop the running turn. This also stops the chat's background "
  + "sub-agents. Anything queued behind it comes back to this box.";

export type StopView =
  | { kind: "none" }
  | { kind: "cant"; title: string }
  | { kind: "stopping"; title: string }
  | { kind: "button"; label: string; title: string; unconfirmed: boolean };

/**
 * The Stop control a row or pane header draws: none for an ended child or
 * a daemon that does not say; "Can't stop" (its reason as the title) when
 * the CLI cannot stop it alone; "stopping…" from the click (`pending`) until
 * the CLI's own end; else the button — "Stop again" once a Stop went
 * unconfirmed.
 */
export function stopView(row: Subagent, pending: boolean): StopView {
  if (!isLive(row.status)) return { kind: "none" };
  if (pending || row.stop === "stopping") {
    return { kind: "stopping", title: "Stopping: waiting for the CLI to confirm." };
  }
  if (row.stoppable === true) {
    const unconfirmed = row.stop === "unconfirmed";
    return { kind: "button", label: unconfirmed ? "Stop again" : "Stop", unconfirmed,
      title: unconfirmed ? STOP_UNCONFIRMED_HELP : STOP_HELP[providerOf(row.sid)] };
  }
  if (row.stoppable === false) return { kind: "cant", title: row.stop_unavailable || "This sub-agent cannot be stopped by itself." };
  return { kind: "none" };
}

/**
 * Whether a press on a sub-agent's Stop is the owner's own (SV-4): the VEX
 * Stop's rule, the same function — a trusted pointer click with the primary
 * button, or Enter/Space on the focused button; never a script's
 * `el.click()` or a dispatched event.
 */
export const isOwnersPress = isTrustedPress;

// ---- where rows sit ---------------------------------------------------------

/**
 * The rows drawn in one transcript: the parent's (`parent` null) shows the
 * top level — no `parent_sid`, or one the window does not know (its parent
 * fell out of the bound); a child's pane shows those whose parent it is.
 */
export function childrenOf(rows: readonly Subagent[], parent: string | null): Subagent[] {
  if (parent !== null) return rows.filter((r) => r.parent_sid === parent);
  const known = new Set(rows.map((r) => r.sid));
  return rows.filter((r) => !r.parent_sid || !known.has(r.parent_sid));
}

export interface Placeable { callIds: readonly string[]; at?: string }

/**
 * Where each row sits (index into `entries`, -1 before the first): a Claude
 * child right after the entry holding its spawn call (its sid is that
 * call's id); a Codex child, or a Claude one whose call is not in view, by
 * time — after the last entry that started no later than it did; with no
 * times at all, at the end. Rows at one spot keep their (oldest first) order.
 */
export function placeRows(entries: readonly Placeable[], rows: readonly Subagent[]): Map<number, Subagent[]> {
  const byCall = new Map<string, number>();
  entries.forEach((e, i) => { for (const id of e.callIds) byCall.set(id, i); });
  const times = entries.map((e) => Date.parse(e.at ?? ""));
  const out = new Map<number, Subagent[]>();
  for (const row of rows) {
    let at = row.sid.startsWith("cl:") ? byCall.get(row.sid.slice(3)) : undefined;
    if (at === undefined) {
      const start = Date.parse(row.started_at);
      let last = -2, first = -1;
      times.forEach((t, i) => {
        if (!Number.isFinite(t)) return;
        if (first < 0) first = i;
        if (Number.isFinite(start) && t <= start) last = i;
      });
      at = first < 0 || !Number.isFinite(start) ? entries.length - 1 : last >= 0 ? last : first - 1;
    }
    const list = out.get(at);
    if (list) list.push(row); else out.set(at, [row]);
  }
  return out;
}

/** A chat transcript's entries, as `placeRows` reads them. */
export const placeableOf = (m: ChatMessage): Placeable => ({
  callIds: m.calls?.map((c) => c.call_id) ?? [],
  at: m.at ?? m.calls?.[0]?.at,
});

// ---- a child's transcript ---------------------------------------------------

export type PaneItem =
  | { kind: "text"; text: string; at?: string; seq: number }
  | { kind: "tools"; group: ChatMessage; at?: string }
  | { kind: "result"; text: string; status: SubStatus | null; at?: string; seq: number }
  | { kind: "declined"; name: string; at?: string; seq: number };

export interface SubTranscript {
  subagent: Subagent | null;
  items: PaneItem[];
  truncated: boolean;
  /** The orchestrator's brief to the child (Claude's; Codex sends none), at most 4000 characters. */
  brief: string;
}

/**
 * `GET …/subagents/{sid}/transcript`, as the pane draws it: texts, tool
 * calls (consecutive ones as one group, labels and outcomes only — never
 * arguments or results, which the route never serves anyway), the CLI's
 * closing word, and a reviewer's decline by the tool's name.
 */
/** A child's words or brief for the pane: format characters (bidi overrides, zero-widths) out, lines kept. */
const words = (text: string) => Array.from(text.replace(/\p{Cf}/gu, "")).slice(0, TEXT_CAP).join("");

export function cleanTranscript(raw: unknown, sid: string): SubTranscript | null {
  if (!isObj(raw)) return null;
  const items: PaneItem[] = [];
  const messages = Array.isArray(raw.messages) ? raw.messages.slice(-300) : [];
  for (const m of messages) {
    if (!isObj(m)) continue;
    const seq = count(m.seq) ?? 0, at = when(m.at);
    if (m.role === "assistant" && typeof m.text === "string" && m.text.trim()) {
      items.push({ kind: "text", text: words(m.text), at, seq });
    } else if (m.role === "result" && typeof m.text === "string" && m.text.trim()) {
      items.push({ kind: "result", text: words(m.text), status: status(m.status), at, seq });
    } else if (m.role === "declined") {
      items.push({ kind: "declined", name: toolName(line(m.name, NAME_CAP) || "tool"), at, seq });
    } else if (m.role === "tool" && anId(m.call_id)) {
      const call: ToolCall = {
        call_id: m.call_id as string, turn_id: sid, name: toolName(m.name), label: toolLabel(m.label),
        outcome: outcome(m.outcome, false), at: at ?? "",
        ...(when(m.finished_at) ? { finished_at: when(m.finished_at) } : {}),
      };
      const last = items.at(-1);
      if (last?.kind === "tools" && !last.group.calls!.some((c) => c.call_id === call.call_id)) {
        items[items.length - 1] = { ...last, group: { ...last.group, calls: [...last.group.calls!, call] } };
      } else items.push({ kind: "tools", group: { role: "tool", text: "", turn_id: sid, calls: [call] }, at });
    }
  }
  const brief = typeof raw.brief === "string" ? words(raw.brief) : "";
  return { subagent: cleanRow(raw.subagent), items, truncated: raw.truncated === true, brief };
}

/** A pane's items, as `placeRows` reads them (a nested child's spawn is its parent's Agent call). */
export const placeableItem = (item: PaneItem): Placeable => item.kind === "tools"
  ? { callIds: item.group.calls!.map((c) => c.call_id), at: item.group.calls![0]?.at }
  : { callIds: [], at: item.at };

// ---- a pane's reference -----------------------------------------------------

export interface SubagentRef { threadId: string; sid: string }

/** A stored `PaneSpec.subagent`: a thread id and a sid, or null. */
export function parseRef(v: unknown): SubagentRef | null {
  if (!isObj(v)) return null;
  const threadId = v.threadId;
  if (typeof threadId !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(threadId) || !validSid(v.sid)) return null;
  return { threadId, sid: v.sid };
}

export const sameRef = (a: SubagentRef | null | undefined, b: SubagentRef | null | undefined) =>
  !!a && !!b && a.threadId === b.threadId && a.sid === b.sid;
