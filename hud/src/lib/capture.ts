// The always-open mic, ported from jarvis.html with its behaviour intact
// (tests/face/hud_capture_check.py is the spec).
//
// The mic is open from boot and never closes. Audio flows continuously into a
// 40-second ring that supplies only the *pre-roll*: a claim starts an
// UtteranceRecorder (recorder.ts) with the ring's audio from before the speech
// was detected, then every frame until the message ends. There is no length
// cap (V4, decision V-1); a long message leaves as ordered chunks (`onChunk`),
// each a WAV this module builds rather than a webm a MediaRecorder owns, and
// the message's end is announced once (`onUtteranceEnd`).
//
// Four rules the design turns on, each of which was a bug first:
//   - an utterance is only sent if it was addressed to him (the orb was held,
//     a wake phrase was heard around it, it landed in the follow-up window, or
//     it resumed within the continuation window of a message just sent);
//     continuous capture without that gate is a hot mic;
//   - a wake phrase claims exactly one utterance;
//   - he does not answer himself. V9: frames the self-voice gate (selfvoice.ts)
//     says are his own voice coming back never open or hold an utterance, and
//     while he is audible nothing is claimed except by the owner's own act (the
//     talk key, or a wake word heard with an utterance the gate did not veto);
//     the follow-up and continuation windows close. When the gate cannot run, V6's
//     blanket hold applies instead (`suppressed` below). An authorization card
//     suppresses the detector outright (a running turn no longer silences it: V3),
//     and a suppressed detector still *tracks the room*
//     so the threshold is current the moment the follow-up window opens;
//   - the minimum length is measured on the speech, not on the padded segment.
//
// The OFF dictation mode is v1's mic mute: nothing claimed, nothing uploaded,
// and `micFloorSample` clamps every upload at the last unmute, so audio
// captured while the owner believed the mic was off can never ride out as
// pre-roll or as a wake hit's back-dating. The one exception is the talk key
// (2026-10-27, owner's call): held while OFF it records exactly what is said
// while it is held -- dictation, as in Claude Code -- and nothing before the
// press (the press moves `micFloorSample`, so there is no pre-roll). The wake
// word, the follow-up and continuation windows stay closed.

import {
  CONTINUE_CHAIN_MS, CONTINUE_MAX_CLAIMS, CONTINUE_MS, END_PAUSE_MAX_MS, END_PAUSE_MIN_MS, END_PAUSE_MS, FOLLOWUP_MS,
  MIN_UTTER_MS, PREROLL_MS, Ring, Vad, WAKE_GRACE_MS, WEAK_CLAIM_GATE, WEAK_CLAIM_WINDOW_MS,
  closeBounds, frameRms, longEnough, msToSamples, samplesToMs,
} from "./vad";
import { CLAIM_CAP_MS, UTTERANCE_LIMIT_MS, UtteranceRecorder, type RecordedChunk } from "./recorder";
import { SEND_WORD_SILENCE_MS } from "./voiceWords";

import { CLEAR, type SelfFrame } from "./selfvoice";
import type { ClaimSource, DetectorKind, DropReason, SuppressionReason } from "./voiceDiagnostics";

/** One chunk of a claimed message, as it leaves for transcription. */
export interface UtteranceChunk extends RecordedChunk {
  source: ClaimSource;
  /** The detector that judged this message's frames (Silero or the energy fallback), for the diagnostics row. */
  detector: DetectorKind;
}

/** A claimed message is over: sent whole, truncated, or never uploaded at all. */
export interface UtteranceEnd {
  utterance: number;
  source: ClaimSource;
  /** Audio recorded, overlap counted once. */
  ms: number;
  /** Chunks that left (0: nothing was uploaded). */
  chunks: number;
  /** The continuation window this message armed (taken back with `cancelContinuation`), if it went out. */
  continuation: number | null;
  /** It ended before the owner finished: the mic went OFF, a card or picker appeared, the window crashed. */
  truncated: boolean;
  /**
   * V10: the quiet that followed the last word when the message ended, in ms
   * (the pause that proved it over, or how long the talk key waited before its
   * release). 0 when it was cut short or hit the ceiling. A trailing send or
   * cancel word counts only after `SEND_WORD_SILENCE_MS` of it.
   */
  tailMs: number;
  /**
   * It turned out not to be a message (too short, a cough) after a chunk had already left: the
   * consumer must cancel that chunk's group and place nothing. The drop is the consumer's to report.
   */
  discard?: boolean;
}

