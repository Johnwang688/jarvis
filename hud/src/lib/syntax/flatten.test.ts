// Flattening captures as Neovim's highlighter resolves them (ED-5), and the
// semantic tokens Monaco is handed.

import { beats, decode, encode, flatten, flattenSlow, LONG_LINE, type Capture } from "./flatten";
import { Lines } from "./positions";

let order = 0;
const cap = (start: number, end: number, type: number, extra: Partial<Capture> = {}): Capture => ({
  start, end, type, modifier: 0, priority: 100, depth: 0, pattern: 0, order: order++, ...extra,
});

describe("flatten resolves overlaps as Neovim's highlighter does", () => {
  it("the inner node wins inside the outer one (an escape inside a string)", () => {
    expect(flatten([cap(0, 10, 1), cap(4, 6, 2)])).toEqual([
      { start: 0, end: 4, type: 1, modifier: 0 },
      { start: 4, end: 6, type: 2, modifier: 0 },
      { start: 6, end: 10, type: 1, modifier: 0 },
    ]);
  });

  it("on the same node, the later pattern wins (`@variable`, then `@function.call`)", () => {
    expect(flatten([cap(0, 5, 1, { pattern: 3 }), cap(0, 5, 2, { pattern: 40 })])).toEqual([{ start: 0, end: 5, type: 2, modifier: 0 }]);
    expect(flatten([cap(0, 5, 2, { pattern: 40 }), cap(0, 5, 1, { pattern: 3 })])).toEqual([{ start: 0, end: 5, type: 2, modifier: 0 }]);
  });

  it("a higher #set! priority wins over nesting and pattern order; a lower one loses", () => {
    expect(flatten([cap(0, 10, 1, { priority: 105 }), cap(2, 4, 2, { pattern: 99 })])).toEqual([{ start: 0, end: 10, type: 1, modifier: 0 }]);
    // Markdown's fenced block is priority 90, so the code inside it shows.
    expect(flatten([cap(0, 10, 1, { priority: 90 }), cap(0, 3, 2)])[0]).toEqual({ start: 0, end: 3, type: 2, modifier: 0 });
  });

  it("an injected language's capture beats its host's at equal priority", () => {
    expect(flatten([cap(0, 10, 1, { pattern: 50 }), cap(0, 10, 2, { depth: 1, pattern: 0 })])).toEqual([{ start: 0, end: 10, type: 2, modifier: 0 }]);
  });

  it("merges neighbours of one type and modifier, keeps different modifiers apart, ignores empty captures", () => {
    expect(flatten([cap(0, 2, 1), cap(2, 4, 1), cap(4, 6, 1, { modifier: 3 }), cap(7, 7, 9)])).toEqual([
      { start: 0, end: 4, type: 1, modifier: 0 },
      { start: 4, end: 6, type: 1, modifier: 3 },
    ]);
  });

  it("equals the brute-force painter on 2,000 random sets of captures", () => {
    let seed = 7;
    const r = (n: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed % n;
    };
    for (let k = 0; k < 2000; k++) {
      const cs: Capture[] = [];
      for (let i = 0, n = 1 + r(12); i < n; i++) {
        const s = r(40);
        cs.push(cap(s, s + r(15), r(5), { priority: [90, 100, 100, 100, 105][r(5)], depth: r(4) === 0 ? 1 : 0, pattern: r(6), modifier: r(2) }));
      }
      expect(flatten(cs)).toEqual(flattenSlow(cs));
    }
  });

  it("beats is a strict order (never both ways)", () => {
    const a = cap(0, 5, 1), b = cap(0, 5, 1);
    expect(beats(a, b) && beats(b, a)).toBe(false);
    expect(beats(a, b) || beats(b, a)).toBe(true);
  });
});

describe("encode: spans as Monaco's semantic tokens", () => {
  const NONE = 9;

  it("delta-encodes, cuts a span at each line break, and leaves breaks out", () => {
    const text = "ab /* x\r\n y */\ncd";
    const lines = new Lines(text);
    const data = encode([{ start: 3, end: 14, type: 1, modifier: 2 }], lines, text, 0, 2, NONE);
    expect(decode(data)).toEqual([
      [0, 0, 3, NONE, 0],
      [0, 3, 4, 1, 4],
      [1, 0, 5, 1, 4],
      [2, 0, 2, NONE, 0],
    ]);
    expect(Array.from(data.slice(0, 15))).toEqual([0, 0, 3, NONE, 0, 0, 3, 4, 1, 4, 1, 0, 5, 1, 4]);
  });

  it("text no span covers is `none` unless it is blank", () => {
    const text = "  x  y\n\t\n";
    const lines = new Lines(text);
    expect(decode(encode([{ start: 2, end: 3, type: 1, modifier: -1 }], lines, text, 0, 1, NONE))).toEqual([
      [0, 2, 1, 1, 0],
      [0, 3, 3, NONE, 0],
    ]);
  });

  it("only the lines asked for, and none past the end", () => {
    const text = "a\nb\nc";
    const lines = new Lines(text);
    const spans = [{ start: 0, end: 5, type: 1, modifier: 0 }];
    expect(decode(encode(spans, lines, text, 1, 1, NONE))).toEqual([[1, 0, 1, 1, 1]]);
    expect(decode(encode(spans, lines, text, 2, 99, NONE))).toEqual([[2, 0, 1, 1, 1]]);
  });

  it("skips a line longer than LONG_LINE", () => {
    const text = `a\n${"x".repeat(LONG_LINE + 1)}\nb`;
    const lines = new Lines(text);
    const out = decode(encode([{ start: 0, end: text.length, type: 1, modifier: 0 }], lines, text, 0, 2, NONE));
    expect(out.map((t) => t[0])).toEqual([0, 2]);
  });

  it("astral characters count two, as Monaco counts them", () => {
    const text = "😀ab";
    const lines = new Lines(text);
    expect(decode(encode([{ start: 2, end: 4, type: 1, modifier: 0 }], lines, text, 0, 0, NONE))).toEqual([
      [0, 0, 2, NONE, 0],
      [0, 2, 2, 1, 1],
    ]);
  });
});
