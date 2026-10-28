// The boot animation (2026-10-12). Pure: a seeded RNG, the
// binary dust's paths, the timeline, the layout, the name plate's geometry
// and the one decision of whether it plays. `components/BootOverlay.tsx`
// draws it.
//
// The look and timing are the owner's approved prototype,
// at its default parameters, ported line for line: binary dust piles at the bottom left, fans right along geometric
// Brownian motion paths (in log space, tanh soft walls), curves back and
// converges into the orb in the middle, while two pens draw the name plate's
// box counter-clockwise and a pen per letter draws "JARVIS"; when the pens
// finish, the dots pop in — "J.A.R.V.I.S." — with a faint fill and an inner
// rule.

import { DEFAULT_RINGS, type Ring } from "./orbdraw";

/** The prototype's defaults, which the owner approved ("literally perfection"). */
export const BOOT_DEFAULTS = {
  speed: 1,
  n: 700,
  drift: 0.55,
  vol: 0.28,
  trail: 0.8,
  travel: 1.5,
  ret: 1.05,
  pen: 1.8,
  ratio: 3.4,
  seed: 7,
} as const;

export type BootConfig = { [K in keyof typeof BOOT_DEFAULTS]: number };

/** Fixed beats of the timeline, in seconds (scaled only by `speed`). */
export const BEAT = { pileIn: 0.25, launch0: 0.2, launchSpread: 0.8, plate0: 0.7, dots: 0.5, orbTail: 0.35 } as const;

/** How long the finished picture fades away to the HUD under it, after the end. */
export const FADE_OUT_S = 0.45;
/** Keep the pointer shield until the corresponding click has been eaten. */
export const SKIP_FADE_MS = 200;
/** How long the click shield outlasts the last release or click (review of PR #82, F3). */
export const SHIELD_MS = 450;
/** However the frames go, the overlay is gone this long after it mounted. */
export const HARD_CAP_MS = 7_000;

/** Whether this window has played it already (once per window session). */
export const PLAYED_KEY = "jarvis.hud.boot.played";

export type { Ring };

/** The orb's default ring set (Orb.tsx `RING_SETS.default`; a test keeps them equal). */
export const RINGS = DEFAULT_RINGS;
/** Ring speed multiplier during boot. */
export const SPIN = 1.2;

// ---- the decision ------------------------------------------------------------

export interface PlayFacts {
  /** `navigator.webdriver`: Playwright and every headless suite. */
  webdriver: boolean;
  /** This window session played it already. */
  seen: boolean;
  /** `prefers-reduced-motion: reduce`. */
  reducedMotion: boolean;
  /** The test-only force (`window.__hudBootForce`), for its own suite. */
  forced: boolean;
  /** An authorization card is up. */
  cardUp: boolean;
}

/**
 * Whether the boot animation plays. Never under automation unless forced (the
 * owner's rule: other agents' suites must see the HUD exactly as before),
 * never twice in a window session (a reload does not replay it, forced or
 * not), never with reduced motion (skipped outright, no stand-in fade), and
 * never over an authorization card.
 */
export function shouldPlay(f: PlayFacts): boolean {
  if (f.webdriver && !f.forced) return false;
  if (f.seen) return false;
  if (f.reducedMotion) return false;
  if (f.cardUp) return false;
  return true;
}

// ---- helpers -----------------------------------------------------------------

export const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
export const easeInOutCubic = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
export const easeInOutQuad = (x: number) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);
export const easeOutBack = (x: number) => {
  const c = 1.6;
  return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2);
};

/** A seeded PRNG: the same seed gives the same sequence everywhere. */
export function mulberry32(a: number): () => number {
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A standard normal from `rng` (Box–Muller). */
export function gaussFrom(rng: () => number): () => number {
  return () => {
    let u = 0;
    while (!u) u = rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng());
  };
}

export const ringRot = (ring: Ring, t: number) => (t * ring.speed * SPIN * Math.PI * 2) / 8;
const bez = (a: number, b: number, c: number, d: number, s: number) => {
  const m = 1 - s;
  return m * m * m * a + 3 * m * m * s * b + 3 * m * s * s * c + s * s * s * d;
};

// ---- layout ------------------------------------------------------------------

export interface BootLayout {
  W: number;
  H: number;
  S: number;
  orbR: number;
  cx: number;
  cy: number;
  /** The plate: width, height and top-left corner. */
  pw: number;
  ph: number;
  px: number;
  py: number;
  pileX: number;
  pileY: number;
}