export interface CaptureHooks {
  /** Suppressed whenever the detector would be listening to him, not the owner. */
  suppressed: () => boolean;
  suppressionReason?: () => SuppressionReason;
  onDrop?: (reason: DropReason, ms: number, source: ClaimSource, detector: DetectorKind) => void;
  /** OFF: nothing is claimed and nothing is uploaded, except while the talk key is held. */
  muted: () => boolean;
  onLevel: (level: number) => void;
  /**
   * A claimed utterance began. `tag` is what the follow-up window was opened
   * for (V3: the pane whose reply opened it), when `source` is "follow_up".
   * `utterance` names it for `onChunk` and `onUtteranceEnd`.
   */
  onListening: (source?: ClaimSource, tag?: number | null, utterance?: number) => void;
  onIdle: (note?: string) => void;
  /**
   * A chunk of a claimed message left the recorder (V4): the first when the
   * message is 8-25 s long, or at its end. The dictation mode decides what
   * happens to the words once all of them are in. This is also the seam for
   * per-utterance audio (`chunk.samples`, speaker verification in V12).
   */
  onChunk: (chunk: UtteranceChunk) => void;
  /** Called exactly once for every `onListening`, after its last chunk. */
  onUtteranceEnd: (end: UtteranceEnd) => void;
  /**
   * The message reached its ceiling and was sent as it stood: the 15-minute one,
   * or (`claimCap`) the 60 s one for a claim made without the wake word or the
   * talk key.
   */
  onLimit?: (ms: number, claimCap: boolean) => void;
  /**
   * Why a recording must stop whatever else is true -- a card, a picker, Settings
   * or a crashed window -- independent of which suppression reason is reported
   * first. Without it only `suppressionReason()` decides, which his own voice
   * (`speaking`) or a closed chat (`no_chat`) can mask.
   */
  hardStop?: () => SuppressionReason | null;
}

/** A wake hit within this long of his voice last being audible is not kept in reserve for the next utterance. */
export const SELF_ARM_GUARD_MS = 1500;

/** Reasons that stop a recording even when the owner is holding the talk key. */
const HARD_STOPS: ReadonlySet<DropReason> = new Set<DropReason>([
  "muted", "suppressed:card", "suppressed:picker", "suppressed:settings", "suppressed:crash",
]);

export class Capture {
  ring = new Ring();
  vad = new Vad();
  followUpUntil = 0;
  /** Opaque: what the follow-up window was opened for (a pane), handed back on a claim. */
  followUpTag: number | null = null;
  /**
   * The continuation window, on the ring's sample clock (-1: closed). Opened
   * when a claimed message goes out; speech whose onset falls before it is
   * claimed as that message's continuation (V3, plan §3.2).
   */
  continueUntil = -1;
  /**
   * The chain a continuation belongs to (a stopgap against room speech until
   * speaker verification): begun by a wake or follow-up claim, it allows
   * CONTINUE_MAX_CLAIMS continuation claims and ends CONTINUE_CHAIN_MS after
   * that claim's message ended (so a long dictation still gets its window).
   */
  chainFrom = -1;
  chainClaims = 0;
  /** Names the window the last message armed, so a caller can take back that one only. */
  private continueToken = 0;
  private tokenSeq = 0;
  wakeHitAt = -1e9;
  segClaimed = false;
  /** The held talk key. `dictating`: pressed while OFF, so its own recording is exempt from the mute. */
  ptt: { from: number; at: number; adopted: boolean; dictating: boolean } | null = null;
  /** The recording of the claimed message in progress, if any. */
  private rec: UtteranceRecorder | null = null;
  private utterances = 0;
  micFloorSample = 0;
  private followUpTimer: any = null;
  private claimSource: ClaimSource = "unclaimed";
  /** The detector that judged the utterance now in progress (or the last one): Silero while the model was scoring its frames. */
  judgedBy: DetectorKind = "energy";
  // Observation only: the real VAD deliberately resets while suppressed.
  private blockedVad = new Vad();
  private blockedReason: DropReason | null = null;
  private blockedAlreadyReported = false;
  private wasBlocked = false;
  /** V9: his voice is audible within the echo delay (the self-voice gate's last word on a frame). */
  selfActive = false;
  /**
   * V10: his voice was audible at some point while the recording in progress was open. His words can then be
   * in the transcript, as its last word too ("over", "cancel" said by a reply, echoing in), so the message
   * reports no trailing silence and no send or cancel word is read from it.
   */
  private selfInRec = false;
  private wasSelfObserved = false;
  private wasPlaying = false;
  /** The last time (the capture's clock) his voice was audible to the self-voice gate. */
  private selfLastActiveAt = -1e9;
  /** The first sample of the run of frames the gate let through while he was audible; -1 when the last frame was his. */
  private freeFrom = -1;

