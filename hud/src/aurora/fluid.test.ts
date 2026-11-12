import { describe, expect, it } from "vitest";
import { DYE_MAX_AREA, ENERGY_TAU, driftScales, SIM_MAX_AREA, gridFor, size } from "./fluid";
import { createFluid } from "./fluid";
import { fakeGL } from "./fakegl";
import { DEFAULT_PALETTE } from "./palette";

describe("the simulation grid is capped by area", () => {
  it.each([[16 / 9], [21 / 9], [32 / 9], [1], [9 / 16], [1 / 4]])("aspect %f stays under both caps", (aspect) => {
    const g = gridFor(Math.round(1000 * aspect), 1000);
    expect(g.sim.w * g.sim.h).toBeLessThanOrEqual(SIM_MAX_AREA * 1.05);
    expect(g.dye.w * g.dye.h).toBeLessThanOrEqual(DYE_MAX_AREA * 1.05);
  });

  it("a 4K window costs what a small one does (the grid follows aspect, not pixels)", () => {
    expect(gridFor(3840, 2160)).toEqual(gridFor(960, 540));
  });

  it("keeps the aspect, and a small area is not scaled up past the short side", () => {
    expect(size(100, 2, 1e9)).toEqual({ w: 200, h: 100 });
    expect(size(100, 0.5, 1e9)).toEqual({ w: 100, h: 200 });
    const capped = size(100, 4, 10000);
    expect(capped.w * capped.h).toBeLessThanOrEqual(10500);
    expect(capped.w / capped.h).toBeCloseTo(4, 0);
  });
});

describe("fluid", () => {
  it("is built on a GPU and refused on software unless allowed", () => {
    const gpu = fakeGL();
    const f = createFluid(gpu.gl, DEFAULT_PALETTE, 500, 300);
    expect(f?.style).toBe("fluid");
    f?.release();
    expect(gpu.live()).toBe(0);
    const sw = fakeGL({ renderer: "SwiftShader" });
    expect(createFluid(sw.gl, DEFAULT_PALETTE, 500, 300)).toBeNull();
    expect(createFluid(sw.gl, DEFAULT_PALETTE, 500, 300, true)?.style).toBe("fluid");
  });

  it("stirs and ripples do not throw, even with extreme input, and leave nothing live after release", () => {
    const g = fakeGL();
    const f = createFluid(g.gl, DEFAULT_PALETTE, 500, 300)!;
    f.step(1 / 60);
    f.stir(0.5, 0.5, 5, -5, 9, 7);
    f.stir(0.5, 0.5, NaN, 0, 1, 0);
    f.ripple(0.5, 0.5, 1, 2);
    f.ripple(0.5, 0.5, 0, 2);
    f.step(1 / 60);
    f.draw();
    f.release();
    f.release();
    expect(g.live()).toBe(0);
  });

  it("a stirred fluid's real parameters (vorticity) settle back to rest", () => {
    const g = fakeGL();
    const f = createFluid(g.gl, DEFAULT_PALETTE, 500, 300)!;
    const read = () => ({ curl: g.state.uniforms.uCurlK! });
    for (let i = 0; i < 60; i++) f.step(1 / 60);
    const rest = read();
    expect(Number.isFinite(rest.curl)).toBe(true);
    for (let i = 0; i < 8; i++) f.stir(0.5, 0.5, 0.2, 0.1, 1, 0);
    f.step(1 / 60);
    const stirred = read();
    expect(stirred.curl).toBeGreaterThan(rest.curl + 0.5); // stirring raises the vorticity
    let prev = stirred.curl;
    for (let sec = 1; sec <= 4; sec++) {
      for (let i = 0; i < 60; i++) f.step(1 / 60);
      const now = read().curl;
      expect(now).toBeLessThan(prev); // settling, every second
      prev = now;
    }
    // four seconds on (2.5 time constants), most of the stir is gone; ten, all of it
    expect(prev - rest.curl).toBeLessThan((stirred.curl - rest.curl) * 0.25);
    for (let i = 0; i < 6 * 60; i++) f.step(1 / 60);
    expect(read().curl).toBeCloseTo(rest.curl, 6);
    f.release();
  });

  it("the energy constant is short enough to settle in a few seconds", () => {
    expect(ENERGY_TAU).toBeLessThanOrEqual(2.5);
  });
});

describe("natural drift strength", () => {
  const make = (drift?: number) => {
    const g = fakeGL();
    const f = createFluid(g.gl, DEFAULT_PALETTE, 500, 300)!;
    if (drift !== undefined) f.setDrift(drift);
    return { g, f };
  };
  /** One step's velocity splat (the ambient force) and dye splat, as uploaded. */
  const stepOnce = (g: ReturnType<typeof fakeGL>, f: ReturnType<typeof make>["f"]) => {
    f.step(1 / 60);
    const c = g.state.colours;
    return { vel: c[c.length - 2]!, dye: c[c.length - 1]!, curl: g.state.uniforms.uCurlK! };
  };

  it("100% is today's tuning exactly (rest curl 3, force 1x), and the default is 100%", () => {
    expect(driftScales(1)).toEqual({ path: 1, force: 1, curlRest: 3 });
    const a = make();
    const b = make(1);
    for (let i = 0; i < 30; i++) {
      const x = stepOnce(a.g, a.f);
      const y = stepOnce(b.g, b.f);
      expect(y).toEqual(x);
    }
    expect(stepOnce(a.g, a.f).curl).toBe(3);
  });

  it("0% has no ambient force and rest curl at its floor, but still feeds the dye", () => {
    expect(driftScales(0)).toEqual({ path: 0, force: 0, curlRest: 0 });
    const { g, f } = make(0);
    for (let i = 0; i < 5; i++) {
      const r = stepOnce(g, f);
      expect(r.vel.every((v) => v === 0)).toBe(true);
      expect(r.dye.some((v) => v > 0)).toBe(true);
      expect(r.curl).toBe(0);
    }
  });

  it("at 0% a stir still raises the vorticity and it settles back to the floor", () => {
    const { g, f } = make(0);
    for (let i = 0; i < 8; i++) f.stir(0.5, 0.5, 0.2, 0.1, 1, 0);
    expect(stepOnce(g, f).curl).toBeGreaterThan(3);
    for (let i = 0; i < 10 * 60; i++) f.step(1 / 60);
    expect(g.state.uniforms.uCurlK).toBeCloseTo(0, 6);
  });

  it("200% is livelier at rest but its vorticity stays far below the stirred one", () => {
    const hi = driftScales(2);
    expect(hi.force).toBe(2);
    expect(hi.curlRest).toBeGreaterThan(3);
    expect(hi.curlRest).toBeLessThan(5);
    const { g, f } = make(2);
    const r = stepOnce(g, f);
    expect(r.curl).toBeLessThan(11 / 2);
    expect(r.vel.some((v) => v !== 0)).toBe(true);
  });

  it("out-of-range or garbage values are clamped, never NaN", () => {
    expect(driftScales(9).force).toBe(2);
    expect(driftScales(-3).force).toBe(0);
    expect(driftScales(NaN)).toEqual(driftScales(1));
  });
});
