// sherpa-onnx KWS in WebAssembly, in a CLASSIC worker.
//
// sherpa-onnx ships its WASM build as an npm package aimed at Node
// (sherpa-onnx-wasm-nodejs.js). The Emscripten runtime inside also supports
// web/worker, so we load it here with a tiny CommonJS shim for the helper
// (sherpa-onnx-kws.js) and an in-memory FS populated from fetch(). If a future
// release ships a browser KWS build this file should switch to it.
importScripts('/sherpa/sherpa-onnx-wasm-nodejs.js');   // defines `Module` factory (async)

let kws, stream, busy = 0, frames = 0, frameMs = [], hits = [];

async function init(spec) {
  const t0 = performance.now();
  const wasm = await Module({ locateFile: (f) => '/sherpa/' + f });
  const FS = wasm.FS;
  const put = async (url, path) => {
    const b = new Uint8Array(await (await fetch(url)).arrayBuffer());
    FS.writeFile(path, b);
    return b.length;
  };
  const enc = spec.int8 ? '.int8.onnx' : '.onnx';
  const base = 'encoder-epoch-12-avg-2-chunk-16-left-64';
  let bytes = 0;
  bytes += await put(spec.modelBase + '/tokens.txt', '/tokens.txt');
  bytes += await put(spec.modelBase + '/' + base + enc, '/encoder.onnx');
  bytes += await put(spec.modelBase + '/' + base.replace('encoder', 'decoder') + enc, '/decoder.onnx');
  bytes += await put(spec.modelBase + '/' + base.replace('encoder', 'joiner') + enc, '/joiner.onnx');
  const keywords = await (await fetch(spec.keywords)).text();
  const src = await (await fetch('/sherpa/sherpa-onnx-kws.js')).text();
  const mod = { exports: {} };
  new Function('module', 'exports', 'process', src)(mod, mod.exports, { versions: { node: '0' } });
  kws = mod.exports.createKws(wasm, {
    featConfig: { sampleRate: 16000, featureDim: 80 },
    modelConfig: {
      transducer: { encoder: '/encoder.onnx', decoder: '/decoder.onnx', joiner: '/joiner.onnx' },
      tokens: '/tokens.txt', provider: 'cpu', modelType: '', numThreads: 1, debug: 0, modelingUnit: 'cjkchar', bpeVocab: '',
    },
    maxActivePaths: 4, numTrailingBlanks: 1, keywordsScore: spec.score || 1.0, keywordsThreshold: spec.threshold || 0.25,
    keywords,
  });
  stream = kws.createStream();
  return { load_ms: performance.now() - t0, model_bytes: bytes, wasm_threads: 1 };
}

function chunk(x, tAudio) {
  const t0 = performance.now();
  stream.acceptWaveform(16000, x);
  while (kws.isReady(stream)) {
    kws.decode(stream);
    const r = kws.getResult(stream);
    if (r && r.keyword) { hits.push({ kw: r.keyword, t: tAudio }); kws.reset(stream); }
  }
  const dt = performance.now() - t0;
  busy += dt; frames++; frameMs.push(dt);
}

let seq = 0;
self.onmessage = (e) => {
  const d = e.data;
  try {
    if (d.type === 'init') init(d.spec).then((r) => self.postMessage({ id: d.id, ...r }), (er) => self.postMessage({ id: d.id, error: String(er.stack || er) }));
    else if (d.type === 'frame') chunk(d.samples, (++seq * d.samples.length) / 16000);
    else if (d.type === 'flush') self.postMessage({ id: d.id, frames, busy_ms: busy, frame_ms: Array.from(frameMs), hits });
    else if (d.type === 'burst') {
      const s = d.samples; const F = d.spec.frame; const n = Math.floor(s.length / F);
      const t0 = performance.now();
      for (let i = 0; i < n; i++) chunk(s.subarray(i * F, (i + 1) * F).slice(), ((i + 1) * F) / 16000);
      self.postMessage({ id: d.id, frames, busy_ms: busy, wall_ms: performance.now() - t0, audio_s: (n * F) / 16000,
                         frame_ms: Array.from(frameMs), hits });
    }
  } catch (err) { self.postMessage({ id: d.id, error: String(err.stack || err) }); }
};
