// The owner inbox, the HUD's half (inbox A2; design
// docs/plans/2026-10-24-owner-inbox-design.md §7, the daemon's half in
// jarvis/v2/inbox.py and docs/notes/control-plane.md "The owner inbox routes").
//
// Pure rules only: the row's shape as the HUD accepts it, the four groups,
// the sidebar's count, a proposal's Withdraw window, where a row links, and
// the app badge. The store is state/inbox.ts, the view components/InboxView.tsx.
//
// **Every string a row carries is untrusted text.** `title` and `blocks` are
// built by the daemon from ids and states, but `origin` carries a
// model-chosen task name and every `detail` field (a question, a brief) is a
// model's words. All of it is drawn as React text (`textContent`), never
// HTML or Markdown, after `oneLine`/`textBlock` take out format characters
// (bidi overrides, zero-widths) that could make one line read as another.

export interface InboxRow {
  /** Kind prefix + the source's own id: `appr:…`, `q:…`, `tq:…`, `prop:…`, `task:…`. */
  id: string;
  kind: string;
  /** IN-4's band, 1 to 8. */
  rank: number;
  title: string;
  blocks: string;
  project_id: string | null;
  thread_id: string | null;
  task_id: string | null;
  asked_at: string | null;
  expires_at: string | null;
  origin: string;
  detail: Record<string, unknown>;
}

/** The kinds the HUD answers from the inbox, and the body each takes. */
export const ANSWERS_TEXT: ReadonlySet<string> = new Set(["question", "task_question"]);
export const ANSWERS_WITHDRAW = "proposal";

export interface InboxGroup {
  key: "now" | "you" | "time" | "fyi";
  label: string;
  min: number;
  max: number;
}

/** Design §7's thin headers, by rank. Ranks 6 and 7 come with later packages. */
export const GROUPS: readonly InboxGroup[] = [
  { key: "now", label: "Waiting now", min: 1, max: 2 },
  { key: "you", label: "Waiting on you", min: 3, max: 5 },
  { key: "time", label: "When you have time", min: 6, max: 7 },
  { key: "fyi", label: "For your information", min: 8, max: 8 },
];

/** The sidebar's count and the app badge: ranks 1 to 5 (design §7). */
export const COUNTED_MAX_RANK = 5;
/** Amber (pending) is for ranks 1 and 2 only. */
export const PENDING_MAX_RANK = 2;

const ID_MAX = 400;
const LINE_MAX = 400;
const TEXT_MAX = 4000;

/** One line of untrusted text: format characters out, every run of white space or control characters one space. */
export function oneLine(v: unknown, cap = LINE_MAX): string {
  if (typeof v !== "string") return "";
  const flat = v.replace(/\p{Cf}/gu, "").replace(/[\s\p{Cc}\p{Zl}\p{Zp}]+/gu, " ").trim();
  const chars = Array.from(flat);
  return chars.length > cap ? chars.slice(0, cap).join("") + "…" : flat;
}

/** A block of untrusted text: format characters and controls out, its lines kept. */
export function textBlock(v: unknown, cap = TEXT_MAX): string {
  if (typeof v !== "string") return "";
  const kept = v.replace(/\r\n?/g, "\n").replace(/\p{Cf}/gu, "").replace(/[^\S\n]+/gu, " ")
    .replace(/[\p{Cc}\p{Zl}\p{Zp}]/gu, (c) => (c === "\n" ? c : " ")).trim();
  const chars = Array.from(kept);
  return chars.length > cap ? chars.slice(0, cap).join("") + "…" : kept;
}

const idOrNull = (v: unknown): string | null => (typeof v === "string" && v && v.length <= ID_MAX ? v : null);
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** One row as served, or null for anything that is not one (dropped, never guessed at). */
export function parseRow(raw: unknown): InboxRow | null {
  if (!isObj(raw)) return null;
  const id = idOrNull(raw.id);
  const rank = raw.rank;
  if (!id || !/^[a-z]+:./.test(id) || typeof raw.kind !== "string" || !raw.kind) return null;
  if (typeof rank !== "number" || !Number.isInteger(rank) || rank < 1 || rank > 8) return null;
  return {
    id, kind: raw.kind, rank,
    title: oneLine(raw.title, 160), blocks: oneLine(raw.blocks, 160),
    project_id: idOrNull(raw.project_id), thread_id: idOrNull(raw.thread_id), task_id: idOrNull(raw.task_id),
    asked_at: typeof raw.asked_at === "string" ? raw.asked_at : null,
    expires_at: typeof raw.expires_at === "string" ? raw.expires_at : null,
    origin: oneLine(raw.origin, 160),
    detail: isObj(raw.detail) ? raw.detail : {},
  };
}

