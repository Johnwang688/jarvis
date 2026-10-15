// The wake stage (voice rework V11, plan §3.6): the microphone's frames go to the
// keyword worker, and a hit becomes a call, `onWake()` for "Jarvis" or
// `onCommand(c)` for a playback command. Nothing else leaves this stage and no
// audio leaves the window: the worker is same-origin and its only output is a hit.
//
//   mic frames ──> push() ──copy──> keyword worker ──> hit ──> onWake / onCommand
//
// **It decides nothing about what a wake hit means.** `onWake` is the Capture's
// `onWake` path (V9): the self-voice gate, the suppression rules and the claim logic
// all stay where they were. `onCommand` is `CommandRouter.fromLocal`, which refuses
// under a card unless `setLocalUnderCard` was turned on. A hit is a fact about a sound.
//
// **Bounded, and visible when it cannot run.** No cloud recognizer stands behind
// this: when the worker cannot start (no Worker, a failed script, a missing or
// altered model file, a runtime that will not start), takes over LOAD_DEADLINE_MS to
// load, or falls more than MAX_BACKLOG_MS of audio behind, the stage is `failed` with
// a code, saying so in Settings -> Voice, and saying "Jarvis" does nothing until the
// owner tries again (`retry()`). The talk key and the orb still work. A failure is
// sticky: nothing flips it back by itself, so it never flaps between listening and not.
//
// While the microphone is OFF (`active() === false`) nothing is posted, and the
// worker forgets its audio when listening resumes.
//
// Two keyword sets, one at a time: "wake" (Jarvis) normally, and "commands" ("Jarvis
// pause|resume|skip|stop") while an approval card is up and the owner left spoken
// commands on. Switching rebuilds the worker's stream (about 0.5 s of context lost).

import { WAKE_FAIL_TEXT, type FailCode, type WakeFromWorker, type WakeToWorker } from "./protocol";
import { SENSITIVITY, type PlaybackCommand, type Sensitivity } from "./keywords";

export const MAX_BACKLOG_MS = 2000; // audio waiting on the worker before it is judged too far behind
export const LOAD_DEADLINE_MS = 30_000; // fetch + checksums + compile + warm-up
export const SAMPLE_RATE = 16000;
/**
 * How long after a word the spotter may report it (V0: 0.35 s median, 0.64 s p95 after the word, plus the echo's own delay).
 * A hit with his voice audible anywhere in this look-back is treated as possibly his own word (App: `audibleWithin`).
 */
export const WAKE_LAG_MS = 1000;

export interface WakeWorkerLike {
  postMessage(message: WakeToWorker, transfer?: Transferable[]): void;
  terminate(): void;
  onmessage: ((e: MessageEvent<WakeFromWorker>) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onmessageerror: ((e: unknown) => void) | null;
}

export type WakeState = "idle" | "loading" | "ready" | "failed";

export interface WakeStatus {
  state: WakeState;
  code: FailCode | null;
  /** One sentence for Settings -> Voice: no URL, no path, no engine text. */
  note: string;
  /** The keyword set the worker is told to listen for. */
  set: "wake" | "commands";
  sensitivity: Sensitivity;
  /** Seconds of the worker's time per second of audio, as a percentage of one core (0 until measured). */
  cpuPct: number;
  loadMs: number;
  /** Wake hits delivered to `onWake` / command hits to `onCommand`. */
  wakeHits: number;
  commandHits: number;
}

export interface WakeStageOptions {
  onWake: () => void;
  onCommand?: (command: PlaybackCommand) => void;
  createWorker?: () => WakeWorkerLike;
  now?: () => number;
  /** False while nothing should be listened to (the mic is OFF, the window crashed). */
  active?: () => boolean;
  sensitivity?: () => Sensitivity;
  onStatus?: (status: WakeStatus) => void;
}

const defaultWorker = (): WakeWorkerLike =>
  new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }) as unknown as WakeWorkerLike;

export class WakeStage {
  private worker: WakeWorkerLike | null = null;
  private state: WakeState = "idle";
  private code: FailCode | null = null;
  private wanted: "wake" | "commands" = "wake";
  private sensitivity: Sensitivity;
  private startedAt = 0;
  private wasActive = true;
  private posted = 0; // samples sent since the worker started
  private processed = 0; // samples the worker has reported consuming since then
  private busyMs = 0;
  private audioMs = 0;
  private loadMs = 0;
  private wakeHits = 0;
  private commandHits = 0;
  private lastStatus = "";

  constructor(private o: WakeStageOptions) {
    this.sensitivity = o.sensitivity?.() ?? "normal";
  }

  private now() { return (this.o.now ?? (() => performance.now()))(); }

  status(): WakeStatus {
    const ready = this.state === "ready";
    return {
      state: this.state,
      code: this.code,
      note: ready ? "listening in this window, nothing sent anywhere"
        : this.state === "loading" ? "loading the wake-word model"
        : this.state === "failed" && this.code ? WAKE_FAIL_TEXT[this.code]
        : "not started",
      set: this.wanted,
      sensitivity: this.sensitivity,
      cpuPct: this.audioMs > 2000 ? (100 * this.busyMs) / this.audioMs : 0,
      loadMs: ready ? this.loadMs : 0,
      wakeHits: this.wakeHits,
      commandHits: this.commandHits,
    };
  }