  private drop(reason: DropReason, ms: number, source = this.claimSource, detector = this.judgedBy) {
    try { this.hooks.onDrop?.(reason, ms, source, detector); } catch { /* diagnostics cannot change capture */ }
  }

  private observeBlocked(rms: number, n: number, reason: DropReason | null, prob?: number | null) {
    if (!reason) {
      if (this.blockedVad.open && !this.blockedAlreadyReported && this.blockedReason)
        this.drop(this.blockedReason, samplesToMs(this.blockedVad.voiced), "unclaimed", this.blockedVad.ml ? "silero" : "energy");
      this.blockedVad.reset(); this.blockedReason = null; this.blockedAlreadyReported = false;
      return;
    }
    const event = this.blockedVad.feed(rms, n, this.ring.written, prob);
    if (event === "open") this.blockedReason = reason;
    if (event === "close" || event === "cap") {
      if (!this.blockedAlreadyReported) this.drop(this.blockedReason ?? reason, samplesToMs(this.blockedVad.voiced), "unclaimed", this.blockedVad.ml ? "silero" : "energy");
      this.blockedReason = null; this.blockedAlreadyReported = false;
    }
  }

  constructor(private hooks: CaptureHooks, private now: () => number = () => performance.now()) {}

  /** The quiet run that ends a message (V5, plan §3.2: about 2.5 s, tunable). */
  get endPauseMs() {
    return this.vad.endPauseMs;
  }

  /** Tune the message pause; held to END_PAUSE_MIN_MS..END_PAUSE_MAX_MS, anything else keeps the default. */
  setEndPause(ms: number) {
    const v = Number.isFinite(ms) ? Math.round(Math.max(END_PAUSE_MIN_MS, Math.min(END_PAUSE_MAX_MS, ms))) : END_PAUSE_MS;
    this.vad.endPauseMs = v;
    this.blockedVad.endPauseMs = v;
  }

  conversing() {
    return this.now() < this.followUpUntil;
  }

  /** Opened when he stops speaking: the next utterance is simply the next turn. */
  openFollowUp(tag: number | null = null) {
    if (this.hooks.muted()) return; // a muted mic must not promise "just speak"
    this.followUpTag = tag;
    this.followUpUntil = this.now() + FOLLOWUP_MS;
    clearTimeout(this.followUpTimer);
    this.followUpTimer = setTimeout(() => {
      this.followUpUntil = 0;
    }, FOLLOWUP_MS);
  }

  closeFollowUp() {
    this.followUpUntil = 0;
    this.followUpTag = null;
    clearTimeout(this.followUpTimer);
  }

  /** Speech beginning now would continue the message just sent. */
  continuing() {
    if (this.continueUntil < 0 || this.chainClaims >= CONTINUE_MAX_CLAIMS) return false;
    const onset = this.ring.written - this.vad.run;
    return onset <= this.continueUntil && onset <= this.chainFrom + msToSamples(CONTINUE_CHAIN_MS);
  }

  closeContinuation() {
    this.continueUntil = -1;
    this.continueToken = 0;
  }

  /** The message that armed window `token` did not go out (upload failed, nothing heard, refused): take that window back. */
  cancelContinuation(token: number | null | undefined) {
    if (token && token === this.continueToken) this.closeContinuation();
  }

  /**
   * The window stopped drawing (a render error): nothing in hand is sent — a
   * push-to-talk being held, an utterance a wake hit or the follow-up window
   * claimed, a wake hit waiting for one. The caller keeps the detector
   * suppressed from then on.
   */
  abandon() {
    if (this.ptt) this.drop("suppressed:crash", samplesToMs(this.ring.written - this.ptt.from), "ptt");
    else if (this.vad.open) this.drop("suppressed:crash", samplesToMs(this.vad.voiced));
    this.blockedAlreadyReported = this.vad.open;
    this.stopRecording(true);
    this.ptt = null;
    this.segClaimed = false;
    this.wakeHitAt = -1e9;
    this.closeFollowUp();
    this.closeContinuation();
  }

