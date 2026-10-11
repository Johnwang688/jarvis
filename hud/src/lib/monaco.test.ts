import { describe, expect, it } from "vitest";
import { isMarkdown } from "./monaco";

// The editor's colours are ED-3's Graphite scheme now, tested as data in
// editorTheme.test.ts (ED-1's interim moves off amber and red are gone with it).

describe("markdown files", () => {
  it("are known by extension", () => {
    expect(isMarkdown("README.md")).toBe(true);
    expect(isMarkdown("notes.MARKDOWN")).toBe(true);
    expect(isMarkdown("calc.py")).toBe(false);
  });
});
