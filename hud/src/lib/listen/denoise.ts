// Noise suppression in front of the VAD (voice rework V14, plan §3.4): RNNoise
// behind V5's `denoise` seam, run inside the VAD worker.
//
//   16 kHz mic frames (512) ──> Denoiser16k ──> 16 kHz frames the VAD scores
//                               │ 160-sample blocks (10 ms)
//                               │ ×3 up (polyphase low-pass)  -> 480 @ 48 kHz
//                               │ RNNoise, in place
//                               └ ÷3 down (low-pass, decimate) -> 160 @ 16 kHz
//
// **It changes only what the VAD scores.** The stage hands the worker a copy of
// each frame and delivers the raw frame on to the capture untouched, so what is
// recorded and uploaded is exactly what the microphone gave (STT takes the
// echo-cancelled stream, not a denoised one: V0 section 2 measured RNNoise
// doubling WER at 0 dB pink noise).
//
// **Latency** (fixed, measured in denoise.test.ts on Kokoro speech): an input
// sample reaches the output about 32 ms later, 511 samples, one VAD frame. Most
// of it is RNNoise 0.2 itself (~20 ms: its output lags its input by two 10 ms
// frames), the adapter holds back one 10 ms block so a 512-sample frame can
// always be filled (512 is not a multiple of 160), and the two 95-tap filters
// add 94 samples at 48 kHz (~2 ms). The VAD therefore scores audio that is one
// frame older than the frame it labels. The capture absorbs that: it needs 60 ms
// of speech to open, and its 800 ms pre-roll reaches back far past the lag, so
// no start of speech is lost; the message-end timer starts ~32 ms later. The
// output always has exactly as many samples as the input, so the worker's
// one-answer-per-frame protocol is unchanged.
//
// **Fails soft.** `Suppressor` owns the lifecycle: load on demand, never on
// start-up (a switch that is off fetches and compiles nothing), ready, off, or
// failed with a code. A failure, or a run of frames too slow to be real-time,
// turns suppression off and the frames are scored raw from then on; the owner
// of the Suppressor is told, so the window can say so.

import { DenoiseFailError } from "./fail";
import { RNNOISE_FRAME, RNNOISE_FRAME_16K } from "./pins";
import type { DenoiseEvent, DenoiseFailCode } from "./protocol";
import type { RnnoiseEngine } from "./rnnoise";

const RATIO = 3;
const TAPS = 95; // odd: linear phase, an integer delay of (TAPS - 1) / 2 samples at 48 kHz
const PHASE_TAPS = Math.ceil(TAPS / RATIO); // 32
/** Pass to ~7 kHz, stop from ~8.7 kHz (60+ dB): the 16 kHz Nyquist is 8 kHz. */
const CUTOFF = 7800 / 48000;

function bessel0(x: number): number {
  let sum = 1, term = 1;
  for (let k = 1; k < 40; k++) { term *= (x / (2 * k)) ** 2; sum += term; }
  return sum;
}

/** Kaiser-windowed sinc low-pass, unity DC gain. */
export function lowpass(taps = TAPS, cutoff = CUTOFF, beta = 6.5): Float32Array {
  const h = new Float32Array(taps);
  const mid = (taps - 1) / 2;
  let sum = 0;
  for (let j = 0; j < taps; j++) {
    const t = j - mid;
    const sinc = t === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * t) / (Math.PI * t);
    const r = t / mid;
    const w = bessel0(beta * Math.sqrt(Math.max(0, 1 - r * r))) / bessel0(beta);
    h[j] = sinc * w;
    sum += h[j];
  }
  for (let j = 0; j < taps; j++) h[j] /= sum;
  return h;
}

const H = lowpass();
/** H split into the three polyphase branches used to interpolate by 3. */
const PHASES: Float32Array[] = [0, 1, 2].map((p) => {
  const taps: number[] = [];
  for (let j = p; j < TAPS; j += RATIO) taps.push(H[j]);
  return Float32Array.from(taps);
});

/** The samples of delay the adapter adds, in 16 kHz samples (one block). */
export const ADAPTER_DELAY = RNNOISE_FRAME_16K;

export interface Frame48 {
  /** Denoise 480 samples at 48 kHz in place. */
  frame(io: Float32Array): unknown;
  reset?(): void;
}

export class Denoiser16k {
  private xhist = new Float32Array(PHASE_TAPS - 1);
  private yhist = new Float32Array(TAPS - 1);
  private inBuf = new Float32Array(1024);
  private inLen = 0;
  private outBuf = new Float32Array(1024);
  private outLen = 0;
  private up = new Float32Array(RNNOISE_FRAME);
  private ubuf = new Float32Array(PHASE_TAPS - 1 + RNNOISE_FRAME_16K);
  private dbuf = new Float32Array(TAPS - 1 + RNNOISE_FRAME);

  constructor(private engine: Frame48) {
    this.outLen = ADAPTER_DELAY; // zeros: the held-back block
  }

  /** Forget the audio so far. The filters, the FIFOs and the engine's own state start over. */
  reset() {
    this.xhist.fill(0); this.yhist.fill(0);
    this.inLen = 0;
    this.outBuf.fill(0); this.outLen = ADAPTER_DELAY;
    this.engine.reset?.();
  }

