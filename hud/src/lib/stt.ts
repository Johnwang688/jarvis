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
// V4 (chunked STT, plan §3.5): a long message leaves the recorder as ordered
// chunks. They are one *group* here -- one diagnostics row, one meta, one
// placement -- whose chunks are clips like any other: each has its own `seq`,
// deadline, retry, kept audio and Retry button; at most STT_MAX_IN_FLIGHT are
// uploading at a time; and the group's text is placed once, when the message
// has ended and none of its chunks is still in flight: the chunk texts joined
// in order, the words an overlap repeats said once (joinChunks.ts). A chunk
// that failed does not hold the line, as before: the others are placed (marked
// `late`, so AUTO boxes them rather than sending half a message) and the
// failed one's words follow when its Retry lands.
//
// Nothing here persists or uploads anything beyond the one `transcribe` call
// the caller supplies, and a retry is refused while `canUpload()` is false (the
// mic is OFF, the window crashed, a card is up) -- the same fail-closed rule as
// the first attempt.

import type { ClaimSource, DetectorKind, DropReason, VoiceDiagnostics } from "./voiceDiagnostics";
import { dropLeadingOverlap, dropTrailingOverlap, overlapMerge } from "./joinChunks";

/** Where a chunk of a late message is missing: its words are not here, and Retry adds them at the end. */
export const GAP_MARK = "[…]";

export const STT_MAX_CLIPS = 20;
export const STT_MAX_CLIP_MS = 10 * 60_000;
export const STT_MAX_CLIP_BYTES = 32 * 1024 * 1024;
export const STT_RETRY_DELAY_MS = 2000;
export const STT_MIN_DEADLINE_MS = 15_000;
/** Automatic retries per clip. The owner's Retry button is separate and explicit. */
export const STT_AUTO_RETRIES = 1;
/** Chunks uploading at once (plan §3.5). The class itself is unbounded unless told; the HUD passes this. */
export const STT_MAX_IN_FLIGHT = 2;

/** What the daemon answers (HTTP 422) to a clip it refused as not the enrolled speaker's (V12). */
export const NOT_OWNER_MESSAGE = "not the enrolled speaker";

/** The daemon refused this voice: not a failure to retry, and nothing of it is kept. */
export function isNotOwner(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const e = error as { status?: unknown; message?: unknown };
  return e.status === 422 && e.message === NOT_OWNER_MESSAGE;
}

export const sttDeadlineMs = (clipMs: number) =>
  Math.max(STT_MIN_DEADLINE_MS, 2 * (Number.isFinite(clipMs) ? Math.max(0, clipMs) : 0));

export type SttState = "transcribing" | "waiting" | "ready" | "failed";

export interface SttClipView {
  id: number;
  /** The message this clip belongs to (its own id when it is a whole message). */
  group: number;
  /** Which chunk of its message this is, from 1, and how many the message has had so far. */
  part: number;
  of: number;
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
  /** Chunks whose text was joined into this one (1 for a clip that was never chunked). */
  parts: number;
}

export type SttEvent =
  | { kind: "busy"; pending: number }
  | { kind: "retrying" }
  | { kind: "failed"; id: number; message: string; kept: boolean }
  | { kind: "empty" }
  /** V12: `parts` chunk(s) of this message were refused as not the owner's voice. Their audio is gone. */
  | { kind: "rejected"; parts: number }
  | { kind: "blocked"; message: string };

/** What the daemon says about a transcript (V13): the text and which backend heard it. */
export interface SttReply { text: string; backend?: unknown; /** Present when local was meant to serve the clip and did not. */ fellBack?: boolean }

export interface SttOptions<M> {
  /** `budgetMs` is this attempt's whole client deadline: the daemon fits its model chain inside it. */
  transcribe: (blob: Blob, o: {
    signal: AbortSignal; utteranceId: number; seq: number; budgetMs: number;
    /** Report what speaker verification did for this clip (its answer's `verify`), for the diagnostics row. */
    verified: (kind: string) => void;
  }) => Promise<string | SttReply>;
  /** Called in `seq` order with every text that is ready to be placed. */
  place: (batch: SttPlaced<M>[]) => void | Promise<void>;
  diagnostics: VoiceDiagnostics;
  /**
   * Consulted before every upload (a chunk's first attempt too, since it may have
   * waited its turn) and every retry. `explicit` is the owner's own Retry press,
   * which the owner may make from Settings or with a picker open. `clip` says
   * what claimed it and what it was captured under: a clip the talk key
   * dictated while the mic was OFF may upload while it is OFF.
   */
  canUpload?: (explicit: boolean, clip: { source: ClaimSource; meta: M }) => boolean;
  /** Uploads at once; later chunks wait their turn, in `seq` order. Default: no limit. */
  maxInFlight?: number;
  onEvent?: (e: SttEvent) => void;
  now?: () => number;
}