/** `GET /inbox`'s body, in rank order (stable: the daemon's order within a rank, which is oldest first). Null when it is not a list. */
export function parseInbox(raw: unknown): InboxRow[] | null {
  if (!Array.isArray(raw)) return null;
  const seen = new Set<string>();
  const rows: InboxRow[] = [];
  for (const item of raw) {
    const row = parseRow(item);
    if (!row || seen.has(row.id)) continue;
    seen.add(row.id);
    rows.push(row);
  }
  return rows.map((row, i) => ({ row, i })).sort((a, b) => a.row.rank - b.row.rank || a.i - b.i).map(({ row }) => row);
}

/** The groups that have rows, in order, each with its rows in rank order. */
export function groupRows(rows: readonly InboxRow[]): { group: InboxGroup; rows: InboxRow[] }[] {
  const out: { group: InboxGroup; rows: InboxRow[] }[] = [];
  for (const group of GROUPS) {
    const mine = rows.filter((r) => r.rank >= group.min && r.rank <= group.max);
    if (mine.length) out.push({ group, rows: mine });
  }
  return out;
}

/** The sidebar's count and the app badge: what is waiting on the owner (ranks 1 to 5). */
export function waitingCount(rows: readonly InboxRow[]): number {
  return rows.filter((r) => r.rank >= 1 && r.rank <= COUNTED_MAX_RANK).length;
}

/** Whether anything pending (ranks 1 and 2) is waiting: the count reads amber. */
export function anyPending(rows: readonly InboxRow[]): boolean {
  return rows.some((r) => r.rank >= 1 && r.rank <= PENDING_MAX_RANK);
}

/** A proposal's grace window's end in ms, or null when it has none the HUD can read. */
export function withdrawUntil(row: InboxRow): number | null {
  if (row.kind !== ANSWERS_WITHDRAW) return null;
  const at = row.detail.withdraw_until;
  const ms = typeof at === "string" ? Date.parse(at) : NaN;
  return Number.isFinite(ms) ? ms : null;
}

/** Withdraw is offered only while the window is open; once it passes the button goes. */
export function withdrawOpen(row: InboxRow, now: number): boolean {
  const until = withdrawUntil(row);
  return until !== null && now < until;
}

/** Where a row's link goes: its thread when it has one (the pane the item belongs to), else its task. */
export function linkOf(row: InboxRow): { kind: "thread" | "task"; id: string } | null {
  if (row.thread_id) return { kind: "thread", id: row.thread_id };
  if (row.task_id) return { kind: "task", id: row.task_id };
  return null;
}

/** The approval request id an `appr:` row is about (its `detail` is `ApprovalRequest.to_json()`). */
export function approvalReq(row: InboxRow): string | null {
  if (row.kind !== "approval") return null;
  return idOrNull(row.detail.req_id);
}

/** A question row's text and options, as text. */
export function questionOf(row: InboxRow): { text: string; options: string[] } {
  const options = Array.isArray(row.detail.options)
    ? row.detail.options.filter((o): o is string => typeof o === "string").map((o) => oneLine(o, 200)).filter(Boolean)
    : [];
  return { text: textBlock(row.detail.text), options };
}

/** A row's brief (proposals and task rows), as text. */
export function briefOf(row: InboxRow): string {
  return textBlock(row.detail.brief, 400);
}

/** "15:04:11" in the window's local time, or "" for a time the HUD cannot read. */
export function clockOf(at: string | number | null | undefined): string {
  const ms = typeof at === "number" ? at : typeof at === "string" ? Date.parse(at) : NaN;
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

/** The answer path: the row id through `encodeURIComponent`, which the daemon decodes once. */
export function answerPath(id: string): string {
  return `/inbox/${encodeURIComponent(id)}/answer`;
}

type BadgeNavigator = {
  setAppBadge?: (n?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

/**
 * The installed app's icon badge: the count, cleared at 0. Feature-detected
 * (a tab, or a browser without the Badging API, does nothing) and every error
 * swallowed: the badge is a count only and never worth a failure. It never
 * touches `document.title` (the `J.A.R.V.I.S.` invariant).
 */
export function setBadge(count: number, nav: BadgeNavigator | undefined =
  typeof navigator === "undefined" ? undefined : (navigator as unknown as BadgeNavigator)): void {
  if (!nav) return;
  try {
    if (count > 0) {
      if (typeof nav.setAppBadge !== "function") return;
      void Promise.resolve(nav.setAppBadge(count)).catch(() => {});
    } else if (typeof nav.clearAppBadge === "function") {
      void Promise.resolve(nav.clearAppBadge()).catch(() => {});
    } else if (typeof nav.setAppBadge === "function") {
      void Promise.resolve(nav.setAppBadge(0)).catch(() => {});
    }
  } catch {
    /* the badge is best-effort */
  }
}
