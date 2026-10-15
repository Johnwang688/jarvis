// The one tunable timer a person may want to change: how long a quiet stretch
// ends a message (voice rework V5, decision V-1: "about 2.5 s, tunable"). A
// per-window convenience kept in localStorage behind try/catch, like read-aloud;
// a window with storage blocked uses the default and forgets the choice.
// The segment gap (700 ms) and the continuation window (6 s) are constants in
// vad.ts, not settings.

import { END_PAUSE_MAX_MS, END_PAUSE_MIN_MS, END_PAUSE_MS } from "./vad";

const KEY = "jarvis.hud.voice.endPauseMs";

/** The choices offered, in ms: the default among them. */
export const END_PAUSE_CHOICES: readonly number[] = [1500, 2000, END_PAUSE_MS, 3000, 4000];

export function clampEndPause(ms: number): number {
  return Number.isFinite(ms) ? Math.round(Math.max(END_PAUSE_MIN_MS, Math.min(END_PAUSE_MAX_MS, ms))) : END_PAUSE_MS;
}

export function loadEndPause(storage?: Pick<Storage, "getItem">): number {
  try {
    const raw = (storage ?? window.localStorage).getItem(KEY);
    return raw === null ? END_PAUSE_MS : clampEndPause(Number(raw));
  } catch {
    return END_PAUSE_MS;
  }
}

export function saveEndPause(ms: number, storage?: Pick<Storage, "setItem">): void {
  try {
    (storage ?? window.localStorage).setItem(KEY, String(clampEndPause(ms)));
  } catch {
    /* forgets, still works */
  }
}