interface Entry<M> {
  id: number; gid: number; seq: number; diag: number; ms: number; overlapMs: number; repeats: boolean; source: ClaimSource; meta: M;
  blob: Blob | null; bytes: number;
  state: SttState; attempts: number; autoRetries: number; error: string; text: string;
  startedAt: number; hadFailure: boolean; retried: boolean; cancelled: boolean;
  ac: AbortController | null; retryTimer: ReturnType<typeof setTimeout> | null; cancel: (() => void) | null;
}

/** One message: a clip, or the chunks a long dictation was cut into. */
interface Group<M> {
  /** Creation order; what `beginGroup` hands back. */
  key: number;
  /** What the daemon is told the utterance is: the id of its first chunk (0 until there is one). */
  uid: number;
  source: ClaimSource; meta: M;
  diag: number;
  /** More chunks may still be submitted. The group's text is not placed until it is closed. */
  open: boolean;
  members: number[];
  /** What each member said (and its overlap), kept while the group lives so a late retry can be joined to its neighbours. */
  parts: Map<number, { text: string; overlap: boolean }>;
  placed: Set<number>;
  lastAt: number;
  hadFailure: boolean;
  /** V12: chunks the daemon refused as not the owner's voice (their audio was dropped, not kept for Retry). */
  rejected: number;
  /** The message is not all of what was said (it was cut short): never sent on its own. */
  partial: boolean;
  /** Handed to `place`, which has not returned yet. */
  placing: number;
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

class Cancelled extends Error {}

/**
 * What a recorded chunk tells `submit` about itself. `repeats` matters: a cut at a pause leads the
 * next chunk with digital silence, not with words said twice, so the join must not look for an echo
 * there ("it was very" + "very good indeed" keeps both "very"s). Left out, `submit` assumes a repeat.
 */
export function chunkSubmitOptions(chunk: { overlapMs: number; repeats: boolean; detector?: DetectorKind }, group: number) {
  return { group, overlapMs: chunk.overlapMs, repeats: chunk.repeats, detector: chunk.detector };
}

export class SttAssembler<M = undefined> {
  private entries = new Map<number, Entry<M>>();
  private groups = new Map<number, Group<M>>();
  private nextId = 0;
  private groupSeq = 0;
  private listeners = new Set<() => void>();
  private chain: Promise<void> = Promise.resolve();
  private waiters = new Map<number, (() => void)[]>();
  private groupWaiters = new Map<number, (() => void)[]>();
  private queue: { e: Entry<M>; explicit: boolean }[] = [];
  private running = 0;
  private value: SttSnapshot = { clips: [], pending: 0 };
  private now: () => number;
  private max: number;

  constructor(private o: SttOptions<M>) {
    this.now = o.now ?? (() => performance.now());
    this.max = o.maxInFlight && o.maxInFlight > 0 ? o.maxInFlight : Infinity;
  }

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

  /** The same for a whole message (V4): closed, nothing in flight, its text placed or its chunks failed for good. */
  whenGroupSettled(gid: number): Promise<void> {
    const g = this.groups.get(gid);
    if (!g || this.groupDone(g)) return Promise.resolve();
    return new Promise<void>(done => { this.groupWaiters.set(gid, [...(this.groupWaiters.get(gid) ?? []), done]); });
  }
  private groupDone(g: Group<M>): boolean {
    if (g.open || g.placing > 0) return false;
    return !g.members.some(id => { const e = this.entries.get(id); return e && e.state !== "failed"; });
  }
  private settleGroup(g: Group<M>) {
    if (!this.groupDone(g)) return;
    const w = this.groupWaiters.get(g.key);
    this.groupWaiters.delete(g.key);
    for (const fn of w ?? []) fn();
    // Nothing of it is held any more (failed chunks included): forget its texts.
    if (!g.members.some(id => this.entries.has(id))) this.groups.delete(g.key);
  }

  /** The kept audio for a clip, for Play. Null once it is placed, discarded or evicted. */
  clip(id: number): Blob | null { return this.entries.get(id)?.blob ?? null; }

