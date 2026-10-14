// STT reliability (voice rework V2, plan §3.5, decision V-11).
//
// A finished utterance used to be one bare `fetch`: no deadline, no retry, the
// WAV discarded on any failure, and each reply placed whenever it happened to
// land, so two quick utterances could arrive in the wrong order. This module
// owns everything between "Capture produced a clip" and "its text is placed":
//
//   - every attempt has a client deadline, max(15 s, 2 x the clip), that fires
//     even when the transport ignores its abort signal;
//   - a retryable failure (timeout, network, 408/429/5xx) is retried exactly
//     once, automatically, after 2 s. A 4xx other than 408/429 is not retried:
//     the same bytes would be refused again;
//   - a clip that still fails is KEPT, in memory only, so the owner can press
//     Retry. One press is one attempt; the owner decides whether to try again;
//   - clips are bounded (20, 10 minutes of audio, 32 MiB). Past that the oldest
//     failed clip is dropped (and the diagnostics row says so); a new clip that
//     cannot fit is refused loudly rather than evicting audio still in flight;
//   - text is placed in capture order (`seq`). A reply that lands early waits
//     for earlier clips that are still in flight. A clip that has *failed* does
//     not hold the line: later text is placed, and the failed clip's text goes
//     in when its retry lands, marked `late`.
//
// Nothing here persists or uploads anything beyond the one `transcribe` call
// the caller supplies, and a retry is refused while `canUpload()` is false (the
// mic is OFF, the window crashed, a card is up) -- the same fail-closed rule as
// the first attempt.

import type { ClaimSource, VoiceDiagnostics } from "./voiceDiagnostics";

export const STT_MAX_CLIPS = 20;
export const STT_MAX_CLIP_MS = 10 * 60_000;
export const STT_MAX_CLIP_BYTES = 32 * 1024 * 1024;
export const STT_RETRY_DELAY_MS = 2000;
export const STT_MIN_DEADLINE_MS = 15_000;
/** Automatic retries per clip. The owner's Retry button is separate and explicit. */
export const STT_AUTO_RETRIES = 1;

export const sttDeadlineMs = (clipMs: number) =>
  Math.max(STT_MIN_DEADLINE_MS, 2 * (Number.isFinite(clipMs) ? Math.max(0, clipMs) : 0));

export type SttState = "transcribing" | "waiting" | "ready" | "failed";

export interface SttClipView {
  id: number;
  diag: number;
  seq: number;
  ms: number;
  bytes: number;
  state: SttState;
  attempts: number;
  error: string;
}
export interface SttSnapshot { clips: readonly SttClipView[]; pending: number }

export interface SttPlaced<M> {
  id: number;
  seq: number;
  text: string;
  ms: number;
  source: ClaimSource;
  meta: M;
  /** The diagnostics row; the placer settles it with the real outcome. */
  diag: number;
  latencyMs: number;
  /** True when this clip failed first, so later text was placed before it. */
  late: boolean;
  /** True when the owner's Retry button produced this text. */
  retried: boolean;
}

export type SttEvent =
  | { kind: "busy"; pending: number }
  | { kind: "retrying" }
  | { kind: "failed"; id: number; message: string; kept: boolean }
  | { kind: "empty" }
  | { kind: "blocked"; message: string };

/** What the daemon says about a transcript (V13): the text and which backend heard it. */
export interface SttReply { text: string; backend?: unknown; /** Present when local was meant to serve the clip and did not. */ fellBack?: boolean }

export interface SttOptions<M> {
  /** `budgetMs` is this attempt's whole client deadline: the daemon fits its model chain inside it. */
  transcribe: (blob: Blob, o: { signal: AbortSignal; utteranceId: number; seq: number; budgetMs: number }) => Promise<string | SttReply>;
  /** Called in `seq` order with every text that is ready to be placed. */
  place: (batch: SttPlaced<M>[]) => void | Promise<void>;
  diagnostics: VoiceDiagnostics;
  /** Consulted before every retry (automatic or explicit), never the first attempt. */
  canUpload?: () => boolean;
  onEvent?: (e: SttEvent) => void;
  now?: () => number;
}

