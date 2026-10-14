import { describe, expect, it } from "vitest";
import { FRAME_MS, LAG_MAX_MS, ReferenceTimeline, SelfVoiceGate, type SelfFrame } from "./selfvoice";

// ---- synthetic rooms -------------------------------------------------------
//
// Speech-like signals: noise shaped by a syllable envelope (120-260 ms bursts,
// 40-220 ms gaps, a pause now and then), generated from a seed. Jarvis's voice is
// 24 kHz (Kokoro's rate); the microphone is 16 kHz, 512-sample frames.

function rng(seed: number) {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
}

function speechlike(seconds: number, rate: number, seed: number, peak = 0.3): Float32Array {
  const r = rng(seed);
  const out = new Float32Array(Math.floor(seconds * rate));
  let t = 0;
  while (t < out.length) {
    const burst = Math.floor((0.12 + r() * 0.14) * rate);
    const amp = peak * (0.3 + 0.7 * r());
    for (let i = 0; i < burst && t + i < out.length; i++) {
      const w = Math.sin((Math.PI * i) / burst);
      out[t + i] = amp * w * w * (r() * 2 - 1);
    }
    t += burst + Math.floor((0.04 + r() * 0.18) * rate) + (r() < 0.1 ? Math.floor(0.4 * rate) : 0);
  }
  return out;
}

/** A fake page clock the test advances one microphone frame at a time. */
class Room {
  now = 1000;
  readonly gate: SelfVoiceGate;
  frames = 0;
  constructor() {
    this.gate = new SelfVoiceGate(() => this.now);
  }
  /** Jarvis starts a sound now (the gate hears it from the player). */
  play(id: number, samples: Float32Array, rate = 24000) {
    this.gate.reference.start(id, samples, rate);
  }
  stop(id: number) {
    this.gate.reference.stop(id);
  }
  /** One frame: `mic(i)` gives the 16 kHz sample for the i-th sample of the whole run. */
  frame(mic: (sample: number) => number): SelfFrame {
    const f = new Float32Array(512);
    const base = this.frames * 512;
    for (let i = 0; i < 512; i++) f[i] = mic(base + i);
    this.frames++;
    this.now += FRAME_MS;
    return this.gate.push(f);
  }
}

/** The room hears Jarvis `delayMs` after he sounds at wall time `startedAt`, scaled by `gain`, plus a floor of noise. */
function echoOf(ref: Float32Array, rate: number, startedAtFrame: number, delayMs: number, gain: number, noise: number, seed: number) {
  const r = rng(seed);
  return (sample: number) => {
    const t = (sample * 1000) / 16000 - startedAtFrame * FRAME_MS - delayMs;
    const k = Math.floor((t * rate) / 1000);
    return (k >= 0 && k < ref.length ? gain * ref[k] : 0) + noise * (r() * 2 - 1);
  };
}

describe("ReferenceTimeline", () => {
  it("averages the power over a span and is silent where nothing sounded", () => {
    const tl = new ReferenceTimeline();
    tl.add(1, 100, new Float32Array(2400).fill(0.5), 24000); // 100 ms at 0.5
    expect(tl.power(0, 100)).toBe(0);
    expect(tl.power(100, 200)).toBeCloseTo(0.25, 5);
    expect(tl.power(150, 250)).toBeCloseTo(0.125, 5); // half of it falls inside
    expect(tl.power(200, 300)).toBe(0);
  });
  it("a stop ends the sound there, and a natural end changes nothing", () => {
    const tl = new ReferenceTimeline();
    tl.add(1, 0, new Float32Array(24000).fill(0.4), 24000); // 1 s
    tl.stop(1, 500);
    expect(tl.power(0, 500)).toBeCloseTo(0.16, 5);
    expect(tl.power(500, 1000)).toBe(0);
    tl.stop(1, 5000); // later than its end: no change
    expect(tl.power(0, 500)).toBeCloseTo(0.16, 5);
  });
  it("prunes what ended long ago", () => {
    const tl = new ReferenceTimeline();
    tl.add(1, 0, new Float32Array(2400), 24000);
    tl.prune(10_000);
    expect(tl.size()).toBe(0);
  });
});

