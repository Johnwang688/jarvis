import { describe, expect, it } from "vitest";
import { ACCENT_CHROMA_FLOOR, AMBER, BASE_MAX_LUMINANCE, COLOUR_PRESETS, DEFAULT_APPEARANCE, DEFAULT_COLOURS, RGB_ACCENT, RGB_BASE, WASH_CHROMA_FLOOR, WASH_MAX_LUMINANCE, accentContrast, checkPreset, colourProps, impersonatesMeaning, parseAppearance, resolveColours, rgbWashes } from "./appearance";
import { luminance } from "./oklch";
import { rgbPalette } from "../aurora/rgb";

describe("independent colour boundaries", () => {
  it("every RGB table entry matches rgbWashes at the same phase", () => {
    for (let d = 0; d < 360; d++) {
      const phase = (d + 0.5) / 360;
      const actual = rgbPalette(phase).colours.map(c => "#" + c.map(v => Math.round(v * 255).toString(16).padStart(2, "0")).join(""));
      const expected = rgbWashes(phase);
      actual.forEach((w, i) => expect(w, "degree " + d + ", wash " + (i + 1)).toBe(expected[i]));
    }
  });
  it("every RGB phase retains its luminance and accent contrast bounds", () => {
    expect(luminance(RGB_BASE)).toBeLessThanOrEqual(BASE_MAX_LUMINANCE);
    for (let d = 0; d < 360; d++) {
      const washes = rgbWashes((d + 0.5) / 360);
      for (const w of washes) expect(luminance(w)).toBeLessThanOrEqual(WASH_MAX_LUMINANCE);
      expect(accentContrast(RGB_ACCENT, washes)).toBeGreaterThanOrEqual(3);
    }
  });
  it.each([false, null, 0, 1, "true", "false", [], {}])("only boolean true can bypass the warm-colour guard: %j", rgb => {
    const raw = { rgb, accent: AMBER, codeAccent: AMBER, washes: ["#c04010", "#ff7a00", "#ffcc00"], base: "#ff0000" };
    for (const record of [raw, parseAppearance(JSON.stringify(raw))]) {
      const c = resolveColours(record as any);
      expect(c).toEqual(DEFAULT_COLOURS);
      expect(impersonatesMeaning(c.accent, ACCENT_CHROMA_FLOOR)).toBe(false);
      expect(impersonatesMeaning(c.codeAccent, ACCENT_CHROMA_FLOOR)).toBe(false);
      for (const w of c.washes) expect(impersonatesMeaning(w, WASH_CHROMA_FLOOR)).toBe(false);
    }
  });
  it.each(["url(x)", "var(--red)", "red", "#fff", "#12345678", "#c0aef5;--evil:red", "#c0aef5\n", "#c0aef5\r"]) ("hostile codeAccent cannot reach an opaque CSS colour: %j", codeAccent => {
    const c = resolveColours(parseAppearance(JSON.stringify({ ...DEFAULT_APPEARANCE, codeAccent })));
    expect(c.codeAccent).toHaveLength(7);
    expect(colourProps(c)["--code-accent"]).toBe(DEFAULT_COLOURS.codeAccent);
  });
  it("RGB never exempts the independent code accent from the meaning guard", () => {
    for (const codeAccent of [AMBER, "#f87171", "#ff9900"]) {
      expect(resolveColours({ rgb: true, codeAccent }).codeAccent).toBe(DEFAULT_COLOURS.codeAccent);
    }
  });
  it("all shipped presets still pass the complete static guard", () => {
    for (const p of COLOUR_PRESETS) expect(checkPreset(p), p.id).toBeNull();
  });
});
