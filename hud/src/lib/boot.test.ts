import { describe, expect, it } from "vitest";
import { BOOT_RINGS, FADE_OUT_S, HARD_CAP_MS, NAME_SPARKS, SHIELD_ABSOLUTE_MS, SHIELD_DEADLINE_MS, SHIELD_MS, ORB_RADIUS_FRACTION, TIMELINE, TOTAL_S, auroraAt, buildSparks,
  dockAt, dockFromRect, fallbackDock, geoFor, handoffAlpha, maskPoints, mulberry32, orbAt, ringArrive, ringLand, ringLock,
  ringRot, ringSolid, scatterAt, shouldPlay, sparkAt, hueOf, sparkLavender } from "./boot";

describe("boot eligibility", () => {
  for (let mask = 0; mask < 32; mask++) {
    const [webdriver, forced, reducedMotion, seen, cardUp] = [0, 1, 2, 3, 4].map(i => !!(mask & (1 << i)));
    it(`facts ${JSON.stringify({ webdriver, forced, reducedMotion, seen, cardUp })}`, () => {
      expect(shouldPlay({ webdriver, forced, reducedMotion, seen, cardUp }))
        .toBe((!webdriver || forced) && !reducedMotion && !seen && !cardUp);
    });
  }
});

/** A grid of name points standing in for the sampled font: a block 600 x 120 under the orb. */
function blockPoints(W: number, H: number) {
  const g = geoFor(W, H), W2 = 300, rgba = new Uint8Array(W * H * 4);
  for (let y = Math.round(g.wy - 60); y < g.wy + 60; y++) for (let x = Math.round(g.cx - W2); x < g.cx + W2; x++) rgba[(y * W + x) * 4 + 3] = 255;
  return { g, points: maskPoints(rgba, W, H, g.wsize) };
}

describe("the seeded spark set", () => {
  const W = 1600, H = 1000, { g, points } = blockPoints(W, H);
  const first = buildSparks(W, H, g, points);
  it("is deterministic, and the seed's sequence repeats", () => {
    expect(buildSparks(W, H, g, points)).toEqual(first);
    const a = mulberry32(23), b = mulberry32(23), c = mulberry32(24);
    const seq = (r: () => number) => Array.from({ length: 50 }, r);
    expect(seq(a)).toEqual(seq(b));
    expect(seq(mulberry32(23))).not.toEqual(seq(c));
  });
  it("has the ring sparks and up to 1000 name sparks", () => {
    const ring = first.filter(s => s.ring >= 0), name = first.filter(s => s.ring < 0);
    expect(ring.length).toBe(BOOT_RINGS.reduce((a, r) => a + r.n, 0));
    expect(ring.length).toBeGreaterThan(550);
    expect(name.length).toBe(Math.min(NAME_SPARKS, points.length));
    BOOT_RINGS.forEach((r, i) => expect(ring.filter(s => s.ring === i).length).toBe(r.n));
  });
  it("stays on its own seed's ring: ring sparks never depend on the name", () => {
    const other = buildSparks(W, H, g, points.slice(0, 50));
    expect(other.filter(s => s.ring >= 0)).toEqual(first.filter(s => s.ring >= 0));
  });
});

