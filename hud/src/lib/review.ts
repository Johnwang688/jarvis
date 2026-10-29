// The morning review's rules, shared by the HUD's "Morning review" view
// (components/ReviewView.tsx) and the phone's Review tab (mobile/review.ts);
// the daemon's half is jarvis/v2/review.py, the plan
// docs/plans/2026-10-28-morning-review.md (MR-1 to MR-12).
//
// Pure: the report's shape as the surfaces accept it, the grouping (by
// project, then chat or task, most urgent then oldest; within a chat:
// deferred approvals, questions, decisions, blocked tasks, proposals), the
// summary line and the jump chips, and the selection: **empty on every
// render, a tick only by its own tap, no "select all"**; what Approve
// selected and Leave selected send (exactly the ticked items, with their
// tags), what blocks them (the footer names which item and why), the
// confirmation's list, and what a 409 `changed` unticks.
//
// Every string a row, a place or a project carries is untrusted text: the
// surfaces draw it with textContent / React text only. Every label here is
// code's own words.

import { deferredOf, oneLine, parseRow, type InboxRow } from "./inbox";

/** Overnight; "Waiting now" (first asked after the night ended); still waiting from before it. */
export type Section = "overnight" | "since" | "before";

export interface ReviewItem {
  row: InboxRow;
  section: Section;
  /** The chat or task it belongs to: a `places` key. */
  place: string;
  /** May be ticked for Approve selected, here and now (the daemon's word, checked again here). */
  approve: boolean;
  /** May be ticked for Leave selected (a deferred row, an "and N more" row included). */
  leave: boolean;
  /** Why it may not be approved, as the daemon's code ("" when it may). */
  why: string;
  /** MR-6's binding of the exact card shown, only when `approve`. */
  tag: string | null;
}

export interface ReviewPlace {
  key: string;
  kind: "chat" | "task" | "none";
  id: string | null;
  projectId: string | null;
  /** The chat's title or the task's brief: untrusted text, one line. */
  title: string;
}

export interface ReviewReport {
  quiet: { enabled: boolean; now: boolean; start: string | null; end: string | null };
  summary: { approvals: number; questions: number; decisions: number; blocked: number; proposals: number; live: number };
  live: InboxRow[];
  overnight: ReviewItem[];
  /** Rows first asked after the night ended (review of #329, NIT 7). */
  since: ReviewItem[];
  before: ReviewItem[];
  fyi: number;
  more: number;
  open: number;
  limits: { approve: number; leave: number };
  projects: Map<string, string>;
  places: Map<string, ReviewPlace>;
  /** The phone only: §4.1's last word, display only (the daemon checks again). */
  group: { ok: boolean; why: string } | null;
  /** The phone only (MR-12): the morning push was the last one and something is open. */
  opensHere: boolean;
}

/** The owner's answer 2: at most 10 per Approve selected; 25 per Leave selected. */
export const APPROVE_MAX = 10;
export const LEAVE_MAX = 25;

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): number => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : 0);
const TAG = /^[0-9a-f]{64}$/;
const DEFER_ID = /^defer:[A-Za-z0-9_-]{1,64}$/;
const MORE_ID = /^defer:more\.[A-Za-z0-9_-]{1,200}$/;
const PLACE_KEY = /^(?:chat|task):[^\s]{1,200}$|^none$/;

function parseItem(raw: unknown, section: Section): ReviewItem | null {
  if (!isObj(raw)) return null;
  const row = parseRow(raw.row);
  if (!row) return null;
  const place = typeof raw.place === "string" && PLACE_KEY.test(raw.place) ? raw.place : "none";
  const deferred = row.kind === "deferred" ? deferredOf(row) : null;
  const tag = typeof raw.tag === "string" && TAG.test(raw.tag) ? raw.tag : null;
  // Both the daemon's word and the surface's own rules: only a deferred
  // approval, of a plain id, with a tag, never a Browser card or a day-old one.
  const approve = raw.approve === true && !!deferred && DEFER_ID.test(row.id) && tag !== null
    && !deferred.hudOnly && !deferred.expired && !deferred.more;
  const leave = raw.leave === true && !!deferred && (DEFER_ID.test(row.id) || MORE_ID.test(row.id));
  return { row, section, place, approve, leave, why: approve ? "" : oneLine(raw.why, 40), tag: approve ? tag : null };
}

