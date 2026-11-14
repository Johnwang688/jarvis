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
// Two things sit outside the amber/red rule, each for one reason:
//  - RGB mode (owner 2026-11-13) cycles the aurora around the FULL wheel, red, orange and yellow included,
//    like an RGB keyboard. That loosens the band for the aurora background in RGB mode only: the accent stays
//    white, every wash keeps the D-8 luminance cap, and nothing hand-picked or in a preset is exempt.
//  - the editor and terminal colour (`codeAccent`) is its own setting, judged exactly like the accent.
// Fields this HUD does not know ride along untouched through a save.

import { readKey, writeKey } from "./storage";
import { deltaE, hexLch, hexRgb, hueDiff, lchHex, linearToLab, luminance, luminanceRgb, rgbToLab, type Lch } from "./oklch";

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

// ---- the whole palette: what the background can actually show --------------------------------------------
//
// Each wash passes the guard alone, but the aurora shows BLENDS of the base and the washes, and a blend of
// an olive and a magenta can land in the red band. So the guard runs over every colour the background can
// display. Two mixing paths, each weight 0..1 independently (the cube, a superset of what either draws):
//  - the shaders (aurora/palette.ts `displayColor`): wash 3, then 2, then 1 mixed over the base in LINEAR
//    light, then to sRGB, then the dither darkens every channel by up to one 8-bit step;
//  - the CSS fallback (theme.css #aurora): three radial gradients over `--base`, alpha-over in sRGB space,
//    each alpha 0..1, wash 1 on top: the same nested mix in gamma space. The dither range is applied here
//    too (tighten-only: browsers dither gradients as well).
//
// Sampling a grid is not enough (a finer grid only moves the gap: the second miss was at weight 0.03, between
// 0.05 grid points near a dark base). This check is a proof, not a sample. It covers the 4-D box of weights
// w1, w2, w3 and dither d with cells, and for each cell computes a box that is GUARANTEED to hold the OKLab
// colour of every point in the cell:
//  1. per channel, the mix is multilinear in (w1, w2, w3) (a nested lerp), so its range over a cell is exactly
//     the min and max of its 8 corners;
//  2. every later step is monotone per channel (to sRGB, minus the dither, clamp, then half an 8-bit step
//     either way for the screen's rounding, to linear), and the LMS matrix has only positive entries, so
//     each LMS range is the matrix applied to the ends; the cube root is monotone; and L, a, b are linear in the roots, so each takes its min and max at the matching ends
//     (interval arithmetic: an over-estimate, never an under-estimate); that box is intersected with a
//     tighter mean-value one (cellBox: the centre's exact colour plus the most the cell can move it);
//  3. the cell is SAFE when that box provably misses the forbidden set: the box in (a, b) does not meet the
//     band's sector at chroma >= the floor (an exact test of the box: the farthest point of box-within-sector
//     is a box corner or where a band edge leaves the box), or its whole L is below DARK_L (below), and its
//     OKLab distance to amber and to red is at least MEANING_DELTA_E.
// A cell that is not proved safe has its centre tested exactly (a hit refuses at once: it is a real colour the
// background shows), and is otherwise split in two along the axis that moves its colour most. A cell still
// unproved at the smallest size, or more cells than the budget, REFUSES the palette: it never accepts on a guess.
//
// DARK_L: near black the cube root is steep and a box of LMS roots is far wider than the colours in it, so a
// cell at black is proved by step 3's chroma test only after very deep splitting (the default washes over
// #000000 took about 16,900 cells without DARK_L, 1,000 with it). But OKLab is homogeneous: scaling a
// linear colour by t scales L, a and b by t^(1/3), so C/L and hue are constant along each ray from black, and
// every colour in the sRGB cube lies on a ray to its far faces. On those faces C/L in the band (hue 5 to 105)
// peaks at 0.4104 (pure red; appearancePalette.test.ts re-measures it on a 1/1000 grid). With K_BAND = 0.42,
// any in-gamut colour with L < WASH_CHROMA_FLOOR / 0.42 = 0.119 has chroma below the floor, so is not in the
// band, whatever its hue; and it is far from amber (L 0.84) and red (L 0.71).
//
// The screen's 8-bit rounding is covered too: every channel may land half a step either side (cellBox), and a
// hit counts on the rounded colour as well as the computed one. Not modelled: the fluid's 0.95 weight scale and
// simplex (subsets of the cube). Refused, never shifted.

