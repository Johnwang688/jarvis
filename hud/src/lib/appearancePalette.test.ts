import { describe, expect, it } from "vitest";
import {
  DEFAULT_BASE, DEFAULT_WASHES, checkBase, checkPalette, checkWash, labImpersonates, resolveColours,
} from "./appearance";
import { hexRgb, linearToLab, rgbHex, hexLch, lchHex } from "./oklch";
import { displayColor, parseColour, type AuroraPalette, type Rgb } from "../aurora/palette";

const W = [...DEFAULT_WASHES];
const OLIVE = "#858260", MAGENTA = "#e70075";

/** A fixed-seed PRNG so the proof test is repeatable. */
function rng(seed: number) {
  return () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
}
const toPalette = (washes: string[], base: string): AuroraPalette => ({
  base: parseColour(base)!, colours: washes.map((w) => parseColour(w)!) as unknown as AuroraPalette["colours"],
});
const srgbLin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const labOfRgb = (c: Rgb) => linearToLab(srgbLin(c[0]), srgbLin(c[1]), srgbLin(c[2]));

describe("whole-palette guard (the blends, not only each wash)", () => {
  it("refuses the olive + magenta repro: each passes alone, their blend is red", () => {
    expect(checkWash(OLIVE).ok).toBe(true);
    expect(checkWash(MAGENTA).ok).toBe(true);
    // The reported blend: weights [.45, .5, 0] over the default base is about #9b5a5b, hue 19, chroma .086.
    const d = displayColor(toPalette([OLIVE, MAGENTA, W[2]!], DEFAULT_BASE), [0.45, 0.5, 0], 1);
    const lab = labOfRgb(d);
    expect(labImpersonates(lab)).toBe(true);
    const r = checkPalette([OLIVE, MAGENTA, W[2]!], DEFAULT_BASE);
    expect(r?.reason).toMatch(/Together with your other aurora colours this would mix into amber or red/);
  });

  it("passes the defaults and ordinary cool palettes", () => {
    const nice: Record<string, string[]> = {
      defaults: W,
      glacier: ["#2a8fa0", "#3a7bb0", "#5a6fc0"],
      northern: ["#2fa88a", "#4a6fd0", "#7a4fc8"],
      greens: ["#2a9a6a", "#2a8fa0", "#4a70c0"],
      purples: ["#5a40b0", "#7a4fc8", "#3468c4"],
      deepBlue: ["#2848a8", "#3468c4", "#2a8fa0"],
    };
    for (const [name, washes] of Object.entries(nice)) {
      const clamped = washes.map((w) => (checkWash(w) as { value: string }).value);
      expect(checkPalette(clamped, DEFAULT_BASE), name).toBeNull();
      for (const w of washes) expect(checkWash(w).ok, `${name} ${w}`).toBe(true);
    }
    expect(checkPalette(W, "#0b1020")).toBeNull();
  });

  it("falls back to the default washes when a stored palette is unsafe", () => {
    const c = resolveColours({ washes: [OLIVE, MAGENTA, W[2]!] });
    expect(c.washes).toEqual(DEFAULT_WASHES);
    const cool = ["#2fa88a", "#4a6fd0", "#7a4fc8"];
    expect(resolveColours({ washes: cool }).washes).toEqual(cool.map((w) => (checkWash(w) as { value: string }).value));
  });

  it("an unsafe base over safe washes falls back to the default base", () => {
    // A dark base is itself safe, but pair it with a pair of washes whose blend with it is red-ish.
    const c = resolveColours({ washes: [OLIVE, MAGENTA, W[2]!], base: "#050505" });
    expect(checkPalette(c.washes, c.base)).toBeNull();
  });

  it("runs fast enough for every input event", () => {
    checkPalette(W, DEFAULT_BASE); // warm up
    const t = performance.now();
    const n = 20;
    for (let i = 0; i < n; i++) checkPalette(W, DEFAULT_BASE);
    const per = (performance.now() - t) / n;
    console.log(`checkPalette: ${per.toFixed(2)} ms per call`);
    expect(per).toBeLessThan(5);
  });

  it("matches the shader's JS twin (displayColor) in linear mode", () => {
    const p = toPalette(["#2a8fa0", "#e70075", "#858260"], "#0a1116");
    const r = rng(7);
    for (let i = 0; i < 200; i++) {
      const w: [number, number, number] = [r(), r(), r()];
      const d = displayColor(p, w, 1); // dither 0
      // The check's own arithmetic, repeated inline.
      const lin = [0, 1, 2].map((ch) => {
        const f = (c: Rgb) => srgbLin(c[ch]!);
        let c = f(p.base);
        c += (f(p.colours[2]) - c) * w[2];
        c += (f(p.colours[1]) - c) * w[1];
        c += (f(p.colours[0]) - c) * w[0];
        return c;
      });
      const a = linearToLab(lin[0]!, lin[1]!, lin[2]!), b = labOfRgb(d);
      expect(Math.hypot(a.L - b.L, a.a - b.a, a.b - b.b)).toBeLessThan(2e-3);
    }
  });

  it("PROOF: a 0.01 brute force over random accepted palettes finds nothing the 0.05 check missed", () => {
    const r = rng(1234);
    // Random hex draws, dark enough to pass checkWash, biased toward saturated colours near the band's edges.
    const draw = () => {
      const lch = { L: 0.35 + r() * 0.3, C: 0.04 + r() * 0.16, h: r() * 360 };
      return lchHex(lch);
    };
    let accepted = 0, tried = 0, refused = 0;
    while (accepted < 40 && tried < 4000) {
      tried++;
      const ws = [draw(), draw(), draw()].map((w) => checkWash(w));
      if (!ws.every((x) => x.ok)) continue;
      const washes = ws.map((x) => (x as { value: string }).value);
      const base = (checkBase(lchHex({ L: 0.15 + r() * 0.15, C: r() * 0.05, h: r() * 360 })) as { value: string }).value;
      if (checkPalette(washes, base)) { refused++; continue; }
      accepted++;
      // Fine, unmargined rule at step 0.01 for both mixing paths: must find nothing.
      expect(checkPalette(washes, base, 0.01, 0), `${washes} on ${base}`).toBeNull();
    }
    expect(accepted).toBeGreaterThanOrEqual(30);
    expect(refused).toBeGreaterThan(0);
  }, 60000);

  it("PROOF: the coarse check is no more lenient than the exact rule on the repro's neighbourhood", () => {
    // Walk wash 2 across hues next to the repro: whatever 0.01 (unmargined) refuses, 0.05 must refuse too.
    for (let h = 0; h < 360; h += 9) {
      const w2 = lchHex({ L: 0.55, C: 0.17, h });
      const washes = [OLIVE, w2, W[2]!];
      if (!checkWash(w2).ok) continue;
      const fine = checkPalette(washes, DEFAULT_BASE, 0.01, 0);
      if (fine) expect(checkPalette(washes, DEFAULT_BASE), `h=${h}`).not.toBeNull();
    }
  }, 60000);

  it("hex round-trips used here stay sane", () => {
    expect(rgbHex(hexRgb("#123456"))).toBe("#123456");
    expect(hexLch("#2a8fa0").h).toBeGreaterThan(190);
  });
});
