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
// altered model, a runtime that will not start), or is genuinely too slow,
// frames are delivered at once with `p = null` and the Capture judges them by
// energy, as it did before V5. `status()` / `onStatus` name the detector in use
// and why; it is never silent. Every frame is delivered exactly once, in order.
//
// **A busy main thread is not a slow worker.** While the page's main thread is
// stalled (a long task, a heavy render) the microphone's frames and the
// worker's verdicts both queue up behind it and arrive in a burst. Judging the
// worker by main-thread time would drop Silero for the session on a 700 ms
// stall that cost the worker a millisecond a frame. So:
//   - lateness is the WORKER's own receive-to-answer time (`total`), which a
//     stalled main thread cannot inflate;
//   - frames past the queue bound (MAX_PENDING) are not posted: they are
//     delivered with `p = null` (energy) in order, no failure, and the worker's
//     state is reset before the next posted frame (it missed audio);
//   - what is left is judged only over time the main thread was responsive
//     (the gap between two pushes at most RESPONSIVE_GAP_MS): a verdict that
//     does not come for HEAD_WAIT_MS of it, or a full queue for STRAIN_MS of it,
//     or LATE_RUN consecutive late verdicts, is a worker that cannot keep up.
//   - a gap of SLEEP_GAP_MS between frames (sleep/resume, or the mic itself
//     paused) is a reset, not a failure.
//
// **Failures are sticky, except the ones that may be a bad moment.** `late`,
// `backlog`, `timeout` and `worker` retry by themselves, at most MAX_AUTO_RETRIES
// times per mic session, after 30 s, 2 min, 10 min, and only when `canRetry()`
// says nothing is in flight (no utterance open, mic not OFF, Settings closed);
// the count resets after HEALTHY_RESET_MS of Silero working. `hash`, `no_worker`
// and `runtime` never retry on their own (the same bytes would fail the same
// way); `fetch` gets one more try when the mic is next opened. `retry()` is the
// owner's button and always tries. A page returning to the foreground re-arms
// (`rearm()`), free of the count.
//
// **Bounded.** At most MAX_PENDING frames (0.5 s of audio) wait on the worker.
// While the mic is OFF (`active() === false`) nothing is posted at all, and the
// worker's state is reset when listening resumes.
//
// **Seam for RNNoise (V14).** `denoise` runs on the copy the worker scores, never
// on the frame the Capture records: speech-to-text takes the echo-cancelled
// stream, not a denoised one (V0 section 2). A noise-suppression stage plugs in
// there, in front of the VAD.

import { FAIL_TEXT, type FailCode, type FromWorker, type ToWorker } from "./protocol";

export const MAX_PENDING = 16; // frames of audio waiting on the worker: 16 x 32 ms
export const FRAME_DEADLINE_MS = 500; // a verdict slower than this (receive to answer, in the worker) is late...
export const LATE_RUN = 8; // ...and this many in a row is a worker that cannot keep up
export const RESPONSIVE_GAP_MS = 100; // pushes this close are a main thread that is running
export const HEAD_WAIT_MS = 750; // the oldest frame unanswered this long (responsive time only)
export const STRAIN_MS = 1500; // the queue full this long (responsive time only)
export const SLEEP_GAP_MS = 2000; // no frames for this long: a resume, reset rather than fail
export const LOAD_DEADLINE_MS = 20_000; // fetch + hash + compile + warm-up
export const RETRY_DELAYS_MS: readonly number[] = [30_000, 120_000, 600_000];
export const MAX_AUTO_RETRIES = RETRY_DELAYS_MS.length;
export const HEALTHY_RESET_MS = 300_000;
const STATS_EVERY = 64; // verdicts between statistics publishes (about 2 s)

/** Failures that may be a bad moment rather than a verdict on this machine. */
export const TRANSIENT: ReadonlySet<FailCode> = new Set<FailCode>(["late", "backlog", "timeout", "worker"]);
/** Failures a retry cannot change: the same bytes or runtime would fail the same way. */
const PERMANENT: ReadonlySet<FailCode> = new Set<FailCode>(["hash", "no_worker", "runtime"]);

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
  /** An automatic retry is still to come. */
  retrying: boolean;
}

