// The editor's settings (ED-8, editor plan §2.8): one record for every editor
// in the window, kept in localStorage `jarvis.hud.editor` behind try/catch.
// Pure, so it is tested without a window; `state/editorSettings.ts` holds the
// live copy and tells the editors.
//
// **Garbage in any field falls back to that field's default, alone**: a
// stored record from an older build, a hand-edited one, a field of the wrong
// type — each field is judged by itself, so one bad value never costs the
// others. Nothing here is ever sent anywhere; it is the owner's taste, not
// state Jarvis reads.
//
// Decided, and not settings (decisions E-9): **auto-save is off and nothing
// formats on save.** `.editorconfig` (lib/editorconfig.ts) sets indentation
// and new-line endings for the files it matches and never rewrites one.

export const SETTINGS_KEY = "jarvis.hud.editor";

export type WordWrap = "off" | "on";
export type Whitespace = "none" | "boundary" | "selection" | "trailing" | "all";
export type CursorStyle = "line" | "block" | "underline" | "line-thin" | "block-outline" | "underline-thin";

export const WHITESPACE: readonly Whitespace[] = ["none", "boundary", "selection", "trailing", "all"];
export const CURSOR_STYLES: readonly CursorStyle[] = ["line", "block", "underline", "line-thin", "block-outline", "underline-thin"];
export const FONT_SIZE_MIN = 8;
export const FONT_SIZE_MAX = 32;
export const TAB_SIZE_MIN = 1;
export const TAB_SIZE_MAX = 16;
/** A font list longer than this is refused: nobody types 200 characters of font names. */
export const FONT_FAMILY_MAX = 200;
const FAMILIES_MAX = 8;

export interface EditorSettings {
  /** A CSS font-family list, checked by `safeFontFamily`; "" is Monaco's own default. */
  fontFamily: string;
  fontSize: number;
  /** The tab size a file without its own indentation (or `.editorconfig`) gets. */
  tabSize: number;
  /** Spaces (true) or tabs, for a file without its own indentation (or `.editorconfig`). */
  insertSpaces: boolean;
  /** Word wrap in code. */
  wordWrap: WordWrap;
  /** Word wrap in Markdown: on, as prose wants. */
  wordWrapMarkdown: WordWrap;
  minimap: boolean;
  stickyScroll: boolean;
  whitespace: Whitespace;
  bracketPairs: boolean;
  cursorStyle: CursorStyle;
  /** Vim keybindings (decisions E-11): off unless the owner turns them on. */
  vim: boolean;
}

/**
 * What a window with nothing stored draws: what the editor drew before ED-8
 * (Monaco's own font, size 12 as ED-1 set it, sticky scroll, whitespace on the
 * selection, bracket-pair colours, a line cursor), with the minimap off,
 * Markdown wrapped and vim off.
 */
export const DEFAULTS: Readonly<EditorSettings> = Object.freeze({
  fontFamily: "",
  fontSize: 12,
  tabSize: 4,
  insertSpaces: true,
  wordWrap: "off",
  wordWrapMarkdown: "on",
  minimap: false,
  stickyScroll: true,
  whitespace: "selection",
  bracketPairs: true,
  cursorStyle: "line",
  vim: false,
});

