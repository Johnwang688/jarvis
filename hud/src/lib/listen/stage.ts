// The VAD stage (voice rework V5, plan §3.1/§3.2): sits between the microphone
// and the Capture. Every 512-sample frame comes in here, goes to the Silero
// worker as a copy, and is handed on to `deliver(frame, p)` IN ORDER once its
// speech probability is back (about a millisecond later). The Capture decides
// nothing differently; `p` only replaces the energy test for that frame.
//
//   mic frames ──> push() ──┬── copy ──> Silero worker ──> p
//                           └── held until its p arrives ──> deliver(frame, p)
//
// **The energy detector is the fallback, and the window says so.** Whenever the
// worker is not loaded yet, cannot load (no Worker, a failed script, a missing or
// altered model, a runtime that will not start), has fallen too far behind, or
// answers late, frames are delivered at once with `p = null` and the Capture
// judges them by energy, as it did before V5. `status()` / `onStatus` name the
// detector in use and why; it is never silent. A failure is sticky for the life
// of the window (no flapping between detectors mid-sentence); `retry()` tries
// again, and the Settings → Voice panel offers it.
//
// **Bounded.** At most MAX_PENDING frames (0.5 s of audio) wait on the worker;
// one more and the stage drops to energy rather than buffering without limit.
// A verdict later than FRAME_DEADLINE_MS drops it too. While the mic is OFF
// (`active() === false`) nothing is posted to the worker at all, and its state
// is reset when listening resumes.
//
// **Seam for RNNoise (V14).** `denoise` runs on the copy the worker scores, never
// on the frame the Capture records: speech-to-text takes the echo-cancelled
// stream, not a denoised one (V0 section 2). A noise-suppression stage plugs in
// there, in front of the VAD.

import { FAIL_TEXT, type FailCode, type FromWorker, type ToWorker } from "./protocol";

export const MAX_PENDING = 16; // frames of audio waiting on the worker: 16 x 32 ms
export const FRAME_DEADLINE_MS = 500; // the oldest unanswered frame may wait this long
export const LOAD_DEADLINE_MS = 20_000; // fetch + hash + compile + warm-up

export interface WorkerLike {
  postMessage(message: ToWorker, transfer?: Transferable[]): void;
  terminate(): void;
  onmessage: ((e: MessageEvent<FromWorker>) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onmessageerror: ((e: unknown) => void) | null;
}

export type DetectorName = "silero" | "energy";
export type DetectorState = "idle" | "loading" | "ready" | "failed";

export interface DetectorStatus {
  /** The detector judging frames right now. */
  detector: DetectorName;
  state: DetectorState;
  /** Why it is the energy detector, as a code (null while Silero is live or loading). */
  code: FailCode | null;
  /** One sentence for Settings → Voice: no URL, no path, no engine text. */
  note: string;
  /** Mean time the worker took per frame (ms), 0 until it has answered. */
  frameMs: number;
  /** Frames the ML VAD scored so far. */
  scored: number;
  /** How long the worker took to become ready (fetch, checksum, compile, warm-up), 0 until it has. */
  loadMs: number;
}

export interface StageOptions {
  /** Where frames go on, in order. `p` is null when the energy detector must judge it. */
  deliver: (frame: Float32Array, p: number | null) => void;
  createWorker?: () => WorkerLike;
  now?: () => number;
  /** False while nothing should be scored (the mic is OFF). */
  active?: () => boolean;
  /** A noise-suppression stage in front of the VAD: given a frame, the one the VAD scores. */
  denoise?: (frame: Float32Array) => Float32Array;
  onStatus?: (status: DetectorStatus) => void;
}

interface Held {
  frame: Float32Array;
  p: number | null;
  resolved: boolean;
  seq: number;
  sentAt: number;
}

const defaultWorker = (): WorkerLike =>
  new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }) as unknown as WorkerLike;

export class VadStage {
  private worker: WorkerLike | null = null;
  private state: DetectorState = "idle";
  private code: FailCode | null = null;
  private held: Held[] = [];
  private pending = new Map<number, Held>();
  private seq = 0;
  private startedAt = 0;
  private wasActive = true;
  private scored = 0;
  private totalMs = 0;
  private loadMs = 0;
  private lastStatus = "";

  constructor(private o: StageOptions) {}

  private now() {
    return (this.o.now ?? (() => performance.now()))();
  }

  status(): DetectorStatus {
    const silero = this.state === "ready";
    return {
      detector: silero ? "silero" : "energy",
      state: this.state,
      code: this.code,
      note: silero ? "Silero VAD, running in a worker"
        : this.state === "loading" ? "loading the Silero VAD (the energy detector listens meanwhile)"
        : this.state === "failed" && this.code ? FAIL_TEXT[this.code]
        : "not started",
      frameMs: this.scored ? this.totalMs / this.scored : 0,
      scored: this.scored,
      loadMs: silero ? this.loadMs : 0,
    };
  }

