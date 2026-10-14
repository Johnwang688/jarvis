// The messages between the VAD stage (main thread) and the Silero worker, and
// the closed set of reasons a detector can be missing. Reasons are codes, not
// error text: the diagnostics line must never carry a URL, a path or the
// engine's own message (voiceDiagnostics.ts: "No audio, transcript, device
// identity or error text").

export type FailCode =
  | "no_worker"   // this window cannot start a Web Worker
  | "worker"      // the worker threw, or its script did not load
  | "fetch"       // the model could not be fetched
  | "hash"        // the model's bytes are not the pinned file
  | "runtime"     // onnxruntime-web / WebAssembly could not build a session
  | "timeout"     // loading took longer than LOAD_DEADLINE_MS
  | "late"        // a frame's verdict came too late
  | "backlog";    // the worker fell behind by more than the queue allows

export const FAIL_TEXT: Readonly<Record<FailCode, string>> = {
  no_worker: "this window cannot start a Web Worker",
  worker: "the VAD worker failed to start",
  fetch: "the VAD model could not be loaded",
  hash: "the VAD model did not match its pinned checksum",
  runtime: "the WebAssembly runtime could not start",
  timeout: "loading the VAD took too long",
  late: "the VAD answered too late",
  backlog: "the VAD fell behind the microphone",
};

/** Why noise suppression (V14) is not running. It never stops the VAD: the frames are scored raw. */
export type DenoiseFailCode =
  | "fetch"     // the RNNoise binary could not be fetched
  | "hash"      // its bytes are not the pinned file
  | "runtime"   // WebAssembly could not compile or run it
  | "slow";     // it kept the worker from keeping up with the microphone

export const DENOISE_FAIL_TEXT: Readonly<Record<DenoiseFailCode, string>> = {
  fetch: "the noise-suppression model could not be loaded",
  hash: "the noise-suppression model did not match its pinned checksum",
  runtime: "the noise-suppression engine could not start",
  slow: "noise suppression was too slow to keep up with the microphone",
};

export type ToWorker =
  | { type: "init" }
  | { type: "frame"; seq: number; samples: Float32Array }
  | { type: "reset" }
  | { type: "denoise"; on: boolean };

export type DenoiseEvent =
  | { state: "off" }
  | { state: "loading" }
  | { state: "ready"; loadMs: number }
  | { state: "failed"; code: DenoiseFailCode };

export type FromWorker =
  | { type: "ready"; loadMs: number }
  | { type: "failed"; code: FailCode }
  | { type: "prob"; seq: number; p: number; ms: number; /** ms spent suppressing noise on this frame (0 when it is not running) */ dms?: number }
  | ({ type: "denoise" } & DenoiseEvent);
