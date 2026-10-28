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
//     reason`, the daemon's "project · branch · folder");
//   - the command block is collapsed only while a summary is pending or
//     there; with none (failed, off, a cap, an older daemon) it is expanded.
//     It is always one press away.
//
// Pure: strings and booleans, no markup.

export const SUMMARY_CAP = 160;
export const WHERE_CAP = 200;
export const PREVIEW_CAP = 200;
/** How long AUTHORIZE and ALWAYS stay off after a card appears or its summary changes (AS-13). */
export const ARM_MS = 600;

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

/** A card's (or a deferred record's) summary as the surfaces show it. */
export function summaryView(card: { summary?: unknown; summary_state?: unknown } | null | undefined): SummaryView {
  const state = card?.summary_state;
  const text = line(card?.summary, SUMMARY_CAP);
  if (state === "ready" && text) return { state: "ready", text, slot: true };
  if (state === "pending") return { state: "pending", text: "", slot: true };
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

/** The code-built Where line, "" when there is none. */
export function whereText(raw: unknown): string {
  return line(raw, WHERE_CAP);
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
