// The whole-palette guard (lib/appearance.ts checkPalette): every colour the aurora can show, both mixing paths,
// every weight and dither, clear of the amber/red band and of amber and red. The check is a proof by cells
// (see the header in appearance.ts), so these tests check the proof's parts and then the whole against an
// independent brute-force reference written here from aurora/palette.ts `displayColor`, not from the check.
import { describe, expect, it } from "vitest";
import {
  AMBER, DARK_L, DEFAULT_ACCENT, DEFAULT_BASE, DEFAULT_WASHES, K_BAND, MEANING_DELTA_E, PALETTE_DITHER, RED,
  WARM_BAND, WASH_CHROMA_FLOOR, boxMeetsBand, cellBox, checkAccent, checkBase, checkPalette, checkWash,
  labImpersonates, paletteInternals, paletteVerdict, resolveColours,
} from "./appearance";
import { hexRgb, lchHex, linearToLab, rgbToLab, type Lab } from "./oklch";
import { displayColor, parseColour, type AuroraPalette, type Rgb } from "../aurora/palette";

const W = [...DEFAULT_WASHES];
const OLIVE = "#858260", MAGENTA = "#e70075";
/** Round 2's miss, verbatim: at weights [.03, 0, 0] its display is hue 5.60, chroma .0533. */
const REG2 = { washes: ["#fa0397", "#3468c4", "#6450c4"], base: "#021000" };

/** A fixed-seed PRNG so every run sees the same palettes. */
function rng(seed: number) {
  return () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
}

// ---- the independent reference -------------------------------------------------------------------------------

const srgbLin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toPalette = (washes: string[], base: string): AuroraPalette => ({
  base: parseColour(base)!, colours: washes.map((w) => parseColour(w)!) as unknown as AuroraPalette["colours"],
});
const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
/** The screen clamps what the shader writes (displayColor itself leaves a dithered 0 slightly negative). */
const labOfSrgb = (c: Rgb): Lab => linearToLab(srgbLin(clamp01(c[0])), srgbLin(clamp01(c[1])), srgbLin(clamp01(c[2])));
/** The shader path: displayColor, the GLSL twin. `d` is the check's dither (0 none .. 1 a full step darker). */
const shaderLab = (p: AuroraPalette, w: number[], d: number) => labOfSrgb(displayColor(p, [w[0]!, w[1]!, w[2]!], 1 - d));
/** The CSS path, from theme.css #aurora: wash 3, 2, 1 alpha-over the base in sRGB space, then the dither. */
function cssLab(p: AuroraPalette, w: number[], d: number): Lab {
  const c = [0, 1, 2].map((ch) => {
    let v = p.base[ch]!;
    v = v * (1 - w[2]!) + p.colours[2][ch]! * w[2]!;
    v = v * (1 - w[1]!) + p.colours[1][ch]! * w[1]!;
    v = v * (1 - w[0]!) + p.colours[0][ch]! * w[0]!;
    return v - d / 255;
  });
  return labOfSrgb(c as unknown as Rgb);
}
const AMBER_LAB = rgbToLab(hexRgb(AMBER)), RED_LAB = rgbToLab(hexRgb(RED));
/** The plan's rule, written again: >= 0 means unsafe, and the bigger the deeper inside. */
function ruleScore(lab: Lab): number {
  const C = Math.hypot(lab.a, lab.b);
  const h = ((Math.atan2(lab.b, lab.a) * 180) / Math.PI + 360) % 360;
  const mid = (WARM_BAND.from + WARM_BAND.to) / 2, half = (WARM_BAND.to - WARM_BAND.from) / 2;
  const off = Math.abs(((h - mid + 540) % 360) - 180);
  const band = Math.min((C - WASH_CHROMA_FLOOR) / WASH_CHROMA_FLOOR, (half - off) / half);
  const near = Math.max(...[AMBER_LAB, RED_LAB].map((m) => (MEANING_DELTA_E - Math.hypot(lab.L - m.L, lab.a - m.a, lab.b - m.b)) / MEANING_DELTA_E));
  return Math.max(band, near);
}

/**
 * Brute force over a palette's displayed colours: a grid of the weight cube (0.04 by default), all 12 cube edges
 * at 0.002, each at dither 0 and 1, both paths; then a hill climb (steps down to 1e-5, over weights and dither)
 * from the 12 points nearest the rule. Returns the worst point found.
 */
