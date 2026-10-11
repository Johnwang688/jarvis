import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import source from "./editorTheme.ts?raw";
import { TokenMetadata } from "monaco-editor/editor/common/encodedTokenAttributes";
import { TokenTheme } from "monaco-editor/editor/common/languages/supports/tokenization";
import {
  CHROME, LEGEND, SCHEME, SYNTAX_TOKENS, TOKENS, chromeColor, contrast, hueSat, isWarm, parseToken,
  readPalette, themeColors, themeData, themeRules, type Palette,
} from "./editorTheme";

/** The declarations on theme.css's `:root`, comments stripped. */
function rootDeclarations(): Map<string, string> {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const block = /:root\s*\{([\s\S]*?)\n\}/.exec(text);
  if (!block) throw new Error("theme.css has no :root block");
  return new Map([...block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const decl = rootDeclarations();
const palette: Palette = readPalette((t) => decl.get(t) ?? "");
const BG = ["--bg-1", "--bg-2"] as const;

/** What Monaco's own matcher makes of a token type under the theme: colour and style. */
const tokenTheme = TokenTheme.createFromRawTokenTheme(themeData(palette).rules, []);
const colorMap = tokenTheme.getColorMap().map((c) => (c ? c.toString() : ""));
function resolve(token: string): { color: string; style: string } {
  const m = tokenTheme._match(token).metadata;
  const fs = TokenMetadata.getFontStyle(m);
  const style = [[2, "bold"], [1, "italic"], [4, "underline"], [8, "strikethrough"]]
    .filter(([bit]) => fs & (bit as number)).map(([, name]) => name).join(" ");
  return { color: colorMap[TokenMetadata.getForeground(m)], style };
}

describe("the --syn-* tokens and the table agree", () => {
  it("the four editor hues are on :root, with exactly the plan's values (§2.5)", () => {
    expect(decl.get("--syn-fn")).toBe("#8ec5d6");
    expect(decl.get("--syn-string")).toBe("#a6c79b");
    expect(decl.get("--syn-const")).toBe("#b9aade");
    expect(decl.get("--syn-type")).toBe("#a8b5c2");
  });

  it("every token the scheme or the chrome names is declared on :root as a colour", () => {
    for (const t of TOKENS) {
      expect(decl.has(t), t).toBe(true);
      expect(parseToken(decl.get(t)!), t).not.toBeNull();
      expect(palette[t], t).toBe(parseToken(decl.get(t)!));
    }
    for (const row of SCHEME) expect((SYNTAX_TOKENS as readonly string[]).includes(row.color), row.captures[0]).toBe(true);
    for (const row of CHROME) expect((TOKENS as readonly string[]).includes(row.token), row.key).toBe(true);
  });

  it("every rule and chrome colour is the declared token's value, so nothing is copied by hand", () => {
    const rules = themeRules(palette);
    for (const row of SCHEME) {
      for (const token of [...row.captures, ...row.monarch]) {
        const rule = rules.find((r) => r.token === token)!;
        expect("#" + rule.foreground, token).toBe(decl.get(row.color));
        expect(rule.fontStyle, token).toBe(row.style);
      }
    }
    const colors = themeColors(palette);
    for (const row of CHROME) expect(colors[row.key].slice(0, 7), row.key).toBe(parseToken(decl.get(row.token)!)!.slice(0, 7));
  });

  it("editorTheme.ts writes no colour of its own: no hex literal outside its comments", () => {
    const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(code.match(/#[0-9a-fA-F]{3,8}\b/g) ?? []).toEqual([]);
  });

  it("the palette refuses what is not a colour, naming the token", () => {
    expect(() => readPalette((t) => (t === "--syn-fn" ? "var(--accent)" : decl.get(t) ?? ""))).toThrow(/--syn-fn/);
    expect(() => readPalette((t) => (t === "--text" ? "#fff" : decl.get(t) ?? ""))).toThrow(/--text/);
    // A syntax colour must be opaque: its contrast is against the background alone.
    expect(() => readPalette((t) => (t === "--syn-type" ? "rgba(168, 181, 194, 0.5)" : decl.get(t) ?? "")))
      .toThrow(/--syn-type/);
    expect(() => readPalette(() => "")).toThrow(/--bg-1/);
    expect(parseToken(" rgba(255, 255, 255, 0.08) ")).toBe("#ffffff14");
    expect(parseToken("rgb(1, 2, 3)")).toBe("#010203");
    expect(parseToken("#8EC5D6")).toBe("#8ec5d6");
    expect(parseToken("rgba(300, 0, 0, 1)")).toBeNull();
  });
});

describe("contrast and hue, recomputed from theme.css", () => {
  it("the arithmetic is WCAG's", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#e2dfd7", "#1a1a19")).toBeCloseTo(13.1, 1);
  });

  it("every syntax colour is at least 4.5:1 on --bg-1 and on the current-line --bg-2", () => {
    for (const row of SCHEME) {
      for (const bg of BG) {
        const ratio = contrast(palette[row.color], palette[bg]);
        expect(ratio, `${row.color} (${row.captures[0]}) on ${bg}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the hue test knows amber, orange and red when it sees them, and the syntax hues when it does not", () => {
    for (const c of ["#ce9178", "#f44747", "#ffd700", "#cca700", "#ea5c00", "#fbbf24", "#f87171", "#d7ba7d", "#ee9d28"]) {
      expect(isWarm(c), c).toBe(true);
    }
    for (const c of ["#a6c79b", "#8ec5d6", "#b9aade", "#a8b5c2", "#e2dfd7", "#98948b"]) expect(isWarm(c), c).toBe(false);
    expect(hueSat("#ff0000").h).toBe(0);
  });

  it("no syntax colour's hue is in the amber (25°–65°) or red (340°–20°) band at saturation ≥ 0.35 (decisions E-4)", () => {
    for (const t of SYNTAX_TOKENS) {
      const { h, s } = hueSat(palette[t]);
      expect(isWarm(palette[t]), `${t} ${palette[t]}: ${h.toFixed(0)}° s ${s.toFixed(2)}`).toBe(false);
    }
    // And no colour any token can be drawn in: Monaco's whole token colour map.
    for (const c of colorMap.filter(Boolean)) expect(isWarm(c), c).toBe(false);
  });

  it("a token can only take a colour from the table: the colour map is the syntax tokens and the pane", () => {
    const allowed = new Set([...SYNTAX_TOKENS.map((t) => palette[t]), palette["--bg-1"]]);
    for (const c of colorMap.filter(Boolean)) expect(allowed.has(c), c).toBe(true);
  });
});

describe("every capture and Monarch token resolves to its row", () => {
  it("no token is in two rows, and every capture is a well-formed name", () => {
    const seen = new Map<string, string>();
    for (const row of SCHEME) {
      for (const token of [...row.captures, ...row.monarch]) {
        expect(seen.has(token), `${token} also in the row of ${seen.get(token)}`).toBe(false);
        seen.set(token, row.captures[0]);
      }
      for (const c of row.captures) expect(c, c).toMatch(/^[a-z]+(\.[a-z_]+)*$/);
    }
  });

  it("every legend type has a rule, and Monaco's own matcher resolves it to the row's colour and style", () => {
    expect(LEGEND.length).toBeGreaterThan(50);
    const rules = new Set(themeRules(palette).map((r) => r.token));
    for (const row of SCHEME) {
      for (const token of [...row.captures, ...row.monarch]) {
        expect(rules.has(token), token).toBe(true);
        expect(resolve(token), token).toEqual({ color: palette[row.color], style: row.style });
      }
    }
  });

  it("covers every capture the plan's table names", () => {
    const plan = [
      "variable", "variable.member", "variable.parameter", "property", "keyword", "keyword.directive",
      "keyword.import", "attribute", "function.macro", "function", "function.call", "function.method",
      "function.method.call", "constructor", "function.builtin", "type", "type.builtin", "type.definition",
      "module", "module.builtin", "string", "string.special", "character", "string.escape", "string.regexp",
      "character.special", "number", "number.float", "boolean", "constant", "constant.builtin",
      "variable.builtin", "operator", "punctuation.delimiter", "punctuation.bracket", "punctuation.special",
      "comment", "comment.documentation", "comment.todo", "comment.note", "comment.warning", "comment.error",
      "label", "tag", "tag.attribute", "tag.delimiter", "markup.heading", "markup.strong", "markup.italic",
      "markup.strikethrough", "markup.raw", "markup.link.label", "markup.link.url", "markup.quote",
      "markup.list", "error",
    ];
    for (const c of plan) expect(LEGEND.includes(c), c).toBe(true);
  });

  it("tokens as Monarch and the languages spell them land on the right row", () => {
    const fn = { color: palette["--syn-fn"], style: "" };
    const str = { color: palette["--syn-string"], style: "" };
    const cst = { color: palette["--syn-const"], style: "" };
    const typ = { color: palette["--syn-type"], style: "" };
    const kw = { color: palette["--text"], style: "bold" };
    const cases: [string, { color: string; style: string }][] = [
      ["keyword.if.cpp", kw], ["keyword.python", kw], ["keyword.flow.scss", kw],
      ["keyword.directive.include.cpp", typ], ["keyword.directive.include.begin.cpp", str],
      ["string.include.identifier.cpp", str], ["keyword.import.cpp", typ], ["keyword.import.python", kw],
      ["function.cpp", fn], ["function.builtin.python", { color: palette["--syn-fn"], style: "italic" }],
      ["type.builtin.cpp", typ], ["module.cpp", typ], ["type.identifier.ts", typ], ["type.yaml", typ],
      ["constant.builtin.python", cst], ["number.float.cpp", cst], ["number.hex.python", cst],
      ["keyword.json", cst], ["string.key.json", typ], ["key.ini", typ],
      ["string.cpp", str], ["string.escape.cpp", cst], ["string.escape.python", str], ["string.invalid.cpp", str],
      ["variable.builtin.python", { color: palette["--text"], style: "italic" }],
      ["comment.python", { color: palette["--text-muted"], style: "italic" }],
      ["comment.doc.cpp", { color: palette["--text-muted"], style: "italic" }],
      ["delimiter.parenthesis.cpp", { color: palette["--text-2"], style: "" }],
      ["tag.python", typ], ["tag.html", fn], ["attribute.name.html", typ], ["attribute.value.html", str],
      ["delimiter.html", { color: palette["--text-2"], style: "" }], ["annotation.cpp", typ],
      ["keyword.md", { color: palette["--syn-fn"], style: "bold" }], ["variable.md", str],
      ["variable.source.md", str], ["keyword.table.left.md", { color: palette["--text-2"], style: "" }],
      ["invalid", { color: palette["--text"], style: "" }], ["regexp.js", cst],
      ["identifier.python", { color: palette["--text"], style: "" }],
      // A token no rule names is the default: text, plain — never vs-dark's colour for it.
      ["nothing.anyone.named", { color: palette["--text"], style: "" }], ["", { color: palette["--text"], style: "" }],
    ];
    for (const [token, want] of cases) expect(resolve(token), token).toEqual(want);
  });
});

describe("the chrome", () => {
  it("every colour is a strict hex", () => {
    for (const r of themeRules(palette)) {
      expect(r.foreground, r.token).toMatch(/^[0-9a-f]{6}$/);
      if (r.background !== undefined) expect(r.background, r.token).toMatch(/^[0-9a-f]{6}$/);
    }
    for (const [k, v] of Object.entries(themeColors(palette))) expect(v, k).toMatch(/^#[0-9a-f]{6}([0-9a-f]{2})?$/);
  });

  it("no key is pinned twice", () => {
    expect(new Set(CHROME.map((r) => r.key)).size).toBe(CHROME.length);
  });

  it("the plan's pins: pane, current line, line numbers, cursor, selection, matches, brackets, errors", () => {
    const c = themeColors(palette);
    const p = palette;
    expect(c["editor.background"]).toBe(p["--bg-1"]);
    expect(c["editor.lineHighlightBackground"]).toBe(p["--bg-2"]);
    expect(c["editorLineNumber.foreground"]).toBe(p["--text-muted"]);
    expect(c["editorLineNumber.activeForeground"]).toBe(p["--text-2"]);
    expect(c["editorCursor.foreground"]).toBe(p["--accent-hi"]);
    expect(c["editor.selectionBackground"]).toBe(`${p["--accent"]}40`);
    expect(c["editor.inactiveSelectionBackground"]).toBe(`${p["--accent"]}21`);
    expect(c["editor.findMatchBackground"]).toBe(`${p["--accent"]}59`);
    expect(c["editor.findMatchBorder"]).toBe(p["--accent"]);
    expect(c["editor.findMatchHighlightBackground"]).toBe(`${p["--accent"]}26`);
    expect(c["editor.wordHighlightBackground"]).toBe("#ffffff14"); // white at 8%: --selected
    expect(c["editorBracketMatch.border"]).toBe(p["--text-muted"]);
    expect([1, 2, 3].map((n) => c[`editorBracketHighlight.foreground${n}`]))
      .toEqual([p["--syn-fn"], p["--syn-const"], p["--syn-type"]]);
    expect(c["editorUnicodeHighlight.border"]).toBe(p["--syn-const"]);
    expect(c["editorError.foreground"]).toBe(p["--red"]);
    expect(c["editorWarning.foreground"]).toBe(p["--text-2"]);
    expect(c["diffEditor.insertedTextBackground"]).toBe(`${p["--green"]}26`);
    expect(c["diffEditor.removedLineBackground"]).toBe(`${p["--red"]}12`);
    expect(c["editorWhitespace.foreground"]).toBe(p["--border-strong"]);
    expect(c["editorStickyScroll.background"]).toBe(p["--bg-1"]);
    expect(c["editorStickyScroll.border"]).toBe(p["--border"]);
  });

  // Every colour Monaco 0.57.0 resolves to orange, gold, amber or red under a
  // dark theme, read from the `--vscode-*` variables it publishes for its
  // whole registry with ED-1's build (the headless editor-scheme check reads
  // them live, and fails on any it finds warm and unpinned).
  const MONACO_WARM_DEFAULTS = [
    "activityErrorBadge.background", "activityWarningBadge.background", "charts.red",
    "diffEditor.moveActive.border", "diffEditor.removedLineBackground", "diffEditor.removedTextBackground",
    "editorError.foreground", "editorLightBulb.foreground", "editorLightBulbAi.foreground",
    "editorOverviewRuler.errorForeground", "errorForeground", "inputValidation.errorBackground",
    "inputValidation.errorBorder", "inputValidation.warningBackground", "inputValidation.warningBorder",
    "list.errorForeground", "list.invalidItemForeground", "list.warningForeground",
    "listFilterWidget.noMatchesOutline", "minimap.errorHighlight", "peekViewEditor.matchHighlightBackground",
    "peekViewResult.matchHighlightBackground", "symbolIcon.classForeground", "symbolIcon.enumeratorForeground",
    "symbolIcon.eventForeground", "textPreformat.foreground",
    // and the ones ED-1 had already moved: find, bracket pairs, the unicode box, warnings.
    "editor.findMatchBackground", "editor.findMatchHighlightBackground", "editorOverviewRuler.findMatchForeground",
    "editorBracketHighlight.foreground1", "editorBracketHighlight.unexpectedBracket.foreground",
    "editorUnicodeHighlight.border", "editorWarning.foreground", "editorOverviewRuler.warningForeground",
  ];

  it("every chrome key Monaco defaults to orange, gold, amber or red is pinned", () => {
    const pinned = new Set(CHROME.map((r) => r.key));
    for (const k of MONACO_WARM_DEFAULTS) expect(pinned.has(k), k).toBe(true);
  });

  it("and pinned off them, except where red means an error or a diff's 'removed'", () => {
    for (const row of CHROME) {
      const value = chromeColor(palette, row);
      if (row.red) {
        expect(row.token, row.key).toBe("--red");
        if (row.red === "removed") expect(parseInt(value.slice(7, 9), 16), row.key).toBeLessThan(0x40);
      } else if (!(row.alpha === 0)) {
        expect(isWarm(value), `${row.key} ${value}`).toBe(false);
      }
      // Red is only ever --red, and only where a row says why.
      if (row.token === "--red") expect(row.red, row.key).toBeDefined();
    }
  });
});
