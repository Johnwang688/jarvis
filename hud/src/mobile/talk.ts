// Push to talk on the phone (phone design §6, the Chats tab; P-7;
// jarvis/v2/remote_stt.py). A chat's message box takes a "Hold to talk"
// button: press and hold to record, release to upload and transcribe; the
// words land in the box. Nothing more.
//
//   - **Inserted, never sent.** The text is added to the chat's draft (after
//     what is already typed) and the box is drawn again. Send stays P-5's
//     tap. Nothing here posts a message, an answer or a decision: the only
//     request is `POST /m/api/stt`, and its reply is `{text}`.
//   - **Only in a chat's message box.** The Inbox's questions, decisions,
//     approval cards and proposals never get a mic (a voice note never
//     answers a question; speech never authorizes).
//   - **The microphone is asked for only on the press.** `getUserMedia` runs
//     inside the gesture's handler, never at start-up or on a render. Every
//     track is stopped and the audio context closed as soon as the recording
//     ends, however it ends (release, slide off, error, limit, a tab hidden,
//     a permission granted after the finger already left), so the iPhone's
//     microphone indicator goes out.
//   - **The desk's clip format.** Web Audio, resampled to 16 kHz mono
//     (lib/resample.ts, the desk's resampler) and written as 16-bit PCM WAV
//     (lib/vad.ts `wavBytes`, the desk's writer), so the desk's local
//     engine and speaker verification take it. Not MediaRecorder: Safari's
//     writes `audio/mp4` (AAC), which neither takes. The ScriptProcessor
//     path is used, not the desk's AudioWorklet: the worklet is a separate
//     file the remote listener does not serve (it serves only what the
//     build manifest lists for the phone page), and the phone's CSP is not
//     widened for a `blob:` module.
//   - At most `MAX_MS` (the server's `remote_stt.MAX_S`): the recording stops
//     itself there and is transcribed. Under `MIN_MS` nothing is sent.
//
// Every string is set with textContent.

import "./talk.css";
import { MicFramer, OUT_RATE } from "../lib/resample";
import { wavBytes } from "../lib/vad";

export const STT_URL = "/m/api/stt";
/** remote_stt.MAX_S. */
export const MAX_MS = 60_000;
export const MIN_MS = 300;
export const RATE = OUT_RATE;
export const MAX_SAMPLES = (RATE * MAX_MS) / 1000;
/** A slide this far past the button's edge cancels (px). */
export const SLOP_PX = 24;

export type TalkPhase = "idle" | "starting" | "listening" | "transcribing";

/** The words the button and its line show, pinned by talk.test.ts. */
export const WORDS = {
  idle: "Hold to talk",
  starting: "Opening the mic…",
  listening: "Listening · release",
  transcribing: "Transcribing…",
  listeningLine: "Listening. Release to transcribe; slide off to cancel.",
  transcribingLine: "Transcribing. The words go in the box, unsent.",
  added: "Added to the box, unsent. Read it, then tap Send.",
  nothing: "Didn't catch that: nothing was added.",
  short: "Too short: hold the button while you speak.",
  early: "Released before the mic opened: hold the button while you speak.",
  cancelled: "Cancelled: nothing was recorded.",
  offline: "Offline: the clip was not sent.",
  denied: "Microphone permission denied. Allow the microphone for Jarvis in the iPhone's Settings, then hold again.",
  noMic: "No microphone was found.",
  noRecording: "This browser cannot record here.",
  limit: "One minute is the most a clip holds; it was transcribed as it stood.",
} as const;

type Json = Record<string, unknown>;

/** What the recorder needs of Web Audio (a fake in the tests). */
export interface AudioNodeLike { connect(node: unknown): void; disconnect(): void }
export interface ProcessorLike extends AudioNodeLike {
  onaudioprocess: ((e: { inputBuffer: { getChannelData(channel: number): Float32Array } }) => void) | null;
}
export interface AudioContextLike {
  sampleRate: number;
  destination: unknown;
  resume?: () => Promise<void>;
  createMediaStreamSource(stream: MediaStream): AudioNodeLike;
  createScriptProcessor(size: number, inputs: number, outputs: number): ProcessorLike;
  createGain(): AudioNodeLike & { gain: { value: number } };
  close(): Promise<void>;
}

