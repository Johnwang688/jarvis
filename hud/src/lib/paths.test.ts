import { describe, expect, it } from "vitest";
import { basename, basenameOrPath, dirname } from "./paths";

// The table every inline copy these helpers replaced agreed with.
describe("basename", () => {
  const table: [string, string][] = [
    ["", ""],
    ["a", "a"],
    ["a/", ""],
    ["/a/b", "b"],
    ["a/b/", ""],
    ["dir/.env", ".env"],
    ["/", ""],
  ];
  for (const [input, want] of table) {
    it(`${JSON.stringify(input)} -> ${JSON.stringify(want)}`, () => expect(basename(input)).toBe(want));
  }
});

describe("basenameOrPath", () => {
  const table: [string, string][] = [
    ["", ""],
    ["a", "a"],
    ["a/", "a/"],
    ["/a/b", "b"],
    ["a/b/", "a/b/"],
    ["/", "/"],
  ];
  for (const [input, want] of table) {
    it(`${JSON.stringify(input)} -> ${JSON.stringify(want)}`, () => expect(basenameOrPath(input)).toBe(want));
  }
});

describe("dirname", () => {
  const table: [string, string][] = [
    ["", ""],
    ["a", ""],
    ["a/", "a"],
    ["/a", ""],
    ["/a/b", "/a"],
    ["a/b/", "a/b"],
    ["a/b/c.ts", "a/b"],
  ];
  for (const [input, want] of table) {
    it(`${JSON.stringify(input)} -> ${JSON.stringify(want)}`, () => expect(dirname(input)).toBe(want));
  }
  it("matches both inline copies it replaced on every string over a small alphabet", () => {
    const searchView = (p: string) => (p.lastIndexOf("/") > 0 ? p.slice(0, p.lastIndexOf("/")) : "");
    const parentOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
    const all = (n: number): string[] => (n === 0 ? [""] : all(n - 1).flatMap((s) => ["", "a", "/"].map((c) => s + c)));
    for (const p of all(5)) {
      expect(dirname(p)).toBe(searchView(p));
      expect(dirname(p)).toBe(parentOf(p));
    }
  });
});
