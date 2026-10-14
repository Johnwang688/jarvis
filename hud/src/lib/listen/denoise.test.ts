// @vitest-environment node
// Noise suppression (V14): the 16 kHz <-> 48 kHz adapter, the real RNNoise
// binary run under Node (the same WebAssembly the worker compiles), and the
// Suppressor's lifecycle. Synthetic audio only: generated tones and noise, and
// the Kokoro text-to-speech clips the browser checks already use.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ADAPTER_DELAY, Denoiser16k, lowpass, Suppressor, SLOW_FRAME_MS, SLOW_RUN, type Frame48 } from "./denoise";
import { DenoiseFailError } from "./fail";
import { RNNOISE_BYTES, RNNOISE_FRAME, RNNOISE_SHA256 } from "./pins";
import type { DenoiseEvent } from "./protocol";
import { createRnnoise, verifiedRnnoise, type RnnoiseEngine } from "./rnnoise";

const HUD = resolve(__dirname, "../../..");
const WASM = readFileSync(resolve(HUD, "vendor/rnnoise/rnnoise_0.2.wasm"));
const wasmBytes = () => new Uint8Array(WASM.buffer, WASM.byteOffset, WASM.byteLength);

function wav(name: string): Float32Array {
  const b = readFileSync(resolve(HUD, "../tests/face/fixtures/voice_v5", name));
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const tag = (at: number) => String.fromCharCode(v.getUint8(at), v.getUint8(at + 1), v.getUint8(at + 2), v.getUint8(at + 3));
  let at = 12;
  while (tag(at) !== "data") at += 8 + v.getUint32(at + 4, true);
  const n = v.getUint32(at + 4, true) / 2;
  const x = new Float32Array(n);
  for (let i = 0; i < n; i++) x[i] = v.getInt16(at + 8 + 2 * i, true) / 32768;
  return x;
}

/** A small deterministic generator (mulberry32), so a failure reproduces. */
function rng(seed: number) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function whiteNoise(n: number, rms: number, seed = 1): Float32Array {
  const r = rng(seed);
  const x = new Float32Array(n);
  for (let i = 0; i < n; i++) x[i] = (r() * 2 - 1) * rms * Math.sqrt(3);
  return x;
}
const rmsOf = (x: Float32Array, a = 0, b = x.length) => {
  let s = 0;
  for (let i = a; i < b; i++) s += x[i] * x[i];
  return Math.sqrt(s / Math.max(1, b - a));
};
const db = (ratio: number) => 20 * Math.log10(ratio);
const tone = (n: number, hz: number, amp = 0.3) => Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / 16000));

/** Feed `x` through in frames of 512 (the HUD's), return everything that came out. */
function run(d: { process(f: Float32Array): Float32Array }, x: Float32Array, frame = 512): Float32Array {
  const out = new Float32Array(x.length);
  for (let i = 0; i + frame <= x.length; i += frame) out.set(d.process(x.subarray(i, i + frame)), i);
  return out;
}

/** The lag (in samples) at which `y` best matches `x`. */
function lagOf(x: Float32Array, y: Float32Array, max = 800): number {
  let best = 0, bestV = -Infinity;
  for (let lag = 0; lag <= max; lag++) {
    let s = 0;
    for (let i = 0; i + lag < y.length; i += 2) s += x[i] * y[i + lag];
    if (s > bestV) { bestV = s; best = lag; }
  }
  return best;
}

const identity: Frame48 = { frame: () => 0 };

describe("the resampling filter", () => {
  const h = lowpass();
  const gainAt = (hz: number) => {
    let re = 0, im = 0;
    for (let j = 0; j < h.length; j++) { re += h[j] * Math.cos((2 * Math.PI * hz * j) / 48000); im -= h[j] * Math.sin((2 * Math.PI * hz * j) / 48000); }
    return Math.hypot(re, im);
  };
  it("has unity gain at DC, is symmetric (linear phase) and 95 taps", () => {
    expect(h.length).toBe(95);
    expect(Array.from(h).reduce((a, b) => a + b, 0)).toBeCloseTo(1, 5);
    for (let j = 0; j < h.length; j++) expect(h[j]).toBeCloseTo(h[h.length - 1 - j], 7);
  });
  it("passes speech (to 6 kHz within 0.3 dB) and rejects what would alias (from 9 kHz, over 55 dB)", () => {
    for (const hz of [100, 1000, 3000, 5000, 6000]) expect(Math.abs(db(gainAt(hz)))).toBeLessThan(0.3);
    for (const hz of [9000, 10000, 16000, 23000]) expect(db(gainAt(hz))).toBeLessThan(-55);
  });
});

