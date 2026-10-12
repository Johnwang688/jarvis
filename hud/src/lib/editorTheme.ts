// The Graphite editor scheme (ED-3; editor plan §2.5, decisions E-4): one
// colour scheme for Monaco, in the spirit of Neovim 0.10's default. Most code
// is the HUD's normal text colour, told apart by weight and slant; colour is
// kept for a few kinds of thing, in four quiet hues (`--syn-*`), and **no
// syntax colour is ever amber or red** — amber is the HUD's "waiting on you"
// and red its error, and an editor that borrowed either would teach the owner
// to ignore both.
//
// **One source of truth.** Every colour here is named by a HUD token, never
// written as hex: `cssPalette()` reads the tokens off `:root` (theme.css) when
// Monaco loads, and `themeData()` derives the hex Monaco needs from them.
// `editorTheme.test.ts` parses theme.css itself and recomputes every contrast
// and hue from the values there, so the file and the scheme cannot drift.
//
// **The table is keyed by nvim-treesitter capture names** (`captures`, without
// the `@`), which is what ED-5's tree-sitter colour will hand Monaco as
// semantic token types (`LEGEND`). The same rows style Monarch's coarser
// tokens (`monarch`), which stay underneath as the fallback. Monaco matches a
// token type against the rules by dotted prefix, the longest match winning, so
// `keyword` styles `keyword.if.cpp` and `string.escape.python` overrides
// `string.escape` for Python alone. A rule that holds only for some languages
// is written `<capture>.<language>`, Monarch's own postfix convention; ED-5
// gets the same effect by passing the model's language as the last modifier
// (Monaco joins a semantic token's type and modifiers with dots before
// matching them).

// ---- the palette -----------------------------------------------------------

/** The tokens a syntax colour may be: the HUD's three text greys and the four editor hues. */
export const SYNTAX_TOKENS = [
  "--text", "--text-2", "--text-muted", "--syn-fn", "--syn-string", "--syn-const", "--syn-type",
] as const;

/** The further tokens the editor's chrome is drawn in. */
export const CHROME_TOKENS = [
  "--bg-1", "--bg-2", "--bg-3", "--border", "--border-strong", "--selected",
  "--accent", "--accent-hi", "--red", "--green",
] as const;

export const TOKENS = [...SYNTAX_TOKENS, ...CHROME_TOKENS] as const;

export type SyntaxToken = (typeof SYNTAX_TOKENS)[number];
export type Token = (typeof TOKENS)[number];

/** Each token as a strict lower-case `#rrggbb`, or `#rrggbbaa` for one with alpha (`--selected`). */
export type Palette = Readonly<Record<Token, string>>;

const HEX = /^#([0-9a-f]{6})$/;
const RGBA = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(0|1|0?\.\d+)\s*)?\)$/;

const byte = (n: number) => Math.round(Math.min(255, Math.max(0, n))).toString(16).padStart(2, "0");

/**
 * One token's value as Monaco wants it. A hex token must be exactly `#rrggbb`
 * (lower case, no shorthand); an `rgb()`/`rgba()` one becomes `#rrggbb[aa]`.
 * Anything else is refused rather than guessed at.
 */
export function parseToken(value: string): string | null {
  const v = value.trim().toLowerCase();
  if (HEX.test(v)) return v;
  const m = RGBA.exec(v);
  if (!m) return null;
  const [r, g, b] = [m[1], m[2], m[3]].map(Number);
  if ([r, g, b].some((c) => c > 255)) return null;
  const a = m[4] === undefined ? 1 : Number(m[4]);
  return `#${byte(r)}${byte(g)}${byte(b)}${a < 1 ? byte(a * 255) : ""}`;
}

/** The palette from a reader of custom properties; throws naming every token it could not read. */
export function readPalette(read: (name: Token) => string): Palette {
  const out = {} as Record<Token, string>;
  const bad: string[] = [];
  for (const t of TOKENS) {
    const raw = read(t) ?? "";
    const v = parseToken(raw);
    // A syntax colour is opaque: its contrast is against the background alone.
    if (!v || ((SYNTAX_TOKENS as readonly string[]).includes(t) && v.length !== 7)) bad.push(`${t} (${JSON.stringify(raw.trim())})`);
    else out[t] = v;
  }
  if (bad.length) throw new Error(`editor theme: not a colour on :root: ${bad.join(", ")}`);
  return out;
}

/** The palette as the window draws it: the tokens on `:root`, from theme.css. */
export function cssPalette(root: Element = document.documentElement): Palette {
  const style = getComputedStyle(root);
  return readPalette((name) => style.getPropertyValue(name));
}

