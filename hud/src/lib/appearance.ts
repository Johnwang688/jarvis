// The HUD's per-window appearance preferences (AG-2: motion and stirring; AG-3: the colours).
//
// Stored in `localStorage['jarvis.hud.appearance']` as JSON, read and written behind try/catch, a corrupt
// record falling back to the defaults field by field. A per-window convenience: nothing is sent to the
// daemon and nothing in it is trusted (every field is validated on read, and again before it is applied).
//
// AG-3 adds `accent`, `washes` (three) and `base`, all `#rrggbb`. The rules that keep a custom colour from
// breaking the HUD live here, pure and unit-tested (appearance.test.ts); lib/appearanceApply.ts only
// writes the result to `:root`. Those rules are tighten-only boundaries:
//  - only `#rrggbb` ever reaches a custom property (no url(), var(), names, shorthand);
//  - the accent keeps 3:1 on the glass pane (D-8); a wash is darkened until its luminance is no more than
//    the default brightest wash's, so every D-8 floor holds as measured; the base is capped likewise.
// Any hue is allowed for every owner colour, amber, red, orange, gold and yellow included (owner 2026-11-14,
// replacing the amber/red band guard and its whole-palette blend proof): the HUD's own meaning colours are
// theme.css's --amber and --red, which a pick never changes. The editor and terminal colour (`codeAccent`)
// is its own setting, judged exactly like the accent. RGB mode (owner 2026-11-13) cycles the aurora around
// the full wheel with a white accent.
// Fields this HUD does not know ride along untouched through a save.

import { readKey, writeKey } from "./storage";
import { hexLch, hexRgb, hueDiff, lchHex, luminance, luminanceRgb } from "./oklch";

export const APPEARANCE_KEY = "jarvis.hud.appearance";
/** Fired on `window` after a change is saved, so the aurora (and AG-3's colours) re-read at once. */
export const APPEARANCE_EVENT = "hud-appearance-change";

export const MOTIONS = ["fluid", "flow", "still"] as const;
export type Motion = (typeof MOTIONS)[number];

export interface Appearance {
  /** Fluid (default), the lighter Flow, or Still (one frame). */
  motion: Motion;
  /** The pointer stirs the fluid (default on). */
  stir: boolean;
  /** A chat ripples the fluid while it streams a reply (default on). */
  activity: boolean;
  /** Natural drift strength, 0..2 (default 1): how lively the aurora is at rest. Fluid and Flow only. */
  drift: number;
  /** AG-3: the accent, `#rrggbb`. Absent: the default. Checked again (checkAccent) before it is applied. */
  accent?: string;
  /** AG-3: the three wash colours, `#rrggbb` each (--wash-1..3). Absent: the defaults. */
  washes?: string[];
  /** AG-3: the colour under the washes (--base), `#rrggbb`. Absent: the default. */
  base?: string;
  /** The editor's and terminal's accent, `#rrggbb` (--code-accent). Absent: the default. Same guards as `accent`. */
  codeAccent?: string;
  /** RGB mode: the aurora cycles the full colour wheel, the accent is white. Only a stored `true` is on. */
  rgb: boolean;
  /** Seconds per full RGB cycle, 8..120 (default 30). */
  rgbSpeed: number;
}

export const DEFAULT_APPEARANCE: Appearance = { motion: "fluid", stir: true, activity: true, drift: 1, rgb: false, rgbSpeed: 30 };

/** A stored RGB cycle time: a finite number clamped to 8..120 seconds; anything else is 30. */
export function clampRgbSpeed(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? Math.min(120, Math.max(8, v)) : 30;
}

/** A stored drift strength: a finite number clamped to 0..2; anything else is 1 (today's drift). */
export function clampDrift(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? Math.min(2, Math.max(0, v)) : 1;
}

// ---- colours: defaults and the guards --------------------------------------------------------------------

export const DEFAULT_ACCENT = "#6fc3df";
/** The editor's and terminal's accent starts as the same cyan, but is its own setting. */
export const DEFAULT_CODE_ACCENT = DEFAULT_ACCENT;
export const DEFAULT_WASHES = ["#2a8fa0", "#3468c4", "#6450c4"] as const;
export const DEFAULT_BASE = "#0a1116";
/** The meaning colours (theme.css, never changed): amber is pending, red is an error. */
export const AMBER = "#fbbf24";
export const RED = "#f87171";