describe("the adapter around the 48 kHz engine", () => {
  it("returns exactly as many samples as it was given, for any chunking", () => {
    const x = whiteNoise(512 * 7 + 100, 0.1);
    for (const chunk of [512, 160, 1, 77, 1000]) {
      const d = new Denoiser16k(identity);
      let total = 0;
      for (let i = 0; i < x.length; i += chunk) total += d.process(x.subarray(i, Math.min(x.length, i + chunk))).length;
      expect(total).toBe(x.length);
    }
  });

  it("gives the same samples whatever the chunking (a stream, not a set of frames)", () => {
    const x = whiteNoise(512 * 6, 0.1, 3);
    const ref = run(new Denoiser16k(identity), x, 512);
    for (const chunk of [256, 128, 32]) {
      const got = run(new Denoiser16k(identity), x, chunk);
      let worst = 0;
      for (let i = 0; i < x.length; i++) worst = Math.max(worst, Math.abs(got[i] - ref[i]));
      expect(worst).toBeLessThan(1e-5);
    }
  });

  it("with the engine doing nothing, is a delayed, near-unity-gain copy (tones from 200 Hz to 6 kHz)", () => {
    for (const hz of [200, 1000, 3000, 6000]) {
      const x = tone(512 * 20, hz);
      const y = run(new Denoiser16k(identity), x);
      const lo = 512 * 8, hi = 512 * 20;
      expect(Math.abs(db(rmsOf(y, lo, hi) / rmsOf(x, lo, hi)))).toBeLessThan(0.5);
    }
  });

  it("its own delay is the held block plus the two filters: about 12 ms, constant", () => {
    const x = whiteNoise(512 * 30, 0.2, 9);
    const lag = lagOf(x, run(new Denoiser16k(identity), x));
    // 160 held + 94/3 for the two 95-tap filters at 48 kHz (31.3 samples at 16 kHz)
    expect(lag).toBeGreaterThanOrEqual(ADAPTER_DELAY + 30);
    expect(lag).toBeLessThanOrEqual(ADAPTER_DELAY + 33);
  });

  it("reset forgets the past: the same audio after a reset gives the same output", () => {
    const x = whiteNoise(512 * 5, 0.1, 4);
    const d = new Denoiser16k(identity);
    const a = run(d, x);
    d.reset();
    const b = run(d, x);
    for (let i = 0; i < x.length; i++) expect(b[i]).toBeCloseTo(a[i], 6);
  });

  it("hands the engine whole 480-sample frames, in order, never more than one per 160 input samples", () => {
    const sizes: number[] = [];
    const d = new Denoiser16k({ frame: (io) => { sizes.push(io.length); } });
    run(d, whiteNoise(512 * 10, 0.1), 512);
    expect(new Set(sizes)).toEqual(new Set([RNNOISE_FRAME]));
    expect(sizes.length).toBe(Math.floor((512 * 10) / 160));
  });
});

