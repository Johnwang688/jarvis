// Self-voice reference gating (voice rework V9, plan §3.4 step 2, decision V-5).
//
// The HUD plays Jarvis through Web Audio, so it knows exactly what it is putting
// out. That signal is a *reference*: a frame of the microphone whose loudness over
// the last few hundred milliseconds is explained by the reference, delayed by the
// speakers, the room and the audio stack, is his own voice coming back, and it
// must never open an utterance. This replaces V6's blanket "the detector is held
// off whenever he speaks", which made hands-free speech impossible while he talked.
//
// How a frame is judged (all in amplitude envelopes, no phase, so it needs no
// sample-accurate alignment between the output and the input clocks):
//
//   - The reference timeline holds, for every sound the player started, its power
//     per 8 ms hop (computed once from the decoded buffer) and when it started
//     and stopped, in the page's performance.now() clock.
//   - The mic's frames are placed on that clock with a minimum filter over how
//     late each arrived (a frame is never early, so the least-late arrival is the
//     truth, and a burst after a stall lands where it belongs).
//   - **The room is learned, then the frames are judged against it.** Over the last
//     second and a quarter the mic's envelope x is compared with the reference's y
//     at every delay from 0 to LAG_MAX_MS; when one delay and one gain a explain it
//     (score >= LOCK_SCORE: S = 1 - sum|x - a*y| / (sum x + a*sum y)), the gate
//     holds that *lock*. A mic that stays at the room's noise while he is loud has
//     no echo to learn (headphones, a good echo canceller): the lock is *dry*.
//     Learning from a window short enough to follow the owner would let chance
//     alignments of two speech envelopes pass for an echo, which is why the
//     decision about a *frame* is not made from a short window's score.
//   - With a lock, a frame is the owner's only when the newest two frames are each
//     clearly louder than the echo the lock predicts (UNEXPLAINED_FACTOR times a*y,
//     plus a floor); everything else is his own voice. Without one (the first
//     second of the first reply, a mic that is neither dry nor correlated) every
//     frame while he is audible is his voice: V6's blanket hold, for that second.
//
// **It fails closed.** A gate that cannot run (no reference from the player, a
// frame that arrives absurdly late, any exception) reports the frame as his own
// voice while he is audible, and `runnable()` is false so the caller falls back to
// V6's blanket hold for the whole of a reply. Nothing here claims, sends or
// approves anything: it returns a verdict per frame and nothing else.
//
// Not claimed: that a physical speaker's echo, through Windows' audio stack and a
// real room, scores as it does in the synthetic tests. That needs the owner's PC.

import { CAP_RATE } from "./vad";

export const FRAME_MS = 32; // one 512-sample frame at 16 kHz
export const REF_HOP_MS = 8;
export const LAG_STEP_MS = 16;
/** Output buffering + the air + the input path: the delays searched. */
export const LAG_MAX_MS = 640;
/** The window the room is learned over (1.28 s), and the least of it that must have been seen. */
export const LEARN_FRAMES = 40;
export const LEARN_MIN_FRAMES = 24;
/** S at or above this over the learning window: one delay and gain explain the mic. */
export const LOCK_SCORE = 0.9;
/**
 * A lock needs this many frames in which the reference was audible at the delay, and this many in which
 * the mic held something (the echo itself has to be there to be learned, not only its first syllable).
 */
