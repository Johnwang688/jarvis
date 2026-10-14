// Wiring for the real window: the Web Audio player, the Web Lock gate and the
// `/say` route behind one controller. Tests build the controller from fakes
// instead (`controller.test.ts`); this file is the only place the three real
// parts meet.

import { api } from "../../api";
import { SelfVoiceGate } from "../selfvoice";
import { SpeechController } from "./controller";
import { WebAudioPlayer } from "./player";
import type { Clock } from "./queue";
import { SAY_LOCAL_ONLY } from "./policy";
import { SpeakerGate, bindFocus, browserLocks } from "./speaker";
import { ThreadVoices } from "./voices";

const KEY = "jarvis.readAloud";

/** Read-aloud on/off, a per-window convenience kept in localStorage (default on). */
export function loadReadAloud(storage?: Pick<Storage, "getItem">): boolean {
  try {
    return (storage ?? window.localStorage).getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function saveReadAloud(on: boolean, storage?: Pick<Storage, "setItem">): void {
  try {
    (storage ?? window.localStorage).setItem(KEY, on ? "on" : "off");
  } catch {
    /* a window with storage blocked still works, it just forgets */
  }
}

const GATE_KEY = "jarvis.selfVoiceGate";

/** Self-voice gating on/off, a per-window convenience (default on). Off is V6's blanket hold: the mic is held for the whole of a reply. */
export function loadSelfVoiceGate(storage?: Pick<Storage, "getItem">): boolean {
  try {
    return (storage ?? window.localStorage).getItem(GATE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function saveSelfVoiceGate(on: boolean, storage?: Pick<Storage, "setItem">): void {
  try {
    (storage ?? window.localStorage).setItem(GATE_KEY, on ? "on" : "off");
  } catch {
    /* a window with storage blocked still works, it just forgets */
  }
}

export const realClock: Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (h) => window.clearTimeout(h as number),
};

export interface Speech {
  controller: SpeechController;
  gate: SpeakerGate;
  player: WebAudioPlayer;
  /** What the window is playing, as the reference for telling his voice from the owner's (V9). */
  selfVoice: SelfVoiceGate;
  /** Each chat's voice and mute (V7), kept by the daemon. */
  voices: ThreadVoices;
  dispose(): void;
}

export function createSpeech(): Speech {
  const gate = new SpeakerGate(browserLocks());
  const player = new WebAudioPlayer();
  // The player is the reference: the gate hears exactly what Web Audio puts out (V9).
  // No Web Audio, no reference: the gate is then not in force and the blanket hold applies.
  const hasWebAudio = typeof window !== "undefined" && !!(window.AudioContext ?? (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext);
  const selfVoice = new SelfVoiceGate(undefined, hasWebAudio);
  selfVoice.setEnabled(loadSelfVoiceGate());
  player.setReference(selfVoice.reference);
  // An audio device came or went (headphones, a speaker): what was learned of the room is not to be trusted.
  const devices = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
  const onDevices = () => selfVoice.invalidate();
  try { devices?.addEventListener?.("devicechange", onDevices); } catch { /* an environment without the event */ }
  const voices = new ThreadVoices({
    list: () => api.speechVoices(),
    change: (thread, change) => api.speechVoice(thread, change),
  });
  const controller = new SpeechController({
    say: (text, opts) => api.say(text, { voice: opts.voice, localOnly: SAY_LOCAL_ONLY, signal: opts.signal }),
    summarize: (text, opts) => api.speechSummary(text, opts.thread, { signal: opts.signal }),
    player,
    clock: realClock,
    gate,
    voiceFor: voices.voiceFor,
  });
  void voices.load();
  controller.setEnabled(loadReadAloud());
  const unbind = bindFocus(gate, window, () => document.hasFocus());
  return {
    controller, gate, player, selfVoice, voices,
    dispose: () => { unbind(); try { devices?.removeEventListener?.("devicechange", onDevices); } catch { /* ditto */ } controller.dispose(); gate.dispose(); player.dispose(); },
  };
}
