// Folding from the tree (ED-6, editor plan §2.6): nvim-treesitter's
// `folds.scm` for each covered language, its `@fold` captures turned into
// Monaco folding ranges. The worker runs the query (engine.ts `folds`, within
// the `matchMs` budget); this module only reads its matches, so it is pure
// and Node runs it on the real grammars. The main thread's half is the
// `FoldingRangeProvider` in client.ts, which Monaco's sticky scroll reads too.
//
// From a match to a range, as Neovim's own fold expression reads the same
// captures (runtime/lua/vim/treesitter/_fold.lua):
//  - a quantified capture (`(preproc_include)+ @fold`) is one fold, from its
//    first node to its last;
//  - `#trim!` (Markdown's sections and code blocks) moves the end back past
//    trailing blank lines, and, given its four flags, the start forward past
//    leading ones;
//  - a node that ends at the start of a line (column 0) ends on the line
//    before;
//  - a fold must span two lines at least.
// One rule of VS Code's, because Monaco's indentation folding (the fallback)
// and the owner's VS Code both keep it: **a closing bracket that starts its
// own line stays visible**, so `void f() {` folds to `void f() { ⋯` with its
// `}` below. Only the node's own closer counts — found by following last
// children at most `CLOSER_DEPTH` levels down (`};` and `},` past their
// punctuation) — so a Python `def` whose last statement ends in a multi-line
// call folds whole rather than leaving a lone `)` behind. Ranges sharing a
// start line keep the widest.

import type { Node, Query, QueryMatch } from "web-tree-sitter";
import type { Lines } from "./positions";

/** What Monaco calls a fold's kind: "Fold All Block Comments", `foldingImportsByDefault`. */
export type FoldKind = "comment" | "imports";

/** A fold in Monaco's lines: 1-based, `end > start`, the start line stays visible. */
export interface FoldRange {
  start: number;
  end: number;
  kind?: FoldKind;
}

/** The capture nvim's folds queries name. */
export const FOLD_CAPTURE = "fold";

/** Tokens that close a node's body. */
const CLOSERS = new Set(["}", "]", ")", "end_tag", "jsx_closing_element"]);

/**
 * How far below a fold's node its closer may be: 1 is a child (`{ … }` itself,
 * a JSON object), 2 a grandchild (a function's body), 3 for an `if … else`,
 * a `try … catch` or a templated class. A Python statement's last bracket is
 * four or more down, and is not the fold's.
 */
export const CLOSER_DEPTH = 3;

const IMPORTS = new Set(["preproc_include", "import_statement", "import_from_statement"]);

const isSpace = (c: number) => c === 32 || c === 9 || c === 10 || c === 13 || c === 12 || c === 11;

/** `#trim! @fold [start_linewise start_charwise end_linewise end_charwise]`: which ends to trim, or null. */
function trimOf(query: Query, m: QueryMatch): { start: boolean; end: boolean } | null {
  for (const p of query.predicates[m.patternIndex] ?? []) {
    if (p.operator !== "trim!") continue;
    const [cap, ...rest] = p.operands;
    if (!cap || cap.type !== "capture" || cap.name !== FOLD_CAPTURE) continue;
    // A capture alone trims blank lines from the end (nvim's default).
    if (rest.length === 0) return { start: false, end: true };
    const on = rest.map((s) => s.type === "string" && s.value !== "0");
    return { start: !!(on[0] || on[1]), end: !!(on[2] || on[3]) };
  }
  return null;
}

/** The token that closes `node`'s body, if it is within `CLOSER_DEPTH` levels of last children. */
function closerOf(node: Node): Node | null {
  let n: Node | null = node;
  for (let depth = 1; n && depth <= CLOSER_DEPTH; depth++) {
    let c: Node | null = n.lastChild;
    while (c && (c.type === ";" || c.type === ",")) c = c.previousSibling;
    if (!c) return null;
    if (CLOSERS.has(c.type)) return c;
    n = c;
  }
  return null;
}

/** Matches read between looks at the clock. */
const CHECK_EVERY = 128;

/**
 * The fold ranges of a folds query's matches over `text` (`lines` is
 * Monaco's reckoning of it), sorted by start line. `keep` filters a match by
 * the worker's own predicates (`#has-ancestor?` …, engine.ts). `over` is the
 * time budget, looked at every `CHECK_EVERY` matches: once it says so the
 * answer is null — reading tens of thousands of folds out of a near-1 MB file
 * costs more than the query did.
 */
export function foldRanges(
  query: Query,
  matches: readonly QueryMatch[],
  text: string,
  lines: Lines,
  keep: (query: Query, m: QueryMatch) => boolean = () => true,
  over: () => boolean = () => false,
): FoldRange[] | null {
  const widest = new Map<number, FoldRange>();
  for (let i = 0; i < matches.length; i++) {
    if (i % CHECK_EVERY === CHECK_EVERY - 1 && over()) return null;
    const m = matches[i];
    if (!keep(query, m)) continue;
    // Node offsets are UTF-16 code units, as Monaco's (positions.ts).
    let first: Node | null = null;
    let last: Node | null = null;
    let from = Infinity;
    let to = -1;
    for (const c of m.captures) {
      if (c.name !== FOLD_CAPTURE) continue;
      const s = c.node.startIndex;
      const e = c.node.endIndex;
      if (s < from) {
        first = c.node;
        from = s;
      }
      if (e > to) {
        last = c.node;
        to = e;
      }
    }
    if (!first || !last) continue;
    from = Math.min(from, text.length);
    to = Math.min(to, text.length);
    const trim = trimOf(query, m);
    if (trim?.start) while (from < to && isSpace(text.charCodeAt(from))) from++;
    if (trim?.end) while (to > from && isSpace(text.charCodeAt(to - 1))) to--;
    if (to <= from) continue;
    const start = lines.position(from).line;
    const at = lines.position(to);
    let end = at.character === 0 && at.line > start ? at.line - 1 : at.line;
    if (end <= start) continue; // one line: no fold, and nothing more to read
    // The node's own closer, starting the fold's last line: that line stays visible.
    const closer = closerOf(last);
    if (closer && closer.startIndex >= from) {
      const line = lines.position(closer.startIndex).line;
      if (line === end && !/\S/.test(text.slice(lines.start(line), closer.startIndex))) end = line - 1;
    }
    if (end <= start) continue;
    const kind: FoldKind | undefined = /comment$/.test(first.type) ? "comment" : IMPORTS.has(first.type) ? "imports" : undefined;
    const range: FoldRange = { start: start + 1, end: end + 1, ...(kind ? { kind } : {}) };
    const had = widest.get(range.start);
    if (!had || range.end > had.end) widest.set(range.start, range);
  }
  return [...widest.values()].sort((a, b) => a.start - b.start);
}
