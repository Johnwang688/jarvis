// The aurora's lifecycle in jsdom, with a fake WebGL2 context and a hand-driven frame clock (ported from the
// Bible app's lifecycle, no-GPU, context-loss and stir tests, adapted to the HUD: hover stirs on `window`,
// pause on `document.hidden` only, one still frame under Still or reduced motion).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAuroraController, backingSize, MAX_BACKING_PIXELS, type AuroraController, type AuroraInputs } from "./controller";
import { DEFAULT_PALETTE } from "./palette";
import { fakeGL, type FakeGL } from "./fakegl";

let fake: FakeGL | null;
let raf: Map<number, FrameRequestCallback>;
let nextId: number;
let host: HTMLElement;
let ctl: AuroraController | null;
let clockMs: number;

const inputs = (o: Partial<AuroraInputs> = {}): AuroraInputs => ({
  style: "fluid", palette: DEFAULT_PALETTE, still: false, visible: true, stir: true, activity: true, ...o,
});
const canvas = () => host.querySelector("canvas")!;
const attr = (n: string) => canvas().getAttribute(n);
/** Runs the pending frame callbacks once, at `clockMs`, then advances the clock by `ms`. */
const tick = (ms = 17) => {
  const cbs = [...raf.values()];
  raf.clear();
  cbs.forEach((cb) => cb(clockMs));
  clockMs += ms;
};
/** Frames drawn to the screen (the data attribute lags by up to a quarter second). */
const frames = () => fake!.state.screenDraws;
const run = (n: number, ms = 17) => { for (let i = 0; i < n; i++) tick(ms); };

function pointer(type: string, x: number, y: number, t: number, extra: Record<string, unknown> = {}, target: EventTarget = document.body) {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(ev, { clientX: x, clientY: y, pointerType: "mouse", pointerId: 1 }, extra);
  Object.defineProperty(ev, "timeStamp", { value: t });
  target.dispatchEvent(ev);
  return ev;
}

