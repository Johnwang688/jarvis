// Offsets, lines and points: the one place the syntax worker converts between
// Monaco's positions and tree-sitter's (ED-5, editor plan §2.6).
//
// **The unit is the UTF-16 code unit, on both sides.** web-tree-sitter parses
// a JS string as UTF-16 and reports every index and column in code units
// (measured on 0.27.0: "a😀b" is four units, 5..9, in its node table), and
// Monaco's offsets and columns are code units too. So an offset passes between
// them unchanged; what differs is where a line breaks:
//  - **tree-sitter** counts rows at `\n` only: a `\r` before it is the last
//    column of its row. `tree.edit()` needs points in that reckoning
//    (`TsLines`, `pointAt`).
//  - **Monaco** breaks lines at `\r\n`, `\n` and a lone `\r`, and a line's
//    length leaves its break out. Semantic tokens are given in Monaco's lines
//    (`Lines`), and the worker never uses tree-sitter's rows for them — only
//    its offsets — so a lone `\r` (which Monaco normalises away anyway) cannot
//    put a token on the wrong line.
// A tab is one unit to both and is never expanded here.
//
// **One exception: a query's range.** web-tree-sitter 0.27 passes a query's
// `startIndex`/`endIndex` to the C library as they are, which counts them in
// bytes of its UTF-16 input — two per code unit — so a range given in code
// units covers the first half of what was meant (measured: units 6..9 asked
// as 12..18). `queryRange` converts; a test pins the behaviour, so a runtime
// that starts converting itself fails loudly instead of colouring half a
// screen.

/** Bytes per UTF-16 code unit in a query's index range (see above). */
export const QUERY_BYTES_PER_UNIT = 2;

/** A query's index range for the code units `[from, to)`. */
export function queryRange(from: number, to: number): { startIndex: number; endIndex: number } {
  return { startIndex: from * QUERY_BYTES_PER_UNIT, endIndex: to * QUERY_BYTES_PER_UNIT };
}

export interface Point {
  row: number;
  column: number;
}

/** Line starts by a breaking rule. `starts[0]` is 0; a line starts after each break. */
class LineTable {
  readonly starts: number[];
  constructor(readonly text: string, monaco: boolean) {
    const starts = [0];
    const n = text.length;
    for (let i = 0; i < n; i++) {
      const c = text.charCodeAt(i);
      if (c === 10) starts.push(i + 1);
      else if (c === 13 && monaco) {
        if (text.charCodeAt(i + 1) === 10) i++;
        starts.push(i + 1);
      }
    }
    this.starts = starts;
  }

  get count(): number {
    return this.starts.length;
  }

  /** The 0-based line holding `offset` (an offset at a line's start is that line's). */
  lineOf(offset: number): number {
    const s = this.starts;
    let lo = 0;
    let hi = s.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (s[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }
}

/** Monaco's lines over a text: 0-based line numbers, characters in code units. */
export class Lines {
  private readonly table: LineTable;
  constructor(readonly text: string) {
    this.table = new LineTable(text, true);
  }

  get count(): number {
    return this.table.count;
  }

  start(line: number): number {
    return this.table.starts[line];
  }

  /** The offset just past the line's last character (its break excluded). */
  end(line: number): number {
    const next = line + 1 < this.table.count ? this.table.starts[line + 1] : this.text.length;
    let e = next;
    if (e > this.table.starts[line] && line + 1 < this.table.count) {
      e--; // \n or a lone \r
      if (this.text.charCodeAt(e) === 10 && e > this.table.starts[line] && this.text.charCodeAt(e - 1) === 13) e--;
    }
    return e;
  }

  length(line: number): number {
    return this.end(line) - this.start(line);
  }

  position(offset: number): { line: number; character: number } {
    const line = this.table.lineOf(offset);
    return { line, character: offset - this.table.starts[line] };
  }

  offset(line: number, character: number): number {
    return this.table.starts[line] + character;
  }
}

/** tree-sitter's rows over a text: rows at `\n`, columns in code units. */
export class TsLines {
  private readonly table: LineTable;
  constructor(readonly text: string) {
    this.table = new LineTable(text, false);
  }

  point(offset: number): Point {
    const row = this.table.lineOf(offset);
    return { row, column: offset - this.table.starts[row] };
  }

  offset(point: Point): number {
    const row = Math.min(Math.max(point.row, 0), this.table.count - 1);
    return Math.min(this.table.starts[row] + Math.max(point.column, 0), this.text.length);
  }
}

/** A point in tree-sitter's reckoning for one offset, without a table (for one-off use). */
export function pointAt(text: string, offset: number): Point {
  let row = 0;
  let last = -1;
  for (let i = text.indexOf("\n"); i !== -1 && i < offset; i = text.indexOf("\n", i + 1)) {
    row++;
    last = i;
  }
  return { row, column: offset - last - 1 };
}

/** One Monaco content change: replace `length` units at `offset` with `text`. */
export interface Change {
  offset: number;
  length: number;
  text: string;
}

/** The arguments `tree.edit()` takes for one change, and the text after it. */
export interface TreeEdit {
  startIndex: number;
  oldEndIndex: number;
  newEndIndex: number;
  startPosition: Point;
  oldEndPosition: Point;
  newEndPosition: Point;
}

/** The point at the end of `inserted`, starting from `start`. */
function advance(start: Point, inserted: string): Point {
  const nl = inserted.lastIndexOf("\n");
  if (nl === -1) return { row: start.row, column: start.column + inserted.length };
  let rows = 0;
  for (let i = inserted.indexOf("\n"); i !== -1; i = inserted.indexOf("\n", i + 1)) rows++;
  return { row: start.row + rows, column: inserted.length - nl - 1 };
}

/**
 * Apply one change to `text`: the new text and the edit for tree-sitter, in
 * code units and tree-sitter's points. Offsets are clamped to the text, so a
 * change can never index past it.
 */
export function applyChange(text: string, change: Change, lines?: TsLines): { text: string; edit: TreeEdit } {
  const start = Math.min(Math.max(change.offset, 0), text.length);
  const oldEnd = Math.min(start + Math.max(change.length, 0), text.length);
  const table = lines ?? new TsLines(text);
  const startPosition = table.point(start);
  const oldEndPosition = table.point(oldEnd);
  const next = text.slice(0, start) + change.text + text.slice(oldEnd);
  return {
    text: next,
    edit: {
      startIndex: start,
      oldEndIndex: oldEnd,
      newEndIndex: start + change.text.length,
      startPosition,
      oldEndPosition,
      newEndPosition: advance(startPosition, change.text),
    },
  };
}
