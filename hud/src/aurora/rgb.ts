// RGB mode's aurora palette (owner 2026-11-13): the RGB base and the three washes of `rgbWashes(phase)`
// (lib/appearance.ts), as 0..1 sRGB channels for the renderer. The 360 palettes (one per degree of hue)
// are made once, so the controller's per-frame call is a lookup and allocates nothing. Every wash is under
// the D-8 luminance cap (rgbWashes), so the contrast bound in palette.ts still holds at every phase.
import { RGB_BASE, RGB_TABLE, rgbDegree } from '../lib/appearance';
import { parseColour, type AuroraPalette, type Rgb } from './palette';

const BASE = parseColour(RGB_BASE) as Rgb;
const TABLE: readonly AuroraPalette[] = Array.from({ length: 360 }, (_, d) => {
  // Degree d exactly as `rgbWashes` builds it (no phase round trip, which can land on the previous degree).
  const w = [RGB_TABLE[d]!, RGB_TABLE[(d + 120) % 360]!, RGB_TABLE[(d + 240) % 360]!];
  return { base: BASE, colours: [parseColour(w[0]!) as Rgb, parseColour(w[1]!) as Rgb, parseColour(w[2]!) as Rgb] };
});

/** The palette `phase` of the way through a cycle (0..1 is a full turn; it wraps, and non-finite is 0). */
export function rgbPalette(phase: number): AuroraPalette {
  return TABLE[rgbDegree(phase)]!;
}