// ---- syntax: captures and Monarch tokens -> colour and style --------------

/** Monaco's `fontStyle`: "" is plain, and it is always written, so no rule inherits its parent's bold. */
export type FontStyle = "" | "bold" | "italic" | "underline" | "strikethrough";

export interface SchemeRow {
  /** nvim-treesitter capture names, without the `@`: ED-5's semantic token types. */
  readonly captures: readonly string[];
  /** Monarch token types styled the same way, beyond the capture names themselves. */
  readonly monarch: readonly string[];
  readonly color: SyntaxToken;
  readonly style: FontStyle;
  readonly example?: string;
}

/**
 * The plan's captures → colours table (§2.5), row for row. Monarch's tokens
 * mostly share the capture names (`keyword`, `string`, `comment`, `number`,
 * `type`, `tag`); the `monarch` lists are the ones that do not, and the
 * per-language exceptions. C, C++ and Python's Monarch tokenizers are refined
 * to emit `function`, `function.builtin`, `type.builtin`, `constant.builtin`,
 * `variable.builtin` and `module` (lib/monarchRefine.ts), so the fallback
 * tells those apart too.
 */
export const SCHEME: readonly SchemeRow[] = [
  { captures: ["variable", "variable.member", "variable.parameter", "property"],
    monarch: ["identifier"], color: "--text", style: "", example: "speed, self.x, int inches" },
  { captures: ["keyword", "keyword.function", "keyword.operator", "keyword.import", "keyword.type",
    "keyword.modifier", "keyword.repeat", "keyword.return", "keyword.conditional", "keyword.exception",
    "keyword.coroutine", "keyword.debug"],
  monarch: [], color: "--text", style: "bold", example: "if, return, def, class, const, import, for" },
  { captures: ["keyword.directive", "keyword.directive.define", "keyword.import.c", "keyword.import.cpp",
    "attribute", "attribute.builtin", "function.macro"],
  // C++'s `[[nodiscard]]`, HTML's doctype, Python's decorators, pragmas.
  monarch: ["annotation", "metatag", "tag.python", "keyword.pragma"],
  color: "--syn-type", style: "", example: '#include "vex.h"' },
  { captures: ["function", "function.call", "function.method", "function.method.call", "constructor"],
    monarch: [], color: "--syn-fn", style: "", example: "driveFor(…)" },
  { captures: ["function.builtin"], monarch: ["predefined"], color: "--syn-fn", style: "italic", example: "print, len" },
  { captures: ["type", "type.builtin", "type.definition", "module", "module.builtin"],
    monarch: [], color: "--syn-type", style: "", example: "int, motor, vex, std" },
  { captures: ["string", "string.special", "character", "string.documentation"],
    // HTML attribute values; Python's quote marks (Monarch calls them
    // `string.escape`); a C `#include`'s `<` `>` or quotes, so `<cmath>` reads
    // as one string.
    monarch: ["attribute.value", "string.escape.python", "keyword.directive.include.begin",
      "keyword.directive.include.end"],
    color: "--syn-string", style: "", example: '"Auton"' },
  { captures: ["string.escape", "string.regexp", "character.special"], monarch: ["regexp", "escape"],
    color: "--syn-const", style: "", example: "\\n" },
  { captures: ["number", "number.float", "boolean", "constant", "constant.builtin", "constant.macro"],
    // JSON's true/false/null (our own Monarch definition, lib/monaco.ts); CSS numbers and units.
    monarch: ["keyword.json", "attribute.value.number", "attribute.value.unit", "attribute.value.hex"],
    color: "--syn-const", style: "", example: "90.0, true, nullptr, None" },
  { captures: ["variable.builtin"], monarch: ["variable.predefined"], color: "--text", style: "italic", example: "self, this" },
  { captures: ["operator"], monarch: ["operators"], color: "--text-2", style: "", example: "+ = -> ::" },
  { captures: ["punctuation", "punctuation.delimiter", "punctuation.bracket", "punctuation.special"],
    monarch: ["delimiter", "constant.delimiter"], color: "--text-2", style: "", example: "; , ( ) { }" },
  { captures: ["comment", "comment.documentation"], monarch: [], color: "--text-muted", style: "italic" },
  // TODO, FIXME: never amber or red, unlike nvim's default.
  { captures: ["comment.todo", "comment.note", "comment.warning", "comment.error"], monarch: [],
    color: "--text", style: "bold", example: "TODO, FIXME" },
  { captures: ["label"], monarch: [], color: "--text-2", style: "", example: "case labels, goto" },
  { captures: ["tag"], monarch: [], color: "--syn-fn", style: "", example: "<div>" },
  { captures: ["tag.attribute"], monarch: ["attribute.name"], color: "--syn-type", style: "", example: "class=" },
  { captures: ["tag.delimiter"], monarch: ["delimiter.html", "delimiter.xml"], color: "--text-2", style: "", example: "< > />" },
  // Keys in JSON, YAML and TOML (YAML's Monarch already calls its keys `type`).
  { captures: ["property.json", "property.yaml", "property.toml"], monarch: ["string.key.json", "key"],
    color: "--syn-type", style: "", example: '"name":' },
  // Markdown's Monarch calls headings (and, coarsely, list markers) `keyword`.
  { captures: ["markup.heading"], monarch: ["keyword.md"], color: "--syn-fn", style: "bold", example: "# Plan" },
  { captures: ["markup.strong"], monarch: ["strong"], color: "--text", style: "bold" },
  { captures: ["markup.italic"], monarch: ["emphasis"], color: "--text", style: "italic" },
  { captures: ["markup.strikethrough"], monarch: [], color: "--text", style: "strikethrough" },
  // HTML's <u> (nvim's html_tags queries; ED-5).
  { captures: ["markup.underline"], monarch: [], color: "--text", style: "underline" },
  { captures: ["markup.raw", "markup.raw.block"], monarch: ["variable.md", "variable.source.md"],
    color: "--syn-string", style: "", example: "code spans and fences" },
  { captures: ["markup.link", "markup.link.label"], monarch: ["string.link.md"], color: "--syn-fn", style: "underline" },
  { captures: ["markup.link.url"], monarch: ["string.target.md"], color: "--text-muted", style: "underline" },
  { captures: ["markup.quote"], monarch: [], color: "--text-muted", style: "italic" },
  { captures: ["markup.list"], monarch: ["keyword.table", "meta.separator"], color: "--text-2", style: "", example: "-, 1." },
  // Parse errors are not underlined, as in nvim's default — and never red.
  { captures: ["error"], monarch: ["invalid"], color: "--text", style: "", example: "ERROR" },
];

