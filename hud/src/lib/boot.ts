// The boot animation, "Constellation + Orb" (owner, 2026-11-12, plan D-12). Pure: a seeded RNG, the
// timeline, the layout, the sparks' paths and the one decision of whether it plays. `components/BootOverlay.tsx`
// draws it. Ported from concept 6 ("blend") of docs/plans/2026-11-12-boot-concepts.html.
//
// The story, about 4.1 s: glass sparks drift over a dark aurora; about 560 of them fly onto the orb's five
// rings, outer first, and each ring turns to solid glass as its sparks land, spinning fast and then springing to a
// slow locked turn; about 1,000 more spell the name beneath the orb and shimmer. At 1.75 s the core ignites and
// sends a ripple through the aurora, which brightens. The name's sparks scatter outward, the orb docks into the
// HUD's own orb, and the overlay crossfades to the real HUD, which was there all along.

// ---- constants kept from the earlier boot ---------------------------------------

/** How long the picture fades away to the HUD under it, starting at `TIMELINE.end`. */
export const FADE_OUT_S = 0.6;
/** Keep the pointer shield until the corresponding click has been eaten. */
export const SKIP_FADE_MS = 200;
/** How long the click shield outlasts the last release or click (review of PR #82, F3). */
export const SHIELD_MS = 450;
/** However the frames go, the overlay is gone this long after it mounted. */
export const HARD_CAP_MS = 7_000;
/** A pointer still held this long after mount loses the shield then: the cap, plus the release protection a release at the cap still owes. */
export const SHIELD_DEADLINE_MS = HARD_CAP_MS + SHIELD_MS;
/** The overlay is gone this long after mount whatever happens: a release just before the deadline still owes its SHIELD_MS. */
export const SHIELD_ABSOLUTE_MS = SHIELD_DEADLINE_MS + SHIELD_MS;
/** How long the name waits for the bundled font before it is sampled in whatever face there is. */
export const FONT_WAIT_MS = 600;

/** The concept's lavender, used only when no live token gives a hue outside the amber and red band. */
const FALLBACK_LAVENDER: readonly [number, number, number] = [176 / 255, 166 / 255, 240 / 255];

