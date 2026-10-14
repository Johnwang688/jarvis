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

/** The most words the half second of overlap can hold, with room for a fast talker. */
export const MAX_OVERLAP_WORDS = 8;

const norm = (w: string) => w.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, "");
const words = (text: string) => text.split(/\s+/).filter(Boolean);

function same(a: string, b: string, edge: "first" | "last" | "only" | "mid"): boolean {
  if (a === b) return a !== "";
  // A fragment shorter than four letters could be any short word: not enough to call an echo.
  if (Math.min(a.length, b.length) < 4) return false;
  // `a` is the previous text's word, `b` the next text's at the same place in the run.
  if ((edge === "first" || edge === "only") && a.endsWith(b)) return true;   // the next chunk heard only the end of it
  if ((edge === "last" || edge === "only") && b.startsWith(a)) return true;  // the previous chunk heard only the start of it
  return false;
}

/** How many words end `before` and begin `after`: the overlap to remove. 0 when there is none. */
export function overlapWords(before: string, after: string): number {
  const a = words(before).map(norm);
  const b = words(after).map(norm);
  for (let k = Math.min(MAX_OVERLAP_WORDS, a.length, b.length); k >= 1; k--) {
    let ok = true;
    for (let i = 0; i < k && ok; i++) {
      const edge = k === 1 ? "only" : i === 0 ? "first" : i === k - 1 ? "last" : "mid";
      ok = same(a[a.length - k + i], b[i], edge);
    }
    if (ok) return k;
  }
  return 0;
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
    out.push(p.overlaps && prev ? dropLeadingOverlap(prev, text) : text);
    prev = text;
  }
  return out.filter(Boolean).join(" ");
}
