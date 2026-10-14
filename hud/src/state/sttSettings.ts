import { api } from "../api";
import type { SttSettings } from "../lib/sttSettings";

// The window's copy of the daemon's speech-to-text switches (V13). Memory only;
// the daemon owns them. `null` means "not known yet" and is treated as the
// closed answer wherever it matters (the wake-word recognizer stays off).
export const STT_BACKSTOP_MS = 60_000;
export const STT_UNKNOWN_RETRY_MS = 15_000;
let value: SttSettings | null = null;
let request = 0;
const listeners = new Set<() => void>();
const commit = (next: SttSettings | null) => { value = next; for (const notify of listeners) { try { notify(); } catch { /* observers cannot affect it */ } } };

export const sttSettingsStore = {
  subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  snapshot: () => value,
  /** What a change's answer says, so every part of the window agrees at once. */
  apply(next: SttSettings) { ++request; commit(next); },
  async refresh() {
    const seq = ++request;
    try { const next = await api.sttSettings(); if (seq === request) commit(next); }
    catch { /* keep what is known; unknown stays closed */ }
  },
};

/**
 * Chrome's wake-word recognizer (`webkitSpeechRecognition`) streams the microphone
 * to Google's speech service whatever the daemon's backend is, so it may run only
 * while uploading audio is allowed. Unknown is not allowed.
 */
export const wakeRecognizerAllowed = (s: SttSettings | null): boolean => s !== null && s.cloudFallback === true;
