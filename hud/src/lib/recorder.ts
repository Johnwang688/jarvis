// The uncapped utterance recorder (voice rework V4, plan §3.1, decision V-1).
//
// A claim used to be carved out of the 40-second ring after the fact, which is
// why an utterance was cut at 30 s and a push-to-talk longer than 40 s lost its
// start. The ring now supplies only the pre-roll. A claim starts one of these:
// the pre-roll, then every frame until the message ends, however long that is.
//
// Memory is bounded by the chunk, not the utterance. The recorder holds at most
// one chunk of audio (CHUNK_HARD_MS plus a frame); a chunk that is cut leaves as
// a WAV for the STT assembler, which keeps it under its own caps (stt.ts), and
// the recorder keeps only the overlap it needs for the next one. Chunks are cut
//
//   - at a pause where there is one: once the chunk is CHUNK_SOFT_MS long, the
//     first quiet run of CHUNK_GAP_MS ends it; past CHUNK_LATE_MS a shorter
//     pause (CHUNK_LATE_GAP_MS) will do. Nothing is lost at such a cut, so the
//     next chunk starts fresh (overlap 0);
//   - the FIRST chunk is cut earlier (V5, plan §3.2's segment gap): at the first quiet run of
//     CHUNK_GAP_MS once FIRST_CUT_SPEECH_MS of speech has been heard, whatever the chunk's
//     length, so the opening of a long message is transcribing while the rest is still being
//     said. Bounded: it happens once per message; every later chunk follows the rule above;
//     and a final chunk with no speech in it is never uploaded;
//   - failing that, hard at CHUNK_HARD_MS, in the middle of speech. The next
//     chunk then starts CHUNK_OVERLAP_MS earlier, so a word the cut fell in is
//     whole in one of them, and the transcripts are joined by the words the
//     two share (joinChunks.ts).
//
// A 15-minute ceiling (UTTERANCE_LIMIT_MS) ends a message with a status line,
// never silently. Silence trimming at the end of a message is the caller's: it
// passes the number of trailing samples to drop.

import { CAP_RATE, SEGMENT_GAP_MS, msToSamples, samplesToMs, wavBlob } from "./vad";

export const CHUNK_SOFT_MS = 8_000;
/** Speech heard (pre-roll not counted) before the first pause may cut the first chunk. */
export const FIRST_CUT_SPEECH_MS = 350;
export const CHUNK_GAP_MS = SEGMENT_GAP_MS; // the plan's segment gap (V5, vad.ts)
export const CHUNK_LATE_MS = 18_000;
export const CHUNK_LATE_GAP_MS = 300;
export const CHUNK_HARD_MS = 25_000;
export const CHUNK_OVERLAP_MS = 500;
export const UTTERANCE_LIMIT_MS = 15 * 60_000;

export interface RecordedChunk {
  /** The utterance this chunk belongs to (Capture's own counter). */
  utterance: number;
  /** 0-based position in the utterance. */
  index: number;
  /**
   * The chunk's 16 kHz mono PCM, for a consumer that needs the audio itself
   * (speaker verification, V12). Shared with nothing the recorder keeps, but
   * it is not copied again: do not mutate it, and do not hold on to it.
   */
  samples: Float32Array;
  /** The same audio as a WAV, the form /stt takes. */
  wav: Blob;
  /** The chunk's length, overlap included. */
  ms: number;
  /** How much of its start repeats the end of the previous chunk (0 after a cut at a pause). */
  overlapMs: number;
  /** Where the chunk's first sample falls, from the start of the utterance. */
  startMs: number;
}

export interface RecorderSummary { chunks: number; ms: number }

export class UtteranceRecorder {
  private frames: Float32Array[] = [];
  private len = 0;             // samples in the current chunk, overlap included
  private overlap = 0;         // leading samples shared with the previous chunk
  private voiced = 0;          // loud samples among the new ones
  private heard = 0;           // loud samples pushed after the pre-roll (the first-cut rule)
  private quiet = 0;           // the trailing quiet run
  private index = 0;           // chunks flushed
  private total = 0;           // unique samples recorded (overlap counted once)
  private startedAt = 0;       // unique samples before the current chunk's first new sample
  private done = false;

  constructor(readonly id: number, private onChunk: (chunk: RecordedChunk) => void) {}

