// Markdown -> speech, a port of `voice.speakable()` (jarvis/voice.py, V6).
//
// The model writes markdown because the HUD renders it; TTS reads the source.
// These strip the syntax and drop what has no spoken form. Deliberately lossy
// in one direction only: never invent words, only remove punctuation the owner
// was never meant to hear. Idempotent: plain prose passes through unchanged.
//
// `prose.ts` builds the streaming filter from the same per-line pieces, so a
// delta-by-delta read and a whole-text read agree. `vectors.json` (generated
// from the Python original) holds this port to it.

// A fence at any indent: one nested under a list item is indented four or more spaces.
export const MD_FENCE = /^\s*(?:```|~~~)/;
export const MD_RULE = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$/;
export const MD_TABLE_RULE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;
const MD_HEADING = /^\s{0,3}#{1,6}\s+/;
const MD_QUOTE = /^\s{0,3}>\s?/;
const MD_BULLET = /^(\s*)(?:[-*+]|\d+[.)])\s+/;
const MD_IMAGE = /!\[([^\]]*)\]\([^)]*\)/g;
const MD_LINK = /\[([^\]]+)\]\([^)]*\)/g;
const MD_AUTOLINK = /<(https?:\/\/[^>\s]+)>/g;
const MD_STRONG = /(\*\*|__|~~)(\S(?:.*?\S)?)\1/gs;
// A `_` flanked by word characters is not emphasis, nor is `2 * 3 * 4`.
const MD_EM = /(?<![*\w])\*(\S(?:[^*]*?\S)?)\*(?!\*)|(?<![_\w])_(\S(?:[^_]*?\S)?)_(?!\w)/g;
const MD_ESCAPE = /\\([\\`*_{}[\]()#+\-.!~>|])/g;
/** Backtick spans. The streaming filter decides what a span is worth; this port keeps its text. */
export const MD_CODE = /`+([^`]+)`+/g;

export type CodeSpan = (content: string) => string;

/**
 * One line of markdown as speech. `first` applies the line-start rules
 * (heading, quote, list marker, table row), which only mean something at the
 * start of a line: a streamed line speaks its later pieces with `first` false.
 */
export function speakableLine(line: string, first = true, code: CodeSpan = (c) => c): string {
  if (first) {
    line = line.replace(MD_HEADING, "");
    line = line.replace(MD_QUOTE, "");
    line = line.replace(MD_BULLET, "$1");
    if (line.trim().startsWith("|") || (line.split("|").length - 1) >= 2) {
      // A table row reads as a list: "name, 12, ready".
      line = line.trim().replace(/^\|+|\|+$/g, "").replace(/\s*\|\s*/g, ", ").replace(/^[, ]+|[, ]+$/g, "");
    }
  }
  line = line.replace(MD_IMAGE, "$1");
  line = line.replace(MD_LINK, "$1");
  line = line.replace(MD_AUTOLINK, "$1");
  line = line.replace(MD_CODE, (_m, content: string) => code(content));
  line = line.replace(MD_STRONG, "$2");
  line = line.replace(MD_EM, (_m, a: string | undefined, b: string | undefined) => a || b || "");
  line = line.replace(MD_ESCAPE, "$1");
  return line.replace(/\s+$/, "");
}

/** Python's `str.splitlines()` separators. */
const LINE_BREAKS = new RegExp(["\\r\\n", "\\n", "\\r", "\\v", "\\f", "\\x1c", "\\x1d", "\\x1e", "\\x85", "\\u2028", "\\u2029"].join("|"));

/** Strip markdown so TTS reads the words, not the syntax; "" when nothing is left. */
export function speakable(text: string): string {
  const lines: string[] = [];
  let inFence = false;
  for (const raw of (text || "").split(LINE_BREAKS)) {
    if (MD_FENCE.test(raw)) {
      inFence = !inFence;
      continue;
    }
    if (inFence || MD_RULE.test(raw)) continue;
    if (raw.includes("|") && MD_TABLE_RULE.test(raw)) continue; // the |---|---| under a table header
    lines.push(speakableLine(raw));
  }
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}
