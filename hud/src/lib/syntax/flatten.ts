// Captures → non-overlapping spans → Monaco's semantic tokens (ED-5, editor
// plan §2.6). Pure; the syntax worker calls it, and the tests drive it with
// hand-made captures.
//
// **Who wins where captures overlap.** The queries are nvim-treesitter's, and
// they are written for Neovim's highlighter, which draws every capture as an
// extmark: a higher `#set! priority` wins (default 100), and between equal
// priorities the one drawn later wins — an injected language's after its
// host's, and within one tree the order the query cursor returns them in,
// which is by start position, then by pattern (a later pattern in the file
// wins over an earlier one on the same node — the convention nvim's queries
// are written to: a general `(identifier) @variable` first, the specific
// `@function.call` after it). So the rank, highest winning, is:
//   priority, then injection depth, then start offset (an inner node starts
//   no earlier than its parent, so the innermost wins), then pattern index,
//   then the shorter span, then the capture's own order.
// The plan's shorthand for this was "the most specific capture wins, and later
// patterns break ties"; the rank above is what Neovim does, and where the two
// could differ (an outer node captured by a later pattern at the same start as
// an inner one) Neovim's answer is the one the queries expect.

export interface Capture {
  start: number;
  end: number;
  /** Index into the legend's token types. */
  type: number;
  /** Index into the legend's modifiers (the language it is styled as), or -1. */
  modifier: number;
  priority: number;
  /** 0 for the document's own grammar, 1 for an injected one. */
  depth: number;
  pattern: number;
  /** The order it was collected in: the last tie-break. */
  order: number;
}

export interface Span {
  start: number;
  end: number;
  type: number;
  modifier: number;
}

/** a beats b. */
export function beats(a: Capture, b: Capture): boolean {
  if (a.priority !== b.priority) return a.priority > b.priority;
  if (a.depth !== b.depth) return a.depth > b.depth;
  if (a.start !== b.start) return a.start > b.start;
  if (a.pattern !== b.pattern) return a.pattern > b.pattern;
  const la = a.end - a.start, lb = b.end - b.start;
  if (la !== lb) return la < lb;
  return a.order > b.order;
}

/** A binary max-heap of captures by `beats`. */
class Heap {
  private readonly items: Capture[] = [];
  get size() {
    return this.items.length;
  }
  peek(): Capture | undefined {
    return this.items[0];
  }
  push(c: Capture) {
    const a = this.items;
    a.push(c);
    let i = a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (!beats(a[i], a[p])) break;
      [a[i], a[p]] = [a[p], a[i]];
      i = p;
    }
  }
  pop(): Capture | undefined {
    const a = this.items;
    const top = a[0];
    const last = a.pop();
    if (a.length && last) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < a.length && beats(a[l], a[m])) m = l;
        if (r < a.length && beats(a[r], a[m])) m = r;
        if (m === i) break;
        [a[i], a[m]] = [a[m], a[i]];
        i = m;
      }
    }
    return top;
  }
}

/**
 * Overlapping captures as non-overlapping spans, in order: at every offset the
 * capture that `beats` all others covering it. Adjacent spans with the same
 * type and modifier are merged. Empty captures are ignored. O(n log n).
 */
export function flatten(captures: readonly Capture[]): Span[] {
  const live = captures.filter((c) => c.end > c.start);
  if (!live.length) return [];
  const byStart = [...live].sort((a, b) => a.start - b.start);
  const points = new Set<number>();
  for (const c of live) {
    points.add(c.start);
    points.add(c.end);
  }
  const cuts = [...points].sort((a, b) => a - b);
  const heap = new Heap();
  const out: Span[] = [];
  let next = 0;
  for (let k = 0; k + 1 < cuts.length; k++) {
    const from = cuts[k], to = cuts[k + 1];
    while (next < byStart.length && byStart[next].start <= from) heap.push(byStart[next++]);
    // Drop winners that ended (lazily: only the top matters).
    while (heap.size && heap.peek()!.end <= from) heap.pop();
    const top = heap.peek();
    if (!top) continue;
    const prev = out[out.length - 1];
    if (prev && prev.end === from && prev.type === top.type && prev.modifier === top.modifier) prev.end = to;
    else out.push({ start: from, end: to, type: top.type, modifier: top.modifier });
  }
  return out;
}

