// The streaming prose filter (V6, decision V-3): what an agent writes, as text
// worth speaking. Agent output is markdown with code in it; reading code, a
// diff or a table aloud is worse than silence (plan §3.7).
//
// It is fed the deltas of one message as they arrive and returns, for each,
// the newly speakable text. Three properties hold it together:
//
//   - Append-only. What it has returned never changes, which is what lets
//     `split.stableChunks` declare a chunk settled (v1's stability argument).
//   - Split-safe. A delta may end anywhere: inside a fence marker, inside a
//     backtick span, between the two halves of a table row. A line whose kind
//     is not yet knowable is held back until it is.
//   - Early where it is safe. A paragraph is one long line, so waiting for its
//     newline would hold speech until the paragraph ended. A line that is
//     plainly prose releases each *sentence* as it completes.
//
// Dropped: fenced code, unfenced diffs, indented code, `[...]` diagnostics,
// rules and table separators. Reworded: paths read as their basename, URLs as
// "a link", hashes as "a commit", ids as "an id", inline code that is really
// code as nothing, a long table as "a table". Everything else goes through
// `speakableLine`, the port of `voice.speakable()`.

import { MD_RULE, MD_TABLE_RULE, speakableLine } from "./speakable";

/** A table of at most this many rows (header included) reads as a list; longer is "a table". */
export const TABLE_ROWS_READ = 4;

const CODE_PUNCT = /[(){}[\];=<>|&$\\]/;
const URL = /^https?:\/\//i;
const HEX = /^[0-9a-f]{7,40}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LONG_ID = /^[A-Za-z0-9_-]{24,}$/;
const HAS_DIGIT_AND_LETTER = (s: string) => /\d/.test(s) && /[a-f]/i.test(s);

/** The basename of a path, without a trailing `:line[:col]`; null when `s` is not path-shaped. */
export function pathBasename(s: string): string | null {
  const bare = s.replace(/:\d+(?::\d+)?$/, "");
  if (/\s/.test(bare)) return null;
  const rooted = /^(?:~|\.{1,2})?\//.test(bare) || /^[A-Za-z]:[\\/]/.test(bare);
  const segments = bare.split(/[\\/]/).filter(Boolean);
  const last = segments[segments.length - 1] ?? "";
  const extension = /\.[A-Za-z0-9]{1,6}$/.test(last);
  if (!(rooted || (segments.length >= 3) || (segments.length >= 2 && extension))) return null;
  return last || null;
}

/** What one inline code span is worth in speech. "" drops it. */
export function codeSpan(content: string): string {
  const c = content.trim();
  if (!c) return "";
  if (URL.test(c)) return "a link";
  if (UUID.test(c)) return "an id";
  if (HEX.test(c) && HAS_DIGIT_AND_LETTER(c)) return "a commit";
  const path = pathBasename(c);
  if (path) return path;
  if (CODE_PUNCT.test(c) || c.length > 40 || c.split(/\s+/).length >= 3) return "";
  if (LONG_ID.test(c) && /\d/.test(c)) return "an id";
  return c;
}

/** Bare URLs, paths and hashes in plain prose. */
export function tokens(text: string): string {
  return text
    .replace(/https?:\/\/[^\s)>\]]+/gi, (m) => {
      const tail = /[.,;:!?]+$/.exec(m);
      return "a link" + (tail ? tail[0] : "");
    })
    .replace(/(?:[A-Za-z]:[\\/]|~\/|\.{1,2}\/|\/)?[\w.@+-]+(?:[\\/][\w.@+-]+)+\/?/g, (m) => {
      if (/^\d+(?:\/\d+)+$/.test(m)) return m; // dates and fractions
      const base = pathBasename(m);
      return base ?? m;
    })
    .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "an id")
    .replace(/\b[0-9a-f]{7,40}\b/gi, (m) => (HAS_DIGIT_AND_LETTER(m) && /^[0-9a-f]+$/i.test(m) ? "a commit" : m));
}

/** Dropping a span leaves doubled spaces and spaces before punctuation. */
function tidy(text: string): string {
  return text.replace(/[ \t]{2,}/g, " ").replace(/[ \t]+([.,;:!?])/g, "$1");
}