interface Entry<M> {
  id: number; seq: number; diag: number; ms: number; source: ClaimSource; meta: M;
  blob: Blob | null; bytes: number;
  state: SttState; attempts: number; autoRetries: number; error: string; text: string;
  startedAt: number; hadFailure: boolean; retried: boolean; cancelled: boolean;
  ac: AbortController | null; retryTimer: ReturnType<typeof setTimeout> | null;
}

class Deadline extends Error {}

/** A retry could change the answer: the transport failed or the service was busy. */
export function retryable(error: unknown): boolean {
  if (error instanceof Deadline) return true;
  const status = error && typeof error === "object" ? (error as { status?: unknown }).status : undefined;
  if (typeof status !== "number") return true; // a network error has no status
  return status === 408 || status === 429 || status >= 500;
}

function describe(error: unknown, deadlineMs: number): string {
  if (error instanceof Deadline) return `timed out after ${Math.round(deadlineMs / 1000)} s`;
  const m = error && typeof error === "object" ? (error as { message?: unknown }).message : undefined;
  return typeof m === "string" && m ? m.slice(0, 200) : "transcription failed";
}

export class SttAssembler<M = undefined> {
  private entries = new Map<number, Entry<M>>();
  private nextId = 0;
  private listeners = new Set<() => void>();
  private chain: Promise<void> = Promise.resolve();
  private waiters = new Map<number, (() => void)[]>();
  private value: SttSnapshot = { clips: [], pending: 0 };
  private now: () => number;

  constructor(private o: SttOptions<M>) { this.now = o.now ?? (() => performance.now()); }

  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  snapshot = () => this.value;
  /**
   * Resolves once the clip's fate is known: its text was placed (the placer has
   * run), it came back empty, it failed for good (after the one automatic
   * retry), or it was discarded. An unknown or already settled clip resolves
   * at once. The caller uses it for work that must wait for the outcome (V6's
   * barge-in resumes speech only then); a later explicit Retry is not waited on.
   */
  whenSettled(id: number): Promise<void> {
    const e = this.entries.get(id);
    if (!e || e.state === "failed") return Promise.resolve();
    return new Promise<void>(done => { this.waiters.set(id, [...(this.waiters.get(id) ?? []), done]); });
  }
  private settle(id: number) {
    const w = this.waiters.get(id);
    this.waiters.delete(id);
    for (const fn of w ?? []) fn();
  }

  /** The kept audio for a clip, for Play. Null once it is placed, discarded or evicted. */
  clip(id: number): Blob | null { return this.entries.get(id)?.blob ?? null; }

  private emit() {
    const all = [...this.entries.values()].filter(e => e.blob).sort((a, b) => a.seq - b.seq);
    this.value = {
      pending: all.filter(e => e.state === "transcribing" || e.state === "waiting").length,
      clips: all.map(e => ({ id: e.id, diag: e.diag, seq: e.seq, ms: e.ms, bytes: e.bytes, state: e.state, attempts: e.attempts, error: e.error })),
    };
    for (const fn of this.listeners) { try { fn(); } catch { /* observers cannot affect transcription */ } }
  }

  private event(e: SttEvent) { try { this.o.onEvent?.(e); } catch { /* ditto */ } }

  /**
   * Make room for `extra`, evicting the oldest failed clips -- but only if that
   * admits it: the fit is computed first, so failed audio is never thrown away
   * for a clip that will be refused anyway.
   */
  private makeRoom(extraBytes: number, extraMs: number, extraClips: number): boolean {
    const held = [...this.entries.values()].filter(e => e.blob);
    const victims = held.filter(e => e.state === "failed").sort((a, b) => a.seq - b.seq);
    let clips = held.length + extraClips;
    let ms = held.reduce((n, e) => n + e.ms, 0) + extraMs;
    let bytes = held.reduce((n, e) => n + e.bytes, 0) + extraBytes;
    const evict: Entry<M>[] = [];
    while (clips > STT_MAX_CLIPS || ms > STT_MAX_CLIP_MS || bytes > STT_MAX_CLIP_BYTES) {
      const v = victims.shift();
      if (!v) return false;
      evict.push(v); clips--; ms -= v.ms; bytes -= v.bytes;
    }
    for (const v of evict) this.drop(v);
    return true;
  }

  private drop(e: Entry<M>) {
    this.entries.delete(e.id);
    e.blob = null;
    this.o.diagnostics.keep(e.diag, false);
  }

