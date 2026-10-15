// What the local wake word loads, pinned (voice rework V11, plan §3.6).
//
// The int8 files of V0's sherpa-onnx KWS model (zipformer2 transducer,
// gigaspeech 3.3M, Apache-2.0) are vendored in `hud/vendor/kws-zipformer-gigaspeech-3.3M/`
// and built into `dist/assets/` with content hashes by Vite, so they are served
// from the HUD's own origin: nothing is fetched from a CDN, and no audio
// leaves the window. The worker checks the length and SHA-256 of every file it
// fetched BEFORE any of it reaches the runtime; a changed or truncated file never
// runs and the window says so (Settings -> Voice). `wake.test.ts`/`pins.test.ts`
// hold these, the manifest next to the files and `tests/voice_v0/models.json`
// (which pins the archive they came from) together.

export const KWS_VERSION = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 int8";

export interface Pin { bytes: number; sha256: string }

export const KWS_PINS = {
  encoder: { bytes: 4807159, sha256: "1e721676515bcd42a186979733981213c66c80db680e1cc582dfedf3be76e678" },
  decoder: { bytes: 277985, sha256: "e40ff43297abe815e8898494c17e71bba2152d9d40fa3eb803f75d0f7533329a" },
  joiner: { bytes: 163380, sha256: "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c" },
  tokens: { bytes: 5006, sha256: "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53" },
} as const satisfies Record<string, Pin>;

export type KwsFile = keyof typeof KWS_PINS;
export const KWS_FILES = Object.keys(KWS_PINS) as KwsFile[];

/** The shapes the model was exported with (read from its metadata, V11 notes). */
export const KWS_CHUNK_FRAMES = 45; // feature frames per encoder call ("T")
export const KWS_CHUNK_SHIFT = 32; // new feature frames per call ("decode_chunk_len")
export const KWS_FEATURE_DIM = 80;
export const KWS_VOCAB = 500;
export const KWS_CONTEXT = 2; // tokens of history the prediction network takes
export const KWS_JOINER_DIM = 320;