/** The accent must reach this contrast on the glass pane over the brightest wash (D-8). */
export const ACCENT_MIN_CONTRAST = 3;
/** A wash may not be brighter than the default brightest wash (D-8's floors were measured with it). */
export const WASH_MAX_LUMINANCE = luminance(DEFAULT_WASHES[0]);
/** The base may be at most this many times as luminous as the default base. */
export const BASE_LUMINANCE_FACTOR = 1.5;
export const BASE_MAX_LUMINANCE = luminance(DEFAULT_BASE) * BASE_LUMINANCE_FACTOR;

/** The pane's paint (theme.css --glass-pane and the peak of --glass-sheen), pinned by glass tests. */
const PANE_TINT = [11, 19, 25, 0.5] as const;
const SHEEN_PEAK = 0.09;

/** `#rrggbb` (either case) as lower-case `#rrggbb`, else null. Nothing else is ever a colour here. */
export function normHex(v: unknown): string | null {
  return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : null;
}

export type Refusal = { ok: false; reason: string };
export type Accepted = { ok: true; value: string; clamped: boolean; note?: string };
export type Check = Accepted | Refusal;

const refuse = (reason: string): Refusal => ({ ok: false, reason });
const BAD_FORMAT = "Use a colour written as #rrggbb.";
const rounded2 = (n: number) => (Math.round(n * 100) / 100).toFixed(2);

/** The glass pane as drawn over a wash: the tint, then the sheen's peak. 0-255 channels, unrounded. */
function paneOver(wash: string): [number, number, number] {
  const w = hexRgb(wash);
  return [0, 1, 2].map((i) => {
    const tinted = PANE_TINT[i] * PANE_TINT[3] + w[i] * (1 - PANE_TINT[3]);
    return 255 * SHEEN_PEAK + tinted * (1 - SHEEN_PEAK);
  }) as [number, number, number];
}
/** The pane's luminance over the brightest of these washes and the default brightest one. */
function worstPaneLuminance(washes: readonly string[]): number {
  return Math.max(...[...washes, DEFAULT_WASHES[0]].map((w) => luminanceRgb(paneOver(w))));
}
/** WCAG contrast of an accent on the worst-case glass pane. */
export function accentContrast(accent: string, washes: readonly string[] = DEFAULT_WASHES): number {
  const a = luminance(accent), p = worstPaneLuminance(washes);
  return (Math.max(a, p) + 0.05) / (Math.min(a, p) + 0.05);
}

/** The accent rule: format, then 3:1 on the glass, for any hue (owner 2026-11-14). Refused, never shifted. */
export function checkAccent(input: unknown, washes: readonly string[] = DEFAULT_WASHES): Check {
  const hex = normHex(input);
  if (!hex) return refuse(BAD_FORMAT);
  const c = accentContrast(hex, washes);
  if (c < ACCENT_MIN_CONTRAST) {
    return refuse(`Too dark to read on the glass (${rounded2(c)}:1; it needs ${ACCENT_MIN_CONTRAST}:1). Pick a lighter colour.`);
  }
  return { ok: true, value: hex, clamped: false };
}

/** Lower OKLCH lightness (hue and chroma kept, gamut-mapped) until `hex`'s luminance is at most `cap`. */
export function clampLuminance(hex: string, cap: number): string {
  if (luminance(hex) <= cap) return hex;
  const lch = hexLch(hex);
  let lo = 0, hi = lch.L;
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (luminance(lchHex({ ...lch, L: mid })) <= cap) lo = mid; else hi = mid;
  }
  // 8-bit rounding can land a hair over: step down until it does not.
  let out = lchHex({ ...lch, L: lo });
  for (let L = lo; luminance(out) > cap && L > 0; L -= 0.002) out = lchHex({ ...lch, L });
  return out;
}

function checkDark(input: unknown, cap: number, label: string): Check {
  const hex = normHex(input);
  if (!hex) return refuse(BAD_FORMAT);
  const value = clampLuminance(hex, cap);
  return value === hex
    ? { ok: true, value, clamped: false }
    : { ok: true, value, clamped: true, note: `Darkened to ${value} so text stays readable over the ${label}.` };
}

/** A wash: any hue, clamped (not refused) to the default brightest wash's luminance. */
export const checkWash = (input: unknown): Check => checkDark(input, WASH_MAX_LUMINANCE, "aurora");
/** The base: any hue, capped at 1.5x the default base's luminance. Darker is fine. */
export const checkBase = (input: unknown): Check => checkDark(input, BASE_MAX_LUMINANCE, "background");