function reference(washes: string[], base: string, grid = 0.04) {
  const p = toPalette(washes, base);
  // Only the 12 worst points are kept (a 0.01 grid is millions of points).
  const pts: { s: number; x: number[]; path: typeof shaderLab }[] = [];
  const consider = (s: number, x: number[], path: typeof shaderLab) => {
    if (pts.length === 12 && s <= pts[11]!.s) return;
    pts.push({ s, x, path });
    pts.sort((a, b) => b.s - a.s);
    if (pts.length > 12) pts.pop();
  };
  for (const path of [shaderLab, cssLab]) {
    const n = Math.round(1 / grid);
    for (let i = 0; i <= n; i++) for (let j = 0; j <= n; j++) for (let k = 0; k <= n; k++) for (const d of [0, 1]) {
      const x = [i / n, j / n, k / n, d];
      consider(ruleScore(path(p, x, d)), x, path);
    }
    for (let e = 0; e < 12; e++) {
      const ax = e % 3, fixed = [0, 1, 2].filter((a) => a !== ax), bits = Math.floor(e / 3);
      for (let t = 0; t <= 500; t++) for (const d of [0, 1]) {
        const x = [0, 0, 0, d];
        x[ax] = t / 500;
        x[fixed[0]!] = bits & 1;
        x[fixed[1]!] = (bits >> 1) & 1;
        consider(ruleScore(path(p, x, d)), x, path);
      }
    }
  }
  let best = pts[0]!;
  for (const start of pts) {
    const x = start.x.slice();
    let s = start.s;
    for (let step = 0.02; step > 1e-5 && s < 0; step /= 2) {
      for (let improved = true; improved && s < 0;) {
        improved = false;
        for (let ax = 0; ax < 4; ax++) for (const dir of [-1, 1]) {
          const y = x.slice();
          y[ax] = clamp01(y[ax]! + dir * step);
          const t = ruleScore(start.path(p, y, y[3]!));
          if (t > s) { s = t; x.splice(0, 4, ...y); improved = true; }
        }
      }
    }
    if (s > best.s) best = { s, x, path: start.path };
    if (s >= 0) break;
  }
  return { unsafe: best.s >= 0, score: best.s, at: best.x };
}

/** A palette the settings can actually hold: each wash through checkWash, the base through checkBase. */
function settle(washes: string[], base: string): { washes: string[]; base: string } | null {
  const ws = washes.map(checkWash), b = checkBase(base);
  if (!ws.every((x) => x.ok) || !b.ok) return null;
  return { washes: ws.map((x) => (x as { value: string }).value), base: b.value };
}

// ---- the regressions and ordinary palettes ---------------------------------------------------------------------