  private emit() {
    const all = [...this.entries.values()].filter(e => e.blob).sort((a, b) => a.seq - b.seq);
    this.value = {
      pending: all.filter(e => e.state === "transcribing" || e.state === "waiting").length,
      clips: all.map(e => {
        const g = this.groups.get(e.gid);
        return { id: e.id, group: g?.uid ?? e.id, part: g ? g.members.indexOf(e.id) + 1 : 1, of: g ? g.members.length : 1, diag: e.diag, seq: e.seq, ms: e.ms, bytes: e.bytes, state: e.state, attempts: e.attempts, error: e.error };
      }),
    };
    for (const fn of this.listeners) { try { fn(); } catch { /* observers cannot affect transcription */ } }
  }

  private event(e: SttEvent) { try { this.o.onEvent?.(e); } catch { /* ditto */ } }

  /** The diagnostics row says "audio kept" while any chunk of its message still holds audio. */
  private syncKept(g: Group<M> | undefined) {
    if (g && g.diag) this.o.diagnostics.keep(g.diag, g.members.some(id => !!this.entries.get(id)?.blob));
  }

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
    this.syncKept(this.groups.get(e.gid));
  }

  /**
   * A message that will arrive as several chunks (V4). Submit them with
   * `{ group }`, then `closeGroup` when the message has ended: nothing of it is
   * placed before, and everything is placed together after.
   */
  beginGroup(source: ClaimSource, meta: M): number {
    const key = ++this.groupSeq;
    this.groups.set(key, {
      key, uid: 0, source, meta, diag: 0, open: true, members: [], parts: new Map(), placed: new Set(),
      lastAt: this.now(), hadFailure: false, rejected: 0, partial: false, placing: 0,
    });
    return key;
  }

  /**
   * The message has ended; no chunk will follow. `late`: it ended before the
   * owner finished (the mic went OFF, a card appeared) or part of it was refused,
   * so what is placed is marked late and AUTO boxes it instead of sending it.
   */
  closeGroup(gid: number, opts: { late?: boolean } = {}) {
    const g = this.groups.get(gid);
    if (!g) return;
    if (opts.late) g.partial = true;
    g.open = false;
    if (!g.members.length) { this.settleGroup(g); this.groups.delete(gid); return; }
    this.release();
    this.settleGroup(g);
  }

  /**
   * Queue a finished clip -- or, with `opts.group`, one chunk of a message.
   * `overlapMs` is how much of its start repeats the previous chunk's end; `repeats`
   * (default: whenever there is overlap) says that overlap may hold words the previous
   * text also ends with, which the join then says once.
   * Returns its id, or null when the backlog is full (visible, never silent).
   */
  submit(blob: Blob, ms: number, source: ClaimSource, meta: M, opts: { group?: number; overlapMs?: number; repeats?: boolean; detector?: DetectorKind } = {}): number | null {
    let g = opts.group === undefined ? undefined : this.groups.get(opts.group);
    if (opts.group !== undefined && (!g || !g.open)) return null; // a closed message takes no more
    const overlapMs = Math.max(0, Math.min(ms, opts.overlapMs ?? 0));
    const repeats = overlapMs > 0 && (opts.repeats ?? true);
    // The incoming clip counts against every cap. Only a clip that cannot fit
    // while other audio is held is refused; a lone oversize clip is still sent
    // once, and is dropped (and said to be) the moment it fails.
    if (!this.makeRoom(blob.size, ms, 1) && [...this.entries.values()].some(e => e.blob)) {
      if (g) {
        // A chunk lost from the middle of a message: the rest must not go out as if it were all of it.
        g.hadFailure = true;
        if (g.diag) this.o.diagnostics.finish(g.diag, "stt_failed", this.now() - g.lastAt);
        else g.diag = this.o.diagnostics.add(ms - overlapMs, source, "stt_failed", opts.detector);
      } else this.o.diagnostics.add(ms, source, "stt_failed", opts.detector);
      return null;
    }
    const id = ++this.nextId;
    if (!g) {
      g = {
        key: ++this.groupSeq, uid: 0, source, meta, diag: 0, open: false, members: [], parts: new Map(), placed: new Set(),
        lastAt: this.now(), hadFailure: false, rejected: 0, partial: false, placing: 0,
      };
      this.groups.set(g.key, g);
    }
    if (!g.uid) g.uid = id;
    if (!g.diag) g.diag = this.o.diagnostics.add(ms - overlapMs, source, "transcribing", opts.detector);
    else this.o.diagnostics.extend(g.diag, ms - overlapMs);
    g.members.push(id);
    g.parts.set(id, { text: "", overlap: repeats });
    g.lastAt = this.now();
    const e: Entry<M> = {
      id, gid: g.key, seq: id, diag: g.diag, ms, overlapMs, repeats, source, meta: g.meta,
      blob, bytes: blob.size, state: "waiting", attempts: 0, autoRetries: 0, error: "", text: "",
      startedAt: this.now(), hadFailure: false, retried: false, cancelled: false, ac: null, retryTimer: null, cancel: null,
    };
    this.entries.set(id, e);
    this.syncKept(g);
    this.schedule(e, false);
    if (e.state === "waiting") this.emit(); // queued behind the uploads already running
    return id;
  }

