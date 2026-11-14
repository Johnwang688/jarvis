import { RGB_BASE, rgbWashes } from "../lib/appearance";
import { parseColour } from "./palette";
import { rgbPalette } from "./rgb";

// Adjacent representable numbers expose quantization errors hidden by ordinary epsilon choices.
function adjacent(x: number, up: boolean): number {
  if (x === 0) return up ? Number.MIN_VALUE : -Number.MIN_VALUE;
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, x);
  const bits = view.getBigUint64(0);
  view.setBigUint64(0, bits + (up === (x > 0) ? 1n : -1n));
  return view.getFloat64(0);
}
function agreement(phase: number) {
  const p = rgbPalette(phase);
  expect(p.base, `base at ${phase}`).toEqual(parseColour(RGB_BASE));
  expect(p.colours, `washes at ${phase}`).toEqual(rgbWashes(phase).map(parseColour));
  expect(rgbPalette(phase), `cached at ${phase}`).toBe(p);
}

it("palette and washes agree on both adjacent floats at every positive and negative degree boundary", () => {
  for (let d = -720; d <= 720; d++) {
    const phase = d / 360;
    agreement(adjacent(phase, false));
    agreement(phase);
    agreement(adjacent(phase, true));
    agreement((d + 0.5) / 360);
  }
});

it.each([
  -0, Number.MIN_VALUE, -Number.MIN_VALUE, Number.EPSILON, -Number.EPSILON,
  1e6 + 0.123, -1e6 - 0.123, 1e12 + 0.125, -1e12 - 0.125,
  2 ** 40 + 1 / 360, -(2 ** 40) - 1 / 360,
  Number.MAX_SAFE_INTEGER - 0.5, -Number.MAX_SAFE_INTEGER + 0.5,
  Number.MAX_VALUE, -Number.MAX_VALUE, Infinity, -Infinity, NaN,
])("palette and washes agree for an extreme phase %s", phase => agreement(phase));

it("both APIs normalize exact whole turns and nonfinite inputs to the phase-zero palette", () => {
  for (const phase of [-1e100, -1000, -1, -0, 0, 1, 1000, 1e100, NaN, Infinity, -Infinity]) {
    expect(rgbPalette(phase)).toBe(rgbPalette(0));
    expect(rgbWashes(phase)).toEqual(rgbWashes(0));
  }
  expect(rgbWashes(-0.25)).toEqual(rgbWashes(0.75));
  expect(rgbPalette(-0.25)).toBe(rgbPalette(0.75));
});