export interface StageOptions {
  /** Where frames go on, in order. `p` is null when the energy detector must judge it. */
  deliver: (frame: Float32Array, p: number | null) => void;
  createWorker?: () => WorkerLike;
  now?: () => number;
  /** False while nothing should be scored (the mic is OFF). */
  active?: () => boolean;
  /** True when nothing is in flight that a worker restart could disturb (no utterance open, Settings closed). */
  canRetry?: () => boolean;
  /** A noise-suppression stage in front of the VAD: given a frame, the one the VAD scores. */
  denoise?: (frame: Float32Array) => Float32Array;
  onStatus?: (status: DetectorStatus) => void;
}

interface Held {
  frame: Float32Array;
  p: number | null;
  resolved: boolean;
  seq: number;
}

const defaultWorker = (): WorkerLike =>
  new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }) as unknown as WorkerLike;

export class VadStage {
  private worker: WorkerLike | null = null;
  private state: DetectorState = "idle";
  private code: FailCode | null = null;
  /** A failure kept across stop(): the next mic open does not retry it on its own. */
  private permanent: FailCode | null = null;
  private held: Held[] = [];
  private pending = new Map<number, Held>();
  private seq = 0;
  private gen = 0;
  private startedAt = 0;
  private needReset = false;
  private wasActive = true;
  private scored = 0;
  private totalMs = 0;
  private loadMs = 0;
  private lastStatus = "";
  // Judging the worker over time the main thread was running.
  private lastPush: number | null = null;
  private headRef: Held | null = null;
  private headWait = 0;
  private strain = 0;
  private lateRun = 0;
  // Automatic retries.
  private autoRetries = 0;
  private retryAt = Infinity;
  private readySince: number | null = null;

  constructor(private o: StageOptions) {}

  private now() {
    return (this.o.now ?? (() => performance.now()))();
  }

  private retryPending() {
    return this.state === "failed" && this.code !== null && TRANSIENT.has(this.code) && this.autoRetries < MAX_AUTO_RETRIES;
  }

  status(): DetectorStatus {
    const silero = this.state === "ready";
    const retrying = this.retryPending();
    return {
      detector: silero ? "silero" : "energy",
      state: this.state,
      code: this.code,
      note: silero ? "Silero VAD, running in a worker"
        : this.state === "loading" ? "loading the Silero VAD (the energy detector listens meanwhile)"
        : this.state === "failed" && this.code ? FAIL_TEXT[this.code] + (retrying ? "; trying again later" : "")
        : "not started",
      frameMs: this.scored ? this.totalMs / this.scored : 0,
      scored: this.scored,
      loadMs: silero ? this.loadMs : 0,
      retrying,
    };
  }

  private publish() {
    const s = this.status();
    const key = `${s.detector}|${s.state}|${s.code}|${s.note}|${Math.round(s.frameMs * 100)}`;
    if (key === this.lastStatus) return;
    this.lastStatus = key;
    try { this.o.onStatus?.(s); } catch { /* an observer cannot change detection */ }
  }

  /**
   * The mic opened: begin loading the worker. Returns a token for the matching
   * `stop` (a stale run cannot stop the live one). Safe to call again while
   * loading or ready. A failure the same bytes would repeat (hash, runtime, no
   * Worker) is not retried here; the owner's `retry()` is the way out of those.
   */
  start(): number {
    const gen = ++this.gen;
    if (this.state === "loading" || this.state === "ready") return gen;
    if (this.permanent) { this.state = "failed"; this.code = this.permanent; this.publish(); return gen; }
    this.begin();
    return gen;
  }