describe("whole-palette guard: the known misses, and ordinary palettes", () => {
  it("refuses round 1's olive + magenta: each passes alone, their blend is red", () => {
    expect(checkWash(OLIVE).ok).toBe(true);
    expect(checkWash(MAGENTA).ok).toBe(true);
    const lab = labOfSrgb(displayColor(toPalette([OLIVE, MAGENTA, W[2]!], DEFAULT_BASE), [0.45, 0.5, 0], 1));
    expect(labImpersonates(lab)).toBe(true);
    const r = checkPalette([OLIVE, MAGENTA, W[2]!], DEFAULT_BASE);
    expect(r?.reason).toMatch(/Together with your other aurora colours this would mix into amber or red/);
  });

  it("refuses round 2's miss: washes [#fa0397, #3468c4, #6450c4] over #021000, in the band at weights [.03, 0, 0]", () => {
    expect(settle(REG2.washes, REG2.base)).toEqual(REG2);
    const lab = labOfSrgb(displayColor(toPalette(REG2.washes, REG2.base), [0.03, 0, 0], 1));
    const C = Math.hypot(lab.a, lab.b), h = (Math.atan2(lab.b, lab.a) * 180) / Math.PI;
    expect(h).toBeCloseTo(5.6, 1);
    expect(C).toBeCloseTo(0.0533, 3);
    expect(labImpersonates(lab)).toBe(true);
    // Refused because the proof found the real colour, not because it ran out of cells.
    const v = paletteVerdict(REG2.washes, REG2.base);
    expect(v).toMatchObject({ unsafe: true, why: "hit" });
    expect(checkPalette(REG2.washes, REG2.base)).not.toBeNull();
  });

  it("is not absurdly strict: the defaults and a dozen cool palettes pass", () => {
    const cool: Record<string, [string[], string]> = {
      defaults: [W, DEFAULT_BASE],
      glacier: [["#2a8fa0", "#3a7bb0", "#5a6fc0"], DEFAULT_BASE],
      tealBlueViolet: [["#2fa88a", "#4a6fd0", "#7a4fc8"], DEFAULT_BASE],
      deepTeal: [["#1d7f86", "#2a5fb0", "#5848b8"], DEFAULT_BASE],
      forest: [["#1f6b3a", "#2e8b57", "#698b69"], "#06100a"],
      emerald: [["#2a9a6a", "#1f8f7a", "#3a70b0"], DEFAULT_BASE],
      sage: [["#5f8f78", "#4f8a8a", "#6a7fa8"], "#0a110e"],
      navy: [["#1a2a6a", "#22306e", "#2a3a8a"], "#05070f"],
      midnight: [["#2848a8", "#3468c4", "#2a8fa0"], "#000000"],
      magentaViolet: [["#a040a0", "#8a3ab0", "#6450c4"], DEFAULT_BASE],
      orchid: [["#a040a0", "#8a3ab0", "#da70d6"], "#0b0a14"],
      purples: [["#5a40b0", "#7a4fc8", "#3468c4"], DEFAULT_BASE],
      indigoBase: [W, "#0b1020"],
    };
    for (const [name, [washes, base]] of Object.entries(cool)) {
      const s = settle(washes, base);
      expect(s, name).not.toBeNull();
      expect(checkPalette(s!.washes, s!.base), name).toBeNull();
    }
  });

  it("falls back to the default washes, then the default base, when a stored palette is unsafe", () => {
    const c = resolveColours({ washes: [OLIVE, MAGENTA, W[2]!] });
    expect(c.washes).toEqual(DEFAULT_WASHES);
    const cool = ["#2fa88a", "#4a6fd0", "#7a4fc8"];
    expect(resolveColours({ washes: cool }).washes).toEqual(cool.map((w) => (checkWash(w) as { value: string }).value));
    const r2 = resolveColours({ washes: REG2.washes, base: REG2.base });
    expect(checkPalette(r2.washes, r2.base)).toBeNull();
    expect(r2.washes).toEqual(DEFAULT_WASHES);
  });

  it("keeps a stored accent that is only unreadable over a refused wash set (it is judged against the shown washes)", () => {
    const washes = [MAGENTA, OLIVE, "#ffffff"];
    const clamped = washes.map((w) => (checkWash(w) as { value: string }).value);
    // The precondition: over the refused set the accent fails, over the defaults it passes.
    expect(checkPalette(clamped, DEFAULT_BASE)).not.toBeNull();
    expect(checkAccent("#ababab", clamped).ok).toBe(false);
    expect(checkAccent("#ababab", DEFAULT_WASHES).ok).toBe(true);
    const c = resolveColours({ accent: "#ababab", washes });
    expect(c).toEqual({ accent: "#ababab", washes: [...DEFAULT_WASHES], base: DEFAULT_BASE });
    // An accent that is bad on its own still falls back.
    expect(resolveColours({ accent: AMBER, washes }).accent).toBe(DEFAULT_ACCENT);
  });

  it("refuses a malformed palette rather than judging it", () => {
    expect(checkPalette(["#2a8fa0", "#3468c4"], DEFAULT_BASE)).not.toBeNull();
    expect(checkPalette(["#2a8fa0", "#3468c4", "url(x)"], DEFAULT_BASE)).not.toBeNull();
    expect(checkPalette(W, "red")).not.toBeNull();
  });
});

// ---- the proof's parts ---------------------------------------------------------------------------------------

