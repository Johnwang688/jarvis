import { describe, expect, it } from "vitest";
import { CHROME, INTERIM_RULES, NEXT_MATCH_KEY, editorKeyRules, isMarkdown, type MonacoKeys } from "./monaco";

/** Hue in degrees and HSL saturation of a #rrggbb or #rrggbbaa colour (or a bare rrggbb). */
function hsl(hex: string): { h: number; s: number } {
  const v = hex.replace("#", "").slice(0, 6);
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
  if (d === 0) return { h: 0, s: 0 };
  const s = d / (1 - Math.abs(2 * l - 1));
  let h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h *= 60;
  return { h: h < 0 ? h + 360 : h, s };
}

/**
 * The plan's bands (§2.5) are amber 25°–65° and red 340°–20°, at saturation
 * ≥ 0.35 — which leaves orange between them: Monaco's own find-match colour,
 * #ea5c00, sits at 24°. One band, 340°–65°, so orange is caught too.
 */
const warm = (hex: string) => {
  const { h, s } = hsl(hex);
  return s >= 0.35 && (h >= 340 || h <= 65);
};

describe("the editor's interim colours (until ED-3's Graphite scheme)", () => {
  it("the hue test itself knows amber and red when it sees them", () => {
    for (const c of ["#ce9178", "#f44747", "#ffd700", "#cca700", "#ea5c00"]) expect(warm(c)).toBe(true);
    for (const c of ["#a6c79b", "#8ec5d6", "#b9aade", "#a8b5c2", "#e2dfd7"]) expect(warm(c)).toBe(false);
  });

  it("no syntax colour is amber or red (decisions E-4)", () => {
    for (const r of INTERIM_RULES) {
      expect(r.foreground, r.token).toMatch(/^[0-9a-f]{6}$/);
      expect(warm(r.foreground!), r.token).toBe(false);
    }
  });

  it("each vs-dark token colour E-4 rules out is overridden", () => {
    const moved = new Set(INTERIM_RULES.map((r) => r.token));
    for (const t of ["string", "string.sql", "string.value.json", "attribute.value", "meta.tag", "keyword.json",
      "metatag", "annotation", "invalid"]) expect(moved.has(t), t).toBe(true);
  });

  it("every chrome key Monaco paints orange, gold or amber is pinned off them", () => {
    for (const k of ["editor.findMatchBackground", "editor.findMatchHighlightBackground",
      "editorOverviewRuler.findMatchForeground", "editorBracketHighlight.foreground1",
      "editorBracketHighlight.unexpectedBracket.foreground", "editorUnicodeHighlight.border",
      "editorWarning.foreground", "editorOverviewRuler.warningForeground"]) {
      expect(CHROME[k], k).toMatch(/^#[0-9a-f]{6}([0-9a-f]{2})?$/);
      expect(warm(CHROME[k]), k).toBe(false);
    }
  });

  it("red stays where it means 'removed' in a diff, at low alpha", () => {
    for (const k of ["diffEditor.removedTextBackground", "diffEditor.removedLineBackground"]) {
      expect(parseInt(CHROME[k].slice(7, 9), 16)).toBeLessThan(0x40);
    }
  });
});

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
