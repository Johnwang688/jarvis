import { describe, expect, it } from "vitest";
import { BEAT, BOOT_DEFAULTS, FADE_OUT_S, RINGS, buildDust, dotsAt, layoutFor,
  mulberry32, orbProgress, particleAt, penAt, plateGeometry, shouldPlay } from "./boot";
import { RING_SETS } from "../components/Orb";

describe("boot eligibility", () => {
  for (let mask = 0; mask < 32; mask++) {
    const [webdriver, forced, reducedMotion, seen, cardUp] = [0, 1, 2, 3, 4].map(i => !!(mask & (1 << i)));
    it(`facts ${JSON.stringify({ webdriver, forced, reducedMotion, seen, cardUp })}`, () => {
      expect(shouldPlay({ webdriver, forced, reducedMotion, seen, cardUp }))
        .toBe((!webdriver || forced) && !reducedMotion && !seen && !cardUp);
    });
  }
});

it("uses the HUD orb's default rings", () => expect(RINGS).toEqual(RING_SETS.default));
it("repeats a seed's random sequence and complete dust paths", () => {
  const a = mulberry32(7), b = mulberry32(7), c = mulberry32(8);
  const sequence = (rng: () => number) => Array.from({ length: 100 }, rng);
  expect(sequence(a)).toEqual(sequence(b));
  expect(sequence(mulberry32(7))).not.toEqual(sequence(c));
  const L = layoutFor(1280, 860);
  const first = buildDust(L), second = buildDust(L);
  expect(first).toEqual(second);
  expect(first.parts.length).toBe(700);
  expect(buildDust(L, { ...BOOT_DEFAULTS, seed: 8 })).not.toEqual(first);
  for (const t of [0, 0.8, 1.9, 3, 4.8]) {
    expect(first.parts.map(p => particleAt(L, p, t))).toEqual(second.parts.map(p => particleAt(L, p, t)));
  }
});
it("ends with a full orb, dots and no pens; every grain dissolves before 7 seconds", () => {
  const L = layoutFor(1280, 860), { parts, timeline } = buildDust(L);
  expect(timeline.end + FADE_OUT_S).toBeLessThan(7);
  expect(timeline.end).toBeGreaterThan(timeline.orbEnd);
  const dustEnd = Math.max(...parts.map(p => p.arrive)) + 0.6;
  expect(dustEnd).toBeLessThan(7);
  expect(parts.every(p => particleAt(L, p, dustEnd + 0.001) === null)).toBe(true);
  expect(orbProgress(timeline, timeline.end)).toBe(1);
  expect(penAt(timeline, BOOT_DEFAULTS, timeline.end)).toEqual({ e: 1, vis: 0 });
  expect(dotsAt(timeline, timeline.end)).toEqual({ q: 1, pop: 1 });
  expect(dotsAt(timeline, BEAT.plate0 + BOOT_DEFAULTS.pen).q).toBe(0);
});
it("draws opposite corners counter-clockwise, sharp letters and six square dots", () => {
  const L = layoutFor(1280, 860), g = plateGeometry(L);
  expect(g.boxA).toBe(`M${L.px + L.pw} ${L.py} L${L.px} ${L.py} L${L.px} ${L.py + L.ph}`);
  expect(g.boxB).toBe(`M${L.px} ${L.py + L.ph} L${L.px + L.pw} ${L.py + L.ph} L${L.px + L.pw} ${L.py}`);
  expect(g.letters).toHaveLength(6); expect(g.dots).toHaveLength(6);
  expect(g.letters.flat().every(d => !/[CQAHV]/.test(d))).toBe(true);
});

it("matches the approved prototype's seed-7 reference positions and beats", () => {
  // Captured from prototype.html's own pure functions at 1280 × 800.
  const L = layoutFor(1280, 800), { parts, timeline } = buildDust(L);
  expect(timeline.orbStart).toBeCloseTo(3.067684355849349, 12);
  expect(timeline.orbEnd).toBeCloseTo(3.960070811413176, 12);
  expect(timeline.end).toBeCloseTo(4.560070811413176, 12);
  const expected = [
    [150.9719458913353, 743.681220967963, 0],
    [246.67896464352725, 606.7245684557171, 0.7241173066198826],
    [983.4986623771109, 314.84116727212296, 0.7241173066198826],
    [662.5698440080334, 342.8089978420478, 0.7241173066198826],
    [637.5427086664815, 336.51535487660027, 0.07614675341063071],
  ];
  expected.forEach((xyz, t) => xyz.forEach((v, axis) =>
    expect(particleAt(L, parts[0], t)![axis]).toBeCloseTo(v, 9)));
});
