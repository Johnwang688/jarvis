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
  /** Kind prefix + the source's own id: `appr:…`, `q:…`, `tq:…`, `prop:…`, `task:…`, `missed:…`, `dec:…`, `pr:…`, `prx:…`, `defer:…`. */
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
  /** Inbox B4 (`GET /inbox?all=1`): the owner's snooze or dismissal hiding this row, or null when it shows. */
  hidden: HiddenBy | null;
  /** Inbox B4: the daemon says the owner may snooze or dismiss it (rank 5 to 8, never "PR list unavailable"). */
  markable: boolean;
}

/** What hides a row (inbox B4): a snooze until a time, or a dismissal at one. */
export interface HiddenBy {
  snoozedUntil: string | null;
  dismissedAt: string | null;
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

/** Design §7's thin headers, by rank. Rank 6 is decisions (B1); 7 is live checks (B2). */
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
    // A `hidden` the HUD's own rule (IN-8) says cannot be is dropped: such a row shows.
    hidden: markableRank(rank, raw.kind, id) ? parseHidden(raw.hidden) : null,
    // Both the daemon's word and the HUD's own rule (IN-8): ranks 1 to 4 and
    // "PR list unavailable" never offer a snooze or dismiss, whatever is served.
    markable: raw.markable === true && markableRank(rank, raw.kind, id),
  };
}

/** The HUD's own copy of IN-8: rank 5 to 8, never "PR list unavailable" (by kind or `prx:` id). */
function markableRank(rank: number, kind: unknown, id: string): boolean {
  // A deferred approval (QH) is never markable either: "Leave it" is how it goes.
  return rank >= MARKABLE_MIN_RANK && rank <= MARKABLE_MAX_RANK && kind !== PR_UNAVAILABLE && !id.startsWith("prx:")
    && kind !== DEFERRED && !id.startsWith("defer:");
}

/** Whether a row is hidden: it says so and the HUD's rule allows it to be. A
 * rank 1 to 4 row or "PR list unavailable" is never hidden, whatever it carries. */
export function isHidden(row: InboxRow): boolean {
  return !!row.hidden && markableRank(row.rank, row.kind, row.id);
}

