// Joining the transcripts of one message's chunks (voice rework V4, plan §3.5
// and §5, "overlap is deduplicated").
//
// A chunk cut in the middle of speech starts CHUNK_OVERLAP_MS earlier than the
// previous one ended (recorder.ts), so both transcripts contain the words said
// in that half second. The join removes them once: the longest run of words
// that ends the first text and begins the second. The cut can fall inside a
// word, so the first word of the run may be only the tail of the word the
// previous chunk heard whole, and the last only the start of the word the next
// chunk hears whole; both count as a match at those two edges and nowhere else.
//
// Only a boundary the recorder says overlaps is deduplicated. A cut at a pause
// has no overlap, and the same word either side of it ("very" ... "very") is
// then speech, not an echo.

/** The most words the half second of overlap can hold (fast speech is five or six words a second, and the run is matched from both ends). */
export const MAX_OVERLAP_WORDS = 4;

const norm = (w: string) => w.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, "");
const words = (text: string) => text.split(/\s+/).filter(Boolean);

type Match = "exact" | "suffix" | "prefix" | null;

function same(a: string, b: string, edge: "first" | "last" | "only" | "mid"): Match {
  if (a === b) return a !== "" ? "exact" : null;
  // A fragment shorter than four letters could be any short word: not enough to call an echo.
  if (Math.min(a.length, b.length) < 4) return null;
  // `a` is the previous text's word, `b` the next text's at the same place in the run.
  if ((edge === "first" || edge === "only") && a.endsWith(b)) return "suffix";  // the next chunk heard only the end of it
  if ((edge === "last" || edge === "only") && b.startsWith(a)) return "prefix"; // the previous chunk heard only the start of it
  return null;
}

/** The overlap's length in words and whether its last word is only the start of the word the next chunk heard whole. */
function match(before: string, after: string): { k: number; fragment: boolean } {
  const a = words(before).map(norm);
  const b = words(after).map(norm);
  for (let k = Math.min(MAX_OVERLAP_WORDS, a.length, b.length); k >= 1; k--) {
    let ok = true, fragment = false;
    for (let i = 0; i < k && ok; i++) {
      const edge = k === 1 ? "only" : i === 0 ? "first" : i === k - 1 ? "last" : "mid";
      const m = same(a[a.length - k + i], b[i], edge);
      ok = m !== null;
      if (m === "prefix" && i === k - 1) fragment = true;
    }
    if (ok) return { k, fragment };
  }
  return { k: 0, fragment: false };
}

/** How many words end `before` and begin `after`: the overlap to remove. 0 when there is none. */
export function overlapWords(before: string, after: string): number {
  return match(before, after).k;
}

/**
 * What to remove from each side so the overlap is said once: normally the run
 * from the head of `after`; but when `before` ends in only the start of a word
 * ("transcrip") and `after` has it whole ("transcription"), the fragment goes
 * from the end of `before` and `after` stays whole.
 */
export function overlapMerge(before: string, after: string): { dropBefore: number; dropAfter: number } {
  const { k, fragment } = match(before, after);
  return fragment ? { dropBefore: k, dropAfter: 0 } : { dropBefore: 0, dropAfter: k };
}

/** `after` without the words it repeats from the end of `before`. */
export function dropLeadingOverlap(before: string, after: string): string {
  const k = overlapWords(before, after);
  return k ? words(after).slice(k).join(" ") : after.trim();
}

/** `before` without the words that `after`, already placed, repeats from its end. */
export function dropTrailingOverlap(before: string, after: string): string {
  const k = overlapWords(before, after);
  if (!k) return before.trim();
  return words(before).slice(0, -k).join(" ");
}

export interface ChunkText {
  text: string;
  /** This chunk's start overlaps the end of the one before it (the recorder's `overlapMs > 0`). */
  overlaps: boolean;
}

/** The message's text: each chunk's, in order, with the overlapping words once. */
export function joinChunks(parts: ChunkText[]): string {
  const out: string[] = [];
  let prev = "";
  for (const p of parts) {
    const text = p.text.trim();
    if (!text) { continue; }
    let add = words(text);
    if (p.overlaps && prev) {
      const m = overlapMerge(prev, text);
      out.length -= Math.min(m.dropBefore, out.length);
      add = add.slice(m.dropAfter);
    }
    out.push(...add);
    prev = text;
  }
  return out.join(" ");
}
