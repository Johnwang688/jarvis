// Acceptance tests for RGB mode in the aurora controller (owner 2026-11-13). Written by the orchestrator
// before the build; the implementer may not edit this file.
//
// Contract: AuroraInputs gains `cycle: number | null` (seconds per full RGB cycle; null = off). While the
// aurora is running and `cycle` is set, each simulation step advances a phase by dt / cycle and sends the
// renderer `rgbPalette(phase)`: the RGB base and `rgbWashes(phase)` from lib/appearance. No CSS token is
// written per frame. Still (or reduced motion) draws one frame with the palette it was given, no cycling.
// Turning `cycle` off restores `inputs.palette`.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAuroraController, type AuroraController, type AuroraInputs } from "./controller";
import { DEFAULT_PALETTE, parseColour, toLinear } from "./palette";
import { fakeGL, type FakeGL } from "./fakegl";
import { rgbPalette } from "./rgb";
import { RGB_BASE, rgbWashes } from "../lib/appearance";

let fake: FakeGL;
let raf: Map<number, FrameRequestCallback>;
let nextId: number;
let host: HTMLElement;
let ctl: AuroraController | null;
let clockMs: number;
/** Every uC1 vec3 uploaded (the first wash, linear light as the shader takes it), oldest first. */
let c1: number[][];

const inputs = (o: Partial<AuroraInputs> = {}): AuroraInputs => ({
  style: "fluid", palette: DEFAULT_PALETTE, still: false, visible: true, stir: true, activity: true, drift: 1, cycle: null, ...o,
});
const tick = (ms = 17) => {
  const cbs = [...raf.values()];
  raf.clear();
  cbs.forEach((cb) => cb(clockMs));
  clockMs += ms;
};
const run = (n: number, ms = 17) => { for (let i = 0; i < n; i++) tick(ms); };
const near = (a: readonly number[], b: readonly number[]) => a.every((v, i) => Math.abs(v - b[i]!) < 1e-6);
/** An sRGB 0..1 colour as the linear-light vec3 the palette uniforms hold. */
const lin = (c: readonly number[]) => c.map(toLinear);

beforeEach(() => {
  fake = fakeGL();
  c1 = [];
  const orig = fake.gl.uniform3fv.bind(fake.gl);
  (fake.gl as { uniform3fv: unknown }).uniform3fv = (loc: unknown, v: ArrayLike<number>) => {
    if ((loc as { name?: string } | null)?.name === "uC1") c1.push(Array.from(v));
    return orig(loc as never, v as never);
  };
  raf = new Map();
  nextId = 1;
  clockMs = 1000;
  ctl = null;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { raf.set(nextId, cb); return nextId++; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => void raf.delete(id));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((kind: string) => (kind === "webgl2" ? fake.gl : null)) as never);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue(
    { left: 0, top: 0, width: 1000, height: 600, right: 1000, bottom: 600, x: 0, y: 0, toJSON() {} } as DOMRect);
  host = document.createElement("div");
  host.id = "aurora";
  host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 600, right: 1000, bottom: 600, x: 0, y: 0, toJSON() {} }) as DOMRect;
  document.body.appendChild(host);
});
afterEach(() => {
  ctl?.destroy();
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("rgbPalette", () => {
  it("is the RGB base and rgbWashes(phase), as 0..1 channels, and wraps", () => {
    const p = rgbPalette(0.3);
    expect(near(p.base, parseColour(RGB_BASE)!)).toBe(true);
    rgbWashes(0.3).forEach((w, i) => expect(near(p.colours[i]!, parseColour(w)!)).toBe(true));
    expect(rgbPalette(1.3)).toEqual(rgbPalette(0.3));
  });
});

describe("RGB cycling in the controller", () => {
  it("off (cycle null): the first wash stays the given palette's while running", () => {
    ctl = createAuroraController(host, inputs());
    run(40);
    const seen = c1.slice(-20);
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((v) => near(v, lin(DEFAULT_PALETTE.colours[0])))).toBe(true);
  });

  it("on: the first wash changes from frame to frame and follows rgbPalette", () => {
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(60);
    const distinct = new Set(c1.slice(-40).map((v) => v.map((x) => x.toFixed(4)).join(",")));
    expect(distinct.size).toBeGreaterThan(3);
    // Every uploaded first wash is one of the cycle's own colours (no stray palette).
    const table = Array.from({ length: 3600 }, (_, k) => lin(rgbPalette(k / 3600).colours[0]));
    for (const v of c1.slice(-10)) expect(table.some((t) => Math.abs(t[0] - v[0]!) < 0.02 && Math.abs(t[1] - v[1]!) < 0.02 && Math.abs(t[2] - v[2]!) < 0.02)).toBe(true);
  });

  it("a shorter cycle moves faster: after the same frames the phase has gone further", () => {
    const hueAfter = (cycle: number) => {
      c1 = [];
      const c = createAuroraController(host, inputs({ cycle }));
      run(30);
      const last = c1[c1.length - 1]!;
      c.destroy();
      return last;
    };
    const slow = hueAfter(120), fast = hueAfter(8);
    const start = lin(rgbPalette(0).colours[0]);
    const dist = (v: readonly number[]) => Math.hypot(v[0]! - start[0], v[1]! - start[1], v[2]! - start[2]);
    expect(dist(fast)).toBeGreaterThan(dist(slow));
  });

  it("still: no cycling (one frame, the given palette)", () => {
    ctl = createAuroraController(host, inputs({ cycle: 8, still: true }));
    run(40);
    expect(new Set(c1.map((v) => v.join(","))).size).toBeLessThanOrEqual(1);
  });

  it("turning RGB off restores the given palette", () => {
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(40);
    ctl.update(inputs({ cycle: null }));
    run(10);
    expect(near(c1[c1.length - 1]!, lin(DEFAULT_PALETTE.colours[0]))).toBe(true);
  });

  it("no CSS token is written while cycling", () => {
    const before = document.documentElement.getAttribute("style");
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(60);
    expect(document.documentElement.getAttribute("style")).toBe(before);
  });
});