export interface Colours {
  accent: string;
  washes: [string, string, string];
  base: string;
  /** The editor's and terminal's accent. */
  codeAccent: string;
}
export const DEFAULT_COLOURS: Colours = {
  accent: DEFAULT_ACCENT, washes: [...DEFAULT_WASHES], base: DEFAULT_BASE, codeAccent: DEFAULT_CODE_ACCENT,
};

// ---- RGB mode ---------------------------------------------------------------------------------------------
//
// Three washes 120 degrees apart rotate around the whole OKLCH wheel (L 0.55, C 0.14 sit inside sRGB at every
// hue), each darkened to the D-8 luminance cap. The 360 colours (one per degree of hue) are made once, so a
// phase is a table lookup. The accent is white and the base a dark neutral, both inside their own caps.

/** RGB mode's accent: the near-white of Moonlight (chroma under the accent floor, so no hue can matter). */
export const RGB_ACCENT = "#e8edf3";
/** RGB mode's background under the washes: a dark neutral, inside BASE_MAX_LUMINANCE. */
export const RGB_BASE = "#0a0b10";
const RGB_LIGHTNESS = 0.55, RGB_CHROMA = 0.14;
/** The wash at each whole degree of hue (index = degree); `rgbWashes` and the renderer's palette table share it. */
export const RGB_TABLE: readonly string[] = Array.from({ length: 360 }, (_, deg) =>
  clampLuminance(lchHex({ L: RGB_LIGHTNESS, C: RGB_CHROMA, h: deg }), WASH_MAX_LUMINANCE));

/** The table index (whole degrees) of a phase, 0..1 a full turn: it wraps either way, and a non-finite one is 0. */
export function rgbDegree(phase: number): number {
  if (!Number.isFinite(phase)) return 0;
  const f = phase - Math.floor(phase);
  return Math.min(359, Math.floor(f * 360));
}
/** The three washes at `phase` of a cycle: hues 120 degrees apart, every one under the wash luminance cap. */
export function rgbWashes(phase: number): [string, string, string] {
  const d = rgbDegree(phase);
  return [RGB_TABLE[d]!, RGB_TABLE[(d + 120) % 360]!, RGB_TABLE[(d + 240) % 360]!];
}

/**
 * Ready-made palettes for Settings -> Appearance. A preset is only a shortcut for picking all five colours at
 * once: it is saved through the same guards as a hand pick (checkPreset), and appearance.test.ts holds every
 * one to them unclamped, so a preset never needs darkening. The first is
 * the default; choosing it clears the stored colours, exactly as Reset does.
 */
export interface ColourPreset {
  id: string;
  name: string;
  accent: string;
  washes: readonly [string, string, string];
  base: string;
}
export const COLOUR_PRESETS: readonly ColourPreset[] = [
  { id: "aurora", name: "Aurora", accent: DEFAULT_ACCENT, washes: DEFAULT_WASHES, base: DEFAULT_BASE },
  { id: "moonlight", name: "Moonlight", accent: "#e8edf3", washes: ["#3e5266", "#2c3d55", "#4a4f68"], base: "#0a0d12" },
  { id: "graphite", name: "Graphite", accent: "#e5e7eb", washes: ["#3a3f48", "#2a2e35", "#1e2126"], base: "#0e0f12" },
  { id: "deep-blue", name: "Deep blue", accent: "#7fb2ff", washes: ["#1e3a7a", "#143063", "#0f2147"], base: "#070e1f" },
  { id: "northern-lights", name: "Northern lights", accent: "#5eead4", washes: ["#4a2391", "#0e6b76", "#1b2a6b"], base: "#0b0d20" },
  { id: "emerald", name: "Emerald", accent: "#6ee7b7", washes: ["#1f8a5e", "#1d6f78", "#2b5a9e"], base: "#08130f" },
  { id: "glacier", name: "Glacier", accent: "#a5f3fc", washes: ["#2a7f9a", "#368cac", "#2b5f8a"], base: "#081218" },
  { id: "nebula", name: "Nebula", accent: "#c4a8ff", washes: ["#5b3fb8", "#7a3aa8", "#3a46b8"], base: "#0d0b1a" },
  { id: "orchid", name: "Orchid", accent: "#e48cf5", washes: ["#7a2f9a", "#4b3ab0", "#8a2f7e"], base: "#120a17" },
  { id: "pink", name: "Pink", accent: "#f9a8d4", washes: ["#9a2f6e", "#6a2f8a", "#7a2f7e"], base: "#140a12" },
];

