// The keyword spotter on onnxruntime-web (voice rework V11): features -> streaming
// zipformer2 encoder (45 frames in, 32 new, 8 out, with its attention/convolution
// caches carried between calls) -> the beam search of decoder.ts with the prediction
// network and joiner as the scorer. A port of sherpa-onnx's `KeywordSpotter` for
// this one model; the loop mirrors `keyword-spotter-transducer-impl.h`, including
// its restart after 1.5 s of trailing silence and after every hit.
//
// Only the runtime is abstract (`WakeOrt`), so the arithmetic is tested against a
// fake and against the real model in Node (`engine.test.ts`, `parity.test.ts`).

import { FailError } from "../listen/fail";
import { sha256Hex } from "../listen/sha256";
import { ContextGraph, type KeywordSpec } from "./contextGraph";
import { KeywordStream, DEFAULTS, type Hit, type Joint } from "./decoder";
import { OnlineFbank } from "./fbank";
import { KWS_CHUNK_FRAMES, KWS_CHUNK_SHIFT, KWS_CONTEXT, KWS_FEATURE_DIM, KWS_FILES, KWS_JOINER_DIM, KWS_PINS, KWS_VOCAB, type KwsFile } from "./pins";

/** The slice of onnxruntime-web this uses; a fake stands in for it in tests. */
export interface WakeTensor { data: Float32Array | BigInt64Array; dims: readonly number[]; type?: string }
export interface WakeSession {
  inputNames: readonly string[];
  outputNames: readonly string[];
  inputMetadata: readonly { name: string; type?: string; shape?: readonly (number | string)[] }[];
  run(feeds: Record<string, unknown>): Promise<Record<string, WakeTensor>>;
}
export interface WakeOrt {
  InferenceSession: { create(model: Uint8Array, options: Record<string, unknown>): Promise<WakeSession> };
  Tensor: new (type: string, data: Float32Array | BigInt64Array, dims: readonly number[]) => unknown;
}

export type KwsBytes = Record<KwsFile, Uint8Array>;

/**
 * The files' bytes, or a `fetch` / `hash` failure. Every file is checked against its pinned length AND
 * digest before anything is parsed; one wrong file fails the lot, so a mixed set never runs.
 */
export async function verifiedFiles(fetchOne: (file: KwsFile) => Promise<Uint8Array>): Promise<KwsBytes> {
  const out = {} as KwsBytes;
  for (const file of KWS_FILES) {
    let data: Uint8Array;
    try { data = await fetchOne(file); } catch { throw new FailError("fetch"); }
    if (data.length !== KWS_PINS[file].bytes || (await sha256Hex(data)) !== KWS_PINS[file].sha256) throw new FailError("hash");
    out[file] = data;
  }
  return out;
}

const MAX_CACHED_CONTEXTS = 256;

export class KwsEngine {
  private decoderCache = new Map<string, Float32Array>();
  /** Encoder calls and the time they took, for the CPU line in Settings. */
  chunks = 0;
  encoderMs = 0;

  private constructor(
    private ort: WakeOrt,
    private enc: WakeSession,
    private dec: WakeSession,
    private join: WakeSession,
  ) {}

  static async create(ort: WakeOrt, bytes: KwsBytes): Promise<KwsEngine> {
    const options = { executionProviders: ["wasm"], graphOptimizationLevel: "all", intraOpNumThreads: 1 };
    let enc: WakeSession, dec: WakeSession, join: WakeSession;
    try {
      [enc, dec, join] = await Promise.all([
        ort.InferenceSession.create(bytes.encoder, options),
        ort.InferenceSession.create(bytes.decoder, options),
        ort.InferenceSession.create(bytes.joiner, options),
      ]);
    } catch { throw new FailError("runtime"); }
    // The encoder's inputs are x + its caches and its outputs are encoder_out + the new caches, in one order.
    if (enc.inputNames[0] !== "x" || enc.outputNames.length !== enc.inputNames.length) throw new FailError("runtime");
    const engine = new KwsEngine(ort, enc, dec, join);
    try {
      // JIT warm-up: the first call of each session is the slow one. A silent chunk through the whole path.
      const s = engine.newStream([]);
      await s.warm();
    } catch { throw new FailError("runtime"); }
    engine.chunks = 0;
    engine.encoderMs = 0;
    return engine;
  }

  newStream(keywords: KeywordSpec[]): WakeStream {
    return new WakeStream(this, keywords);
  }

  /** Zeroed encoder caches, shaped by the model's own input metadata (batch 1). */
  initialStates(): { name: string; tensor: WakeTensor }[] {
    return this.enc.inputMetadata.filter((m) => m.name !== "x").map((m) => {
      const dims = (m.shape ?? []).map((d) => (typeof d === "number" && d > 0 ? d : 1));
      const n = dims.reduce((a, b) => a * b, 1);
      const int = (m.type ?? "float32") === "int64";
      return { name: m.name, tensor: { data: int ? new BigInt64Array(n) : new Float32Array(n), dims, type: int ? "int64" : "float32" } };
    });
  }

