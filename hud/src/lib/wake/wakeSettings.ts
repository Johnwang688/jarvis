// The owner's two choices about the local wake word (Settings -> Voice, V11), kept in
// this browser profile's localStorage like the end-of-message pause: sensitivity, and
// whether "Jarvis pause|resume|skip|stop" is heard while an approval card is up (Q10).
// Storage that throws leaves the defaults, and the window works.

import { SENSITIVITY, type Sensitivity } from "./keywords";

const SENSITIVITY_KEY = "jarvis.wakeSensitivity";
const CARD_COMMANDS_KEY = "jarvis.wakeCardCommands";

export const DEFAULT_SENSITIVITY: Sensitivity = "normal";
/**
 * On by default: V11 measured the whole phrases at 29 of 32 on synthetic speech with no false hit in 20 min of
 * dictation (the bare words were 38 to 69%), and the owner asked for it (Q10). It can only pause, resume,
 * skip or stop his voice; it never reaches an approval. The owner can turn it off here.
 */
export const DEFAULT_CARD_COMMANDS = true;

type Reader = Pick<Storage, "getItem">;
type Writer = Pick<Storage, "setItem">;

export function loadSensitivity(storage?: Reader): Sensitivity {
  try {
    const v = (storage ?? window.localStorage).getItem(SENSITIVITY_KEY);
    return v && Object.hasOwn(SENSITIVITY, v) ? (v as Sensitivity) : DEFAULT_SENSITIVITY;
  } catch { return DEFAULT_SENSITIVITY; }
}

export function saveSensitivity(value: Sensitivity, storage?: Writer) {
  try { (storage ?? window.localStorage).setItem(SENSITIVITY_KEY, value); } catch { /* the window just forgets */ }
}

export function loadCardCommands(storage?: Reader): boolean {
  try {
    const v = (storage ?? window.localStorage).getItem(CARD_COMMANDS_KEY);
    return v === "on" ? true : v === "off" ? false : DEFAULT_CARD_COMMANDS;
  } catch { return DEFAULT_CARD_COMMANDS; }
}

export function saveCardCommands(on: boolean, storage?: Writer) {
  try { (storage ?? window.localStorage).setItem(CARD_COMMANDS_KEY, on ? "on" : "off"); } catch { /* ditto */ }
}
