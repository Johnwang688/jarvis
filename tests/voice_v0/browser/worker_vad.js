// Silero VAD (ONNX, onnxruntime-web WASM, single thread) in a module worker.
import * as ort from '/ort/ort.wasm.min.mjs';

ort.env.wasm.wasmPaths = '/ort/';
ort.env.wasm.numThreads = 1;
ort.env.wasm.proxy = false;

let session, state, ctx, srT, busy = 0, frames = 0, probs = [], frameMs = [];
const CTX = 64, N = 512;
const sr = new BigInt64Array([16000n]);

async function init(spec) {
  const buf = await (await fetch(spec.model)).arrayBuffer();
  const t0 = performance.now();
  session = await ort.InferenceSession.create(new Uint8Array(buf), {
    executionProviders: ['wasm'], graphOptimizationLevel: 'all', intraOpNumThreads: 1,
  });
  state = new Float32Array(2 * 1 * 128);
  ctx = new Float32Array(CTX);
  srT = new ort.Tensor('int64', sr, []);
  return { load_ms: performance.now() - t0, inputs: session.inputNames, outputs: session.outputNames,
           model_bytes: buf.byteLength, ort: ort.env.versions, simd: ort.env.wasm.simd, threads: ort.env.wasm.numThreads };
}

async function frame(x) {
  const t0 = performance.now();
  const input = new Float32Array(CTX + N);
  input.set(ctx, 0);
  input.set(x, CTX);
  const out = await session.run({
    input: new ort.Tensor('float32', input, [1, CTX + N]),
    state: new ort.Tensor('float32', state, [2, 1, 128]),
    sr: srT,
  });
  state = out.stateN.data.slice();
  ctx = x.slice(N - CTX, N);
  const p = out.output.data[0];
  const dt = performance.now() - t0;
  busy += dt; frames++; frameMs.push(dt); probs.push(p);
  return p;
}

function b64(f32) {
  const u8 = new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength);
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}

let chain = Promise.resolve();
self.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'init') {
    init(m.spec).then((r) => self.postMessage({ id: m.id, ...r }), (err) => self.postMessage({ id: m.id, error: String(err.stack || err) }));
  } else if (m.type === 'frame') {
    chain = chain.then(() => frame(m.samples));
  } else if (m.type === 'flush') {
    chain.then(() => self.postMessage({ id: m.id, frames, busy_ms: busy, frame_ms: Array.from(frameMs) }));
  } else if (m.type === 'burst') {
    (async () => {
      const s = m.samples; const t0 = performance.now();
      const n = Math.floor(s.length / N);
      for (let i = 0; i < n; i++) await frame(s.subarray(i * N, (i + 1) * N).slice());
      const wall = performance.now() - t0;
      self.postMessage({ id: m.id, frames, busy_ms: busy, wall_ms: wall, audio_s: (n * N) / 16000,
                         frame_ms: Array.from(frameMs), probs_b64: b64(Float32Array.from(probs)) });
    })().catch((err) => self.postMessage({ id: m.id, error: String(err.stack || err) }));
  }
};
