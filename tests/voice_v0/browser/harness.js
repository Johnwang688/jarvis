// Main-thread driver for the V0 browser probes. Playwright calls window.runJob(spec).
//
// spec = { kind: 'silero'|'rnnoise'|'kws'|'null', mode: 'burst'|'paced',
//          audio: URL of raw float32 PCM, rate: 16000|48000, seconds, frame, model?, ... }
//
// burst: the worker chews through the audio as fast as it can and reports
//        per-frame busy time (performance.now inside the worker).
// paced: the main thread posts one frame per real-time frame interval, exactly
//        as an AudioWorklet port would, for `seconds` of wall time. Python
//        samples browser process CPU time around this call (CDP), so the cost
//        includes message passing, structured clones and GC.
const WORKERS = {
  silero: ['/harness/worker_vad.js', 'module'],
  rnnoise: ['/harness/worker_rnnoise.js', 'module'],
  kws: ['/harness/worker_kws.js', 'classic'],
  null: ['/harness/worker_null.js', 'module'],
};

function rpc(worker, msg, transfer = []) {
  return new Promise((resolve, reject) => {
    const id = Math.random().toString(36).slice(2);
    const on = (e) => {
      if (e.data && e.data.id === id) {
        worker.removeEventListener('message', on);
        e.data.error ? reject(new Error(e.data.error)) : resolve(e.data);
      }
    };
    worker.addEventListener('message', on);
    worker.postMessage({ ...msg, id }, transfer);
  });
}

async function loadF32(url) {
  const buf = await (await fetch(url)).arrayBuffer();
  return new Float32Array(buf);
}

const jobs = new Map();

// Phase 1: create the worker, fetch the audio, load/compile the model.
window.prepare = async function (spec) {
  const [url, type] = WORKERS[spec.kind];
  const worker = new Worker(url, type === 'module' ? { type: 'module' } : undefined);
  const errors = [];
  worker.addEventListener('error', (e) => errors.push(String(e.message || e)));
  const audio = await loadF32(spec.audio);
  const init = await rpc(worker, { type: 'init', spec });
  const id = jobs.size + 1;
  jobs.set(id, { spec, worker, audio, init, errors });
  return { id, init };
};

// Phase 2: run it. Python samples process CPU around this call.
window.run = async function (id) {
  const { spec, worker, audio, init, errors } = jobs.get(id);
  const out = { spec, init, errors };
  if (spec.mode === 'burst') {
    const copy = audio.slice();
    out.result = await rpc(worker, { type: 'burst', samples: copy, spec }, [copy.buffer]);
  } else {
    const frame = spec.frame;
    const intervalMs = (frame / spec.rate) * 1000;
    const total = Math.floor((spec.seconds * 1000) / intervalMs);
    const t0 = performance.now();
    let late = 0;
    let maxLate = 0;
    for (let i = 0; i < total; i++) {
      const due = t0 + i * intervalMs;
      const wait = due - performance.now();
      if (wait > 2) await new Promise((r) => setTimeout(r, wait - 1));
      const lateBy = performance.now() - due;
      if (lateBy > intervalMs) late++;
      maxLate = Math.max(maxLate, lateBy);
      const off = (i * frame) % (audio.length - frame);
      const f = audio.slice(off, off + frame);
      worker.postMessage({ type: 'frame', samples: f }, [f.buffer]);
    }
    out.cadence = { frames: total, interval_ms: intervalMs, late_frames: late, max_late_ms: maxLate,
                    wall_ms: performance.now() - t0 };
    out.result = await rpc(worker, { type: 'flush' });
  }
  worker.terminate();
  jobs.delete(id);
  return out;
};
window.harnessReady = true;
