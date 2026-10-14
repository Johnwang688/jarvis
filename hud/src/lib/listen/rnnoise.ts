// RNNoise 0.2 as WebAssembly, without the Emscripten glue (voice rework V14).
//
// The binary is the one inside Jitsi's `rnnoise-sync.js`, vendored and pinned
// (pins.ts). We compile the bytes ourselves, after checking them against the
// pinned length and SHA-256, and give the module the two functions it imports
// (`a.a` grows the heap, `a.b` is memcpy); nothing is evaluated, no script is
// fetched, and the only CSP allowance used is `wasm-unsafe-eval`, which the
// Silero runtime already needs. A swapped, truncated or misserved binary never
// compiles: `hash`. The exports are minified names fixed by the pinned binary
// (manifest.json); if one is missing the binary is not the one we pinned.
//
// One frame is 480 samples at 48 kHz (10 ms), floats in [-1, 1], denoised in
// place. RNNoise itself works on int16-scaled floats, which this converts.

import { DenoiseFailError } from "./fail";
import { RNNOISE_BYTES, RNNOISE_FRAME, RNNOISE_SHA256 } from "./pins";
import { sha256Hex } from "./sha256";

export interface RnnoiseEngine {
  /** Denoise 480 samples (48 kHz, [-1, 1]) in place. Returns RNNoise's own speech probability. */
  frame(io: Float32Array): number;
  /** Forget the audio so far (a new stretch of audio). */
  reset(): void;
  /** Free the native state; the engine is unusable afterwards. */
  dispose(): void;
}

/**
 * The binary's bytes, or a `fetch`/`hash` failure. Checked against the pinned
 * digest AND the pinned length before anything compiles them.
 */
export async function verifiedRnnoise(
  fetchBytes: () => Promise<Uint8Array>,
  sha256 = RNNOISE_SHA256,
  bytes = RNNOISE_BYTES,
): Promise<Uint8Array> {
  let data: Uint8Array;
  try { data = await fetchBytes(); } catch { throw new DenoiseFailError("fetch"); }
  if (data.length !== bytes || (await sha256Hex(data)) !== sha256) throw new DenoiseFailError("hash");
  return data;
}

interface Exports {
  c: WebAssembly.Memory;
  d(): void;                                      // static constructors
  e(size: number): number;                        // malloc
  f(ptr: number): void;                           // free
  h(model: number): number;                       // rnnoise_create(NULL) -> state
  i(state: number): void;                         // rnnoise_destroy
  j(state: number, out: number, input: number): number; // rnnoise_process_frame
}

const PAGE = 65536;
const MAX_HEAP = 256 * 1024 * 1024; // RNNoise needs a few MB; refuse anything near the 2 GB the glue allowed

export async function createRnnoise(bytes: Uint8Array): Promise<RnnoiseEngine> {
  let ex: Exports;
  try {
    const imports = {
      a: {
        // emscripten_resize_heap
        a: (requested: number): number => {
          const need = requested >>> 0;
          if (need > MAX_HEAP) return 0;
          try {
            const have = (ex.c.buffer as ArrayBuffer).byteLength;
            if (need > have) ex.c.grow(Math.ceil((need - have) / PAGE));
            return 1;
          } catch { return 0; }
        },
        // emscripten_memcpy_big
        b: (dest: number, src: number, num: number): void => {
          new Uint8Array(ex.c.buffer).copyWithin(dest, src, src + num);
        },
      },
    };
    const { instance } = await WebAssembly.instantiate(bytes as BufferSource, imports);
    ex = instance.exports as unknown as Exports;
    for (const name of ["d", "e", "f", "h", "i", "j"] as const) if (typeof ex[name] !== "function") throw new Error("export");
    if (!(ex.c instanceof WebAssembly.Memory)) throw new Error("memory");
    ex.d();
  } catch {
    throw new DenoiseFailError("runtime");
  }

  const ptr = ex.e(RNNOISE_FRAME * 4);
  let state = ex.h(0);
  if (!ptr || !state) throw new DenoiseFailError("runtime");
  let view = new Float32Array(ex.c.buffer, ptr, RNNOISE_FRAME);
  let alive = true;

  const engine: RnnoiseEngine = {
    frame(io) {
      if (!alive) throw new Error("disposed");
      if (io.length !== RNNOISE_FRAME) throw new Error("bad frame");
      if (view.buffer !== ex.c.buffer) view = new Float32Array(ex.c.buffer, ptr, RNNOISE_FRAME); // the heap grew
      for (let i = 0; i < RNNOISE_FRAME; i++) view[i] = io[i] * 32768;
      const vad = ex.j(state, ptr, ptr);
      for (let i = 0; i < RNNOISE_FRAME; i++) {
        const v = view[i] / 32768;
        io[i] = v > 1 ? 1 : v < -1 ? -1 : v;
      }
      return vad;
    },
    reset() {
      if (!alive) return;
      ex.i(state);
      state = ex.h(0);
      if (!state) { alive = false; throw new Error("create"); }
    },
    dispose() {
      if (!alive) return;
      alive = false;
      try { ex.i(state); ex.f(ptr); } catch { /* going away anyway */ }
    },
  };
  try {
    // JIT warm-up (V0: the slowest frame is the first): the first call is the slow one.
    engine.frame(new Float32Array(RNNOISE_FRAME));
    engine.reset();
  } catch {
    engine.dispose();
    throw new DenoiseFailError("runtime");
  }
  return engine;
}
