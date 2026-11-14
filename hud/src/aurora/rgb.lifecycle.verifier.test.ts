// Independent lifecycle probes: exercise the actual renderer with fake WebGL.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAuroraController, type AuroraController, type AuroraInputs } from "./controller";
import { DEFAULT_PALETTE, toLinear } from "./palette";
import { fakeGL, type FakeGL } from "./fakegl";
import { rgbPalette } from "./rgb";

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


describe("independent RGB lifecycle probes", () => {
  it("switching a cycling renderer to Still restores the supplied RGB palette", () => {
    const p = rgbPalette(0);
    ctl = createAuroraController(host, inputs({ cycle: 8, palette: p }));
    run(80);
    expect(near(c1[c1.length - 1]!, lin(p.colours[0]))).toBe(false);
    ctl.update(inputs({ cycle: 8, palette: p, still: true }));
    expect(near(c1[c1.length - 1]!, lin(p.colours[0]))).toBe(true);
    const count = c1.length;
    run(100);
    expect(c1.length).toBe(count);
    expect(raf.size).toBe(0);
  });
  it("a hidden renderer neither cycles nor advances its phase", () => {
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(80);
    ctl.update(inputs({ cycle: 8, visible: false }));
    const count = c1.length;
    run(100, 1000);
    expect(c1.length).toBe(count);
    expect(raf.size).toBe(0);
    ctl.update(inputs({ cycle: 8 }));
    run(1);
    const expectedPhase = (2 / 60 + 79 * 0.017) / 8;
    expect(near(c1[c1.length - 1]!, lin(rgbPalette(expectedPhase).colours[0]))).toBe(true);
  });
  it.each([0, -8, NaN, Infinity])("invalid cycle %s draws the supplied palette without cycling", cycle => {
    ctl = createAuroraController(host, inputs({ cycle }));
    run(100);
    expect(c1.length).toBeGreaterThan(0);
    expect(c1.every(v => near(v, lin(DEFAULT_PALETTE.colours[0])))).toBe(true);
    ctl.update(inputs({ cycle: 8 }));
    run(80);
    expect(near(c1[c1.length - 1]!, lin(DEFAULT_PALETTE.colours[0]))).toBe(false);
  });
  it.each([false, true])("loss/restore after RGB-off keeps the supplied palette (still=%s)", still => {
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(80);
    const canvas = host.querySelector("canvas")!;
    fake.lose();
    canvas.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    ctl.update(inputs({ cycle: null, still }));
    fake.restore();
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    run(10);
    expect(near(c1[c1.length - 1]!, lin(DEFAULT_PALETTE.colours[0]))).toBe(true);
  });
  it("loss/restore into Still with RGB on restores phase zero instead of the last cycled wash", () => {
    const p = rgbPalette(0);
    ctl = createAuroraController(host, inputs({ cycle: 8, palette: p }));
    run(80);
    const canvas = host.querySelector("canvas")!;
    fake.lose();
    canvas.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    ctl.update(inputs({ cycle: 8, palette: p, still: true }));
    fake.restore();
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    run(10);
    expect(near(c1[c1.length - 1]!, lin(p.colours[0]))).toBe(true);
    expect(raf.size).toBe(0);
  });
  it("cycling performs no CSS property writes even when a write would leave the same final value", () => {
    const write = vi.spyOn(document.documentElement.style, "setProperty");
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    run(80);
    expect(write).not.toHaveBeenCalled();
  });
  it("large timestamps and hundreds of phase wraps retain the expected phase", () => {
    clockMs = 2 ** 42;
    ctl = createAuroraController(host, inputs({ cycle: 8 }));
    // Each capped step is 0.05 s: 250 full cycles, without a growing phase accumulator.
    run(40000, 50);
    const expectedPhase = ((1 / 60 + 39999 * 0.05) / 8) % 1;
    expect(near(c1[c1.length - 1]!, lin(rgbPalette(expectedPhase).colours[0]))).toBe(true);
  });
});
