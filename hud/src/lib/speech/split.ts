// The sentence splitter, a port of v1's `_sentences`, `_stable_chunks`,
// `_word_cut` and `_merges` (jarvis/face/server.py, V6). v1 spoke after the
// turn; this speaks while the model writes (decision V-3), but the chunking
// rules are the ones that were tuned against local Kokoro, so they are copied
// rather than reinvented: the short opener (time to first speech), the
// binding words that may not end a chunk, and the 25-character merge rule.
//
// `vectors.json` is generated from the Python originals by
// `tests/face/speech_vectors_check.py --write`, and `split.test.ts` holds this
// port to it. Change a rule in both places or the check names the sentence.
//
// One honest difference: Python counts code points and JS counts UTF-16
// units, so a reply full of astral characters (emoji) can measure a few
// characters differently. The limits are soft (a chunk length), so it costs a
// slightly different cut and never a wrong word.

/** Words that cannot end a spoken phrase (articles, auxiliaries, prepositions, ...). */
export const BINDING: ReadonlySet<string> = new Set(`
a an the this that these those my your his her its our their
every each some any no all both another such
is are was were be been being am
do does did have has had will would shall should can could may might must
of to in on at by for with from into onto over under about across through
and or but nor so yet as than if while when where because although though
very quite rather entirely completely fairly really extremely
`.split(/\s+/).filter(Boolean));

/** Python's `str.rfind(sub, 0, end)`: the last index whose match ends at or before `end`. */
function rfind(s: string, sub: string, end: number): number {
  const from = end - sub.length;
  return from < 0 ? -1 : s.lastIndexOf(sub, from);
}

/** Python's `str.strip(chars)`. */
function stripChars(s: string, chars: string): string {
  let a = 0;
  let b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
}

/** Python's `str.strip()`. */
const strip = (s: string) => s.trim();

/** Last space before `limit` that does not strand a binding word. */
export function wordCut(head: string, limit: number, floor = 20): number {
  const space = rfind(head, " ", limit);
  if (space < floor) return 0;
  let at = space;
  while (at >= floor) {
    const word = stripChars(head.slice(0, at).split(" ").pop()!.toLowerCase(), ",;:.—-");
    if (!BINDING.has(word)) return at;
    const next = rfind(head, " ", at);
    if (next < floor) break;
    at = next;
  }
  return space;
}

/** Should `part` be glued onto the chunk before it? (See v1's `_merges`.) */
function merges(out: string[], part: string, maxLen: number, firstMin: number): boolean {
  const last = out[out.length - 1];
  if (last.length + part.length + 1 >= maxLen) return false;
  if (part.length < 25) return true;
  if (last.length < 25) return !(out.length === 1 && last.length >= firstMin);
  return false;
}

/** Split a reply into speakable chunks: sentences, tiny ones merged. */
export function sentences(text: string, maxLen = 300, firstMax = 90, firstMin = 12): string[] {
  const parts = strip(text).split(/(?<=[.!?…])\s+|\n+/);
  const out: string[] = [];
  for (let part of parts) {
    part = strip(part);
    if (!part) continue;
    while (part.length > maxLen) {
      let cut = part.lastIndexOf(" ", maxLen - 1);
      cut = cut > 0 ? cut : maxLen;
      out.push(part.slice(0, cut));
      part = strip(part.slice(cut));
    }
    if (out.length && merges(out, part, maxLen, firstMin)) out[out.length - 1] = out[out.length - 1] + " " + part;
    else out.push(part);
  }
  if (!out.length) return [strip(text)];

  if (out[0].length > firstMax) {
    const head = out[0];
    const cut = Math.max(rfind(head, ", ", firstMax), rfind(head, "; ", firstMax),
      rfind(head, ": ", firstMax), rfind(head, " — ", firstMax));
    let take = cut >= 20 ? cut + 1 : 0;
    if (take === 0) take = wordCut(head, firstMax);
    if (take) out.splice(0, 1, strip(head.slice(0, take)), strip(head.slice(take)));
  }
  return out;
}

/**
 * The chunks of a half-written reply that appending cannot still change: one
 * is settled once the one after it exists and is long enough that it will not
 * be merged back into it.
 */
export function stableChunks(partial: string): string[] {
  const chunks = sentences(partial);
  const stable: string[] = [];
  for (let i = 0; i < chunks.length - 1; i++) {
    if (chunks[i + 1].length < 25) break;
    stable.push(chunks[i]);
  }
  return stable;
}