/**
 * The reference answer for `flatten`, by brute force: paint each capture in
 * rank order, losers first, one offset at a time. The tests hold `flatten` to
 * it on random captures.
 */
export function flattenSlow(captures: readonly Capture[]): Span[] {
  const live = captures.filter((c) => c.end > c.start);
  if (!live.length) return [];
  const max = Math.max(...live.map((c) => c.end));
  const at: (Capture | null)[] = new Array(max).fill(null);
  for (const c of live) {
    for (let i = c.start; i < c.end; i++) if (!at[i] || beats(c, at[i]!)) at[i] = c;
  }
  const out: Span[] = [];
  for (let i = 0; i < max; i++) {
    const c = at[i];
    if (!c) continue;
    const prev = out[out.length - 1];
    if (prev && prev.end === i && prev.type === c.type && prev.modifier === c.modifier) prev.end = i + 1;
    else out.push({ start: i, end: i + 1, type: c.type, modifier: c.modifier });
  }
  return out;
}

export interface LineView {
  count: number;
  start(line: number): number;
  end(line: number): number;
}

export const LONG_LINE = 10_000;

/**
 * Spans as Monaco's semantic tokens for lines `[first, last]` (0-based):
 * five numbers per token — line delta, start delta, length, type, modifier
 * bits — each token on one line, a span that crosses lines cut at each break.
 * Text no span covers is given `none` (`noneType`) where it is not blank, so
 * tree-sitter's answer replaces Monarch's there too instead of letting its
 * guess show through (Python's `match` as a variable, not a keyword). A line
 * longer than `LONG_LINE` gets no tokens: Monarch keeps it.
 */
export function encode(
  spans: readonly Span[],
  lines: LineView,
  text: string,
  first: number,
  last: number,
  noneType: number,
): Uint32Array {
  const out: number[] = [];
  let prevLine = 0;
  let prevChar = 0;
  const emit = (line: number, char: number, length: number, type: number, modifier: number) => {
    if (length <= 0) return;
    const dl = line - prevLine;
    out.push(dl, dl === 0 ? char - prevChar : char, length, type, modifier >= 0 ? 1 << modifier : 0);
    prevLine = line;
    prevChar = char;
  };
  const blank = (from: number, to: number) => {
    for (let i = from; i < to; i++) {
      const c = text.charCodeAt(i);
      if (c !== 32 && c !== 9) return false;
    }
    return true;
  };
  // Spans are sorted and disjoint: walk them alongside the lines.
  let s = 0;
  const lastLine = Math.min(last, lines.count - 1);
  for (let line = Math.max(first, 0); line <= lastLine; line++) {
    const ls = lines.start(line), le = lines.end(line);
    if (le - ls > LONG_LINE) continue;
    while (s < spans.length && spans[s].end <= ls) s++;
    let at = ls;
    for (let k = s; k < spans.length && spans[k].start < le; k++) {
      const sp = spans[k];
      const from = Math.max(sp.start, ls), to = Math.min(sp.end, le);
      if (from > at && !blank(at, from)) emit(line, at - ls, from - at, noneType, -1);
      if (to > from) emit(line, from - ls, to - from, sp.type, sp.modifier);
      at = Math.max(at, to);
    }
    if (le > at && !blank(at, le)) emit(line, at - ls, le - at, noneType, -1);
  }
  return Uint32Array.from(out);
}

/** Decode tokens back to absolute `[line, char, length, type, modifierBits]` (tests, the hook). */
export function decode(data: ArrayLike<number>): [number, number, number, number, number][] {
  const out: [number, number, number, number, number][] = [];
  let line = 0, char = 0;
  for (let i = 0; i + 4 < data.length; i += 5) {
    line += data[i];
    char = data[i] === 0 ? char + data[i + 1] : data[i + 1];
    out.push([line, char, data[i + 2], data[i + 3], data[i + 4]]);
  }
  return out;
}
