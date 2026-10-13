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
 * It waits exactly as Monaco's keybinding service does, because the two must
 * agree on which key ends the chord (review of PR #43: a key typed in the
 * chat box ended ours but not Monaco's, so the Ctrl+B after it both folded
 * the sidebar and set the anchor):
 *  - a chord starts only with Ctrl+K typed in an editor;
 *  - only a key typed in an editor ends it — Monaco's one keybinding service
 *    hears every editor's keys and no other key — so a key anywhere else
 *    leaves it waiting;
 *  - bare modifiers, auto-repeat and IME composition change nothing;
 *  - it gives up after `CHORD_MS`, and when the window loses focus
 *    (`useLayout` resets it on `blur`; Monaco checks `document.hasFocus()`).
 *
 * Feed it every keydown, with whether the key is going into an editor and
 * the time. `second` says this key finishes a chord, so the HUD leaves it to
 * Monaco.
 */
export function chordStep(
  state: ChordState, e: KeyLike, inEditor: boolean, now: number,
): { state: ChordState; second: boolean } {
  const live = state !== null && now - state <= CHORD_MS ? state : null;
  if (e.isComposing || e.repeat || MODIFIERS.has(e.key)) return { state: live, second: false };
  if (!inEditor) return { state: live, second: false };
  if (live !== null) return { state: null, second: true };
  if (isCtrlK(e)) return { state: now, second: false };
  return { state: null, second: false };
}

/**
 * **One tracker for the window** (PR #40 on ED-1): the layout keys
 * (`useLayout`) and push-to-talk (App.tsx) both listen on the window in the
 * capture phase, and both must agree on whether a keydown ends a Monaco
 * chord — push-to-talk leaves a Ctrl+D that does to Monaco, as the layout
 * keys leave Ctrl+B. So the state lives here, and a keydown is stepped once,
 * by whichever listener asks first: the answer is kept per event, so the
 * second asks for the same event and gets the same answer (the listeners'
 * order is their registration order, which a re-run effect changes).
 */
let shared: ChordState = null;
const stepped = new WeakMap<object, boolean>();

/** Step the window's tracker with this keydown (once), and say whether it ends a chord. */
export function chordSecond(e: KeyLike & { timeStamp: number }, inEditor: boolean): boolean {
  const seen = stepped.get(e);
  if (seen !== undefined) return seen;
  const step = chordStep(shared, e, inEditor, e.timeStamp);
  shared = step.state;
  stepped.set(e, step.second);
  return step.second;
}

/** The window lost focus: Monaco leaves a pending chord, and so does the tracker. */
export function resetChord(): void {
  shared = null;
}

/**
 * Ctrl+S (Cmd+S): save. Matched on `key`, as the layout keys are, and never
 * with Alt — AltGr arrives as Ctrl+Alt, and AltGr+S types a character on some
 * layouts.
 */
export function isSaveKey(e: KeyLike): boolean {
  return (e.ctrlKey || !!e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "s";
}