  /** Unique audio recorded so far, in ms. */
  get ms() { return samplesToMs(this.total); }
  /** Chunks flushed so far. */
  get chunks() { return this.index; }
  /** Audio held and not yet flushed (what an abort would lose), in ms. */
  get heldMs() { return samplesToMs(this.len - this.overlap); }
  get ended() { return this.done; }

  /** The pre-roll. It cannot be told apart quiet from loud, so it counts as speech. */
  start(preroll: Float32Array) {
    const step = 512;
    for (let at = 0; at < preroll.length; at += step) this.add(preroll.subarray(at, Math.min(preroll.length, at + step)), true, false);
  }

  /** One frame. Returns true when the message has reached its ceiling and must end. */
  push(frame: Float32Array, loud: boolean): boolean {
    return this.add(frame, loud, true);
  }

  private add(frame: Float32Array, loud: boolean, live: boolean): boolean {
    if (this.done || !frame.length) return false;
    this.frames.push(frame.slice());
    this.len += frame.length;
    this.total += frame.length;
    if (loud) { this.voiced += frame.length; this.quiet = 0; if (live) this.heard += frame.length; } else this.quiet += frame.length;
    const ms = samplesToMs(this.len);
    if (ms >= CHUNK_HARD_MS) this.cut(true);
    else if (ms >= CHUNK_SOFT_MS && this.quiet >= msToSamples(ms >= CHUNK_LATE_MS ? CHUNK_LATE_GAP_MS : CHUNK_GAP_MS)) this.cut(false);
    else if (this.index === 0 && this.heard >= msToSamples(FIRST_CUT_SPEECH_MS) && this.quiet >= msToSamples(CHUNK_GAP_MS)) this.cut(false);
    return this.total >= msToSamples(UTTERANCE_LIMIT_MS);
  }

  /**
   * The message ended. `trim` trailing samples (the silence that proved it
   * ended) are dropped, never more than the chunk's own new audio. A final
   * chunk with no speech in it is not sent when an earlier chunk exists; the
   * first chunk always is (a push-to-talk hold is the owner's say-so).
   */
  end(trim = 0): RecorderSummary {
    if (!this.done) {
      const fresh = this.len - this.overlap;
      const drop = Math.max(0, Math.min(trim, fresh));
      if (drop) this.dropTail(drop);
      const keep = this.len - this.overlap > 0 && (this.index === 0 || this.voiced > 0);
      if (keep) this.flush();
      this.done = true;
      this.frames = [];
      this.len = 0;
    }
    return { chunks: this.index, ms: samplesToMs(this.total) };
  }

  /** The message is abandoned. What is held is discarded unsent; what already left stays out. */
  abort(): RecorderSummary & { droppedMs: number } {
    const droppedMs = this.heldMs;
    this.done = true;
    this.frames = [];
    this.len = 0;
    return { chunks: this.index, ms: samplesToMs(this.total), droppedMs };
  }

  private dropTail(n: number) {
    this.len -= n;
    this.total -= n;
    while (n > 0 && this.frames.length) {
      const last = this.frames[this.frames.length - 1];
      if (last.length <= n) { n -= last.length; this.frames.pop(); }
      else { this.frames[this.frames.length - 1] = last.subarray(0, last.length - n); n = 0; }
    }
  }

  private concat(): Float32Array {
    const out = new Float32Array(this.len);
    let at = 0;
    for (const f of this.frames) { out.set(f, at); at += f.length; }
    return out;
  }

  private flush() {
    const samples = this.concat();
    const chunk: RecordedChunk = {
      utterance: this.id, index: this.index, samples, wav: wavBlob(samples, CAP_RATE),
      ms: samplesToMs(samples.length), overlapMs: samplesToMs(this.overlap),
      startMs: samplesToMs(this.startedAt - this.overlap),
    };
    this.index++;
    this.startedAt += samples.length - this.overlap;
    this.onChunk(chunk);
    return samples;
  }

  private cut(hard: boolean) {
    const samples = this.flush();
    this.voiced = 0;
    this.quiet = 0;
    if (hard) {
      const keep = Math.min(samples.length, msToSamples(CHUNK_OVERLAP_MS));
      const tail = samples.slice(samples.length - keep);
      this.frames = [tail];
      this.len = keep;
      this.overlap = keep;
    } else {
      this.frames = [];
      this.len = 0;
      this.overlap = 0;
    }
  }
}