const PALETTE_REASON =
  "Together with your other aurora colours this would mix into amber or red, which the HUD keeps for a tool running, an approval waiting and an error.";

const srgbLin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const linSrgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);
const clamp01 = (c: number) => (c < 0 ? 0 : c > 1 ? 1 : c);
const AMBER_LAB = rgbToLab(hexRgb(AMBER)), RED_LAB = rgbToLab(hexRgb(RED));
const BAND_MID = (WARM_BAND.from + WARM_BAND.to) / 2, BAND_HALF = (WARM_BAND.to - WARM_BAND.from) / 2;

/** The shader's dither: every channel darkened by up to one 8-bit step (aurora/palette.ts DITHER). */
export const PALETTE_DITHER = 1 / 255;
/** The screen's 8-bit output: each channel lands within half a step of the computed one (verifier MED). */
export const PALETTE_ROUNDING = 0.5 / 255;
/** The most C/L any in-band colour of the sRGB cube reaches (0.4104 measured, with margin). */
export const K_BAND = 0.42;
/** Below this OKLab L, no in-gamut colour reaches the chroma floor inside the band. */
export const DARK_L = WASH_CHROMA_FLOOR / K_BAND;
/** Cells never get narrower than this (weights) or this (dither); unproved there: refused. */
export const PALETTE_MIN_CELL = 2 ** -20;
const MIN_DITHER_CELL = 2 ** -10;
/** Cells examined per mixing path before the check gives up and refuses. */
export const PALETTE_CELL_BUDGET = 40000;
/** Floating-point slack: every bound is widened by this (OKLab units), every threshold met by it. */
const FP = 1e-6;

/** Is this displayed colour inside the reserved band, or near amber or red? The rule itself, exactly. */
export function labImpersonates(lab: { L: number; a: number; b: number }): boolean {
  const C = Math.sqrt(lab.a * lab.a + lab.b * lab.b);
  if (C >= WASH_CHROMA_FLOOR) {
    let h = (Math.atan2(lab.b, lab.a) * 180) / Math.PI;
    if (h < 0) h += 360;
    if (Math.abs(hueDiff(h, BAND_MID)) <= BAND_HALF) return true;
  }
  for (const m of [AMBER_LAB, RED_LAB]) {
    if (Math.hypot(lab.L - m.L, lab.a - m.a, lab.b - m.b) < MEANING_DELTA_E) return true;
  }
  return false;
}

// The band's two edges as unit rays in the (a, b) plane, opened by a hair for floating point.
const rad = (deg: number) => (deg * Math.PI) / 180;
const EDGE_LO = [Math.cos(rad(WARM_BAND.from - 1e-6)), Math.sin(rad(WARM_BAND.from - 1e-6))] as const;
const EDGE_HI = [Math.cos(rad(WARM_BAND.to + 1e-6)), Math.sin(rad(WARM_BAND.to + 1e-6))] as const;
/** Inside the band's sector (it spans 100 degrees, under 180, so two half-planes define it). */
const inSector = (a: number, b: number) => EDGE_LO[0] * b - EDGE_LO[1] * a >= 0 && a * EDGE_HI[1] - b * EDGE_HI[0] >= 0;

/** How far along unit ray `u` it leaves the box, or -1 when the ray misses it. */
function rayExit(u: readonly [number, number], a0: number, a1: number, b0: number, b1: number): number {
  let lo = 0, hi = Infinity;
  const ta0 = a0 / u[0], ta1 = a1 / u[0], tb0 = b0 / u[1], tb1 = b1 / u[1];
  lo = Math.max(lo, Math.min(ta0, ta1), Math.min(tb0, tb1));
  hi = Math.min(hi, Math.max(ta0, ta1), Math.max(tb0, tb1));
  return lo <= hi ? hi : -1;
}