  /** One chunk: 45 x 80 features and the caches in, 8 x 320 frames and the new caches out. */
  async encode(features: Float32Array, states: { name: string; tensor: WakeTensor }[]): Promise<{ frames: Float32Array[]; states: { name: string; tensor: WakeTensor }[] }> {
    const feeds: Record<string, unknown> = { x: new this.ort.Tensor("float32", features, [1, KWS_CHUNK_FRAMES, KWS_FEATURE_DIM]) };
    for (const s of states) feeds[s.name] = new this.ort.Tensor(s.tensor.type === "int64" ? "int64" : "float32", s.tensor.data, s.tensor.dims);
    const t0 = performance.now();
    const out = await this.enc.run(feeds);
    this.encoderMs += performance.now() - t0;
    this.chunks++;
    const names = this.enc.outputNames;
    const eo = out[names[0]];
    const T = eo.dims[1];
    const dim = eo.dims[2];
    if (dim !== KWS_JOINER_DIM || !(eo.data instanceof Float32Array)) throw new Error("bad encoder output");
    const frames = Array.from({ length: T }, (_, t) => eo.data.slice(t * dim, (t + 1) * dim) as Float32Array);
    const next = states.map((s, i) => {
      const o = out[names[i + 1]];
      if (!o || o.data.length !== s.tensor.data.length) throw new Error("bad state");
      return { name: s.name, tensor: { data: o.data, dims: o.dims, type: s.tensor.type } };
    });
    return { frames, states: next };
  }

  /** The prediction network + joiner, batched over the live paths. */
  joint: Joint = async (contexts, frame) => {
    const n = contexts.length;
    const keys = contexts.map((c) => c.join(","));
    const missing = [...new Set(keys.filter((k) => !this.decoderCache.has(k)))];
    if (missing.length) {
      const y = new BigInt64Array(missing.length * KWS_CONTEXT);
      missing.forEach((k, i) => k.split(",").forEach((v, j) => { y[i * KWS_CONTEXT + j] = BigInt(v); }));
      const out = await this.dec.run({ y: new this.ort.Tensor("int64", y, [missing.length, KWS_CONTEXT]) });
      const d = out[this.dec.outputNames[0]].data as Float32Array;
      if (this.decoderCache.size + missing.length > MAX_CACHED_CONTEXTS) this.decoderCache.clear();
      missing.forEach((k, i) => this.decoderCache.set(k, d.slice(i * KWS_JOINER_DIM, (i + 1) * KWS_JOINER_DIM)));
    }
    const e = new Float32Array(n * KWS_JOINER_DIM);
    const d = new Float32Array(n * KWS_JOINER_DIM);
    for (let i = 0; i < n; i++) { e.set(frame, i * KWS_JOINER_DIM); d.set(this.decoderCache.get(keys[i])!, i * KWS_JOINER_DIM); }
    const out = await this.join.run({
      encoder_out: new this.ort.Tensor("float32", e, [n, KWS_JOINER_DIM]),
      decoder_out: new this.ort.Tensor("float32", d, [n, KWS_JOINER_DIM]),
    });
    const logits = out[this.join.outputNames[0]].data as Float32Array;
    return Array.from({ length: n }, (_, i) => logits.slice(i * KWS_VOCAB, (i + 1) * KWS_VOCAB));
  };
}

/** A stretch of audio being listened to: feed it samples, it says when a keyword was spoken. */
export class WakeStream {
  private fbank = new OnlineFbank();
  private decoder: KeywordStream;
  private states: { name: string; tensor: WakeTensor }[];
  private startFrame = 0; // absolute feature frame where the encoder last (re)started
  private processed = 0; // feature frames the encoder has consumed since then
  /** Samples fed so far. */
  samples = 0;

  constructor(private engine: KwsEngine, keywords: KeywordSpec[]) {
    this.decoder = new KeywordStream(new ContextGraph(keywords), engine.joint, DEFAULTS);
    this.states = engine.initialStates();
  }

  /** Restart the network and the search (features keep flowing), as the original does after a hit or long silence. */
  private restart() {
    this.startFrame += this.processed;
    this.processed = 0;
    this.states = this.engine.initialStates();
    this.decoder.reset();
  }

  /** Forget all audio: a new stretch (the mic was off), not a continuation. */
  reset() {
    this.fbank = new OnlineFbank();
    this.startFrame = 0;
    this.processed = 0;
    this.samples = 0;
    this.states = this.engine.initialStates();
    this.decoder.reset();
  }

  /** Runs one silent chunk through the network (warm-up) without touching this stream's own state. */
  async warm() {
    const { frames } = await this.engine.encode(new Float32Array(KWS_CHUNK_FRAMES * KWS_FEATURE_DIM), this.engine.initialStates());
    await this.engine.joint([[-1, 0]], frames[0]);
  }

  /** Audio in; the first keyword it completed, or null. Every ready chunk is decoded. */
  async accept(chunk: Float32Array): Promise<Hit | null> {
    this.fbank.accept(chunk);
    this.samples += chunk.length;
    let hit: Hit | null = null;
    while (this.startFrame + this.processed + KWS_CHUNK_FRAMES < this.fbank.ready) {
      // 40 ms per output frame: after 1.5 s of blanks the original starts over.
      if (this.decoder.trailingBlanks * 4 * 0.01 > 1.5) this.restart();
      const features = this.fbank.take(this.startFrame + this.processed, KWS_CHUNK_FRAMES);
      const { frames, states } = await this.engine.encode(features, this.states);
      this.states = states;
      this.processed += KWS_CHUNK_SHIFT;
      this.fbank.discard(this.startFrame + this.processed);
      const h = await this.decoder.decode(frames);
      if (h) { hit = hit ?? h; this.restart(); }
    }
    return hit;
  }
}
