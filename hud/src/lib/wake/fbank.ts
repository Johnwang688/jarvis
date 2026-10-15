// Log-mel filterbank features, the way the keyword model was trained on them
// (voice rework V11). A port of kaldi-native-fbank as sherpa-onnx configures it
// for this model: 16 kHz, 80 mel bins from 20 Hz to Nyquist - 400 Hz, a 25 ms
// Povey window every 10 ms, DC offset removed, pre-emphasis 0.97, a 512-point
// power spectrum, floor at float epsilon, no dither, `snip_edges = false` (the
// first frame is centred on the first 10 ms, edges are reflected). The samples
// are used in [-1, 1] as they arrive (sherpa's `normalize_samples = true`).
//
// It is online: `accept()` takes any amount of audio and every frame that no
// longer needs audio from the future is ready at once. Pure arithmetic, no
// browser API, so it runs in the worker and under test.

export const SAMPLE_RATE = 16000;
export const FRAME_LEN = 400; // 25 ms
export const FRAME_SHIFT = 160; // 10 ms
export const NFFT = 512;
export const NUM_MEL = 80;
const LOW_HZ = 20;
const HIGH_HZ = SAMPLE_RATE / 2 - 400;
const PREEMPH = 0.97;
const EPS = 1.1920928955078125e-7; // FLT_EPSILON
/** The sample index of the first sample of frame `i` (negative: reflected). */
export const firstSampleOfFrame = (i: number) => i * FRAME_SHIFT + FRAME_SHIFT / 2 - FRAME_LEN / 2;

const mel = (hz: number) => 1127 * Math.log(1 + hz / 700);

interface MelBin { from: number; weights: Float64Array }

function melBanks(): MelBin[] {
  const binWidth = SAMPLE_RATE / NFFT;
  const lo = mel(LOW_HZ);
  const hi = mel(HIGH_HZ);
  const delta = (hi - lo) / (NUM_MEL + 1);
  const out: MelBin[] = [];
  for (let b = 0; b < NUM_MEL; b++) {
    const left = lo + b * delta;
    const center = lo + (b + 1) * delta;
    const right = lo + (b + 2) * delta;
    const w: number[] = [];
    let from = -1;
    for (let i = 0; i < NFFT / 2; i++) {
      const m = mel(binWidth * i);
      let v = 0;
      if (m > left && m < right) v = m <= center ? (m - left) / (center - left) : (right - m) / (right - center);
      if (v > 0 && from < 0) from = i;
      if (from >= 0) w.push(v);
    }
    while (w.length && w[w.length - 1] === 0) w.pop();
    out.push({ from: Math.max(0, from), weights: Float64Array.from(w) });
  }
  return out;
}

const BANKS = melBanks();

// Povey: a Hann window raised to 0.85.
const WINDOW = Float64Array.from({ length: FRAME_LEN }, (_, i) => Math.pow(0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (FRAME_LEN - 1)), 0.85));

const COS = Float64Array.from({ length: NFFT / 2 }, (_, i) => Math.cos((2 * Math.PI * i) / NFFT));
const SIN = Float64Array.from({ length: NFFT / 2 }, (_, i) => -Math.sin((2 * Math.PI * i) / NFFT));
const REV = (() => {
  const bits = Math.log2(NFFT);
  return Uint16Array.from({ length: NFFT }, (_, i) => {
    let r = 0;
    for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b);
    return r;
  });
})();

/** In-place radix-2 FFT of `re`/`im` (length NFFT). */
function fft(re: Float64Array, im: Float64Array) {
  for (let i = 0; i < NFFT; i++) {
    const j = REV[i];
    if (j > i) { const a = re[i]; re[i] = re[j]; re[j] = a; const b = im[i]; im[i] = im[j]; im[j] = b; }
  }
  for (let size = 2; size <= NFFT; size <<= 1) {
    const half = size >> 1;
    const step = NFFT / size;
    for (let start = 0; start < NFFT; start += size) {
      for (let k = 0; k < half; k++) {
        const wr = COS[k * step];
        const wi = SIN[k * step];
        const a = start + k;
        const b = a + half;
        const tr = re[b] * wr - im[b] * wi;
        const ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
      }
    }
  }
}