/**
 * Can any point of the box [a0, a1] x [b0, b1] be in the band at chroma >= the floor? Exact for the box: the
 * part of the box inside the sector is a convex polygon, and its farthest point from the origin is one of its
 * vertices, which are box corners inside the sector or points where a band edge crosses the box (the farther
 * of those is where the edge leaves it).
 */
export function boxMeetsBand(a0: number, a1: number, b0: number, b1: number): boolean {
  const floor = WASH_CHROMA_FLOOR - FP, f2 = floor * floor;
  for (const a of [a0, a1]) for (const b of [b0, b1]) if (inSector(a, b) && a * a + b * b >= f2) return true;
  return rayExit(EDGE_LO, a0, a1, b0, b1) >= floor || rayExit(EDGE_HI, a0, a1, b0, b1) >= floor;
}

/** OKLab distance from point m to the box (0 inside it). */
function boxDistance(m: { L: number; a: number; b: number }, L0: number, L1: number, a0: number, a1: number, b0: number, b1: number) {
  const d = (v: number, lo: number, hi: number) => (v < lo ? lo - v : v > hi ? v - hi : 0);
  return Math.hypot(d(m.L, L0, L1), d(m.a, a0, a1), d(m.b, b0, b1));
}

export interface LabBox { L0: number; L1: number; a0: number; a1: number; b0: number; b1: number }

/** Is the whole box provably clear of the rule (see the header: DARK_L or no band, and far from both meanings)? */
export function boxIsSafe(x: LabBox): boolean {
  const band = x.L1 >= DARK_L && boxMeetsBand(x.a0, x.a1, x.b0, x.b1);
  if (band) return false;
  for (const m of [AMBER_LAB, RED_LAB]) if (boxDistance(m, x.L0, x.L1, x.a0, x.a1, x.b0, x.b1) < MEANING_DELTA_E + FP) return false;
  return true;
}

type Space = "linear" | "gamma";
/** One mixing path, ready: the four colours in the space it mixes in. */
interface Path { space: Space; B: number[]; W1: number[]; W2: number[]; W3: number[] }

function preparePath(washes: readonly string[], base: string, space: Space): Path {
  const prep = (h: string) => hexRgb(h).map((n) => (space === "linear" ? srgbLin(n / 255) : n / 255));
  return { space, B: prep(base), W1: prep(washes[0]!), W2: prep(washes[1]!), W3: prep(washes[2]!) };
}
/** The mix in the path's own space for one channel: wash 3, then 2, then 1, over the base. */
function mixChannel(p: Path, ch: number, w1: number, w2: number, w3: number): number {
  const c3 = p.B[ch]! + (p.W3[ch]! - p.B[ch]!) * w3;
  const c2 = c3 + (p.W2[ch]! - c3) * w2;
  return c2 + (p.W1[ch]! - c2) * w1;
}
/** The path's mix-space value to the displayed sRGB channel, before the dither. */
const toSrgb = (p: Path, v: number) => (p.space === "linear" ? linSrgb(clamp01(v)) : clamp01(v));

/** The exact displayed OKLab colour at one point (weights, dither 0..1 of a step), or its 8-bit rounding. */
export function displayedLab(p: Path, w1: number, w2: number, w3: number, d: number, rounded = false) {
  const ch = (i: number) => {
    const s = clamp01(toSrgb(p, mixChannel(p, i, w1, w2, w3)) - d * PALETTE_DITHER);
    return srgbLin(rounded ? Math.round(s * 255) / 255 : s);
  };
  return linearToLab(ch(0), ch(1), ch(2));
}
/** Either the computed colour or its 8-bit rounding at this point is in the forbidden set (a real hit). */
const hitAt = (p: Path, w1: number, w2: number, w3: number, d: number) =>
  labImpersonates(displayedLab(p, w1, w2, w3, d)) || labImpersonates(displayedLab(p, w1, w2, w3, d, true));

