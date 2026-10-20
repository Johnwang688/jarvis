// @vitest-environment jsdom
// B-DIRECT-CAPTURE: fakes only. A synthetic stream is not M6 acceptance.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DISPLAY_OPTIONS, DirectRefused, GRID_MAX, blank, captureDirect, checkFrame, frameCovered, grabCroppedFrame, scaleOf,
} from "./directCapture";
import { createElement } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { DevServerPrompts } from "../components/DevServerPrompt";
import { fakeEnv, place } from "./directCaptureFake";

let host: HTMLDivElement, frame: HTMLIFrameElement, shield: HTMLDivElement, hit: Element | null;
beforeEach(() => {
  host = document.createElement("div"); host.className = "framehost";
  frame = document.createElement("iframe"); shield = document.createElement("div"); shield.className = "frameshield";
  host.append(frame, shield); document.body.append(host);
  place(frame, { left: 100, top: 100, width: 400, height: 250 });
  hit = shield;
  (document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint = () => hit;
});
afterEach(() => { host.remove(); document.body.replaceChildren(); });

async function refused(p: Promise<unknown>): Promise<DirectRefused> {
  try { await p; } catch (e) { if (e instanceof DirectRefused) return e; throw e; }
  throw new Error("expected a refusal");
}
const allEnded = (f: ReturnType<typeof fakeEnv>) => f.tracks.length > 0 && f.tracks.every(t => t.readyState === "ended");

describe("Direct capture: one cropped frame, every track stopped", () => {
  it("success crops to the frame, grabs once, stops every track before looking, and closes the frame", async () => {
    const f = fakeEnv();
    const image = await captureDirect(frame, f.env, () => true);
    expect(image).toEqual({ mime: "image/png", data_b64: "aGk=", width: 800, height: 500 });
    expect(f.options).toEqual([DISPLAY_OPTIONS]);
    expect(f.tracks[0].cropped).toEqual({ el: frame });
    expect(f.calls.filter(c => c === "grabFrame")).toHaveLength(1);
    expect(allEnded(f)).toBe(true);
    // Both tracks (video and the stray audio) stop before the pixels are read.
    expect(f.calls.indexOf("stop")).toBeLessThan(f.calls.indexOf("sample"));
    expect(f.frames.every(x => x.closed)).toBe(true);
  });
  it("a cancelled prompt leaves nothing: no stream, no crop, no frame", async () => {
    for (const name of ["NotAllowedError", "AbortError"]) {
      const f = fakeEnv({ prompt: name });
      const e = await refused(captureDirect(frame, f.env, () => true));
      expect(e.reason).toBe("cancelled");
      expect(f.tracks).toEqual([]); expect(f.frames).toEqual([]);
      expect(f.calls).not.toContain("cropTo");
    }
  });
  it("crop, grab and prompt errors stop every track", async () => {
    for (const [o, reason] of [[{ cropError: "NotSupportedError" }, "not_cropped"], [{ noCropTo: true }, "not_cropped"],
      [{ grabError: "InvalidStateError" }, "failed"], [{ surface: "monitor" }, "not_cropped"]] as const) {
      const f = fakeEnv(o);
      expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe(reason);
      expect(allEnded(f)).toBe(true);
    }
    const f = fakeEnv({ prompt: "InvalidStateError" });
    expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe("failed");
  });
  it("a gesture that went stale during the prompt stops the stream the moment it arrives", async () => {
    let live = true;
    const f = fakeEnv({ hold: true });
    const pending = refused(captureDirect(frame, f.env, () => live));
    await new Promise(r => setTimeout(r, 0));
    expect(f.calls).toContain("getDisplayMedia");
    live = false;          // unmount, a card, a hidden pane or another tab
    f.release();
    const e = await pending;
    expect(e.reason).toBe("stale"); expect(e.message).toBe("");
    expect(allEnded(f)).toBe(true);
    expect(f.calls).not.toContain("cropTo"); expect(f.calls).not.toContain("grabFrame");
  });
  it("stale after the grab closes the frame and stops the tracks", async () => {
    let n = 0;
    const f = fakeEnv();
    const e = await refused(captureDirect(frame, f.env, () => { n++; return n < 4; }));
    expect(e.reason).toBe("stale");
    expect(allEnded(f)).toBe(true); expect(f.frames.every(x => x.closed)).toBe(true);
  });
  it("a track with no frame yet is tried again, and only the frame that arrives is kept", async () => {
    const f = fakeEnv({ grabFails: 2 });
    await captureDirect(frame, f.env, () => true);
    expect(f.calls.filter(c => c === "grabFrame")).toHaveLength(3);
    expect(f.frames).toHaveLength(1); expect(allEnded(f)).toBe(true);
    const g = fakeEnv({ grabFails: 3 });
    expect((await refused(captureDirect(frame, g.env, () => true))).errorName).toBe("InvalidStateError");
    expect(allEnded(g)).toBe(true);
  });
  it("a track that never delivers a frame times out and still stops", async () => {
    vi.useFakeTimers();
    try {
      const f = fakeEnv({ grabHang: true });
      const pending = refused(captureDirect(frame, f.env, () => true));
      await vi.advanceTimersByTimeAsync(20_000);
      const e = await pending;
      expect([e.reason, e.errorName]).toEqual(["failed", "TimeoutError"]);
      expect(allEnded(f)).toBe(true);
    } finally { vi.useRealTimers(); }
  });
  it("missing APIs refuse before any prompt", async () => {
    for (const drop of ["CropTarget", "ImageCapture", "mediaDevices"] as const) {
      const f = fakeEnv();
      delete (f.env as unknown as Record<string, unknown>)[drop];
      expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe("unavailable");
      expect(f.calls).not.toContain("getDisplayMedia");
    }
  });
});

describe("Direct capture: the pixels are checked before use", () => {
  it("a frame of the wrong size, the whole tab, or blank is discarded with its tracks stopped", async () => {
    const cases = [
      [{ frame: { width: 790, height: 500 } }, "size"],
      [{ frame: { width: 3000, height: 2000 } }, "not_cropped"],
      [{ sample: new Uint8ClampedArray(16 * 16 * 4).fill(255) }, "blank"],
      [{ sample: new Uint8ClampedArray(16 * 16 * 4) }, "blank"],
      [{ sample: null }, "blank"],
    ] as const;
    for (const [o, reason] of cases) {
      const f = fakeEnv(o);
      const e = await refused(captureDirect(frame, f.env, () => true));
      expect(e.reason).toBe(reason);
      expect(e.message).toContain("Open this in Remote to annotate.");
      expect(allEnded(f)).toBe(true); expect(f.frames.every(x => x.closed)).toBe(true);
      expect(f.calls).not.toContain("encode");
    }
  });
  it("the frame is judged by the layout at the grab, not after Chrome's sharing bar goes away", async () => {
    const f = fakeEnv();
    const box = (h: number) => ({ left: 100, top: 100, right: 500, bottom: 100 + h, x: 100, y: 100, width: 400, height: h, toJSON() {} });
    frame.getBoundingClientRect = () => box(f.tracks.length && f.tracks.every(t => t.readyState === "ended") ? 290 : 250) as DOMRect;
    expect((await captureDirect(frame, f.env, () => true)).height).toBe(500);
  });
  it("accepts the pane's shape at one uniform scale from CSS pixels to device pixels", () => {
    const rect = { width: 400.4, height: 250.3 }, vp = { width: 1500, height: 1000 };
    // Device pixels, and the CSS-pixel frame Chrome delivered under an emulated 1.5 DPR.
    expect(checkFrame({ width: 601, height: 375 }, rect, 1.5, vp)).toEqual({ verdict: "", scale: 601 / 400.4 });
    expect(checkFrame({ width: 400, height: 250 }, rect, 1.5, vp).verdict).toBe("");
    // A large pane at 4K, downscaled by Chrome between the two.
    expect(checkFrame({ width: 2560, height: 1600 }, { width: 1600, height: 1000 }, 2, { width: 1920, height: 1080 }).verdict).toBe("");
    // The short edge must agree within about a pixel.
    expect(checkFrame({ width: 601, height: 379 }, rect, 1.5, vp).verdict).toBe("size");
    // Both edges rounded (to even sizes, say): 1001×999 delivered as 1002×998.
    expect(checkFrame({ width: 1002, height: 998 }, { width: 1001, height: 999 }, 1, vp).verdict).toBe("");
    // Below 0.5, or above the DPR, is not this pane.
    expect(checkFrame({ width: 160, height: 100 }, rect, 1.5, vp).verdict).toBe("size");
    expect(checkFrame({ width: 801, height: 501 }, rect, 1.5, vp).verdict).toBe("size");
    expect(checkFrame({ width: 0, height: 0 }, { width: 0, height: 0 }, 1, vp).verdict).toBe("size");
  });
  it("a frame of the window's shape is not a crop at any scale, and a pane of the window's shape is ambiguous", () => {
    const rect = { width: 400, height: 300 }, vp = { width: 1500, height: 1000 };
    // Including an uncropped tab delivered at 0.4, below the accepted scales.
    for (const f of [{ width: 2250, height: 1500 }, { width: 1500, height: 1000 }, { width: 600, height: 400 }, { width: 150, height: 100 }])
      expect(checkFrame(f, rect, 1.5, vp).verdict).toBe("not_cropped");
    // A pane of the window's shape is ambiguous before the frame is looked at, whatever its scale.
    const square = { width: 1400, height: 875 }, win = { width: 1600, height: 1000 };
    for (const f of [square, { width: 560, height: 350 }, { width: 10, height: 10 }])
      expect(checkFrame(f, square, 2, win).verdict).toBe("ambiguous");
    expect(scaleOf({ width: 1400, height: 875 }, win, 2)).toBe(0.875);
  });
  it("blank means one colour throughout or fully transparent", () => {
    const px = new Uint8ClampedArray(64).fill(200);
    expect(blank(px)).toBe(true);
    px[20] = 10; expect(blank(px)).toBe(false);
    expect(blank(new Uint8ClampedArray(64).map((_, i) => i % 4 === 3 ? 0 : i))).toBe(true);
  });
});

describe("Direct capture: privacy and focus", () => {
  it("anything painted over the frame refuses before the prompt", async () => {
    const card = document.createElement("div");
    for (const setup of [
      () => { hit = card; },                                                     // hit-tests above the frame
      () => { card.setAttribute("role", "dialog"); document.body.append(card); place(card, { left: 300, top: 200, width: 50, height: 50 }); },
      () => { card.setAttribute("data-testid", "approval-card"); document.body.append(card); place(card, { left: 0, top: 0, width: 2000, height: 2000 }); },
      () => { card.setAttribute("role", "menu"); document.body.append(card); place(card, { left: 450, top: 300, width: 200, height: 200 }); },
    ]) {
      hit = shield; card.remove(); for (const a of ["role", "data-testid"]) card.removeAttribute(a);
      setup();
      const f = fakeEnv();
      expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe("covered");
      expect(f.calls).toEqual([]);
    }
  });
  it("the real dev-server prompt over the frame refuses, even between hit-test points", async () => {
    const stub = globalThis as unknown as Record<string, unknown>;
    const saved = stub.ResizeObserver;
    stub.ResizeObserver = class { observe() {} disconnect() {} };
    const mount = document.createElement("div"); document.body.append(mount);
    const root = createRoot(mount);
    try {
      (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
      await act(async () => root.render(createElement(DevServerPrompts, {
        rows: [{ id: "ds_12345678", url: "http://localhost:5173/", source: { kind: "terminal", id: "term1" },
          at: 0, phase: "prompt", token: 1, renewedAt: 0 }] as never,
        hidden: false, layoutKey: "k", zoom: 100, label: () => "Terminal",
        targets: [{ pane: 1, slot: 1, label: "Chat", disabled: false }],
        onOpen: () => {}, onPlace: () => {}, onDismiss: () => {},
      })));
      const card = mount.querySelector('[data-testid="devserver-prompt"]')!;
      expect(card).not.toBeNull();
      expect(card.textContent).toContain("localhost:5173");
      // pointer-events: none on its layer: the hit test sees only the frame's shield.
      const layer = mount.querySelector(".devserver-prompts")!;
      place(layer, { left: 0, top: 0, width: 0, height: 0 });
      place(card, { left: 121, top: 131, width: 12, height: 9 });
      const f = fakeEnv();
      expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe("covered");
      expect(f.calls).toEqual([]);
      // An empty layer (no prompt) covers nothing.
      card.remove();
      expect(frameCovered(frame, [shield], { document, innerWidth: 1600, innerHeight: 1000 })).toBe("");
    } finally {
      await act(async () => root.unmount()); mount.remove(); stub.ResizeObserver = saved;
    }
  });
  it("terminal toasts over the frame refuse", () => {
    const toasts = document.createElement("div"); toasts.className = "termtoasts"; document.body.append(toasts);
    place(toasts, { left: 480, top: 330, width: 15, height: 15 });
    expect(frameCovered(frame, [shield], { document, innerWidth: 1600, innerHeight: 1000 })).toBe("covered");
  });
  it("hit-tests every 48 CSS px or closer, capped near 2000 points", () => {
    const points: [number, number][] = [];
    (document as unknown as { elementFromPoint: (x: number, y: number) => Element }).elementFromPoint = (x, y) => { points.push([x, y]); return shield; };
    expect(frameCovered(frame, [shield], { document, innerWidth: 1600, innerHeight: 1000 })).toBe("");
    const xs = [...new Set(points.map(p => p[0]))].sort((a, b) => a - b);
    expect(Math.max(...xs.slice(1).map((x, i) => x - xs[i]))).toBeLessThanOrEqual(48);
    points.length = 0;
    place(frame, { left: 0, top: 0, width: 7680, height: 4320 });
    frameCovered(frame, [shield], { document, innerWidth: 7680, innerHeight: 4320 });
    expect(points.length).toBeLessThanOrEqual(GRID_MAX); expect(points.length).toBeGreaterThan(1000);
  });
  it("an overlay that shows only between the grabs is caught by the check before each grab", async () => {
    const f = fakeEnv({ grabFails: 1 });
    const card = document.createElement("div"); card.setAttribute("role", "tooltip");
    const grab = f.env.ImageCapture!.prototype.grabFrame;
    let n = 0;
    f.env.ImageCapture!.prototype.grabFrame = async function (this: unknown) {
      if (++n === 1) { document.body.append(card); place(card, { left: 200, top: 200, width: 30, height: 30 }); }
      return grab.call(this);
    };
    const e = await refused(captureDirect(frame, f.env, () => true));
    expect(e.reason).toBe("covered"); expect(allEnded(f)).toBe(true);
    card.remove();
  });
  it("an overlay elsewhere or a hidden options dialog does not refuse", () => {
    const menu = document.createElement("div"); menu.setAttribute("role", "dialog"); menu.hidden = true;
    const far = document.createElement("div"); far.setAttribute("role", "menu");
    document.body.append(menu, far);
    place(menu, { left: 100, top: 100, width: 300, height: 300 });
    place(far, { left: 900, top: 100, width: 100, height: 100 });
    expect(frameCovered(frame, [shield], { document, innerWidth: 1600, innerHeight: 1000 })).toBe("");
  });
  it("a hidden pane, a background tab, an off-screen frame or a hidden page is never captured", async () => {
    const env = { document, innerWidth: 1600, innerHeight: 1000 };
    host.setAttribute("aria-hidden", "true"); expect(frameCovered(frame, [shield], env)).toBe("hidden");
    host.removeAttribute("aria-hidden"); host.setAttribute("inert", ""); expect(frameCovered(frame, [shield], env)).toBe("hidden");
    host.removeAttribute("inert");
    place(frame, { left: 1400, top: 100, width: 400, height: 250 }); expect(frameCovered(frame, [shield], env)).toBe("hidden");
    place(frame, { left: 0, top: 0, width: 0, height: 0 }); expect(frameCovered(frame, [shield], env)).toBe("hidden");
    place(frame, { left: 100, top: 100, width: 400, height: 250 });
    const vis = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState")!;
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    try {
      const f = fakeEnv();
      expect((await refused(captureDirect(frame, f.env, () => true))).reason).toBe("hidden");
      expect(f.calls).toEqual([]);
    } finally { Object.defineProperty(document, "visibilityState", vis); delete (document as unknown as Record<string, unknown>).visibilityState; }
  });
  it("a card that arrives while the prompt is up discards the frame", async () => {
    const f = fakeEnv({ hold: true });
    const pending = refused(captureDirect(frame, f.env, () => true));
    await new Promise(r => setTimeout(r, 0));
    const card = document.createElement("div"); card.setAttribute("data-testid", "approval-card");
    document.body.append(card); place(card, { left: 0, top: 0, width: 1600, height: 1000 });
    f.release();
    expect((await pending).reason).toBe("covered");
    expect(allEnded(f)).toBe(true); expect(f.frames.every(x => x.closed)).toBe(true);
    expect(f.calls).not.toContain("encode");
  });
  it("nothing focuses the frame", async () => {
    const button = document.createElement("button"); document.body.append(button); button.focus();
    let focused = 0;
    frame.focus = () => { focused++; };
    const f = fakeEnv();
    await captureDirect(frame, f.env, () => true);
    expect(focused).toBe(0); expect(document.activeElement).toBe(button);
  });
  it("the facts record each step for the probe", async () => {
    const f = fakeEnv();
    const facts = { prompt: "not_shown" as const, error: "", track: null, crop: { applied: false, error: "" }, tracksLiveAfter: 9, timing: {} };
    const shot = await grabCroppedFrame(frame, f.env, () => true, facts);
    shot.close();
    expect(facts).toMatchObject({ prompt: "accepted", crop: { applied: true, error: "" }, tracksLiveAfter: 0,
      track: { display_surface: "browser", width: 1600, height: 1000, frame_rate: 5, cursor: "never" } });
    expect(Object.keys(facts.timing).sort()).toEqual(["crop", "crop_target", "frame", "prompt", "total"]);
  });
});