export const LOCK_AUDIBLE_FRAMES = 12;
export const LOCK_HEARD_FRAMES = 12;
export const HEARD_AMP = 0.003;
/** Reference amplitude below which it counts as silent. */
export const REF_AUDIBLE_AMP = 0.01;
/** A dry lock: the mic stayed this quiet (mean, max above the room's noise) while he was audible. */
export const DRY_MEAN = 0.001;
export const DRY_MAX = 0.004;
/** ...for this many frames in a row to call the room dry, and this many to call it dry against an echo already learned. */
export const DRY_RUN_FRAMES = 16;
export const DRY_REPLACES_ECHO_FRAMES = 150;
/** A newest frame this many times louder than the echo the lock predicts is not an echo. */
export const UNEXPLAINED_FACTOR = 2;
export const UNEXPLAINED_FLOOR = 0.006;
/** A frame that arrives later than this behind the filter's clock cannot be placed: fail closed. */
export const LATE_MAX_MS = 1500;
/** The room's ring after his last sample. */
export const ECHO_TAIL_MS = 160;
/** How long a lock is trusted once he has gone quiet: an echo for a while, a dry one for less (headphones come and go). */
export const ECHO_LOCK_KEEP_MS = 10 * 60_000;
export const DRY_LOCK_KEEP_MS = 30_000;
/** The prediction rings on this many frames after a syllable ends, decaying by TAIL_DECAY each, and is searched this far either side of the learned delay. */
export const TAIL_FRAMES = 4;
export const TAIL_DECAY = 0.7;
export const LAG_SLACK_CELLS = 2;
/** After the owner is heard over him, this many frames stay free (a syllable's gap is not his voice returning). */
export const OWNER_HANGOVER_FRAMES = 4;
const OFFSET_SPAN_MS = 5000;
const MAX_GAIN = 8;

export type SelfReason = "clear" | "echo" | "dry" | "calibrating" | "unexplained" | "unreliable" | "error";

/** The gate's verdict on one microphone frame. */
export interface SelfFrame {
  /** His voice was audible within the echo delay (a sound is playing, or ended less than that ago): the frame is judged against it. */
  active: boolean;
  /** A sound of his is sounding in this very frame (the rising edge of this is a new reply starting). */
  playing: boolean;
  /** The frame is his own voice (or fail-closed while he is audible): it must not open an utterance. */
  self: boolean;
  reason: SelfReason;
}

export const CLEAR: SelfFrame = { active: false, playing: false, self: false, reason: "clear" };

/** What the player feeds: a sound started, a sound stopped. Samples are read once and not kept. */
export interface ReferenceSink {
  start(id: number, samples: Float32Array, rate: number): void;
  stop(id: number): void;
}

interface Segment {
  start: number;
  end: number;
  /** Power (mean square) per REF_HOP_MS from `start`. */
  power: Float32Array;
}

/** The reference: what was put out, when, as power per hop. */
export class ReferenceTimeline {
  private segments = new Map<number, Segment>();

  add(id: number, startMs: number, samples: Float32Array, rate: number): void {
    const hop = Math.max(1, Math.round((rate * REF_HOP_MS) / 1000));
    const n = Math.ceil(samples.length / hop);
    const power = new Float32Array(n);
    for (let h = 0; h < n; h++) {
      const from = h * hop;
      const to = Math.min(samples.length, from + hop);
      let s = 0;
      for (let i = from; i < to; i++) s += samples[i] * samples[i];
      power[h] = s / Math.max(1, to - from);
    }
    this.segments.set(id, { start: startMs, end: startMs + (samples.length * 1000) / rate, power });
  }

  /** The sound ends here (stopped, skipped, paused). A natural end changes nothing. */
  stop(id: number, atMs: number): void {
    const s = this.segments.get(id);
    if (s) s.end = Math.min(s.end, Math.max(s.start, atMs));
  }

  prune(beforeMs: number): void {
    for (const [id, s] of this.segments) if (s.end < beforeMs) this.segments.delete(id);
  }

  /** Mean power of the reference over [a, b): zero where nothing sounded. */
  power(a: number, b: number): number {
    if (!(b > a)) return 0;
    let total = 0;
    for (const s of this.segments.values()) {
      const lo = Math.max(a, s.start);
      const hi = Math.min(b, s.end);
      if (hi <= lo) continue;
      const h0 = Math.floor((lo - s.start) / REF_HOP_MS);
      const h1 = Math.min(s.power.length - 1, Math.floor((hi - s.start - 1e-9) / REF_HOP_MS));
      for (let h = h0; h <= h1; h++) {
        const hs = s.start + h * REF_HOP_MS;
        const overlap = Math.min(hi, hs + REF_HOP_MS) - Math.max(lo, hs);
        if (overlap > 0) total += s.power[h] * overlap;
      }
    }
    return total / (b - a);
  }