export function layoutFor(W: number, H: number, cfg: BootConfig = BOOT_DEFAULTS): BootLayout {
  const S = Math.min(W, H);
  const orbR = S * 0.17;
  const cx = W / 2;
  const cy = H * 0.4;
  const pw = Math.min(orbR * 3.4, W * 0.86);
  const ph = pw / cfg.ratio;
  return { W, H, S, orbR, cx, cy, pw, ph, px: cx - pw / 2, py: cy + orbR * 1.22, pileX: W * 0.06, pileY: H * 0.93 };
}

// ---- particles: binary dust on GBM paths, then home to the orb -----------------

/** GBM steps per path. */
export const K = 120;

export interface Particle {
  i: number;
  x0: number;
  y0: number;
  launch: number;
  travel: number;
  /** ln S at each of K+1 steps. */
  lp: Float32Array;
  xEnd: number;
  /** Where it lands, as a fraction of the orb's radius. */
  rr: number;
  ringIdx: number;
  a0: number;
  swirl: number;
  ret: number;
  arrive: number;
  size: number;
  alpha: number;
  /** Which hue: 0 the accent, 1 its lighter step, 2 the text colour. */
  hue: 0 | 1 | 2;
  bit: "0" | "1";
  flipPhase: number;
}

export interface Timeline {
  plate0: number;
  /** The pens finish drawing the box and the letters. */
  pensEnd: number;
  /** The dots have popped in. */
  dotsEnd: number;
  orbStart: number;
  /** The orb has fully formed. */
  orbEnd: number;
  /** The picture is complete and held; the fade-out starts here. */
  end: number;
}

export interface Dust {
  parts: Particle[];
  timeline: Timeline;
}

export function buildDust(L: BootLayout, cfg: BootConfig = BOOT_DEFAULTS): Dust {
  const { W, H } = L;
  const rng = mulberry32(cfg.seed);
  const gauss = gaussFrom(rng);
  const parts: Particle[] = [];
  const counts = RINGS.map((r) => r.r); // weight rings by circumference
  const coreShare = 0.12;
  const ringTotal = counts.reduce((a, b) => a + b, 0);
  for (let i = 0; i < cfg.n; i++) {
    // The pile: a low mound at the bottom left.
    const dx = gauss() * 0.35;
    const x0 = L.pileX + dx * W * 0.06;
    const hNorm = Math.abs(gauss()) * Math.max(0.15, 1 - Math.abs(dx) / 1.2);
    const y0 = L.pileY - hNorm * H * 0.03;
    // Grains near the top of the pile leave first.
    const launch = BEAT.launch0 + BEAT.launchSpread * (0.55 * rng() + 0.45 * clamp01(1 - hNorm));
    const travel = cfg.travel * (0.8 + 0.4 * rng());
    // Geometric Brownian motion, in log space: ln S_τ = (μ − σ²/2)τ + σ B_τ.
    // `drift` is the log-drift (μ − σ²/2) directly, so it stays the knob for "how high".
    const lp = new Float32Array(K + 1);
    let B = 0;
    for (let k = 1; k <= K; k++) {
      B += Math.sqrt(1 / K) * gauss();
      lp[k] = cfg.drift * (k / K) + cfg.vol * B;
    }
    const xEnd = W * (0.84 + 0.1 * rng());
    // Where it lands on the orb.
    let rr: number;
    let ringIdx = -1;
    if (rng() < coreShare) rr = Math.sqrt(rng()) * 0.3;
    else {
      let pick = rng() * ringTotal;
      ringIdx = 0;
      while (pick > counts[ringIdx] && ringIdx < RINGS.length - 1) pick -= counts[ringIdx++];
      rr = RINGS[ringIdx].r * (1 - rng() * 0.03);
    }
    const ret = cfg.ret * (0.8 + 0.5 * rng());
    const roll = rng();
    const hue: 0 | 1 | 2 = roll < 0.7 ? 0 : roll < 0.9 ? 1 : 2;
    parts.push({
      i, x0, y0, launch, travel, lp, xEnd, rr, ringIdx, a0: rng() * Math.PI * 2,
      swirl: 0.6 + rng() * 0.9, ret, arrive: launch + travel + ret,
      size: 9 + Math.floor(rng() * 3) * 2, alpha: 0.4 + 0.55 * rng(), hue,
      bit: rng() < 0.5 ? "0" : "1", flipPhase: rng() * 10,
    });
  }
  const arr = parts.map((p) => p.arrive).sort((a, b) => a - b);
  const q = (f: number) => arr[Math.min(arr.length - 1, Math.floor(f * arr.length))] || 0;
  const orbStart = q(0.35);
  const orbEnd = q(0.92) + BEAT.orbTail;
  const pensEnd = BEAT.plate0 + cfg.pen;
  const dotsEnd = pensEnd + BEAT.dots;
  return {
    parts,
    timeline: { plate0: BEAT.plate0, pensEnd, dotsEnd, orbStart, orbEnd, end: Math.max(orbEnd, dotsEnd) + 0.6 },
  };
}