  /** A claimed message is being recorded (chunks may already have left). */
  recording() {
    return this.rec !== null;
  }

  // ---- recording ----------------------------------------------------------

  /**
   * `announce` runs before the pre-roll is fed in: a long pre-roll (a wake word
   * late into room speech) already cuts chunks, and the consumer must know the
   * message before its first chunk arrives.
   */
  private beginRecording(from: number, announce: (id: number) => void): number {
    const id = ++this.utterances;
    const source = () => this.claimSource;
    const detector = this.judgedBy;
    const capped = this.claimSource === "follow_up" || this.claimSource === "continuation";
    const rec = new UtteranceRecorder(id, (chunk) => {
      try { this.hooks.onChunk({ ...chunk, source: source(), detector }); } catch { /* a consumer cannot break the audio path */ }
    }, capped ? CLAIM_CAP_MS : UTTERANCE_LIMIT_MS);
    this.rec = rec;
    this.selfInRec = this.selfActive;
    announce(id);
    // Nothing from before the last unmute ever leaves, however far back the pre-roll reaches. And while his
    // voice is audible (V9), nothing from before the first frame the gate let through: the pre-roll and a
    // wake hit's back-dating would carry his own words into the owner's message.
    let start = Math.max(from, this.micFloorSample);
    if (this.selfActive) start = Math.max(start, this.freeFrom >= 0 ? this.freeFrom : this.ring.written);
    rec.start(this.ring.slice(start, this.ring.written));
    return id;
  }

  private announceEnd(rec: UtteranceRecorder, summary: { chunks: number; ms: number }, continuation: number | null, truncated: boolean, discard = false, tailMs = 0) {
    try {
      this.hooks.onUtteranceEnd({
        utterance: rec.id, source: this.claimSource, ms: summary.ms, chunks: summary.chunks, continuation, truncated,
        tailMs: truncated || discard ? 0 : Math.max(0, Math.round(tailMs)),
        ...(discard ? { discard: true } : {}),
      });
    } catch { /* ditto */ }
  }

  /**
   * End the recording without sending what is still held: the mic went OFF, a
   * card appeared, the window crashed, or the speech was too short to be a
   * turn. Chunks that already left stay out (they were captured while allowed).
   */
  private stopRecording(truncated: boolean, discard = false): { chunks: number; droppedMs: number } | null {
    const rec = this.rec;
    if (!rec) return null;
    this.rec = null;
    const summary = rec.abort();
    this.announceEnd(rec, summary, null, truncated, discard);
    return summary;
  }

  /**
   * The message is over: flush its last chunk, arm the continuation window, say so.
   * `cutShort`: it did not end because the owner finished (the 60 s stopgap cap): it
   * is announced `truncated`, so it is placed late and boxed, and arms no
   * continuation, which would only split what was one long message.
   * `quietSamples` is the quiet run behind the last word (V10), read before
   * anything resets it.
   */
  private finishRecording(trim: number, cutShort = false, quietSamples = this.vad.quiet) {
    const rec = this.rec;
    if (!rec) return;
    this.rec = null;
    const tailMs = this.selfInRec ? 0 : samplesToMs(quietSamples);
    this.vad.voiced = 0;
    // The message ended here. Whatever follows soon enough continues it --
    // except after push-to-talk, whose release is the owner's own full stop:
    // the next words need the key (or his name) again. Only a wake or
    // follow-up claim starts a chain; a continuation extends it while claims
    // remain. Armed before the hook runs, so a caller that refuses the message
    // can take its window back.
    const source = this.claimSource;
    if (source === "wake" || source === "follow_up") { this.chainFrom = this.ring.written; this.chainClaims = 0; }
    const arm = !cutShort && (source === "wake" || source === "follow_up" || source === "continuation")
      && this.chainClaims < CONTINUE_MAX_CLAIMS;
    this.continueToken = arm ? ++this.tokenSeq : 0;
    this.continueUntil = arm ? this.ring.written + msToSamples(CONTINUE_MS) : -1;
    const summary = rec.end(trim);
    if (summary.chunks === 0) { this.closeContinuation(); this.announceEnd(rec, summary, null, false); return; }
    this.announceEnd(rec, summary, arm ? this.continueToken : null, cutShort, false, tailMs);
  }

