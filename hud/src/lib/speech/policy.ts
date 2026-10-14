// The owner's decisions on the voice plan's questions 9, 11 and 15 (answered
// 2026-10-13, docs/plans/2026-10-13-voice-rework.md §7), each one named
// constant or small function here with its test beside it.

import { shownThread, type Chats } from "../chats";
import { PANE_NOS, inWorkspace, type PaneNo, type Workspace } from "../workspace";

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
 * Is the detector held off because Jarvis is speaking? True for the whole of a
 * reply -- playing, buffering between chunks, and the hangover after the last
 * one -- not only while a chunk sounds; false while paused, and while the
 * platform is blocking audio (nothing is audible, so nothing can be echoed).
 */
export function micSuppressedBySpeech(view: { active: boolean; blocked: boolean }): boolean {
  return SUPPRESS_MIC_WHILE_SPEAKING && view.active && !view.blocked;
}

/**
 * V-8 / Q15 -- the threads that are read aloud: every thread shown in any chat
 * pane of the workspace, *drawn or hidden* (a pane ⊞ folded away into `kept`
 * still reads: its thread is open, it is only not on screen). A thread in no
 * pane is silent, and so is a pane showing anything but chat -- a task view,
 * a file -- though its parked conversation is still held in `chats`.
 * Reading is one shared queue, each thread in its own voice (`floor.ts`).
 */
export function speakingThreads(
  ws: Pick<Workspace, "order" | "kept" | "panes">,
  chats: Chats,
  pending: Readonly<Record<PaneNo, string | null>>,
): Set<string> {
  const out = new Set<string>();
  for (const n of PANE_NOS) {
    if (!inWorkspace(ws, n) || ws.panes[n - 1].view !== "chat") continue;
    const id = shownThread(chats[n], pending[n]);
    if (id) out.add(id);
  }
  return out;
}

/**
 * The mic mode changed. Posts the daemon's mute only if policy says the mic
 * mutes speech -- which it does not (Q11).
 */
export function onMicModeChange(micMuted: boolean, postMute: (muted: boolean) => unknown): void {
  if (MIC_OFF_MUTES_SPEECH) postMute(micMuted);
}
