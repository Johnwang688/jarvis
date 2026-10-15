import { describe, expect, it } from "vitest";
import { ReadingRecorder } from "./enroll";
import { DEFAULT_QUALITY } from "./speaker";

const RATE = 16000;
const FRAME = 512;

/** Feed `seconds` of a 220 Hz tone at `amp`, speaking for `onFrac` of each 0.5 s. */
function speak(r: ReadingRecorder, seconds: number, amp: number, onFrac = 1) {
  const total = Math.round((seconds * RATE) / FRAME);
  let n = 0;
  for (let f = 0; f < total; f++) {
    const frame = new Float32Array(FRAME);
    for (let i = 0; i < FRAME; i++, n++) {
      const t = n / RATE;
      frame[i] = (t % 0.5) < 0.5 * onFrac ? amp * Math.sin(2 * Math.PI * 220 * t) : 0.0004 * Math.sin(n);
    }
    r.push(frame);
  }
}

describe("ReadingRecorder", () => {
  it("measures a tone the way the daemon does", () => {
    const r = new ReadingRecorder();
    speak(r, 10, 0.2);
    const m = r.meter();
    expect(m.seconds).toBeGreaterThan(9.9);
    expect(m.levelDb).toBeCloseTo(20 * Math.log10(0.2 / Math.SQRT2), 0);   // RMS of a sine is amp/sqrt2
    expect(m.speech).toBeGreaterThan(0.95);
    expect(m.clipping).toBe(0);
    expect(r.problems()).toEqual([]);
  });
  it("counts pauses as not speech", () => {
    const r = new ReadingRecorder();
    speak(r, 10, 0.2, 0.7);
    expect(r.meter().speech).toBeGreaterThan(0.6);
    expect(r.meter().speech).toBeLessThan(0.8);
    expect(r.problems()).toEqual([]);
  });
  it("says what is wrong, in the daemon's words", () => {
    const short = new ReadingRecorder(); speak(short, 3, 0.2);
    expect(short.problems()).toEqual(["too short: read the whole passage"]);
    const quiet = new ReadingRecorder(); speak(quiet, 10, 0.003);
    expect(quiet.problems()).toEqual(["too quiet: speak closer to the microphone"]);
    const loud = new ReadingRecorder(); speak(loud, 10, 0.95);
    expect(loud.problems().join()).toMatch(/too loud/);
    const clipped = new ReadingRecorder(); speak(clipped, 10, 3);
    expect(clipped.problems().join()).toMatch(/clipping/);
    const silent = new ReadingRecorder(); speak(silent, 10, 0);
    expect(silent.problems()[0]).toMatch(/too much silence/);
    const mostly = new ReadingRecorder(); speak(mostly, 10, 0.2, 0.2);
    expect(mostly.problems()[0]).toMatch(/too much silence/);
  });
  it("uses the thresholds the daemon sent", () => {
    const r = new ReadingRecorder({ ...DEFAULT_QUALITY, minS: 2 });
    speak(r, 3, 0.2);
    expect(r.problems()).toEqual([]);
  });
  it("is full a little before the daemon's limit and takes no more", () => {
    const r = new ReadingRecorder();
    speak(r, 29.4, 0.2);
    expect(r.full).toBe(false);
    speak(r, 5, 0.2);
    expect(r.full).toBe(true);
    expect(r.seconds).toBeLessThanOrEqual(DEFAULT_QUALITY.maxS - 0.5 + 1e-6);
    const before = r.samples().length;
    r.push(new Float32Array(FRAME));
    expect(r.samples().length).toBe(before);
  });
  it("a take is a 16-bit mono 16 kHz WAV, and clearing forgets the audio", async () => {
    const r = new ReadingRecorder();
    speak(r, 1, 0.2);
    const blob = r.wav();
    const buf = await new Promise<ArrayBuffer>(done => { const fr = new FileReader(); fr.onload = () => done(fr.result as ArrayBuffer); fr.readAsArrayBuffer(blob); });
    const wav = new DataView(buf);
    expect(String.fromCharCode(wav.getUint8(0), wav.getUint8(1), wav.getUint8(2), wav.getUint8(3))).toBe("RIFF");
    expect(wav.getUint16(22, true)).toBe(1);
    expect(wav.getUint32(24, true)).toBe(RATE);
    expect(wav.getUint16(34, true)).toBe(16);
    expect(wav.getUint32(40, true)).toBe(r.samples().length * 2);
    r.clear();
    expect(r.samples().length).toBe(0);
    expect(r.seconds).toBe(0);
    expect(r.meter().speech).toBe(0);
  });
  it("ignores empty frames", () => {
    const r = new ReadingRecorder();
    r.push(new Float32Array(0));
    expect(r.seconds).toBe(0);
  });
});