  /** The ceiling: the message goes as it stands, and the window says so. */
  private hitLimit() {
    const rec = this.rec;
    if (!rec) return;
    const ms = rec.ms;
    const claimCap = rec.limitMs < UTTERANCE_LIMIT_MS;
    this.finishRecording(0, claimCap, 0);
    this.vad.open = false;
    this.segClaimed = false;
    this.ptt = null;
    try { this.hooks.onLimit?.(ms, claimCap); } catch { /* ditto */ }
  }

  /** Called when the mic unmutes: everything before this point stays local. */
  markUnmute() {
    this.micFloorSample = this.ring.written;
    this.closeContinuation();
  }

  /**
   * One 16 kHz frame. `prob` is the ML VAD's speech probability for it when the
   * detector stage has one (V5); without it the frame is judged by energy, as
   * before. `self` is the self-voice gate's verdict on it (V9): a frame that is
   * his own voice is not speech to the detector, and while his voice is audible
   * the windows that let speech be claimed without a wake word are closed. Either
   * way the frame is ring-pushed, level-metered and recorded the same, and every
   * rule below (OFF, suppression, claims, drops) is unchanged.
   */
  onFrame(frame: Float32Array, prob?: number | null, self: SelfFrame | null = null) {
    this.ring.push(frame);
    const rms = frameRms(frame);
    // A talk key pressed while OFF lifts the mute for its own recording only;
    // the mic going OFF under a key pressed while on still ends it, as before.
    const muted = this.hooks.muted() && !this.ptt?.dictating;
    // Muted, the orb shows nothing: a level meter on a muted mic reads as "he
    // is listening", which is exactly the wrong promise.
    this.hooks.onLevel(muted ? 0 : Math.min(1, rms * 4));
    const suppressed = this.hooks.suppressed();
    const verdict = self ?? CLEAR;
    this.selfActive = verdict.active;
    if (verdict.active && this.rec) this.selfInRec = true;
    if (verdict.active) this.selfLastActiveAt = this.now();
    // The run the gate let through ends when he goes inactive or the detector's utterance is closed. A frame judged
    // his voice inside an open utterance (a comma between "Jarvis" and "stop") is a pause in the run, not its end:
    // a claim must still reach back to the owner's first word.
    if (!verdict.active || (verdict.self && !this.vad.open)) this.freeFrom = -1;
    else if (!verdict.self && this.freeFrom < 0) this.freeFrom = this.ring.written - frame.length;
    // Anything that stops the detector listening ends the message it would
    // have continued: words after a card, a picker or his own voice are a new
    // thought, and need the wake word again.
    if (muted || suppressed || verdict.active) this.closeContinuation();
    // ...and the follow-up window is for the reply that opened it: when a new reply begins to
    // sound, it belongs to nobody, and speech is claimed only by name or key.
    if (verdict.playing && !this.wasPlaying) this.closeFollowUp();
    this.wasPlaying = verdict.playing;
    let reason: DropReason | null = muted ? "muted" : suppressed ? `suppressed:${this.hooks.suppressionReason?.() ?? "speaking"}` : null;
    // `hardStop` is asked on its own: his own voice or a closed chat can be the
    // reason reported first while a card, a picker or Settings is also up. The
    // harder reason is the one a drop and a truncation then name.
    let hardWhy: DropReason | null = null;
    if (reason) {
      const hard = muted ? "muted" as const : this.hooks.hardStop?.();
      hardWhy = hard ? (hard === "muted" ? "muted" : `suppressed:${hard}`) : HARD_STOPS.has(reason) ? reason : null;
      if (hardWhy) reason = hardWhy;
    }
    const vetoed = verdict.self && !muted && !suppressed;
    if (reason && !this.wasBlocked && this.vad.open && !this.ptt && !this.blockedAlreadyReported) {
      this.drop(reason, samplesToMs(this.vad.voiced));
      this.blockedAlreadyReported = true;
    }
    if (reason && !this.wasBlocked) this.blockedVad.floor = this.vad.floor;
    this.wasBlocked = reason !== null;
    if (this.ptt) this.blockedAlreadyReported = true;
    // A vetoed frame in the middle of nothing is observed like a suppressed one, so the
    // diagnostics can say "self voice" for speech-shaped sound that was held back. A
    // message being recorded is not "observed": its own pauses are its own business.
    const observed = reason ?? (vetoed && !this.vad.open && this.rec === null && !this.ptt ? "self_voice" : null);
    if (observed === "self_voice" && !this.wasSelfObserved) this.blockedVad.floor = this.vad.floor;
    this.wasSelfObserved = observed === "self_voice";
    this.observeBlocked(rms, frame.length, observed, prob);
    // A recording in progress stops uploading the moment the detector may not
    // listen: nothing under a card, a picker or Settings, nothing while OFF,
    // nothing from a crashed window. The talk key is the owner's own word, so
    // it carries on through his own voice (barge-in) and a chat closing, which
    // the old pipeline also let a held key do.
    if (this.rec && reason && (!this.ptt || hardWhy)) this.truncate(reason);
    // The ceiling and the pause-tracking read what the detector saw in this frame.
    this.vad.uncapped = this.segClaimed || this.rec !== null;
    if (muted || suppressed) {
      // Still track the room, so the threshold is current the moment the
      // follow-up window opens; just never open an utterance.
      if (this.vad.open) {
        this.vad.open = false;
        this.segClaimed = false;
      }
      const had = this.rec;
      this.vad.feed(rms, frame.length, this.ring.written, prob, vetoed);
      if (had && this.rec === had && this.pushFrame(had, frame)) this.hitLimit();
      return;
    }
    const had = this.rec;
    const event = this.vad.feed(rms, frame.length, this.ring.written, prob, vetoed);
    // A recording that began before this frame takes it; one that begins on
    // this frame (the "open" event below) already has it, from the ring.
    if (had && this.rec === had && this.pushFrame(had, frame)) { this.hitLimit(); return; }
    if (event) this.onVadEvent(event);
    else this.lateClaim();
  }

