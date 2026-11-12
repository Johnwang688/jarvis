import { describe, expect, it } from "vitest";
import { FrameStats, STATS_KEY, mountStatsOverlay, statsMode } from "./stats";

describe("stats", () => {
  it("is off unless the key is '1' (or 'fluid')", () => {
    const at = (v: string | null) => statsMode({ getItem: (k) => (k === STATS_KEY ? v : null) });
    expect(at(null)).toBe("off");
    expect(at("0")).toBe("off");
    expect(at("true")).toBe("off");
    expect(at("1")).toBe("on");
    expect(at("fluid")).toBe("fluid");
    expect(statsMode({ getItem: () => { throw new Error("blocked"); } })).toBe("off");
  });

  it("computes fps, the average and the p95 of frame times", () => {
    const s = new FrameStats();
    for (let i = 0; i < 100; i++) s.record(i === 99 ? 20 : 1, 1000 + i * (1000 / 60));
    const snap = s.snapshot();
    expect(snap.fps).toBeGreaterThan(55);
    expect(snap.fps).toBeLessThan(65);
    expect(snap.avg).toBeCloseTo(1.19, 2);
    expect(snap.p95).toBe(1);
    expect(new FrameStats().snapshot()).toEqual({ fps: 0, avg: 0, p95: 0, frames: 0 });
  });

  it("the overlay is aria-hidden and pointer-inert, shows the numbers, and is removed on dispose", () => {
    const s = new FrameStats();
    s.record(2, 0);
    s.record(2, 16);
    const o = mountStatsOverlay(document, s, () => ({ renderer: "SwiftShader", software: true, style: "flow", state: "running", grid: "canvas 960x540" }), () => 1, () => {});
    expect(o.el.getAttribute("aria-hidden")).toBe("true");
    expect(o.el.style.pointerEvents).toBe("none");
    expect(o.el.textContent).toMatch(/flow \(running\).*fps/);
    expect(o.el.textContent).toMatch(/software: SwiftShader/);
    expect(o.el.hasAttribute("tabindex")).toBe(false);
    o.dispose();
    expect(document.querySelector('[data-testid="aurora-stats"]')).toBeNull();
  });
});
