// The worklet-unavailable fallback and the worklet path, with a fake Web Audio
// (jsdom has none). What is real here is the choice between the two paths, the
// framing the fallback does with the same resampler, the 16 kHz frames reaching
// the capture, and the listener seam V5 builds on.

import { afterEach, describe, expect, it, vi } from "vitest";
import { startMicInput } from "./micInput";
import { FRAME_SAMPLES } from "./resample";

class FakeNode {
  connected: unknown[] = [];
  connect(n: unknown) { this.connected.push(n); return n; }
  disconnect() { this.connected = []; }
}
class FakeGain extends FakeNode { gain = { value: 1 }; }
class FakeScript extends FakeNode { onaudioprocess: ((e: unknown) => void) | null = null; }
let workletNodes: FakeWorklet[] = [];
class FakeWorklet extends FakeNode {
  port: { onmessage: ((e: { data: unknown }) => void) | null } = { onmessage: null };
  constructor(public ctx: unknown, public name: string, public options: unknown) { super(); workletNodes.push(this); }
}

function fakeContext(o: { worklet?: "ok" | "missing" | "rejects"; sampleRate?: number } = {}) {
  const calls = { addModule: [] as string[], closed: 0, resumed: 0, script: null as FakeScript | null };
  const ctx: any = {
    sampleRate: o.sampleRate ?? 48000,
    destination: new FakeNode(),
    createGain: () => new FakeGain(),
    createMediaStreamSource: () => new FakeNode(),
    createScriptProcessor: () => (calls.script = new FakeScript()),
    resume: () => { calls.resumed++; return Promise.resolve(); },
    close: () => { calls.closed++; return Promise.resolve(); },
  };
  if (o.worklet !== "missing") {
    ctx.audioWorklet = {
      addModule: async (url: string) => {
        calls.addModule.push(url);
        if (o.worklet === "rejects") throw Object.assign(new Error("Unable to load a worklet's module: private.example/x.js"), { name: "AbortError" });
      },
    };
  }
  return { ctx, calls };
}
const stream = () => {
  const stopped = { n: 0 };
  return { s: { getTracks: () => [{ stop: () => { stopped.n++; } }] } as unknown as MediaStream, stopped };
};

afterEach(() => { vi.unstubAllGlobals(); workletNodes = []; });

describe("startMicInput", () => {
  it("uses the AudioWorklet when it can, and hands its 16 kHz frames to the capture and then to each listener", async () => {
    vi.stubGlobal("AudioWorkletNode", FakeWorklet);
    const { ctx, calls } = fakeContext();
    const { s } = stream();
    const frames: Float32Array[] = [];
    const seen: number[] = [];
    const input = await startMicInput({ stream: s, onFrame: f => frames.push(f), workletUrl: "/assets/micWorklet-x.js", createContext: () => ctx });
    expect(input.path).toBe("worklet");
    expect(input.fallbackReason).toBe("");
    expect(calls.addModule).toEqual(["/assets/micWorklet-x.js"]);
    expect(workletNodes[0].name).toBe("jarvis-mic");
    input.addFrameListener(f => seen.push(f.length));
    const stop = input.addFrameListener(() => { throw new Error("a listener cannot stop the capture"); });
    const f = new Float32Array(FRAME_SAMPLES).fill(0.1);
    workletNodes[0].port.onmessage!({ data: f });
    workletNodes[0].port.onmessage!({ data: "not audio" });          // anything else on the port is ignored
    expect(frames).toEqual([f]);
    expect(seen).toEqual([FRAME_SAMPLES]);
    stop();
    // the node is pulled through a muted gain: it must not play the microphone back
    expect((workletNodes[0].connected[0] as FakeGain).gain.value).toBe(0);
    expect(calls.script).toBeNull();
  });

  it("falls back to the ScriptProcessor, visibly, when there is no AudioWorklet", async () => {
    vi.stubGlobal("AudioWorkletNode", undefined);
    const { ctx, calls } = fakeContext({ worklet: "missing" });
    const frames: Float32Array[] = [];
    const input = await startMicInput({ stream: stream().s, onFrame: f => frames.push(f), workletUrl: "/x.js", createContext: () => ctx });
    expect(input.path).toBe("script_processor");
    expect(input.fallbackReason).toMatch(/AudioWorklet is not available/);
    expect(calls.addModule).toEqual([]);
    // the fallback resamples with the same windowed sinc and frames to 512
    const block = new Float32Array(2048).fill(0.2);
    for (let i = 0; i < 20; i++) calls.script!.onaudioprocess!({ inputBuffer: { getChannelData: () => block } });
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.every(f => f.length === FRAME_SAMPLES)).toBe(true);
    expect(frames[frames.length - 1][100]).toBeCloseTo(0.2, 2);
  });

  it("falls back when the module will not load, and says why without naming where it tried", async () => {
    vi.stubGlobal("AudioWorkletNode", FakeWorklet);
    const { ctx } = fakeContext({ worklet: "rejects" });
    const input = await startMicInput({ stream: stream().s, onFrame: () => {}, workletUrl: "/x.js", createContext: () => ctx });
    expect(input.path).toBe("script_processor");
    expect(input.fallbackReason).toBe("the AudioWorklet module did not load (AbortError)");
    expect(input.fallbackReason).not.toContain("private.example");
    expect(workletNodes).toHaveLength(0);
  });

  it("falls back when the node cannot be constructed", async () => {
    vi.stubGlobal("AudioWorkletNode", class { constructor() { throw new DOMException("bad", "InvalidStateError"); } });
    const { ctx } = fakeContext();
    const input = await startMicInput({ stream: stream().s, onFrame: () => {}, workletUrl: "/x.js", createContext: () => ctx });
    expect(input.path).toBe("script_processor");
  });

  it("stop() releases the microphone and the context", async () => {
    vi.stubGlobal("AudioWorkletNode", FakeWorklet);
    const { ctx, calls } = fakeContext();
    const { s, stopped } = stream();
    const input = await startMicInput({ stream: s, onFrame: () => {}, workletUrl: "/x.js", createContext: () => ctx });
    input.stop();
    expect(stopped.n).toBe(1);
    expect(calls.closed).toBe(1);
  });
});
