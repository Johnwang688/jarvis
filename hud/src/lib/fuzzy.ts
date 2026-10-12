// Fuzzy matching for quick open and the palette (ED-7, editor plan §2.8):
// pure, so it is tested without a window.
//
// A query matches a target when its characters (spaces ignored, case
// ignored) appear in order. Among matches, the better one:
//  - matches inside the file's own name rather than spread over its folders;
//  - matches at word starts (after `/ _ - . space`, or a capital after a
//    lower-case letter) and in runs of consecutive characters;
//  - starts the name (`main` → `main.cpp` beats `domain.cpp`);
//  - is shorter, then sorts first alphabetically.

export interface FuzzyHit {
  score: number;
  /** Indexes into the target of each matched character, in order. */
  positions: number[];
}

const SEP = new Set(["/", "\\", "_", "-", ".", " ", ":"]);

function wordStart(target: string, i: number): boolean {
  if (i === 0) return true;
  const prev = target[i - 1];
  if (SEP.has(prev)) return true;
  const c = target[i];
  return c >= "A" && c <= "Z" && prev >= "a" && prev <= "z";
}

/** The query's characters, lower-cased, spaces dropped. */
export function normalizeQuery(query: string): string {
  return query.replace(/\s+/g, "").toLowerCase();
}

/**
 * Match `q` (already `normalizeQuery`d) in `target[from..]`, preferring word
 * starts: each character takes the next word start that still leaves room for
 * the rest, else the next occurrence. Null when it does not match.
 */
function place(q: string, target: string, lower: string, from: number): number[] | null {
  const out: number[] = [];
  let at = from;
  for (let k = 0; k < q.length; k++) {
    const ch = q[k];
    let i = lower.indexOf(ch, at);
    if (i < 0) return null;
    // A later word start for this character, if the rest still fits after it.
    if (!wordStart(target, i) && !(k > 0 && out[k - 1] === i - 1)) {
      let j = lower.indexOf(ch, i + 1);
      while (j >= 0) {
        if (wordStart(target, j)) {
          if (fits(q, lower, k + 1, j + 1)) i = j;
          break;
        }
        j = lower.indexOf(ch, j + 1);
      }
    }
    out.push(i);
    at = i + 1;
  }
  return out;
}

function fits(q: string, lower: string, k: number, from: number): boolean {
  let at = from;
  for (; k < q.length; k++) {
    const i = lower.indexOf(q[k], at);
    if (i < 0) return false;
    at = i + 1;
  }
  return true;
}

function scoreOf(target: string, positions: number[], nameStart: number, query: string): number {
  let score = 0;
  for (let k = 0; k < positions.length; k++) {
    const i = positions[k];
    score += 1;
    if (wordStart(target, i)) score += 8;
    if (k > 0 && positions[k - 1] === i - 1) score += 6;
    if (i >= nameStart) score += 4;
    if (k > 0) score -= Math.min(6, (i - positions[k - 1] - 1) * 0.5);
    if (query[k] === target[i]) score += 0.5; // the same case as typed
  }
  if (positions[0] === nameStart) score += 12;
  if (positions.every((i) => i >= nameStart)) score += 20;
  return score - target.length * 0.02;
}

/** How well `query` matches `target` (a path or a label), or null. */
export function fuzzyMatch(query: string, target: string): FuzzyHit | null {
  const q = normalizeQuery(query);
  if (!q) return { score: 0, positions: [] };
  const lower = target.toLowerCase();
  const slash = target.lastIndexOf("/");
  const nameStart = slash + 1;
  // Within the name alone first, then over the whole path.
  const inName = nameStart > 0 ? place(q, target, lower, nameStart) : null;
  const whole = place(q, target, lower, 0);
  if (!whole && !inName) return null;
  const a = inName ? scoreOf(target, inName, nameStart, query.replace(/\s+/g, "")) : -Infinity;
  const b = whole ? scoreOf(target, whole, nameStart, query.replace(/\s+/g, "")) : -Infinity;
  return a >= b ? { score: a, positions: inName! } : { score: b, positions: whole! };
}

/** A cheap first pass: are `q`'s characters in `lower`, in order? */
function subsequence(q: string, lower: string): boolean {
  let at = 0;
  for (let k = 0; k < q.length; k++) {
    const i = lower.indexOf(q[k], at);
    if (i < 0) return false;
    at = i + 1;
  }
  return true;
}

export interface Ranked<T> {
  item: T;
  score: number;
  positions: number[];
}

/**
 * The best `limit` items for `query`, best first. An empty query keeps the
 * items' own order. `key` gives each item's text.
 */
export function fuzzyFilter<T>(query: string, items: readonly T[], key: (item: T) => string, limit = 50): Ranked<T>[] {
  const q = normalizeQuery(query);
  if (!q) return items.slice(0, limit).map((item) => ({ item, score: 0, positions: [] }));
  const out: Ranked<T>[] = [];
  for (const item of items) {
    const text = key(item);
    if (!subsequence(q, text.toLowerCase())) continue;
    const hit = fuzzyMatch(query, text);
    if (hit) out.push({ item, score: hit.score, positions: hit.positions });
  }
  out.sort((x, y) => y.score - x.score || key(x.item).length - key(y.item).length
    || (key(x.item) < key(y.item) ? -1 : key(x.item) > key(y.item) ? 1 : 0));
  return out.slice(0, limit);
}

/**
 * `file.cpp:42` or `file.cpp:42:7` (VS Code's quick open): the query and the
 * place to go. A bare `:42` is a line in the file already open.
 */
export function splitLine(query: string): { query: string; line: number | null; col: number | null } {
  const m = /^(.*?):(\d+)(?::(\d+))?\s*$/.exec(query);
  if (!m) return { query, line: null, col: null };
  const line = Number(m[2]);
  const col = m[3] ? Number(m[3]) : null;
  if (!Number.isSafeInteger(line) || line < 1) return { query, line: null, col: null };
  return { query: m[1], line, col: col && col >= 1 ? col : null };
}
