// The orb's rings and emblem, drawn the same way by the big orb
// (components/Orb.tsx) and the boot overlay (components/BootOverlay.tsx), so
// the two can never drift apart.
//
// The look (2026-10-27, owner's choice) is the films' JARVIS: a faceless
// holographic HUD in layered cyan — a hairline bezel, a graduated tick scale,
// a heavy segmented band, dashed and hairline rings turning against each
// other, and a bright core ring with three brackets. The colour stays the
// state palette's (lib/orbpalette.ts); nothing here may introduce amber or
// red, which are meanings.

export interface Ring {
  /** Radius as a fraction of the orb's. */
  r: number;
  /** Radial ticks; `len` is their length as a fraction of the radius. */
  ticks?: number;
  len?: number;
  /** Every `major`th tick is drawn `majorLen` long instead. */
  major?: number;
  majorLen?: number;
  /** Arcs as [start, end] radians. */
  arcs?: [number, number][];
  /** `[count, duty]`: count evenly spaced dashes, each `duty` of its slot. */
  dash?: [number, number];
  /** A regular polygon of `poly` sides, `copies` of it interlocked. */
  poly?: number;
  copies?: number;
  w: number;
  speed: number;
  alpha: number;
}

const TAU = Math.PI * 2;

/** Every ring but the outermost: shared, so an avatar's set swaps only that one. */
export const INNER_RINGS: Ring[] = [
  { r: 0.95, ticks: 120, len: 0.03, major: 10, majorLen: 0.085, w: 1, speed: 0.03, alpha: 0.55 },
  {
    r: 0.845,
    arcs: [[0, 0.5], [0.6, 0.75], [0.85, 1.9], [2.2, 2.35], [2.45, 3.6], [3.9, 4.6], [4.7, 4.8], [4.95, 5.9]],
    w: 5, speed: -0.08, alpha: 0.32,
  },
  { r: 0.77, arcs: [[0.3, 2.6], [3.4, 5.5]], w: 1, speed: 0.2, alpha: 0.6 },
  { r: 0.7, dash: [60, 0.5], w: 2.5, speed: -0.14, alpha: 0.42 },
  { r: 0.6, arcs: [[0, 2.4], [2.9, 5.6]], w: 1, speed: 0.3, alpha: 0.45 },
];

/** The outermost ring of the default set: a still hairline bezel. */
export const DEFAULT_RINGS: Ring[] = [{ r: 0.99, arcs: [[0, TAU]], w: 0.75, speed: 0, alpha: 0.35 }, ...INNER_RINGS];

/** Draws one ring at centre (cx, cy), orb radius R, turned by `rot`. */
export function drawRing(ctx: CanvasRenderingContext2D, ring: Ring, cx: number, cy: number, R: number, rot: number) {
  const rr = R * ring.r;
  if (ring.poly) {
    const copies = ring.copies || 1;
    for (let k = 0; k < copies; k++) {
      const spin = rot + (k / copies) * (TAU / ring.poly);
      ctx.beginPath();
      for (let i = 0; i < ring.poly; i++) {
        const a = spin + Math.PI / 2 + (i / ring.poly) * TAU;
        const x = cx + Math.cos(a) * rr;
        const y = cy - Math.sin(a) * rr;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.stroke();
    }
  } else if (ring.ticks) {
    ctx.beginPath();
    for (let i = 0; i < ring.ticks; i++) {
      const a = rot + (i / ring.ticks) * TAU;
      const len = ring.major && i % ring.major === 0 ? ring.majorLen || 0 : ring.len || 0;
      const inner = rr * (1 - len);
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
    }
    ctx.stroke();
  } else if (ring.dash) {
    const [count, duty] = ring.dash;
    const step = TAU / count;
    ctx.beginPath();
    for (let i = 0; i < count; i++) {
      const a = rot + i * step;
      ctx.moveTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
      ctx.arc(cx, cy, rr, a, a + step * duty);
    }
    ctx.stroke();
  } else if (ring.arcs) {
    for (const [a1, a2] of ring.arcs) {
      ctx.beginPath();
      ctx.arc(cx, cy, rr, rot + a1, rot + a2);
      ctx.stroke();
    }
  }
}

/**
 * The emblem where no avatar face is drawn: a bright core ring and three
 * brackets turning slowly around it. `alpha` is its opacity, `hue` the
 * palette's "r,g,b" for the glow, `blur` the glow's size.
 */
export function drawEmblem(
  ctx: CanvasRenderingContext2D, cx: number, cy: number, R: number, rot: number, alpha: number, hue: string, blur: number,
) {
  ctx.strokeStyle = `rgba(240,238,232,${alpha})`;
  ctx.shadowColor = `rgb(${hue})`;
  ctx.shadowBlur = blur;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, R * 0.17, 0, TAU);
  ctx.stroke();
  ctx.lineWidth = 1.5;
  for (let i = 0; i < 3; i++) {
    const a = rot + (i / 3) * TAU;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.25, a, a + 1.2);
    ctx.stroke();
  }
  ctx.shadowBlur = 0;
}