describe("the pinned RNNoise binary", () => {
  it("is the bytes pins.ts, the manifest and V0's tarball record", () => {
    expect(WASM.length).toBe(RNNOISE_BYTES);
    expect(createHash("sha256").update(WASM).digest("hex")).toBe(RNNOISE_SHA256);
    const manifest = JSON.parse(readFileSync(resolve(HUD, "vendor/rnnoise/manifest.json"), "utf8")).model;
    expect(manifest.sha256).toBe(RNNOISE_SHA256);
    expect(manifest.bytes).toBe(RNNOISE_BYTES);
    const v0 = JSON.parse(readFileSync(resolve(HUD, "../tests/voice_v0/models.json"), "utf8")).models["npm-jitsi-rnnoise-wasm-0.2.1"];
    expect(v0.sha256).toBe(manifest.derived_from.tarball_sha256);
    expect(v0.npm_integrity).toBe(manifest.derived_from.npm_integrity);
  });

  it("travels with both licences", () => {
    expect(readFileSync(resolve(HUD, "vendor/rnnoise/LICENSE-rnnoise-BSD-3-Clause.txt"), "utf8")).toContain("Xiph.Org Foundation");
    expect(readFileSync(resolve(HUD, "vendor/rnnoise/LICENSE-jitsi-rnnoise-wasm-Apache-2.0.txt"), "utf8")).toContain("Apache License");
  });

  it("is accepted as is, and refused if one byte changed, or truncated, or padded (hash)", async () => {
    expect(await verifiedRnnoise(async () => wasmBytes())).toHaveLength(RNNOISE_BYTES);
    const flip = wasmBytes().slice(); flip[RNNOISE_BYTES >> 1] ^= 1;
    await expect(verifiedRnnoise(async () => flip)).rejects.toMatchObject({ name: "DenoiseFailError", code: "hash" });
    await expect(verifiedRnnoise(async () => wasmBytes().slice(0, RNNOISE_BYTES - 1000))).rejects.toMatchObject({ code: "hash" });
    await expect(verifiedRnnoise(async () => new Uint8Array([...wasmBytes(), 0]))).rejects.toMatchObject({ code: "hash" });
  });

  it("says fetch when it cannot be read at all, without the engine's text", async () => {
    await expect(verifiedRnnoise(async () => { throw new Error("404 https://secret.example/x.wasm"); }))
      .rejects.toMatchObject({ code: "fetch", message: "fetch" });
  });

  it("bytes that are not a WebAssembly module, even with a matching pin, say runtime", async () => {
    const junk = Uint8Array.from({ length: 64 }, (_, i) => i);
    const pin = createHash("sha256").update(junk).digest("hex");
    const bytes = await verifiedRnnoise(async () => junk, pin, 64);
    await expect(createRnnoise(bytes)).rejects.toBeInstanceOf(DenoiseFailError);
    await expect(createRnnoise(bytes)).rejects.toMatchObject({ code: "runtime" });
  });
});