beforeEach(() => {
  fake = fakeGL();
  raf = new Map();
  nextId = 1;
  clockMs = 1000;
  ctl = null;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { raf.set(nextId, cb); return nextId++; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => void raf.delete(id));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((kind: string) => (kind === "webgl2" && fake ? fake.gl : null)) as never);
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

describe("the canvas", () => {
  it("is aria-hidden, inert to the pointer, never focusable, and half resolution under an area cap", () => {
    ctl = createAuroraController(host, inputs());
    const c = canvas();
    expect(c.getAttribute("aria-hidden")).toBe("true");
    expect(c.style.pointerEvents).toBe("none");
    expect(c.hasAttribute("tabindex")).toBe(false);
    expect([c.width, c.height]).toEqual([500, 300]);
    const big = backingSize(3840, 2160, 2)!;
    expect(big.w * big.h).toBeLessThanOrEqual(MAX_BACKING_PIXELS + big.w + big.h);
    expect(backingSize(1920, 1080, 1)).toEqual({ w: 960, h: 540 });
    expect(backingSize(0, 10, 1)).toBeNull();
  });
});

describe("lifecycle", () => {
  it("runs frames, draws on the GPU at the fluid style", () => {
    ctl = createAuroraController(host, inputs());
    expect(attr("data-aurora-state")).toBe("running");
    expect(attr("data-aurora-style")).toBe("fluid");
    run(10);
    expect(frames()).toBeGreaterThan(3);
    expect(canvas().style.visibility).toBe("visible");
  });

  it("pauses while the document is hidden and resumes", () => {
    ctl = createAuroraController(host, inputs());
    run(8);
    ctl.update(inputs({ visible: false }));
    expect(attr("data-aurora-state")).toBe("paused");
    expect(raf.size).toBe(0);
    const f = fake!.state.screenDraws;
    run(10);
    expect(fake!.state.screenDraws).toBe(f);
    ctl.update(inputs());
    expect(attr("data-aurora-state")).toBe("running");
    expect(raf.size).toBe(1);
  });

  it("Still (or reduced motion) draws exactly one frame and stops", () => {
    ctl = createAuroraController(host, inputs({ still: true }));
    expect(attr("data-aurora-state")).toBe("still");
    expect(fake!.state.screenDraws).toBe(1);
    expect(attr("data-aurora-frames")).toBe("1");
    run(10);
    expect(fake!.state.screenDraws).toBe(1);
    expect(raf.size).toBe(0);
    ctl.update(inputs());
    expect(attr("data-aurora-state")).toBe("running");
    ctl.update(inputs({ still: true }));
    expect(raf.size).toBe(0);
  });

  it("without WebGL2 nothing is drawn and the canvas stays hidden (the CSS wash shows)", () => {
    fake = null;
    ctl = createAuroraController(host, inputs());
    expect(attr("data-aurora-state")).toBe("off");
    expect(canvas().style.visibility).toBe("hidden");
    expect(raf.size).toBe(0);
    ctl.update(inputs({ visible: false }));
    ctl.update(inputs());
    expect(canvas().style.visibility).toBe("hidden");
  });

  it("without half-float targets fluid falls back to flow", () => {
    fake = fakeGL({ halfFloat: false });
    ctl = createAuroraController(host, inputs());
    expect(attr("data-aurora-style")).toBe("flow");
    expect(attr("data-aurora-state")).toBe("running");
  });

  it("a lost context hides the canvas, nothing runs, and a restore rebuilds and resumes", () => {
    ctl = createAuroraController(host, inputs());
    run(5);
    fake!.lose();
    const ev = new Event("webglcontextlost", { cancelable: true });
    canvas().dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true); // or the browser never restores it
    expect(attr("data-aurora-state")).toBe("off");
    expect(canvas().style.visibility).toBe("hidden");
    const draws = fake!.state.screenDraws;
    run(5);
    expect(fake!.state.screenDraws).toBe(draws);
    fake!.restore();
    canvas().dispatchEvent(new Event("webglcontextrestored"));
    expect(attr("data-aurora-state")).toBe("running");
    run(5);
    expect(fake!.state.screenDraws).toBeGreaterThan(draws);
  });

  it("destroy removes the canvas, stops frames and frees every GL object", () => {
    ctl = createAuroraController(host, inputs());
    run(3);
    ctl.destroy();
    expect(host.querySelector("canvas")).toBeNull();
    expect(raf.size).toBe(0);
    expect(fake!.live()).toBe(0);
  });
});

describe("no GPU (a software renderer)", () => {
  it("runs the lighter flow shader, at 30 fps at most", () => {
    fake = fakeGL({ renderer: "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)" });
    ctl = createAuroraController(host, inputs());
    expect(attr("data-aurora-style")).toBe("flow");
    expect(attr("data-aurora-state")).toBe("running");
    run(120, 1000 / 60); // two seconds of a 60 Hz display
    expect(frames()).toBeGreaterThanOrEqual(56);
    expect(frames()).toBeLessThanOrEqual(62);
    expect(ctl.info().software).toBe(true);
  });

  it("the stats override lets fluid run on software for measuring", () => {
    fake = fakeGL({ renderer: "SwiftShader" });
    ctl = createAuroraController(host, inputs(), { allowSoftwareFluid: true });
    expect(attr("data-aurora-style")).toBe("fluid");
  });

  it.each([[60, 120], [120, 120], [144, 144]])("a GPU runs at about 60 fps, not the display's rate (%i Hz: %i-ish frames in two seconds)", (hz) => {
    ctl = createAuroraController(host, inputs());
    run(hz * 2, 1000 / hz);
    expect(frames()).toBeLessThanOrEqual(150);
    expect(frames()).toBeGreaterThanOrEqual(112);
  });
});

