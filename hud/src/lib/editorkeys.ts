// The editor's keys, as pure functions (editor plan §2.2 and §2.10, ED-1).
// The window's one capture-phase key handler (`useLayout` in
// components/Layout.tsx) is where the HUD's keys are taken; these decide what
// it does with a key that arrives while Monaco has focus.

/** How long Monaco waits for a chord's second key before it gives up. */
export const CHORD_MS = 5000;

/** The time Ctrl+K was pressed in an editor and is still waiting, else null. */
export type ChordState = number | null;

export interface KeyLike {
  key: string;
  ctrlKey: boolean;
  metaKey?: boolean;
  altKey: boolean;
  shiftKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
}

const MODIFIERS = new Set(["Control", "Shift", "Alt", "AltGraph", "Meta", "OS", "Hyper", "Super", "CapsLock"]);

const isCtrlK = (e: KeyLike) =>
  (e.ctrlKey || !!e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k";

/**
 * Monaco's Ctrl+K chords, followed from outside (decisions E-10). Ctrl+B
 * folds the sidebar from the editor too — except as the second key of a
 * Ctrl+K chord, where it is Monaco's (Ctrl+K Ctrl+B sets the selection
 * anchor). The same holds for every HUD key: Ctrl+K Ctrl+0 is Monaco's fold
 * all, not the HUD's zoom reset, as in VS Code.
 *
 * Feed it every keydown, with whether the key is going into an editor and
 * the time. `second` says this key finishes a chord, so the HUD leaves it to
 * Monaco. A chord starts only with Ctrl+K typed in an editor, and waits as
 * Monaco does: until the next key that is not a bare modifier, for at most
 * `CHORD_MS`. Auto-repeat and IME composition change nothing (a held Ctrl+K
 * is still one chord start; a key that belongs to the IME is not Monaco's).
 */
export function chordStep(
  state: ChordState, e: KeyLike, inEditor: boolean, now: number,
): { state: ChordState; second: boolean } {
  if (e.isComposing || e.repeat || MODIFIERS.has(e.key)) return { state, second: false };
  if (state !== null && now - state <= CHORD_MS) return { state: null, second: inEditor };
  if (inEditor && isCtrlK(e)) return { state: now, second: false };
  return { state: null, second: false };
}

/**
 * Ctrl+S (Cmd+S): save. Matched on `key`, as the layout keys are, and never
 * with Alt — AltGr arrives as Ctrl+Alt, and AltGr+S types a character on some
 * layouts.
 */
export function isSaveKey(e: KeyLike): boolean {
  return (e.ctrlKey || !!e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "s";
}