/** A cell: [w1lo, w1hi, w2lo, w2hi, w3lo, w3hi, dlo, dhi], the dither as a fraction of one step. */
type Cell = [number, number, number, number, number, number, number, number];

// OKLab's two matrices (lib/oklch.ts linearToLab, the same numbers): linear sRGB to LMS, cube roots to L, a, b.
const LMS_M = [
  [0.4122214708, 0.5363325363, 0.0514459929],
  [0.2119034982, 0.6806995451, 0.1073969566],
  [0.0883024619, 0.2817188376, 0.6299787005],
] as const;
const LAB_M = [
  [0.2104542553, 0.793617785, -0.0040720468],
  [1.9779984951, -2.428592205, 0.4505937099],
  [0.0259040371, 0.7827717662, -0.808675766],
] as const;
/** LAB_M x LMS_M entry by entry, for the derivative of lab k in linear channel ch through root i. */
const LAB_LMS = LAB_M.map((row) => [0, 1, 2].map((ch) => [0, 1, 2].map((i) => row[i]! * LMS_M[i]![ch]!)));

type Iv = [number, number];
const ivMul = (x: Iv, y: Iv): Iv => {
  const p = [x[0] * y[0], x[0] * y[1], x[1] * y[0], x[1] * y[1]];
  return [Math.min(...p), Math.max(...p)];
};
/** Slope of linear -> sRGB: 12.92 on the linear toe, then decreasing (so non-increasing throughout). */
const gamSlope = (m: number) => (m <= 0.0031308 ? 12.92 : (1.055 / 2.4) * m ** (1 / 2.4 - 1));
/** Slope of sRGB -> linear: 1/12.92 on the toe, then increasing (so non-decreasing throughout). */
const linSlope = (s: number) => (s <= 0.04045 ? 1 / 12.92 : (2.4 / 1.055) * ((s + 0.055) / 1.055) ** 1.4);

/**
 * A box GUARANTEED to hold the OKLab colour of every point in the cell, and which axis to split if that box is
 * not enough. Two enclosures, both sound, intersected:
 *  - the direct one (header, steps 1 and 2): channel ranges from the 8 corners, pushed through the monotone
 *    steps, then interval arithmetic on L, a, b. Robust everywhere, loose where the roots move together;
 *  - the mean-value one: the exact colour at the cell's centre, plus the largest change the cell allows,
 *    lab(x) - lab(centre) = J(xi) (x - centre) for some xi in the cell, with every entry of J (weights and
 *    dither to L, a, b, by the chain rule) bounded over the whole cell by interval products. It keeps the
 *    cancellation between l, m and s that the direct one throws away, so it is several times tighter on
 *    ordinary cells; it is skipped near black, where the cube root's slope is unbounded.
 * Both cover the screen's 8-bit rounding: every sRGB channel range is widened by half a step each way
 * (PALETTE_ROUNDING, clamped to 0..1) before it goes to linear light, and the mean-value radius gains the most
 * that half a step per channel can move the colour (the slope to L, a, b bounded over the widened box).
 */