describe("RNNoise on audio (real WebAssembly, synthetic signals)", () => {
  let engine: RnnoiseEngine;
  const make = async () => createRnnoise(await verifiedRnnoise(async () => wasmBytes()));

  it("starts, and a 480-sample frame in is a 480-sample frame out in [-1, 1]", async () => {
    engine = await make();
    const f = whiteNoise(RNNOISE_FRAME, 0.05);
    const p = engine.frame(f);
    expect(f).toHaveLength(RNNOISE_FRAME);
    expect(p).toBeGreaterThanOrEqual(0); expect(p).toBeLessThanOrEqual(1);
    for (const v of f) expect(Math.abs(v)).toBeLessThanOrEqual(1);
    engine.dispose();
    expect(() => engine.frame(f)).toThrow();
  });

  it("removes steady noise: white and pink-ish noise at -30 dBFS come out at least 15 dB quieter", async () => {
    for (const seed of [1, 2]) {
      const e = await make();
      const d = new Denoiser16k(e);
      let x = whiteNoise(16000 * 4, 0.03, seed);
      if (seed === 2) { let a = 0; x = x.map((v) => (a = 0.97 * a + v) * 0.25); } // low-passed: more like a fan
      const y = run(d, x);
      const from = 16000 * 1.5;
      expect(db(rmsOf(y, from) / rmsOf(x, from))).toBeLessThan(-15);
      e.dispose();
    }
  });

  it("keeps speech: a Kokoro clip comes out within 3 dB of its energy and strongly correlated with the input", async () => {
    const speech = wav("kokoro_a.wav");
    const e = await make();
    const d = new Denoiser16k(e);
    const y = run(d, speech.subarray(0, Math.floor(speech.length / 512) * 512));
    const x = speech.subarray(0, y.length);
    expect(Math.abs(db(rmsOf(y) / rmsOf(x)))).toBeLessThan(3);
    const lag = lagOf(x, y);
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i + lag < y.length; i++) { sxy += x[i] * y[i + lag]; sxx += x[i] * x[i]; syy += y[i + lag] * y[i + lag]; }
    expect(sxy / Math.sqrt(sxx * syy)).toBeGreaterThan(0.8);
    e.dispose();
  });

  it("speech in steady noise: the noise-only stretches drop much more than the speech", async () => {
    const speech = wav("kokoro_a.wav");
    const lead = 16000 * 2;
    const n = lead + Math.floor((speech.length + 16000) / 512) * 512;
    const noise = whiteNoise(n, 0.03, 5);
    const x = new Float32Array(n);
    x.set(noise);
    for (let i = 0; i < speech.length; i++) x[lead + i] += speech[i];
    const e = await make();
    const y = run(new Denoiser16k(e), x);
    const noiseDrop = db(rmsOf(y, 16000 * 1, lead) / rmsOf(x, 16000 * 1, lead));
    const speechDrop = db(rmsOf(y, lead, lead + speech.length) / rmsOf(x, lead, lead + speech.length));
    expect(noiseDrop).toBeLessThan(-15);
    expect(speechDrop).toBeGreaterThan(noiseDrop + 10);
    e.dispose();
  });

  it("end to end the signal is delayed about 32 ms (one 512-sample frame), the figure the notes quote", async () => {
    for (const clip of ["kokoro_a.wav", "kokoro_b.wav"]) {
      const speech = wav(clip);
      const x = speech.subarray(0, Math.floor(speech.length / 512) * 512);
      const e = await make();
      const lag = lagOf(x, run(new Denoiser16k(e), x), 900);
      // 160 held block + ~31 filters + ~320 inside RNNoise 0.2 (two 10 ms frames) = ~511, measured
      console.log(`${clip}: measured end-to-end latency ${lag} samples = ${(lag / 16).toFixed(1)} ms`);
      expect(lag).toBeGreaterThan(480);
      expect(lag).toBeLessThan(540);
      e.dispose();
    }
  });

  it("is fast: a 512-sample frame takes well under 8 ms (budget 32 ms)", async () => {
    const e = await make();
    const d = new Denoiser16k(e);
    const x = whiteNoise(512 * 200, 0.05, 8);
    run(d, x.subarray(0, 512 * 20)); // warm
    const t0 = performance.now();
    run(d, x.subarray(512 * 20));
    const per = (performance.now() - t0) / 180;
    console.log(`denoise cost under Node/V8: ${per.toFixed(3)} ms per 32 ms frame`);
    expect(per).toBeLessThan(8);
    e.dispose();
  });

  it("reset gives the same output as a fresh engine", async () => {
    const x = whiteNoise(512 * 12, 0.05, 6);
    const e = await make();
    const d = new Denoiser16k(e);
    run(d, x);
    d.reset();
    const again = run(d, x);
    const e2 = await make();
    const fresh = run(new Denoiser16k(e2), x);
    let worst = 0;
    for (let i = 0; i < x.length; i++) worst = Math.max(worst, Math.abs(again[i] - fresh[i]));
    expect(worst).toBeLessThan(1e-5);
    e.dispose(); e2.dispose();
  });
});

// ---------------------------------------------------------------------------

function fakeEngine(over: Partial<RnnoiseEngine> = {}): RnnoiseEngine & { disposed: boolean } {
  const e = { disposed: false, frame: () => 0, reset: () => {}, dispose() { e.disposed = true; }, ...over };
  return e as RnnoiseEngine & { disposed: boolean };
}
const settle = () => new Promise((r) => setTimeout(r, 0));

function suppressor(load: () => Promise<RnnoiseEngine>, clock = { t: 0, step: 0 }) {
  const events: DenoiseEvent[] = [];
  const s = new Suppressor({ load, now: () => (clock.t += clock.step), notify: (e) => events.push(e) });
  return { s, events, clock };
}