describe("whole-palette guard: each step of the proof is sound", () => {
  it("K_BAND: no in-band colour of the sRGB cube has C/L above it (far faces on a 1/1000 grid), and L > 0", () => {
    let k = 0, minL = Infinity;
    const N = 1000;
    for (let f = 0; f < 3; f++) for (let i = 0; i <= N; i++) for (let j = 0; j <= N; j++) {
      const c = [0, 0, 0];
      c[f] = 1;
      c[(f + 1) % 3] = i / N;
      c[(f + 2) % 3] = j / N;
      const lab = linearToLab(c[0]!, c[1]!, c[2]!);
      minL = Math.min(minL, lab.L);
      const h = ((Math.atan2(lab.b, lab.a) * 180) / Math.PI + 360) % 360;
      if (h >= WARM_BAND.from - 1 && h <= WARM_BAND.to + 1) k = Math.max(k, Math.hypot(lab.a, lab.b) / lab.L);
    }
    expect(k).toBeGreaterThan(0.4);
    expect(k * 1.02).toBeLessThan(K_BAND);
    expect(minL).toBeGreaterThan(0.4);
    expect(DARK_L).toBeCloseTo(WASH_CHROMA_FLOOR / K_BAND, 12);
  });

  it("boxMeetsBand never says no when a point of the box is in the band (random boxes, dense grids)", () => {
    const r = rng(99);
    const inBand = (a: number, b: number) => {
      const C = Math.hypot(a, b), h = ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360;
      return C >= WASH_CHROMA_FLOOR && h >= WARM_BAND.from && h <= WARM_BAND.to;
    };
    let yes = 0, no = 0;
    for (let n = 0; n < 3000; n++) {
      // Boxes around the band's edges and the chroma floor, small and large.
      const ang = ((r() < 0.5 ? WARM_BAND.from : WARM_BAND.to) + (r() - 0.5) * 30) * (Math.PI / 180);
      const rad = WASH_CHROMA_FLOOR * (0.3 + r() * 1.6);
      const ca = rad * Math.cos(ang), cb = rad * Math.sin(ang), wa = r() * 0.06, wb = r() * 0.06;
      const a0 = ca - wa * r(), a1 = ca + wa * r(), b0 = cb - wb * r(), b1 = cb + wb * r();
      let hit = false;
      for (let i = 0; i <= 60 && !hit; i++) for (let j = 0; j <= 60 && !hit; j++) hit = inBand(a0 + ((a1 - a0) * i) / 60, b0 + ((b1 - b0) * j) / 60);
      const said = boxMeetsBand(a0, a1, b0, b1);
      if (hit) expect(said, `${[a0, a1, b0, b1]}`).toBe(true);
      if (said) yes++; else no++;
    }
    expect(yes).toBeGreaterThan(300);
    expect(no).toBeGreaterThan(300);
  });

  it("cellBox holds every displayed colour of its cell (random palettes and cells, both paths, every dither)", () => {
    const r = rng(4242);
    let checked = 0;
    for (let n = 0; n < 250; n++) {
      const dark = r() < 0.3;
      const washes = [0, 1, 2].map(() => lchHex({ L: (dark ? 0.05 : 0.3) + r() * 0.45, C: r() * 0.25, h: r() * 360 }));
      const base = r() < 0.2 ? "#000000" : lchHex({ L: r() * 0.25, C: r() * 0.06, h: r() * 360 });
      const pal = toPalette(washes, base);
      for (const space of ["linear", "gamma"] as const) {
        const path = paletteInternals.preparePath(washes, base, space);
        const ref = space === "linear" ? shaderLab : cssLab;
        for (let c = 0; c < 8; c++) {
          // A dyadic cell anywhere, often against the cube's faces (where the base and the washes sit).
          const cell: number[] = [];
          for (let ax = 0; ax < 3; ax++) {
            const wdt = 2 ** -Math.floor(r() * 14);
            const lo = r() < 0.3 ? 0 : r() < 0.15 ? 1 - wdt : Math.floor(r() / wdt) * wdt;
            cell.push(lo, Math.min(1, lo + wdt));
          }
          const dw = 2 ** -Math.floor(r() * 6), dlo = Math.floor(r() / dw) * dw;
          cell.push(dlo, Math.min(1, dlo + dw));
          const { box } = cellBox(path, cell as Parameters<typeof cellBox>[1]);
          for (let s = 0; s < 40; s++) {
            const x = [0, 1, 2, 3].map((ax) => {
              const lo = cell[2 * ax]!, hi = cell[2 * ax + 1]!;
              return s < 16 ? ((s >> ax) & 1 ? hi : lo) : lo + (hi - lo) * r();
            });
            const lab = ref(pal, x, x[3]!);
            const where = `${space} ${washes} on ${base} cell ${cell} at ${x}`;
            expect(lab.L, where).toBeGreaterThanOrEqual(box.L0);
            expect(lab.L, where).toBeLessThanOrEqual(box.L1);
            expect(lab.a, where).toBeGreaterThanOrEqual(box.a0);
            expect(lab.a, where).toBeLessThanOrEqual(box.a1);
            expect(lab.b, where).toBeGreaterThanOrEqual(box.b0);
            expect(lab.b, where).toBeLessThanOrEqual(box.b1);
            checked++;
          }
        }
      }
    }
    expect(checked).toBe(250 * 2 * 8 * 40);
  }, 60000);

  it("the dither is one 8-bit step, as in aurora/palette.ts", () => {
    expect(PALETTE_DITHER).toBe(1 / 255);
  });
});