describe("the timeline", () => {
  const W = 1600, H = 1000, { g, points } = blockPoints(W, H);
  const sparks = buildSparks(W, H, g, points);
  const dock = fallbackDock(W, H);

  it("lands each ring's sparks on the ring by the ring's land time, outer ring first", () => {
    BOOT_RINGS.forEach((ring, i) => {
      if (i > 0) expect(ringLand(i)).toBeGreaterThan(ringLand(i - 1));
      const t = ringArrive(i), o = orbAt(g, dock, t);
      for (const s of sparks.filter(x => x.ring === i)) {
        const [x, y] = sparkAt(s, t, o, Math.max(W, H));
        const ang = s.a + ringRot(i, t);
        expect(x).toBeCloseTo(o.cx + Math.cos(ang) * o.R * ring.r, 6);
        expect(y).toBeCloseTo(o.cy + Math.sin(ang) * o.R * ring.r, 6);
      }
    });
  });
  it("turns each ring solid after its sparks land, and not before they fly", () => {
    BOOT_RINGS.forEach((_, i) => {
      expect(ringSolid(i, ringLand(i) - 0.1)).toBe(0);
      expect(ringSolid(i, ringArrive(i))).toBeGreaterThan(0.5);
      expect(ringSolid(i, ringLand(i) + 0.35)).toBe(1);
      expect(ringSolid(i, TIMELINE.end)).toBe(1);
    });
  });
  it("spins each ring fast as it forms, then locks it to a slow constant turn (the spring settles)", () => {
    BOOT_RINGS.forEach((ring, i) => {
      const rate = (t: number) => (ringRot(i, t + 0.001) - ringRot(i, t)) / 0.001;
      const slow = ring.dir * 0.35 * ring.slow, settled = ringLand(i) + 1.1;
      expect(Math.abs(rate(ringLand(i) + 0.2))).toBeGreaterThan(Math.abs(slow) * 4);
      expect(ringLock(i, settled)).toBeGreaterThan(0.97);
      for (const t of [settled, settled + 0.3, TIMELINE.end]) expect(rate(t)).toBeCloseTo(slow, 6);
      expect(ringLock(i, settled + 1)).toBe(ringLock(i, settled)); // it holds
    });
  });
  it("lights the core, then scatters the name, then docks: in that order", () => {
    expect(TIMELINE.ignite).toBeLessThan(TIMELINE.scatterStart);
    expect(TIMELINE.scatterStart).toBeLessThan(TIMELINE.dockStart);
    expect(TIMELINE.dockStart).toBeLessThan(TIMELINE.dockEnd);
    expect(TIMELINE.scatterEnd).toBeLessThan(TIMELINE.dockEnd);
    // Every ring has landed before the ignition, and every name spark is on its letter.
    expect(ringArrive(BOOT_RINGS.length - 1)).toBeLessThan(TIMELINE.ignite);
    expect(scatterAt(TIMELINE.ignite + 0.2)).toBe(0);
    expect(scatterAt(TIMELINE.scatterEnd)).toBe(1);
    expect(dockAt(TIMELINE.dockStart)).toBe(0);
    expect(dockAt(TIMELINE.dockEnd)).toBe(1);
    expect(auroraAt(TIMELINE.ignite + 1.5)).toBeGreaterThan(auroraAt(TIMELINE.ignite) + 0.5);
    // The name is spelled (every spark on its letter) before the scatter begins.
    const spelled = 2.1, o = orbAt(g, dock, spelled);
    expect(spelled).toBeGreaterThan(TIMELINE.ignite);
    expect(spelled).toBeLessThan(TIMELINE.scatterStart);
    for (const s of sparks.filter(x => x.ring < 0)) {
      const [x, y] = sparkAt(s, spelled, o, 1600);
      expect([x, y]).toEqual([s.tx, s.ty]);
    }
  });
  it("the name's sparks are gone before the orb is docked, and the crossfade hands over to the HUD", () => {
    const o = orbAt(g, dock, TIMELINE.dockEnd);
    for (const s of sparks) expect(sparkAt(s, TIMELINE.dockEnd, o, 1600)[2]).toBeLessThan(0.16);
    expect(handoffAlpha(TIMELINE.end)).toBe(1);
    expect(handoffAlpha(TIMELINE.end + FADE_OUT_S)).toBe(0);
    expect(handoffAlpha(TIMELINE.end + FADE_OUT_S / 2)).toBeCloseTo(0.5, 6);
  });
  it("totals no more than the hard cap, with room to spare", () => {
    expect(TOTAL_S).toBe(TIMELINE.end + FADE_OUT_S);
    expect(TOTAL_S * 1000).toBeLessThanOrEqual(HARD_CAP_MS);
    expect(TOTAL_S).toBeGreaterThan(3.9);
    expect(TOTAL_S).toBeLessThan(4.3);
  });
  it("the shield's bounds: held-pointer deadline at cap + SHIELD_MS, absolute bound one SHIELD_MS after that", () => {
    expect(HARD_CAP_MS).toBe(7_000);
    expect(SHIELD_MS).toBe(450);
    expect(SHIELD_DEADLINE_MS).toBe(7_450);
    expect(SHIELD_ABSOLUTE_MS).toBe(7_900);
    expect(SHIELD_ABSOLUTE_MS).toBe(SHIELD_DEADLINE_MS + SHIELD_MS);
  });
});

describe("docking into the real orb", () => {
  const W = 1600, H = 1000;
  it("lands the orb's centre and radius exactly on the measured orb", () => {
    const rect = { left: 10, top: 858, width: 132, height: 132 }, d = dockFromRect(rect, W, H);
    expect(d).toEqual({ x: 76, y: 924, R: 132 * ORB_RADIUS_FRACTION });
    expect(d.R).toBeCloseTo(63, 9);
    const g = geoFor(W, H), end = orbAt(g, d, TIMELINE.dockEnd);
    expect([end.cx, end.cy, end.R]).toEqual([76, 924, d.R]);
    const start = orbAt(g, d, 0);
    expect([start.cx, start.cy, start.R]).toEqual([g.cx, g.cy, g.R]);
  });
  it("follows a scaled (folded sidebar) orb", () => {
    expect(dockFromRect({ left: 2, top: 900, width: 32, height: 32 }, W, H).R).toBeCloseTo(32 * ORB_RADIUS_FRACTION, 9);
  });
  it("falls back to the concept's spot when the orb is absent, hidden or off the window", () => {
    const f = fallbackDock(W, H);
    expect(f).toEqual({ x: 116, y: H - 116, R: 56 });
    expect(fallbackDock(700, 800).x).toBe(700 - 64);
    for (const r of [null, { left: 0, top: 0, width: 0, height: 0 }, { left: 10, top: 10, width: 4, height: 4 },
      { left: -300, top: 900, width: 132, height: 132 }, { left: 2000, top: 10, width: 132, height: 132 }]) {
      expect(dockFromRect(r, W, H)).toEqual(f);
    }
  });
});

describe("the spark lavender", () => {
  it("is the lifted --wash-3, close to the concept's lavender for the default palette", () => {
    const c = sparkLavender([0x64 / 255, 0x50 / 255, 0xc4 / 255], [0x6f / 255, 0xc3 / 255, 0xdf / 255]);
    const concept = [176, 166, 240];
    c.forEach((v, i) => expect(Math.abs(v * 255 - concept[i]!)).toBeLessThan(20));
  });
  it("follows a custom wash and never lands in the amber or red band", () => {
    const green = sparkLavender([0.2, 0.7, 0.3], [0.4, 0.8, 0.9]);
    expect(Math.abs(hueOf(green) - hueOf([0.2, 0.7, 0.3]))).toBeLessThan(1);
    for (const wash of [[0.9, 0.1, 0.1], [0.9, 0.6, 0.1], [0.9, 0.1, 0.4]] as const) {
      const c = sparkLavender(wash, [0.4, 0.8, 0.9]);
      const h = hueOf(c);
      expect(h >= 70 && h <= 335).toBe(true);
    }
    const all = sparkLavender([0.9, 0.1, 0.1], [0.9, 0.6, 0.1]);
    expect(hueOf(all)).toBeGreaterThan(200);
  });
});