const re = new Float64Array(NFFT);
const im = new Float64Array(NFFT);
const win = new Float64Array(FRAME_LEN);

/** One frame's 80 log-mel values from FRAME_LEN samples. */
export function fbankFrame(samples: ArrayLike<number>, out: Float32Array = new Float32Array(NUM_MEL)): Float32Array {
  let mean = 0;
  for (let i = 0; i < FRAME_LEN; i++) { win[i] = samples[i]; mean += win[i]; }
  mean /= FRAME_LEN;
  for (let i = 0; i < FRAME_LEN; i++) win[i] -= mean;
  for (let i = FRAME_LEN - 1; i > 0; i--) win[i] -= PREEMPH * win[i - 1];
  win[0] -= PREEMPH * win[0];
  re.fill(0); im.fill(0);
  for (let i = 0; i < FRAME_LEN; i++) re[i] = win[i] * WINDOW[i];
  fft(re, im);
  const power = new Float64Array(NFFT / 2);
  for (let i = 0; i < NFFT / 2; i++) power[i] = re[i] * re[i] + im[i] * im[i];
  for (let b = 0; b < NUM_MEL; b++) {
    const { from, weights } = BANKS[b];
    let acc = 0;
    for (let k = 0; k < weights.length; k++) acc += weights[k] * power[from + k];
    out[b] = Math.log(Math.max(acc, EPS));
  }
  return out;
}

/** Streaming features: audio in, frames out. Frame indices are absolute from the first sample ever accepted. */
export class OnlineFbank {
  private samples = new Float32Array(0); // from sample index `base`
  private base = 0;
  private total = 0;
  private computed = 0; // frames computed so far (absolute count)
  private frames: Float32Array[] = []; // frames [firstKept, computed)
  private firstKept = 0;
  private scratch = new Float32Array(FRAME_LEN);

  /** Frames available, counted from the first sample ever accepted. */
  get ready() { return this.computed; }

  accept(chunk: Float32Array) {
    if (!chunk.length) return;
    const merged = new Float32Array(this.samples.length + chunk.length);
    merged.set(this.samples, 0);
    merged.set(chunk, this.samples.length);
    this.samples = merged;
    this.total += chunk.length;
    // Frame i needs samples up to firstSampleOfFrame(i) + FRAME_LEN; none beyond the end are invented.
    while (firstSampleOfFrame(this.computed) + FRAME_LEN <= this.total) {
      const start = firstSampleOfFrame(this.computed);
      for (let k = 0; k < FRAME_LEN; k++) {
        let s = start + k;
        if (s < 0) s = -s - 1; // kaldi reflects the head
        this.scratch[k] = this.samples[s - this.base];
      }
      this.frames.push(fbankFrame(this.scratch));
      this.computed++;
    }
    // Samples before the next frame's start are never read again.
    const keepFrom = Math.max(0, firstSampleOfFrame(this.computed));
    if (keepFrom > this.base) {
      this.samples = this.samples.slice(keepFrom - this.base);
      this.base = keepFrom;
    }
  }

  /** `n` frames from absolute frame `from`, row-major [n, 80]. Throws if not all are ready or already dropped. */
  take(from: number, n: number): Float32Array {
    if (from < this.firstKept || from + n > this.computed) throw new Error("frames unavailable");
    const out = new Float32Array(n * NUM_MEL);
    for (let i = 0; i < n; i++) out.set(this.frames[from - this.firstKept + i], i * NUM_MEL);
    return out;
  }

  /** Forget frames before `upTo` (absolute). */
  discard(upTo: number) {
    const drop = Math.min(upTo, this.computed) - this.firstKept;
    if (drop > 0) { this.frames.splice(0, drop); this.firstKept += drop; }
  }
}