  /** One frame into the recording, telling it whether what it holds is a message yet (its first-gap cut asks). */
  private pushFrame(rec: UtteranceRecorder, frame: Float32Array): boolean {
    rec.cutOk = longEnough(this.vad);
    return rec.push(frame, this.vad.loud);
  }

  private truncate(reason: DropReason) {
    const ptt = this.ptt;
    const stopped = this.stopRecording(true);
    if (ptt) {
      this.drop(reason, stopped?.droppedMs ?? 0, "ptt");
      this.ptt = null;
    }
    this.segClaimed = false;
    if (stopped && stopped.chunks === 0) this.hooks.onIdle();
  }

  private onVadEvent(event: "open" | "close" | "cap") {
    // The talk key owns the recording while it is held: the detector neither
    // claims a second utterance beside it nor ends it at a pause.
    if (this.ptt) { this.segClaimed = false; return; }
    if (event === "open") {
      // The detector that opened it is the one that judged it.
      this.judgedBy = this.vad.ml ? "silero" : "energy";
      this.gateUntil = this.ring.written + msToSamples(WEAK_CLAIM_WINDOW_MS);
      this.tryClaim();
      return;
    }
    const { to } = closeBounds(this.vad, this.ring.written, event);
    const claimed = this.segClaimed;
    this.segClaimed = false;
    if (!claimed) { this.drop("unclaimed", samplesToMs(this.vad.voiced)); return; }
    if (!longEnough(this.vad)) {
      // (The first chunk is only cut early once the audio is long enough, so a chunk having left
      // already is rare; if it did, the consumer cancels its group and reports the drop itself.)
      const left = this.rec?.chunks ?? 0;
      if (left === 0) this.drop("too_short", samplesToMs(this.vad.voiced));
      this.vad.voiced = 0;
      this.stopRecording(false, left > 0);
      // He was addressed; give the owner the window back -- but only if a wake
      // word or the follow-up window did the addressing. A too-short
      // continuation (a cough, a TV's click) just drops: reopening follow-up
      // would let the next noise start a fresh chain with no wake word.
      if (this.claimSource === "wake" || this.claimSource === "follow_up") this.openFollowUp();
      this.hooks.onIdle("DIDN'T CATCH THAT");
      return;
    }
    // The utterance ended a hangover ago: drop the silence that proved it.
    this.finishRecording(Math.max(0, this.ring.written - to));
  }