  /** Was anything sounding at any moment in [a, b]? */
  audibleBetween(a: number, b: number): boolean {
    for (const s of this.segments.values()) if (s.end >= a && s.start <= b) return true;
    return false;
  }

  size(): number {
    return this.segments.size;
  }
}

/** Minimum over a trailing span: the offset between a clock and its sample count that no delay can make smaller. */
class MinFilter {
  private buckets: { at: number; min: number }[] = [];
  add(now: number, value: number): number {
    const last = this.buckets[this.buckets.length - 1];
    if (last && now - last.at < 500) last.min = Math.min(last.min, value);
    else this.buckets.push({ at: now, min: value });
    while (this.buckets.length > 1 && now - this.buckets[0].at > OFFSET_SPAN_MS) this.buckets.shift();
    let m = Infinity;
    for (const b of this.buckets) m = Math.min(m, b.min);
    return m;
  }
  reset() {
    this.buckets = [];
  }
}

export interface GateStatus {
  /** The player gives a reference. */
  supported: boolean;
  /** The owner has not switched gating off (Settings, the blanket hold). */
  enabled: boolean;
  /** The gate threw once: it stays out of the way until reset. */
  broken: boolean;
  /** Reference gating is in force; false means the blanket hold applies. */
  runnable: boolean;
  /** His voice is audible within the echo delay right now. */
  active: boolean;
  /** What has been learned of the room: nothing yet, a correlated echo (at this delay and gain), or none at all. */
  lock: "none" | "echo" | "dry";
  lagMs: number;
  gain: number;
  /** The best score of the last learning window. */
  score: number;
  /** Frames vetoed as his own voice. */
  vetoed: number;
}

interface Lock {
  kind: "echo" | "dry";
  lag: number; // cells
  gain: number;
  at: number; // wall time it was last confirmed
}

export class SelfVoiceGate {
  readonly timeline = new ReferenceTimeline();
  /** What the player calls. */
  readonly reference: ReferenceSink;
  private supportedFlag: boolean;
  private enabledFlag = true;
  private brokenFlag = false;
  private frames = 0;
  private offsets = new MinFilter();
  /** Newest first. */
  private xs: number[] = [];
  private noise = 0.002;
  private lock: Lock | null = null;
  private activeNow = false;
  private playingNow = false;
  private score = 0;
  private vetoes = 0;
  /** Consecutive frames in which the mic stayed at the room's noise while he was audible at the longest delay. */
  private dryRun = 0;
  /** The frame index until which the owner is taken to be still speaking. */
  private freeUntil = -1;

  constructor(private readonly now: () => number = () => performance.now(), supported = true) {
    this.supportedFlag = supported;
    this.reference = {
      start: (id, samples, rate) => this.timeline.add(id, this.now(), samples, rate),
      stop: (id) => this.timeline.stop(id, this.now()),
    };
  }

  /** The player cannot give a reference (no Web Audio), or can again. */
  setSupported(on: boolean): void {
    this.supportedFlag = on;
  }

  /** The owner's switch: off means the blanket hold. */
  setEnabled(on: boolean): void {
    this.enabledFlag = on;
  }

  /** Forget a failure and what was learned of the room. */
  reset(): void {
    this.brokenFlag = false;
    this.lock = null;
    this.xs = [];
    this.dryRun = 0;
    this.freeUntil = -1;
  }

  /** Is reference gating in force? When not, the caller holds the mic for the whole of a reply, as V6 did. */
  runnable(): boolean {
    return this.supportedFlag && this.enabledFlag && !this.brokenFlag;
  }

  status(): GateStatus {
    const l = this.lock;
    return {
      supported: this.supportedFlag, enabled: this.enabledFlag, broken: this.brokenFlag, runnable: this.runnable(),
      active: this.activeNow, lock: l ? l.kind : "none", lagMs: l ? l.lag * LAG_STEP_MS : 0, gain: l ? l.gain : 0,
      score: this.score, vetoed: this.vetoes,
    };
  }