  private publish() {
    const s = this.status();
    const key = `${s.detector}|${s.state}|${s.code}|${s.note}`;
    if (key === this.lastStatus) return;
    this.lastStatus = key;
    try { this.o.onStatus?.(s); } catch { /* an observer cannot change detection */ }
  }

  /** Begin loading the worker. Safe to call again: it only acts from idle or failed. */
  start() {
    if (this.state === "loading" || this.state === "ready") return;
    this.code = null;
    this.scored = 0;
    this.totalMs = 0;
    this.loadMs = 0;
    this.startedAt = this.now();
    if (typeof Worker === "undefined" && !this.o.createWorker) { this.fail("no_worker"); return; }
    let w: WorkerLike;
    try { w = (this.o.createWorker ?? defaultWorker)(); } catch { this.fail("worker"); return; }
    this.worker = w;
    this.state = "loading";
    w.onmessage = (e) => this.onMessage(e.data);
    w.onerror = () => this.fail("worker");
    w.onmessageerror = () => this.fail("worker");
    try { w.postMessage({ type: "init" }); } catch { this.fail("worker"); return; }
    this.publish();
  }

  /** Try the ML detector again after a failure. */
  retry() {
    if (this.state === "failed") this.start();
  }

  /** The mic closed: nothing more is scored, and what is held is dropped with it. */
  stop() {
    this.release();
    this.state = "idle";
    this.code = null;
    this.held = [];
    this.pending.clear();
    this.publish();
  }

  private release() {
    const w = this.worker;
    this.worker = null;
    if (!w) return;
    w.onmessage = null; w.onerror = null; w.onmessageerror = null;
    try { w.terminate(); } catch { /* already gone */ }
  }

  private fail(code: FailCode) {
    if (this.state === "failed" && this.code === code) return;
    this.release();
    this.state = "failed";
    this.code = code;
    // Everything waiting is judged by energy now, in order.
    for (const h of this.held) if (!h.resolved) { h.resolved = true; h.p = null; }
    this.pending.clear();
    this.drain();
    this.publish();
  }

  private drain() {
    while (this.held.length && this.held[0].resolved) {
      const h = this.held.shift()!;
      try { this.o.deliver(h.frame, h.p); } catch { /* the capture's own guards; a throw must not wedge the stage */ }
    }
  }

  private onMessage(m: FromWorker) {
    if (!m || typeof m !== "object") return;
    if (m.type === "ready") {
      if (this.state === "loading") {
        this.state = "ready";
        this.wasActive = true;
        this.loadMs = Number.isFinite(m.loadMs) ? Math.max(0, m.loadMs) : 0;
        this.publish();
      }
    } else if (m.type === "failed") {
      this.fail(m.code in FAIL_TEXT ? m.code : "runtime");
    } else if (m.type === "prob") {
      const h = this.pending.get(m.seq);
      if (!h) return;
      this.pending.delete(m.seq);
      h.p = typeof m.p === "number" && Number.isFinite(m.p) ? Math.max(0, Math.min(1, m.p)) : null;
      h.resolved = true;
      if (h.p !== null) {
        this.scored++;
        this.totalMs += Number.isFinite(m.ms) ? m.ms : 0;
      }
      const late = this.now() - h.sentAt > FRAME_DEADLINE_MS;
      this.drain();
      if (late) this.fail("late");
      else if (this.scored === 1) this.publish();
    }
  }

  /** One mic frame. Ownership stays with the caller until `deliver` hands it on. */
  push(frame: Float32Array) {
    const t = this.now();
    if (this.state === "loading" && t - this.startedAt > LOAD_DEADLINE_MS) this.fail("timeout");
    const head = this.held[0];
    if (this.state === "ready" && head && !head.resolved && t - head.sentAt > FRAME_DEADLINE_MS) this.fail("late");

    const h: Held = { frame, p: null, resolved: true, seq: 0, sentAt: t };
    const active = this.o.active ? this.o.active() : true;
    if (this.state === "ready" && this.worker && active) {
      if (!this.wasActive) { this.post({ type: "reset" }); }
      h.resolved = false;
      h.seq = ++this.seq;
      this.pending.set(h.seq, h);
      const scored = Float32Array.from(this.o.denoise ? this.o.denoise(frame) : frame);
      this.held.push(h);
      this.post({ type: "frame", seq: h.seq, samples: scored }, [scored.buffer]);
      if (this.pending.size > MAX_PENDING) this.fail("backlog");
    } else {
      this.held.push(h);
    }
    this.wasActive = active;
    this.drain();
  }

  private post(m: ToWorker, transfer?: Transferable[]) {
    try { this.worker?.postMessage(m, transfer); } catch { this.fail("worker"); }
  }
}