  /** Until when (ring clock) an unclaimed open utterance may still pass the weak gate. */
  private gateUntil = -1;

  /**
   * Decide whether the utterance that just opened is claimed. V5 stopgap (until V12): with the ML
   * VAD a follow-up or continuation claim needs the onset to have risen WEAK_CLAIM_GATE x above the
   * energy threshold, so steady room speech the model calls speech does not claim without the wake
   * word. A soft onset is not condemned for good: the gate is looked at again on each frame for the
   * first WEAK_CLAIM_WINDOW_MS (`lateClaim`), and the recording starts at the utterance's own start,
   * which the ring still holds.
   */
  private tryClaim() {
    const weak = this.vad.ml && this.vad.peak < this.vad.thresh * WEAK_CLAIM_GATE;
    const continuing = this.continuing() && !weak;
    const following = this.conversing() && !weak;
    const woken = this.now() - this.wakeHitAt < WAKE_GRACE_MS;
    this.claimSource = following ? "follow_up" : woken ? "wake" : continuing ? "continuation" : "unclaimed";
    this.segClaimed = following || woken || continuing;
    if (this.claimSource === "continuation") this.chainClaims++;
    if (this.segClaimed) {
      // Saying his name claims exactly one utterance.
      this.wakeHitAt = -1e9;
      const tag = this.claimSource === "follow_up" ? this.followUpTag : null;
      this.closeFollowUp();
      this.beginRecording(this.vad.openedAt, (id) => this.hooks.onListening(this.claimSource, tag, id));
    }
  }

  /** An open, unclaimed ML utterance whose onset was too soft: claim it if it has risen since, within the window. */
  private lateClaim() {
    if (!this.vad.open || this.segClaimed || this.ptt || !this.vad.ml || this.ring.written > this.gateUntil) return;
    if (this.vad.peak < this.vad.thresh * WEAK_CLAIM_GATE) return;
    this.tryClaim();
  }

  // ---- wake ---------------------------------------------------------------

  /**
   * A wake phrase was heard. Nothing starts recording here — the mic has been
   * running all along. The wake word is reported several hundred ms after it
   * was said (V11's local keyword spotter: ~0.35 s median, 0.64 s at p95), so the detector usually already has an utterance open:
   * claim that one, and back-date its start far enough to cover the lag.
   */
  onWake(): "claimed" | "armed" | "ignored" {
    if (this.hooks.muted()) return "armed"; // a result already in flight can land late
    if (this.ptt) return "armed"; // the talk key already owns this recording
    // The detector is held off (a card, a picker, Settings, or V6's blanket hold while he speaks): a
    // wake hit has nothing to claim. Without this a reply saying "Jarvis" under the blanket hold would
    // claim the observed-but-suppressed utterance and pause himself for the frame it took to be truncated.
    if (this.hooks.suppressed()) {
      this.drop(`suppressed:${this.hooks.suppressionReason?.() ?? "speaking"}`, 0, "wake");
      return "ignored";
    }
    // V9: while his voice is audible, his own word "Jarvis" -- a reply that says it, a
    // recognizer that hears the speakers -- must not claim anything. A hit counts only
    // against an utterance the self-voice gate did not veto (frames it vetoes never open
    // one), so it is honoured when the owner is speaking over him and ignored otherwise;
    // and it is never held in reserve for the next utterance, which is how a reply
    // saying "Jarvis" would claim the owner's next word.
    // Nor a hit that comes late, just after he stopped (the recognizer reports a phrase well after it was
    // said): armed, it would claim the owner's next words. Only a claim of an utterance already open is allowed.
    const claimable = this.vad.open && !this.segClaimed;
    if ((this.selfActive || this.now() - this.selfLastActiveAt < SELF_ARM_GUARD_MS) && !claimable) {
      this.drop("self_voice", 0, "wake");
      return "ignored";
    }
    // Saying his name over a message the 60 s stopgap would cut makes it a wake claim:
    // the same lift the talk key gives.
    if (this.rec && this.rec.limitMs < UTTERANCE_LIMIT_MS) {
      this.rec.limitMs = UTTERANCE_LIMIT_MS;
      this.claimSource = "wake";
      this.wakeHitAt = -1e9;
      return "claimed";
    }
    this.wakeHitAt = this.now();
    if (this.vad.open && !this.segClaimed) {
      this.segClaimed = true;
      this.claimSource = "wake";
      this.wakeHitAt = -1e9; // spent: this hit claims this utterance and no other
      this.vad.openedAt = Math.min(
        this.vad.openedAt,
        this.ring.written - msToSamples(WAKE_GRACE_MS + PREROLL_MS),
      );
      this.closeFollowUp();
      this.beginRecording(this.vad.openedAt, (id) => this.hooks.onListening("wake", null, id));
      return "claimed";
    }
    return "armed";
  }

