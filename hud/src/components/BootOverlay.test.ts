// BootOverlay in jsdom with reusable, NON-throwing GL and 2D stubs. A stub that throws on a released
// context would let a removed card guard pass by accident (the effect re-runs, throws, and the boot ends
// anyway); these keep working after loseContext(), so only the card guards themselves can end the boot.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FONT_WAIT_MS, HARD_CAP_MS, SHIELD_MS, SKIP_FADE_MS } from "../lib/boot";
import BootOverlay from "./BootOverlay";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Rig {
  root: Root; host: HTMLDivElement;
  gls: { lost: number; draws: number }[];
  rafs: Map<number, FrameRequestCallback>;
  listeners: Map<string, Set<unknown>>;
  fail: { no2d: boolean; shader: boolean; draw: boolean };
  /** Outstanding timers the boot owns (its cap, font wait, skip fade and shield). */
  timers: Set<number>;
}
let rig: Rig;
const restores: (() => void)[] = [];
const spyOn = <T extends object, K extends keyof T>(o: T, k: K, v: T[K]) => {
  const prev = o[k]; (o as Record<string, unknown>)[k as string] = v; restores.push(() => { (o as Record<string, unknown>)[k as string] = prev; });
};

function setup(fonts?: unknown) {
  const gls: Rig["gls"] = [];
  const rafs = new Map<number, FrameRequestCallback>();
  const listeners = new Map<string, Set<unknown>>();
  const fail = { no2d: false, shader: false, draw: false };
  let next = 1;
  const timers = new Set<number>();
  const ownSet = window.setTimeout, ownClear = window.clearTimeout;
  const mine = new Set<number>([HARD_CAP_MS, FONT_WAIT_MS, SKIP_FADE_MS, SHIELD_MS]);
  spyOn(window, "setTimeout", ((fn: TimerHandler, ms?: number, ...a: unknown[]) => {
    const id = (ownSet as (...x: unknown[]) => number)(fn, ms, ...a);
    if (mine.has(ms ?? -1)) timers.add(id);
    return id;
  }) as unknown as typeof setTimeout);
  spyOn(window, "clearTimeout", ((id?: number) => { timers.delete(id as number); (ownClear as (x?: number) => void)(id); }) as typeof clearTimeout);
  spyOn(window, "requestAnimationFrame", ((cb: FrameRequestCallback) => { rafs.set(next, cb); return next++; }) as typeof requestAnimationFrame);
  spyOn(window, "cancelAnimationFrame", ((id: number) => { rafs.delete(id); }) as typeof cancelAnimationFrame);
  spyOn(window, "matchMedia", (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof matchMedia);
  for (const m of ["addEventListener", "removeEventListener"] as const) {
    const own = window[m].bind(window);
    spyOn(window, m, ((type: string, ...rest: unknown[]) => {
      if (type === "resize" || type === "keydown" || type === "blur") {
        const set = listeners.get(type) ?? new Set<unknown>();
        listeners.set(type, set);
        if (m === "addEventListener") set.add(rest[0]); else set.delete(rest[0]);
      }
      return (own as (...a: unknown[]) => void)(type, ...rest);
    }) as typeof window.addEventListener);
  }
  Object.defineProperty(document, "fonts", { value: fonts, configurable: true });
  restores.push(() => { delete (document as { fonts?: unknown }).fonts; });
  const own = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string) {
    if (type === "webgl") {
      const g = { lost: 0, draws: 0 };
      gls.push(g);
      return new Proxy({}, { get: (_t, p: string) => {
        if (p === "getExtension") return (n: string) => (n === "WEBGL_lose_context" ? { loseContext: () => { g.lost++; } } : {});
        if (p === "getShaderParameter") return () => !fail.shader;
        if (p === "getProgramParameter") return () => true;
        if (p === "getShaderInfoLog") return () => "";
        if (p === "drawArrays") return () => { if (fail.draw) throw new Error("draw"); g.draws++; };
        return () => ({});
      } });
    }
    if (type === "2d") {
      if (fail.no2d && this.closest("#hud-boot")) return null;
      return new Proxy({}, { get: (_t, p: string) => {
        if (p === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
        if (p === "createRadialGradient") return () => ({ addColorStop() {} });
        return () => {};
      }, set: () => true });
    }
    return null;
  } as typeof own;
  restores.push(() => { HTMLCanvasElement.prototype.getContext = own; });
  const host = document.createElement("div");
  document.body.appendChild(host);
  rig = { root: createRoot(host), host, gls, rafs, listeners, fail, timers };
}

const render = (cardUp: boolean) => act(() => { rig.root.render(createElement(BootOverlay, { cardUp })); });
const overlay = () => document.querySelector<HTMLElement>("#hud-boot");
const frames = (n: number) => { for (let i = 0; i < n; i++) { const cbs = [...rig.rafs.entries()]; rig.rafs.clear(); for (const [, cb] of cbs) cb(performance.now() + 400 * (i + 1)); } };
const drawsTotal = () => rig.gls.reduce((a, g) => a + g.draws, 0);
const tick = (ms: number) => { act(() => { vi.advanceTimersByTime(ms); }); };
const key = () => act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" })); });
const press = (type: string) => act(() => { overlay()?.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true })); });
const flush = async () => { await act(async () => { await Promise.resolve(); }); };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  sessionStorage.clear();
  (window as { __hudBootForce?: boolean }).__hudBootForce = true;
});
afterEach(() => {
  act(() => { rig.root.unmount(); });
  rig.host.remove();
  while (restores.length) restores.pop()!();
  vi.useRealTimers();
});

