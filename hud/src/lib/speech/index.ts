// Wiring for the real window: the Web Audio player, the Web Lock gate and the
// `/say` route behind one controller. Tests build the controller from fakes
// instead (`controller.test.ts`); this file is the only place the three real
// parts meet.

import { api } from "../../api";
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

const realClock: Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (h) => window.clearTimeout(h as number),
};

export interface Speech {
  controller: SpeechController;
  gate: SpeakerGate;
  player: WebAudioPlayer;
  /** Each chat's voice and mute (V7), kept by the daemon. */
  voices: ThreadVoices;
  dispose(): void;
}

export function createSpeech(): Speech {
  const gate = new SpeakerGate(browserLocks());
  const player = new WebAudioPlayer();
  const voices = new ThreadVoices({
    list: () => api.speechVoices(),
    change: (thread, change) => api.speechVoice(thread, change),
  });
  const controller = new SpeechController({
    say: (text, opts) => api.say(text, { voice: opts.voice, localOnly: SAY_LOCAL_ONLY, signal: opts.signal }),
    player,
    clock: realClock,
    gate,
    voiceFor: voices.voiceFor,
  });
  void voices.load();
  controller.setEnabled(loadReadAloud());
  const unbind = bindFocus(gate, window, () => document.hasFocus());
  return {
    controller, gate, player, voices,
    dispose: () => { unbind(); controller.dispose(); gate.dispose(); player.dispose(); },
  };
}
