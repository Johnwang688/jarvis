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
//     a wake phrase was heard around it, or it landed in the follow-up window);
//     continuous capture without that gate is a hot mic;
//   - a wake phrase claims exactly one utterance;
//   - he does not answer himself — the detector is suppressed while he speaks,
//     thinks, or holds an authorization card, but it still *tracks the room*
//     so the threshold is current the moment the follow-up window opens;
//   - the minimum length is measured on the speech, not on the padded segment.
//
// The OFF dictation mode is v1's mic mute: nothing claimed, nothing uploaded,
// and `micFloorSample` clamps every upload at the last unmute, so audio
// captured while the owner believed the mic was off can never ride out as
// pre-roll or as a wake hit's back-dating.

import {
  CAP_RATE, FOLLOWUP_MS, MIN_UTTER_MS, PREROLL_MS, Ring, Vad, WAKE_GRACE_MS,
  closeBounds, frameRms, longEnough, msToSamples, samplesToMs, wavBlob,
} from "./vad";

export interface CaptureHooks {
  /** Suppressed whenever the detector would be listening to him, not the owner. */
  suppressed: () => boolean;
  /** OFF: nothing is claimed and nothing is uploaded. */
  muted: () => boolean;
  onLevel: (level: number) => void;
  onListening: () => void;
  onIdle: (note?: string) => void;
  /** A finished, claimed utterance. The dictation mode decides what happens next. */
  onUtterance: (wav: Blob, ms: number) => void;
}

export class Capture {
  ring = new Ring();
  vad = new Vad();
  followUpUntil = 0;
  wakeHitAt = -1e9;
  segClaimed = false;
  ptt: { from: number } | null = null;
  micFloorSample = 0;
  private followUpTimer: any = null;

  constructor(private hooks: CaptureHooks, private now: () => number = () => performance.now()) {}

  conversing() {
    return this.now() < this.followUpUntil;
  }

  /** Opened when he stops speaking: the next utterance is simply the next turn. */
  openFollowUp() {
    if (this.hooks.muted()) return; // a muted mic must not promise "just speak"
    this.followUpUntil = this.now() + FOLLOWUP_MS;
    clearTimeout(this.followUpTimer);
    this.followUpTimer = setTimeout(() => {
      this.followUpUntil = 0;
    }, FOLLOWUP_MS);
  }

  closeFollowUp() {
    this.followUpUntil = 0;
    clearTimeout(this.followUpTimer);
  }

  /** Called when the mic unmutes: everything before this point stays local. */
  markUnmute() {
    this.micFloorSample = this.ring.written;
  }

  onFrame(frame: Float32Array) {
    this.ring.push(frame);
    const rms = frameRms(frame);
    const muted = this.hooks.muted();
    // Muted, the orb shows nothing: a level meter on a muted mic reads as "he
    // is listening", which is exactly the wrong promise.
    this.hooks.onLevel(muted ? 0 : Math.min(1, rms * 4));
    if (muted || this.hooks.suppressed()) {
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
      this.segClaimed = this.conversing() || this.now() - this.wakeHitAt < WAKE_GRACE_MS;
      if (this.segClaimed) {
        // Saying his name claims exactly one utterance.
        this.wakeHitAt = -1e9;
        this.closeFollowUp();
        this.hooks.onListening();
      }
      return;
    }
    const { from, to } = closeBounds(this.vad, this.ring.written, event);
    const claimed = this.segClaimed;
    this.segClaimed = false;
    if (!claimed) return;
    if (!longEnough(this.vad)) {
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
    if (!samples.length) return;
    this.hooks.onUtterance(wavBlob(samples, CAP_RATE), samplesToMs(samples.length));
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
      this.wakeHitAt = -1e9; // spent: this hit claims this utterance and no other
      this.vad.openedAt = Math.min(
        this.vad.openedAt,
        this.ring.written - msToSamples(WAKE_GRACE_MS + PREROLL_MS),
      );
      this.closeFollowUp();
      this.hooks.onListening();
      return "claimed";
    }
    return "armed";
  }

  // ---- push to talk -------------------------------------------------------

  press() {
    if (this.hooks.muted()) return false; // muting takes away recording, not the interrupt
    if (this.ptt) return false;
    this.ptt = { from: Math.max(0, this.ring.written - msToSamples(PREROLL_MS)) };
    this.closeFollowUp();
    this.hooks.onListening();
    return true;
  }

  release() {
    if (!this.ptt) return;
    const { from } = this.ptt;
    this.ptt = null;
    const to = this.ring.written;
    if (samplesToMs(to - from) < MIN_UTTER_MS + PREROLL_MS) {
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