/** What the speakers do to his voice before the mic hears it: a short smear (a room's early reflections) and a slow wobble in gain. */
function reverb(ref: Float32Array, rate: number, seed: number): Float32Array {
  const r = rng(seed);
  const out = new Float32Array(ref.length);
  const taps = [[0, 1], [Math.round(rate * 0.011), 0.45], [Math.round(rate * 0.023), 0.3], [Math.round(rate * 0.047), 0.18]];
  const wobble = 0.15 * (r() * 2 - 1);
  for (let i = 0; i < ref.length; i++) {
    let v = 0;
    for (const [d, g] of taps) if (i >= d) v += g * ref[i - d];
    out[i] = v * (1 + wobble * Math.sin(i / rate * 2.1));
  }
  return out;
}

/** A room that has heard a few seconds of him, so the gate has learned it. */
function learnedRoom(delay: number, gain: number, seed = 1) {
  const room = new Room();
  const ref = speechlike(5, 24000, 500 + seed);
  for (let i = 0; i < 20; i++) room.frame(() => 0.0005 * Math.sin(i * 7));
  room.play(1, ref);
  const heard = reverb(ref, 24000, seed);
  const echo = echoOf(heard, 24000, room.frames, delay, gain, 0.0005, 9 + seed);
  const verdicts: SelfFrame[] = [];
  for (let i = 0; i < 120; i++) verdicts.push(room.frame(echo));
  return { room, verdicts };
}