  /** Any number of 16 kHz samples in; the same number out, delayed by `latencySamples`. */
  process(x: Float32Array): Float32Array {
    this.grow(x.length);
    this.inBuf.set(x, this.inLen);
    this.inLen += x.length;
    let read = 0;
    while (this.inLen - read >= RNNOISE_FRAME_16K) {
      this.upsample(this.inBuf.subarray(read, read + RNNOISE_FRAME_16K));
      this.engine.frame(this.up);
      this.downsample();
      read += RNNOISE_FRAME_16K;
    }
    this.inBuf.copyWithin(0, read, this.inLen);
    this.inLen -= read;

    const out = new Float32Array(x.length);
    const n = Math.min(x.length, this.outLen); // outLen >= x.length always; min is a guard, the rest stays 0
    out.set(this.outBuf.subarray(0, n));
    this.outBuf.copyWithin(0, n, this.outLen);
    this.outLen -= n;
    return out;
  }

  private grow(n: number) {
    if (this.inLen + n > this.inBuf.length) {
      const b = new Float32Array((this.inLen + n) * 2); b.set(this.inBuf.subarray(0, this.inLen)); this.inBuf = b;
    }
    const need = this.outLen + n + RNNOISE_FRAME_16K;
    if (need > this.outBuf.length) {
      const b = new Float32Array(need * 2); b.set(this.outBuf.subarray(0, this.outLen)); this.outBuf = b;
    }
  }

  private upsample(block: Float32Array) {
    const hist = PHASE_TAPS - 1;
    const buf = this.ubuf;
    buf.set(this.xhist, 0);
    buf.set(block, hist);
    for (let i = 0; i < RNNOISE_FRAME_16K; i++) {
      for (let p = 0; p < RATIO; p++) {
        const taps = PHASES[p];
        let acc = 0;
        for (let k = 0; k < taps.length; k++) acc += taps[k] * buf[hist + i - k];
        this.up[RATIO * i + p] = RATIO * acc; // zero-stuffing removed 2/3 of the energy: ×3 puts it back
      }
    }
    this.xhist.set(buf.subarray(RNNOISE_FRAME_16K, RNNOISE_FRAME_16K + hist));
  }

  private downsample() {
    const hist = TAPS - 1;
    const buf = this.dbuf;
    buf.set(this.yhist, 0);
    buf.set(this.up, hist);
    for (let i = 0; i < RNNOISE_FRAME_16K; i++) {
      const n = hist + RATIO * i;
      let acc = 0;
      for (let j = 0; j < TAPS; j++) acc += H[j] * buf[n - j];
      this.outBuf[this.outLen++] = acc;
    }
    this.yhist.set(buf.subarray(RNNOISE_FRAME, RNNOISE_FRAME + hist));
  }
}

// ---------------------------------------------------------------------------

/** A frame this slow, this many times in a row, and suppression is turned off (the budget is 32 ms per frame). */
export const SLOW_FRAME_MS = 24;
export const SLOW_RUN = 16;

export type SuppressorState = "off" | "loading" | "ready" | "failed";

export interface SuppressorOptions {
  /** Fetch, verify and start the engine. Rejects with a DenoiseFailError. */
  load: () => Promise<RnnoiseEngine>;
  now?: () => number;
  notify?: (event: DenoiseEvent) => void;
}

export class Suppressor {
  state: SuppressorState = "off";
  code: DenoiseFailCode | null = null;
  private denoiser: Denoiser16k | null = null;
  private engine: RnnoiseEngine | null = null;
  private gen = 0;
  private slowRun = 0;

  constructor(private o: SuppressorOptions) {}

  private now() { return (this.o.now ?? (() => performance.now()))(); }
  private emit(e: DenoiseEvent) { try { this.o.notify?.(e); } catch { /* an observer cannot change scoring */ } }

  /** Start loading, from off or failed. A no-op while loading or ready. */
  enable() {
    if (this.state === "loading" || this.state === "ready") return;
    const gen = ++this.gen;
    const t0 = this.now();
    this.state = "loading";
    this.code = null;
    this.slowRun = 0;
    this.emit({ state: "loading" });
    this.o.load().then((engine) => {
      if (gen !== this.gen) { engine.dispose(); return; } // switched off (or restarted) meanwhile
      this.engine = engine;
      this.denoiser = new Denoiser16k(engine);
      this.state = "ready";
      this.emit({ state: "ready", loadMs: Math.max(0, this.now() - t0) });
    }, (e: unknown) => {
      if (gen !== this.gen) return;
      this.fail(e instanceof DenoiseFailError ? e.code : "runtime");
    });
  }

  /** Stop and release the engine. */
  disable() {
    this.gen++;
    this.release();
    if (this.state !== "off") { this.state = "off"; this.code = null; this.emit({ state: "off" }); }
  }

  /** The mic closed or reopened: the next audio is not a continuation of the last. */
  reset() { try { this.denoiser?.reset(); } catch { this.fail("runtime"); } }

  private release() {
    const e = this.engine;
    this.engine = null; this.denoiser = null;
    try { e?.dispose(); } catch { /* gone */ }
  }

  private fail(code: DenoiseFailCode) {
    this.gen++;
    this.release();
    this.state = "failed";
    this.code = code;
    this.emit({ state: "failed", code });
  }

  /**
   * The samples the VAD should score: the denoised frame while ready, else the
   * frame itself (never anything from the past: a failure is raw from then on).
   * `ms` is the time suppression took on this frame, 0 when it is not running.
   */
  process(frame: Float32Array): { samples: Float32Array; ms: number } {
    if (this.state !== "ready" || !this.denoiser) return { samples: frame, ms: 0 };
    const t0 = this.now();
    let samples: Float32Array;
    try { samples = this.denoiser.process(frame); } catch { this.fail("runtime"); return { samples: frame, ms: 0 }; }
    const ms = Math.max(0, this.now() - t0);
    if (ms > SLOW_FRAME_MS) {
      if (++this.slowRun >= SLOW_RUN) { this.fail("slow"); return { samples, ms }; }
    } else {
      this.slowRun = 0;
    }
    return { samples, ms };
  }
}