/** Every capture the scheme styles: the semantic token types ED-5's legend may use. */
export const LEGEND: readonly string[] = SCHEME.flatMap((r) => r.captures);

export interface ThemeRule {
  token: string;
  foreground: string;
  background?: string;
  fontStyle: FontStyle;
}

const bare = (hex: string) => hex.slice(1, 7);

/** Monaco's token rules: the default (text on the pane) first, then every capture and Monarch token. */
export function themeRules(p: Palette): ThemeRule[] {
  const rules: ThemeRule[] = [{ token: "", foreground: bare(p["--text"]), background: bare(p["--bg-1"]), fontStyle: "" }];
  for (const row of SCHEME) {
    for (const token of [...row.captures, ...row.monarch]) {
      rules.push({ token, foreground: bare(p[row.color]), fontStyle: row.style });
    }
  }
  return rules;
}

// ---- chrome ----------------------------------------------------------------

export interface ChromeRow {
  readonly key: string;
  readonly token: Token;
  /** 0–1. Replaces the token's own alpha; absent keeps it (opaque, or `--selected`'s 8%). */
  readonly alpha?: number;
  /**
   * Set only where red is allowed, and then the token is `--red`: an error
   * (a squiggle, its ruler mark, an invalid regex in find), or a diff's
   * "removed" at low alpha. Everything else in the editor stays off amber
   * and red.
   */
  readonly red?: "error" | "removed";
}

/**
 * Editor chrome, pinned so nothing amber or red appears except errors (§2.5).
 * The second half pins every colour Monaco 0.57 resolves to orange, gold,
 * amber or red under a dark theme — found by reading the `--vscode-*`
 * variables Monaco publishes for its whole colour registry (the headless
 * editor-scheme check does the same and fails on any it finds unpinned), so
 * most of these are drawn by nothing the HUD loads today; pinned anyway, so
 * a feature turned on later cannot bring Monaco's orange with it.
 */
