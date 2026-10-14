// The Web Audio player (V6, plan §3.9).
//
// Web Audio, not <audio>: `decodeAudioData` + an `AudioBufferSourceNode` give
// exact stop/start control and an output level for the orb (an analyser), and
// the HUD's CSP (`media-src 'self'`) excludes the `blob:` URLs an <audio>
// element would need. Pause is "stop the source"; resume plays the sentence
// again from its start (open question 13), so no offsets are tracked.
//
// Chrome starts an AudioContext suspended until the page has seen a gesture.
// The player listens for one and resumes; until then `blocked()` is true and a
// source scheduled on the suspended context simply waits, so nothing is lost.

import type { PlayHandle, Player } from "./queue";

type ContextFactory = () => AudioContext;

const GESTURES = ["pointerdown", "keydown", "touchend"] as const;

export class WebAudioPlayer implements Player {
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private samples: Float32Array<ArrayBuffer> | null = null;
  private gestureBound = false;
  private wake: (() => void) | null = null;
  private onState: (() => void) | null = null;
  private onBlocked = new Set<() => void>();

  constructor(
    private readonly make: ContextFactory = () => {
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      return new Ctor();
    },
    private readonly target: Pick<Window, "addEventListener" | "removeEventListener"> | null =
      typeof window === "undefined" ? null : window,
  ) {}

  private context(): AudioContext {
    if (!this.ctx) {
      const ctx = this.make();
      this.ctx = ctx;
      try {
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.connect(ctx.destination);
        this.analyser = analyser;
        this.samples = new Float32Array(analyser.fftSize);
      } catch {
        this.analyser = null;
      }
      this.onState = () => this.changed();
      ctx.addEventListener?.("statechange", this.onState);
      this.bindGesture();
    }
    return this.ctx;
  }

  /** The first gesture on the page resumes a suspended context. */
  private bindGesture(): void {
    const ctx = this.ctx;
    if (this.gestureBound || !ctx || !this.target || ctx.state === "running") return;
    this.gestureBound = true;
    const target = this.target;
    const wake = () => {
      void ctx.resume().catch(() => {});
      if (ctx.state === "running") this.unbindGesture();
    };
    this.wake = wake;
    for (const g of GESTURES) target.addEventListener(g, wake, true);
  }

  private unbindGesture(): void {
    if (this.wake && this.target) for (const g of GESTURES) this.target.removeEventListener(g, this.wake, true);
    this.wake = null;
    this.gestureBound = false;
  }

  private changed(): void {
    for (const cb of [...this.onBlocked]) {
      try { cb(); } catch { /* never breaks playback */ }
    }
  }

  /** Called when the platform starts or stops holding audio back. */
  subscribeBlocked(cb: () => void): () => void {
    this.onBlocked.add(cb);
    return () => { this.onBlocked.delete(cb); };
  }

  decode(bytes: ArrayBuffer): Promise<unknown> {
    return this.context().decodeAudioData(bytes);
  }

  play(audio: unknown): PlayHandle {
    const ctx = this.context();
    const source = ctx.createBufferSource();
    source.buffer = audio as AudioBuffer;
    source.connect(this.analyser ?? ctx.destination);
    let finish!: (how: "ended" | "stopped") => void;
    const done = new Promise<"ended" | "stopped">((resolve) => { finish = resolve; });
    let stopped = false;
    source.onended = () => {
      try { source.disconnect(); } catch { /* already gone */ }
      finish(stopped ? "stopped" : "ended");
    };
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    source.start();
    return {
      done,
      stop: () => {
        if (stopped) return;
        stopped = true;
        try { source.stop(); } catch { /* never started, or ended */ }
        // `onended` may not fire for a source that never played; settle anyway.
        finish("stopped");
      },
    };
  }

  /** How long a decoded buffer lasts, in ms (the queue's watchdog). */
  duration(audio: unknown): number {
    return ((audio as AudioBuffer | null)?.duration ?? NaN) * 1000;
  }

  /** 0..1, from the analyser; 0 when nothing is sounding. */
  level(): number {
    const { analyser, samples } = this;
    if (!analyser || !samples) return 0;
    analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.min(1, Math.sqrt(sum / samples.length) * 5);
  }

  blocked(): boolean {
    return !!this.ctx && this.ctx.state === "suspended";
  }

  dispose(): void {
    const ctx = this.ctx;
    this.unbindGesture();
    if (ctx && this.onState) ctx.removeEventListener?.("statechange", this.onState);
    this.onState = null;
    this.ctx = null;
    this.analyser = null;
    this.onBlocked.clear();
    void ctx?.close().catch(() => {});
  }
}