const nothingLeft = () => {
  expect(overlay()).toBeNull();
  expect(document.querySelectorAll("#hud-boot canvas").length).toBe(0);
  expect(rig.rafs.size).toBe(0);
  expect(rig.gls.length).toBeGreaterThan(0);
  expect(rig.gls.every((g) => g.lost >= 1)).toBe(true);
  expect(rig.listeners.get("resize")?.size ?? 0).toBe(0);
  expect(rig.listeners.get("keydown")?.size ?? 0).toBe(0);
  expect(rig.listeners.get("blur")?.size ?? 0).toBe(0);
  tick(HARD_CAP_MS + SHIELD_MS);
  expect(rig.timers.size).toBe(0);
};

describe("BootOverlay", () => {
  it("plays: an overlay, one live GL context, frames being drawn", async () => {
    setup(); await render(false); await flush();
    expect(overlay()).not.toBeNull();
    frames(2);
    expect(drawsTotal()).toBeGreaterThan(1);
    expect(rig.gls.length).toBe(1);
  });

  it("a card arriving mid-boot ends it at once, and nothing restarts under the card", async () => {
    setup(); await render(false); await flush();
    frames(3);
    const contexts = rig.gls.length, draws = drawsTotal();
    await render(true);
    nothingLeft();
    expect(rig.gls.length).toBe(contexts); // no context re-acquired by an effect re-run
    frames(5); tick(HARD_CAP_MS + 1000);
    expect(drawsTotal()).toBe(draws); // no frame drawn under the card
    await render(false); // answering the card never resumes it
    expect(overlay()).toBeNull();
    expect(rig.gls.length).toBe(contexts);
  });

  it("a card arriving during the skip fade ends it at once as well", async () => {
    setup(); await render(false); await flush();
    frames(2);
    key(); // the skip: frames freeze while it fades
    expect(overlay()?.style.opacity).toBe("0");
    const contexts = rig.gls.length, draws = drawsTotal();
    await render(true);
    nothingLeft();
    expect(rig.gls.length).toBe(contexts);
    tick(SKIP_FADE_MS + HARD_CAP_MS); frames(3);
    expect(drawsTotal()).toBe(draws);
    expect(overlay()).toBeNull();
  });

  it("a card up from the start never mounts it", async () => {
    setup(); await render(true); await flush();
    expect(overlay()).toBeNull();
    expect(rig.gls.length).toBe(0);
  });

  it("no 2D context: ends silently and releases the GL context, listeners and frames", async () => {
    setup(); rig.fail.no2d = true; await render(false); await flush();
    nothingLeft();
  });

  it("a shader that does not compile: ends silently and releases everything", async () => {
    setup(); rig.fail.shader = true; await render(false); await flush();
    nothingLeft();
  });

  it("an exception in the initial frame: ends silently, no resize listener or frame loop survives", async () => {
    setup(); rig.fail.draw = true; await render(false); await flush();
    nothingLeft();
  });

  it("unmounting early cancels the font-wait timer (the font never loads)", async () => {
    setup({ load: () => new Promise(() => {}) }); await render(false); await flush();
    expect(rig.timers.size).toBe(2); // the cap and the font wait
    await act(async () => { rig.root.render(createElement("div")); });
    expect(overlay()).toBeNull();
    expect(rig.timers.size).toBe(0);
    expect(rig.gls.every((g) => g.lost >= 1)).toBe(true);
  });

  it("the cap ends the drawing even while the pointer is held; only the invisible shield outlasts it", async () => {
    setup(); await render(false); await flush();
    press("pointerdown");
    frames(1);
    tick(HARD_CAP_MS + 10);
    expect(rig.gls.every((g) => g.lost >= 1)).toBe(true); // GL released at the cap
    expect(rig.rafs.size).toBe(0);
    expect(overlay()?.style.opacity).toBe("0");
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    let reached = false;
    document.body.addEventListener("click", () => { reached = true; }, { once: true });
    act(() => { overlay()!.dispatchEvent(click); }); // a held press's click is still eaten
    expect(reached).toBe(false);
    press("pointerup");
    tick(SHIELD_MS + 10);
    await render(false);
    nothingLeft();
  });

  it("the cap alone ends an idle boot", async () => {
    setup(); await render(false); await flush();
    tick(HARD_CAP_MS + 10);
    await render(false);
    nothingLeft();
  });
});