export function cellBox(p: Path, c: Cell): { box: LabBox; split: number } {
  const h = [(c[1] - c[0]) / 2, (c[3] - c[2]) / 2, (c[5] - c[4]) / 2, (c[7] - c[6]) / 2];
  const x0 = [0, 0, 0], x1 = [0, 0, 0], toLin: Iv[] = [], spread = [0, 0, 0, (c[7] - c[6]) * PALETTE_DITHER];
  /** d linear channel / d (w1, w2, w3, dither), each bounded over the cell. */
  const jx: Iv[][] = [];
  for (let ch = 0; ch < 3; ch++) {
    const v: number[] = [];
    for (let k = 0; k < 8; k++) v.push(mixChannel(p, ch, c[(k & 1) ? 1 : 0], c[(k & 2) ? 3 : 2], c[(k & 4) ? 5 : 4]));
    let mn = v[0]!, mx = v[0]!;
    for (const x of v) { if (x < mn) mn = x; if (x > mx) mx = x; }
    // sRGB range of the channel, then the dither (it only darkens), clamped, then back to linear light.
    const sp0 = toSrgb(p, mn), sp1 = toSrgb(p, mx);
    const u0 = sp0 - c[7] * PALETTE_DITHER, u1 = sp1 - c[6] * PALETTE_DITHER;
    // The screen rounds each channel to 8 bits: anything within half a step of the clamped value.
    const s0 = clamp01(clamp01(u0) - PALETTE_ROUNDING), s1 = clamp01(clamp01(u1) + PALETTE_ROUNDING);
    x0[ch] = srgbLin(s0);
    x1[ch] = srgbLin(s1);
    toLin.push([linSlope(s0), linSlope(s1)]);
    // The chain: mix -> sRGB (slope gamSlope, linear path only) -> minus dither -> clamp (slope 0..1 where it
    // can bind) -> linear (slope linSlope). Each factor's range over the cell comes from its monotone slope.
    const toS: Iv = p.space === "linear" ? [gamSlope(clamp01(mx)), gamSlope(clamp01(mn))] : [1, 1];
    const clampS: Iv = u1 <= 0 ? [0, 0] : u0 < 0 ? [0, 1] : [1, 1];
    const toX: Iv = ivMul([linSlope(s0), linSlope(s1)], clampS);
    const row: Iv[] = [];
    for (let ax = 0; ax < 3; ax++) {
      // The mix is multilinear, so d mix / d w_ax is bilinear in the other two weights: its range over the
      // cell is the range of the 4 edge slopes along that axis.
      const bit = 1 << ax, wd = c[2 * ax + 1]! - c[2 * ax]!;
      let dlo = Infinity, dhi = -Infinity;
      for (let k = 0; k < 8; k++) {
        if (k & bit) continue;
        const d = wd > 0 ? (v[k | bit]! - v[k]!) / wd : 0;
        if (d < dlo) dlo = d;
        if (d > dhi) dhi = d;
        spread[ax] = Math.max(spread[ax]!, Math.abs(v[k | bit]! - v[k]!) * toS[1]);
      }
      row.push(ivMul(ivMul(toX, toS), [dlo, dhi]));
    }
    row.push(ivMul(toX, [-PALETTE_DITHER, -PALETTE_DITHER]));
    jx.push(row);
  }
  // Direct enclosure. LMS: every matrix entry positive, so the ends map to the ends; the cube root is monotone.
  const q0 = LMS_M.map((r) => r[0] * x0[0]! + r[1] * x0[1]! + r[2] * x0[2]!);
  const q1 = LMS_M.map((r) => r[0] * x1[0]! + r[1] * x1[1]! + r[2] * x1[2]!);
  const r0 = q0.map(Math.cbrt), r1 = q1.map(Math.cbrt);
  // L, a, b are linear in the roots: each bound takes each root at the end its coefficient's sign picks.
  const lo = [0, 0, 0], hi = [0, 0, 0];
  for (let k = 0; k < 3; k++) {
    for (let i = 0; i < 3; i++) {
      const e = LAB_M[k]![i]!;
      lo[k] += e * (e >= 0 ? r0[i]! : r1[i]!);
      hi[k] += e * (e >= 0 ? r1[i]! : r0[i]!);
    }
  }
  let split = spread[0]! >= spread[1]! ? (spread[0]! >= spread[2]! ? 0 : 2) : spread[1]! >= spread[2]! ? 1 : 2;
  if (spread[3]! > spread[split]!) split = 3;
  // Mean-value enclosure, where every LMS value is clear of zero (the root's slope 1/(3 q^(2/3)) is bounded).
  if (Math.min(...q0) > 1e-12) {
    const slope: Iv[] = [0, 1, 2].map((i) => [1 / (3 * r1[i]! * r1[i]!), 1 / (3 * r0[i]! * r0[i]!)]);
    const centre = displayedLab(p, (c[0] + c[1]) / 2, (c[2] + c[3]) / 2, (c[4] + c[5]) / 2, (c[6] + c[7]) / 2);
    const mid = [centre.L, centre.a, centre.b];
    const score = [0, 0, 0, 0];
    for (let k = 0; k < 3; k++) {
      // d lab_k / d linear ch = sum over roots i of LAB_M[k][i] * slope_i * LMS_M[i][ch].
      const jl: Iv[] = [0, 1, 2].map((ch) => {
        let a = 0, b = 0;
        for (let i = 0; i < 3; i++) {
          const e = LAB_LMS[k]![ch]![i]!;
          a += e * (e >= 0 ? slope[i]![0] : slope[i]![1]);
          b += e * (e >= 0 ? slope[i]![1] : slope[i]![0]);
        }
        return [a, b];
      });
      // The 8-bit rounding: up to half a step in each sRGB channel, through the slope sRGB -> linear -> lab_k.
      let radius = 0;
      for (let ch = 0; ch < 3; ch++) {
        const t = ivMul(jl[ch]!, toLin[ch]!);
        radius += Math.max(Math.abs(t[0]), Math.abs(t[1])) * PALETTE_ROUNDING;
      }
      for (let ax = 0; ax < 4; ax++) {
        let a = 0, b = 0;
        for (let ch = 0; ch < 3; ch++) {
          const t = ivMul(jl[ch]!, jx[ch]![ax]!);
          a += t[0];
          b += t[1];
        }
        const reach = Math.max(Math.abs(a), Math.abs(b)) * h[ax]!;
        radius += reach;
        score[ax] += reach;
      }
      // Widened by a relative hair as well, for the rounding of the sums above.
      const slack = FP + 1e-9 * (Math.abs(mid[k]!) + radius);
      lo[k] = Math.max(lo[k]!, mid[k]! - radius - slack);
      hi[k] = Math.min(hi[k]!, mid[k]! + radius + slack);
    }
    split = score.indexOf(Math.max(...score));
  }
  const box: LabBox = { L0: lo[0]! - FP, L1: hi[0]! + FP, a0: lo[1]! - FP, a1: hi[1]! + FP, b0: lo[2]! - FP, b1: hi[2]! + FP };
  return { box, split };
}