describe("the Suppressor: off until asked, and nothing loads before that", () => {
  it("never calls load while off (a new one, frames passing through)", () => {
    let loads = 0;
    const { s, events } = suppressor(async () => { loads++; return fakeEngine(); });
    const f = whiteNoise(512, 0.1);
    expect(s.process(f)).toEqual({ samples: f, ms: 0 });
    expect(s.state).toBe("off");
    expect(loads).toBe(0);
    expect(events).toEqual([]);
  });

  it("enable loads once, reports loading then ready, and then denoises", async () => {
    let loads = 0;
    const eng = fakeEngine({ frame: (io) => { io.fill(0); return 0; } });
    const { s, events } = suppressor(async () => { loads++; return eng; }, { t: 0, step: 1 });
    s.enable(); s.enable();
    expect(loads).toBe(1);
    expect(s.state).toBe("loading");
    expect(s.process(whiteNoise(512, 0.1)).ms).toBe(0); // raw while loading
    await settle();
    expect(s.state).toBe("ready");
    expect(events.map((e) => e.state)).toEqual(["loading", "ready"]);
    const out = s.process(whiteNoise(512, 0.1));
    expect(rmsOf(out.samples)).toBeLessThan(1e-6); // the engine zeroed it
    expect(out.ms).toBeGreaterThan(0);
  });

  it("disable frees the engine and goes back to raw", async () => {
    const eng = fakeEngine();
    const { s, events } = suppressor(async () => eng);
    s.enable(); await settle();
    s.disable();
    expect(eng.disposed).toBe(true);
    expect(s.state).toBe("off");
    expect(events.at(-1)).toEqual({ state: "off" });
    const f = whiteNoise(512, 0.1);
    expect(s.process(f).samples).toBe(f);
  });

  it("switched off while still loading: the engine that arrives is freed, never used", async () => {
    const eng = fakeEngine();
    let finish!: (e: RnnoiseEngine) => void;
    const { s, events } = suppressor(() => new Promise<RnnoiseEngine>((r) => { finish = r; }));
    s.enable();
    s.disable();
    finish(eng); await settle();
    expect(eng.disposed).toBe(true);
    expect(s.state).toBe("off");
    expect(events.map((e) => e.state)).toEqual(["loading", "off"]);
  });

  it("a failed load is named by code (a DenoiseFailError's own, else runtime) and frames stay raw", async () => {
    for (const [err, code] of [[new DenoiseFailError("hash"), "hash"], [new DenoiseFailError("fetch"), "fetch"], [new Error("boom at http://x"), "runtime"]] as const) {
      const { s, events } = suppressor(async () => { throw err; });
      s.enable(); await settle();
      expect(s.state).toBe("failed");
      expect(s.code).toBe(code);
      expect(events.at(-1)).toEqual({ state: "failed", code });
      expect(JSON.stringify(events)).not.toMatch(/boom|http/);
      const f = whiteNoise(512, 0.1);
      expect(s.process(f).samples).toBe(f);
    }
  });

  it("a failure can be retried with enable (loading again), and a later success is ready", async () => {
    let n = 0;
    const { s } = suppressor(async () => { if (n++ === 0) throw new DenoiseFailError("fetch"); return fakeEngine(); });
    s.enable(); await settle();
    expect(s.state).toBe("failed");
    s.enable(); await settle();
    expect(s.state).toBe("ready");
    expect(s.code).toBeNull();
  });

  it("an engine that throws mid-stream turns suppression off with 'runtime'; that frame is raw", async () => {
    const eng = fakeEngine();
    const { s } = suppressor(async () => eng);
    s.enable(); await settle();
    eng.frame = () => { throw new Error("trap"); };
    const f = whiteNoise(512, 0.1);
    expect(s.process(f).samples).toBe(f);
    expect(s.state).toBe("failed");
    expect(s.code).toBe("runtime");
    expect(eng.disposed).toBe(true);
  });

  it(`${SLOW_RUN} frames in a row slower than ${SLOW_FRAME_MS} ms turn it off ('slow'); a fast frame in between resets the count`, async () => {
    const clock = { t: 0, step: 0 };
    const { s, events } = suppressor(async () => fakeEngine(), clock);
    s.enable(); await settle();
    const f = whiteNoise(512, 0.1);
    clock.step = SLOW_FRAME_MS * 2 / 2; // each call to now() advances; process reads it twice -> SLOW_FRAME_MS ... use more
    clock.step = SLOW_FRAME_MS + 5;
    for (let i = 0; i < SLOW_RUN - 1; i++) s.process(f);
    expect(s.state).toBe("ready");
    clock.step = 0.1; s.process(f); // a quick one
    clock.step = SLOW_FRAME_MS + 5;
    for (let i = 0; i < SLOW_RUN - 1; i++) s.process(f);
    expect(s.state).toBe("ready");
    s.process(f);
    expect(s.state).toBe("failed");
    expect(s.code).toBe("slow");
    expect(events.at(-1)).toEqual({ state: "failed", code: "slow" });
  });

  it("an observer that throws cannot stop the scoring", async () => {
    const s = new Suppressor({ load: async () => fakeEngine(), notify: () => { throw new Error("observer"); } });
    s.enable(); await settle();
    expect(s.state).toBe("ready");
    expect(s.process(whiteNoise(512, 0.1)).samples).toHaveLength(512);
  });
});