/** Hue in degrees (0..360) of an sRGB colour with 0..1 channels. */
export function hueOf(c: readonly [number, number, number]): number {
  const [r, g, b] = c;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (d < 1e-6) return 0;
  const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

/** Amber and red keep their meaning (pending, error): no tint may land in 0..70 or 335..360 degrees. */
const inWarmBand = (c: readonly [number, number, number]) => {
  const h = hueOf(c);
  return h < 70 || h > 335;
};

/**
 * The spark glass's violet tint, from the live tokens: `--wash-3` lifted halfway to white (the default
 * palette gives about the concept's lavender), else the accent lifted the same way, else the concept's own
 * value, whenever a custom palette would put it in the amber or red band.
 */
export function sparkLavender(wash3: readonly [number, number, number], accent: readonly [number, number, number]): [number, number, number] {
  const lift = (c: readonly [number, number, number]): [number, number, number] => [c[0] + (1 - c[0]) * 0.5, c[1] + (1 - c[1]) * 0.5, c[2] + (1 - c[2]) * 0.5];
  for (const c of [wash3, accent]) if (!inWarmBand(c)) return lift(c);
  return [...FALLBACK_LAVENDER];
}

/** Whether this window has played it already (once per window session). */
export const PLAYED_KEY = "jarvis.hud.boot.played";

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
/** 0 before `a`, 1 after `b`, linear between. */
export const seg = (t: number, a: number, b: number) => clamp01((t - a) / (b - a));
export const easeOutCubic = (x: number) => 1 - Math.pow(1 - x, 3);
export const easeInCubic = (x: number) => x * x * x;
export const easeInOutCubic = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
/** A damped spring from 0 toward 1 over x in 0..1.8 (it is 1 from there). */
export const spring = (x: number, z = 0.45, w = 10) => {
  if (x <= 0) return 0;
  if (x >= 1.8) return 1;
  const wd = w * Math.sqrt(1 - z * z);
  return 1 - Math.exp(-z * w * x) * (Math.cos(wd * x) + ((z * w) / wd) * Math.sin(wd * x));
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

const TAU = 6.283;

// ---- the timeline ---------------------------------------------------------------

/** Seconds. The picture is complete at `end`; the crossfade to the HUD runs `FADE_OUT_S` after it. */
export const TIMELINE = {
  /** The core ignites and the ripple leaves it. */
  ignite: 1.75,
  /** The name's sparks fly outward. */
  scatterStart: 2.55,
  scatterEnd: 3.35,
  /** The orb travels to the HUD's orb. */
  dockStart: 2.75,
  dockEnd: 3.55,
  /** The orb is docked and the crossfade begins. */
  end: 3.5,
} as const;

/** The whole run, crossfade included. Less than `HARD_CAP_MS`. */
export const TOTAL_S = TIMELINE.end + FADE_OUT_S;

/** The overlay's opacity: 1 until `end`, then easing to 0 as the real HUD takes over. */
export function handoffAlpha(t: number): number {
  return 1 - easeInOutCubic(seg(t, TIMELINE.end, TIMELINE.end + FADE_OUT_S));
}

/** The aurora's strength: it wakes, then brightens at the ignition. */
export function auroraAt(t: number): number {
  return 0.1 + 0.28 * easeOutCubic(seg(t, 0, 1.4)) + 0.62 * easeOutCubic(seg(t, TIMELINE.ignite, TIMELINE.ignite + 1.5));
}

/** The core: how lit it is (0..1) and the pulse that rides the ignition and decays. */
export function coreAt(t: number): { glow: number; pulse: number } {
  const glow = easeOutCubic(seg(t, TIMELINE.ignite - 0.15, TIMELINE.ignite + 0.25));
  return { glow, pulse: Math.exp(-Math.max(0, t - TIMELINE.ignite) * 3) * glow };
}

/** The scatter's progress (0..1, accelerating). */
export const scatterAt = (t: number) => easeInCubic(seg(t, TIMELINE.scatterStart, TIMELINE.scatterEnd));
/** The dock's progress (0..1). */
export const dockAt = (t: number) => easeInOutCubic(seg(t, TIMELINE.dockStart, TIMELINE.dockEnd));

// ---- the rings ---------------------------------------------------------------------

export interface BootRing {
  /** Radius, as a fraction of the orb's. */
  r: number;
  /** How many sparks sit on it. */
  n: number;
  /** Which arcs exist (radians), or null for a full circle. */
  arcs: [number, number][] | null;
  /** Spin direction. */
  dir: 1 | -1;
  /** Its share of the slow turn. */
  slow: number;
}

const arcs18: [number, number][] = [...Array(18)].map((_, i) => [(i / 18) * TAU + 0.03, ((i + 1) / 18) * TAU - 0.03]);
/** The orb's five rings, outer first. */
export const BOOT_RINGS: readonly BootRing[] = [
  { r: 0.95, n: 140, arcs: null, dir: 1, slow: 0.2 },
  { r: 0.82, n: 170, arcs: arcs18, dir: -1, slow: 1 },
  { r: 0.64, n: 110, arcs: [[0, 1.8], [2.3, 3.0], [3.5, 5.1], [5.5, 6.0]], dir: 1, slow: 1 },
  { r: 0.52, n: 72, arcs: null, dir: -1, slow: 0.7 },
  { r: 0.36, n: 70, arcs: null, dir: 1, slow: 1 },
];

/** When ring `i`'s sparks land. */
export const ringLand = (i: number) => 0.95 + i * 0.12;
/** The latest any of ring `i`'s sparks reaches its spot (the spread is at most 0.3 × 0.3 s). */
export const ringArrive = (i: number) => ringLand(i) + 0.09;
/** How solid ring `i`'s glass is (0..1); it is 1 from 0.35 s after it lands. */
export const ringSolid = (i: number, t: number) => easeOutCubic(seg(t, ringLand(i) - 0.1, ringLand(i) + 0.35));
/** The spin spring: it settles by `ringLand(i) + 1.1`. */
export const ringLock = (i: number, t: number) => spring(seg(t, ringLand(i) - 0.1, ringLand(i) + 1.1), 0.5, 8);
/** Ring `i`'s rotation (radians): a fast turn as it forms, springing to a slow locked turn. */
export function ringRot(i: number, t: number): number {
  const rg = BOOT_RINGS[i]!;
  return rg.dir * (t * 0.35 * rg.slow + (1 - ringLock(i, t)) * 5.5 * rg.slow + i * 0.7);
}

// ---- layout ------------------------------------------------------------------------

export interface BootGeo {
  /** The orb at rest: its centre and radius, and where the name sits beneath it. */
  cx: number;
  cy: number;
  R: number;
  wy: number;
  wsize: number;
}

export function geoFor(W: number, H: number): BootGeo {
  const S = Math.min(W, H);
  const cy = H * 0.37;
  const R = S * 0.17;
  return { cx: W / 2, cy, R, wy: cy + R * 1.62, wsize: Math.min(W * 0.11, H * 0.12) };
}

/** Where the orb ends up: the real orb's centre and radius, in the overlay's pixels. */
export interface DockTarget {
  x: number;
  y: number;
  R: number;
}

/** The concept's own spot, bottom left (bottom right on a narrow window), for a HUD whose orb is absent or hidden. */
export function fallbackDock(W: number, H: number): DockTarget {
  return { x: W < 760 ? W - 64 : 116, y: H - 116, R: 56 };
}

/** The real orb canvas is 132 px square and draws its rings at `SIZE / 2 - 3` (components/Orb.tsx). */
export const ORB_RADIUS_FRACTION = (132 / 2 - 3) / 132;

/**
 * The dock for a measured orb rect (viewport px), or the fallback when it cannot be used: collapsed, hidden,
 * not laid out or off the window.
 */
export function dockFromRect(
  rect: { left: number; top: number; width: number; height: number } | null,
  W: number,
  H: number,
): DockTarget {
  if (rect && rect.width >= 8 && rect.height >= 8 && rect.left + rect.width > 0 && rect.top + rect.height > 0
    && rect.left < W && rect.top < H) {
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, R: rect.width * ORB_RADIUS_FRACTION };
  }
  return fallbackDock(W, H);
}

/** The orb at time t: its centre and radius, travelling from rest to the dock. */
export function orbAt(g: BootGeo, dock: DockTarget, t: number): { cx: number; cy: number; R: number; dock: number } {
  const d = dockAt(t);
  return { cx: g.cx + (dock.x - g.cx) * d, cy: g.cy + (dock.y - g.cy) * d, R: g.R + (dock.R - g.R) * d, dock: d };
}

// ---- sparks ------------------------------------------------------------------------

/** How many tints a spark can have; the component maps each to a colour from the live tokens. */
export const TINTS = 4;

export interface Spark {
  /** Which ring it lands on, or -1 for a spark of the name. */
  ring: number;
  /** Its angle on the ring. */
  a: number;
  /** Its target in the name (pixels), for a name spark. */
  tx: number;
  ty: number;
  /** Where it starts, and its staggered delay. */
  sx: number;
  sy: number;
  d: number;
  /** Its size, shimmer phase, tint (0..TINTS-1) and its scatter heading and reach. */
  r: number;
  ph: number;
  tint: number;
  ang: number;
  far: number;
}

/** How many sparks spell the name, at most. */
export const NAME_SPARKS = 1000;

/** The seed of the spark set. */
export const BOOT_SEED = 23;

/**
 * Scan a name's alpha mask (the offscreen canvas's RGBA bytes, W × H) for lit points on a grid, in scan order.
 * The grid step follows the font size, so a big name does not get more sparks than a small one.
 */
export function maskPoints(rgba: ArrayLike<number>, W: number, H: number, wsize: number): [number, number][] {
  const step = Math.max(3, Math.round(wsize / 30));
  const out: [number, number][] = [];
  for (let y = 0; y < H; y += step) for (let x = 0; x < W; x += step) if (rgba[(y * W + x) * 4 + 3]! > 128) out.push([x, y]);
  return out;
}

/** The whole spark set for a window, deterministic in (W, H, geo, points). */
export function buildSparks(W: number, H: number, g: BootGeo, points: [number, number][]): Spark[] {
  const rnd = mulberry32(BOOT_SEED);
  const sp: Spark[] = [];
  BOOT_RINGS.forEach((ring, ri) => {
    for (let k = 0; k < ring.n; k++) {
      let a: number;
      if (ring.arcs) {
        const arc = ring.arcs[k % ring.arcs.length]!;
        a = arc[0] + (arc[1] - arc[0]) * rnd();
      } else a = (k / ring.n) * TAU;
      sp.push({
        ring: ri, a, tx: 0, ty: 0, sx: rnd() * W, sy: rnd() * H, d: rnd() * 0.3, r: 0.7 + rnd() * 1.0, ph: rnd() * 6.28,
        tint: Math.floor(rnd() * TINTS), ang: rnd() * TAU, far: 0.3 + rnd() * 0.5,
      });
    }
  });
  const tg = points.slice();
  for (let i = tg.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [tg[i], tg[j]] = [tg[j]!, tg[i]!];
  }
  for (const [tx, ty] of tg.slice(0, NAME_SPARKS)) {
    sp.push({
      ring: -1, a: 0, tx, ty, sx: rnd() * W, sy: rnd() * H, d: rnd() * 0.35, r: 0.7 + rnd() * 1.1, ph: rnd() * 6.28,
      tint: Math.floor(rnd() * TINTS), ang: Math.atan2(ty - g.wy, tx - g.cx) + (rnd() - 0.5) * 0.8, far: 0.4 + rnd() * 0.8,
    });
  }
  return sp;
}

/** A ring spark's flight progress (0..1): 1 once it is on its spot, at `ringArrive` at the latest. */
export const ringFlight = (s: Spark, t: number) =>
  easeInOutCubic(seg(t, 0.2 + s.ring * 0.12 + s.d, ringLand(s.ring) + s.d * 0.3));
/** A name spark's flight progress (0..1): 1 once it is on its letter, by 2.05 s at the latest. */
export const nameFlight = (s: Spark, t: number) => easeInOutCubic(seg(t, 0.45 + s.d, 1.7 + s.d));

/** Where a spark is at time t, and how bright (0..1, before the overlay's own fade). */
export function sparkAt(
  s: Spark,
  t: number,
  o: { cx: number; cy: number; R: number },
  D: number,
): [number, number, number] {
  const intro = clamp01(t / 0.3);
  if (s.ring >= 0) {
    // Ring sparks fly to their spot on the ring, ride it, and fade into the glass as it solidifies.
    const rg = BOOT_RINGS[s.ring]!;
    const k = ringFlight(s, t);
    const ang = s.a + ringRot(s.ring, t);
    const tx = o.cx + Math.cos(ang) * o.R * rg.r;
    const ty = o.cy + Math.sin(ang) * o.R * rg.r;
    return [
      s.sx + (tx - s.sx) * k + Math.sin(t * 1.3 + s.ph) * 5 * (1 - k),
      s.sy + (ty - s.sy) * k + Math.cos(t * 1.1 + s.ph) * 5 * (1 - k),
      (0.25 + 0.6 * k) * (1 - 0.85 * ringSolid(s.ring, t)) * intro,
    ];
  }
  // Name sparks spell the name, shimmer, then scatter outward with the dock.
  const k = nameFlight(s, t);
  const sc = scatterAt(t);
  const tw = 0.55 + 0.45 * Math.sin(t * 6 + s.ph);
  return [
    s.sx + (s.tx - s.sx) * k + Math.sin(t * 1.3 + s.ph) * 6 * (1 - k) + Math.cos(s.ang) * sc * D * s.far,
    s.sy + (s.ty - s.sy) * k + Math.cos(t * 1.1 + s.ph) * 6 * (1 - k) + Math.sin(s.ang) * sc * D * s.far,
    (0.22 + 0.65 * k * tw) * (1 - sc) * intro,
  ];
}
