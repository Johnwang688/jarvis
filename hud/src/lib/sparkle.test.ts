import { afterEach, describe, expect, it } from "vitest";
import {
  BURST_MAX, BURST_MIN, LAYER_ID, LIFETIME_MS, MAX_PARTICLES, SPARKLE_KEY, createSparkle, loadSparkle, saveSparkle,
  type Sparkle, type SparkleHost,
} from "./sparkle";

const store = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};

// A host whose frames and timers the test drives by hand.
function rig(extra: Partial<SparkleHost> = {}) {
  const frames: (() => void)[] = [];
  const timers = new Map<number, { cb: () => void; ms: number }>();
  let next = 1;
  const sp = createSparkle({
    doc: document,
    storage: store(),
    reducedMotion: () => false,
    raf: (cb) => { frames.push(cb); },
    setTimer: (cb, ms) => { timers.set(next, { cb, ms }); return next++; },
    clearTimer: (id) => { timers.delete(id as number); },
    ...extra,
  });
  return {
    sp, frames, timers,
    flush() { while (frames.length) frames.shift()!(); },
    layer: () => document.getElementById(LAYER_ID),
    bits: () => Array.from(document.querySelectorAll<HTMLElement>(".sparkle-bit")),
  };
}

let active: Sparkle[] = [];
function mount(extra: Partial<SparkleHost> = {}) {
  const r = rig(extra);
  active.push(r.sp);
  return r;
}
afterEach(() => {
  for (const s of active) s.dispose();
  active = [];
  document.body.innerHTML = "";
});

function button(html = "<button>go</button>") {
  document.body.insertAdjacentHTML("beforeend", html);
  return document.body.lastElementChild as HTMLElement;
}
const click = (el: Element, init: MouseEventInit = {}) =>
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX: 40, clientY: 50, detail: 1, ...init }));

