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
//  - amber and red are meanings (a tool running or an approval pending; an error): an accent or a wash may
//    not enter their OKLCH hue band or come near them, and is refused with a reason, never shifted;
//  - the accent keeps 3:1 on the glass pane (D-8); a wash is darkened until its luminance is no more than
//    the default brightest wash's, so every D-8 floor holds as measured; the base is capped likewise.
// Fields this HUD does not know ride along untouched through a save.

import { readKey, writeKey } from "./storage";
import { deltaE, hexLch, hexRgb, hueDiff, lchHex, luminance, luminanceRgb, type Lch } from "./oklch";

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
  /** AG-3: the accent, `#rrggbb`. Absent: the default. Checked again (checkAccent) before it is applied. */
  accent?: string;
  /** AG-3: the three wash colours, `#rrggbb` each (--wash-1..3). Absent: the defaults. */
  washes?: string[];
  /** AG-3: the colour under the washes (--base), `#rrggbb`. Absent: the default. */
  base?: string;
}

export const DEFAULT_APPEARANCE: Appearance = { motion: "fluid", stir: true, activity: true };

// ---- colours: defaults and the guards --------------------------------------------------------------------

export const DEFAULT_ACCENT = "#6fc3df";
export const DEFAULT_WASHES = ["#2a8fa0", "#3468c4", "#6450c4"] as const;
export const DEFAULT_BASE = "#0a1116";
/** The meaning colours (theme.css, never changed): amber is pending, red is an error. */
export const AMBER = "#fbbf24";
export const RED = "#f87171";

/**
 * The hue band no accent or wash may enter, in OKLCH degrees. --red sits at 22 and --amber at 84; the band
 * runs 17 degrees below red and 21 above amber, so orange, coral, gold and yellow are all inside it.
 * (Lime at 110+ and rose/magenta at 340 and below are outside.)
 */
export const WARM_BAND = { from: 5, to: 105 } as const;
/** Below this OKLCH chroma an accent is a near-grey, and its hue angle means nothing: allowed. */
export const ACCENT_CHROMA_FLOOR = 0.04;
/** A wash is refused in the band only when it is this saturated (a dim, dull warm wash is just brown). */
export const WASH_CHROMA_FLOOR = 0.05;
/**
 * No accent or wash may come within this OKLab distance of --amber or --red, whatever its hue angle.
 * (Amber and red are 0.21 apart; 0.12 is about six just-noticeable differences and also catches the
 * pinks that sit just outside the hue band.)
 */
export const MEANING_DELTA_E = 0.12;
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
const WARM_REASON = "Too close to amber or red, which the HUD keeps for a tool running, an approval waiting and an error.";

function inBand(lch: Lch): boolean {
  const mid = (WARM_BAND.from + WARM_BAND.to) / 2, half = (WARM_BAND.to - WARM_BAND.from) / 2;
  return Math.abs(hueDiff(lch.h, mid)) <= half;
}
/** Is `hex` in the amber/orange/red band at this chroma, or near either meaning colour? */
export function impersonatesMeaning(hex: string, chromaFloor: number): boolean {
  const lch = hexLch(hex);
  if (lch.C >= chromaFloor && inBand(lch)) return true;
  return deltaE(hex, AMBER) < MEANING_DELTA_E || deltaE(hex, RED) < MEANING_DELTA_E;
}

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

/** The accent rule: format, then the amber/red guard, then 3:1 on the glass. Refused, never shifted. */
export function checkAccent(input: unknown, washes: readonly string[] = DEFAULT_WASHES): Check {
  const hex = normHex(input);
  if (!hex) return refuse(BAD_FORMAT);
  // Check the actual gamut-mapped, rounded relatives as well as the pick. The alpha tokens, ring,
  // badge and xterm cursor/cyan reuse `accent`; the orb also uses `hi` and `composing`.
  if (Object.values(accentColours(hex)).some((v) => impersonatesMeaning(v, ACCENT_CHROMA_FLOOR))) return refuse(WARM_REASON);
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
  if (impersonatesMeaning(hex, WASH_CHROMA_FLOOR)) return refuse(WARM_REASON);
  const value = clampLuminance(hex, cap);
  // Gamut mapping and 8-bit rounding can cross the hue band or the near-grey chroma floor.
  if (impersonatesMeaning(value, WASH_CHROMA_FLOOR)) return refuse(WARM_REASON);
  return value === hex
    ? { ok: true, value, clamped: false }
    : { ok: true, value, clamped: true, note: `Darkened to ${value} so text stays readable over the ${label}.` };
}

/** A wash: the amber/red guard, then clamped (not refused) to the default brightest wash's luminance. */
export const checkWash = (input: unknown): Check => checkDark(input, WASH_MAX_LUMINANCE, "aurora");
/** The base: the same guard, capped at 1.5x the default base's luminance. Darker is fine. */
export const checkBase = (input: unknown): Check => checkDark(input, BASE_MAX_LUMINANCE, "background");

export interface Colours {
  accent: string;
  washes: [string, string, string];
  base: string;
}
export const DEFAULT_COLOURS: Colours = { accent: DEFAULT_ACCENT, washes: [...DEFAULT_WASHES], base: DEFAULT_BASE };

/**
 * The colours to apply for a record: every stored value is checked again here, and one that fails (or is
 * missing) is its default. A stored wash is clamped, so an over-bright stored value still applies safely.
 */
export function resolveColours(a: Pick<Appearance, "accent" | "washes" | "base">): Colours {
  const washes = DEFAULT_WASHES.map((d, i) => {
    const r = checkWash(a.washes?.[i]);
    return r.ok ? r.value : d;
  }) as [string, string, string];
  const acc = checkAccent(a.accent, washes);
  const base = checkBase(a.base);
  return { accent: acc.ok ? acc.value : DEFAULT_ACCENT, washes, base: base.ok ? base.value : DEFAULT_BASE };
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
  const { hi, fn, composing } = accentColours(accent);
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
      "--syn-fn": fn,
    },
    orb: { base: triple(accent), hi: triple(hi), composing: triple(composing) },
  };
}

/** Every custom property a colour record sets on :root. */
export function colourProps(c: Colours): Record<string, string> {
  return {
    ...deriveAccent(c.accent).props,
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
  // Only well-formed `#rrggbb` is kept; the guards run in resolveColours, before anything is applied.
  const accent = normHex(r.accent);
  if (accent) out.accent = accent;
  const base = normHex(r.base);
  if (base) out.base = base;
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
  // A patch field set to undefined is a reset: it must also clear a stored copy, not just hide it.
  for (const k of ["accent", "washes", "base"] as const) {
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
