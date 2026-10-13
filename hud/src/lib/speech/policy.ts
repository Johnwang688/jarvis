// The owner's open questions, as the plan's recommended answers (V6).
//
// Each is one named constant or one small function here, so an answer that
// differs is a one-line change with its test beside it. They are listed under
// "Pending owner confirmation" in the V6 pull request.

import { shownThread, type Chats } from "../chats";
import type { PaneNo } from "../workspace";

/**
 * Q15 -- which window speaks. "last-focused": focusing a window takes the Web
 * Lock (`speaker.ts#bindFocus`). The other answer would be "first-opened".
 */
export const SPEAKER_WINDOW: "last-focused" | "first-opened" = "last-focused";

/**
 * Q11 -- the mic's OFF no longer mutes Jarvis. It used to post `/mute`, v1's
 * voice-out flag; the two were coupled by accident. Mic OFF now stops the mic
 * and nothing else; read-aloud has its own switch (Settings -> Voice).
 */
export const MIC_OFF_MUTES_SPEECH = false;

/** Q11 -- read-aloud never uses cloud TTS: `/say` is asked `local_only`. */
export const SAY_LOCAL_ONLY = true;

/** Q11 -- a chunk the local voice could not make is skipped, and the strip says so. */
export const SKIP_FAILED_CHUNKS = true;

/**
 * V6 keeps v1's rule that the detector is suppressed while he speaks (he does
 * not answer himself). Self-voice gating, which replaces it, is V9.
 */
export const SUPPRESS_MIC_WHILE_SPEAKING = true;

/**
 * Q15 / V-8 -- the threads that are read aloud: in V6, the selected chat's
 * only; task views and threads in no pane are silent. V7 widens this to every
 * chat pane, drawn or hidden, and nothing else here changes.
 */
export function speakingThreads(
  chats: Chats,
  pending: Readonly<Record<PaneNo, string | null>>,
  selected: PaneNo | null,
): Set<string> {
  const out = new Set<string>();
  if (selected === null) return out;
  const id = shownThread(chats[selected], pending[selected]);
  if (id) out.add(id);
  return out;
}

/**
 * The mic mode changed. Posts the daemon's mute only if policy says the mic
 * mutes speech -- which it does not (Q11).
 */
export function onMicModeChange(micMuted: boolean, postMute: (muted: boolean) => unknown): void {
  if (MIC_OFF_MUTES_SPEECH) postMute(micMuted);
}
