// OKLCH colour arithmetic for the appearance settings (AG-3): sRGB hex <-> OKLab/OKLCH (Bjorn Ottosson's
// matrices), a gamut mapper that keeps hue and chroma and lowers lightness, and OKLab distance. Pure; no DOM.

export type Lch = { L: number; C: number; h: number };
type Rgb = [number, number, number];

const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const gam = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);

/** `#rrggbb` to 0-255 channels. The caller has validated the string. */
export function hexRgb(hex: string): Rgb {
  const v = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)) as Rgb;
}
export function rgbHex(rgb: Rgb): string {
  return "#" + rgb.map((n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0")).join("");
}

export type Lab = { L: number; a: number; b: number };

export function rgbToLab(rgb: Rgb): Lab {
  const [r, g, b] = rgb.map((n) => lin(n / 255));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return {
    L: 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    a: 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    b: 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  };
}

/** Linear sRGB (unclamped) for a Lab colour. */
function labToLinear(L: number, a: number, b: number): Rgb {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
}

export function hexLch(hex: string): Lch {
  const { L, a, b } = rgbToLab(hexRgb(hex));
  const C = Math.hypot(a, b);
  let h = (Math.atan2(b, a) * 180) / Math.PI;
  if (h < 0) h += 360;
  return { L, C, h: C < 1e-4 ? 0 : h };
}

const inGamut = (rgb: Rgb) => rgb.every((c) => c >= -1e-6 && c <= 1 + 1e-6);

function lchLinear(c: Lch): Rgb {
  const r = (c.h * Math.PI) / 180;
  return labToLinear(c.L, c.C * Math.cos(r), c.C * Math.sin(r));
}

/** Is the OKLCH colour inside sRGB? */
export function lchInGamut(c: Lch): boolean {
  return inGamut(lchLinear(c));
}

/**
 * The OKLCH colour as `#rrggbb`. An out-of-gamut colour is mapped by lowering chroma at the same L and
 * hue (binary search), which keeps hue and lightness; so L, h are what the caller asked for.
 */
export function lchHex(c: Lch): string {
  let { L, C } = c;
  L = Math.max(0, Math.min(1, L));
  if (!inGamut(lchLinear({ L, C, h: c.h }))) {
    let lo = 0, hi = C;
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (inGamut(lchLinear({ L, C: mid, h: c.h }))) lo = mid; else hi = mid;
    }
    C = lo;
  }
  return rgbHex(lchLinear({ L, C, h: c.h }).map((v) => gam(Math.max(0, Math.min(1, v))) * 255) as Rgb);
}

/** Euclidean distance in OKLab (about 0.02 is a just-noticeable difference). */
export function deltaE(a: string, b: string): number {
  const x = rgbToLab(hexRgb(a)), y = rgbToLab(hexRgb(b));
  return Math.hypot(x.L - y.L, x.a - y.a, x.b - y.b);
}

/** WCAG relative luminance of 0-255 channels (not rounded: a composite keeps its fractions). */
export function luminanceRgb(rgb: Rgb): number {
  const [r, g, b] = rgb.map((n) => lin(n / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
/** WCAG relative luminance of `#rrggbb`. */
export const luminance = (hex: string): number => luminanceRgb(hexRgb(hex));

/** Shortest signed hue difference a - b in degrees, -180..180. */
export function hueDiff(a: number, b: number): number {
  return ((((a - b) % 360) + 540) % 360) - 180;
}
