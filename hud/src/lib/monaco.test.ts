import { describe, expect, it } from "vitest";
import { NEXT_MATCH_KEY, editorKeyRules, isMarkdown, type MonacoKeys } from "./monaco";

// The editor's colours are ED-3's Graphite scheme now, tested as data in
// editorTheme.test.ts (ED-1's interim moves off amber and red are gone with it).

describe("markdown files", () => {
  it("are known by extension", () => {
    expect(isMarkdown("README.md")).toBe(true);
    expect(isMarkdown("notes.MARKDOWN")).toBe(true);
    expect(isMarkdown("calc.py")).toBe(false);
  });
});

// Monaco's own numbers (KeyMod and KeyCode), so a rule built here is the rule
// Monaco gets.
const CTRL = 2048, SHIFT = 1024, D = 34;
const KEYS: MonacoKeys = { KeyMod: { CtrlCmd: CTRL, Shift: SHIFT }, KeyCode: { KeyD: D } };

describe("Monaco's keys beside push-to-talk (PR #40)", () => {
  const rules = editorKeyRules(KEYS);

  it("moves add-selection-to-next-match off Ctrl+D onto Ctrl+Shift+D", () => {
    expect(rules).toEqual([
      { keybinding: CTRL | D, command: "-editor.action.addSelectionToNextFindMatch" },
      { keybinding: CTRL | SHIFT | D, command: "editor.action.addSelectionToNextFindMatch", when: "editorFocus" },
    ]);
    expect(NEXT_MATCH_KEY).toBe("Ctrl+Shift+D");
  });

  it("binds nothing new to plain Ctrl+D, and leaves every chord to Monaco", () => {
    const onCtrlD = rules.filter((r) => r.keybinding === (CTRL | D));
    expect(onCtrlD.every((r) => r.command.startsWith("-"))).toBe(true);
    expect(rules.some((r) => r.keybinding > 0xffff)).toBe(false);
  });
});