  // ---- push to talk -------------------------------------------------------

  press() {
    if (this.ptt) return false;
    // OFF: the key still dictates, but nothing from before the press leaves.
    const dictating = this.hooks.muted();
    if (dictating) this.micFloorSample = this.ring.written;
    this.claimSource = "ptt";
    this.judgedBy = this.vad.ml ? "silero" : "energy";
    const at = this.ring.written;
    this.ptt = { from: Math.max(0, at - msToSamples(PREROLL_MS)), at, adopted: this.rec !== null, dictating };
    this.segClaimed = false;
    this.closeFollowUp();
    this.closeContinuation();
    if (this.rec) {
      // A hands-free message already being recorded is simply carried on by the key,
      // which lifts its 60 s stopgap: the owner is holding the key.
      this.rec.limitMs = UTTERANCE_LIMIT_MS;
      this.hooks.onListening("ptt", null, this.rec.id);
    } else this.beginRecording(this.ptt.from, (id) => this.hooks.onListening("ptt", null, id));
    return true;
  }

  release() {
    if (!this.ptt) return;
    const { at, adopted } = this.ptt;
    this.ptt = null;
    const rec = this.rec;
    if (!rec) return;
    // A tap on the key over a hands-free message that was already going is not
    // "too short": the message is not the tap's, and finishes as it would have.
    if (!adopted && samplesToMs(this.ring.written - at) < MIN_UTTER_MS && rec.chunks === 0) {
      this.drop("too_short", samplesToMs(this.ring.written - at), "ptt");
      this.stopRecording(false);
      this.claimSource = "unclaimed"; // the detector may still be inside the tap: that is not the key's
      this.hooks.onIdle("TOO SHORT");
      return;
    }
    // The detector must not immediately re-open on the tail of what was just
    // sent, and the room may have changed while the owner held the orb. (How
    // long it had been quiet is the message's tail, V10: read it first.)
    const quiet = this.vad.quiet;
    this.vad.quiet = 0;
    this.vad.run = 0;
    this.vad.open = false;
    this.segClaimed = false;
    this.finishRecording(0, false, quiet);
    this.claimSource = "unclaimed";
  }

  /**
   * End the claimed hands-free message now instead of waiting out the pause,
   * if it has been quiet for at least `minQuietMs` (V10, plan §3.3 phase 2).
   * The seam for local keyword spotting (V11): a send or cancel word heard and
   * followed by 500 ms of quiet ends the message at once. Nothing in the
   * transcript phase can call it -- the words are not known until the audio has
   * been transcribed -- and the talk key owns its own recording. True when it ended.
   */
  endNow(minQuietMs = SEND_WORD_SILENCE_MS): boolean {
    if (!this.rec || this.ptt || !this.segClaimed || !this.vad.open) return false;
    if (samplesToMs(this.vad.quiet) < minQuietMs) return false;
    this.vad.open = false;
    this.onVadEvent("close");
    return true;
  }

  /**
   * The mic was switched OFF: a recording in progress ends now, without waiting
   * for the next frame to notice. What already left stays; the held tail is a
   * `muted` drop.
   */
  muteNow() {
    this.closeContinuation();
    if (!this.rec) return;
    if (!this.ptt) { this.drop("muted", samplesToMs(this.vad.voiced)); this.blockedAlreadyReported = true; }
    this.truncate("muted");
    this.vad.open = false;
  }

  /** Test hook: script a room through the real detector, no microphone. */
  feedMs(ms: number, amplitude: number, frameSamples = 1024, prob?: number | null, self: SelfFrame | null = null) {
    const total = msToSamples(ms);
    let done = 0;
    while (done < total) {
      const n = Math.min(frameSamples, total - done);
      const frame = new Float32Array(n);
      // A frame of N samples at a constant amplitude has exactly that RMS.
      frame.fill(amplitude);
      this.onFrame(frame, prob, self);
      done += n;
    }
  }
}