describe("stirring by the pointer (fluid only)", () => {
  const stroke = (t0: number, n = 5, extra: Record<string, unknown> = {}, target?: EventTarget) => {
    const evs = [];
    for (let i = 0; i < n; i++) evs.push(pointer("pointermove", 100 + i * 40, 200 + i * 10, t0 + i * 20, extra, target));
    return evs;
  };
  const stirs = () => Number(attr("data-aurora-stirs"));

  it("a hover trail stirs, with a passive listener that never calls preventDefault", () => {
    const add = vi.spyOn(window, "addEventListener");
    ctl = createAuroraController(host, inputs());
    const reg = (add.mock.calls as unknown[][]).find((c) => c[0] === "pointermove")!;
    expect(reg[2]).toMatchObject({ passive: true });
    const evs = stroke(1000);
    expect(evs.every((e) => !e.defaultPrevented)).toBe(true);
    expect(stirs()).toBeGreaterThan(0);
  });

  it("never takes focus: the focused element does not change", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    ctl = createAuroraController(host, inputs());
    stroke(1000);
    expect(document.activeElement).toBe(input);
    input.remove();
  });

  it("stirs nothing when off, on a touch, over a Browser frame, or when not running fluid", () => {
    ctl = createAuroraController(host, inputs({ stir: false }));
    stroke(1000);
    expect(stirs()).toBe(0);
    ctl.update(inputs());
    stroke(2000, 5, { pointerType: "touch" });
    expect(stirs()).toBe(0);
    const frame = document.createElement("iframe");
    document.body.appendChild(frame);
    stroke(3000, 5, {}, frame);
    const shield = document.createElement("div");
    shield.className = "frameshield";
    document.body.appendChild(shield);
    stroke(4000, 5, {}, shield);
    expect(stirs()).toBe(0);
    frame.remove(); shield.remove();
    ctl.update(inputs({ style: "flow" }));
    stroke(5000);
    expect(stirs()).toBe(0);
    ctl.update(inputs({ style: "flow", visible: false }));
    ctl.update(inputs({ style: "flow" }));
    stroke(6000); // flow: still nothing
    expect(stirs()).toBe(0);
  });

  it("is throttled: a 1000 Hz mouse is one stir per ~16 ms", () => {
    ctl = createAuroraController(host, inputs());
    for (let i = 0; i < 200; i++) pointer("pointermove", 100 + i, 300, 1000 + i);
    expect(stirs()).toBeLessThanOrEqual(13);
    expect(stirs()).toBeGreaterThan(5);
  });

  it("stops stirring after destroy (the listener is removed)", () => {
    ctl = createAuroraController(host, inputs());
    ctl.destroy();
    const before = fake!.state.screenDraws;
    stroke(1000);
    expect(host.querySelector("canvas")).toBeNull();
    expect(fake!.state.screenDraws).toBe(before);
  });
});

describe("chat ripples", () => {
  const rect = { left: 100, top: 100, width: 400, height: 80 };
  it("a ripple is made while running fluid and counted; not when off, paused or flow", () => {
    ctl = createAuroraController(host, inputs());
    expect(ctl.ripple(rect)).toBe(true);
    expect(attr("data-aurora-ripples")).toBe("1");
    ctl.update(inputs({ activity: false }));
    expect(ctl.ripple(rect)).toBe(false);
    ctl.update(inputs({ visible: false }));
    expect(ctl.ripple(rect)).toBe(false);
    ctl.update(inputs({ style: "flow" }));
    expect(ctl.ripple(rect)).toBe(false);
    expect(attr("data-aurora-ripples")).toBe("1");
  });

  it("a rect off the screen or empty makes none", () => {
    ctl = createAuroraController(host, inputs());
    expect(ctl.ripple({ left: 5000, top: 5000, width: 10, height: 10 })).toBe(false);
    expect(ctl.ripple({ left: 0, top: 0, width: 0, height: 0 })).toBe(false);
  });
});