  /** Queue a finished clip. Returns its id, or null when the backlog is full (visible, never silent). */
  submit(blob: Blob, ms: number, source: ClaimSource, meta: M): number | null {
    // The incoming clip counts against every cap. Only a clip that cannot fit
    // while other audio is held is refused; a lone oversize clip is still sent
    // once, and is dropped (and said to be) the moment it fails.
    if (!this.makeRoom(blob.size, ms, 1) && [...this.entries.values()].some(e => e.blob)) {
      this.o.diagnostics.add(ms, source, "stt_failed");
      return null;
    }
    const id = ++this.nextId;
    const e: Entry<M> = {
      id, seq: id, diag: this.o.diagnostics.add(ms, source, "transcribing"), ms, source, meta,
      blob, bytes: blob.size, state: "transcribing", attempts: 0, autoRetries: 0, error: "", text: "",
      startedAt: this.now(), hadFailure: false, retried: false, cancelled: false, ac: null, retryTimer: null,
    };
    this.entries.set(id, e);
    this.o.diagnostics.keep(e.diag, true);
    void this.attempt(e, false);
    return id;
  }

  private async attempt(e: Entry<M>, explicit: boolean) {
    const blob = e.blob;
    if (!blob || e.cancelled) return;
    e.state = "transcribing"; e.attempts++; e.error = "";
    const ac = new AbortController(); e.ac = ac;
    const deadline = sttDeadlineMs(e.ms);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { reject(new Deadline()); ac.abort(); }, deadline);
    });
    this.emit();
    this.event({ kind: "busy", pending: this.value.pending });
    let text: unknown = "";
    let failure: unknown = null;
    let served: SttReply | null = null;
    try {
      const reply = await Promise.race([this.o.transcribe(blob, { signal: ac.signal, utteranceId: e.id, seq: e.seq, budgetMs: deadline }), expired]);
      if (reply && typeof reply === "object") { served = reply; text = reply.text; } else text = reply;
    } catch (error) { failure = error ?? new Error("transcription failed"); }
    clearTimeout(timer);
    if (e.cancelled) return;
    if (failure) {
      if (!explicit && e.autoRetries < STT_AUTO_RETRIES && retryable(failure)) {
        e.autoRetries++; e.state = "waiting"; e.error = describe(failure, deadline);
        e.retryTimer = setTimeout(() => {
          e.retryTimer = null;
          if (e.cancelled) return;
          if (this.o.canUpload && !this.o.canUpload()) { this.fail(e, "not retried: dictation is paused"); return; }
          void this.attempt(e, false);
        }, STT_RETRY_DELAY_MS);
        this.emit();
        this.event({ kind: "retrying" });
        return;
      }
      this.fail(e, describe(failure, deadline));
      return;
    }
    if (served) this.o.diagnostics.served(e.diag, served.backend, served.fellBack === true);
    if (typeof text !== "string" || !text.trim()) {
      this.entries.delete(e.id); e.blob = null;
      this.o.diagnostics.keep(e.diag, false);
      this.o.diagnostics.finish(e.diag, "empty_transcript", this.now() - e.startedAt);
      this.emit(); this.event({ kind: "empty" }); this.settle(e.id); this.release();
      return;
    }
    e.text = text; e.state = "ready"; e.error = "";
    this.emit(); this.release();
  }

  private fail(e: Entry<M>, message: string) {
    e.state = "failed"; e.error = message; e.hadFailure = true;
    this.o.diagnostics.finish(e.diag, "stt_failed", this.now() - e.startedAt);
    this.makeRoom(0, 0, 0); // may evict this very clip when it alone is over the caps
    const kept = this.entries.has(e.id);
    this.emit();
    this.event({ kind: "failed", id: e.id, message, kept });
    this.settle(e.id);
    this.release();
  }

  /** The owner pressed Retry: exactly one more attempt for a kept, failed clip. */
  retry(id: number): boolean {
    const e = this.entries.get(id);
    if (!e || e.state !== "failed" || !e.blob) return false;
    if (this.o.canUpload && !this.o.canUpload()) { this.event({ kind: "blocked", message: "NOT RETRIED · MIC OFF OR PAUSED" }); return false; }
    this.o.diagnostics.reopen(e.diag);
    e.startedAt = this.now();
    e.retried = true;
    void this.attempt(e, true);
    return true;
  }

  /** The owner dropped a kept clip. Its audio is released at once. */
  discard(id: number) {
    const e = this.entries.get(id);
    if (!e) return;
    e.cancelled = true; e.ac?.abort(); clearTimeout(e.retryTimer ?? undefined);
    if (e.state !== "failed") this.o.diagnostics.finish(e.diag, "cancelled", this.now() - e.startedAt);
    this.drop(e);
    this.emit(); this.settle(id); this.release();
  }

  /** Place, in capture order, every ready text no earlier clip is still waiting on. */
  private release() {
    const batch: Entry<M>[] = [];
    for (const e of [...this.entries.values()].sort((a, b) => a.seq - b.seq)) {
      if (e.state === "transcribing" || e.state === "waiting") break;
      if (e.state === "ready") batch.push(e);
    }
    if (!batch.length) return;
    const items: SttPlaced<M>[] = batch.map(e => ({
      id: e.id, seq: e.seq, text: e.text.trim(), ms: e.ms, source: e.source, meta: e.meta, diag: e.diag,
      latencyMs: this.now() - e.startedAt, late: e.hadFailure, retried: e.retried,
    }));
    for (const e of batch) { this.entries.delete(e.id); e.blob = null; this.o.diagnostics.keep(e.diag, false); }
    this.emit();
    this.chain = this.chain.then(() => this.o.place(items)).catch(() => {
      for (const i of items) this.o.diagnostics.finish(i.diag, "unplaced", i.latencyMs);
    }).then(() => { for (const i of items) this.settle(i.id); });
  }

  /** Resolves when every placement handed over so far has run (tests, teardown). */
  settled() { return this.chain; }
}

