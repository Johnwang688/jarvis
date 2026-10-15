// One prompted reading for speaker enrollment (voice rework V12, plan 3.4).
//
// Records the microphone's 16 kHz frames (`MicInput.addFrameListener`) while the
// owner reads the prompt, keeps a live meter, and judges the take with the same
// checks the daemon makes: long enough, loud enough but not clipping, mostly
// speech. The daemon is the judge that counts (it re-checks what it is sent and
// stores its own re-encoding); this is the live meter and the early "read it
// again", so a bad take is not uploaded just to be refused.
//
// The audio lives in this object until the take is sent to `POST /speaker/reading`
// or discarded. It is never kept past that, never put in localStorage and never
// sent anywhere else.

import { CAP_RATE, wavBlob } from "./vad";
import { DEFAULT_QUALITY, type ReadingQuality } from "./speaker";

/** 20 ms frames, as the daemon measures them. */
const FRAME = CAP_RATE / 50;
const GATE_FLOOR = Math.pow(10, -55 / 20);

export interface ReadingMeter {
  seconds: number;
  /** RMS of the spoken frames, dBFS (-Infinity before any speech). */
  levelDb: number;
  /** Share of 20 ms frames that are speech, 0..1. */
  speech: number;
  /** Share of samples at full scale. */
  clipping: number;
  /** The newest frame's loudness, dBFS: the live bar. */
  liveDb: number;
}

const db = (x: number) => (x > 1e-9 ? 20 * Math.log10(x) : -Infinity);

export class ReadingRecorder {
  private chunks: Float32Array[] = [];
  private total = 0;
  private rms: number[] = [];
  private carry = new Float32Array(0);
  private clipped = 0;
  private live = 0;

  constructor(private readonly quality: ReadingQuality = DEFAULT_QUALITY) {}

  /** The seconds the daemon will accept, less a margin: past this the take is full and recording should stop. */
  get limitSeconds(): number { return this.quality.maxS - 0.5; }
  get seconds(): number { return this.total / CAP_RATE; }
  get full(): boolean { return this.seconds >= this.limitSeconds; }

  /** Take one frame. A frame past the limit is dropped, so a forgotten Stop cannot grow without bound. */
  push(frame: Float32Array): void {
    if (this.full || !frame.length) return;
    const room = Math.floor(this.limitSeconds * CAP_RATE) - this.total;
    const piece = frame.length > room ? frame.slice(0, room) : frame.slice();
    this.chunks.push(piece);
    this.total += piece.length;
    for (let i = 0; i < piece.length; i++) if (Math.abs(piece[i]) >= 0.99) this.clipped++;
    const joined = new Float32Array(this.carry.length + piece.length);
    joined.set(this.carry, 0);
    joined.set(piece, this.carry.length);
    let at = 0;
    for (; at + FRAME <= joined.length; at += FRAME) {
      let sum = 0;
      for (let i = at; i < at + FRAME; i++) sum += joined[i] * joined[i];
      const r = Math.sqrt(sum / FRAME);
      this.rms.push(r);
      this.live = r;
    }
    this.carry = joined.slice(at);
  }

  private gate(): number {
    if (!this.rms.length) return 1;
    const sorted = [...this.rms].sort((a, b) => a - b);
    const p95 = sorted[Math.min(sorted.length - 1, Math.round(0.95 * (sorted.length - 1)))];
    return Math.max(GATE_FLOOR, 0.2 * p95);
  }

  meter(): ReadingMeter {
    const gate = this.gate();
    const spoken = this.rms.filter(r => r >= gate);
    const mean = spoken.length ? Math.sqrt(spoken.reduce((n, r) => n + r * r, 0) / spoken.length) : 0;
    return {
      seconds: this.seconds,
      levelDb: db(mean),
      speech: this.rms.length ? spoken.length / this.rms.length : 0,
      clipping: this.total ? this.clipped / this.total : 0,
      liveDb: db(this.live),
    };
  }

  /** What is wrong with the take so far, in the daemon's words. Empty: it would be accepted. */
  problems(): string[] {
    const m = this.meter();
    const q = this.quality;
    const out: string[] = [];
    if (m.seconds < q.minS) out.push("too short: read the whole passage");
    if (m.speech < q.minSpeech || !Number.isFinite(m.levelDb)) out.push("too much silence: speak right away and keep going");
    else if (m.levelDb < q.minLevelDb) out.push("too quiet: speak closer to the microphone");
    else if (m.levelDb > q.maxLevelDb) out.push("too loud: move back from the microphone");
    if (m.clipping > q.maxClip) out.push("the sound is clipping: speak more softly");
    return out;
  }

  samples(): Float32Array {
    const out = new Float32Array(this.total);
    let at = 0;
    for (const c of this.chunks) { out.set(c, at); at += c.length; }
    return out;
  }

  /** The take as a 16-bit mono WAV, for the one upload. */
  wav(): Blob { return wavBlob(this.samples(), CAP_RATE); }

  /** Forget the audio. */
  clear(): void {
    this.chunks = []; this.total = 0; this.rms = []; this.carry = new Float32Array(0); this.clipped = 0; this.live = 0;
  }
}
