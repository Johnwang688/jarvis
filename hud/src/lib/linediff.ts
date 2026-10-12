// The git gutter's line diff (ED-9, editor plan §2.5/§2.8): HEAD's lines
// against the *buffer's* — the text in the editor, saved or not — so the
// gutter follows an unsaved edit and returns to clean when it is undone.
//
// Pure. Myers' O(ND) algorithm in linear space: strip the common prefix and
// suffix, find a point on an optimal path where the forward and reverse
// searches meet (the middle snake), and split the problem there — on an
// explicit stack, never recursion, so a long file cannot overflow it.
//
// **Bounded**: every diagonal visited and every snake step spends one unit of
// `budget`. A problem that would cost more is answered coarsely (`exact:
// false`): what is left of it, once its common ends are stripped, is one
// changed run. That is still a correct diff (HEAD's lines in, the buffer's
// out), only not the smallest, so a huge rewrite paints one "modified" block
// instead of hanging the window.

export interface Hunk {
  /** Where the changed run starts in `a` (HEAD's lines, 0-based) and how many lines it replaces. */
  a: number;
  aLen: number;
  /** Where it starts in `b` (the buffer's lines, 0-based) and how many lines stand there now. */
  b: number;
  bLen: number;
}

export interface LineDiff {
  hunks: Hunk[];
  /** False when the budget ran out and part of the diff is one coarse run. */
  exact: boolean;
}

/** About 10–20 ms of work in a browser: an edit of ~1,400 changed lines is still exact. */
export const DIFF_BUDGET = 2_000_000;

/** A text's lines as Monaco counts them: split at CRLF, CR or LF; a trailing break ends in "". */
export function splitLines(text: string): string[] {
  return text.split(/\r\n|\r|\n/);
}

interface Work {
  left: number;
  exact: boolean;
}

/**
 * The point where an optimal path through `a[x0..x1)` × `b[y0..y1)` is split,
 * in absolute coordinates; `"apart"` when the two runs have no line in
 * common (replacing one with the other is then the optimal edit: the paths
 * would only meet at d = ⌈(n+m)/2⌉, past the last step searched); null when
 * the budget runs out first. Both ranges are non-empty and their first and
 * last elements differ (the caller strips the common ends), so the point is
 * never a corner.
 */
function bisect(a: Int32Array, x0: number, x1: number, b: Int32Array, y0: number, y1: number, work: Work): [number, number] | "apart" | null {
  const n = x1 - x0;
  const m = y1 - y0;
  const maxD = Math.ceil((n + m) / 2);
  // A d-step search visits about d² diagonals: never look further than the
  // budget can pay for, so the arrays stay small too.
  const limit = Math.min(maxD, Math.floor(Math.sqrt(Math.max(work.left, 0))) + 2);
  const off = limit;
  const len = 2 * limit + 2;
  const v1 = new Int32Array(len).fill(-1);
  const v2 = new Int32Array(len).fill(-1);
  v1[off + 1] = 0;
  v2[off + 1] = 0;
  work.left -= 2 * len;
  const delta = n - m;
  const front = (delta & 1) !== 0;
  let k1start = 0;
  let k1end = 0;
  let k2start = 0;
  let k2end = 0;
  for (let d = 0; d < limit; d++) {
    if (work.left <= 0) return null;
    // The forward search.
    for (let k1 = -d + k1start; k1 <= d - k1end; k1 += 2) {
      const k1off = off + k1;
      let x = k1 === -d || (k1 !== d && v1[k1off - 1] < v1[k1off + 1]) ? v1[k1off + 1] : v1[k1off - 1] + 1;
      let y = x - k1;
      const sx = x;
      while (x < n && y < m && a[x0 + x] === b[y0 + y]) {
        x++;
        y++;
      }
      work.left -= 1 + (x - sx);
      v1[k1off] = x;
      if (x > n) {
        k1end += 2;
      } else if (y > m) {
        k1start += 2;
      } else if (front) {
        const k2off = off + delta - k1;
        if (k2off >= 0 && k2off < len && v2[k2off] !== -1) {
          if (x >= n - v2[k2off]) return [x0 + x, y0 + y];
        }
      }
    }
    // The reverse search.
    for (let k2 = -d + k2start; k2 <= d - k2end; k2 += 2) {
      const k2off = off + k2;
      let x = k2 === -d || (k2 !== d && v2[k2off - 1] < v2[k2off + 1]) ? v2[k2off + 1] : v2[k2off - 1] + 1;
      let y = x - k2;
      const sx = x;
      while (x < n && y < m && a[x0 + n - x - 1] === b[y0 + m - y - 1]) {
        x++;
        y++;
      }
      work.left -= 1 + (x - sx);
      v2[k2off] = x;
      if (x > n) {
        k2end += 2;
      } else if (y > m) {
        k2start += 2;
      } else if (!front) {
        const k1off = off + delta - k2;
        if (k1off >= 0 && k1off < len && v1[k1off] !== -1) {
          const fx = v1[k1off];
          const fy = off + fx - k1off;
          if (fx >= n - x) return [x0 + fx, y0 + fy];
        }
      }
    }
  }
  return limit === maxD ? "apart" : null;
}