  /** Judge one microphone frame (16 kHz, 512 samples), in order. Never throws. */
  push(frame: Float32Array): SelfFrame {
    try {
      return this.judge(frame);
    } catch {
      this.brokenFlag = true;
      // Fail closed: whatever he is saying is not the owner.
      return { active: this.activeNow, playing: this.activeNow, self: this.activeNow, reason: "error" };
    }
  }

  private judge(frame: Float32Array): SelfFrame {
    const now = this.now();
    const idx = this.frames++;
    const frameMs = (frame.length * 1000) / CAP_RATE;
    const sampleEnd = (idx + 1) * FRAME_MS;
    const off = this.offsets.add(now, now - sampleEnd);
    const tEnd = off + sampleEnd;
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const amp = Math.sqrt(sum / Math.max(1, frame.length));
    this.xs.unshift(amp);
    if (this.xs.length > LEARN_FRAMES) this.xs.length = LEARN_FRAMES;
    this.timeline.prune(tEnd - LAG_MAX_MS - 4000);

    // His echo can still arrive for LAG_MAX_MS (and a tail) after his last sample, and not a moment longer.
    const active = this.timeline.audibleBetween(tEnd - frameMs - LAG_MAX_MS - ECHO_TAIL_MS, tEnd);
    this.activeNow = active;
    this.playingNow = this.timeline.audibleBetween(tEnd - frameMs, tEnd);
    if (!active) {
      // The room, alone: track its noise (falls fast, rises slowly); an old lock goes stale.
      this.noise += (amp - this.noise) * (amp < this.noise ? 0.3 : 0.003);
      this.noise = Math.max(this.noise, 0.0003);
      this.score = 0;
      const l = this.lock;
      if (l && now - l.at > (l.kind === "echo" ? ECHO_LOCK_KEEP_MS : DRY_LOCK_KEEP_MS)) this.lock = null;
      return CLEAR;
    }
    if (now - tEnd > LATE_MAX_MS) return this.verdict(true, "unreliable");

    const n = this.xs.length;
    const lagCells = Math.round(LAG_MAX_MS / LAG_STEP_MS);
    const cellCount = 2 * n + lagCells + 2;
    // cell[c]: the reference's power over [tEnd - 16c, tEnd - 16(c-1)), c from 1.
    const cell = new Float32Array(cellCount + 1);
    for (let c = 1; c <= cellCount; c++) cell[c] = this.timeline.power(tEnd - LAG_STEP_MS * c, tEnd - LAG_STEP_MS * (c - 1));
    const refAt = (j: number, l: number) => Math.sqrt(0.5 * (cell[2 * j + 1 + l] + cell[2 * j + 2 + l]));

    const x = new Float64Array(n);
    let sumX = 0;
    let maxX = 0;
    let heard = 0;
    for (let j = 0; j < n; j++) {
      x[j] = Math.max(0, this.xs[j] - this.noise);
      sumX += x[j];
      maxX = Math.max(maxX, x[j]);
      if (x[j] > HEARD_AMP) heard++;
    }

    // ---- learn the room ----------------------------------------------------
    if (n >= LEARN_MIN_FRAMES) this.learn(x, sumX, maxX, heard, n, lagCells, refAt, now);

    // ---- judge the frame -----------------------------------------------------
    const lock = this.lock;
    if (!lock) return this.verdict(true, "calibrating");
    // What the lock predicts for frame j: the reference at the learned delay, give or take two cells (the delay
    // is only known to a cell, and the audio stack drifts), and with the room's tail: a syllable's end rings on.
    const predicted = (j: number) => {
      if (lock.kind !== "echo") return 0;
      let m = 0;
      for (let k = 0; k <= TAIL_FRAMES && j + k < n; k++) {
        for (let d = -LAG_SLACK_CELLS; d <= LAG_SLACK_CELLS; d++) {
          const l = Math.min(lagCells + 1, Math.max(0, lock.lag + d));
          m = Math.max(m, refAt(j + k, l) * TAIL_DECAY ** k);
        }
      }
      return lock.gain * m;
    };
    const unexplained = (j: number) => x[j] > UNEXPLAINED_FACTOR * predicted(j) + UNEXPLAINED_FLOOR;
    // The owner has just begun, or has been speaking: the newest two frames hold more than the echo.
    if (n >= 2 && unexplained(0) && unexplained(1)) {
      this.freeUntil = idx + OWNER_HANGOVER_FRAMES;
      return this.verdict(false, "unexplained");
    }
    // Once he has begun, a syllable's gap does not hand the mic back to his own voice at once.
    if (idx <= this.freeUntil) return this.verdict(false, "unexplained");
    return this.verdict(true, lock.kind);
  }