  /** Start `e` when a slot is free, earliest `seq` first. */
  private schedule(e: Entry<M>, explicit: boolean) {
    this.queue.push({ e, explicit });
    this.pump();
  }

  private pump() {
    while (this.running < this.max && this.queue.length) {
      this.queue.sort((a, b) => a.e.seq - b.e.seq);
      const { e, explicit } = this.queue.shift()!;
      if (e.cancelled || !e.blob) continue;
      // A chunk that waited its turn is judged again: the mic may be OFF now, a card up.
      if (this.o.canUpload && !this.o.canUpload(explicit, e)) { this.fail(e, "not sent: dictation is paused"); continue; }
      this.running++;
      void this.attempt(e, explicit).finally(() => { this.running--; this.pump(); });
    }
  }

  private async attempt(e: Entry<M>, explicit: boolean) {
    const blob = e.blob;
    if (!blob || e.cancelled) return;
    const g = this.groups.get(e.gid);
    e.state = "transcribing"; e.attempts++; e.error = "";
    const ac = new AbortController(); e.ac = ac;
    const deadline = sttDeadlineMs(e.ms);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { reject(new Deadline()); ac.abort(); }, deadline);
    });
    const cancelled = new Promise<never>((_, reject) => { e.cancel = () => reject(new Cancelled()); });
    expired.catch(() => {}); cancelled.catch(() => {});
    this.emit();
    this.event({ kind: "busy", pending: this.value.pending });
    let text: unknown = "";
    let failure: unknown = null;
    let served: SttReply | null = null;
    try {
      const reply = await Promise.race([this.o.transcribe(blob, {
        signal: ac.signal, utteranceId: g?.uid ?? e.id, seq: e.seq, budgetMs: deadline,
        verified: kind => this.o.diagnostics.verified(g?.diag ?? e.diag, kind),
      }), expired, cancelled]);
      if (reply && typeof reply === "object") { served = reply; text = reply.text; } else text = reply;
    } catch (error) { failure = error ?? new Error("transcription failed"); }
    clearTimeout(timer);
    e.cancel = null;
    if (e.cancelled || failure instanceof Cancelled) return;
    if (failure && isNotOwner(failure)) { this.rejectOwner(e); return; }
    if (failure) {
      if (!explicit && e.autoRetries < STT_AUTO_RETRIES && retryable(failure)) {
        e.autoRetries++; e.state = "waiting"; e.error = describe(failure, deadline);
        e.retryTimer = setTimeout(() => {
          e.retryTimer = null;
          if (e.cancelled) return;
          if (this.o.canUpload && !this.o.canUpload(false, e)) { this.fail(e, "not retried: dictation is paused"); return; }
          this.schedule(e, false);
        }, STT_RETRY_DELAY_MS);
        this.emit();
        this.event({ kind: "retrying" });
        return;
      }
      this.fail(e, describe(failure, deadline));
      return;
    }
    if (served) this.o.diagnostics.served(e.diag, served.backend, served.fellBack === true);
    // A blank text is a normal answer for a chunk of mostly silence: it is
    // settled with the rest of its message (release).
    e.text = typeof text === "string" ? text : "";
    g?.parts.set(e.id, { text: e.text.trim(), overlap: e.repeats });
    // The words are in hand; the audio is only needed to try again, and a long
    // message may wait many chunks for its last one. Holding every chunk's audio
    // until then would make its length a memory bound, so it is let go here.
    e.state = "ready"; e.error = ""; e.blob = null;
    this.syncKept(g);
    this.emit(); this.release();
  }

  /**
   * The daemon refused this chunk as not the owner's voice (V12). It was never
   * transcribed or uploaded; here its audio is dropped at once -- it may be
   * someone else's, so it is not kept for Retry either. Like a chunk that failed,
   * it leaves a hole, so the rest of its message (if any) is placed as `late`:
   * boxed for review, never sent as if it were the whole of what was said.
   */
  private rejectOwner(e: Entry<M>) {
    const g = this.groups.get(e.gid);
    e.cancelled = true; e.state = "failed"; e.error = NOT_OWNER_MESSAGE; e.hadFailure = true;
    this.drop(e);
    if (g) { g.hadFailure = true; g.rejected++; this.o.diagnostics.rejected(g.diag, g.rejected); }
    this.emit();
    this.event({ kind: "rejected", parts: g?.rejected ?? 1 });
    this.settle(e.id);
    this.release();
    if (g) this.settleGroup(g);
  }

  private fail(e: Entry<M>, message: string) {
    const g = this.groups.get(e.gid);
    e.state = "failed"; e.error = message; e.hadFailure = true;
    if (g) {
      g.hadFailure = true;
      this.o.diagnostics.finish(g.diag, "stt_failed", this.now() - g.lastAt);
    }
    this.makeRoom(0, 0, 0); // may evict this very clip when it alone is over the caps
    const kept = this.entries.has(e.id);
    this.syncKept(g);
    this.emit();
    this.event({ kind: "failed", id: e.id, message, kept });
    this.settle(e.id);
    this.release();
    if (g) this.settleGroup(g);
  }

  /** The owner pressed Retry: exactly one more attempt for a kept, failed clip. */
  retry(id: number): boolean {
    const e = this.entries.get(id);
    if (!e || e.state !== "failed" || !e.blob) return false;
    if (this.o.canUpload && !this.o.canUpload(true, e)) { this.event({ kind: "blocked", message: "NOT RETRIED · MIC OFF OR PAUSED" }); return false; }
    const g = this.groups.get(e.gid);
    if (g) { this.o.diagnostics.reopen(g.diag); g.lastAt = this.now(); }
    e.startedAt = this.now();
    e.retried = true;
    e.state = "waiting";
    this.schedule(e, true);
    if (e.state === "waiting") this.emit();
    return true;
  }

  /** The owner dropped a kept clip. Its audio is released at once. */
  discard(id: number) {
    const e = this.entries.get(id);
    if (!e) return;
    const g = this.groups.get(e.gid);
    e.cancelled = true; e.ac?.abort(); e.cancel?.(); clearTimeout(e.retryTimer ?? undefined);
    this.queue = this.queue.filter(q => q.e !== e);
    // A message with other chunks still going is not cancelled by dropping one.
    const alone = !g || g.members.every(m => m === id || !this.entries.has(m));
    if (e.state !== "failed" && g && alone) this.o.diagnostics.finish(g.diag, "cancelled", this.now() - e.startedAt);
    this.drop(e);
    this.emit(); this.settle(id);
    this.release();
    if (g) this.settleGroup(g);
  }

  /**
   * The message turned out not to be one (V5: too short after a chunk had already left). Everything
   * of it is dropped -- in-flight uploads aborted, kept audio released -- and nothing is placed.
   * Its one diagnostics row is settled as `outcome`.
   */
  cancelGroup(gid: number, outcome: DropReason) {
    const g = this.groups.get(gid);
    if (!g) return;
    g.open = false;
    if (g.diag) this.o.diagnostics.finish(g.diag, outcome, this.now() - g.lastAt);
    for (const id of [...g.members]) {
      const e = this.entries.get(id);
      if (!e) continue;
      e.cancelled = true; e.ac?.abort(); e.cancel?.(); clearTimeout(e.retryTimer ?? undefined);
      this.queue = this.queue.filter(q => q.e !== e);
      this.drop(e);
      this.settle(id);
    }
    this.emit();
    this.release();
    this.settleGroup(g);
    if (!g.members.some(id => this.entries.has(id))) this.groups.delete(gid);
  }

  /**
   * The message's words: each ready chunk's text in order, with what an overlap
   * repeats said once, and a mark where a chunk that failed would have gone.
   */
  private joined(g: Group<M>, ready: Entry<M>[]): string {
    const acc: string[] = [];
    const inBatch = new Set(ready.map(e => e.id));
    for (let at = 0; at < g.members.length; at++) {
      const id = g.members[at];
      const e = this.entries.get(id);
      if (!e) continue;
      // Marks only on a message's first placement: a later one is a retried chunk, alone,
      // and the others' gaps were already marked where they belong.
      if (e.state === "failed") { if (g.placed.size === 0 && acc[acc.length - 1] !== GAP_MARK) acc.push(GAP_MARK); continue; }
      if (e.state !== "ready" || !inBatch.has(id)) continue;
      const t = e.text.trim();
      if (!t) continue;
      const prevId = g.members[at - 1];
      const prev = g.parts.get(prevId);
      const nextId = g.members[at + 1];
      const next = nextId === undefined ? undefined : g.parts.get(nextId);
      let add = t.split(/\s+/);
      if (e.repeats && prev?.text) {
        if (inBatch.has(prevId)) {
          const m = overlapMerge(prev.text, t);
          acc.length -= Math.min(m.dropBefore, acc.length);
          add = add.slice(m.dropAfter);
        } else add = dropLeadingOverlap(prev.text, t).split(/\s+/).filter(Boolean);
      }
      // A retried chunk whose successor was placed without it: its tail repeats the successor's head.
      if (next?.overlap && next.text && g.placed.has(nextId)) add = dropTrailingOverlap(add.join(" "), next.text).split(/\s+/).filter(Boolean);
      acc.push(...add.filter(Boolean));
    }
    // A mark alone says nothing; a message of only blanks and failures places nothing.
    return acc.some(w => w !== GAP_MARK) ? acc.join(" ") : "";
  }

  /** Place, in capture order, every message no earlier chunk is still waiting on. */
  private release() {
    const out: { g: Group<M>; ents: Entry<M>[]; item: SttPlaced<M> }[] = [];
    for (const g of [...this.groups.values()].sort((a, b) => a.key - b.key)) {
      if (g.open) break; // still being spoken: it holds the line for what follows
      const live = g.members.map(id => this.entries.get(id)).filter((e): e is Entry<M> => !!e);
      if (live.some(e => e.state === "transcribing" || e.state === "waiting")) break;
      const ready = live.filter(e => e.state === "ready");
      if (!ready.length) {
        // Failed for good, or nothing left. A message every chunk of which was refused as not
        // the owner's is over: the row says so (it is a no-op if a failure already closed it).
        if (g.rejected) this.o.diagnostics.finish(g.diag, "not_owner", this.now() - g.lastAt);
        continue;
      }
      const text = this.joined(g, ready);
      if (!text) {
        // Every chunk that answered was blank.
        const failed = live.some(e => e.state === "failed") || g.hadFailure;
        for (const e of ready) { this.entries.delete(e.id); e.blob = null; g.placed.add(e.id); this.settle(e.id); }
        this.syncKept(g);
        if (!failed) this.o.diagnostics.finish(g.diag, "empty_transcript", this.now() - g.lastAt);
        this.emit();
        if (!failed) this.event({ kind: "empty" });
        this.settleGroup(g);
        continue;
      }
      const item: SttPlaced<M> = {
        id: ready[0].id, seq: ready[0].seq, text,
        ms: ready.reduce((n, e) => n + e.ms - e.overlapMs, 0),
        source: g.source, meta: g.meta, diag: g.diag,
        latencyMs: this.now() - g.lastAt, late: g.hadFailure || g.partial, retried: ready.some(e => e.retried), parts: ready.length,
      };
      for (const e of ready) { this.entries.delete(e.id); e.blob = null; g.placed.add(e.id); }
      this.syncKept(g);
      g.placing++;
      out.push({ g, ents: ready, item });
    }
    if (!out.length) return;
    this.emit();
    const batch = out.map(o => o.item);
    this.chain = this.chain.then(() => this.o.place(batch)).catch(() => {
      for (const i of batch) this.o.diagnostics.finish(i.diag, "unplaced", i.latencyMs);
    }).then(() => {
      for (const o of out) {
        for (const e of o.ents) this.settle(e.id);
        o.g.placing--;
        this.settleGroup(o.g);
      }
    });
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
  /**
   * V10: how long it was quiet after the last word when the message ended
   * (`UtteranceEnd.tailMs`; 0 when it was cut short). A trailing send or cancel
   * word counts only when this is at least `SEND_WORD_SILENCE_MS`.
   */
  tailMs?: number;
  /** The capture's utterance id: what a barge-in that this message began is called (V9). */
  utterance?: number;
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
