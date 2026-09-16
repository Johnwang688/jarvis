import { describe, expect, it } from "vitest";
import { churn, hasAfterSide, statusLabel, summaryLine, totals } from "./difflist";
import type { DiffFile } from "../types";

const FILES: DiffFile[] = [
  { path: "a.py", status: "M", additions: 10, deletions: 3 },
  { path: "b.py", status: "A", additions: 40, deletions: 0 },
  { path: "c.py", status: "D", additions: 0, deletions: 12 },
];

describe("diff list rendering", () => {
  it("names every status", () => {
    expect(statusLabel("A")).toBe("added");
    expect(statusLabel("M")).toBe("modified");
    expect(statusLabel("D")).toBe("deleted");
    expect(statusLabel("R")).toBe("renamed");
  });

  it("passes an unknown status through rather than blanking it", () => {
    expect(statusLabel("T")).toBe("T");
  });

  it("counts churn per file and in total", () => {
    expect(churn(FILES[0])).toBe("+10 −3");
    expect(totals(FILES)).toEqual({ files: 3, additions: 50, deletions: 15 });
  });

  it("states truncation instead of hiding it", () => {
    // A diff the owner believes is whole when it is not is how a review misses
    // the file that mattered.
    expect(summaryLine(FILES, false)).toBe("3 files · +50 −15");
    expect(summaryLine(FILES, true)).toMatch(/TRUNCATED/);
  });

  it("singularises one file", () => {
    expect(summaryLine([FILES[0]])).toMatch(/^1 file · /);
  });

  it("knows a deleted file has no after side", () => {
    expect(hasAfterSide(FILES[2])).toBe(false);
    expect(hasAfterSide(FILES[1])).toBe(true);
  });
});