export const CHROME: readonly ChromeRow[] = [
  // Background, cursor, gutter.
  { key: "editor.background", token: "--bg-1" },
  { key: "editor.foreground", token: "--text" },
  { key: "editorGutter.background", token: "--bg-1" },
  { key: "editor.lineHighlightBackground", token: "--bg-2" },
  { key: "editor.lineHighlightBorder", token: "--bg-2", alpha: 0 },
  { key: "editorLineNumber.foreground", token: "--text-muted" },
  { key: "editorLineNumber.activeForeground", token: "--text-2" },
  { key: "editorCursor.foreground", token: "--accent-hi" },
  { key: "editorIndentGuide.background1", token: "--bg-3" },
  { key: "editorIndentGuide.activeBackground1", token: "--border-strong" },
  { key: "editorWhitespace.foreground", token: "--border-strong" },
  // Selection and matches. Monaco's find matches are orange.
  { key: "editor.selectionBackground", token: "--accent", alpha: 0.25 },
  { key: "editor.inactiveSelectionBackground", token: "--accent", alpha: 0.13 },
  { key: "editor.selectionHighlightBackground", token: "--accent", alpha: 0.15 },
  { key: "editor.findMatchBackground", token: "--accent", alpha: 0.35 },
  { key: "editor.findMatchBorder", token: "--accent" },
  { key: "editor.findMatchHighlightBackground", token: "--accent", alpha: 0.15 },
  { key: "editorOverviewRuler.findMatchForeground", token: "--accent", alpha: 0.6 },
  { key: "editor.wordHighlightBackground", token: "--selected" },
  { key: "editor.wordHighlightStrongBackground", token: "--selected" },
  { key: "editor.wordHighlightTextBackground", token: "--selected" },
  // Brackets. Monaco's first pair colour is gold, which reads as amber, and
  // an unmatched bracket is red; pairs cycle through the three syntax hues
  // (a transparent colour drops out of the cycle).
  { key: "editorBracketMatch.border", token: "--text-muted" },
  { key: "editorBracketMatch.background", token: "--bg-1", alpha: 0 },
  { key: "editorBracketHighlight.foreground1", token: "--syn-fn" },
  { key: "editorBracketHighlight.foreground2", token: "--syn-const" },
  { key: "editorBracketHighlight.foreground3", token: "--syn-type" },
  { key: "editorBracketHighlight.foreground4", token: "--bg-1", alpha: 0 },
  { key: "editorBracketHighlight.foreground5", token: "--bg-1", alpha: 0 },
  { key: "editorBracketHighlight.foreground6", token: "--bg-1", alpha: 0 },
  { key: "editorBracketHighlight.unexpectedBracket.foreground", token: "--text" },
  // The unicode highlighter's box is amber-ish by default.
  { key: "editorUnicodeHighlight.border", token: "--syn-const" },
  // Errors are red, as squiggles; warnings are never amber.
  { key: "editorError.foreground", token: "--red", red: "error" },
  { key: "editorOverviewRuler.errorForeground", token: "--red", alpha: 0.7, red: "error" },
  { key: "editorWarning.foreground", token: "--text-2" },
  { key: "editorOverviewRuler.warningForeground", token: "--text-2" },
  // Diff: red here means "removed", the universal convention; the low alpha
  // keeps it from reading as an error.
  { key: "diffEditor.insertedTextBackground", token: "--green", alpha: 0.15 },
  { key: "diffEditor.insertedLineBackground", token: "--green", alpha: 0.07 },
  { key: "diffEditor.removedTextBackground", token: "--red", alpha: 0.15, red: "removed" },
  { key: "diffEditor.removedLineBackground", token: "--red", alpha: 0.07, red: "removed" },
  { key: "diffEditor.diagonalFill", token: "--bg-3" },
  { key: "diffEditor.moveActive.border", token: "--accent" },
  // Sticky scroll: the pane's own colour, ruled off.
  { key: "editorStickyScroll.background", token: "--bg-1" },
  { key: "editorStickyScroll.border", token: "--border" },
  { key: "editorStickyScrollHover.background", token: "--bg-2" },
  // Widgets (find, suggest, hover) and scrollbars.
  { key: "editorWidget.background", token: "--bg-2" },
  { key: "editorWidget.border", token: "--border-strong" },
  { key: "scrollbarSlider.background", token: "--selected" },
  { key: "scrollbarSlider.hoverBackground", token: "--selected", alpha: 0.13 },
  { key: "scrollbarSlider.activeBackground", token: "--selected", alpha: 0.19 },

  // ---- the rest of Monaco's warm defaults --------------------------------
  // Find's "No results" and a list filter's "no matches" are not errors.
  { key: "errorForeground", token: "--text-2" },
  { key: "listFilterWidget.noMatchesOutline", token: "--border-strong" },
  // An invalid regular expression in find is: a red border, on the widget's own background.
  { key: "inputValidation.errorBorder", token: "--red", red: "error" },
  { key: "inputValidation.errorBackground", token: "--bg-2" },
  { key: "inputValidation.warningBorder", token: "--text-2" },
  { key: "inputValidation.warningBackground", token: "--bg-2" },
  { key: "list.errorForeground", token: "--red", red: "error" },
  { key: "list.warningForeground", token: "--text-2" },
  { key: "list.invalidItemForeground", token: "--text-2" },
  { key: "minimap.errorHighlight", token: "--red", alpha: 0.7, red: "error" },
  { key: "activityErrorBadge.background", token: "--red", red: "error" },
  { key: "activityWarningBadge.background", token: "--bg-3" },
  { key: "charts.red", token: "--red", red: "error" },
  // Code spans in hovers (the unicode highlighter's explanation): `@markup.raw`.
  { key: "textPreformat.foreground", token: "--syn-string" },
  // No code actions are loaded, so no light bulb; never a yellow one.
  { key: "editorLightBulb.foreground", token: "--text-2" },
  { key: "editorLightBulbAi.foreground", token: "--text-2" },
  { key: "peekViewEditor.matchHighlightBackground", token: "--accent", alpha: 0.35 },
  { key: "peekViewResult.matchHighlightBackground", token: "--accent", alpha: 0.15 },
  { key: "symbolIcon.classForeground", token: "--syn-type" },
  { key: "symbolIcon.enumeratorForeground", token: "--syn-const" },
  { key: "symbolIcon.eventForeground", token: "--syn-fn" },
];

