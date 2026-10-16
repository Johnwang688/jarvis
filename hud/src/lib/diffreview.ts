// The Diff tab's Quick review, in words (docs/plans/2026-10-15-diff-quick-review.md).
// Pure, so the panel's sentences are testable without a daemon. Everything the
// daemon sends here is plain text it has already cleaned (D5); nothing here
// turns any of it into markup.

import type { DiffReview, ReviewFallback, ReviewFinding } from "../types";

export const HARNESS_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", v1: "Jarvis v1" };

/** The hover help on the button (D8). */
export const QUICK_REVIEW_HELP =
  "Quick review: one fast, read-only agent pass over every file in this diff, to catch small bugs. " +
  "It may read the project for context, but it changes nothing and asks you nothing. " +
  "It is not a full review: for that, start a full agent.";

export const CANCEL_HELP = "Cancel this quick review";

export const SEVERITY_ORDER: ReviewFinding["severity"][] = ["bug", "risk", "nit"];

/** "Claude 5h at 82% → Codex: busy …": why each harness before the one used was passed over. */
export function fallbackChain(fallbacks: ReviewFallback[]): string {
  return fallbacks.map((f) => `${f.name || HARNESS_NAMES[f.harness] || f.harness} ${f.reason}`).join(" → ");
}

/** The panel's header line: who reviewed, and why any fallback happened. */
export function headerLine(review: DiffReview): string {
  const chain = fallbackChain(review.fallbacks || []);
  if (review.state === "running") {
    const now = HARNESS_NAMES[review.trying || ""] || "a harness";
    return chain ? `${chain} → reviewing with ${now}…` : `Reviewing with ${now}…`;
  }
  if (review.state === "done") {
    const who = review.label || HARNESS_NAMES[review.harness || ""] || "a reviewer";
    return chain ? `${chain} → ${who}` : who;
  }
  const end = review.state === "cancelled" ? "cancelled" : `no review (${review.error || "failed"})`;
  return chain ? `${chain} → ${end}` : end;
}

/** "2 bugs · 1 risk", or "No findings". */
export function countLine(findings: ReviewFinding[]): string {
  const counts = SEVERITY_ORDER.map((s) => [s, findings.filter((f) => f.severity === s).length] as const)
    .filter(([, n]) => n > 0)
    .map(([s, n]) => `${n} ${s}${n === 1 ? "" : "s"}`);
  return counts.length ? counts.join(" · ") : "No findings";
}

/** What was and was not reviewed, said plainly (D1). Empty when everything was. */
export function scopeLine(review: DiffReview): string {
  const s = review.scope;
  if (!s) return "";
  const skipped = s.skipped || [];
  if (!skipped.length) return "";
  const kb = Math.round((s.cap || 0) / 1000);
  const names = skipped.slice(0, 5).join(", ") + (skipped.length > 5 ? `, and ${skipped.length - 5} more` : "");
  return `Reviewed ${s.files_reviewed} of ${s.files_total} files. Not reviewed (over the ${kb} KB cap): ${names}.`;
}

/** What it cost: dollars when reported (Claude's are an equivalent), else tokens. */
export function costLine(review: DiffReview): string {
  const c = review.cost;
  if (!c) return "";
  if (c.usd > 0) {
    const amount = c.usd < 0.01 ? c.usd.toFixed(4) : c.usd.toFixed(2);
    return review.harness === "claude" ? `≈$${amount} equivalent` : `$${amount}`;
  }
  if (c.tokens > 0) return c.tokens >= 1000 ? `${(c.tokens / 1000).toFixed(1)}k tokens` : `${c.tokens} tokens`;
  return "";
}

/** `file:line`, as shown. */
export function location(f: ReviewFinding): string {
  return f.line ? `${f.file}:${f.line}` : f.file;
}

/** Where a click goes: the file in this diff and its line, or null (not in the diff). */
export function target(f: ReviewFinding, paths: string[]): { file: string; line: number | null } | null {
  if (!f.in_diff || !paths.includes(f.file)) return null;
  return { file: f.file, line: f.line && f.line > 0 ? f.line : null };
}

/** Put a diff editor's new-side cursor on `line` and scroll it to the middle
 *  (a finding's click). A line past the end is the last line. */
export function revealIn(ed: any, line: number | undefined) {
  if (!ed || !line) return;
  const side = ed.getModifiedEditor?.();
  const count = side?.getModel?.()?.getLineCount?.() ?? 0;
  if (!side || count < 1) return;
  const at = Math.min(Math.max(1, Math.floor(line)), count);
  side.revealLineInCenter(at);
  side.setPosition({ lineNumber: at, column: 1 });
}
