import { describe, expect, it } from "vitest";
import {
  DIFF_BUDGET, diffLines, gutterDecorations, gutterMarks, marksKey, splitLines, type Hunk,
} from "./linediff";

/** A seeded generator, so a failure names the case that broke. */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** The reference: the longest common subsequence's length, by the textbook O(NM) table. */
function lcs(a: readonly string[], b: readonly string[]): number {
  const row = new Array(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i++) {
    let diag = 0;
    for (let j = 1; j <= b.length; j++) {
      const up = row[j];
      row[j] = a[i - 1] === b[j - 1] ? diag + 1 : Math.max(row[j], row[j - 1]);
      diag = up;
    }
  }
  return row[b.length];
}

/** The hunks really turn `a` into `b`: in order, apart, and every line between them equal. */
function check(a: readonly string[], b: readonly string[], hunks: readonly Hunk[]): string | null {
  let i = 0;
  let j = 0;
  for (const h of hunks) {
    if (h.aLen === 0 && h.bLen === 0) return "an empty hunk";
    if (h.a < i || h.b < j) return "out of order";
    if (h.a - i !== h.b - j) return "the equal run before a hunk is not the same length on both sides";
    if (hunks.indexOf(h) > 0 && h.a === i && h.b === j) return "two hunks touch (they should be one)";
    for (let t = 0; t < h.a - i; t++) if (a[i + t] !== b[j + t]) return `line ${i + t} is called equal and is not`;
    i = h.a + h.aLen;
    j = h.b + h.bLen;
    if (i > a.length || j > b.length) return "past the end";
  }
  if (a.length - i !== b.length - j) return "the tail is not the same length on both sides";
  for (let t = 0; t < a.length - i; t++) if (a[i + t] !== b[j + t]) return "the tail differs";
  return null;
}

const cost = (hunks: readonly Hunk[]) => hunks.reduce((s, h) => s + h.aLen + h.bLen, 0);

function randomLines(next: () => number, n: number, alphabet: number): string[] {
  return Array.from({ length: n }, () => String.fromCharCode(97 + Math.floor(next() * alphabet)));
}

/** `a` edited at random: lines deleted, inserted and replaced, like a person typing. */
function edited(next: () => number, a: readonly string[], alphabet: number): string[] {
  const out: string[] = [];
  for (const line of a) {
    const r = next();
    if (r < 0.1) continue;
    if (r < 0.2) out.push(String.fromCharCode(97 + Math.floor(next() * alphabet)));
    else out.push(line);
    if (next() < 0.1) out.push(String.fromCharCode(97 + Math.floor(next() * alphabet)));
  }
  return out;
}

describe("the Myers line diff, against a reference", () => {
  it("is a correct and minimal edit over 3,000 random pairs (unrelated and edited)", () => {
    const next = rng(9);
    let failures: string[] = [];
    for (let c = 0; c < 3000; c++) {
      const alphabet = 1 + Math.floor(next() * 6);
      const a = randomLines(next, Math.floor(next() * 40), alphabet);
      const b = c % 2 ? edited(next, a, alphabet) : randomLines(next, Math.floor(next() * 40), alphabet);
      const { hunks, exact } = diffLines(a, b);
      const bad = check(a, b, hunks);
      const want = a.length + b.length - 2 * lcs(a, b);
      if (bad || !exact || cost(hunks) !== want) {
        failures.push(`case ${c}: ${bad ?? ""} exact=${exact} cost ${cost(hunks)} vs ${want} a=${a.join("")} b=${b.join("")}`);
        if (failures.length > 5) break;
      }
    }
    expect(failures).toEqual([]);
  });

  it("the edge shapes: empty sides, identical, all different, one line", () => {
    const shapes: [string[], string[]][] = [
      [[], []], [[], ["x"]], [["x"], []], [["x"], ["x"]], [["x"], ["y"]],
      [["a", "b", "c"], ["a", "b", "c"]], [["a", "b"], ["c", "d"]], [["a"], ["a", "a", "a"]],
      [["a", "a", "a"], ["a"]], [["", ""], [""]],
    ];
    for (const [a, b] of shapes) {
      const { hunks, exact } = diffLines(a, b);
      expect(check(a, b, hunks), JSON.stringify([a, b])).toBeNull();
      expect(exact).toBe(true);
      expect(cost(hunks)).toBe(a.length + b.length - 2 * lcs(a, b));
    }
  });

  it("out of budget: a coarse but correct diff, said so", () => {
    const next = rng(4);
    for (let c = 0; c < 300; c++) {
      const a = randomLines(next, 30 + Math.floor(next() * 30), 4);
      const b = edited(next, a, 4);
      for (const budget of [0, 1, 10, 50]) {
        const { hunks, exact } = diffLines(a, b, budget);
        expect(check(a, b, hunks)).toBeNull();
        if (!exact) expect(cost(hunks)).toBeGreaterThanOrEqual(a.length + b.length - 2 * lcs(a, b));
      }
    }
    // A budget of nothing still strips the common ends.
    const { hunks, exact } = diffLines(["a", "x", "y", "b"], ["a", "p", "q", "b"], 0);
    expect(exact).toBe(false);
    expect(hunks).toEqual([{ a: 1, aLen: 2, b: 1, bLen: 2 }]);
  });

  it("a long file with scattered edits is exact and quick; a rewrite of it is bounded", () => {
    const a = Array.from({ length: 50_000 }, (_, i) => `line ${i}`);
    const b = a.slice();
    for (let i = 100; i < b.length; i += 5_000) b[i] = `changed ${i}`;
    b.splice(30_000, 0, "inserted");
    b.splice(40_000, 3);
    let t = performance.now();
    const small = diffLines(a, b);
    expect(performance.now() - t).toBeLessThan(500);
    expect(small.exact).toBe(true);
    expect(check(a, b, small.hunks)).toBeNull();
    expect(small.hunks.length).toBe(12);
    // Every line different: bounded by the budget, never hanging.
    const c = a.map((l) => l + "!");
    t = performance.now();
    const big = diffLines(a, c, DIFF_BUDGET);
    expect(performance.now() - t).toBeLessThan(2_000);
    expect(check(a, c, big.hunks)).toBeNull();
  });
});