export type Uploaded = { status: number; body: Json; offline?: boolean };

export interface TalkEnv {
  /** `navigator.mediaDevices`, or null where there is none. */
  media: () => { getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream> } | null;
  /** A new audio context, or null where Web Audio is missing. */
  context: () => AudioContextLike | null;
  upload: (wav: Blob) => Promise<Uploaded>;
  now: () => number;
  setTimer: (fn: () => void, ms: number) => unknown;
  clearTimer: (handle: unknown) => void;
}

export interface TalkHooks {
  /** The words for this chat's box: inserted, never sent. */
  insert: (threadId: string, text: string) => void;
  /** A 401 came back: the app checks the session. */
  unauthorized: () => void;
  online: () => boolean;
}

/** The clip upload: the WAV as the body, nothing else; "offline" when nothing reached Jarvis. */
export async function uploadClip(wav: Blob): Promise<Uploaded> {
  let res: Response;
  try {
    res = await fetch(STT_URL, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "audio/wav" }, body: wav,
    });
  } catch {
    return { status: 0, body: {}, offline: true };
  }
  let body: Json = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) body = parsed as Json;
  } catch { /* the proxy's own page: its status says enough */ }
  return { status: res.status, body };
}

export const browserTalkEnv: TalkEnv = {
  media: () => (typeof navigator !== "undefined" && navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === "function" ? navigator.mediaDevices : null),
  context: () => {
    const Ctor = (globalThis as { AudioContext?: new () => AudioContextLike; webkitAudioContext?: new () => AudioContextLike })
      .AudioContext ?? (globalThis as { webkitAudioContext?: new () => AudioContextLike }).webkitAudioContext;
    return Ctor ? new Ctor() : null;
  },
  upload: uploadClip,
  now: () => Date.now(),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** What a refused or failed upload says. */
export function uploadNote(r: Uploaded): string {
  const word = typeof r.body.error === "string" ? r.body.error.replace(/\s+/g, " ").trim().slice(0, 300) : "";
  if (r.offline || r.status === 0) return WORDS.offline;
  if (!word && (r.status === 502 || r.status === 503 || r.status === 504)) return WORDS.offline;
  const sentence = word ? word[0].toUpperCase() + word.slice(1) : `Not transcribed (${r.status})`;
  if (r.status === 429 || r.status === 422 || r.status === 415 || r.status === 400 || r.status === 413) {
    return sentence + ".";
  }
  return `Speech-to-text is unavailable: ${word || r.status}.`;
}

function micError(error: unknown): string {
  const name = error && typeof error === "object" && typeof (error as { name?: unknown }).name === "string"
    ? (error as { name: string }).name : "";
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") return WORDS.denied;
  if (name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError") return WORDS.noMic;
  return `The microphone could not start${name ? ` (${name})` : ""}.`;
}

interface Recording {
  gen: number;
  threadId: string;
  ctx: AudioContextLike;
  stream: MediaStream | null;
  nodes: AudioNodeLike[];
  frames: Float32Array[];
  samples: number;
  startedAt: number;
  limit: unknown;
}

export class Talk {
  phase: TalkPhase = "idle";
  /** The last outcome, shown under the button. */
  note = "";
  /** The chat the button speaks for (set by `view`, kept while a clip is in flight). */
  threadId: string | null = null;
  readonly el: HTMLElement;
  readonly button: HTMLButtonElement;
  private readonly line: HTMLElement;
  private gen = 0;
  private rec: Recording | null = null;
  private detach: (() => void) | null = null;

  constructor(private readonly env: TalkEnv, private readonly hooks: TalkHooks) {
    this.el = document.createElement("div");
    this.el.className = "m-talk";
    this.el.dataset.testid = "m-talk";
    this.button = document.createElement("button");
    this.button.type = "button";
    this.button.className = "tap quiet m-mic";
    this.button.dataset.testid = "m-mic";
    this.line = document.createElement("p");
    this.line.className = "m-notice m-talk-line";
    this.line.dataset.testid = "m-talk-line";
    this.line.setAttribute("role", "status");
    this.el.append(this.button, this.line);
    this.bind();
    this.draw();
  }

  /** The button and its line, for `threadId`'s box. The same node every time, so a redraw of the box
   * during a press never takes the button from under the finger. */
  view(threadId: string): HTMLElement {
    if (this.phase === "idle") {
      if (this.threadId !== threadId) this.note = "";
      this.threadId = threadId;
    }
    this.draw();
    return this.el;
  }

  /** The owner pressed: open the mic, inside the gesture. */
  press(): void {
    const threadId = this.threadId;
    if (this.phase !== "idle" || !threadId) return;
    if (!this.hooks.online()) { this.note = WORDS.offline; this.draw(); return; }
    const media = this.env.media();
    const ctx = media ? this.env.context() : null;
    if (!media || !ctx) { this.note = WORDS.noRecording; this.draw(); return; }
    const gen = ++this.gen;
    // Created and resumed in the gesture: iOS starts a context made later suspended.
    try { void ctx.resume?.()?.catch?.(() => {}); } catch { /* already running */ }
    this.rec = { gen, threadId, ctx, stream: null, nodes: [], frames: [], samples: 0, startedAt: this.env.now(), limit: null };
    this.phase = "starting";
    this.note = "";
    this.draw();
    let asked: Promise<MediaStream>;
    try {
      asked = media.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (error) {
      asked = Promise.reject(error);
    }
    asked.then((stream) => this.opened(gen, stream), (error) => this.failed(gen, micError(error)));
  }

  private opened(gen: number, stream: MediaStream): void {
    const rec = this.rec;
    if (!rec || rec.gen !== gen || this.phase !== "starting") {
      // The finger left before the permission came back: nothing records.
      stopTracks(stream);
      return;
    }
    rec.stream = stream;
    try {
      const source = rec.ctx.createMediaStreamSource(stream);
      const node = rec.ctx.createScriptProcessor(4096, 1, 1);
      const quiet = rec.ctx.createGain();
      quiet.gain.value = 0;             // pulled, never played back
      const framer = new MicFramer(rec.ctx.sampleRate, (frame) => this.frame(rec, frame));
      node.onaudioprocess = (e) => {
        if (this.rec === rec && this.phase === "listening") framer.pushMono(Float32Array.from(e.inputBuffer.getChannelData(0)));
      };
      source.connect(node);
      node.connect(quiet);
      quiet.connect(rec.ctx.destination);
      rec.nodes = [source, node, quiet];
    } catch (error) {
      this.failed(gen, micError(error));
      return;
    }
    rec.startedAt = this.env.now();
    rec.limit = this.env.setTimer(() => {
      if (this.rec === rec && this.phase === "listening") void this.release(true);
    }, MAX_MS);
    this.phase = "listening";
    this.draw();
  }

  private frame(rec: Recording, frame: Float32Array): void {
    if (rec.samples >= MAX_SAMPLES) return;
    const kept = frame.length > MAX_SAMPLES - rec.samples ? frame.slice(0, MAX_SAMPLES - rec.samples) : frame;
    rec.frames.push(kept);
    rec.samples += kept.length;
    if (rec.samples >= MAX_SAMPLES) void this.release(true);
  }

  private failed(gen: number, note: string): void {
    if (!this.rec || this.rec.gen !== gen) return;
    this.close();
    this.phase = "idle";
    this.note = note;
    this.draw();
  }

  /** Stop the microphone: every track, every node, the context. */
  private close(): Recording | null {
    const rec = this.rec;
    this.rec = null;
    if (!rec) return null;
    if (rec.limit !== null) this.env.clearTimer(rec.limit);
    for (const node of rec.nodes) {
      try { (node as ProcessorLike).onaudioprocess = null; } catch { /* not a processor */ }
      try { node.disconnect(); } catch { /* already gone */ }
    }
    if (rec.stream) stopTracks(rec.stream);
    try { void rec.ctx.close().catch(() => {}); } catch { /* already closed */ }
    return rec;
  }

  /** The finger slid off, the press was cancelled, or the app went to the background: nothing is sent. */
  cancel(): void {
    if (this.phase !== "starting" && this.phase !== "listening") return;
    this.close();
    this.phase = "idle";
    this.note = WORDS.cancelled;
    this.draw();
  }

  /** The finger lifted (or the limit came): stop, then transcribe into the box. */
  async release(limit = false): Promise<void> {
    if (this.phase === "starting") {
      this.close();
      this.phase = "idle";
      this.note = WORDS.early;
      this.draw();
      return;
    }
    if (this.phase !== "listening") return;
    const rec = this.close();
    if (!rec) return;
    const ms = (rec.samples / RATE) * 1000;
    if (ms < MIN_MS) {
      this.phase = "idle";
      this.note = WORDS.short;
      this.draw();
      return;
    }
    const samples = new Float32Array(rec.samples);
    let at = 0;
    for (const frame of rec.frames) { samples.set(frame, at); at += frame.length; }
    rec.frames = [];
    const wav = new Blob([wavBytes(samples, RATE)], { type: "audio/wav" });
    this.phase = "transcribing";
    this.note = "";
    this.draw();
    const got = await this.env.upload(wav);
    this.phase = "idle";
    if (got.status === 401) {
      this.note = "Not transcribed: signed out.";
      this.draw();
      this.hooks.unauthorized();
      return;
    }
    if (got.status === 200 && typeof got.body.text === "string") {
      const text = got.body.text.trim();
      if (text) {
        this.hooks.insert(rec.threadId, text);
        this.note = limit ? WORDS.limit + " " + WORDS.added : WORDS.added;
      } else {
        this.note = WORDS.nothing;
      }
    } else {
      this.note = uploadNote(got);
    }
    this.draw();
  }

  private draw(): void {
    const b = this.button;
    b.textContent = WORDS[this.phase];
    b.dataset.phase = this.phase;
    b.setAttribute("aria-pressed", String(this.phase === "listening" || this.phase === "starting"));
    b.setAttribute("aria-label", this.phase === "idle" ? "Hold to talk: the words go in the box, unsent" : WORDS[this.phase]);
    b.disabled = this.phase === "transcribing" || (this.phase === "idle" && !this.hooks.online());
    const line = this.phase === "listening" ? WORDS.listeningLine
      : this.phase === "transcribing" ? WORDS.transcribingLine : this.note;
    this.line.textContent = line;
    this.line.hidden = !line;
  }

  private bind(): void {
    const b = this.button;
    const inside = (e: { clientX?: number; clientY?: number }): boolean => {
      if (typeof e.clientX !== "number" || typeof e.clientY !== "number") return true;
      const r = b.getBoundingClientRect();
      if (!r.width && !r.height) return true;
      return e.clientX >= r.left - SLOP_PX && e.clientX <= r.right + SLOP_PX
        && e.clientY >= r.top - SLOP_PX && e.clientY <= r.bottom + SLOP_PX;
    };
    const end = () => { this.detach?.(); this.detach = null; };
    const up = () => { end(); void this.release(); };
    const off = () => { end(); this.cancel(); };
    const move = (e: Event) => { if (!inside(e as MouseEvent)) off(); };
    b.addEventListener("pointerdown", (e) => {
      if (b.disabled || this.phase !== "idle") return;
      e.preventDefault();
      // The release is heard wherever the finger lifts, even if the box was drawn again meanwhile.
      const win = b.ownerDocument.defaultView ?? window;
      win.addEventListener("pointerup", up);
      win.addEventListener("pointercancel", off);
      win.addEventListener("pointermove", move);
      this.detach = () => {
        win.removeEventListener("pointerup", up);
        win.removeEventListener("pointercancel", off);
        win.removeEventListener("pointermove", move);
      };
      this.press();
    });
    b.addEventListener("contextmenu", (e) => e.preventDefault());
    // A keyboard (or a switch control) holds Space or Enter instead.
    b.addEventListener("keydown", (e) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); this.press(); }
    });
    b.addEventListener("keyup", (e) => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); void this.release(); }
    });
  }

  /** The page went to the background: a recording in progress is dropped, its mic released. */
  hidden(): void {
    this.detach?.();
    this.detach = null;
    this.cancel();
  }
}

function stopTracks(stream: MediaStream): void {
  try {
    for (const track of stream.getTracks()) {
      try { track.stop(); } catch { /* already ended */ }
    }
  } catch { /* not a stream */ }
}
