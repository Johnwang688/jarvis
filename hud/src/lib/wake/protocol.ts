// The messages between the wake stage (main thread) and the keyword worker, and
// the closed set of reasons the wake word can be missing. Reasons are codes, not
// error text: Settings must never show a URL, a path or the engine's own message.

import type { FailCode } from "../listen/protocol";

export type { FailCode };

export const WAKE_FAIL_TEXT: Readonly<Record<FailCode, string>> = {
  no_worker: "this window cannot start a Web Worker",
  worker: "the wake-word worker failed to start",
  fetch: "the wake-word model could not be loaded",
  hash: "the wake-word model did not match its pinned checksum",
  runtime: "the WebAssembly runtime could not start",
  timeout: "loading the wake-word model took too long",
  late: "the wake word answered too late",
  backlog: "the wake word fell behind the microphone",
};

export type WakeToWorker =
  | { type: "init" }
  /** Which keyword set listens, and how strict: "wake" is "Jarvis", "commands" is "Jarvis pause|resume|skip|stop". */
  | { type: "config"; set: "wake" | "commands"; threshold: number }
  | { type: "audio"; samples: Float32Array }
  /** A new stretch of audio (the microphone was off), not a continuation. */
  | { type: "reset" };

export type WakeFromWorker =
  | { type: "ready"; loadMs: number }
  | { type: "failed"; code: FailCode }
  | { type: "hit"; set: "wake" | "commands"; phrase: string }
  /** Cumulative since the worker started: samples consumed, and milliseconds spent running the network on them. */
  | { type: "progress"; samples: number; busyMs: number };