  private begin() {
    this.release();
    this.code = null;
    this.scored = 0;
    this.totalMs = 0;
    this.loadMs = 0;
    this.startedAt = this.now();
    this.readySince = null;
    this.clearJudgement();
    this.retryAt = Infinity;
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

  private clearJudgement() {
    this.headRef = null;
    this.headWait = 0;
    this.strain = 0;
    this.lateRun = 0;
  }

  /** The owner's button: try the ML detector again, whatever failed. */
  retry() {
    if (this.state !== "failed") return;
    this.permanent = null;
    this.autoRetries = 0;
    this.begin();
  }

  /**
   * A moment the detector's timing history no longer describes (the page came
   * back to the foreground, the machine slept): forget it. Working, the worker
   * starts from a clean state; failed for a reason that may have been the pause,
   * it tries again now, without spending an automatic retry.
   */
  rearm() {
    if (this.state === "ready") {
      this.clearJudgement();
      this.needReset = true;
    } else if (this.state === "loading") {
      this.startedAt = this.now();
    } else if (this.state === "failed" && this.code !== null && TRANSIENT.has(this.code)) {
      this.begin();
    }
  }

  /** The mic closed: nothing more is scored, and what is held is dropped with it. `gen` is `start()`'s token. */
  stop(gen?: number) {
    if (gen !== undefined && gen !== this.gen) return;
    this.release();
    if (this.state === "failed" && this.code !== null && PERMANENT.has(this.code)) this.permanent = this.code;
    this.state = "idle";
    this.code = null;
    this.held = [];
    this.pending.clear();
    this.autoRetries = 0;
    this.retryAt = Infinity;
    this.lastPush = null;
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
    if (PERMANENT.has(code)) this.permanent = code;
    this.retryAt = TRANSIENT.has(code) && this.autoRetries < MAX_AUTO_RETRIES
      ? this.now() + RETRY_DELAYS_MS[this.autoRetries] : Infinity;
    this.readySince = null;
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
        this.needReset = false;
        this.loadMs = Number.isFinite(m.loadMs) ? Math.max(0, m.loadMs) : 0;
        this.readySince = this.now();
        this.publish();
      }
    } else if (m.type === "failed") {
      this.fail(typeof m.code === "string" && Object.hasOwn(FAIL_TEXT, m.code) ? m.code : "runtime");
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
      const took = Number.isFinite(m.total) ? (m.total as number) : Number.isFinite(m.ms) ? m.ms : 0;
      this.lateRun = took > FRAME_DEADLINE_MS ? this.lateRun + 1 : 0;
      this.drain();
      if (this.lateRun >= LATE_RUN) this.fail("late");
      else if (this.scored === 1 || this.scored % STATS_EVERY === 0) this.publish();
    }
  }

  /** One mic frame. Ownership stays with the caller until `deliver` hands it on. */
  push(frame: Float32Array) {
    const t = this.now();
    const dt = this.lastPush === null ? 0 : Math.max(0, t - this.lastPush);
    this.lastPush = t;
    if (dt >= SLEEP_GAP_MS) this.rearm();
    const responsive = dt <= RESPONSIVE_GAP_MS;

    if (this.state === "loading" && t - this.startedAt > LOAD_DEADLINE_MS) this.fail("timeout");
    if (this.state === "failed") this.maybeRetry(t);
    if (this.state === "ready" && this.readySince !== null && t - this.readySince >= HEALTHY_RESET_MS) {
      this.autoRetries = 0;
      this.readySince = t;
    }

    // The oldest unanswered frame: how long has it waited, counting only time the main thread ran?
    if (this.state === "ready") {
      const head = this.held[0];
      if (head && !head.resolved) {
        if (this.headRef !== head) { this.headRef = head; this.headWait = 0; }
        else if (responsive) this.headWait += dt;
        if (this.headWait > HEAD_WAIT_MS) this.fail("late");
      } else { this.headRef = null; this.headWait = 0; }
    }

    const h: Held = { frame, p: null, resolved: true, seq: 0 };
    const active = this.o.active ? this.o.active() : true;
    if (this.state === "ready" && this.worker && active) {
      if (this.pending.size >= MAX_PENDING) {
        // Past the bound: energy for this frame, in order, no failure. The worker
        // misses this audio, so it starts clean at the next frame it is given.
        this.needReset = true;
        if (responsive) this.strain += dt;
        if (this.strain > STRAIN_MS) { this.held.push(h); this.fail("backlog"); }
        else this.held.push(h);
      } else {
        if (this.strain > 0) this.strain = Math.max(0, this.strain - dt);
        if (!this.wasActive || this.needReset) { this.post({ type: "reset" }); this.needReset = false; }
        h.resolved = false;
        h.seq = ++this.seq;
        this.pending.set(h.seq, h);
        const scored = Float32Array.from(this.o.denoise ? this.o.denoise(frame) : frame);
        this.held.push(h);
        this.post({ type: "frame", seq: h.seq, samples: scored }, [scored.buffer]);
      }
    } else {
      this.held.push(h);
    }
    this.wasActive = active;
    this.drain();
  }

  private maybeRetry(t: number) {
    if (this.code === null || !TRANSIENT.has(this.code) || this.autoRetries >= MAX_AUTO_RETRIES || t < this.retryAt) return;
    if (this.o.active && !this.o.active()) return;
    if (this.o.canRetry && !this.o.canRetry()) return;
    this.autoRetries++;
    this.begin();
  }

  private post(m: ToWorker, transfer?: Transferable[]) {
    try { this.worker?.postMessage(m, transfer); } catch { this.fail("worker"); }
  }
}