export type PaletteVerdict = { unsafe: boolean; why: "clear" | "hit" | "unproved" | "budget"; cells: number; at?: number[] };

/** Proves one mixing path clear (see the header), or finds a hit, or refuses when it cannot decide. */
function provePath(p: Path, budget: number): PaletteVerdict {
  // A quick coarse look first: many unsafe palettes show a hit on a 0.25 grid, before any proof is tried.
  for (let i = 0; i <= 4; i++) for (let j = 0; j <= 4; j++) for (let k = 0; k <= 4; k++) for (const d of [0, 1]) {
    if (hitAt(p, i / 4, j / 4, k / 4, d)) return { unsafe: true, why: "hit", cells: 0, at: [i / 4, j / 4, k / 4, d] };
  }
  // Breadth first, so the coarse cells' centres are all tested (a hit is found early) before any cell is fine.
  const queue: Cell[] = [[0, 1, 0, 1, 0, 1, 0, 1]];
  let cells = 0;
  for (let head = 0; head < queue.length; head++) {
    const c = queue[head]!;
    if (++cells > budget) return { unsafe: true, why: "budget", cells };
    const { box, split } = cellBox(p, c);
    if (boxIsSafe(box)) continue;
    const mid = [(c[0] + c[1]) / 2, (c[2] + c[3]) / 2, (c[4] + c[5]) / 2, (c[6] + c[7]) / 2];
    if (hitAt(p, mid[0]!, mid[1]!, mid[2]!, mid[3]!)) return { unsafe: true, why: "hit", cells, at: mid };
    // Split the chosen axis, or the widest one still splittable; none left: unproved, so refused.
    const width = (ax: number) => c[2 * ax + 1]! - c[2 * ax]!;
    const can = (ax: number) => width(ax) > (ax === 3 ? MIN_DITHER_CELL : PALETTE_MIN_CELL);
    const ax = can(split) ? split : [0, 1, 2, 3].filter(can).sort((x, y) => width(y) - width(x))[0];
    if (ax === undefined) return { unsafe: true, why: "unproved", cells, at: mid };
    const a = c.slice() as Cell, b = c.slice() as Cell;
    a[2 * ax + 1] = mid[ax]!;
    b[2 * ax] = mid[ax]!;
    queue.push(a, b);
  }
  return { unsafe: false, why: "clear", cells };
}

