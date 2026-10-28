// Approval summaries (AS, docs/plans/2026-10-28-approval-summaries.md): the
// words every surface puts on a card beside its buttons, shared by the HUD's
// card (components/Approvals.tsx), the HUD inbox (components/InboxView.tsx),
// the morning review (components/ReviewView.tsx), and the phone's card and
// rows (mobile/card.ts, mobile/views.ts, mobile/review.ts).
//
//   - the **AI summary** is a model's one line: display text only, marked as
//     the model's, never markup (React text or textContent), one line, at most
//     160 characters, cleaned here again whatever the daemon sent;
//   - **Why asked** and **Where** are code's lines (the gate's `[layer]
//     reason`; the daemon's Where as three separate fields, project, branch
//     and folder, each drawn in its own labelled box so no field's text can
//     pass for another's, review of PR #330 MED 1);
//   - the command block is collapsed only while a summary is pending or
//     there; with none (failed, off, a cap, an older daemon) it is expanded.
//     It is always one press away.
//
// Pure: strings and booleans, no markup.

export const SUMMARY_CAP = 160;
/** Each Where field's cap (the daemon's `WHERE_CAPS`); the folder is elided in its middle. */
export const WHERE_CAPS = { project: 60, branch: 80, folder: 120 } as const;
export const PREVIEW_CAP = 200;
/** How long AUTHORIZE and ALWAYS stay off after a card appears or its summary changes (AS-13). */
export const ARM_MS = 600;
/**
 * A summary still "pending" this long after the card was asked is treated as
 * none, and the command shows expanded (review of PR #330, LOW 3): a missed
 * `approval_summary` event must never leave a card collapsed. The daemon's
 * own watchdog is 8 s.
 */
export const STALE_PENDING_MS = 10_000;

export const AI_LABEL = "AI SUMMARY";
export const PENDING_TEXT = "Summarizing…";
export const NONE_TEXT = "No AI summary: the full command is below.";
export const WHY_LABEL = "WHY ASKED";
export const WHERE_LABEL = "WHERE";

// Whitespace, C0/C1 controls and the format characters that reorder or hide
// text (zero-width, bidi overrides, line and paragraph separators).
const BREAKS = /[\s\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]+/g;