/** Where a particle is `tau` (0..1) of the way along its GBM path. */
export function travelPos(L: BootLayout, p: Particle, tau: number): [number, number] {
  const { H } = L;
  const f = Math.pow(tau, 1.25);
  const kf = tau * K;
  const k = Math.min(K - 1, Math.floor(kf));
  const fr = kf - k;
  const lp = p.lp[k] * (1 - fr) + p.lp[k + 1] * fr;
  const x = p.x0 + (p.xEnd - p.x0) * f;
  // Soft walls (tanh) instead of a clamp: a clamp stacks grains into a line at the edge.
  const up = p.y0 - H * 0.05;
  const down = Math.max(4, H - 6 - p.y0);
  const rise = lp * H * 0.9;
  const y = p.y0 - (rise >= 0 ? up * Math.tanh(rise / up) : -down * Math.tanh(-rise / down));
  return [x, y];
}

function targetAngle(p: Particle, t: number) {
  return p.a0 + (p.ringIdx >= 0 ? ringRot(RINGS[p.ringIdx], t) : t * 0.3);
}

/** [x, y, alpha] at time t, or null once it has dissolved into the orb. */
export function particleAt(L: BootLayout, p: Particle, t: number): [number, number, number] | null {
  const { W, H } = L;
  const pileA = clamp01(t / BEAT.pileIn);
  if (t < p.launch) return [p.x0, p.y0, p.alpha * pileA * 0.85];
  const tau = (t - p.launch) / p.travel;
  if (tau <= 1) {
    const [x, y] = travelPos(L, p, tau);
    return [x, y, p.alpha];
  }
  const [ex, ey] = travelPos(L, p, 1);
  const [bx, by] = travelPos(L, p, 0.97);
  let vx = ex - bx;
  let vy = ey - by;
  const vm = Math.hypot(vx, vy) || 1;
  vx /= vm;
  vy /= vm;
  const R = L.orbR * p.rr;
  const s = (t - p.launch - p.travel) / p.ret;
  if (s <= 1) {
    const a = targetAngle(p, t);
    const tx = L.cx + Math.cos(a) * R;
    const ty = L.cy + Math.sin(a) * R;
    const c1x = Math.min(W * 0.985, ex + vx * W * 0.12);
    const c1y = ey + vy * W * 0.12 - H * 0.04;
    const ca = a + p.swirl;
    const cr = Math.max(R, L.orbR * 0.4) * 2.4;
    const c2x = L.cx + Math.cos(ca) * cr;
    const c2y = L.cy + Math.sin(ca) * cr;
    const e = easeInOutCubic(s);
    return [bez(ex, c1x, c2x, tx, e), bez(ey, c1y, c2y, ty, e), p.alpha];
  }
  // Arrived: ride the ring and dissolve into the orb.
  const fade = 1 - clamp01((t - p.arrive) / 0.6);
  if (fade <= 0) return null;
  const a = targetAngle(p, t);
  return [L.cx + Math.cos(a) * R, L.cy + Math.sin(a) * R, p.alpha * fade];
}

/** The glyph a grain shows: it flickers 0/1 while it flies. */
export function bitAt(p: Particle, t: number): "0" | "1" {
  if (t < p.launch || t > p.arrive) return p.bit;
  const slot = Math.floor(t * 7 + p.flipPhase);
  const h = Math.sin(p.i * 12.9898 + slot * 78.233) * 43758.5453;
  return h - Math.floor(h) < 0.5 ? "0" : "1";
}

/** How far the orb has formed (0..1) at time t. */
export function orbProgress(tl: Timeline, t: number): number {
  return easeInOutCubic(clamp01((t - tl.orbStart) / Math.max(0.01, tl.orbEnd - tl.orbStart)));
}

// ---- the name plate --------------------------------------------------------------