  /** True while the wake word can be heard: the model is loaded and the microphone is on. */
  running(): boolean {
    return this.state === "ready" && (this.o.active ? this.o.active() : true);
  }

  private publish() {
    const s = this.status();
    const key = `${s.state}|${s.code}|${s.note}|${s.set}|${s.sensitivity}|${Math.round(s.cpuPct)}|${s.wakeHits}|${s.commandHits}`;
    if (key === this.lastStatus) return;
    this.lastStatus = key;
    try { this.o.onStatus?.(s); } catch { /* an observer cannot change listening */ }
  }

  /** Begin loading the worker (the microphone opened, or a retry). It does nothing while loading or listening. */
  start() {
    if (this.state === "loading" || this.state === "ready") return;
    this.code = null;
    this.startedAt = this.now();
    this.loadMs = 0;
    this.posted = 0;
    this.processed = 0;
    this.busyMs = 0;
    this.audioMs = 0;
    if (typeof Worker === "undefined" && !this.o.createWorker) { this.fail("no_worker"); return; }
    let w: WakeWorkerLike;
    try { w = (this.o.createWorker ?? defaultWorker)(); } catch { this.fail("worker"); return; }
    this.worker = w;
    this.state = "loading";
    w.onmessage = (e) => this.onMessage(e.data);
    w.onerror = () => this.fail("worker");
    w.onmessageerror = () => this.fail("worker");
    try { w.postMessage({ type: "init" }); } catch { this.fail("worker"); return; }
    this.publish();
  }

  /** Try again after a failure (Settings -> Voice offers it). */
  retry() {
    if (this.state === "failed") this.start();
  }

  /** The window closed the microphone: stop listening and release the worker. */
  stop() {
    this.release();
    this.state = "idle";
    this.code = null;
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
    this.publish();
  }

  private sendConfig() {
    this.post({ type: "config", set: this.wanted, threshold: SENSITIVITY[this.sensitivity].threshold });
  }

  /** The owner's sensitivity (Settings -> Voice). Takes effect on the next frame. */
  setSensitivity(s: Sensitivity) {
    if (!Object.hasOwn(SENSITIVITY, s) || s === this.sensitivity) return;
    this.sensitivity = s;
    if (this.state === "ready") this.sendConfig();
    this.publish();
  }

  /** Listen for the spoken playback commands instead of the wake word (an approval card is up). */
  setCommands(on: boolean) {
    const next = on ? "commands" : "wake";
    if (next === this.wanted) return;
    this.wanted = next;
    if (this.state === "ready") this.sendConfig();
    this.publish();
  }

  private onMessage(m: WakeFromWorker) {
    if (!m || typeof m !== "object") return;
    if (m.type === "ready") {
      if (this.state !== "loading") return;
      this.state = "ready";
      this.wasActive = true;
      this.loadMs = Number.isFinite(m.loadMs) ? Math.max(0, m.loadMs) : 0;
      this.sendConfig();
      this.publish();
    } else if (m.type === "failed") {
      this.fail(Object.hasOwn(WAKE_FAIL_TEXT, m.code) ? m.code : "runtime");
    } else if (m.type === "progress") {
      if (Number.isFinite(m.samples) && Number.isFinite(m.busyMs)) {
        this.processed = Math.max(this.processed, m.samples);
        this.busyMs = m.busyMs;
        this.audioMs = (m.samples * 1000) / SAMPLE_RATE;
      }
    } else if (m.type === "hit") {
      // A hit from a keyword set that is no longer wanted (the card came or went in between) is stale.
      if (this.state !== "ready" || m.set !== this.wanted) return;
      if (m.set === "wake") {
        this.wakeHits++;
        this.publish();
        try { this.o.onWake(); } catch { /* the capture's own guards */ }
      } else if (m.phrase === "pause" || m.phrase === "resume" || m.phrase === "skip" || m.phrase === "stop") {
        this.commandHits++;
        this.publish();
        try { this.o.onCommand?.(m.phrase); } catch { /* ditto */ }
      }
    }
  }

  /** One mic frame (16 kHz mono). Ownership stays with the caller; a copy is posted. */
  push(frame: Float32Array) {
    const t = this.now();
    if (this.state === "loading" && t - this.startedAt > LOAD_DEADLINE_MS) this.fail("timeout");
    const active = this.o.active ? this.o.active() : true;
    if (this.state === "ready" && this.worker && active) {
      if (!this.wasActive) this.post({ type: "reset" });
      const copy = Float32Array.from(frame);
      this.posted += copy.length;
      this.post({ type: "audio", samples: copy }, [copy.buffer]);
      // The worker answers every few thousand samples; more than MAX_BACKLOG_MS unanswered means it cannot keep up.
      if (this.posted - this.processed > (MAX_BACKLOG_MS * SAMPLE_RATE) / 1000) this.fail("backlog");
    }
    this.wasActive = active;
  }

  private post(m: WakeToWorker, transfer?: Transferable[]) {
    try { this.worker?.postMessage(m, transfer); } catch { this.fail("worker"); }
  }
}