/** `GET /inbox/review` or `GET /m/api/review` as the surfaces take it; null when it is not one. */
export function parseReview(raw: unknown): ReviewReport | null {
  if (!isObj(raw) || raw.v !== 1 || !Array.isArray(raw.overnight) || !Array.isArray(raw.before)) return null;
  const quiet = isObj(raw.quiet) ? raw.quiet : {};
  const summary = isObj(raw.summary) ? raw.summary : {};
  const seen = new Set<string>();
  const items = (list: unknown[], section: Section) => {
    const out: ReviewItem[] = [];
    for (const one of list) {
      const item = parseItem(one, section);
      if (!item || seen.has(item.row.id)) continue;
      seen.add(item.row.id);
      out.push(item);
    }
    return out;
  };
  const projects = new Map<string, string>();
  for (const p of Array.isArray(raw.projects) ? raw.projects : []) {
    if (isObj(p) && typeof p.id === "string" && p.id.length <= 200) projects.set(p.id, oneLine(p.name, 80) || p.id);
  }
  const places = new Map<string, ReviewPlace>();
  for (const p of Array.isArray(raw.places) ? raw.places : []) {
    if (!isObj(p) || typeof p.key !== "string" || !PLACE_KEY.test(p.key)) continue;
    const kind = p.kind === "chat" || p.kind === "task" ? p.kind : "none";
    places.set(p.key, {
      key: p.key, kind, id: typeof p.id === "string" ? oneLine(p.id, 200) : null,
      projectId: typeof p.project_id === "string" ? p.project_id : null, title: oneLine(p.title, 120),
    });
  }
  const group = isObj(raw.group) ? { ok: raw.group.ok === true, why: oneLine(raw.group.why, 200) } : null;
  return {
    quiet: { enabled: quiet.enabled === true, now: quiet.now === true,
             start: typeof quiet.start === "string" ? quiet.start : null,
             end: typeof quiet.end === "string" ? quiet.end : null },
    summary: { approvals: count(summary.approvals), questions: count(summary.questions),
               decisions: count(summary.decisions), blocked: count(summary.blocked),
               proposals: count(summary.proposals), live: count(summary.live) },
    live: (Array.isArray(raw.live) ? raw.live : []).map((x) => parseRow(isObj(x) ? x.row : null))
      .filter((r): r is InboxRow => !!r && r.kind === "approval"),
    overnight: items(raw.overnight, "overnight"),
    since: items(Array.isArray(raw.since) ? raw.since : [], "since"),
    before: items(raw.before, "before"),
    fyi: count(isObj(raw.fyi) ? raw.fyi.count : 0),
    more: count(raw.more),
    open: count(raw.open),
    // The daemon's limits, never above the owner's own.
    limits: { approve: Math.min(APPROVE_MAX, count(isObj(raw.limits) ? raw.limits.approve : APPROVE_MAX) || APPROVE_MAX),
              leave: Math.min(LEAVE_MAX, count(isObj(raw.limits) ? raw.limits.leave : LEAVE_MAX) || LEAVE_MAX) },
    projects, places, group,
    opensHere: raw.opens_here === true,
  };
}

/** The parts, in the order shown. */
export const SECTIONS: readonly Section[] = ["overnight", "since", "before"];

/** Every item, in the order shown (overnight first). */
export function allItems(report: ReviewReport): ReviewItem[] {
  return [...report.overnight, ...report.since, ...report.before];
}

// -- grouping (MR-2) ---------------------------------------------------------