// One family: a bare name (letters first — `-apple-system` too — then
// letters, digits, spaces, `_`, `.`, `-`), or the same characters quoted.
// Nothing that ends a CSS value or starts a function: no `;`, `{`, `}`,
// `(`, `)`, `\`, `/`, `:`, `<`, `>`, `@`, `!`, no control or invisible
// character, no nested or unbalanced quote.
const BARE = /^-?[A-Za-z][A-Za-z0-9 _.-]*$/;
const QUOTED = /^(["'])([A-Za-z0-9 _.-]+)\1$/;

/**
 * A font-family list made only of the safe characters, normalised
 * (`"Cascadia Mono", Consolas, monospace`); "" for the default; null when it
 * is not one. Monaco writes the value into the editor's styles, and this
 * window draws approval cards, so nothing but a list of names gets through.
 */
export function safeFontFamily(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (value.length > FONT_FAMILY_MAX) return null;
  const trimmed = value.trim();
  if (!trimmed) return "";
  const parts = trimmed.split(",").map((p) => p.trim());
  if (parts.length > FAMILIES_MAX) return null;
  const out: string[] = [];
  for (const part of parts) {
    if (!part) return null;
    if (QUOTED.test(part)) {
      const m = QUOTED.exec(part)!;
      const inner = m[2].trim();
      if (!inner) return null;
      out.push(`"${inner}"`);
    } else if (BARE.test(part)) {
      out.push(part.replace(/\s+/g, " "));
    } else {
      return null;
    }
  }
  return out.join(", ");
}

const intIn = (lo: number, hi: number) => (v: unknown): number | null =>
  typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi ? v : null;
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
const oneOf = <T extends string>(allowed: readonly T[]) => (v: unknown): T | null =>
  typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : null;
const WRAPS = ["off", "on"] as const;

/** Each field's own check: its value as stored, or null when it is not one. */
const FIELDS: { [K in keyof EditorSettings]: (v: unknown) => EditorSettings[K] | null } = {
  fontFamily: safeFontFamily,
  fontSize: intIn(FONT_SIZE_MIN, FONT_SIZE_MAX),
  tabSize: intIn(TAB_SIZE_MIN, TAB_SIZE_MAX),
  insertSpaces: bool,
  wordWrap: oneOf(WRAPS),
  wordWrapMarkdown: oneOf(WRAPS),
  minimap: bool,
  stickyScroll: bool,
  whitespace: oneOf(WHITESPACE),
  bracketPairs: bool,
  cursorStyle: oneOf(CURSOR_STYLES),
  vim: bool,
};

const KEYS = Object.keys(DEFAULTS) as (keyof EditorSettings)[];

/**
 * Settings from whatever was stored: a JSON string, an object, or nothing.
 * Every field is checked alone and falls back to its own default.
 */
export function parseSettings(raw: unknown): EditorSettings {
  let obj: unknown = raw;
  if (typeof raw === "string") {
    try {
      obj = JSON.parse(raw);
    } catch {
      obj = null;
    }
  }
  const o: Record<string, unknown> = obj && typeof obj === "object" && !Array.isArray(obj)
    ? (obj as Record<string, unknown>) : {};
  const out = { ...DEFAULTS } as Record<keyof EditorSettings, unknown>;
  for (const k of KEYS) {
    if (!Object.prototype.hasOwnProperty.call(o, k)) continue;
    const v = (FIELDS[k] as (v: unknown) => unknown)(o[k]);
    if (v !== null) out[k] = v;
  }
  return out as unknown as EditorSettings;
}

/**
 * A change from the dialog, held to the same rules: a field the change gets
 * wrong (or one that is not a setting) is dropped and keeps its current value.
 */
export function withChange(current: EditorSettings, change: Record<string, unknown>): EditorSettings {
  const out = { ...current } as Record<keyof EditorSettings, unknown>;
  for (const k of KEYS) {
    if (!Object.prototype.hasOwnProperty.call(change, k)) continue;
    const v = (FIELDS[k] as (v: unknown) => unknown)(change[k]);
    if (v !== null) out[k] = v;
  }
  return out as unknown as EditorSettings;
}

export function sameSettings(a: EditorSettings, b: EditorSettings): boolean {
  return KEYS.every((k) => a[k] === b[k]);
}

/** What is stored: only the known fields, so nothing else rides along. */
export function serialize(s: EditorSettings): string {
  const out: Record<string, unknown> = {};
  for (const k of KEYS) out[k] = s[k];
  return JSON.stringify(out);
}

type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;

const storageOf = (s?: Storage | null): Storage | null => {
  if (s !== undefined) return s;
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
};

/** The stored settings, or the defaults when storage is missing, blocked or holds garbage. */
export function loadSettings(storage?: Storage | null): EditorSettings {
  const s = storageOf(storage);
  let raw: string | null = null;
  try {
    raw = s ? s.getItem(SETTINGS_KEY) : null;
  } catch {
    raw = null;
  }
  return parseSettings(raw);
}

/** Store them; false when storage refused (a private window, a full quota). */
export function saveSettings(settings: EditorSettings, storage?: Storage | null): boolean {
  const s = storageOf(storage);
  if (!s) return false;
  try {
    s.setItem(SETTINGS_KEY, serialize(settings));
    return true;
  } catch {
    return false;
  }
}

/**
 * The options a File pane's editor takes from the settings. `markdown` picks
 * the word wrap; `fontFamily: undefined` is Monaco's own default (an empty
 * setting must put the default back, not keep the last font).
 */
export function editorOptions(s: EditorSettings, ctx: { markdown: boolean }): Record<string, unknown> {
  return {
    ...displayOptions(s),
    tabSize: s.tabSize,
    insertSpaces: s.insertSpaces,
    wordWrap: ctx.markdown ? s.wordWrapMarkdown : s.wordWrap,
    minimap: { enabled: s.minimap },
  };
}

/** What a diff or Compare view takes: how text is drawn, never how it is typed or wrapped. */
export function displayOptions(s: EditorSettings): Record<string, unknown> {
  return {
    fontFamily: s.fontFamily || undefined,
    fontSize: s.fontSize,
    stickyScroll: { enabled: s.stickyScroll },
    renderWhitespace: s.whitespace,
    bracketPairColorization: { enabled: s.bracketPairs },
    cursorStyle: s.cursorStyle,
  };
}