/** One capped line of untrusted text. */
export function line(raw: unknown, cap: number): string {
  if (typeof raw !== "string") return "";
  const flat = raw.replace(/`/g, "").replace(BREAKS, " ").trim();
  const chars = Array.from(flat);
  return chars.length <= cap ? flat : chars.slice(0, cap - 1).join("").trimEnd() + "…";
}

export type SummaryState = "pending" | "ready" | "none";

export interface SummaryView {
  /** "none" for failed, off, unknown, or a card that never had one. */
  state: SummaryState;
  text: string;
  /** False when the card carries no summary state at all (no summarizer): no slot is drawn. */
  slot: boolean;
}

type SummaryCard = { summary?: unknown; summary_state?: unknown; asked_at?: unknown } | null | undefined;

/** When a pending summary goes stale (ms since the epoch), or null when it is not pending or the card has no time. */
export function staleAt(card: SummaryCard): number | null {
  if (card?.summary_state !== "pending" || typeof card.asked_at !== "string") return null;
  const asked = Date.parse(card.asked_at);
  return Number.isFinite(asked) ? asked + STALE_PENDING_MS : null;
}

/** A card's (or a deferred record's) summary as the surfaces show it, at `now`. */
export function summaryView(card: SummaryCard, now: number = Date.now()): SummaryView {
  const state = card?.summary_state;
  const text = line(card?.summary, SUMMARY_CAP);
  if (state === "ready" && text) return { state: "ready", text, slot: true };
  if (state === "pending") {
    const stale = staleAt(card);
    // Stale (LOW 3): as if it had failed, so the command opens.
    if (stale !== null && now >= stale) return { state: "none", text: "", slot: true };
    return { state: "pending", text: "", slot: true };
  }
  if (typeof state === "string" && state) return { state: "none", text: "", slot: true };
  return { state: "none", text: "", slot: false };
}

/** A deferred record keeps its summary as a plain field (QH): ready when there is one, else none. */
export function recordSummary(summary: unknown): SummaryView {
  const text = line(summary, SUMMARY_CAP);
  return text ? { state: "ready", text, slot: true } : { state: "none", text: "", slot: false };
}

/** Whether the command block starts collapsed (AS-6): only with a summary pending or there. */
export function startsCollapsed(view: SummaryView): boolean {
  return view.state === "pending" || view.state === "ready";
}

/** The summary slot's words. */
export function summaryWords(view: SummaryView): string {
  return view.state === "ready" ? view.text : view.state === "pending" ? PENDING_TEXT : NONE_TEXT;
}

export interface WhereParts { project: string; branch: string; folder: string }
export const WHERE_KEYS = ["project", "branch", "folder"] as const;
/** Each Where field's own label, drawn apart from its value. */
export const WHERE_PART_LABELS: Record<keyof WhereParts, string> = { project: "project", branch: "branch", folder: "folder" };

// What a surface could read as a separator between fields (the middle dot and
// its lookalikes, bullets, bars): each becomes "?", as the daemon does.
const SEPARATORS = /[|\u00a6\u00b7\u0387\u02d1\u16eb\u2016\u2022\u2023\u2027\u2043\u2219\u22c5\u2758-\u275a\u25cf\u25e6\u26ab\u2981\u2b24\u23fa\u2e31\u30fb\uff5c\uff65]/g;

/** `text` at most `cap` characters, its middle replaced by "…": the head and the tail stay. */
export function elideMiddle(text: string, cap: number): string {
  const chars = Array.from(text);
  if (chars.length <= cap) return text;
  const head = Math.max(1, Math.floor((cap - 1) / 3));
  const tail = cap - 1 - head;
  return chars.slice(0, head).join("") + "…" + chars.slice(chars.length - tail).join("");
}

function wherePart(raw: unknown, cap: number, middle: boolean): string {
  if (typeof raw !== "string") return "";
  const flat = line(raw, 100_000).replace(SEPARATORS, "?").trim();
  return middle ? elideMiddle(flat, cap) : line(flat, cap);
}

/**
 * The code-built Where, as separate fields (the daemon's `where_parts`),
 * cleaned here again; null when there is none. Only an object is read: a
 * one-line string (the shape before review of #330) is not drawn.
 */
export function whereParts(raw: unknown): WhereParts | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const out: WhereParts = {
    project: wherePart(r.project, WHERE_CAPS.project, false),
    branch: wherePart(r.branch, WHERE_CAPS.branch, true),
    folder: wherePart(r.folder, WHERE_CAPS.folder, true),
  };
  return out.project || out.branch || out.folder ? out : null;
}

/** The fields to draw, in order, each with its label: [key, label, value]. */
export function whereFields(where: WhereParts | null): [keyof WhereParts, string, string][] {
  if (!where) return [];
  return WHERE_KEYS.filter((k) => where[k]).map((k) => [k, WHERE_PART_LABELS[k], where[k]]);
}

/** The command's size, for the expand control's label. */
export function commandStats(command: string): { lines: number; chars: number } {
  const text = command.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
  return { lines: text ? text.split("\n").length : 0, chars: Array.from(command).length };
}

/** The collapsed preview: the command's first non-empty line, capped. */
export function previewLine(command: string): string {
  const first = command.split(/\r\n?|\n/).find((l) => l.trim()) ?? "";
  return line(first, PREVIEW_CAP);
}

/** The expand control's label (AS-15): what it hides, said plainly. */
export function toggleLabel(expanded: boolean, command: string, argCount = 0): string {
  if (expanded) return command ? "hide full command" : "hide arguments";
  if (!command) return argCount > 1 ? `show arguments · ${argCount}` : "show arguments";
  const { lines, chars } = commandStats(command);
  return `show full command · ${lines} line${lines === 1 ? "" : "s"} · ${chars.toLocaleString("en-US")} chars`;
}
