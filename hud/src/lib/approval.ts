// The sandbox-widening headline on an approval card (2026-10-08).
//
// The server already cleans it (approvals.clean_line: one line, no control
// characters, capped), but the card is the window that gates approvals, so it
// does not trust that: whatever arrives is shown as exactly one capped line.
// A newline in a Codex-supplied path must never draw a second, fake line
// ("Approval required: Read") above the real one.

export const HEADLINE_CAP = 420;

// Whitespace, C0/C1 controls, and the format characters that can reorder or
// hide text (zero-width, bidi overrides, line/paragraph separators).
const BREAKS = /[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]+/g;

export function headlineLine(raw: unknown): string {
  if (typeof raw !== "string") return "";
  const line = raw.replace(/`/g, "").replace(BREAKS, " ").trim();
  return line.length <= HEADLINE_CAP ? line : line.slice(0, HEADLINE_CAP - 1).trimEnd() + "…";
}
