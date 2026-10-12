// @vitest-environment node
// Offsets, lines and points (ED-5): the unit is the UTF-16 code unit on both
// sides, Monaco breaks lines at \r\n, \n and \r, tree-sitter's rows at \n —
// pinned against the runtime itself, not only against our own idea of it.

import { Parser, Query } from "web-tree-sitter";
import { applyChange, Lines, pointAt, queryRange, QUERY_BYTES_PER_UNIT, TsLines } from "./positions";
import { language } from "./testkit";

const SAMPLES = [
  "",
  "a",
  "x = '😀'\ny = 1\n",
  "a\r\nb\r\n\r\nc",
  "\ttab\t\tx\n\t\n",
  "𝔘𝔫𝔦\r\n😀😀\n\r\nend",
  "lone\rcr\n",
  "\n\n\n",
  "no newline at end 𝄞",
];

describe("Monaco's lines", () => {
  it("round-trip every offset through (line, character), astral characters, CRLF and tabs included", () => {
    for (const text of SAMPLES) {
      const lines = new Lines(text);
      for (let off = 0; off <= text.length; off++) {
        const { line, character } = lines.position(off);
        expect(lines.offset(line, character)).toBe(off);
        expect(character).toBeLessThanOrEqual(off - lines.start(line));
      }
    }
  });

  it("break where Monaco does, and a line's length leaves its break out", () => {
    const text = "a\r\nbb\nccc\rdddd";
    const lines = new Lines(text);
    expect(lines.count).toBe(4);
    expect([0, 1, 2, 3].map((l) => text.slice(lines.start(l), lines.end(l)))).toEqual(["a", "bb", "ccc", "dddd"]);
    expect([0, 1, 2, 3].map((l) => lines.length(l))).toEqual([1, 2, 3, 4]);
    const empty = new Lines("a\r\n\r\nb");
    expect(empty.length(1)).toBe(0);
  });

  it("count code units, not code points or columns: a surrogate pair is two, a tab one", () => {
    const lines = new Lines("😀\tx");
    expect(lines.position(3)).toEqual({ line: 0, character: 3 });
    expect(lines.length(0)).toBe(4);
  });
});

describe("tree-sitter's points", () => {
  it("match the runtime's own node positions, on CRLF, astral characters and tabs", async () => {
    const lang = await language("python");
    const parser = new Parser();
    parser.setLanguage(lang);
    for (const text of ["x = '😀😀'\r\n\ty = 'é'\r\nz = 1\n", "a = 1\n\n\tb = '𝔘'\n", "s = '\\t'\r\n"]) {
      const tree = parser.parse(text)!;
      const ts = new TsLines(text);
      const walk = (n: any) => {
        expect(ts.point(n.startIndex)).toEqual(n.startPosition);
        expect(ts.point(n.endIndex)).toEqual(n.endPosition);
        expect(ts.offset(n.startPosition)).toBe(n.startIndex);
        expect(pointAt(text, n.endIndex)).toEqual(n.endPosition);
        // The runtime's indices are code units: its text is ours, sliced.
        expect(n.text).toBe(text.slice(n.startIndex, n.endIndex));
        for (let i = 0; i < n.childCount; i++) walk(n.child(i));
      };
      walk(tree.rootNode);
      tree.delete();
    }
    parser.delete();
  });

  it("a \\r is the last column of its row, not a break", () => {
    const ts = new TsLines("a\r\nb\rc");
    expect(ts.point(1)).toEqual({ row: 0, column: 1 });
    expect(ts.point(3)).toEqual({ row: 1, column: 0 });
    expect(ts.point(5)).toEqual({ row: 1, column: 2 });
  });
});

describe("applyChange", () => {
  it("gives the new text and the edit's indices and points, in code units", () => {
    const text = "ab😀\r\ncd\n";
    const { text: next, edit } = applyChange(text, { offset: 2, length: 4, text: "X\nY😀" });
    expect(next).toBe("abX\nY😀cd\n");
    expect(edit).toEqual({
      startIndex: 2, oldEndIndex: 6, newEndIndex: 7,
      startPosition: { row: 0, column: 2 }, oldEndPosition: { row: 1, column: 0 }, newEndPosition: { row: 1, column: 3 },
    });
  });

  it("clamps a change to the text", () => {
    expect(applyChange("abc", { offset: 10, length: 5, text: "z" }).text).toBe("abcz");
    expect(applyChange("abc", { offset: 1, length: 99, text: "" }).text).toBe("a");
  });

  it("an incremental reparse after the edit equals a fresh parse (CRLF and astral)", async () => {
    const lang = await language("python");
    const parser = new Parser();
    parser.setLanguage(lang);
    let text = "def f():\r\n    return '😀'\r\n";
    let tree = parser.parse(text)!;
    for (const c of [{ offset: 10, length: 0, text: "\t# 𝔘\r\n" }, { offset: 0, length: 3, text: "async def" }]) {
      const r = applyChange(text, c);
      const { Edit } = await import("web-tree-sitter");
      tree.edit(new Edit(r.edit));
      text = r.text;
      const next = parser.parse(text, tree)!;
      tree.delete();
      tree = next;
      const fresh = parser.parse(text)!;
      expect(tree.rootNode.toString()).toBe(fresh.rootNode.toString());
      fresh.delete();
    }
    tree.delete();
    parser.delete();
  });
});

describe("a query's range is in bytes of UTF-16 (web-tree-sitter 0.27)", () => {
  it("queryRange doubles it, and asking in code units would find the wrong half", async () => {
    const lang = await language("python");
    const parser = new Parser();
    parser.setLanguage(lang);
    const text = "a = 1\nb = 2\nc = 3\nd = 4\n";
    const tree = parser.parse(text)!;
    const q = new Query(lang, "(identifier) @x");
    const found = (o: object) => q.captures(tree.rootNode, o).map((c) => c.node.text).join("");
    const c = text.indexOf("c");
    expect(QUERY_BYTES_PER_UNIT).toBe(2);
    expect(found(queryRange(c, c + 5))).toBe("c");
    // If this starts passing, the runtime converts for itself: drop the doubling.
    expect(found({ startIndex: c, endIndex: c + 5 })).not.toBe("c");
    tree.delete();
    parser.delete();
  });
});
