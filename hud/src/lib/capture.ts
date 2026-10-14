// The always-open mic, ported from jarvis.html with its behaviour intact
// (tests/face/hud_capture_check.py is the spec).
//
// The mic is open from boot and never closes. Audio flows continuously into a
// 40-second ring; utterances are carved out of it *afterwards* by the
// detector, and the payload is a WAV this module builds rather than a webm a
// MediaRecorder owns.
//
// Four rules the design turns on, each of which was a bug first:
//   - an utterance is only sent if it was addressed to him (the orb was held,
//     a wake phrase was heard around it, it landed in the follow-up window, or
//     it resumed within the continuation window of a message just sent);
//     continuous capture without that gate is a hot mic;
//   - a wake phrase claims exactly one utterance;
//   - he does not answer himself — the detector is suppressed while he speaks
//     or holds an authorization card (a running turn no longer silences it: V3),
//     but it still *tracks the room*
//     so the threshold is current the moment the follow-up window opens;
//   - the minimum length is measured on the speech, not on the padded segment.
//
// The OFF dictation mode is v1's mic mute: nothing claimed, nothing uploaded,
// and `micFloorSample` clamps every upload at the last unmute, so audio
// captured while the owner believed the mic was off can never ride out as
// pre-roll or as a wake hit's back-dating.

import {
  CAP_RATE, CONTINUE_CHAIN_MS, CONTINUE_MAX_CLAIMS, CONTINUE_MS, FOLLOWUP_MS, MIN_UTTER_MS, PREROLL_MS, Ring, Vad, WAKE_GRACE_MS,
  closeBounds, frameRms, longEnough, msToSamples, samplesToMs, wavBlob,
} from "./vad";

import type { ClaimSource, DropReason, SuppressionReason } from "./voiceDiagnostics";