/** A preset through every colour guard: null when it may be saved as it stands, else the first refusal. */
export function checkPreset(p: ColourPreset): Refusal | null {
  for (const w of p.washes) {
    const r = checkWash(w);
    if (!r.ok) return r;
    if (r.clamped) return refuse(r.note ?? "A preset colour would need darkening.");
  }
  const b = checkBase(p.base);
  if (!b.ok) return b;
  if (b.clamped) return refuse(b.note ?? "A preset background would need darkening.");
  const a = checkAccent(p.accent, p.washes);
  return a.ok ? null : a;
}

/** The preset these colours are, if any (compared after resolveColours, so lower-case `#rrggbb`). */
export function matchingPreset(c: Colours): ColourPreset | undefined {
  return COLOUR_PRESETS.find((p) => p.accent === c.accent && p.base === c.base && p.washes.every((w, i) => w === c.washes[i]));
}

/**
 * The colours to apply for a record: every stored value is checked again here, and one that fails (or is
 * missing) is its default. A stored wash is clamped, so an over-bright stored value still applies safely.
 * Under RGB the accent, washes (at phase 0) and base are RGB mode's own, whatever else is stored; the code
 * accent is kept (judged like the accent) either way.
 */
export function resolveColours(a: Partial<Pick<Appearance, "accent" | "washes" | "base" | "codeAccent" | "rgb">>): Colours {
  let out: { accent: string; washes: [string, string, string]; base: string };
  if (a.rgb === true) {
    out = { accent: RGB_ACCENT, washes: rgbWashes(0), base: RGB_BASE };
  } else {
    const washes = DEFAULT_WASHES.map((d, i) => {
      const r = checkWash(a.washes?.[i]);
      return r.ok ? r.value : d;
    }) as [string, string, string];
    const baseCheck = checkBase(a.base);
    const base = baseCheck.ok ? baseCheck.value : DEFAULT_BASE;
    // The accent is judged against the washes that will actually be shown.
    const accent = checkAccent(a.accent, washes);
    out = { accent: accent.ok ? accent.value : DEFAULT_ACCENT, washes, base };
  }
  // The editor and terminal colour, judged like the accent against the washes that will be shown.
  const code = checkAccent(a.codeAccent, out.washes);
  return { ...out, codeAccent: code.ok ? code.value : DEFAULT_CODE_ACCENT };
}

// ---- the accent family, derived in OKLCH ------------------------------------------------------------------

/** How `to` sits against `from` in OKLCH: a lightness step, a chroma ratio and a hue step. */
function relation(from: string, to: string) {
  const a = hexLch(from), b = hexLch(to);
  return { dL: b.L - a.L, kC: b.C / a.C, dh: hueDiff(b.h, a.h) };
}
/** The default cyan's own relationships to its family, so the default pick reproduces today's values. */
const REL = {
  hi: relation(DEFAULT_ACCENT, "#9ad6ea"),     // --accent-hi, the orb's lighter step
  fn: relation(DEFAULT_ACCENT, "#8ec5d6"),     // --syn-fn, a quieter sibling
  compose: relation(DEFAULT_ACCENT, "#7dcdc8"), // the orb's "composing" hue, a step toward green
};
function sibling(accent: string, r: { dL: number; kC: number; dh: number }): string {
  const c = hexLch(accent);
  return lchHex({ L: Math.min(0.98, c.L + r.dL), C: c.C * r.kC, h: (c.h + r.dh + 360) % 360 });
}
/** All pigments used by the derived tokens, including the orb's hue-shifted composing state. */
function accentColours(accent: string) {
  return { accent, hi: sibling(accent, REL.hi), fn: sibling(accent, REL.fn), composing: sibling(accent, REL.compose) };
}
const rgba = (hex: string, a: number) => {
  const [r, g, b] = hexRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
};
const triple = (hex: string) => hexRgb(hex).join(",");

export interface AccentFamily {
  /** Custom properties for :root. */
  props: Record<string, string>;
  /** The orb's cool hues, "r,g,b" (lib/orbpalette.ts). */
  orb: { base: string; hi: string; composing: string };
}