/** Within a chat: deferred approvals, questions, decisions, blocked tasks, proposals. */
export const KIND_ORDER: Record<string, number> = { deferred: 0, question: 1, task_question: 1, decision: 2, task: 3, proposal: 4 };

export interface PlaceGroup {
  place: ReviewPlace;
  /** The DOM id its jump chip scrolls to. */
  anchor: string;
  items: ReviewItem[];
}

export interface ProjectGroup {
  projectId: string | null;
  /** The project's name (the owner's, untrusted text), or "Other" (code's). */
  name: string;
  places: PlaceGroup[];
}

const stamp = (row: InboxRow) => {
  const ms = row.asked_at ? Date.parse(row.asked_at) : NaN;
  return Number.isFinite(ms) ? ms : Infinity;
};

/** A place's DOM anchor: code-built from the section and the key's safe characters. */
export function anchorOf(section: Section, key: string): string {
  return `review-${section}-${key.replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

function placeOf(report: ReviewReport, key: string): ReviewPlace {
  return report.places.get(key) ?? { key, kind: key.startsWith("task:") ? "task" : key.startsWith("chat:") ? "chat" : "none",
    id: key.includes(":") ? key.slice(key.indexOf(":") + 1) : null, projectId: null, title: "" };
}

/** One section's items, by project, then chat or task: the most urgent first, then the oldest. */
export function groupItems(report: ReviewReport, items: readonly ReviewItem[]): ProjectGroup[] {
  const byPlace = new Map<string, ReviewItem[]>();
  for (const item of items) {
    const list = byPlace.get(item.place);
    if (list) list.push(item); else byPlace.set(item.place, [item]);
  }
  const places = Array.from(byPlace.entries()).map(([key, mine]) => ({
    place: placeOf(report, key),
    anchor: anchorOf(items[0]?.section ?? "overnight", key),
    items: mine.slice().sort((a, b) => (KIND_ORDER[a.row.kind] ?? 9) - (KIND_ORDER[b.row.kind] ?? 9)
      || a.row.rank - b.row.rank || stamp(a.row) - stamp(b.row) || (a.row.id < b.row.id ? -1 : 1)),
    urgent: Math.min(...mine.map((i) => i.row.rank)),
    oldest: Math.min(...mine.map((i) => stamp(i.row))),
  }));
  places.sort((a, b) => a.urgent - b.urgent || a.oldest - b.oldest || (a.place.key < b.place.key ? -1 : 1));
  const groups: ProjectGroup[] = [];
  for (const p of places) {
    const pid = p.place.projectId;
    let group = groups.find((g) => g.projectId === pid);
    if (!group) {
      group = { projectId: pid, name: pid ? (report.projects.get(pid) ?? pid) : "Other", places: [] };
      groups.push(group);
    }
    group.places.push({ place: p.place, anchor: p.anchor, items: p.items });
  }
  return groups;
}

/** A place's heading, in code's words around the untrusted title. */
export function placeLabel(place: ReviewPlace): { kind: string; title: string } {
  if (place.kind === "task") return { kind: "Task", title: place.title || place.id || "" };
  if (place.kind === "chat") return { kind: "Chat", title: place.title || place.id || "" };
  return { kind: "No chat", title: "" };
}

export interface Chip { key: string; anchor: string; kind: string; title: string }

/** One jump chip per chat or task, in the order shown, to its first anchor. */
export function chipsOf(report: ReviewReport): Chip[] {
  const out: Chip[] = [];
  const seen = new Set<string>();
  for (const section of SECTIONS) {
    for (const group of groupItems(report, report[section])) {
      for (const p of group.places) {
        if (seen.has(p.place.key)) continue;
        seen.add(p.place.key);
        out.push({ key: p.place.key, anchor: p.anchor, ...placeLabel(p.place) });
      }
    }
  }
  return out;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The summary line: counts by kind, code's words only. */
export function summaryLine(report: ReviewReport): string {
  const s = report.summary;
  const parts = [
    s.live ? plural(s.live, "live approval", "live approvals") : "",
    s.approvals ? plural(s.approvals, "deferred approval", "deferred approvals") : "",
    s.questions ? plural(s.questions, "question", "questions") : "",
    s.decisions ? plural(s.decisions, "decision", "decisions") : "",
    s.blocked ? plural(s.blocked, "blocked task", "blocked tasks") : "",
    s.proposals ? plural(s.proposals, "proposal", "proposals") : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Nothing waited overnight.";
}

/** What a part is called (MR-1), code's words. */
export function sectionTitle(report: ReviewReport, section: Section): string {
  if (section === "overnight") return "Overnight";
  if (section === "since") return "Waiting now";
  return report.quiet.enabled ? "Still waiting from before" : "Quiet hours are off";
}

// -- the selection (MR-4) ---------------------------------------------------------

/** A fresh selection: nothing ticked. There is no "select all". */
export function emptySelection(): Set<string> {
  return new Set<string>();
}

/** One item's own tap: ticked if it was not, unticked if it was; never another item. */
export function toggled(selection: ReadonlySet<string>, id: string, item: ReviewItem | undefined): Set<string> {
  const next = new Set(selection);
  if (next.has(id)) next.delete(id);
  else if (item && (item.approve || item.leave)) next.add(id);
  return next;
}

/** The ticked items still in the report, in display order; ids gone from it are dropped. */
export function selectedItems(report: ReviewReport, selection: ReadonlySet<string>): ReviewItem[] {
  return allItems(report).filter((i) => selection.has(i.row.id));
}

/** The selection kept to what the report still holds (a re-read unticks what left). */
export function keptSelection(report: ReviewReport, selection: ReadonlySet<string>): Set<string> {
  return new Set(selectedItems(report, selection).map((i) => i.row.id));
}

/** The daemon's `why` codes (and a 409's reasons), in the surfaces' words. */
export const WHY_WORDS: Record<string, string> = {
  more: "too many asks to list one by one: leave them, or ask again from the chat",
  gone: "already answered or gone",
  answered: "already answered or gone",
  "not-grantable": "it cannot be approved from here",
  "hud-only": "a Browser action: open its chat at the desk",
  expired: "over a day old: it can only be left",
  "task-running": "its task is running, not blocked: open the task",
  "chat-closed": "its chat is closed or archived: it can only be left",
  "no-chat": "nothing is waiting to retry it",
  withheld: "this card has withheld text: answer it at the desk",
  changed: "it changed since you read it: look again",
};

export function whyWords(code: string, row?: InboxRow): string {
  if (code === "not-grantable" && row) {
    const d = deferredOf(row);
    if (d?.whyNot) return d.whyNot;
  }
  return WHY_WORDS[code] ?? "it cannot be approved from here";
}

/** An item's short name for the footer and the confirmation: the tool and where, code-built around the tool's name. */
export function itemLabel(report: ReviewReport, item: ReviewItem): string {
  const d = deferredOf(item.row);
  const tool = d?.tool || oneLine(item.row.kind, 40);
  const where = placeLabel(placeOf(report, item.place));
  return `${tool} (${where.kind.toLowerCase()}${where.title ? " " + where.title : ""})`;
}

export interface Blocked {
  /** Whether Approve selected may be pressed. */
  approve: boolean;
  leave: boolean;
  /** Each line names what blocks Approve selected (code's words around untrusted names). */
  approveWhy: string[];
  leaveWhy: string[];
}

/** What the footer allows for this selection. `limited`: the phone's §4.1 says no group approval (the owner's answer 3). */
export function footerState(report: ReviewReport, selection: ReadonlySet<string>,
                            opts: { limited?: string | null } = {}): Blocked {
  const items = selectedItems(report, selection);
  const approveWhy: string[] = [];
  const leaveWhy: string[] = [];
  if (!items.length) {
    approveWhy.push("Select a deferred approval first.");
    leaveWhy.push("Select a deferred approval first.");
  }
  if (opts.limited) approveWhy.push(`${opts.limited}: approve one at a time.`);
  if (items.length > report.limits.approve) approveWhy.push(`At most ${report.limits.approve} at once.`);
  if (items.length > report.limits.leave) leaveWhy.push(`At most ${report.limits.leave} at once.`);
  for (const item of items) {
    if (!item.approve) approveWhy.push(`${itemLabel(report, item)}: ${whyWords(item.why, item.row)}.`);
    if (!item.leave) leaveWhy.push(`${itemLabel(report, item)}: only a deferred approval can be left here.`);
  }
  return { approve: approveWhy.length === 0, leave: leaveWhy.length === 0, approveWhy, leaveWhy };
}

/** What Approve selected posts: exactly the ticked items, each with its tag, in display order. */
export function approveBody(report: ReviewReport, selection: ReadonlySet<string>): { items: { id: string; tag: string }[] } | null {
  const items = selectedItems(report, selection);
  if (!items.length || items.length > report.limits.approve || items.some((i) => !i.approve || !i.tag)) return null;
  return { items: items.map((i) => ({ id: i.row.id, tag: i.tag! })) };
}

/** What Leave selected posts: exactly the ticked items' ids. */
export function leaveBody(report: ReviewReport, selection: ReadonlySet<string>): { items: { id: string }[] } | null {
  const items = selectedItems(report, selection);
  if (!items.length || items.length > report.limits.leave || items.some((i) => !i.leave)) return null;
  return { items: items.map((i) => ({ id: i.row.id })) };
}

export interface ConfirmLine { id: string; where: string; tool: string; firstLine: string;
  /** AS, display only: the record's AI summary, "" when it has none. Never in the tag or the subject. */
  summary: string }

/** The confirmation's list: each selected item's chat, tool and the command's first line (MR-10). */
export function confirmationList(report: ReviewReport, selection: ReadonlySet<string>): ConfirmLine[] {
  return selectedItems(report, selection).map((item) => {
    const d = deferredOf(item.row);
    const where = placeLabel(placeOf(report, item.place));
    const command = d?.command || d?.args || "";
    return { id: item.row.id, where: `${where.kind}${where.title ? ": " + where.title : ""}`,
             tool: d?.tool || "", firstLine: oneLine(command.split("\n")[0] ?? "", 200), summary: d?.summary || "" };
  });
}

/** A 409's `changed` (`[{id, reason}]`): those items unticked, each with why. */
export function afterChanged(selection: ReadonlySet<string>, changed: unknown): { selection: Set<string>; notes: Map<string, string> } {
  const next = new Set(selection);
  const notes = new Map<string, string>();
  for (const c of Array.isArray(changed) ? changed : []) {
    if (!isObj(c) || typeof c.id !== "string") continue;
    next.delete(c.id);
    notes.set(c.id, `Unticked: ${WHY_WORDS[typeof c.reason === "string" ? c.reason : ""] ?? "it changed since you read it"}.`);
  }
  return { selection: next, notes };
}

/** What a re-read did to the set under an open confirmation (review of #329, MED 1):
 * the confirmed ids no longer in the report, no longer approvable, or with
 * another tag. Any of them closes the confirmation: nothing is approved on
 * a set other than the one the owner confirmed. */
export function confirmedChanged(report: ReviewReport, confirmed: readonly { id: string; tag: string }[]): string[] {
  const now = new Map(allItems(report).map((i) => [i.row.id, i]));
  return confirmed.filter((c) => {
    const item = now.get(c.id);
    return !item || !item.approve || item.tag !== c.tag;
  }).map((c) => c.id);
}

/** The words a row unticked by a re-read under the confirmation carries. */
export const UNTICKED_GONE = "Unticked: answered elsewhere or gone.";