/** Both paths' verdict for a palette (the first that is unsafe, else the clear one with both cell counts). */
export function paletteVerdict(washes: readonly string[], base: string, budget = PALETTE_CELL_BUDGET): PaletteVerdict {
  const lin = provePath(preparePath(washes, base, "linear"), budget);
  if (lin.unsafe) return lin;
  const gam = provePath(preparePath(washes, base, "gamma"), budget);
  return gam.unsafe ? gam : { unsafe: false, why: "clear", cells: lin.cells + gam.cells };
}

// Re-renders ask about the same palette again and again: remember the last few answers.
const verdictCache = new Map<string, boolean>();

/**
 * Null when every colour the background can show (both mixing paths, every weight and dither) is proved clear
 * of the reserved band and of amber and red; otherwise the refusal. Fails closed (see the header).
 */
export function checkPalette(washes: readonly string[], base: string): Refusal | null {
  const ws = washes.map(normHex), b = normHex(base);
  if (ws.length !== 3 || ws.some((w) => !w) || !b) return refuse(PALETTE_REASON);
  const key = ws.join(",") + "|" + b;
  let unsafe = verdictCache.get(key);
  if (unsafe === undefined) {
    unsafe = paletteVerdict(ws as string[], b).unsafe;
    if (verdictCache.size >= 64) verdictCache.delete(verdictCache.keys().next().value!);
    verdictCache.set(key, unsafe);
  }
  return unsafe ? refuse(PALETTE_REASON) : null;
}

/** For tests: the prepared path, so a test can reach `displayedLab` and `cellBox` the way the check does. */
export const paletteInternals = { preparePath };

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
const RGB_TABLE: readonly string[] = Array.from({ length: 360 }, (_, deg) =>
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
 * one to them unclamped, so a preset never needs darkening and never comes near amber or red. The first is
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
  const mixed = checkPalette(p.washes, p.base);
  if (mixed) return mixed;
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
    // The blends, not just each colour: an unsafe stored palette falls back (the washes, then the base).
    let mix = { washes, base };
    if (checkPalette(mix.washes, mix.base)) mix = { washes: [...DEFAULT_WASHES], base };
    if (checkPalette(mix.washes, mix.base)) mix = { washes: mix.washes, base: DEFAULT_BASE };
    // The accent is judged against the washes that will actually be shown, after any fallback: a stored accent
    // that only failed against a refused wash set is kept.
    const accent = checkAccent(a.accent, mix.washes);
    out = { accent: accent.ok ? accent.value : DEFAULT_ACCENT, washes: mix.washes as [string, string, string], base: mix.base };
  }
  const code = checkAccent(a.codeAccent, out.washes);
  const colours = out as Colours;
  // Not enumerable on purpose: the object stays the palette (accent, washes, base) to anything that compares
  // or spreads it (presets, matchingPreset, the acceptance tests' toEqual); the editor's colour is read by
  // name (`c.codeAccent`, colourProps).
  Object.defineProperty(colours, "codeAccent", { value: code.ok ? code.value : DEFAULT_CODE_ACCENT, enumerable: false });
  return colours;
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