describe("splitting a text as Monaco counts its lines", () => {
  it("CRLF, CR and LF are one break each; a trailing break ends in an empty line", () => {
    expect(splitLines("")).toEqual([""]);
    expect(splitLines("a")).toEqual(["a"]);
    expect(splitLines("a\n")).toEqual(["a", ""]);
    expect(splitLines("a\r\nb\rc\nd")).toEqual(["a", "b", "c", "d"]);
    // So a CRLF file against an LF buffer of the same text has no changes.
    expect(diffLines(splitLines("x\r\ny\r\n"), splitLines("x\ny\n")).hunks).toEqual([]);
  });
});

describe("what the gutter draws", () => {
  const marks = (a: string, b: string) => {
    const B = splitLines(b);
    return gutterMarks(diffLines(splitLines(a), B).hunks, B.length);
  };

  it("an added line, a modified one, a deletion between lines, at the top and at the end", () => {
    expect(marks("a\nb\nc\n", "a\nb\nNEW\nc\n")).toEqual([{ kind: "added", start: 3, end: 3 }]);
    expect(marks("a\nb\nc\n", "a\nB\nc\n")).toEqual([{ kind: "modified", start: 2, end: 2 }]);
    expect(marks("a\nb\nc\n", "a\nc\n")).toEqual([{ kind: "deleted", start: 1, end: 1 }]);
    expect(marks("a\nb\nc\n", "b\nc\n")).toEqual([{ kind: "deleted", start: 1, end: 1, top: true }]);
    expect(marks("a\nb\nc", "a\nb")).toEqual([{ kind: "deleted", start: 2, end: 2 }]);
    expect(marks("a\nb\n", "a\nb\n")).toEqual([]);
    // A replaced run is "modified" over the lines standing there now.
    expect(marks("a\nb\nc\nd\n", "a\nX\nY\nZ\nd\n")).toEqual([{ kind: "modified", start: 2, end: 4 }]);
    // Everything gone: one marker at the top of the one empty line left.
    expect(marks("a\nb\n", "")).toEqual([{ kind: "deleted", start: 1, end: 1, top: true }]);
  });

  it("undoing back to HEAD's text draws nothing", () => {
    const head = "int main() {\n  return 0;\n}\n";
    expect(marks(head, head.replace("return 0", "return 1"))).toHaveLength(1);
    expect(marks(head, head)).toEqual([]);
  });

  it("decorations: line decorations only, one class per kind", () => {
    const d = gutterDecorations([
      { kind: "added", start: 3, end: 4 }, { kind: "modified", start: 6, end: 6 },
      { kind: "deleted", start: 1, end: 1, top: true },
    ]);
    expect(d.map((x) => x.options.linesDecorationsClassName)).toEqual(["jgit jgit-add", "jgit jgit-mod", "jgit jgit-del jgit-top"]);
    expect(d[0].range).toEqual({ startLineNumber: 3, startColumn: 1, endLineNumber: 4, endColumn: 1 });
    for (const x of d) expect(Object.keys(x.options).sort()).toEqual(["description", "isWholeLine", "linesDecorationsClassName"]);
    expect(marksKey([{ kind: "added", start: 3, end: 4 }])).not.toBe(marksKey([{ kind: "modified", start: 3, end: 4 }]));
  });
});
