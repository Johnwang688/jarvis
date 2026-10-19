// Settings -> Voice: Noise suppression (voice rework V14, owner decision Q16:
// "build it as a setting, off by default"). A per-window convenience kept in
// localStorage behind try/catch, like read-aloud and the message pause: a window
// with storage blocked starts with it off and forgets the choice. Nothing is
// loaded, fetched or compiled while it is off.

import { readKey, writeKey } from "../storage";

const KEY = "jarvis.hud.voice.noiseSuppression";

export function loadNoiseSuppression(storage?: Pick<Storage, "getItem">): boolean {
  return readKey(KEY, storage) === "1";
}

export function saveNoiseSuppression(on: boolean, storage?: Pick<Storage, "setItem">): void {
  writeKey(KEY, on ? "1" : "0", storage);
}
