// Test helpers for the wake word: the vendored model on onnxruntime-web in Node (the same WASM the
// worker runs in a browser), WAV reading, and the synthetic fixtures. Imported by tests only.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import * as ort from "onnxruntime-web/wasm";
import { KwsEngine, type WakeOrt } from "./engine";
import { COMMAND_OF, COMMAND_PIECES, parseTokens, specsFor, WAKE_PIECES } from "./keywords";
import type { Hit } from "./decoder";

export const HUD = resolve(__dirname, "../../..");
export const MODEL_DIR = resolve(HUD, "vendor/kws-zipformer-gigaspeech-3.3M");
export const FIXTURES = resolve(HUD, "../tests/face/fixtures/voice_v11");

export function modelBytes() {
  const f = (n: string) => new Uint8Array(readFileSync(resolve(MODEL_DIR, n)));
  return { encoder: f("encoder.int8.onnx"), decoder: f("decoder.int8.onnx"), joiner: f("joiner.int8.onnx"), tokens: f("tokens.txt") };
}

export function tokenTable() {
  return parseTokens(readFileSync(resolve(MODEL_DIR, "tokens.txt"), "utf8"));
}

let engine: Promise<KwsEngine> | null = null;
/** One engine for a test file (compiling three sessions takes a second). */
export function realEngine(): Promise<KwsEngine> {
  if (!engine) {
    (ort.env.wasm as { numThreads: number }).numThreads = 1;
    (ort.env.wasm as { wasmPaths: string }).wasmPaths = "file://" + resolve(HUD, "node_modules/onnxruntime-web/dist") + "/";
    engine = KwsEngine.create(ort as unknown as WakeOrt, modelBytes());
  }
  return engine;
}

/** 16-bit mono PCM WAV (44-byte header, as the fixtures are) -> floats in [-1, 1]. */
export function wav(path: string): Float32Array {
  const b = readFileSync(path);
  const pcm = new Int16Array(b.buffer, b.byteOffset + 44, (b.length - 44) >> 1);
  return Float32Array.from(pcm, (v) => v / 32768);
}
export const fixture = (name: string) => wav(resolve(FIXTURES, name));

/** Feed `audio` (plus `tailMs` of silence) in `chunk`-sample pieces; every hit with the audio time it came at. */
export async function hitsFor(
  audio: Float32Array,
  set: "wake" | "commands",
  threshold = 0.25,
  chunk = 1600,
  tailMs = 800,
): Promise<{ phrase: string; at: number }[]> {
  const eng = await realEngine();
  const stream = eng.newStream(specsFor(set === "wake" ? WAKE_PIECES : COMMAND_PIECES, tokenTable(), threshold));
  const padded = new Float32Array(audio.length + Math.round((tailMs * 16000) / 1000));
  padded.set(audio);
  const out: { phrase: string; at: number }[] = [];
  for (let p = 0; p < padded.length; p += chunk) {
    const hit: Hit | null = await stream.accept(padded.slice(p, p + chunk));
    if (hit) out.push({ phrase: set === "commands" ? (COMMAND_OF[hit.phrase] ?? hit.phrase) : hit.phrase, at: Math.min(p + chunk, padded.length) / 16000 });
  }
  return out;
}
