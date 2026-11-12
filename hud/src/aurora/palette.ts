// The aurora's colours (AG-2): `--base` and `--wash-1..3` read from `:root` (theme.css, and AG-3 sets them
// at runtime), and the one function that maps the fluid's dye to a displayed colour.
//
// CONTRAST BOUND (owner D-8): the glass contrast tests (lib/glass.test.ts) assume the background is never
// brighter than the brightest wash colour at full strength. So the displayed colour is only ever a convex
// mix of --base and the three washes, with no bloom, no additive light and no white highlight, and no
// shading term at all. `displayColor` below is that mapping in JS; the GLSL `palette()` in gl.ts is the
// same arithmetic (linear-light mix, then sRGB), and `aurora/palette.test.ts` asserts the bound on this
// function. The shader itself cannot be unit-tested in jsdom, so the two are kept line for line.

/** An sRGB colour, each channel 0..1. */
export type Rgb = readonly [number, number, number];

export interface AuroraPalette {
  base: Rgb;
  /** The three wash colours, in the order --wash-1, --wash-2, --wash-3. */
  colours: readonly [Rgb, Rgb, Rgb];
}

/** The shipped values (theme.css), used when a token is missing or does not parse. */
export const DEFAULT_PALETTE: AuroraPalette = {
  base: [0x0a / 255, 0x11 / 255, 0x16 / 255],
  colours: [
    [0x2a / 255, 0x8f / 255, 0xa0 / 255],
    [0x34 / 255, 0x68 / 255, 0xc4 / 255],
    [0x64 / 255, 0x50 / 255, 0xc4 / 255],
  ],
};

const HEX = /^#([0-9a-f]{6})$/i;
const RGB = /^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*[\d.]+%?\s*)?\)$/i;

/** `#rrggbb` or `rgb(r, g, b)` to 0..1 channels; null for anything else (never throws). */
export function parseColour(text: string): Rgb | null {
  const s = text.trim();
  const h = HEX.exec(s);
  if (h) {
    const n = parseInt(h[1]!, 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  const r = RGB.exec(s);
  if (r) {
    const c = [Number(r[1]), Number(r[2]), Number(r[3])];
    if (c.every((v) => v <= 255)) return [c[0]! / 255, c[1]! / 255, c[2]! / 255];
  }
  return null;
}

/** The palette from the tokens on `el` (default `:root`); each token that does not parse keeps its default. */
export function readPalette(el: Element = document.documentElement): AuroraPalette {
  let cs: CSSStyleDeclaration | null = null;
  try {
    cs = getComputedStyle(el);
  } catch {
    cs = null;
  }
  const get = (name: string, dflt: Rgb): Rgb => {
    let v = '';
    try {
      v = cs?.getPropertyValue(name) ?? '';
    } catch {
      v = '';
    }
    return parseColour(v) ?? dflt;
  };
  return {
    base: get('--base', DEFAULT_PALETTE.base),
    colours: [
      get('--wash-1', DEFAULT_PALETTE.colours[0]),
      get('--wash-2', DEFAULT_PALETTE.colours[1]),
      get('--wash-3', DEFAULT_PALETTE.colours[2]),
    ],
  };
}

/** Whether two palettes hold the same colours. */
export function samePalette(a: AuroraPalette, b: AuroraPalette): boolean {
  const eq = (x: Rgb, y: Rgb) => x[0] === y[0] && x[1] === y[1] && x[2] === y[2];
  return eq(a.base, b.base) && a.colours.every((c, i) => eq(c, b.colours[i]!));
}

/** sRGB to linear light, per channel. */
export const toLinear = (v: number) => (v < 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
const toSrgbChannel = (c: number) => {
  c = Math.min(Math.max(c, 0), 1);
  return c < 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
};

/** WCAG relative luminance of an sRGB colour (0..1 channels). */
export function luminance(c: Rgb): number {
  return 0.2126 * toLinear(c[0]) + 0.7152 * toLinear(c[1]) + 0.0722 * toLinear(c[2]);
}

/** The brightest of --base and the three washes: the most light the background may ever show. */
export function brightestLuminance(p: AuroraPalette): number {
  return Math.max(luminance(p.base), ...p.colours.map(luminance));
}

/** The dither's range in sRGB (one 8-bit step). It only ever darkens: see `displayColor`. */
export const DITHER = 1 / 255;

/**
 * The displayed colour for dye weights `w` (one per wash) and a dither sample `d` in 0..1, as the
 * GLSL `palette()` computes it: each weight clamped to 0..1, the washes mixed over --base in linear
 * light (so every channel is a convex combination of the palette's own channels, and so is the
 * luminance), then to sRGB, then dithered by 0..-1/255 (never brightening, so the bound is exact).
 * Nothing is added to the mix: no bloom, no highlight, no shading term.
 */
export function displayColor(p: AuroraPalette, w: readonly [number, number, number], d = 0.5): Rgb {
  const wc = w.map((v) => (Number.isFinite(v) ? Math.min(Math.max(v, 0), 1) : 0));
  const lin = (c: Rgb) => c.map(toLinear) as unknown as [number, number, number];
  let c = lin(p.base);
  const mixInto = (to: [number, number, number], k: number) => {
    c = [c[0] + (to[0] - c[0]) * k, c[1] + (to[1] - c[1]) * k, c[2] + (to[2] - c[2]) * k];
  };
  mixInto(lin(p.colours[2]), wc[2]!);
  mixInto(lin(p.colours[1]), wc[1]!);
  mixInto(lin(p.colours[0]), wc[0]!);
  const dither = (d - 1) * DITHER;
  return [toSrgbChannel(c[0]) + dither, toSrgbChannel(c[1]) + dither, toSrgbChannel(c[2]) + dither];
}