// ---- the whole check against the reference ------------------------------------------------------------------

/** Washes and bases aimed at the band: hot pinks and oranges at its edges, limes, tinted and near-black bases. */
function adversarialPalettes(count: number, seed: number) {
  const r = rng(seed);
  const pick = <T,>(xs: T[]) => xs[Math.floor(r() * xs.length)]!;
  const edgeHue = () => pick([340 + r() * 25, 105 + r() * 25, r() * 8, 95 + r() * 12, r() * 360]);
  const wash = () => lchHex({ L: 0.25 + r() * 0.5, C: 0.03 + r() * 0.22, h: r() < 0.7 ? edgeHue() : r() * 360 });
  const base = () => pick([
    () => lchHex({ L: 0.08 + r() * 0.17, C: 0.02 + r() * 0.035, h: r() * 360 }), // tinted dark, any hue
    () => lchHex({ L: 0.1 + r() * 0.15, C: 0.03 + r() * 0.025, h: 110 + r() * 60 }), // dark greens like #021000
    () => lchHex({ L: r() * 0.12, C: r() * 0.02, h: r() * 360 }), // near black
    () => "#000000",
    () => DEFAULT_BASE,
  ])();
  const out: { washes: string[]; base: string }[] = [];
  for (let tries = 0; out.length < count && tries < count * 50; tries++) {
    const s = settle([wash(), wash(), r() < 0.5 ? wash() : pick(W)], base());
    if (s) out.push(s);
  }
  return out;
}

/** Plain random palettes the settings accept. */
function randomPalettes(count: number, seed: number) {
  const r = rng(seed);
  const out: { washes: string[]; base: string }[] = [];
  while (out.length < count) {
    const draw = () => lchHex({ L: 0.3 + r() * 0.4, C: 0.03 + r() * 0.2, h: r() * 360 });
    const s = settle([draw(), draw(), draw()], lchHex({ L: r() * 0.25, C: r() * 0.05, h: r() * 360 }));
    if (s) out.push(s);
  }
  return out;
}

/** Runs the check and the reference over palettes; returns the counts and every miss. */
function compare(palettes: { washes: string[]; base: string }[], grid?: number) {
  let refUnsafe = 0, refSafe = 0, extra = 0, budget = 0, maxMs = 0;
  const misses: string[] = [];
  for (const { washes, base } of palettes) {
    const ref = reference(washes, base, grid);
    const t = performance.now();
    const v = paletteVerdict(washes, base);
    maxMs = Math.max(maxMs, performance.now() - t);
    if (v.why === "budget") budget++;
    if (ref.unsafe) {
      refUnsafe++;
      if (!v.unsafe) misses.push(`${washes} on ${base}: reference hit at ${ref.at.map((x) => x.toFixed(4))}`);
    } else {
      refSafe++;
      if (v.unsafe) extra++;
    }
  }
  return { refUnsafe, refSafe, extra, budget, maxMs, misses };
}

