// Acceptance tests: owner decision 2026-11-14, "remove this requirement", scope "Everything". The owner's
// colours (accent, editor & terminal colour, the three aurora colours, the background; hand picks, presets,
// custom presets, stored records) may be ANY hue, amber, red, orange, gold and yellow included. Written by the
// orchestrator before the build; the implementer may not edit this file.
//
// What stays (not part of the decision): only `#rrggbb` ever reaches a CSS custom property (the injection
// guard); the accent and the code accent keep 3:1 on the glass; bright washes are still darkened to the
// wash cap and a bright base to the base cap. The whole-palette blend proof existed only for amber/red, so it
// no longer refuses anything. The meaning colours themselves (--amber, --red in theme.css) do not change.
import { describe, expect, it } from "vitest";
import {
  AMBER, RED, BASE_MAX_LUMINANCE, COLOUR_PRESETS, WASH_MAX_LUMINANCE, checkAccent, checkBase, checkPreset, checkWash,
  colourProps, resolveColours, DEFAULT_COLOURS,
} from "./appearance";
import { luminance } from "./oklch";
import { addCustomPreset, checkCustomPreset, loadCustomPresets } from "./presets";
import css from "../theme.css?raw";

// Warm accents that clear the 3:1 readability floor (which stays): amber, gold, orange, light red, pink.
const WARM = [AMBER, "#ffd700", "#ff9900", "#fb923c", "#ffa3a3", "#ff8a8a", "#ff9ed8"];
const mem = () => {
  const m: Record<string, string> = {};
  return { m, getItem: (k: string) => m[k] ?? null, setItem: (k: string, v: string) => void (m[k] = v) };
};

describe("any hue for the owner's colours", () => {
  it("the accent and the code accent accept amber, red, orange, pink, gold", () => {
    for (const c of WARM) {
      expect(checkAccent(c), c).toMatchObject({ ok: true, value: c });
      const r = resolveColours({ accent: c, codeAccent: c });
      expect(r.accent, c).toBe(c);
      expect(r.codeAccent, c).toBe(c);
    }
  });

  it("a warm wash or base is accepted (darkened only if over its brightness cap)", () => {
    for (const c of ["#c04010", "#ff7a00", "#9a1010", "#b08000", AMBER, RED]) {
      const w = checkWash(c);
      expect(w.ok, c).toBe(true);
      if (w.ok) expect(luminance(w.value)).toBeLessThanOrEqual(WASH_MAX_LUMINANCE + 1e-9);
    }
    for (const c of ["#2a0505", "#201000", "#1a1400"]) {
      const b = checkBase(c);
      expect(b.ok, c).toBe(true);
      if (b.ok) expect(luminance(b.value)).toBeLessThanOrEqual(BASE_MAX_LUMINANCE + 1e-9);
    }
  });

  it("a fully warm palette resolves exactly as stored (no blend fallback)", () => {
    const washes = ["#8a2a10", "#7a4a00", "#6a1a2a"];
    const r = resolveColours({ accent: AMBER, washes, base: "#140806", codeAccent: "#ff9900" });
    expect(r).toEqual({ accent: AMBER, washes, base: "#140806", codeAccent: "#ff9900" });
    expect(colourProps(r)["--wash-1"]).toBe("#8a2a10");
  });

  it("presets and custom presets with warm colours pass, add and load", () => {
    const warm = { id: "warm", name: "Warm", accent: AMBER, washes: ["#8a2a10", "#7a4a00", "#6a1a2a"] as const, base: "#140806" };
    expect(checkPreset(warm)).toBeNull();
    expect(checkCustomPreset({ ...warm, id: "c-warm", washes: [...warm.washes], codeAccent: "#ffa3a3" })).toBeNull();
    const s = mem();
    const r = addCustomPreset("Ember", { accent: AMBER, washes: [...warm.washes], base: warm.base, codeAccent: "#ffa3a3" }, s);
    expect(r.ok).toBe(true);
    expect(loadCustomPresets(s).map((p) => p.name)).toEqual(["Ember"]);
    for (const p of COLOUR_PRESETS) expect(checkPreset(p), p.id).toBeNull();
  });

  it("what stays: only #rrggbb, 3:1 for the accents, the brightness caps", () => {
    for (const bad of ["url(x)", "var(--red)", "red", "#fff", "#12345678", "#ff8800;x:y", "#ff8800\n", null, 5]) {
      expect(checkAccent(bad).ok, String(bad)).toBe(false);
      expect(checkWash(bad).ok, String(bad)).toBe(false);
      expect(checkBase(bad).ok, String(bad)).toBe(false);
    }
    expect(checkAccent("#101418").ok).toBe(false);
    // The error red itself is under 3:1 on the glass (2.5:1): refused for readability, not for its hue.
    const red = checkAccent(RED);
    expect(red.ok).toBe(false);
    if (!red.ok) expect(red.reason).not.toMatch(/amber or red/i);
    expect(resolveColours({ codeAccent: "#2a0a0a" }).codeAccent).toBe(DEFAULT_COLOURS.codeAccent);
    const w = checkWash("#ff4400");
    expect(w.ok && luminance(w.value) <= WASH_MAX_LUMINANCE + 1e-9).toBe(true);
  });

  it("the meaning colours themselves are unchanged in theme.css", () => {
    expect(css).toMatch(/--amber:\s*#fbbf24;/);
    expect(css).toMatch(/--red:\s*#f87171;/);
  });
});
