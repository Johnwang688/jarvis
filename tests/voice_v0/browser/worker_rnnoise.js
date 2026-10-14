// RNNoise (Jitsi's WASM build, rnnoise 0.2 sync variant) in a module worker.
// RNNoise runs on 48 kHz, 480-sample (10 ms) frames, samples scaled to int16 range.
import createRNNWasmModuleSync from '/rnnoise/rnnoise-sync.js';

let m, state, inPtr, idx, busy = 0, frames = 0, frameMs = [];
const F = 480;
function b64(f32) {
  const u8 = new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength);
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}


async function init() {
  const t0 = performance.now();
  m = await createRNNWasmModuleSync();
  inPtr = m._malloc(F * 4);
  idx = inPtr >> 2;
  state = m._rnnoise_create();
  return { load_ms: performance.now() - t0 };
}

function frame(x, outArr, outOff, vadOut) {
  const t0 = performance.now();
  for (let i = 0; i < F; i++) m.HEAPF32[idx + i] = x[i] * 32768;
  const vad = m._rnnoise_process_frame(state, inPtr, inPtr);
  if (outArr) for (let i = 0; i < F; i++) outArr[outOff + i] = m.HEAPF32[idx + i] / 32768;
  if (vadOut) vadOut.push(vad);
  const dt = performance.now() - t0;
  busy += dt; frames++; frameMs.push(dt);
}

self.onmessage = (e) => {
  const d = e.data;
  try {
    if (d.type === 'init') init(d.spec).then((r) => self.postMessage({ id: d.id, ...r }), (er) => self.postMessage({ id: d.id, error: String(er.stack || er) }));
    else if (d.type === 'frame') frame(d.samples);
    else if (d.type === 'flush') self.postMessage({ id: d.id, frames, busy_ms: busy, frame_ms: Array.from(frameMs) });
    else if (d.type === 'burst') {
      const s = d.samples; const n = Math.floor(s.length / F);
      const out = new Float32Array(n * F); const vad = [];
      const t0 = performance.now();
      for (let i = 0; i < n; i++) frame(s.subarray(i * F, (i + 1) * F), out, i * F, vad);
      self.postMessage({ id: d.id, frames, busy_ms: busy, wall_ms: performance.now() - t0, audio_s: (n * F) / 48000,
                         frame_ms: Array.from(frameMs), out_b64: b64(out), vad_b64: b64(Float32Array.from(vad)) });
    }
  } catch (err) { self.postMessage({ id: d.id, error: String(err.stack || err) }); }
};
