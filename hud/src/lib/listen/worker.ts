// The Silero VAD worker (voice rework V5, plan §3.2). A same-origin module
// worker Vite emits under /assets/ (the CSP's `worker-src 'self'`), running
// onnxruntime-web's WASM build single-threaded: the HUD is not cross-origin
// isolated, so there are no WASM threads, and one is what V0 measured
// (0.6 ms per 32 ms frame). The model and the .wasm are Vite `?url` imports,
// hashed into dist/assets and served by the daemon; nothing is fetched from
// anywhere else, and the model is checked against its pinned SHA-256 before a
// session is built (runner.ts).
//
// It speaks only the protocol in protocol.ts: `init` -> `ready` | `failed`;
// `frame` -> `prob` (in order); `reset`. It holds no audio past the 64-sample
// context Silero needs, posts none back, and fails with a code, never a message.

import * as ort from "onnxruntime-web/wasm";
import wasmUrl from "onnxruntime-web/ort-wasm-simd-threaded.wasm?url";
import modelUrl from "../../../vendor/silero-vad/silero_vad_v5.1.2.onnx?url";

import { FailError } from "./fail";
import type { FromWorker, ToWorker } from "./protocol";
import { SileroRunner, verifiedModel, type OrtLike } from "./runner";

declare const self: DedicatedWorkerGlobalScope;

const post = (m: FromWorker) => self.postMessage(m);

let runner: SileroRunner | null = null;
let chain: Promise<void> = Promise.resolve();

async function init() {
  const t0 = performance.now();
  try {
    ort.env.wasm.numThreads = 1;
    ort.env.wasm.proxy = false;
    ort.env.wasm.wasmPaths = { wasm: wasmUrl };
    const model = await verifiedModel(async () => {
      const r = await fetch(modelUrl, { credentials: "same-origin", cache: "force-cache" });
      if (!r.ok) throw new Error("status");
      return new Uint8Array(await r.arrayBuffer());
    });
    runner = await SileroRunner.create(ort as unknown as OrtLike, model);
    post({ type: "ready", loadMs: performance.now() - t0 });
  } catch (e) {
    post({ type: "failed", code: e instanceof FailError ? e.code : "runtime" });
  }
}

self.onmessage = (e: MessageEvent<ToWorker>) => {
  const m = e.data;
  if (!m || typeof m !== "object") return;
  if (m.type === "init") {
    chain = chain.then(init);
  } else if (m.type === "reset") {
    chain = chain.then(() => { runner?.reset(); });
  } else if (m.type === "frame") {
    const received = performance.now();
    chain = chain.then(async () => {
      if (!runner) return;
      const t0 = performance.now();
      try {
        const p = await runner.run(m.samples);
        const done = performance.now();
        post({ type: "prob", seq: m.seq, p, ms: done - t0, total: done - received });
      } catch {
        post({ type: "failed", code: "runtime" });
      }
    });
  }
};