/** One chrome row's colour: `#rrggbb`, or `#rrggbbaa` when it has alpha. */
export function chromeColor(p: Palette, row: ChromeRow): string {
  const v = p[row.token];
  if (row.alpha === undefined) return v;
  return v.slice(0, 7) + byte(row.alpha * 255);
}

export function themeColors(p: Palette): Record<string, string> {
  return Object.fromEntries(CHROME.map((row) => [row.key, chromeColor(p, row)]));
}

/**
 * The theme Monaco is given. `inherit: false`, so nothing of vs-dark's
 * remains: a token no rule names is drawn in the default (`--text`), never in
 * a colour this file does not list. `vs-dark` is still the base, which tells
 * Monaco's colour registry to resolve the chrome this does not pin with its
 * dark defaults.
 */
export function themeData(p: Palette) {
  return { base: "vs-dark" as const, inherit: false, rules: themeRules(p), colors: themeColors(p) };
}

/**
 * Plain vs-dark, for a window whose tokens could not be read: Monaco still
 * loads (an editor without its scheme beats a textarea without an editor).
 */
export const FALLBACK_THEME = { base: "vs-dark" as const, inherit: true, rules: [] as ThemeRule[], colors: {} };

/**
 * The theme Monaco is given, never a throw. A theme that threw used to reject
 * `loadMonaco`, which every editor answers with its plain-text fallback and
 * no word of why, until a reload (review of PR #45, LOW-2). Now the reason —
 * naming each token that could not be read — goes to the console, and Monaco
 * loads in plain vs-dark.
 */
export function themeOrFallback(palette: () => Palette, log: (message: string) => void = (m) => console.error(m)) {
  try {
    return themeData(palette());
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e);
    log(`${why}; the editor falls back to plain vs-dark until the window reloads`);
    return FALLBACK_THEME;
  }
}

// ---- colour arithmetic (the tests' and the headless check's rules) ----------

export function rgbOf(hex: string): [number, number, number] {
  const v = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)) as [number, number, number];
}

/** WCAG 2 contrast ratio between two opaque colours. */
export function contrast(a: string, b: string): number {
  const lum = (hex: string) => {
    const [r, g, bl] = rgbOf(hex).map((c) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
}

/** HSL hue in degrees and saturation 0–1. */
export function hueSat(hex: string): { h: number; s: number } {
  const [r, g, b] = rgbOf(hex).map((c) => c / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
  if (d === 0) return { h: 0, s: 0 };
  const s = d / (1 - Math.abs(2 * l - 1));
  let h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h *= 60;
  return { h: h < 0 ? h + 360 : h, s };
}

/**
 * The plan's bands (§2.5): amber 25°–65° and red 340°–20°, at saturation
 * ≥ 0.35. The orange between them (20°–25°) is caught too — Monaco's own
 * find-match colour, #ea5c00, sits at 24° — so the test is one warm band,
 * 340°–65°.
 */
export const WARM = { from: 340, to: 65, saturation: 0.35 } as const;

export function isWarm(hex: string): boolean {
  const { h, s } = hueSat(hex);
  return s >= WARM.saturation && (h >= WARM.from || h <= WARM.to);
}