  /** Update the lock from the learning window. */
  private learn(x: Float64Array, sumX: number, maxX: number, heard: number, n: number, lagCells: number, refAt: (j: number, l: number) => number, now: number): void {
    let best = -1;
    let bestLag = 0;
    let bestGain = 0;
    let bestAudible = 0;
    let anyAudible = 0;
    const y = new Float64Array(n);
    for (let l = 0; l <= lagCells; l++) {
      let sumY = 0;
      let sumXY = 0;
      let sumYY = 0;
      let audible = 0;
      for (let j = 0; j < n; j++) {
        const v = refAt(j, l);
        y[j] = v;
        sumY += v;
        sumXY += x[j] * v;
        sumYY += v * v;
        if (v > REF_AUDIBLE_AMP) audible++;
      }
      if (l === lagCells) anyAudible = audible;
      if (sumYY <= 1e-12 || sumX <= 1e-9) continue;
      const a = Math.min(MAX_GAIN, sumXY / sumYY);
      let diff = 0;
      for (let j = 0; j < n; j++) diff += Math.abs(x[j] - a * y[j]);
      const s = 1 - diff / (sumX + a * sumY);
      if (s > best) { best = s; bestLag = l; bestGain = a; bestAudible = audible; }
    }
    this.score = Math.max(0, best);
    // The lock already held, scored on this window: a rival must beat it, not merely reach LOCK_SCORE.
    const held = this.lock && this.lock.kind === "echo" ? this.lock : null;
    let heldScore = -1;
    if (held) {
      let sy = 0; let syy = 0; let sxy = 0;
      for (let j = 0; j < n; j++) { const v = refAt(j, held.lag); sy += v; syy += v * v; sxy += x[j] * v; }
      if (syy > 1e-12 && sumX > 1e-9) {
        const a = Math.min(MAX_GAIN, sxy / syy);
        let diff = 0;
        for (let j = 0; j < n; j++) diff += Math.abs(x[j] - a * refAt(j, held.lag));
        heldScore = 1 - diff / (sumX + a * sy);
        // Confirmed: the room is still the room, and its gain drifts with the volume.
        if (heldScore >= LOCK_SCORE) { held.at = now; held.gain = held.gain * 0.8 + a * 0.2; }
      }
    }
    if (best >= LOCK_SCORE && bestAudible >= LOCK_AUDIBLE_FRAMES && heard >= LOCK_HEARD_FRAMES && (!held || best > heldScore + 0.03)) {
      this.lock = { kind: "echo", lag: bestLag, gain: bestGain, at: now };
      this.dryRun = 0;
      return;
    }
    // Nothing correlated, and nothing there: he was loud for a good while and the mic never rose above the room.
    const dry = anyAudible >= LOCK_AUDIBLE_FRAMES && sumX / n < DRY_MEAN && maxX < DRY_MAX;
    this.dryRun = dry ? this.dryRun + 1 : 0;
    if (dry && (!this.lock || this.lock.kind === "dry" ? this.dryRun >= DRY_RUN_FRAMES : this.dryRun >= DRY_REPLACES_ECHO_FRAMES)) {
      this.lock = { kind: "dry", lag: 0, gain: 0, at: now };
    }
  }

  private verdict(self: boolean, reason: SelfReason): SelfFrame {
    if (self) this.vetoes++;
    return { active: true, playing: this.playingNow, self, reason };
  }
}