/** HEAD's lines `a` against the buffer's lines `b`: the changed runs, in order. */
export function diffLines(a: readonly string[], b: readonly string[], budget: number = DIFF_BUDGET): LineDiff {
  // Lines as small integers, so a comparison is one integer test.
  const ids = new Map<string, number>();
  const intern = (lines: readonly string[]) => {
    const out = new Int32Array(lines.length);
    for (let i = 0; i < lines.length; i++) {
      let id = ids.get(lines[i]);
      if (id === undefined) {
        id = ids.size;
        ids.set(lines[i], id);
      }
      out[i] = id;
    }
    return out;
  };
  const A = intern(a);
  const B = intern(b);
  const work: Work = { left: budget, exact: true };
  const hunks: Hunk[] = [];
  const push = (x0: number, x1: number, y0: number, y1: number) => {
    const last = hunks[hunks.length - 1];
    if (last && last.a + last.aLen === x0 && last.b + last.bLen === y0) {
      last.aLen += x1 - x0;
      last.bLen += y1 - y0;
    } else {
      hunks.push({ a: x0, aLen: x1 - x0, b: y0, bLen: y1 - y0 });
    }
  };
  // Ranges still to compare, left to right: four numbers each, popped from the end.
  const stack: number[] = [0, A.length, 0, B.length];
  while (stack.length) {
    let y1 = stack.pop()!;
    let y0 = stack.pop()!;
    let x1 = stack.pop()!;
    let x0 = stack.pop()!;
    while (x0 < x1 && y0 < y1 && A[x0] === B[y0]) {
      x0++;
      y0++;
    }
    while (x0 < x1 && y0 < y1 && A[x1 - 1] === B[y1 - 1]) {
      x1--;
      y1--;
    }
    if (x0 === x1 && y0 === y1) continue;
    if (x0 === x1 || y0 === y1) {
      push(x0, x1, y0, y1);
      continue;
    }
    const split = work.left > 0 ? bisect(A, x0, x1, B, y0, y1, work) : null;
    if (split === "apart") {
      push(x0, x1, y0, y1);
      continue;
    }
    if (!split || (split[0] === x0 && split[1] === y0) || (split[0] === x1 && split[1] === y1)) {
      work.exact = false;
      push(x0, x1, y0, y1);
      continue;
    }
    // The right half goes on first, so the left half is compared first.
    stack.push(split[0], x1, split[1], y1);
    stack.push(x0, split[0], y0, split[1]);
  }
  return { hunks, exact: work.exact };
}

export type GutterKind = "added" | "modified" | "deleted";

export interface GutterMark {
  kind: GutterKind;
  /** The buffer's lines, 1-based and inclusive. A deletion marks one line: the one above the gap. */
  start: number;
  end: number;
  /** A deletion before the first line: drawn at the top of line 1 instead of a line's foot. */
  top?: boolean;
}

/**
 * What the gutter draws for a diff: inserted runs "added", replaced runs
 * "modified" (as many lines as stand there now), and removed runs a
 * "deleted" marker at the foot of the line above the gap (or the head of the
 * first line). `bLines` is the buffer's line count.
 */
export function gutterMarks(hunks: readonly Hunk[], bLines: number): GutterMark[] {
  const out: GutterMark[] = [];
  for (const h of hunks) {
    if (h.bLen === 0) {
      if (h.b > 0) out.push({ kind: "deleted", start: Math.min(h.b, bLines), end: Math.min(h.b, bLines) });
      else out.push({ kind: "deleted", start: 1, end: 1, top: true });
    } else {
      out.push({ kind: h.aLen === 0 ? "added" : "modified", start: h.b + 1, end: h.b + h.bLen });
    }
  }
  return out;
}

/** The class each kind is drawn with (theme.css: `--syn-string`, `--syn-fn`, `--red` at 60%). */
export const GUTTER_CLASS: Record<GutterKind, string> = {
  added: "jgit jgit-add",
  modified: "jgit jgit-mod",
  deleted: "jgit jgit-del",
};

/** Monaco decorations for the marks: line decorations only, never the text. */
export function gutterDecorations(marks: readonly GutterMark[]) {
  return marks.map((m) => ({
    range: { startLineNumber: m.start, startColumn: 1, endLineNumber: m.end, endColumn: 1 },
    options: {
      description: `jarvis-git-${m.kind}`,
      isWholeLine: true,
      linesDecorationsClassName: GUTTER_CLASS[m.kind] + (m.top ? " jgit-top" : ""),
    },
  }));
}

/** One string per set of marks: equal exactly when the gutter would draw the same. */
export function marksKey(marks: readonly GutterMark[]): string {
  return marks.map((m) => `${m.kind[0]}${m.start}-${m.end}${m.top ? "t" : ""}`).join(",");
}
