import { describe, it, expect } from "vitest";
import { pcmFrames, microphoneSource } from "./sharedMic";

describe("the Windows microphone feed", () => {
  it("reassembles signed samples across odd byte, callback and HTTP boundaries", () => {
    const samples = Int16Array.from({ length: 4800 }, (_, i) => (i * 107 + 32767) % 65536 - 32768);
    const bytes = new Uint8Array(samples.buffer);
    const frames: Float32Array[] = [];
    const push = pcmFrames(frame => frames.push(frame));
    for (let i = 0; i < bytes.length; i += 391) push(bytes.subarray(i, i + 391));
    expect(frames).toHaveLength(9);
    frames.forEach((f, n) => {
      expect(f).toHaveLength(512);
      for (let j = 0; j < f.length; j++) expect(f[j]).toBe(samples[n * 512 + j] / 32768);
    });
  });
  it("reserves a device across launch, reconnect and stop, restores HUD-only voice before first start", () => {
    expect(microphoneSource(null, false, false, false)).toBe("waiting");
    const off = { state: "off", detail: "" } as const;
    expect(microphoneSource(off, true, false, false)).toBe("browser");
    expect(microphoneSource(off, true, true, false)).toBe("waiting");
    expect(microphoneSource({state:"ready", detail:""}, true, false, false)).toBe("waiting");
    expect(microphoneSource({...off, shared_capable:true}, true, false, false)).toBe("waiting");
    expect(microphoneSource({...off, shared_mic:true}, true, false, false)).toBe("windows");
    expect(microphoneSource({...off, hud_mic_paused:true}, true, false, false)).toBe("stopped");
    expect(microphoneSource(off, true, false, true)).toBe("stopped");
  });
});
