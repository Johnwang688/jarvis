// Whose word was it? The local recognizer hears the speakers as well as the owner, and it reports a word a few hundred
// milliseconds after it was said (V11). Two cases, one rule: **a hit while his voice was audible (within the spotter's
// lag) is his own word unless the self-voice gate released owner speech in that same look-back.**
//
//   - "Jarvis" (wake): `Capture.onWake(heardHim)` applies it (it claims only an utterance the owner opened).
//   - "Jarvis stop" under a card: `OwnerMark` supplies the second half, because the card suppresses the capture and
//     there is no open utterance to ask. A frame the gate judged "not his voice, though he is audible" is the owner
//     talking over him (selfvoice.ts: more than 2x the echo the learned room predicts). Before the room is learned
//     every frame while he is audible is his, so nothing the owner says over his first second can be a command.

import type { SelfFrame } from "../selfvoice";

/** Remembers when the gate last released a frame as the owner's voice over his. */
export class OwnerMark {
  private at = -Infinity;

  /** One gate verdict, in order, at `now` (ms, any monotonic clock). */
  note(verdict: SelfFrame | null | undefined, now: number): void {
    if (verdict && verdict.active && !verdict.self) this.at = now;
  }

  /** The owner's voice was released over his within the last `ms`. */
  within(ms: number, now: number): boolean {
    return now - this.at <= ms;
  }

  reset(): void {
    this.at = -Infinity;
  }
}

/** True when a hit should be set aside as possibly his own voice. */
export function hisOwnWord(audibleWithinLag: boolean, ownerSpokeWithinLag: boolean): boolean {
  return audibleWithinLag && !ownerSpokeWithinLag;
}