describe("SelfVoiceGate: his own voice", () => {
  it("is clear while nothing plays", () => {
    const room = new Room();
    for (let i = 0; i < 40; i++) expect(room.frame(() => 0.2 * Math.sin(i))).toMatchObject({ active: false, self: false });
  });

  it("vetoes his voice coming back, at several delays and gains, from the first frame to the last", () => {
    for (const [delay, gain] of [[40, 0.5], [120, 0.25], [300, 0.1], [450, 0.7], [600, 0.05]] as const) {
      const { room, verdicts } = learnedRoom(delay, gain, delay);
      const free = verdicts.filter((v) => !v.self).length;
      expect(free, `delay ${delay} ms gain ${gain}: ${free} frames let through`).toBe(0);
      const st = room.gate.status();
      expect(st.lock, `delay ${delay}`).toBe("echo");
      expect(Math.abs(st.lagMs - delay)).toBeLessThanOrEqual(32);
      expect(st.gain).toBeGreaterThan(gain * 0.4);
      expect(st.gain).toBeLessThan(gain * 2.5);
    }
  });

  it("never lets a pure echo through across many rooms and voices (the property that matters)", () => {
    let frames = 0;
    let free = 0;
    let unlocked = 0;
    for (let seed = 1; seed <= 40; seed++) {
      const delay = 30 + (seed * 53) % 560;
      const gain = 0.05 + ((seed * 17) % 60) / 100;
      const { room, verdicts } = learnedRoom(delay, gain, seed);
      frames += verdicts.length;
      free += verdicts.filter((v) => !v.self).length;
      if (room.gate.status().lock !== "echo") unlocked++;
    }
    expect(free, `${free} of ${frames} echo frames let through`).toBe(0);
    expect(unlocked).toBeLessThanOrEqual(2); // a room that is not learned is held blanket, which is safe, but should be rare here
  });

  it("before anything is learned every frame is held, the owner's included (fail closed)", () => {
    const room = new Room();
    const ref = speechlike(5, 24000, 3);
    const owner = speechlike(5, 16000, 4, 0.5);
    for (let i = 0; i < 20; i++) room.frame(() => 0);
    room.play(1, ref);
    const base = room.frames * 512;
    const first: SelfFrame[] = [];
    for (let i = 0; i < 20; i++) first.push(room.frame((s) => owner[s - base] ?? 0));
    expect(first.every((v) => v.self && v.reason === "calibrating")).toBe(true);
  });

  it("another voice, once the room is learned, is not his: it is released while it speaks", () => {
    let freed = 0;
    let loudFrames = 0;
    for (let seed = 1; seed <= 12; seed++) {
      const { room } = learnedRoom(100 + seed * 20, 0.2, seed);
      const other = speechlike(5, 16000, 900 + seed, 0.3);
      const base = room.frames * 512;
      const ref = speechlike(5, 24000, 500 + seed);
      const echo = echoOf(reverb(ref, 24000, seed), 24000, room.frames - 120, 100 + seed * 20, 0.2, 0.0005, 77 + seed);
      for (let i = 0; i < 100; i++) {
        const v = room.frame((s) => echo(s) + (other[s - base] ?? 0));
        let e = 0;
        for (let k = 0; k < 512; k++) e += (other[i * 512 + k] ?? 0) ** 2;
        if (i < 25 || Math.sqrt(e / 512) < 0.05) continue;
        loudFrames++;
        if (!v.self) freed++;
      }
    }
    // His echo is the quieter part; the other voice is several times louder at its syllables.
    expect(loudFrames).toBeGreaterThan(200);
    expect(freed / loudFrames).toBeGreaterThan(0.7);
  });

  it("the owner speaking over him is released while he is loud", () => {
    const { room } = learnedRoom(120, 0.15, 2);
    const ref = speechlike(8, 24000, 21);
    const owner = speechlike(8, 16000, 31, 0.5);
    room.play(2, ref);
    const echo = echoOf(reverb(ref, 24000, 2), 24000, room.frames, 120, 0.15, 0.0005, 5);
    const base = room.frames * 512;
    const mic = (s: number) => echo(s) + (owner[s - base] ?? 0);
    const verdicts: SelfFrame[] = [];
    const loud: boolean[] = [];
    for (let i = 0; i < 160; i++) {
      verdicts.push(room.frame(mic));
      let e = 0;
      for (let k = 0; k < 512; k++) e += (owner[i * 512 + k] ?? 0) ** 2;
      loud.push(Math.sqrt(e / 512) > 0.05);
    }
    const heard = verdicts.filter((_, i) => loud[i]);
    expect(heard.length).toBeGreaterThan(20);
    const stuck = heard.filter((v) => v.self).length;
    // Two frames to notice a burst, then free; short bursts are caught half-way, so allow for that.
    expect(stuck / heard.length).toBeLessThan(0.3);
    expect(verdicts.filter((v) => !v.self).length).toBeGreaterThan(30);
  });

  it("a headphone room (no echo at all) is learned dry: the owner is free, silence is his", () => {
    const room = new Room();
    const ref = speechlike(5, 24000, 91);
    const owner = speechlike(5, 16000, 92, 0.5);
    for (let i = 0; i < 20; i++) room.frame(() => 0.0004 * Math.sin(1 + i));
    room.play(1, ref);
    const quiet: SelfFrame[] = [];
    for (let i = 0; i < 70; i++) quiet.push(room.frame((s) => 0.0003 * Math.sin(s * 1.7)));
    expect(room.gate.status().lock).toBe("dry");
    expect(quiet.slice(45).every((v) => v.self)).toBe(true); // nothing there is held, so nothing opens
    const base = room.frames * 512;
    const speaking: SelfFrame[] = [];
    for (let i = 0; i < 40; i++) speaking.push(room.frame((s) => owner[s - base] ?? 0));
    const loud = speaking.filter((_, i) => {
      let e = 0;
      for (let k = 0; k < 512; k++) e += (owner[i * 512 + k] ?? 0) ** 2;
      return Math.sqrt(e / 512) > 0.05;
    });
    const freed = speaking.filter((v, i) => !v.self && loud.includes(speaking[i])).length;
    expect(loud.length).toBeGreaterThan(15);
    expect(freed / loud.length).toBeGreaterThan(0.75);
  });

  it("the owner starting to speak is released within a few frames", () => {
    const { room } = learnedRoom(100, 0.2, 3);
    const ref = speechlike(8, 24000, 22);
    const owner = speechlike(8, 16000, 32, 0.5);
    room.play(2, ref);
    const echo = echoOf(reverb(ref, 24000, 3), 24000, room.frames, 100, 0.2, 0.0005, 6);
    const base = room.frames * 512;
    const from = base + 40 * 512;
    const mic = (s: number) => echo(s) + (s >= from ? owner[s - from] ?? 0 : 0);
    let firstFree = -1;
    for (let i = 0; i < 160; i++) {
      const v = room.frame(mic);
      const s = base + i * 512;
      let e = 0;
      for (let k = 0; k < 512; k++) e += (s + k >= from ? owner[s + k - from] ?? 0 : 0) ** 2;
      if (i >= 40 && firstFree < 0 && Math.sqrt(e / 512) > 0.05 && !v.self) firstFree = i - 40;
    }
    expect(firstFree).toBeGreaterThanOrEqual(0);
    expect(firstFree).toBeLessThanOrEqual(8); // about a quarter of a second at most
  });

  it("owner speech with no reference playing is never touched", () => {
    const room = new Room();
    const owner = speechlike(4, 16000, 41, 0.5);
    for (let i = 0; i < 100; i++) {
      const v = room.frame((s) => owner[s] ?? 0);
      expect(v.self).toBe(false);
    }
  });

  it("owner speech right after he stops is released once his tail has passed", () => {
    const { room } = learnedRoom(100, 0.3, 4);
    const ref = speechlike(2, 24000, 51);
    const owner = speechlike(4, 16000, 52, 0.5);
    room.play(2, ref);
    const echo = echoOf(reverb(ref, 24000, 4), 24000, room.frames, 100, 0.3, 0.0005, 8);
    const base = room.frames * 512;
    const after = base + (2000 / FRAME_MS + 3) * 512; // owner starts 3 frames after his last sample
    const mic = (s: number) => echo(s) + (s >= after ? owner[s - after] ?? 0 : 0);
    let released = false;
    for (let i = 0; i < 120; i++) {
      const v = room.frame(mic);
      if (i > 2000 / FRAME_MS + 6 && !v.self) released = true;
    }
    expect(released).toBe(true);
  });

  it("a stopped sound stops being a reference once its tail has passed", () => {
    const { room } = learnedRoom(100, 0.3, 5);
    const ref = speechlike(6, 24000, 61);
    room.play(2, ref);
    const echo = echoOf(reverb(ref, 24000, 5), 24000, room.frames, 100, 0.3, 0.0005, 9);
    for (let i = 0; i < 40; i++) room.frame(echo);
    room.stop(2);
    const owner = speechlike(3, 16000, 62, 0.5);
    const base = room.frames * 512;
    let last: SelfFrame = { active: true, playing: true, self: true, reason: "echo" };
    for (let i = 0; i < 90; i++) last = room.frame((s) => owner[s - base] ?? 0);
    expect(last.active).toBe(false);
    expect(last.self).toBe(false);
  });

  it("a lock goes stale: a dry one in half a minute, so a new reply re-checks the room", () => {
    const room = new Room();
    const ref = speechlike(3, 24000, 71);
    for (let i = 0; i < 20; i++) room.frame(() => 0);
    room.play(1, ref);
    for (let i = 0; i < 70; i++) room.frame(() => 0.0002);
    expect(room.gate.status().lock).toBe("dry");
    room.now += 31_000;
    room.frame(() => 0);
    expect(room.gate.status().lock).toBe("none");
  });
});

