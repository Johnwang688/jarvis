// sherpa-onnx's own WASM KWS under Node/V8 (single thread).
//
//   node tests/voice_v0/kws_node_wasm.js <sherpa-onnx pkg dir> <kws model dir> <keywords.txt> <audio.f32> [int8]
//
// Cross-check for probe_browser.py: same Emscripten build, same engine (V8) and
// WASM tier as Chrome, without the browser's page/worker overhead. Prints one
// JSON line: CPU seconds per audio second over a paced-less burst, plus hits.
'use strict';
const fs = require('fs');
const path = require('path');
const [, , pkg, modelDir, keywordsFile, audioFile, int8Flag] = process.argv;
const sherpa = require(path.join(pkg, 'index.js'));
const enc = int8Flag === 'int8' ? '.int8.onnx' : '.onnx';
const base = 'encoder-epoch-12-avg-2-chunk-16-left-64';
const kws = sherpa.createKws({
  featConfig: { sampleRate: 16000, featureDim: 80 },
  modelConfig: {
    transducer: {
      encoder: path.join(modelDir, base + enc),
      decoder: path.join(modelDir, base.replace('encoder', 'decoder') + enc),
      joiner: path.join(modelDir, base.replace('encoder', 'joiner') + enc),
    },
    tokens: path.join(modelDir, 'tokens.txt'), provider: 'cpu', modelType: '', numThreads: 1, debug: 0,
    modelingUnit: 'cjkchar', bpeVocab: '',
  },
  maxActivePaths: 4, numTrailingBlanks: 1, keywordsScore: 1.0, keywordsThreshold: 0.25,
  keywords: fs.readFileSync(keywordsFile, 'utf8'),
});
const buf = fs.readFileSync(audioFile);
const x = new Float32Array(buf.buffer, buf.byteOffset, buf.length / 4);
const F = 512;
const stream = kws.createStream();
const hits = [];
function runPass(limit) {
  const t = process.hrtime.bigint();
  const c = process.cpuUsage();
  const n = Math.min(Math.floor(x.length / F), limit);
  for (let i = 0; i < n; i++) {
    stream.acceptWaveform(16000, x.slice(i * F, (i + 1) * F));
    while (kws.isReady(stream)) {
      kws.decode(stream);
      const r = kws.getResult(stream);
      if (r && r.keyword) { hits.push({ kw: r.keyword, t: ((i + 1) * F) / 16000 }); kws.reset(stream); }
    }
  }
  const cu = process.cpuUsage(c);
  return { audio_s: (n * F) / 16000, wall_s: Number(process.hrtime.bigint() - t) / 1e9, cpu_s: (cu.user + cu.system) / 1e6 };
}
runPass(Math.floor(16000 / F) * 5);        // warm-up: 5 s
const r = runPass(Infinity);
console.log(JSON.stringify({ int8: int8Flag === 'int8', ...r, cpu_per_audio_s: r.cpu_s / r.audio_s,
                             wall_per_audio_s: r.wall_s / r.audio_s, rss_mb: process.memoryUsage().rss / 1e6, hits }));
