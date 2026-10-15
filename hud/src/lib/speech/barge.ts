// Barge-in (voice rework V9, decision V-7, owner's answers Q7 and Q13).
//
// The owner talks over Jarvis: he pauses, and then
//   - a command ("Jarvis pause / resume / skip / stop") is run and the pause
//     simply stands or ends as the command says;
//   - nothing came of it (a too-short sound, an empty transcript, a failed
//     upload, text that waited in the REVIEW box, a message cancelled, a voice
//     that was not the owner's, ten seconds with nothing happening) and he
//     carries on, restarting the sentence he was in (Q13);
//   - a message was sent or steered: the message he was in the middle of is
//     skipped, and the rest of the queue goes on.
//
// **What pauses him (Q7, stricter than the plan's first draft).** Before a voice
// is enrolled, only the wake word "Jarvis" or the talk key (Ctrl+D, the orb) pause
// playback: other voices, the TV, a passer-by do not. After enrollment (V12) the
// verified owner's voice may too: `setVerifiedVoice` is that seam, and nothing in
// V9 turns it on. The wake word is itself gated against his own voice
// (`Capture.onWake`, selfvoice.ts), so a reply that says "Jarvis stop" does not
// pause or stop itself.
//
// Output only. This file reaches the speech controller's pause and resume and
// nothing else -- no import, no route, no event that answers an approval (V-6).
// It has a clock for the ten-second safety, injected, and no timers of its own
// beyond that.

import type { Clock } from "./queue";

export type BargeCause = "key" | "wake" | "voice";

/** How one utterance that held him ended. */
export type BargeOutcome =
  | "command"    // a spoken playback command ran (it decides whether he resumes)
  | "sent"       // a message went out or steered a turn: skip what he was saying
  | "boxed"      // the words waited in a box (REVIEW, another chat, a card)
  | "dropped"    // too short, nothing heard, failed, cancelled, the mic went OFF
  | "not_owner"  // V12: the voice was not the enrolled owner's
  | "timeout";   // ten seconds with nothing recording

/** The ten-second safety of the plan's state machine: a hold nothing is feeding ends. */
export const BARGE_HOLD_MS = 10_000;

/** What the barge-in needs of the speech controller. */
export interface BargeTarget {
  /** Pause what is sounding (or about to); false when nothing was. */
  bargeIn(): boolean;
  /** Resume what the barge-in paused; with `skip`, the message in hand is not resumed. */
  endBargeIn(opts?: { skip?: boolean }): void;
}

export interface BargeDeps {
  speech: BargeTarget;
  clock: Clock;
  /** A recording is in progress (a message being spoken, a held talk key): the safety waits for it. */
  recording: () => boolean;
}

export class BargeIn {
  /** The utterances holding him paused. Resumed when the last of them lets go. */
  private holders = new Set<number>();
  private timer: unknown = null;
  /** A message went out from one of the holders: the message he was interrupted in is skipped when the last lets go. */
  private sentAny = false;
  /** Set by V12 after enrollment: a verified owner voice may pause him too (Q7). */
  private verified = false;

  constructor(private readonly deps: BargeDeps) {}

  /** Is he held by a barge-in right now? */
  held(): boolean {
    return this.holders.size > 0;
  }

  /**
   * Seam for V12 (speaker verification). Before enrollment this stays false and
   * only the wake word and the talk key pause him. Nothing in V9 sets it.
   */
  setVerifiedVoice(on: boolean): void {
    this.verified = on;
  }

  /**
   * An utterance began that pauses him: the talk key, or the wake word. `token`
   * names it (the capture's utterance id) so that it, and no other, lets go.
   * Returns whether anything was paused.
   */
  begin(cause: BargeCause, token: number): boolean {
    if (cause === "voice" && !this.verified) return false;
    const paused = this.deps.speech.bargeIn();
    // Nothing was sounding and nothing was held before: there is nothing to resume.
    if (!paused && !this.held()) return false;
    this.holders.add(token);
    this.arm();
    return true;
  }

  /**
   * V12's door: an onset heard over him that the speaker check says is the
   * enrolled owner. Before enrollment this does nothing (Q7).
   */
  verifiedOnset(token: number): boolean {
    return this.verified ? this.begin("voice", token) : false;
  }

  /** The utterance `token` is over, however it ended. The last holder to let go resumes him. */
  end(outcome: BargeOutcome, token: number): void {
    if (!this.holders.delete(token)) return;
    if (outcome === "sent") this.sentAny = true;
    if (this.holders.size > 0) return;
    this.disarm();
    const skip = this.sentAny;
    this.sentAny = false;
    // A command decides for itself (pause stays paused: the controller's own pause
    // cleared the barge-in's mark, so this resumes only what a skip or nothing left).
    this.deps.speech.endBargeIn(skip ? { skip: true } : {});
  }

  /** The window is going away, or the owner took over from the strip: forget every hold. */
  release(): void {
    this.holders.clear();
    this.sentAny = false;
    this.disarm();
  }

  private arm(): void {
    this.disarm();
    this.timer = this.deps.clock.setTimeout(() => {
      this.timer = null;
      // Still being spoken (a long dictation, a held key): the safety is for a hold nothing feeds.
      if (this.deps.recording()) { this.arm(); return; }
      const all = [...this.holders];
      for (const t of all) this.end("timeout", t);
    }, BARGE_HOLD_MS);
  }

  private disarm(): void {
    if (this.timer !== null) this.deps.clock.clearTimeout(this.timer);
    this.timer = null;
  }
}
