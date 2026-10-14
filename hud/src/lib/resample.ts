// Streaming sample-rate conversion and framing for the microphone (voice rework
// V4, plan §3.1). Runs unchanged in the AudioWorklet and on the main thread (the
// ScriptProcessor fallback), and in tests: no DOM, no Web Audio.
//
// The old path averaged each run of input samples (a boxcar), which has almost
// no stop band: anything the browser's microphone chain left above 8 kHz folded
// straight back into the speech band at 16 kHz. This is a windowed-sinc
// low-pass at 90% of the output Nyquist with a Kaiser window, so it is the same
// operation whatever the device rate (48 k, 44.1 k, 16 k, 8 k).

export const OUT_RATE = 16000;
/** The AudioWorkletProcessor's registered name (micWorklet.ts registers it, micInput.ts constructs it). */
export const MIC_PROCESSOR = "jarvis-mic";
/** Samples per frame handed to the capture (plan §3.1: 512 at 16 kHz, 32 ms). */
export const FRAME_SAMPLES = 512;

const ZEROS = 10; // side lobes per side, in output-sample units
const CUT = 0.9; // of the output Nyquist
const BETA = 8.6; // Kaiser: about 80 dB of stop band
const PER_SAMPLE = 256; // table entries per input sample

function bessel0(x: number): number {
  let sum = 1, term = 1;
  for (let k = 1; k < 40; k++) {
    term *= (x / (2 * k)) * (x / (2 * k));
    sum += term;
    if (term < 1e-12 * sum) break;
  }
  return sum;
}

/** A band-limited resampler for a stream fed in arbitrary blocks. */
export class Resampler {
  private scale: number;       // input samples per output sample, at least 1 in the kernel
  private ratio: number;       // input samples per output sample
  private half: number;        // kernel half-width in input samples
  private table: Float32Array; // kernel, PER_SAMPLE entries per input sample
  private hist: Float32Array;    // input not yet fully used, preceded by the kernel's silent history
  private histStart: number;     // absolute input index of hist[0] (negative at first: the silence)
  private produced = 0;          // output samples made so far

  constructor(readonly inRate: number, readonly outRate: number = OUT_RATE) {
    if (!(inRate > 0) || !(outRate > 0)) throw new RangeError("sample rates must be positive");
    this.ratio = inRate / outRate;
    this.scale = Math.max(1, this.ratio);
    this.half = Math.ceil(ZEROS * this.scale);
    const n = this.half * PER_SAMPLE;
    this.table = new Float32Array(n + 2);
    const norm = bessel0(BETA);
    for (let i = 0; i <= n; i++) {
      const u = i / PER_SAMPLE;                 // input samples from the centre
      const v = (u / this.scale) * CUT;
      const sinc = v === 0 ? 1 : Math.sin(Math.PI * v) / (Math.PI * v);
      const r = u / this.half;
      const win = r >= 1 ? 0 : bessel0(BETA * Math.sqrt(1 - r * r)) / norm;
      this.table[i] = (CUT / this.scale) * sinc * win;
    }
    this.hist = new Float32Array(this.half); // silence before the first sample
    this.histStart = -this.half;
  }

  /** Output samples are delayed by this many input samples. */
  get lookahead() { return this.half; }

  reset() {
    this.hist = new Float32Array(this.half);
    this.histStart = -this.half;
    this.produced = 0;
  }

  private tap(d: number): number {
    const x = Math.abs(d) * PER_SAMPLE;
    const i = Math.floor(x);
    const f = x - i;
    return this.table[i] + (this.table[i + 1] - this.table[i]) * f;
  }

  process(input: Float32Array): Float32Array {
    if (this.inRate === this.outRate) return Float32Array.from(input);
    const work = new Float32Array(this.hist.length + input.length);
    work.set(this.hist);
    work.set(input, this.hist.length);
    const base = this.histStart; // absolute input index of work[0]
    const out = new Float32Array(Math.ceil(input.length / this.ratio) + 2);
    let k = 0;
    for (;;) {
      const t = this.produced * this.ratio;      // centre, in input samples from the start
      const c = Math.floor(t) - base;            // index of the centre sample in `work`
      if (c + this.half >= work.length) break;   // not enough lookahead yet
      let acc = 0;
      for (let m = -this.half + 1; m <= this.half; m++) {
        const j = c + m;
        if (j < 0) continue;
        acc += work[j] * this.tap(t - (j + base));
      }
      out[k++] = acc;
      this.produced++;
    }
    // The next output needs nothing earlier than its centre minus the kernel.
    const need = Math.floor(this.produced * this.ratio) - base - this.half + 1;
    const cut = Math.max(0, Math.min(work.length, need));
    this.hist = work.slice(cut);
    this.histStart += cut;
    return out.subarray(0, k);
  }
}

/**
 * The AudioWorklet's logic, with no Web Audio in it: render quanta in
 * (typically 128 samples at the context rate), 512-sample frames at 16 kHz out.
 * Channels are averaged to mono; an input with no channel (a disconnected
 * source) counts as silence so the sample clock never stops.
 */
export class MicFramer {
  private resampler: Resampler;
  private pending = new Float32Array(FRAME_SAMPLES * 4);
  private fill = 0;
  /** 16 kHz samples emitted so far: the capture's own clock, for diagnostics. */
  emitted = 0;

  constructor(inRate: number, private emit: (frame: Float32Array) => void, readonly frame = FRAME_SAMPLES) {
    this.resampler = new Resampler(inRate, OUT_RATE);
  }

  /** `channels` is one render quantum: `inputs[0]` in an AudioWorkletProcessor. */
  push(channels: ArrayLike<Float32Array> | undefined, quantum = 128) {
    let mono: Float32Array;
    if (!channels || channels.length === 0 || !channels[0] || channels[0].length === 0) {
      mono = new Float32Array(quantum);
    } else if (channels.length === 1) {
      mono = channels[0];
    } else {
      mono = new Float32Array(channels[0].length);
      for (let c = 0; c < channels.length; c++) for (let i = 0; i < mono.length; i++) mono[i] += channels[c][i] / channels.length;
    }
    this.pushMono(mono);
  }

  pushMono(mono: Float32Array) {
    const out = this.resampler.process(mono);
    if (this.fill + out.length > this.pending.length) {
      const grown = new Float32Array(Math.max(this.pending.length * 2, this.fill + out.length));
      grown.set(this.pending.subarray(0, this.fill));
      this.pending = grown;
    }
    this.pending.set(out, this.fill);
    this.fill += out.length;
    let at = 0;
    while (this.fill - at >= this.frame) {
      this.emit(this.pending.slice(at, at + this.frame));
      this.emitted += this.frame;
      at += this.frame;
    }
    if (at) {
      this.pending.copyWithin(0, at, this.fill);
      this.fill -= at;
    }
  }
}