/** Everything the accent colours: one pick, the rest derived. */
export function deriveAccent(accent: string): AccentFamily {
  const { hi, composing } = accentColours(accent);
  return {
    props: {
      "--accent": accent,
      "--accent-hi": hi,
      "--accent-soft": rgba(accent, 0.14),
      "--accent-line": rgba(accent, 0.45),
      "--accent-sel": rgba(accent, 0.3),
      "--accent-hover": rgba(accent, 0.22),
      "--accent-badge": rgba(accent, 0.4),
      "--focus-ring": `0 0 0 2px ${accent}`,
      "--badge-fast": accent,
    },
    orb: { base: triple(accent), hi: triple(hi), composing: triple(composing) },
  };
}

/** Everything the editor and terminal colour: one pick, the rest derived (the same relations as the accent's). */
export function deriveCodeAccent(codeAccent: string): Record<string, string> {
  const { hi, fn } = accentColours(codeAccent);
  return { "--code-accent": codeAccent, "--code-accent-hi": hi, "--syn-fn": fn };
}

/** Every custom property a colour record sets on :root. */
export function colourProps(c: Colours): Record<string, string> {
  return {
    ...deriveAccent(c.accent).props,
    ...deriveCodeAccent(c.codeAccent),
    "--wash-1": c.washes[0], "--wash-2": c.washes[1], "--wash-3": c.washes[2],
    "--base": c.base, "--bg-0": c.base,
  };
}

// ---- storage ------------------------------------------------------------------------------------------------

/** The preferences from a stored string; any field that is missing or wrong is its default. Never throws. */
export function parseAppearance(raw: string | null): Appearance {
  const out: Appearance = { ...DEFAULT_APPEARANCE };
  if (!raw) return out;
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch {
    return out;
  }
  if (typeof v !== "object" || v === null || Array.isArray(v)) return out;
  const r = v as Record<string, unknown>;
  if (typeof r.motion === "string" && (MOTIONS as readonly string[]).includes(r.motion)) out.motion = r.motion as Motion;
  if (typeof r.stir === "boolean") out.stir = r.stir;
  if (typeof r.activity === "boolean") out.activity = r.activity;
  out.drift = clampDrift(r.drift);
  // Only well-formed `#rrggbb` is kept; the guards run in resolveColours, before anything is applied.
  const accent = normHex(r.accent);
  if (accent) out.accent = accent;
  const base = normHex(r.base);
  if (base) out.base = base;
  const codeAccent = normHex(r.codeAccent);
  if (codeAccent) out.codeAccent = codeAccent;
  out.rgb = r.rgb === true;
  out.rgbSpeed = clampRgbSpeed(r.rgbSpeed);
  if (Array.isArray(r.washes) && r.washes.length === 3) {
    const w = r.washes.map(normHex);
    if (w.some((x) => x)) out.washes = w.map((x, i) => x ?? DEFAULT_WASHES[i]);
  }
  return out;
}

export function loadAppearance(storage?: Pick<Storage, "getItem">): Appearance {
  return parseAppearance(readKey(APPEARANCE_KEY, storage));
}

/** Saves `patch` over what is stored (so unknown-to-this-HUD fields are not lost) and tells the window. */
export function saveAppearance(patch: Partial<Appearance>, storage?: Pick<Storage, "getItem" | "setItem">): Appearance {
  const raw = readKey(APPEARANCE_KEY, storage);
  // Fields this HUD does not know (a later one's) ride along untouched, never read or applied here.
  let unknown: Record<string, unknown> = {};
  try {
    const v: unknown = raw ? JSON.parse(raw) : null;
    if (typeof v === "object" && v !== null && !Array.isArray(v)) unknown = v as Record<string, unknown>;
  } catch {
    /* corrupt: start from the defaults */
  }
  const next = { ...loadAppearance(storage), ...patch };
  if ("drift" in patch) next.drift = clampDrift(patch.drift);
  if ("rgbSpeed" in patch) next.rgbSpeed = clampRgbSpeed(patch.rgbSpeed);
  // A patch field set to undefined is a reset: it must also clear a stored copy, not just hide it.
  for (const k of ["accent", "washes", "base", "codeAccent"] as const) {
    if (k in patch && patch[k] === undefined) { delete next[k]; delete unknown[k]; }
  }
  writeKey(APPEARANCE_KEY, JSON.stringify({ ...unknown, ...next }), storage);
  try {
    window.dispatchEvent(new Event(APPEARANCE_EVENT));
  } catch {
    /* no window: nothing is listening */
  }
  return next;
}