describe("the click sparkle", () => {
  it("bursts a few 1s and 0s from the click point, a frame after the click", () => {
    const r = mount();
    const b = button();
    click(b);
    expect(r.bits()).toHaveLength(0); // nothing drawn inside the click itself
    r.flush();
    const bits = r.bits();
    expect(bits.length).toBeGreaterThanOrEqual(BURST_MIN);
    expect(bits.length).toBeLessThanOrEqual(BURST_MAX);
    for (const bit of bits) {
      expect(["0", "1"]).toContain(bit.textContent);
      expect(bit.style.left).toBe("40px");
      expect(bit.style.top).toBe("50px");
      expect(Number.isFinite(parseFloat(bit.style.getPropertyValue("--dx")))).toBe(true);
    }
    // Both glyphs turn up over a handful of clicks.
    for (let i = 0; i < 4; i++) { click(b); r.flush(); }
    expect(new Set(r.bits().map((x) => x.textContent))).toEqual(new Set(["0", "1"]));
  });

  it("draws in one aria-hidden layer under <body> that nothing can focus", () => {
    const r = mount();
    click(button());
    r.flush();
    const layer = r.layer()!;
    expect(layer.parentElement).toBe(document.body);
    expect(layer.getAttribute("aria-hidden")).toBe("true");
    expect(layer.querySelectorAll("[tabindex], button, input, a").length).toBe(0);
    expect(document.querySelectorAll(`#${LAYER_ID}`)).toHaveLength(1);
  });

  it("works for [role=button] and a click on a child of a button", () => {
    const r = mount();
    click(button('<div role="button">x</div>'));
    r.flush();
    expect(r.bits().length).toBeGreaterThan(0);
    r.sp.setEnabled(true);
    const b = button("<button><span id='inner'>icon</span></button>");
    const before = r.bits().length;
    click(b.querySelector("#inner")!);
    r.flush();
    expect(r.bits().length).toBeGreaterThan(before);
  });

  it("ignores clicks that are not on a button and on disabled buttons", () => {
    const r = mount();
    click(button("<div>plain</div>"));
    click(button("<button disabled>no</button>"));
    click(button('<div role="button" aria-disabled="true">no</div>'));
    r.flush();
    expect(r.bits()).toHaveLength(0);
    expect(r.layer()).toBeNull();
  });

  it("never prevents or stops the original event, and the button's own handler runs first time", () => {
    const r = mount();
    const b = button();
    let ran = 0;
    let bubbled = 0;
    b.addEventListener("click", () => { ran++; });
    document.body.addEventListener("click", () => { bubbled++; });
    const e = new MouseEvent("click", { bubbles: true, cancelable: true, clientX: 5, clientY: 6, detail: 1 });
    let stopped = false;
    e.stopPropagation = () => { stopped = true; };
    e.stopImmediatePropagation = () => { stopped = true; };
    const notCancelled = b.dispatchEvent(e);
    expect(notCancelled).toBe(true);
    expect(e.defaultPrevented).toBe(false);
    expect(stopped).toBe(false);
    expect(ran).toBe(1);
    expect(bubbled).toBe(1);
    r.flush();
    expect(r.bits().length).toBeGreaterThan(0);
  });

  it("does not touch focus", () => {
    const r = mount();
    document.body.insertAdjacentHTML("beforeend", "<input id='i'>");
    const input = document.getElementById("i") as HTMLInputElement;
    input.focus();
    click(button());
    r.flush();
    expect(document.activeElement).toBe(input);
  });

  it("a keyboard activation (detail 0) bursts from the button's centre", () => {
    const r = mount();
    const b = button();
    b.getBoundingClientRect = () => ({ left: 100, top: 200, width: 60, height: 20, right: 160, bottom: 220, x: 100, y: 200, toJSON() {} }) as DOMRect;
    click(b, { detail: 0, clientX: 0, clientY: 0 });
    r.flush();
    for (const bit of r.bits()) {
      expect(bit.style.left).toBe("130px");
      expect(bit.style.top).toBe("210px");
    }
    expect(r.bits().length).toBeGreaterThan(0);
  });

  it("keeps the centre even when the click's action removes the button before the frame", () => {
    const r = mount();
    const b = button();
    b.getBoundingClientRect = () => ({ left: 10, top: 10, width: 20, height: 20, right: 30, bottom: 30, x: 10, y: 10, toJSON() {} }) as DOMRect;
    click(b, { detail: 0, clientX: 0, clientY: 0 });
    b.remove();
    r.flush();
    expect(r.bits()[0].style.left).toBe("20px");
  });

  it("caps live glyphs under rapid clicking, dropping the oldest", () => {
    const r = mount();
    const b = button();
    for (let i = 0; i < 60; i++) { click(b, { clientX: i, clientY: i }); r.flush(); }
    expect(r.sp.live()).toBeLessThanOrEqual(MAX_PARTICLES);
    expect(r.bits().length).toBe(r.sp.live());
    expect(r.bits().length).toBeGreaterThan(MAX_PARTICLES - BURST_MAX);
    // the newest click's glyphs survive, the first click's are gone
    expect(r.bits().some((x) => x.style.left === "59px")).toBe(true);
    expect(r.bits().some((x) => x.style.left === "0px")).toBe(false);
  });

  it("removes each glyph when its animation ends, and by timer when it never does", () => {
    const r = mount();
    const b = button();
    click(b);
    r.flush();
    const [first, ...rest] = r.bits();
    first.dispatchEvent(new Event("animationend"));
    expect(r.bits()).toHaveLength(rest.length);
    expect(r.sp.live()).toBe(rest.length);
    // The fallback timers outlast the animation a little and clear the rest.
    const ms = [...r.timers.values()].map((t) => t.ms);
    expect(Math.min(...ms)).toBeGreaterThanOrEqual(LIFETIME_MS);
    for (const t of [...r.timers.values()]) t.cb();
    expect(r.bits()).toHaveLength(0);
    expect(r.sp.live()).toBe(0);
    expect(r.timers.size).toBe(0);
  });

  it("leaves nothing behind after dispose, and stops listening", () => {
    const r = mount();
    const b = button();
    click(b);
    r.flush();
    r.sp.dispose();
    expect(r.layer()).toBeNull();
    click(b);
    r.flush();
    expect(r.layer()).toBeNull();
  });

  it("is off under prefers-reduced-motion: reduce, checked at each click", () => {
    let reduce = true;
    const r = mount({ reducedMotion: () => reduce });
    const b = button();
    click(b);
    r.flush();
    expect(r.bits()).toHaveLength(0);
    expect(r.layer()).toBeNull();
    reduce = false;
    click(b);
    r.flush();
    expect(r.bits().length).toBeGreaterThan(0);
  });

  it("reads the system preference through matchMedia by default", () => {
    const real = window.matchMedia;
    window.matchMedia = ((q: string) => ({ matches: q.includes("reduce") })) as unknown as typeof window.matchMedia;
    try {
      const r = mount({ reducedMotion: undefined });
      click(button());
      r.flush();
      expect(r.bits()).toHaveLength(0);
    } finally {
      window.matchMedia = real;
    }
  });

  it("is off when the switch is off, remembered, and clears what is on screen", () => {
    const s = store();
    const r = mount({ storage: s });
    const b = button();
    click(b);
    r.flush();
    expect(r.bits().length).toBeGreaterThan(0);
    r.sp.setEnabled(false);
    expect(s.m[SPARKLE_KEY]).toBe("0");
    expect(r.bits()).toHaveLength(0);
    click(b);
    r.flush();
    expect(r.bits()).toHaveLength(0);
    r.sp.setEnabled(true);
    expect(s.m[SPARKLE_KEY]).toBe("1");
    click(b);
    r.flush();
    expect(r.bits().length).toBeGreaterThan(0);
  });

  it("a click queued before the switch went off draws nothing", () => {
    const r = mount();
    const b = button();
    click(b);
    r.sp.setEnabled(false);
    r.flush();
    expect(r.bits()).toHaveLength(0);
  });

  it("starts from the stored preference and follows another window's change", () => {
    const s = store({ [SPARKLE_KEY]: "0" });
    const r = mount({ storage: s });
    expect(r.sp.enabled()).toBe(false);
    s.m[SPARKLE_KEY] = "1";
    window.dispatchEvent(new StorageEvent("storage", { key: SPARKLE_KEY }));
    expect(r.sp.enabled()).toBe(true);
  });

  it("an exception while drawing never reaches the app", () => {
    const r = mount({ random: () => { throw new Error("boom"); } });
    expect(() => { click(button()); r.flush(); }).not.toThrow();
  });
});

describe("the sparkle preference", () => {
  it("is on by default, and only an explicit 0 turns it off", () => {
    expect(loadSparkle(store())).toBe(true);
    expect(loadSparkle(store({ [SPARKLE_KEY]: "garbage" }))).toBe(true);
    expect(loadSparkle(store({ [SPARKLE_KEY]: "0" }))).toBe(false);
  });
  it("round-trips", () => {
    const s = store();
    saveSparkle(false, s);
    expect(loadSparkle(s)).toBe(false);
    saveSparkle(true, s);
    expect(loadSparkle(s)).toBe(true);
  });
  it("blocked storage means on and does not throw", () => {
    const bad = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
    expect(loadSparkle(bad)).toBe(true);
    expect(() => saveSparkle(false, bad)).not.toThrow();
  });
});
