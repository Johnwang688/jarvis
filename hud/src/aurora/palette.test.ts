// The contrast bound (owner D-8): the fluid never displays more light than the brightest wash.
// `displayColor` is the JS twin of gl.ts's GLSL `palette()`; the last block pins the shader text to it.
import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import { PALETTE } from "./gl";
import { FLUID_SHOW_FS } from "./fluid";
import { FLOW_FS } from "./flow";
import {
  DEFAULT_PALETTE, brightestLuminance, displayColor, luminance, parseColour, readPalette, samePalette,
  type AuroraPalette, type Rgb,
} from "./palette";

const token = (name: string): Rgb => {
  const m = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(css);
  if (!m) throw new Error(`${name} not declared in theme.css`);
  return parseColour(m[1]!)!;
};
const themed: AuroraPalette = {
  base: token("--base"),
  colours: [token("--wash-1"), token("--wash-2"), token("--wash-3")],
};
const palettes: [string, AuroraPalette][] = [
  ["theme.css", themed],
  ["default", DEFAULT_PALETTE],
  ["a bright one", { base: [0.1, 0.1, 0.1], colours: [[1, 1, 1], [0.9, 0.95, 1], [1, 0.9, 0.5]] }],
  ["a dark one", { base: [0, 0, 0], colours: [[0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]] }],
];

describe("parseColour", () => {
  it("reads #rrggbb and rgb(), and nothing else", () => {
    expect(parseColour("#ff8000")).toEqual([1, 128 / 255, 0]);
    expect(parseColour(" rgb(0, 255, 0) ")).toEqual([0, 1, 0]);
    expect(parseColour("rgba(0 255 0 / 0.5)")).toEqual([0, 1, 0]);
    for (const bad of ["", "red", "#fff", "#gggggg", "rgb(300,0,0)", "url(x)", "var(--a)"]) expect(parseColour(bad), bad).toBeNull();
  });
});

describe("readPalette", () => {
  it("reads --base and --wash-1..3 from an element, each falling back to its default when it does not parse", () => {
    const el = document.createElement("div");
    el.style.setProperty("--base", "#000000");
    el.style.setProperty("--wash-2", "not a colour");
    document.body.appendChild(el);
    const p = readPalette(el);
    el.remove();
    // jsdom resolves custom properties on the element itself
    expect(p.base).toEqual([0, 0, 0]);
    expect(p.colours[1]).toEqual(DEFAULT_PALETTE.colours[1]);
    expect(samePalette(p, p)).toBe(true);
    expect(samePalette(p, DEFAULT_PALETTE)).toBe(false);
  });
});

describe("the display mapping is bounded by the brightest wash", () => {
  const grid = [-1, 0, 0.2, 0.5, 0.95, 1, 1.5, 7, NaN, Infinity];
  it.each(palettes)("%s: no weights, in or out of range, and no dither make it brighter than the brightest colour", (_n, p) => {
    const max = brightestLuminance(p);
    let worst = 0;
    for (const a of grid) for (const b of grid) for (const c of grid) for (const d of [0, 0.5, 1]) {
      worst = Math.max(worst, luminance(displayColor(p, [a, b, c], d)));
    }
    expect(worst).toBeLessThanOrEqual(max + 1e-12);
  });

  it("full strength on the brightest wash reaches it exactly, and never more (the dither only darkens)", () => {
    const max = brightestLuminance(themed);
    const i = themed.colours.map(luminance).indexOf(Math.max(...themed.colours.map(luminance)));
    const w: [number, number, number] = [0, 0, 0];
    w[i] = 1;
    // weights are applied wash 3, 2, 1 in turn: a lone full weight on wash i shows wash i alone
    const shown = luminance(displayColor(themed, w, 1));
    expect(shown).toBeCloseTo(max, 6);
    expect(luminance(displayColor(themed, w, 0))).toBeLessThanOrEqual(shown);
  });

  it("the shipped base is darker than every wash, so the bound is the washes", () => {
    expect(luminance(themed.base)).toBeLessThan(Math.min(...themed.colours.map(luminance)));
  });

  it("random mixes (seeded) stay under the bound too", () => {
    let seed = 12345;
    const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
    const max = brightestLuminance(themed);
    for (let i = 0; i < 5000; i++) {
      expect(luminance(displayColor(themed, [rnd() * 1.2, rnd() * 1.2, rnd() * 1.2], rnd()))).toBeLessThanOrEqual(max + 1e-12);
    }
  });
});

describe("the display shaders are exactly the mapping the test checks (the part jsdom cannot run)", () => {
  // Whole-text equality, whitespace aside: from the dye or weights to the final output colour, nothing
  // may be added, scaled or reordered without this failing (a trailing `*2.0` included).
  const squash = (t: string) => t.replace(/\s+/g, "");
  const HEAD_PALETTE =
    "uniformvec3uBase;uniformvec3uC1;uniformvec3uC2;uniformvec3uC3;" +
    "vec3toSrgb(vec3c){c=clamp(c,0.0,1.0);returnmix(c*12.92,1.055*pow(c,vec3(1.0/2.4))-0.055,step(0.0031308,c));}" +
    "floathash(vec2p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);returnfract(p.x*p.y);}" +
    "vec3palette(vec3w){w=clamp(w,0.0,1.0);vec3c=uBase;c=mix(c,uC3,w.z);c=mix(c,uC2,w.y);c=mix(c,uC1,w.x);" +
    "vec3s=toSrgb(c);returns+(hash(gl_FragCoord.xy)-1.0)/255.0;}";
  it("the palette function is the JS twin's mapping, character for character", () => {
    expect(squash(PALETTE)).toBe(HEAD_PALETTE);
  });
  it("the fluid's display shader is the palette plus exactly its dye-to-weights step and one output", () => {
    expect(squash(FLUID_SHOW_FS)).toBe(
      HEAD_PALETTE +
        "uniformsampler2DuDye;voidmain(){vec3w=max(texture(uDye,vUv).rgb,0.0);w/=max(1.0,w.x+w.y+w.z);o=vec4(palette(w*0.95),1.0);}",
    );
  });
  it("the flow shader ends in exactly one output, straight through palette()", () => {
    const body = squash(FLOW_FS);
    expect(body.startsWith(HEAD_PALETTE)).toBe(true);
    expect(body.endsWith("o=vec4(palette(clamp(vec3(w1,w2,w3)*0.95,0.0,1.0)),1.0);}")).toBe(true);
    expect(body.match(/\bo=/g)?.length).toBe(1);
    expect(body.match(/gl_FragColor/g)).toBeNull();
    // the weights it feeds in are products of smoothsteps and layout leans of at most 1.15, kept by the clamp
    expect(body).toContain("w1*=0.55+0.6*vUv.y;w2*=0.55+0.6*vUv.x;w3*=1.15-0.6*vUv.y;");
  });
  it("no style adds light after the mapping", () => {
    for (const src of [FLUID_SHOW_FS, FLOW_FS]) expect(src).not.toMatch(/bloom|highlight|specular|spec\b/i);
  });
});
