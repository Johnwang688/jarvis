// The wake-word worker (voice rework V11, plan §3.6). A same-origin module worker
// Vite emits under /assets/ (the CSP's `worker-src 'self'`), running
// onnxruntime-web's WASM build single-threaded, as the Silero worker does. The model
// files and the .wasm are Vite `?url` imports served by the daemon from the HUD's own
// origin: nothing is fetched from anywhere else, and every file is checked against
// its pinned length and SHA-256 before a session is built (engine.ts).
//
// It listens to one keyword set at a time and says only `hit`: it posts no audio
// back, keeps none past the few hundred milliseconds the network needs, makes no
// network request of its own beyond fetching those files, and fails with a code.

import * as ort from "onnxruntime-web/wasm";
import wasmUrl from "onnxruntime-web/ort-wasm-simd-threaded.wasm?url";
import encoderUrl from "../../../vendor/kws-zipformer-gigaspeech-3.3M/encoder.int8.onnx?url";
import decoderUrl from "../../../vendor/kws-zipformer-gigaspeech-3.3M/decoder.int8.onnx?url";
import joinerUrl from "../../../vendor/kws-zipformer-gigaspeech-3.3M/joiner.int8.onnx?url";
import tokensUrl from "../../../vendor/kws-zipformer-gigaspeech-3.3M/tokens.txt?url";

import { FailError } from "../listen/fail";
import { KwsEngine, verifiedFiles, type WakeOrt, type WakeStream } from "./engine";
import { COMMAND_OF, COMMAND_PIECES, parseTokens, specsFor, WAKE_PIECES } from "./keywords";
import type { WakeFromWorker, WakeToWorker } from "./protocol";

declare const self: DedicatedWorkerGlobalScope;

const post = (m: WakeFromWorker) => self.postMessage(m);
const URLS = { encoder: encoderUrl, decoder: decoderUrl, joiner: joinerUrl, tokens: tokensUrl };

let engine: KwsEngine | null = null;
let tokens: Map<string, number> | null = null;
let stream: WakeStream | null = null;
let set: "wake" | "commands" = "wake";
let threshold = 0.25;
let busyMs = 0;
let consumed = 0; // samples taken in, ever (the stage's backlog check counts against it)
let reported = 0;
let chain: Promise<void> = Promise.resolve();

function rebuild() {
  if (!engine || !tokens) return;
  stream = engine.newStream(specsFor(set === "wake" ? WAKE_PIECES : COMMAND_PIECES, tokens, threshold));
}

async function init() {
  const t0 = performance.now();
  try {
    ort.env.wasm.numThreads = 1;
    ort.env.wasm.proxy = false;
    ort.env.wasm.wasmPaths = { wasm: wasmUrl };
    const files = await verifiedFiles(async (file) => {
      const r = await fetch(URLS[file], { credentials: "same-origin", cache: "force-cache" });
      if (!r.ok) throw new Error("status");
      return new Uint8Array(await r.arrayBuffer());
    });
    tokens = parseTokens(new TextDecoder().decode(files.tokens));
    engine = await KwsEngine.create(ort as unknown as WakeOrt, files);
    rebuild();
    post({ type: "ready", loadMs: performance.now() - t0 });
  } catch (e) {
    post({ type: "failed", code: e instanceof FailError ? e.code : "runtime" });
  }
}

self.onmessage = (e: MessageEvent<WakeToWorker>) => {
  const m = e.data;
  if (!m || typeof m !== "object") return;
  if (m.type === "init") {
    chain = chain.then(init);
  } else if (m.type === "config") {
    chain = chain.then(() => {
      if (m.set !== set || m.threshold !== threshold || !stream) {
        set = m.set === "commands" ? "commands" : "wake";
        threshold = Number.isFinite(m.threshold) ? Math.min(0.9, Math.max(0.05, m.threshold)) : 0.25;
        rebuild();
      }
    });
  } else if (m.type === "reset") {
    chain = chain.then(() => { stream?.reset(); });
  } else if (m.type === "audio") {
    chain = chain.then(async () => {
      if (!stream) return;
      const t0 = performance.now();
      try {
        const hit = await stream.accept(m.samples);
        busyMs += performance.now() - t0;
        consumed += m.samples.length;
        if (hit) post({ type: "hit", set, phrase: set === "commands" ? (COMMAND_OF[hit.phrase] ?? "") : hit.phrase });
        if (consumed - reported >= 4096) { reported = consumed; post({ type: "progress", samples: consumed, busyMs }); }
      } catch {
        post({ type: "failed", code: "runtime" });
      }
    });
  }
};