export interface CaptureHooks {
  /** Suppressed whenever the detector would be listening to him, not the owner. */
  suppressed: () => boolean;
  suppressionReason?: () => SuppressionReason;
  onDrop?: (reason: DropReason, ms: number, source: ClaimSource) => void;
  /** OFF: nothing is claimed and nothing is uploaded. */
  muted: () => boolean;
  onLevel: (level: number) => void;
  /**
   * A claimed utterance began. `tag` is what the follow-up window was opened
   * for (V3: the pane whose reply opened it), when `source` is "follow_up".
   */
  onListening: (source?: ClaimSource, tag?: number | null) => void;
  onIdle: (note?: string) => void;
  /** A finished, claimed utterance. The dictation mode decides what happens next. */
  onUtterance: (wav: Blob, ms: number, source?: ClaimSource, continuation?: number | null) => void;
}

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
   * CONTINUE_MAX_CLAIMS continuation claims and ends CONTINUE_CHAIN_MS after it began.
   */
  chainFrom = -1;
  chainClaims = 0;
  /** Names the window the last message armed, so a caller can take back that one only. */
  private continueToken = 0;
  private tokenSeq = 0;
  wakeHitAt = -1e9;
  segClaimed = false;
  ptt: { from: number } | null = null;
  micFloorSample = 0;
  private followUpTimer: any = null;
  private claimSource: ClaimSource = "unclaimed";
  // Observation only: the real VAD deliberately resets while suppressed.
  private blockedVad = new Vad();
  private blockedReason: DropReason | null = null;
  private blockedAlreadyReported = false;
  private wasBlocked = false;

  private drop(reason: DropReason, ms: number, source = this.claimSource) {
    try { this.hooks.onDrop?.(reason, ms, source); } catch { /* diagnostics cannot change capture */ }
  }

  private observeBlocked(rms: number, n: number, reason: DropReason | null) {
    if (!reason) {
      if (this.blockedVad.open && !this.blockedAlreadyReported && this.blockedReason)
        this.drop(this.blockedReason, samplesToMs(this.blockedVad.voiced), "unclaimed");
      this.blockedVad.reset(); this.blockedReason = null; this.blockedAlreadyReported = false;
      return;
    }
    const event = this.blockedVad.feed(rms, n, this.ring.written);
    if (event === "open") this.blockedReason = reason;
    if (event === "close" || event === "cap") {
      if (!this.blockedAlreadyReported) this.drop(this.blockedReason ?? reason, samplesToMs(this.blockedVad.voiced), "unclaimed");
      this.blockedReason = null; this.blockedAlreadyReported = false;
    }
  }

  constructor(private hooks: CaptureHooks, private now: () => number = () => performance.now()) {}

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
    this.ptt = null;
    this.segClaimed = false;
    this.wakeHitAt = -1e9;
    this.closeFollowUp();
    this.closeContinuation();
  }

  /** Called when the mic unmutes: everything before this point stays local. */
  markUnmute() {
    this.micFloorSample = this.ring.written;
    this.closeContinuation();
  }

  onFrame(frame: Float32Array) {
    this.ring.push(frame);
    const rms = frameRms(frame);
    const muted = this.hooks.muted();
    // Muted, the orb shows nothing: a level meter on a muted mic reads as "he
    // is listening", which is exactly the wrong promise.
    this.hooks.onLevel(muted ? 0 : Math.min(1, rms * 4));
    const suppressed = this.hooks.suppressed();
    // Anything that stops the detector listening ends the message it would
    // have continued: words after a card, a picker or his own voice are a new
    // thought, and need the wake word again.
    if (muted || suppressed) this.closeContinuation();
    const reason: DropReason | null = muted ? "muted" : suppressed ? `suppressed:${this.hooks.suppressionReason?.() ?? "speaking"}` : null;
    if (reason && !this.wasBlocked && this.vad.open && !this.ptt && !this.blockedAlreadyReported) {
      this.drop(reason, samplesToMs(this.vad.voiced));
      this.blockedAlreadyReported = true;
    }
    if (reason && !this.wasBlocked) this.blockedVad.floor = this.vad.floor;
    this.wasBlocked = reason !== null;
    if (this.ptt) this.blockedAlreadyReported = true;
    this.observeBlocked(rms, frame.length, reason);
    if (muted || suppressed) {
      // Still track the room, so the threshold is current the moment the
      // follow-up window opens; just never open an utterance.
      if (this.vad.open) {
        this.vad.open = false;
        this.segClaimed = false;
      }
      this.vad.feed(rms, frame.length, this.ring.written);
      return;
    }
    const event = this.vad.feed(rms, frame.length, this.ring.written);
    if (event) this.onVadEvent(event);
  }

  private onVadEvent(event: "open" | "close" | "cap") {
    if (event === "open") {
      const continuing = this.continuing();
      this.claimSource = this.ptt ? "ptt" : this.conversing() ? "follow_up" : this.now() - this.wakeHitAt < WAKE_GRACE_MS ? "wake"
        : continuing ? "continuation" : "unclaimed";
      this.segClaimed = this.conversing() || this.now() - this.wakeHitAt < WAKE_GRACE_MS || continuing;
      if (this.claimSource === "continuation") this.chainClaims++;
      if (this.segClaimed) {
        // Saying his name claims exactly one utterance.
        this.wakeHitAt = -1e9;
        const tag = this.claimSource === "follow_up" ? this.followUpTag : null;
        this.closeFollowUp();
        this.hooks.onListening(this.claimSource, tag);
      }
      return;
    }
    const { from, to } = closeBounds(this.vad, this.ring.written, event);
    const claimed = this.segClaimed;
    this.segClaimed = false;
    if (!claimed) { if (!this.ptt) this.drop("unclaimed", samplesToMs(this.vad.voiced)); return; }
    if (!longEnough(this.vad)) {
      this.drop("too_short", samplesToMs(this.vad.voiced));
      this.vad.voiced = 0;
      this.openFollowUp(); // he was addressed; give the owner the window back
      this.hooks.onIdle("DIDN'T CATCH THAT");
      return;
    }
    this.send(from, to);
  }

  private send(from: number, to: number) {
    from = Math.max(from, this.micFloorSample);
    const samples = this.ring.slice(from, to);
    this.vad.voiced = 0;
    if (!samples.length) { this.drop("too_short", 0); return; }
    // The message ended here. Whatever follows soon enough continues it --
    // except after push-to-talk, whose release is the owner's own full stop:
    // the next words need the key (or his name) again. Only a wake or
    // follow-up claim starts a chain; a continuation extends it while claims
    // remain. Armed before the hook runs, so a caller that refuses the clip
    // can take its window back.
    const source = this.claimSource;
    if (source === "wake" || source === "follow_up") { this.chainFrom = from; this.chainClaims = 0; }
    const arm = (source === "wake" || source === "follow_up" || source === "continuation")
      && this.chainClaims < CONTINUE_MAX_CLAIMS;
    this.continueToken = arm ? ++this.tokenSeq : 0;
    this.continueUntil = arm ? this.ring.written + msToSamples(CONTINUE_MS) : -1;
    this.hooks.onUtterance(wavBlob(samples, CAP_RATE), samplesToMs(samples.length), source, arm ? this.continueToken : null);
  }

  // ---- wake ---------------------------------------------------------------

  /**
   * A wake phrase was heard. Nothing starts recording here — the mic has been
   * running all along. The recognizer reports a phrase several hundred ms
   * after it was said, so the detector usually already has an utterance open:
   * claim that one, and back-date its start far enough to cover the lag.
   */
  onWake(): "claimed" | "armed" {
    if (this.hooks.muted()) return "armed"; // a result already in flight can land late
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
      this.hooks.onListening("wake", null);
      return "claimed";
    }
    return "armed";
  }

  // ---- push to talk -------------------------------------------------------

  press() {
    if (this.hooks.muted()) return false; // muting takes away recording
    if (this.ptt) return false;
    this.claimSource = "ptt";
    this.ptt = { from: Math.max(0, this.ring.written - msToSamples(PREROLL_MS)) };
    this.closeFollowUp();
    this.closeContinuation();
    this.hooks.onListening("ptt", null);
    return true;
  }

  release() {
    if (!this.ptt) return;
    const { from } = this.ptt;
    this.ptt = null;
    const to = this.ring.written;
    if (samplesToMs(to - from) < MIN_UTTER_MS + PREROLL_MS) {
      this.drop("too_short", samplesToMs(to - from), "ptt");
      this.hooks.onIdle("TOO SHORT");
      return;
    }
    // The detector must not immediately re-open on the tail of what was just
    // sent, and the room may have changed while the owner held the orb.
    this.vad.quiet = 0;
    this.vad.run = 0;
    this.vad.open = false;
    this.segClaimed = false;
    this.send(from, to);
  }

  /** Test hook: script a room through the real detector, no microphone. */
  feedMs(ms: number, amplitude: number, frameSamples = 1024) {
    const total = msToSamples(ms);
    let done = 0;
    while (done < total) {
      const n = Math.min(frameSamples, total - done);
      const frame = new Float32Array(n);
      // A frame of N samples at a constant amplitude has exactly that RMS.
      frame.fill(amplitude);
      this.onFrame(frame);
      done += n;
    }
  }
}
