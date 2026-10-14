// Silero VAD v5 on onnxruntime-web, without the worker around it, so the model
// loop and the checksum gate are testable with a fake runtime (worker.ts is only
// the message plumbing and the bundled URLs).
//
// Per call: input = the previous frame's last 64 samples + this frame's 512
// (576 floats), the recurrent state [2,1,128] carried from the last call, the
// sample rate as an int64 scalar; output = the speech probability and the new
// state. This is the loop tests/voice_v0/browser/worker_vad.js measured, whose
// probabilities matched native onnxruntime's (V0 check_silero_parity.py).

import { FailError } from "./fail";
import { SILERO_BYTES, SILERO_CONTEXT, SILERO_FRAME, SILERO_SHA256, SILERO_STATE } from "./pins";
import { sha256Hex } from "./sha256";

/** The slice of onnxruntime-web this uses; a fake stands in for it in tests. */
export interface OrtLike {
  InferenceSession: { create(model: Uint8Array, options: Record<string, unknown>): Promise<SessionLike> };
  Tensor: new (type: string, data: Float32Array | BigInt64Array, dims: readonly number[]) => unknown;
}
export interface SessionLike {
  run(feeds: Record<string, unknown>): Promise<Record<string, { data: Float32Array | BigInt64Array }>>;
}

/**
 * The model's bytes, or a `hash` failure. Checked against the pinned digest AND
 * the pinned length before anything parses them: a model that fails either never
 * reaches the runtime.
 */
export async function verifiedModel(
  fetchBytes: () => Promise<Uint8Array>,
  sha256 = SILERO_SHA256,
  bytes = SILERO_BYTES,
): Promise<Uint8Array> {
  let data: Uint8Array;
  try { data = await fetchBytes(); } catch { throw new FailError("fetch"); }
  if (data.length !== bytes || (await sha256Hex(data)) !== sha256) throw new FailError("hash");
  return data;
}

export class SileroRunner {
  private state = new Float32Array(SILERO_STATE);
  private context = new Float32Array(SILERO_CONTEXT);
  private readonly sr: unknown;

  constructor(private ort: OrtLike, private session: SessionLike) {
    this.sr = new ort.Tensor("int64", new BigInt64Array([16000n]), []);
  }

  static async create(ort: OrtLike, model: Uint8Array): Promise<SileroRunner> {
    let session: SessionLike;
    try {
      session = await ort.InferenceSession.create(model, {
        executionProviders: ["wasm"], graphOptimizationLevel: "all", intraOpNumThreads: 1,
      });
    } catch { throw new FailError("runtime"); }
    const runner = new SileroRunner(ort, session);
    try {
      await runner.run(new Float32Array(SILERO_FRAME)); // JIT warm-up: the first call is the slow one
    } catch { throw new FailError("runtime"); }
    runner.reset();
    return runner;
  }

  /** Forget the audio so far (a new stretch of audio, not a continuation of the last). */
  reset() {
    this.state = new Float32Array(SILERO_STATE);
    this.context = new Float32Array(SILERO_CONTEXT);
  }

  /** Speech probability of one 512-sample frame. */
  async run(frame: Float32Array): Promise<number> {
    if (frame.length !== SILERO_FRAME) throw new Error("bad frame");
    const input = new Float32Array(SILERO_CONTEXT + SILERO_FRAME);
    input.set(this.context, 0);
    input.set(frame, SILERO_CONTEXT);
    const out = await this.session.run({
      input: new this.ort.Tensor("float32", input, [1, SILERO_CONTEXT + SILERO_FRAME]),
      state: new this.ort.Tensor("float32", this.state, [2, 1, 128]),
      sr: this.sr,
    });
    const next = out.stateN?.data as Float32Array | undefined;
    const p = (out.output?.data as Float32Array | undefined)?.[0];
    if (!next || next.length !== SILERO_STATE || typeof p !== "number" || !Number.isFinite(p)) throw new Error("bad output");
    this.state = next.slice();
    this.context = frame.slice(SILERO_FRAME - SILERO_CONTEXT);
    return Math.max(0, Math.min(1, p));
  }
}