describe("whole-palette guard: no miss against an independent brute force", () => {
  it("PROOF: over 320 palettes (218 aimed at the band) the check never accepts what the reference refuses", () => {
    const palettes = [REG2, { washes: [OLIVE, MAGENTA, W[2]!], base: DEFAULT_BASE }, ...randomPalettes(100, 1234), ...adversarialPalettes(218, 77)];
    const c = compare(palettes);
    console.log(`palette proof: ${palettes.length} palettes, reference unsafe ${c.refUnsafe}, safe ${c.refSafe}; misses ${c.misses.length}; ` +
      `refused though the reference found nothing ${c.extra} (${c.budget} on the cell budget); slowest check ${c.maxMs.toFixed(1)} ms`);
    expect(c.misses).toEqual([]);
    // The test means something only if both kinds are well represented.
    expect(c.refUnsafe).toBeGreaterThan(60);
    expect(c.refSafe).toBeGreaterThan(60);
    // And the proof is not refusing wholesale: most palettes the reference passes, it passes too.
    expect(c.extra).toBeLessThan(c.refSafe * 0.15);
  }, 240000);

  // The long form (PALETTE_PROOF_HEAVY=1): a 0.01 grid of the whole cube plus everything above, more palettes.
  it.runIf(!!process.env.PALETTE_PROOF_HEAVY)("PROOF (heavy): a 0.01 grid over 200 more palettes finds no miss", async () => {
    const palettes = [...randomPalettes(60, 555), ...adversarialPalettes(140, 909)];
    // One palette at a time, yielding between them, so vitest's worker can still talk to its runner.
    const parts = [];
    for (const p of palettes) {
      parts.push(compare([p], 0.01));
      await new Promise((r) => setTimeout(r, 0));
    }
    const c = {
      refUnsafe: parts.reduce((n, x) => n + x.refUnsafe, 0), refSafe: parts.reduce((n, x) => n + x.refSafe, 0),
      extra: parts.reduce((n, x) => n + x.extra, 0), budget: parts.reduce((n, x) => n + x.budget, 0),
      maxMs: Math.max(...parts.map((x) => x.maxMs)), misses: parts.flatMap((x) => x.misses),
    };
    console.log(`palette proof (heavy, 0.01 grid): ${palettes.length} palettes, reference unsafe ${c.refUnsafe}, safe ${c.refSafe}; ` +
      `misses ${c.misses.length}; extra refusals ${c.extra} (${c.budget} on the budget); slowest check ${c.maxMs.toFixed(1)} ms`);
    expect(c.misses).toEqual([]);
  }, 3600000);

  it("PROOF: walking one wash across every hue next to the repros, whatever the reference refuses, the check refuses", () => {
    let refused = 0;
    for (const [w1, w3, base] of [[OLIVE, W[2]!, DEFAULT_BASE], ["#3468c4", "#6450c4", "#021000"]] as const) {
      for (let h = 0; h < 360; h += 6) {
        const s = settle([w1, lchHex({ L: 0.55, C: 0.17, h }), w3], base);
        if (!s || !reference(s.washes, s.base).unsafe) continue;
        refused++;
        expect(checkPalette(s.washes, s.base), `h=${h} on ${base}`).not.toBeNull();
      }
    }
    expect(refused).toBeGreaterThan(10);
  }, 120000);

  it("stays quick: ordinary palettes are proved well inside an input event (generous bound; the timing is logged)", () => {
    // A generous bound so a loaded machine cannot fail it; the typical numbers are logged for the record.
    const palettes = [[W, DEFAULT_BASE], [["#2fa88a", "#4a6fd0", "#7a4fc8"], DEFAULT_BASE], [["#1f6b3a", "#2e8b57", "#698b69"], "#06100a"],
      [W, "#000000"], [["#a040a0", "#8a3ab0", "#c25abf"], DEFAULT_BASE]] as const;
    for (let i = 0; i < 3; i++) for (const [w, b] of palettes) paletteVerdict([...w], b); // warm up
    const times: number[] = [];
    for (const [w, b] of palettes) {
      const t = performance.now();
      for (let i = 0; i < 5; i++) paletteVerdict([...w], b);
      times.push((performance.now() - t) / 5);
    }
    console.log(`paletteVerdict: ${times.map((t) => t.toFixed(2)).join(", ")} ms per palette (uncached)`);
    for (const t of times) expect(t).toBeLessThan(60);
    // Machine-independent: the cells each needs. A black base stays cheap only because of DARK_L.
    for (const [w, b] of palettes) expect(paletteVerdict([...w], b).cells, `${w} on ${b}`).toBeLessThan(3000);
  });
});
