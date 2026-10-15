import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

describe("createSpeech wires the self-voice gate (V9)", () => {
  it("an audio device coming or going makes the gate forget the room, and dispose lets go of the listener", async () => {
    const devices = new EventTarget();
    vi.stubGlobal("navigator", { mediaDevices: devices });
    vi.stubGlobal("fetch", async () => new Response("{}", { status: 200 }));
    const { createSpeech } = await import("./index");
    const speech = createSpeech();
    const spy = vi.spyOn(speech.selfVoice, "invalidate");
    devices.dispatchEvent(new Event("devicechange"));
    expect(spy).toHaveBeenCalledTimes(1);
    speech.dispose();
    devices.dispatchEvent(new Event("devicechange"));
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("a window with Web Audio has the gate in force; one without has the blanket hold", async () => {
    vi.stubGlobal("fetch", async () => new Response("{}", { status: 200 }));
    for (const has of [true, false]) {
      vi.stubGlobal("window", Object.assign(Object.create(globalThis.window ?? null), has ? { AudioContext: class {} } : { AudioContext: undefined, webkitAudioContext: undefined }));
      vi.resetModules();
      const { createSpeech } = await import("./index");
      const speech = createSpeech();
      expect(speech.selfVoice.runnable(), `Web Audio ${has}`).toBe(has);
      speech.dispose();
      vi.unstubAllGlobals();
    }
  });
});
