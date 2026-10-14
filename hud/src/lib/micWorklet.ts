// The microphone's AudioWorkletProcessor (voice rework V4, plan §3.1). Bundled
// as its own same-origin module (`micInput.ts` imports its URL with Vite's
// `?worker&url`), so the CSP's `script-src 'self'` covers it and nothing loads
// from a CDN. All the logic is `MicFramer` (resample.ts), which is tested
// without Web Audio; this file only adapts it to the worklet scope.
//
// Frames leave on the node's port as transferred Float32Arrays of 512 samples
// at 16 kHz. They queue on the main thread's side if it stalls instead of being
// dropped, which the ScriptProcessorNode it replaces could not promise. V5's
// VAD worker reads the same frames: `micInput.addFrameListener` forwards each
// one, so the worklet needs no change to feed it.

import { MIC_PROCESSOR, MicFramer } from "./resample";

declare const sampleRate: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  constructor(options?: unknown);
}
declare function registerProcessor(name: string, ctor: new (options?: unknown) => AudioWorkletProcessor): void;

class MicProcessor extends AudioWorkletProcessor {
  private framer = new MicFramer(sampleRate, frame => this.port.postMessage(frame, [frame.buffer]));

  process(inputs: Float32Array[][]): boolean {
    this.framer.push(inputs[0]);
    return true;
  }
}

registerProcessor(MIC_PROCESSOR, MicProcessor);
