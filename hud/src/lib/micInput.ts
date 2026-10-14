// Opening the microphone (voice rework V4, plan §3.1): getUserMedia, then an
// AudioWorklet that resamples to 16 kHz mono in 512-sample frames. If the
// worklet cannot run (an insecure origin, an old browser, a module that fails
// to load) the same resampler runs behind a ScriptProcessorNode instead and
// the window says so: `path` names which one is live, and the diagnostics and
// the error line show it. Nothing here decides what to do with a frame.
//
// The seam V5 and V12 build on: every frame goes to `onFrame` (the capture) and
// to each `addFrameListener` (a VAD worker, a recorder), in order.

import { MIC_PROCESSOR, MicFramer } from "./resample";

export type CapturePath = "worklet" | "script_processor";

export interface MicInput {
  path: CapturePath;
  /** The audio context's own rate; frames are always 16 kHz. */
  sampleRate: number;
  /** Also receive every frame, after the capture has. Returns the unsubscribe. */
  addFrameListener(fn: (frame: Float32Array) => void): () => void;
  /** Why the worklet was not used, when `path` is the fallback. Never contains audio or device names. */
  fallbackReason: string;
  stop(): void;
}

export interface MicOptions {
  stream: MediaStream;
  onFrame: (frame: Float32Array) => void;
  /** The bundled worklet module's URL. */
  workletUrl: string;
  createContext?: () => AudioContext;
}

export async function startMicInput(o: MicOptions): Promise<MicInput> {
  const ctx = o.createContext ? o.createContext() : new AudioContext();
  const listeners = new Set<(frame: Float32Array) => void>();
  const deliver = (frame: Float32Array) => {
    o.onFrame(frame);
    for (const fn of listeners) { try { fn(frame); } catch { /* a listener cannot stop the capture */ } }
  };
  try { void ctx.resume?.()?.catch?.(() => {}); } catch { /* an already running context */ }
  const source = ctx.createMediaStreamSource(o.stream);
  const quiet = ctx.createGain();
  quiet.gain.value = 0; // the node must be pulled, and must not play the mic back
  quiet.connect(ctx.destination);

  let node: AudioNode | null = null;
  let reason = "";
  const hasWorklet = !!(ctx as any).audioWorklet && typeof (globalThis as any).AudioWorkletNode === "function";
  if (!hasWorklet) reason = "AudioWorklet is not available in this window";
  else {
    try {
      await ctx.audioWorklet.addModule(o.workletUrl);
      const worklet = new AudioWorkletNode(ctx, MIC_PROCESSOR, { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      worklet.port.onmessage = (e: MessageEvent) => { if (e.data instanceof Float32Array) deliver(e.data); };
      source.connect(worklet);
      worklet.connect(quiet);
      node = worklet;
    } catch (error: unknown) {
      reason = `the AudioWorklet module did not load (${error instanceof Error && error.name ? error.name : "error"})`;
      node = null;
    }
  }
  let path: CapturePath = "worklet";
  if (!node) {
    path = "script_processor";
    const framer = new MicFramer(ctx.sampleRate, deliver);
    const sp = ctx.createScriptProcessor(2048, 1, 1);
    sp.onaudioprocess = (e) => framer.pushMono(Float32Array.from(e.inputBuffer.getChannelData(0)));
    source.connect(sp);
    sp.connect(quiet);
    node = sp;
  }
  return {
    path, sampleRate: ctx.sampleRate, fallbackReason: reason,
    addFrameListener: (fn) => { listeners.add(fn); return () => { listeners.delete(fn); }; },
    stop: () => {
      try { source.disconnect(); } catch { /* already gone */ }
      try { node?.disconnect(); } catch { /* ditto */ }
      o.stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
    },
  };
}
