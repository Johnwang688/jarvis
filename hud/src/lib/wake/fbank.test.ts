import { describe, expect, it } from "vitest";
import { FRAME_LEN, FRAME_SHIFT, NUM_MEL, OnlineFbank, fbankFrame, firstSampleOfFrame } from "./fbank";

const tone = (hz: number, n: number, amp = 0.3, from = 0) => Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * (i + from)) / 16000));

describe("log-mel features", () => {
  it("a frame is 80 values; silence sits at the floor, log of float epsilon", () => {
    const f = fbankFrame(new Float32Array(FRAME_LEN));
    expect(f.length).toBe(NUM_MEL);
    for (const v of f) expect(v).toBeCloseTo(Math.log(1.1920928955078125e-7), 4);
  });

  it("a tone lights the mel bins around it and darkens the far ones", () => {
    const peak = (hz: number) => { const f = fbankFrame(tone(hz, FRAME_LEN)); return f.indexOf(Math.max(...f)); };
    expect(peak(500)).toBeLessThan(peak(2000));
    expect(peak(2000)).toBeLessThan(peak(6000));
    const f = fbankFrame(tone(1000, FRAME_LEN));
    expect(f[peak(1000)] - f[NUM_MEL - 1]).toBeGreaterThan(8);
  });

  it("is louder when the signal is (a factor of 2 in amplitude is a factor of 4 in power)", () => {
    const a = fbankFrame(tone(1000, FRAME_LEN, 0.1));
    const b = fbankFrame(tone(1000, FRAME_LEN, 0.2));
    const i = a.indexOf(Math.max(...a));
    expect(b[i] - a[i]).toBeCloseTo(Math.log(4), 1);
  });

  it("ignores a constant offset (DC is removed)", () => {
    const x = tone(800, FRAME_LEN);
    const shifted = x.map((v) => v + 0.2);
    const a = fbankFrame(x); const b = fbankFrame(shifted);
    for (let i = 0; i < NUM_MEL; i++) expect(b[i]).toBeCloseTo(a[i], 3);
  });

  it("frame i is centred on 10 ms * i + 5 ms, the first one reflected at the head", () => {
    expect(firstSampleOfFrame(0)).toBe(-120);
    expect(firstSampleOfFrame(1)).toBe(40);
    expect(firstSampleOfFrame(10)).toBe(1480);
  });
});

describe("the online extractor", () => {
  it("has a frame ready only when its whole window has arrived", () => {
    const f = new OnlineFbank();
    f.accept(new Float32Array(279));
    expect(f.ready).toBe(0);
    f.accept(new Float32Array(1));
    expect(f.ready).toBe(1); // frame 0 needs samples to 280
    f.accept(new Float32Array(FRAME_SHIFT - 1));
    expect(f.ready).toBe(1);
    f.accept(new Float32Array(1));
    expect(f.ready).toBe(2);
  });

  it("gives the same frames however the audio is cut up", () => {
    const x = tone(700, 16000 * 1.3, 0.25);
    const whole = new OnlineFbank(); whole.accept(x);
    const n = whole.ready;
    for (const cut of [1, 160, 333, 512, 1600, 7777]) {
      const f = new OnlineFbank();
      for (let p = 0; p < x.length; p += cut) f.accept(x.slice(p, p + cut));
      expect(f.ready).toBe(n);
      expect(Array.from(f.take(0, n))).toEqual(Array.from(whole.take(0, n)));
    }
  });

  it("matches a direct computation of the frame from the samples (head reflected)", () => {
    const x = tone(900, 3200, 0.2);
    const f = new OnlineFbank(); f.accept(x);
    const direct = (i: number) => {
      const w = new Float32Array(FRAME_LEN);
      for (let k = 0; k < FRAME_LEN; k++) { let s = firstSampleOfFrame(i) + k; if (s < 0) s = -s - 1; w[k] = x[s]; }
      return fbankFrame(w);
    };
    for (const i of [0, 1, 7, 12]) expect(Array.from(f.take(i, 1))).toEqual(Array.from(direct(i)));
  });

  it("forgets frames it was told to discard, and refuses ones it never had", () => {
    const f = new OnlineFbank(); f.accept(new Float32Array(8000));
    f.discard(10);
    expect(() => f.take(5, 1)).toThrow();
    expect(() => f.take(f.ready, 1)).toThrow();
    expect(f.take(10, 2).length).toBe(2 * NUM_MEL);
  });
});
