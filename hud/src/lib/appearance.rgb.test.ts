// Acceptance tests for the Pink preset and RGB mode's colour model (owner 2026-11-13). Written by the
// orchestrator before the build; the implementer may not edit this file.
//
// Owner decision 2026-11-13: RGB mode cycles the aurora through the FULL wheel, red, orange and yellow
// included (it loosens the amber/red rule for the aurora background in RGB mode only). The accent stays
// white, and every wash keeps the D-8 luminance cap so text stays readable.
import { describe, expect, it } from "vitest";
import {
  BASE_MAX_LUMINANCE, COLOUR_PRESETS, DEFAULT_CODE_ACCENT, DEFAULT_APPEARANCE, RGB_ACCENT, RGB_BASE, WASH_MAX_LUMINANCE,
  checkAccent, checkPreset, clampRgbSpeed, parseAppearance, resolveColours, rgbWashes, saveAppearance,
  APPEARANCE_KEY,
} from "./appearance";
import { hexLch, hueDiff, luminance } from "./oklch";

const memStorage = () => {
  const m: Record<string, string> = {};
  return { m, getItem: (k: string) => m[k] ?? null, setItem: (k: string, v: string) => void (m[k] = v) };
};

describe("Pink preset", () => {
  it("ships after Orchid, as soft pink over plum, and passes every guard unclamped", () => {
    const ids = COLOUR_PRESETS.map((p) => p.id);
    expect(ids.indexOf("pink")).toBe(ids.indexOf("orchid") + 1);
    const pink = COLOUR_PRESETS.find((p) => p.id === "pink")!;
    expect(pink).toMatchObject({ name: "Pink", accent: "#f9a8d4", washes: ["#9a2f6e", "#6a2f8a", "#7a2f7e"], base: "#140a12" });
    expect(checkPreset(pink)).toBeNull();
  });
});

describe("RGB colour model", () => {
  it("defaults: RGB off, 30 seconds per cycle", () => {
    expect(DEFAULT_APPEARANCE.rgb).toBe(false);
    expect(DEFAULT_APPEARANCE.rgbSpeed).toBe(30);
    expect(parseAppearance(null)).toMatchObject({ rgb: false, rgbSpeed: 30 });
  });

  it("rgb is on only for a stored boolean true; rgbSpeed is clamped to 8..120 and garbage is 30", () => {
    expect(parseAppearance(JSON.stringify({ rgb: true })).rgb).toBe(true);
    for (const bad of ["true", 1, null, {}]) expect(parseAppearance(JSON.stringify({ rgb: bad })).rgb).toBe(false);
    expect(clampRgbSpeed(1)).toBe(8);
    expect(clampRgbSpeed(999)).toBe(120);
    expect(clampRgbSpeed(45)).toBe(45);
    for (const bad of [NaN, Infinity, "30", null, undefined]) expect(clampRgbSpeed(bad)).toBe(30);
    expect(parseAppearance(JSON.stringify({ rgbSpeed: 3 })).rgbSpeed).toBe(8);
    const s = memStorage();
    saveAppearance({ rgbSpeed: 5000 }, s);
    expect(JSON.parse(s.m[APPEARANCE_KEY]!).rgbSpeed).toBe(120);
  });

  it("rgbWashes: three #rrggbb colours 120 degrees apart, all under the wash luminance cap", () => {
    for (let k = 0; k < 72; k++) {
      const w = rgbWashes(k / 72);
      expect(w).toHaveLength(3);
      for (const c of w) {
        expect(c).toMatch(/^#[0-9a-f]{6}$/);
        expect(luminance(c)).toBeLessThanOrEqual(WASH_MAX_LUMINANCE + 1e-9);
      }
      const h = w.map((c) => hexLch(c).h);
      // Gamut mapping and 8-bit rounding move a hue a little; 120 apart within 12 degrees.
      expect(Math.abs(Math.abs(hueDiff(h[1]!, h[0]!)) - 120)).toBeLessThan(12);
      expect(Math.abs(Math.abs(hueDiff(h[2]!, h[1]!)) - 120)).toBeLessThan(12);
    }
  });

  it("rgbWashes wraps (phase 0 = 1 = -1 = 2), moves with the phase, and visits the whole wheel, warm hues included", () => {
    expect(rgbWashes(1)).toEqual(rgbWashes(0));
    expect(rgbWashes(-1)).toEqual(rgbWashes(0));
    expect(rgbWashes(2.25)).toEqual(rgbWashes(0.25));
    expect(rgbWashes(0.5)).not.toEqual(rgbWashes(0));
    const firstHues = Array.from({ length: 36 }, (_, k) => hexLch(rgbWashes(k / 36)[0]).h);
    // Every 30-degree sector of the wheel is reached by wash 1 over one cycle, warm band (5..105) included.
    for (let sector = 0; sector < 360; sector += 30) {
      expect(firstHues.some((h) => Math.abs(hueDiff(h, sector + 15)) <= 20), `sector ${sector}`).toBe(true);
    }
    expect(firstHues.some((h) => h > 20 && h < 90)).toBe(true);
  });

  it("rgbWashes never throws on a non-finite phase (it is phase 0)", () => {
    expect(rgbWashes(NaN)).toEqual(rgbWashes(0));
    expect(rgbWashes(Infinity)).toEqual(rgbWashes(0));
  });

  it("the white accent stays readable over every palette in the cycle; the base is under its cap", () => {
    expect(hexLch(RGB_ACCENT).C).toBeLessThan(0.04);
    for (let k = 0; k < 360; k += 5) expect(checkAccent(RGB_ACCENT, rgbWashes(k / 360)).ok, `phase ${k}`).toBe(true);
    expect(luminance(RGB_BASE)).toBeLessThanOrEqual(BASE_MAX_LUMINANCE);
  });

  it("resolveColours under RGB: white accent, the phase-0 washes and the RGB base, whatever else is stored", () => {
    const c = resolveColours({ rgb: true, accent: "#c4a8ff", washes: ["#5b3fb8", "#7a3aa8", "#3a46b8"], base: "#0d0b1a" });
    expect(c).toEqual({ accent: RGB_ACCENT, washes: rgbWashes(0), base: RGB_BASE, codeAccent: DEFAULT_CODE_ACCENT });
    // RGB off: the stored colours come back untouched.
    expect(resolveColours({ rgb: false, accent: "#c4a8ff" }).accent).toBe("#c4a8ff");
  });

  it("hand-picked colours keep the amber/red guard (the loosening is RGB mode only)", () => {
    expect(resolveColours({ washes: ["#c04010", "#3468c4", "#6450c4"] }).washes[0]).toBe("#2a8fa0");
  });
});