/** Where and in which mode a clip was captured (the assembler's `meta`). */
export interface SttCapture {
  mode: "auto" | "review" | "off";
  /** `draftKey` of the selected chat at capture; null with no chat. */
  chat: string | null;
  /**
   * V3: the words are for the selected chat's box whatever else holds -- a
   * follow-up window named a pane that is gone or hidden by the time it was
   * claimed, so where they belong is unclear.
   */
  boxOnly?: boolean;
  /** The continuation window this message armed (`Capture`'s token), settled when it is placed. */
  continuation?: number | null;
}

/** What is true when a transcript lands. */
export interface SttLanding {
  mode: "auto" | "review" | "off";
  /** `draftKey` of the selected chat now; null with no chat. */
  chat: string | null;
  /** The selected chat is running a turn. */
  busy: boolean;
  /**
   * The running turn is this chat's own thread's, so a send is taken by it as
   * a steer (V3). A busy chat whose turn is something else -- a first send
   * still opening its thread, a turn tracked from another thread -- would
   * start a new turn instead, and that is not something to do unseen.
   */
  steers: boolean;
  /** An authorization card is waiting. */
  card: boolean;
}

/**
 * AUTO sends a transcript on its own only when nothing has moved since the
 * words were said: it was AUTO then and still is, the same chat is still
 * selected, no card is up, and it is neither a late text nor the product of
 * the owner's Retry. A chat running a turn is no obstacle (V3, decision V-10):
 * the words steer that turn, as typed words do, provided the turn really is
 * that chat's own (`steers`). Anything else puts the words in the box, unsent:
 * a late or retried transcript must never become a message, to a chat or into
 * a running turn, that the owner no longer expects, and a card is answered by
 * hand, never by what the microphone heard.
 */
export function placementFor(captured: SttCapture, landing: SttLanding, item: { late: boolean; retried: boolean }): "send" | "review" {
  const same = captured.chat !== null && captured.chat === landing.chat;
  return captured.mode === "auto" && landing.mode === "auto" && same && !captured.boxOnly && (!landing.busy || landing.steers) && !landing.card
    && !item.late && !item.retried ? "send" : "review";
}

/** Play a kept clip. Web Audio, not <audio src=blob:>: the HUD's CSP media-src is 'self'. */
export async function playClip(blob: Blob): Promise<void> {
  const Ctx: typeof AudioContext | undefined = (globalThis as any).AudioContext ?? (globalThis as any).webkitAudioContext;
  if (!Ctx) return;
  const ctx = new Ctx();
  try {
    const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    await new Promise<void>(done => { source.onended = () => done(); source.start(); });
  } finally { void ctx.close().catch(() => {}); }
}