/** Letters on a 0..w × 0..80 grid, all straight lines and sharp corners; each stroke is one pen run. */
type P = (x: number, y: number) => string;
export const GLYPHS: Record<string, { w: number; s: (P: P) => string[] }> = {
  J: { w: 56, s: (P) => [`M${P(8, 0)} L${P(52, 0)} L${P(52, 80)} L${P(4, 80)} L${P(4, 56)}`] },
  A: { w: 60, s: (P) => [`M${P(0, 80)} L${P(30, 0)} L${P(60, 80)}`, `M${P(12.75, 46)} L${P(47.25, 46)}`] },
  R: { w: 58, s: (P) => [`M${P(0, 80)} L${P(0, 0)} L${P(56, 0)} L${P(56, 40)} L${P(0, 40)}`, `M${P(30, 40)} L${P(58, 80)}`] },
  V: { w: 60, s: (P) => [`M${P(0, 0)} L${P(30, 80)} L${P(60, 0)}`] },
  I: { w: 8, s: (P) => [`M${P(4, 0)} L${P(4, 80)}`] },
  S: { w: 60, s: (P) => [`M${P(58, 0)} L${P(2, 0)} L${P(2, 40)} L${P(58, 40)} L${P(58, 80)} L${P(2, 80)}`] },
};
export const GAP = 34;
export const WORD = "JARVIS";

export interface PlateGeometry {
  /** The box: pen A from the top-right corner along the top and down the left edge… */
  boxA: string;
  /** …pen B from the bottom-left corner along the bottom and up the right edge. Both counter-clockwise. */
  boxB: string;
  boxWidth: number;
  /** Each letter's strokes, in drawing order. */
  letters: string[][];
  letterWidth: number;
  /** The square dots after each letter: centre and side. */
  dots: { x: number; y: number; size: number }[];
  /** The faint fill, and the inner rule's inset. */
  rect: { x: number; y: number; w: number; h: number };
  inset: number;
  /** The word's left and right edges, for layout checks. */
  wordLeft: number;
  wordRight: number;
}

export function plateGeometry(L: BootLayout): PlateGeometry {
  const x1 = L.px;
  const y1 = L.py;
  const x2 = x1 + L.pw;
  const y2 = y1 + L.ph;
  const h = L.ph * 0.42;
  const s = h / 80;
  const lw = Math.max(1.8, h * 0.085);
  const units = [...WORD].reduce((a, ch) => a + GLYPHS[ch].w, 0) + GAP * (WORD.length - 1) + GAP / 2;
  let ox = L.cx - (units * s) / 2;
  const wordLeft = ox;
  const y0 = y1 + (L.ph - h) / 2;
  const letters: string[][] = [];
  const dots: PlateGeometry["dots"] = [];
  for (const ch of WORD) {
    const g = GLYPHS[ch];
    const at = ox;
    const P: P = (x, y) => `${(at + x * s).toFixed(2)} ${(y0 + y * s).toFixed(2)}`;
    letters.push(g.s(P));
    ox += g.w * s;
    dots.push({ x: ox + (GAP * s) / 2, y: y0 + 80 * s, size: lw * 1.25 });
    ox += GAP * s;
  }
  return {
    boxA: `M${x2} ${y1} L${x1} ${y1} L${x1} ${y2}`,
    boxB: `M${x1} ${y2} L${x2} ${y2} L${x2} ${y1}`,
    boxWidth: Math.max(1.6, L.ph * 0.018),
    letters,
    letterWidth: lw,
    dots,
    rect: { x: x1, y: y1, w: L.pw, h: L.ph },
    inset: Math.max(4, L.ph * 0.06),
    wordLeft,
    wordRight: ox - GAP * s,
  };
}

/** The pens' progress (0..1, eased) at time t, and how visible their tips are. */
export function penAt(tl: Timeline, cfg: BootConfig, t: number): { e: number; vis: number } {
  const raw = (t - tl.plate0) / cfg.pen;
  const e = easeInOutQuad(clamp01(raw));
  // Pens glow while drawing and fade out over a quarter second once done.
  const vis = raw <= 0 ? 0 : raw < 1 ? 1 : 1 - clamp01((t - tl.pensEnd) / 0.25);
  return { e, vis };
}

/** The dots' (and the fill's) appearance at time t: opacity, and the pop's scale. */
export function dotsAt(tl: Timeline, t: number): { q: number; pop: number } {
  const q = clamp01((t - tl.pensEnd) / BEAT.dots);
  return { q, pop: q > 0 ? easeOutBack(q) : 0 };
}

/** "#6fc3df" → "111,195,223"; null if it is not a 3- or 6-digit hex colour. */
export function hexToRgb(hex: string): string | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const v = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)).join(",");
}