const DIAGNOSTIC = /^\s*\[[^\]\n]*\]\s*$/;
const CUT_OFF = /\s*\[(?:cut off|session not saved|output truncated|truncated)[^\]\n]*\]\s*$/i;
const DIFF_HEADER = /^(?:diff --git |@@ .*@@|(?:---|\+\+\+) (?:a\/|b\/|\/dev\/null)|index [0-9a-f]+\.\.[0-9a-f]+)/;
const DIFF_BODY = /^(?:[+\- ]|\\ No newline|diff --git |@@ |index |new file mode|deleted file mode|similarity index|rename (?:from|to) |old mode|new mode)/;
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+\S/;
const INDENTED = /^(?: {4}|\t)/;
const SENTENCE_END = /[.!?…](?=\s)/g;
const QUOTE_LEAD = /^(?:\s*>\s?)+/;
const LIST_MARKER = /^\s*(?:[-*+]|\d+[.)])\s+/;

/** Is `line` a table row? Two pipes, or a leading pipe (the same test speakable() applies). */
function isTableRow(line: string): boolean {
  return line.trim().startsWith("|") || (line.split("|").length - 1) >= 2;
}

/** Is `s` free of an open span that a cut would split? */
function balanced(s: string): boolean {
  const count = (re: RegExp) => (s.match(re) || []).length;
  return count(/`/g) % 2 === 0 && count(/\[/g) === count(/\]/g) && count(/\*\*/g) % 2 === 0 && count(/__/g) % 2 === 0;
}

export class ProseFilter {
  private pending = "";
  private lineEmitted = false;
  private fence: { ch: string; len: number; quoted: boolean } | null = null;
  private diff = false;
  private indented = false;
  private afterBlank = true;
  private inList = false;
  private table: string[] | null = null;
  private tail = ""; // the last characters returned, to collapse blank lines

  /** Feed one delta; returns the newly speakable text (possibly ""). */
  push(delta: string): string {
    this.pending += delta;
    let out = "";
    let nl: number;
    while ((nl = this.pending.indexOf("\n")) >= 0) {
      const line = this.pending.slice(0, nl).replace(/\r$/, "");
      this.pending = this.pending.slice(nl + 1);
      out += this.line(line, true);
      this.lineEmitted = false;
    }
    out += this.early();
    return this.emit(out);
  }

  /** The line in progress can no longer be finished (a gap in the stream): forget it, keep the modes (a fence stays open). */
  discardLine(): void {
    this.pending = "";
    this.lineEmitted = false;
  }

  /** The message ends: the last line is a whole line, and every mode resets. */
  end(): string {
    let out = "";
    if (this.pending) out += this.line(this.pending.replace(/\r$/, ""), false);
    out += this.flushTable();
    this.pending = "";
    this.lineEmitted = false;
    this.fence = null;
    this.diff = false;
    this.indented = false;
    this.afterBlank = true;
    this.inList = false;
    return this.emit(out);
  }

  private emit(text: string): string {
    if (!text) return "";
    // At most one blank line in a row, and none at the very start.
    let out = "";
    for (const ch of text) {
      if (ch === "\n" && (this.tail === "" || this.tail.endsWith("\n\n"))) continue;
      out += ch;
      this.tail = (this.tail + ch).slice(-2);
    }
    return out;
  }

  /** Release the finished sentences of a line that is plainly prose, before its newline. */
  private early(): string {
    if (this.fence || this.diff || this.table || this.indented || !this.pending) return "";
    const p = this.pending;
    const lead = p.trimStart();
    if (!lead) return "";
    const first = lead[0];
    if ("`~|@+<[".includes(first) || p.includes("|")) return "";
    if (INDENTED.test(p)) return "";
    // A list marker waits for its text; "**bold" and "3 files" are prose.
    if (/^\s*(?:[-*+]|\d+[.)])(?:\s|$)/.test(p) && !LIST_ITEM.test(p)) return "";
    if (first === "#" && !/^\s{0,3}#{1,6}\s+\S/.test(p)) return "";
    if (/^(?:diff |index |---|===)/.test(lead)) return "";
    // A fence marker anywhere in the line: wait for the whole line.
    if (/`{3,}|~{3,}/.test(p)) return "";
    // A leading list number ("1.") is not a sentence end.
    const marker = LIST_MARKER.exec(p)?.[0].length ?? 0;
    let cut = -1;
    for (const m of p.matchAll(SENTENCE_END)) {
      if ((m.index ?? 0) < marker) continue;
      const end = (m.index ?? 0) + 1;
      if (balanced(p.slice(0, end))) cut = end;
    }
    if (cut < 0) return "";
    const head = p.slice(0, cut);
    this.pending = p.slice(cut).replace(/^\s+/, "");
    const text = this.prose(head, !this.lineEmitted);
    this.lineEmitted = true;
    return text ? text + " " : "";
  }

  /** One line of prose, in speech. */
  private prose(line: string, first: boolean): string {
    const spoken = tokens(tidy(speakableLine(line, first, codeSpan)));
    return spoken.replace(CUT_OFF, "").replace(/\s+$/, "");
  }

  private flushTable(): string {
    if (!this.table) return "";
    const rows = this.table;
    this.table = null;
    if (!rows.length) return "";
    if (rows.length > TABLE_ROWS_READ) return "A table.\n";
    return rows.map((r) => this.prose(r, true)).filter(Boolean).join("\n") + "\n";
  }

  /** A complete line (`terminated`: it ended in a newline). */
  private line(raw: string, terminated: boolean): string {
    const nl = terminated ? "\n" : "";
    // Inside fenced code everything is dropped until the closing marker: the
    // same character, at least as long, at any indent (a fence nested under a
    // list item is indented), and inside a blockquote when it opened in one.
    if (this.fence) {
      const body = this.fence.quoted ? raw.replace(QUOTE_LEAD, "") : raw;
      const m = /^\s*(`{3,}|~{3,})\s*$/.exec(body);
      if (m && m[1][0] === this.fence.ch && m[1].length >= this.fence.len) this.fence = null;
      return "";
    }
    // A fence opens at any indent, in a blockquote too.
    const quoted = QUOTE_LEAD.test(raw);
    const bare = quoted ? raw.replace(QUOTE_LEAD, "") : raw;
    const open = /^\s*(`{3,}|~{3,})/.exec(bare);
    if (open) {
      const table = this.flushTable();
      const ch = open[1][0];
      const len = open[1].length;
      // "```code```" on one line is a span that opens nothing.
      const rest = bare.slice(open[0].length);
      const closing = new RegExp((ch === "`" ? "`" : "~") + `{${len},}`).exec(rest);
      if (!closing) this.fence = { ch, len, quoted };
      this.diff = false;
      this.indented = false;
      this.afterBlank = false;
      if (closing) {
        // What follows the span is prose again.
        const after = rest.slice(closing.index + closing[0].length);
        return table + (after.trim() ? this.line(after.replace(/^\s+/, ""), terminated) : "");
      }
      return table;
    }
    // A fence in the middle of a line ("Here is the code: ```python"): the words
    // before it are prose, and the rest is the fence.
    const mid = /`{3,}/.exec(raw);
    if (mid && mid.index > 0) {
      return this.line(raw.slice(0, mid.index), true) + this.line(raw.slice(mid.index), terminated);
    }
    const blank = raw.trim() === "";
    // An unfenced diff runs while its lines look like diff lines.
    if (this.diff) {
      // A context line for an empty source line is a single space: not blank.
      if (raw !== "" && DIFF_BODY.test(raw)) return "";
      this.diff = false;
    }
    if (!blank && DIFF_HEADER.test(raw) && !isTableRow(raw)) {
      const table = this.flushTable();
      this.diff = true;
      this.afterBlank = false;
      return table;
    }
    // Indented code: indented, after a blank line or other code, and not a
    // continuation of a list item.
    if (this.indented && blank) { this.afterBlank = true; return ""; }
    if (this.indented && INDENTED.test(raw)) return "";
    if (!blank && INDENTED.test(raw) && (this.afterBlank || this.indented) && !this.inList && !LIST_ITEM.test(raw)) {
      this.indented = true;
      return this.flushTable();
    }
    if (!blank) this.indented = false;
    // Tables are held whole: a long one is "a table".
    if (!blank && isTableRow(raw)) {
      if (!this.table) this.table = [];
      if (!(raw.includes("|") && MD_TABLE_RULE.test(raw))) this.table.push(raw);
      this.afterBlank = false;
      this.inList = false;
      return "";
    }
    const table = this.flushTable();
    if (blank) {
      this.afterBlank = true;
      return table + nl;
    }
    this.afterBlank = false;
    if (MD_RULE.test(raw)) return table;
    if (DIAGNOSTIC.test(raw)) return table;
    this.inList = LIST_ITEM.test(raw) || (this.inList && INDENTED.test(raw));
    const text = this.prose(raw, !this.lineEmitted);
    this.lineEmitted = false;
    return table + (text ? text + nl : "");
  }
}

/** The whole text through the filter in one push (tests and the `text` remainder). */
export function proseOf(text: string): string {
  const f = new ProseFilter();
  return f.push(text) + f.end();
}
