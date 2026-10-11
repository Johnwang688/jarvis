import { describe, expect, it } from "vitest";
import { NEXT_MATCH_KEY, editorKeyRules, type MonacoKeys } from "./monaco";

// Monaco's own numbers (monaco-editor 0.52's KeyMod and KeyCode), so a rule
// built here is the rule Monaco gets.
const CTRL = 2048, SHIFT = 1024, D = 34, K = 41;
const M: MonacoKeys = {
  KeyMod: { CtrlCmd: CTRL, Shift: SHIFT, chord: (a, b) => (a | ((b & 0xffff) << 16)) >>> 0 },
  KeyCode: { KeyD: D, KeyK: K },
};
const chord = M.KeyMod.chord;

describe("Monaco's keys beside push-to-talk", () => {
  const rules = editorKeyRules(M);

  it("moves add-selection-to-next-match off Ctrl+D onto Ctrl+Shift+D", () => {
    expect(rules).toContainEqual({ keybinding: CTRL | D, command: "-editor.action.addSelectionToNextFindMatch" });
    expect(rules).toContainEqual({
      keybinding: CTRL | SHIFT | D, command: "editor.action.addSelectionToNextFindMatch", when: "editorFocus",
    });
    expect(NEXT_MATCH_KEY).toBe("Ctrl+Shift+D");
  });

  it("moves the Ctrl+K Ctrl+D chord too, whose second key is push-to-talk", () => {
    expect(rules).toContainEqual({
      keybinding: chord(CTRL | K, CTRL | D), command: "-editor.action.moveSelectionToNextFindMatch",
    });
    expect(rules).toContainEqual({
      keybinding: chord(CTRL | K, CTRL | SHIFT | D), command: "editor.action.moveSelectionToNextFindMatch",
      when: "editorFocus",
    });
  });

  it("binds nothing new to plain Ctrl+D: every rule on it is a removal", () => {
    const onCtrlD = rules.filter((r) => r.keybinding === (CTRL | D) || r.keybinding === chord(CTRL | K, CTRL | D));
    expect(onCtrlD.length).toBe(2);
    expect(onCtrlD.every((r) => r.command.startsWith("-"))).toBe(true);
  });
});