describe("SelfVoiceGate: failing closed", () => {
  it("is runnable by default, and not when unsupported or switched off", () => {
    const g = new SelfVoiceGate(() => 0);
    expect(g.runnable()).toBe(true);
    g.setEnabled(false);
    expect(g.runnable()).toBe(false);
    g.setEnabled(true);
    g.setSupported(false);
    expect(g.runnable()).toBe(false);
    expect(g.status()).toMatchObject({ supported: false, runnable: false });
  });

  it("a frame that cannot be placed is his voice while he is audible", () => {
    const room = new Room();
    const ref = speechlike(6, 24000, 71);
    for (let i = 0; i < 20; i++) room.frame(() => 0);
    room.play(1, ref);
    const owner = speechlike(3, 16000, 72, 0.5);
    for (let i = 0; i < 30; i++) room.frame((s) => owner[s % owner.length]);
    room.now += 4000; // a four-second stall: the next frame is far behind the clock's own line
    const v = room.frame((s) => owner[s % owner.length]);
    // The first frame back is late by seconds; the filter then lets later ones place themselves.
    expect(["unreliable", "unexplained", "echo", "quiet"]).toContain(v.reason);
    expect(v.active).toBe(true);
  });

  it("an exception makes the gate not runnable and the frame his own while he is audible", () => {
    const room = new Room();
    const ref = speechlike(2, 24000, 81);
    for (let i = 0; i < 20; i++) room.frame(() => 0);
    room.play(1, ref);
    room.frame(() => 0.1);
    const bad = { length: 512, get 0(): number { throw new Error("boom"); } } as unknown as Float32Array;
    const v = room.gate.push(bad);
    expect(v).toMatchObject({ self: true, reason: "error" });
    expect(room.gate.runnable()).toBe(false);
    room.gate.reset();
    expect(room.gate.runnable()).toBe(true);
  });

  it("covers the delays it searches", () => {
    expect(LAG_MAX_MS).toBeGreaterThanOrEqual(600);
  });
});