function parseHidden(v: unknown): HiddenBy | null {
  if (!isObj(v)) return null;
  const at = (x: unknown) => (typeof x === "string" && x.length <= 64 && Number.isFinite(Date.parse(x)) ? x : null);
  const snoozedUntil = at(v.snoozed_until);
  const dismissedAt = at(v.dismissed_at);
  // Something says it is hidden: kept hidden, even when its time cannot be read.
  return { snoozedUntil, dismissedAt };
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

/** The sidebar's count and the app badge: what is waiting on the owner (ranks 1 to 5), never a hidden row. */
export function waitingCount(rows: readonly InboxRow[]): number {
  return rows.filter((r) => !isHidden(r) && r.rank >= 1 && r.rank <= COUNTED_MAX_RANK).length;
}

// --- marks: snooze and dismiss (inbox B4, IN-7, IN-8) -----------------------

/** The lowest rank the owner may snooze or dismiss: a live approval, a blocking
 * question, a proposal or a blocked task (1 to 4) is never hidden. */
export const MARKABLE_MIN_RANK = 5;
export const MARKABLE_MAX_RANK = 8;
// "PR list unavailable" (`PR_UNAVAILABLE`, inbox B3, below) is never hidden:
// that would read as "nothing waiting".

/** Whether this row offers Snooze and Dismiss. */
export function canMark(row: InboxRow): boolean {
  return row.markable && !row.hidden && markableRank(row.rank, row.kind, row.id);
}

/** The rows that show (nothing hidden), and the hidden ones, in their order. */
export function shownRows(rows: readonly InboxRow[]): InboxRow[] {
  return rows.filter((r) => !isHidden(r));
}
export function hiddenRows(rows: readonly InboxRow[]): InboxRow[] {
  return rows.filter(isHidden);
}

/** When the earliest snooze among `rows` ends (ms), or null: the store reads
 * again then, a backup to the daemon's `snooze_ended` event. */
export function nextSnoozeEnd(rows: readonly InboxRow[]): number | null {
  const ends = rows.filter(isHidden).map((r) => (r.hidden!.snoozedUntil ? Date.parse(r.hidden!.snoozedUntil) : NaN))
    .filter((ms) => Number.isFinite(ms));
  return ends.length ? Math.min(...ends) : null;
}

export type SnoozeFor = "hour" | "tomorrow" | "week";
export const SNOOZES: readonly { key: SnoozeFor; label: string }[] = [
  { key: "hour", label: "Snooze 1 h" },
  { key: "tomorrow", label: "Until tomorrow" },
  { key: "week", label: "1 week" },
];
/** "Until tomorrow" ends at this local hour of the next day. */
export const TOMORROW_HOUR = 8;

/** When a snooze ends, as the ISO time `POST /inbox/<id>/mark` takes (always with its zone, `Z`). */
export function snoozeUntil(choice: SnoozeFor, now: number): string {
  if (choice === "hour") return new Date(now + 3_600_000).toISOString();
  if (choice === "week") return new Date(now + 7 * 86_400_000).toISOString();
  const d = new Date(now);
  d.setDate(d.getDate() + 1);
  d.setHours(TOMORROW_HOUR, 0, 0, 0);
  return d.toISOString();
}

/** What a mark posts: a snooze, a dismissal, or the Undo of either. */
export type MarkBody = { snooze_until: string } | { dismiss: true } | { clear: true };

/** The mark path: the row id through `encodeURIComponent`, which the daemon decodes once. */
export function markPath(id: string): string {
  return `/inbox/${encodeURIComponent(id)}/mark`;
}

/** What a hidden row says about why it is hidden, in code's words. */
export function hiddenNote(row: InboxRow, now: number = Date.now()): string {
  const h = row.hidden;
  if (!h) return "";
  if (h.snoozedUntil) {
    const ms = Date.parse(h.snoozedUntil);
    const day = new Date(ms).toDateString() === new Date(now).toDateString() ? "" :
      ` ${new Date(ms).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}`;
    return `Snoozed until ${clockOf(ms).slice(0, 5)}${day}`;
  }
  return "Dismissed";
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

/** An approval deferred during quiet hours (QH, jarvis/v2/quiet_defer.py): rank 4 for a day, then rank 8. */
export const DEFERRED = "deferred";
/** What a deferred row posts to `POST /inbox/<id>/answer`. */
export type DeferredAction = "retry" | "leave";

export interface DeferredView {
  /** The tool, as text. */
  tool: string;
  /** The card's public projection, every field untrusted text: the command, why, a leading line, the arguments. */
  command: string;
  reason: string;
  headline: string;
  /** The arguments as JSON text ("" when there are none, or a Browser card's private projection). */
  args: string;
  /** Whether "Approve and retry" is offered: never a Browser (HUD-only) card, never a day late, never a card kept cut. */
  grantable: boolean;
  /** Over a day old: information only, "Leave it" still offered. */
  expired: boolean;
  /** A Browser card (HUD-only): its row offers its chat and "Leave it" only. */
  hudOnly: boolean;
  /** When quiet hours ended (ISO), or null. */
  until: string | null;
  /** How many times the same call was asked while it waited (1 at least). */
  count: number;
  /** An "and N more" row: N asks past the chat's limit, never listed one by one; Leave it only. */
  more: number;
  /** Why Approve and retry is not offered, in the HUD's own words ("" when it is, or not known). */
  whyNot: string;
}

/** The daemon's `why_not` codes, in the HUD's words (anything else: the generic line). */
const WHY_NOT: Record<string, string> = {
  browser: "A Browser action is approved only while its page is live: open its chat to ask again.",
  codex: "A Codex request is approved only while it waits: open its chat to ask again.",
  "owner-only": "This kind of call is asked every time, never approved ahead: open its chat to ask again.",
  "no-chat": "Nothing is waiting to retry it.",
  "too-large": "It is too long to show in full here, so it cannot be approved from here: open its chat.",
  unreadable: "It cannot be approved from here: open its chat to ask again.",
};

/**
 * A deferred row's words (QH), every string untrusted text, or null for any
 * other row. "Approve and retry" is offered only when the daemon says so and
 * the HUD's own rules agree (not a Browser card, not over a day old).
 */
export function deferredOf(row: InboxRow): DeferredView | null {
  if (row.kind !== DEFERRED) return null;
  const d = row.detail;
  const card = isObj(d.card) ? d.card : {};
  let args = "";
  if (isObj(card.args) && Object.keys(card.args).length) {
    try { args = textBlock(JSON.stringify(card.args, null, 2), 4000); } catch { args = ""; }
  }
  const hudOnly = d.hud_only === true || card.hud_only === true;
  const expired = d.expired === true || row.rank !== 4;
  const num = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v > 0 ? v : 0);
  const more = num(d.more);
  const code = typeof d.why_not === "string" ? d.why_not : "";
  const whyNot = hudOnly ? WHY_NOT.browser : code ? (WHY_NOT[code] ?? WHY_NOT.unreadable) : "";
  return {
    tool: oneLine(d.tool ?? card.tool, 120),
    command: textBlock(card.command, 4000),
    reason: textBlock(card.reason, 1000),
    headline: oneLine(card.headline, 400),
    args,
    grantable: d.grantable === true && !hudOnly && !expired && card.cut !== true && !more && !code
      && typeof d.deferral_id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(d.deferral_id),
    expired,
    hudOnly,
    until: typeof d.until === "string" ? d.until : null,
    count: Math.max(1, num(d.count)),
    more,
    whyNot,
  };
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
  // As the daemon's `github_repo`: no `.`, `..` or other name starting with a dot.
  if (parts.length !== 2 || !parts.every((p) => GH_NAME.test(p) && !p.startsWith("."))) return null;
  if (typeof number !== "number" || !Number.isInteger(number) || number < 1) return null;
  let url: string | null = null;
  const m = typeof d.url === "string" ? PR_URL.exec(d.url) : null;
  if (m && !m[1].startsWith(".") && !m[2].startsWith(".") && `${m[1]}/${m[2]}`.toLowerCase() === repo.toLowerCase() && Number(m[3]) === number) url = d.url as string;
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

/** A live check (inbox B2, IN-6): rank 7 while open, rank 8 once every step has the owner's verdict.
 * Steps are text only: nothing in a check runs anything, and "copy" puts a step's words on the clipboard. */
export const CHECK = "check";
const CHECK_ID = /^c[0-9a-f]{12}$/;
export const VERDICTS = ["pass", "fail", "skip"] as const;
export type Verdict = (typeof VERDICTS)[number];
/** The owner's note on one step (the daemon's `checks.NOTE_MAX`). */
export const NOTE_MAX = 1000;

export interface CheckStep {
  do: string;
  expect: string;
  /** The owner's verdict, once marked. */
  verdict: Verdict | null;
  note: string;
  at: string | null;
}

export interface CheckView {
  /** The check's own id (`c` + 12 hex), for `POST /inbox/checks/<id>/steps/<n>`. */
  id: string;
  title: string;
  prs: number[];
  steps: CheckStep[];
  done: boolean;
  /** Whether the daemon's deployed test lets a step be marked now (IN-6). */
  markable: boolean;
  /** The deployed test's line (code-built: PR numbers only), "" when nothing is gated. */
  deploy: string;
  deployedSha: string | null;
  importedFrom: string | null;
}

/**
 * A check row's words, every one untrusted text (`oneLine`/`textBlock`), or
 * null for any other row or a check whose detail is not one (never guessed
 * at, never markable).
 */
export function checkOf(row: InboxRow): CheckView | null {
  if (row.kind !== CHECK) return null;
  const d = row.detail;
  const id = typeof d.check_id === "string" && CHECK_ID.test(d.check_id) ? d.check_id : null;
  if (!id || !Array.isArray(d.steps) || !d.steps.length || d.steps.length > 30) return null;
  const results = new Map<number, Record<string, unknown>>();
  if (Array.isArray(d.results)) {
    for (const r of d.results) {
      if (isObj(r) && typeof r.step === "number" && Number.isInteger(r.step)) results.set(r.step, r);
    }
  }
  const steps: CheckStep[] = [];
  for (let i = 0; i < d.steps.length; i++) {
    const s = d.steps[i];
    if (!isObj(s)) return null;
    const r = results.get(i);
    const verdict = r && (VERDICTS as readonly string[]).includes(r.verdict as string) ? (r.verdict as Verdict) : null;
    steps.push({ do: textBlock(s.do, 2000), expect: textBlock(s.expect, 2000), verdict,
                 note: verdict ? textBlock(r!.note, NOTE_MAX) : "", at: verdict && typeof r!.at === "string" ? r!.at : null });
  }
  const deploy = isObj(d.deploy) ? d.deploy : {};
  const prs = Array.isArray(d.prs) ? d.prs.filter((n): n is number => typeof n === "number" && Number.isInteger(n) && n > 0) : [];
  const done = steps.every((s) => s.verdict !== null);
  return {
    id, title: oneLine(d.title, 200), prs, steps, done,
    markable: !done && deploy.markable === true,
    deploy: oneLine(deploy.text, 300),
    deployedSha: typeof d.deployed_sha === "string" && /^[0-9a-f]{40,64}$/.test(d.deployed_sha) ? d.deployed_sha : null,
    importedFrom: typeof d.imported_from === "string" ? oneLine(d.imported_from, 400) : null,
  };
}

/** A step's mark path (`POST /inbox/checks/<id>/steps/<n>`, inbox B2). */
export function checkStepPath(id: string, step: number): string {
  return `/inbox/checks/${encodeURIComponent(id)}/steps/${Math.trunc(step)}`;
}

/** The runbook import (`POST /inbox/checks/import`, inbox B2, design §6). */
export const CHECK_IMPORT_PATH = "/inbox/checks/import";

/** One draft the import returns, unsaved until the owner saves it. */
export interface CheckDraft {
  index: number;
  title: string;
  prs: number[];
  steps: { do: string; expect: string }[];
  savable: boolean;
  reason: string | null;
}

/** The import's reply, read as text; null when it is not one. */
export function parseDrafts(raw: unknown): { path: string; digest: string; drafts: CheckDraft[] } | null {
  if (!isObj(raw) || typeof raw.path !== "string" || typeof raw.digest !== "string" || !Array.isArray(raw.drafts)) return null;
  const drafts: CheckDraft[] = [];
  for (const d of raw.drafts) {
    if (!isObj(d) || typeof d.index !== "number" || !Array.isArray(d.steps)) continue;
    drafts.push({
      index: d.index, title: oneLine(d.title, 200),
      prs: Array.isArray(d.prs) ? d.prs.filter((n): n is number => typeof n === "number" && Number.isInteger(n)) : [],
      steps: d.steps.filter(isObj).map((s) => ({ do: textBlock(s.do, 2000), expect: textBlock(s.expect, 2000) })),
      savable: d.savable === true, reason: typeof d.reason === "string" ? oneLine(d.reason, 200) : null,
    });
  }
  return { path: oneLine(raw.path, 1024), digest: raw.digest, drafts };
}

/** A line of Markdown text that cannot start a heading, list or quote, or close a code span. */
function mdLine(text: string): string {
  return text.replace(/\r?\n+/g, " ").replace(/([\\`*_[\]<>|#])/g, "\\$1").trim();
}

/**
 * The receipt as Markdown, for the owner to paste into a checkpoint (§5):
 * made here from the stored record the row carries, never from the network.
 * Every agent- or runbook-written string is escaped so it stays one line of text.
 */
export function checkReceipt(view: CheckView, origin: string): string {
  const counts = { pass: 0, fail: 0, skip: 0 } as Record<Verdict, number>;
  for (const s of view.steps) if (s.verdict) counts[s.verdict] += 1;
  const lines = [
    `## Live check ${view.id}: ${mdLine(view.title)}`,
    "",
    `- Result: ${counts.pass} passed, ${counts.fail} failed, ${counts.skip} skipped, ` +
      `${view.steps.length - counts.pass - counts.fail - counts.skip} not marked`,
  ];
  if (view.prs.length) lines.push(`- Pull requests: ${view.prs.map((n) => `#${n}`).join(", ")}`);
  if (view.deployedSha) lines.push(`- Build: ${view.deployedSha.slice(0, 12)}`);
  if (origin) lines.push(`- Posted by: ${mdLine(origin)}`);
  if (view.importedFrom) lines.push(`- Imported from: ${mdLine(view.importedFrom)}`);
  lines.push("- Recorded by the owner in the Jarvis inbox", "");
  view.steps.forEach((s, i) => {
    const mark = s.verdict ? s.verdict.toUpperCase() : "NOT MARKED";
    lines.push(`${i + 1}. **${mark}** ${mdLine(s.do)}`);
    if (s.expect) lines.push(`   - Expect: ${mdLine(s.expect)}`);
    if (s.note) lines.push(`   - Note: ${mdLine(s.note)}`);
    if (s.at) lines.push(`   - At: ${mdLine(s.at)}`);
  });
  return lines.join("\n") + "\n";
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
