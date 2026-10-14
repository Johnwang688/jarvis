import { describe, expect, it } from "vitest";
import { FRAME_SAMPLES, MicFramer, Resampler } from "./resample";

const sine = (hz: number, rate: number, n: number, amp = 0.5) =>
  Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / rate));
const rms = (a: Float32Array, from = 0, to = a.length) => {
  let s = 0;
  for (let i = from; i < to; i++) s += a[i] * a[i];
  return Math.sqrt(s / Math.max(1, to - from));
};
const run = (r: Resampler, x: Float32Array, block: number) => {
  const parts: Float32Array[] = [];
  for (let i = 0; i < x.length; i += block) parts.push(r.process(x.subarray(i, i + block)));
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
};
/** What the old path did: average each run of input samples. */
function boxcar(x: Float32Array, ratio: number) {
  const out: number[] = [];
  let acc = 0, n = 0, pos = 0;
  for (const v of x) { acc += v; n++; pos += 1; if (pos >= ratio) { out.push(acc / n); pos -= ratio; acc = 0; n = 0; } }
  return Float32Array.from(out);
}
const db = (a: number, b: number) => 20 * Math.log10(a / b);

describe("Resampler (windowed sinc)", () => {
  it("keeps DC at unity gain", () => {
    const out = run(new Resampler(48000), new Float32Array(48000).fill(0.5), 128);
    expect(rms(out, 400, out.length - 400)).toBeCloseTo(0.5, 3);
  });

  it("passes speech-band tones at their own level, in phase with an ideal 16 kHz tone", () => {
    for (const rate of [48000, 44100, 32000, 16000, 8000]) {
      const n = rate;
      const out = run(new Resampler(rate), sine(1000, rate, n), 128);
      expect(rms(out, 300, out.length - 300), `rate ${rate}`).toBeCloseTo(0.5 / Math.SQRT2, 2);
      // zero group delay: sample for sample against the tone sampled at 16 kHz
      let worst = 0;
      for (let i = 400; i < out.length - 400; i++) worst = Math.max(worst, Math.abs(out[i] - 0.5 * Math.sin((2 * Math.PI * 1000 * i) / 16000)));
      expect(worst, `rate ${rate}`).toBeLessThan(0.01);
    }
  });

  it("removes what would alias: a 12 kHz tone is 60 dB down, where the old boxcar let it through", () => {
    const x = sine(12000, 48000, 48000);
    const good = run(new Resampler(48000), x, 128);
    const old = boxcar(x, 3);
    const ref = 0.5 / Math.SQRT2;
    expect(db(rms(good, 300, good.length - 300), ref)).toBeLessThan(-60);
    expect(db(rms(old, 300, old.length - 300), ref)).toBeGreaterThan(-30);
  });

  it("is the same stream however it is blocked", () => {
    const x = sine(440, 48000, 9000);
    const one = run(new Resampler(48000), x, x.length);
    for (const block of [1, 7, 128, 1000]) {
      const got = run(new Resampler(48000), x, block);
      expect(got.length).toBe(one.length);
      let worst = 0;
      for (let i = 0; i < got.length; i++) worst = Math.max(worst, Math.abs(got[i] - one[i]));
      expect(worst, `block ${block}`).toBeLessThan(1e-6);
    }
  });

  it("makes about a third of the samples at 48 kHz, short only by its lookahead", () => {
    const r = new Resampler(48000);
    const out = run(r, new Float32Array(48000), 128);
    expect(out.length).toBeGreaterThan(16000 - 20);
    expect(out.length).toBeLessThanOrEqual(16000);
  });

  it("does not drift over ten minutes of input (counted, not accumulated)", () => {
    const r = new Resampler(44100);
    let total = 0;
    const block = new Float32Array(441);
    for (let i = 0; i < 44100 * 600 / 441; i++) total += r.process(block).length;
    expect(Math.abs(total - 16000 * 600)).toBeLessThan(20);
  });

  it("rejects a nonsense rate", () => {
    expect(() => new Resampler(0)).toThrow();
  });
});

describe("MicFramer (the worklet's logic)", () => {
  it("turns 128-sample quanta at 48 kHz into whole 512-sample frames and counts them", () => {
    const frames: Float32Array[] = [];
    const f = new MicFramer(48000, fr => frames.push(fr));
    const tone = sine(500, 48000, 128 * 600);
    for (let i = 0; i < 600; i++) f.push([tone.subarray(i * 128, (i + 1) * 128)]);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.every(fr => fr.length === FRAME_SAMPLES)).toBe(true);
    expect(f.emitted).toBe(frames.length * FRAME_SAMPLES);
    // 600 quanta = 76800 input samples = 25600 output; frames hold all but the tail
    expect(f.emitted).toBeGreaterThan(25600 - FRAME_SAMPLES - 20);
    expect(rms(frames[frames.length - 1])).toBeCloseTo(0.5 / Math.SQRT2, 2);
  });

  it("averages channels, and counts a disconnected input as silence so the clock keeps running", () => {
    const frames: Float32Array[] = [];
    const f = new MicFramer(16000, fr => frames.push(fr));
    const a = new Float32Array(128).fill(0.4), b = new Float32Array(128).fill(0.2);
    for (let i = 0; i < 4; i++) f.push([a, b]);
    expect(frames[0][10]).toBeCloseTo(0.3, 5);
    for (let i = 0; i < 4; i++) f.push([]);           // no channel: source gone
    for (let i = 0; i < 4; i++) f.push(undefined);
    expect(f.emitted).toBe(12 * 128 - ((12 * 128) % FRAME_SAMPLES));
    expect(frames.at(-1)!.every(v => v === 0)).toBe(true);
  });
});
