// The owner inbox, the HUD's half (inbox A2, and A3's "denied while you were
// away" rows; design
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
  /** Kind prefix + the source's own id: `appr:…`, `q:…`, `tq:…`, `prop:…`, `task:…`, `missed:…`, `dec:…`, `pr:…`, `prx:…`. */
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

/** Design §7's thin headers, by rank. Rank 6 is decisions (B1); 7 (checks) comes with B2. */
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

/** How long before `withdraw_until` (by this window's clock) the hint says "closing". */
export const CLOSING_MS = 15_000;

/**
 * A hint only, never a gate (review of PR #305, MED 1). `withdraw_until` is
 * the daemon's clock and the comparison is the browser's: a daemon 90 s
 * behind (WSL2 after the host slept) would make a gate hide Withdraw while
 * the window is still open, and one ahead would offer it after it closed.
 * So Withdraw is offered for as long as the row is in the daemon's
 * projection (A1 drops it when the window closes), a late press is the
 * daemon's 409, and this only says whether the window looks close to
 * closing: "closing" from `CLOSING_MS` before `withdraw_until` on (past it
 * included), else "open" (an unreadable time too).
 */
export function withdrawHint(row: InboxRow, now: number): "open" | "closing" {
  const until = withdrawUntil(row);
  return until !== null && now >= until - CLOSING_MS ? "closing" : "open";
}

/** The "denied while you were away" kind (inbox A3, IN-3): information only. */
export const MISSED = "missed";

/**
 * Where a row's link goes: its thread when it has one (the pane the item
 * belongs to), else its task. A "denied while you were away" row links to its
 * chat only (design §10, question 3): no "ask again", and no task link.
 */
export function linkOf(row: InboxRow): { kind: "thread" | "task"; id: string } | null {
  if (row.thread_id) return { kind: "thread", id: row.thread_id };
  if (row.kind === MISSED) return null;
  if (row.task_id) return { kind: "task", id: row.task_id };
  return null;
}

/**
 * What a "denied while you were away" row says under its title, chosen by
 * code from its `resolution` (never the served words): why it was denied, and
 * that asking again is its chat's to do. Null for any other kind.
 */
export function missedNote(row: InboxRow): { why: string; next: string } | null {
  if (row.kind !== MISSED) return null;
  const why = row.detail.resolution === "timeout"
    ? "Nobody answered in time, so it was denied."
    : row.detail.resolution === "shutdown"
      ? "Jarvis stopped before you answered, so it was denied."
      : "It was denied without an answer.";
  return { why, next: row.thread_id ? "To try it again, ask from its chat." : "Its chat is not known." };
}

/** A pull request waiting on the owner's merge (inbox B3, IN-4): rank 5, information and a link only. */
export const PR = "pr";
/** "PR list unavailable" (inbox B3): the daemon could not read a repository's open PRs. */
export const PR_UNAVAILABLE = "pr_unavailable";
const GH_NAME = /^[A-Za-z0-9_.-]{1,100}$/;
const PR_URL = /^https:\/\/github\.com\/([A-Za-z0-9_.-]{1,100})\/([A-Za-z0-9_.-]{1,100})\/pull\/([1-9][0-9]{0,9})$/;

export interface PrView {
  repo: string;
  number: number;
  /** The PR's own title: untrusted text, one line. */
  title: string;
  /** Its page on GitHub, only when it is exactly this repository's PR `number`; else null (no link). */
  url: string | null;
}

/**
 * A PR row's words and link, or null for any other row or a detail that is
 * not one. The daemon already links only a URL it validated
 * (`inbox_prs.pr_url`); this checks it again, so a row can never link
 * anywhere but `https://github.com/<repo>/pull/<number>` for its own repo.
 */
export function prOf(row: InboxRow): PrView | null {
  if (row.kind !== PR) return null;
  const d = row.detail;
  const repo = typeof d.repo === "string" ? d.repo : "";
  const parts = repo.split("/");
  const number = d.number;
  if (parts.length !== 2 || !parts.every((p) => GH_NAME.test(p))) return null;
  if (typeof number !== "number" || !Number.isInteger(number) || number < 1) return null;
  let url: string | null = null;
  const m = typeof d.url === "string" ? PR_URL.exec(d.url) : null;
  if (m && `${m[1]}/${m[2]}`.toLowerCase() === repo.toLowerCase() && Number(m[3]) === number) url = d.url as string;
  return { repo, number, title: oneLine(d.title, 300), url };
}

/** A "PR list unavailable" row's repositories and why, as text, in the daemon's words. */
export function prUnavailableOf(row: InboxRow): { repo: string; reason: string }[] | null {
  if (row.kind !== PR_UNAVAILABLE) return null;
  const repos = Array.isArray(row.detail.repos) ? row.detail.repos : [];
  return repos.filter(isObj).map((r) => ({ repo: oneLine(r.repo, 200), reason: oneLine(r.reason, 120) }))
    .filter((r) => r.repo).slice(0, 50);
}

/** A decision an agent posted with `owner_post` (inbox B1, IN-5): rank 6 while open, rank 8 once
 * recorded for a chat that was closed. Answered with one option or the owner's own words. */
export const DECISION = "decision";
const DECISION_ID = /^d[0-9a-f]{12}$/;

export interface DecisionView {
  /** The decision's own id (`d` + 12 hex), for `POST /inbox/decisions/<id>`. */
  id: string;
  question: string;
  options: string[];
  /** The agent's suggested option, preselected and never submitted by itself. */
  suggested: number | null;
  why: string;
  open: boolean;
  /** Once answered: the words the owner gave (the option's text, or what they typed). */
  answer: string;
}

/**
 * A decision row's words, every one untrusted text (`oneLine`/`textBlock`), or
 * null for any other row or a decision whose detail is not one (never guessed
 * at, never answerable).
 */
export function decisionOf(row: InboxRow): DecisionView | null {
  if (row.kind !== DECISION) return null;
  const d = row.detail;
  const id = typeof d.decision_id === "string" && DECISION_ID.test(d.decision_id) ? d.decision_id : null;
  if (!id || !Array.isArray(d.options)) return null;
  const options = d.options.map((o) => oneLine(o, 200));
  if (options.length < 2 || options.length > 6 || options.some((o) => !o)) return null;
  // An option that draws the HUD's own marker is not a decision the HUD shows as answerable.
  if (options.some((o) => o.normalize("NFKC").toLowerCase().includes("(suggested)"))) return null;
  const pick = (v: unknown): number | null =>
    typeof v === "number" && Number.isInteger(v) && v >= 0 && v < options.length ? v : null;
  const answer = isObj(d.answer) ? d.answer : null;
  let said = "";
  if (answer) said = pick(answer.choice) !== null ? options[pick(answer.choice)!] : textBlock(answer.text, 2000);
  return { id, question: textBlock(d.question, 1000), options, suggested: pick(d.default), why: textBlock(d.why, 2000),
           open: !answer, answer: said };
}

/** A decision's answer path (`POST /inbox/decisions/<id>`, inbox B1). */
export function decisionPath(id: string): string {
  return `/inbox/decisions/${encodeURIComponent(id)}`;
}

/** What Answer posts for a decision: the typed words when there are any, else the picked option, else nothing. */
export function decisionBody(picked: number | null, typed: string, d: DecisionView): { text: string } | { choice: number } | null {
  const text = typed.trim();
  if (text) return { text };
  if (picked !== null && picked >= 0 && picked < d.options.length) return { choice: picked };
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
