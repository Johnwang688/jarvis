// @vitest-environment jsdom
// B-DIRECT-CAPTURE: fakes only. A synthetic stream is not M6 acceptance.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DISPLAY_OPTIONS, DirectRefused, blank, captureDirect, checkFrame, expectedSize, frameCovered, grabCroppedFrame,
} from "./directCapture";
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
      [{ frame: { width: 3200, height: 2000 } }, "not_cropped"],
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
  it("two device pixels of rounding are allowed, three are not", () => {
    const rect = { width: 400.4, height: 250.3 }, vp = { width: 1600, height: 1000 };
    expect(expectedSize(rect, 1.5)).toEqual({ width: 601, height: 375 });
    expect(checkFrame({ width: 603, height: 373 }, rect, 1.5, vp)).toBe("");
    expect(checkFrame({ width: 604, height: 375 }, rect, 1.5, vp)).toBe("size");
    expect(checkFrame({ width: 2400, height: 1500 }, rect, 1.5, vp)).toBe("not_cropped");
    expect(checkFrame({ width: 0, height: 0 }, { width: 0, height: 0 }, 1, vp)).toBe("size");
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
