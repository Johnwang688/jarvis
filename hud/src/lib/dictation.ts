// The dictation send mode (design §12.1): a three-position control deciding
// only what happens to a *finished* utterance. The v1 capture pipeline runs
// unchanged underneath it.
//
//   AUTO    send when speech ends (v1's behaviour)
//   REVIEW  the transcript lands in the input box and the box is focused;
//           nothing is ever sent without a click or an Enter
//   OFF     v1's mic mute: nothing claimed, nothing uploaded, the wake
//           recognizer stopped outright (it streams audio to Google's speech
//           service — a muted mic must stop that too)
//
// A fresh window boots into REVIEW, the mic-fails-toward-muted reasoning
// applied to sending: a window that restarts into a mic that sends on its own
// is the wrong surprise. The choice persists in localStorage.

export type DictationMode = "auto" | "review" | "off";

export const DICTATION_MODES: DictationMode[] = ["auto", "review", "off"];
export const DEFAULT_MODE: DictationMode = "review";
const KEY = "jarvis.dictation";

export function isMode(value: unknown): value is DictationMode {
  return typeof value === "string" && (DICTATION_MODES as string[]).includes(value);
}

export function loadMode(storage?: Pick<Storage, "getItem">): DictationMode {
  try {
    const store = storage ?? window.localStorage;
    const raw = store.getItem(KEY);
    return isMode(raw) ? raw : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function saveMode(mode: DictationMode, storage?: Pick<Storage, "setItem">) {
  try {
    (storage ?? window.localStorage).setItem(KEY, mode);
  } catch {
    /* a window with storage blocked still works, it just forgets */
  }
}

/** True while the microphone is muted — the OFF position and nothing else. */
export const isMuted = (mode: DictationMode) => mode === "off";

/** True when a finished utterance may be uploaded for transcription at all. */
export const mayCapture = (mode: DictationMode) => mode !== "off";

/** True when a *transcribed* utterance is sent without the owner pressing send. */
export const sendsOnItsOwn = (mode: DictationMode) => mode === "auto";

export type UtteranceOutcome = "discard" | "send" | "review";

/**
 * What to do with a finished, *claimed* utterance. Nothing else decides this —
 * the one place the mode is consulted, so AUTO and REVIEW cannot drift apart.
 */
export function outcomeFor(mode: DictationMode): UtteranceOutcome {
  if (mode === "off") return "discard";
  return mode === "auto" ? "send" : "review";
}

/**
 * The line under the orb (PR #29, where the input bar's hint used to be): what
 * the selected chat's turn is doing, else that a card is waiting for an
 * answer, else that the mic is muted, else what the orb is doing — nothing
 * while it idles. Only OFF ever says MUTED: every other line is an invitation
 * to speak, or silence, and a muted mic must never look like it is listening
 * (nor a live one look muted).
 */
export function orbLine(p: { status?: string; mode: DictationMode; approvals: number; orb: string }): string {
  if (p.status) return p.status;
  if (p.approvals > 0) return "ANSWER THE AUTHORIZATION";
  if (isMuted(p.mode)) return "MIC MUTED";
  return p.orb === "idle" ? "" : p.orb;
}

/** The folded sidebar's one mode button cycles OFF → REVIEW → AUTO → OFF: one
 * click from muted never lands on a mic that sends on its own (the reasoning
 * that boots a fresh window into REVIEW). */
export function nextMode(mode: DictationMode): DictationMode {
  return mode === "off" ? "review" : mode === "review" ? "auto" : "off";
}
