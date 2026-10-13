// Push-to-talk's key (2026-10-10, the owner's ask: "Ctrl + D just like it is
// in Claude"). Hold Ctrl+D while speaking, release to send. It replaced Space,
// which is a character in every box the owner types in: each typing surface
// had to keep Space from bubbling to a document handler, and Monaco did not,
// so a space typed in the editor started the mic and never appeared.
//
// The window listens in the capture phase (App.tsx), so the key works from the
// chat box, Monaco, a menu, a chip or the sidebar: nothing under the cursor
// can stand between the owner and the mic, and nothing under it ever acts on
// the key either — its default (Chrome's bookmark dialog) is always prevented
// and it stops at the window. One exception, the owner's: in a focused
// terminal Ctrl+D is the shell's end of input, left to xterm untouched, the
// way Ctrl+B, Ctrl+Alt+B and Ctrl+_ already are (`terminalTakesKey`). From a
// terminal, push-to-talk is the orb. Under an authorization card the key is
// the card's to ignore: nothing reaches a shell then either, and push-to-talk
// refuses (App.tsx `press`).
//
// Monaco's own Ctrl+D commands moved to Ctrl+Shift+D (lib/monaco.ts).

/** The parts of a KeyboardEvent the rules read: a plain object passes in tests. */
export interface KeyLike {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey?: boolean;
  altKey: boolean;
  shiftKey?: boolean;
  isComposing?: boolean;
  getModifierState?: (key: string) => boolean;
}

/**
 * Ctrl+D and nothing else: no Shift, Alt or Meta, and never AltGr (which
 * arrives as Ctrl+Alt, and as `AltGraph` on some systems), never mid-
 * composition. Matched on `key`, as the layout keys are (lib/layout.ts), so a
 * layout whose D key types another Latin letter (Dvorak's E) is that letter's
 * key. Only where the physical D key types no Latin letter at all (Cyrillic,
 * Greek) is `code` KeyD accepted, as Chrome's own Ctrl+D accepts it.
 */
export function isTalkKey(e: KeyLike): boolean {
  if (!e.ctrlKey || e.altKey || e.shiftKey || e.metaKey) return false;
  if (e.isComposing) return false;
  try {
    if (e.getModifierState?.("AltGraph")) return false;
  } catch {
    /* an event that cannot say is judged on its flags */
  }
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "d") return true;
  return e.code === "KeyD" && !/^[a-z]$/.test(k);
}

export type TalkAction = "talk" | "shell" | null;

/**
 * What the window does with a keydown. `"talk"`: push-to-talk's — its
 * default prevented, stopped at the window, a press unless it is auto-repeat.
 * `"shell"`: a focused terminal's end of input, left alone for xterm (which
 * prevents the default itself; preventing it here would make xterm drop the
 * key). Under a card nothing reaches a shell, so there it is `"talk"` too, and
 * push-to-talk refuses. `null`: not the key.
 */
export function talkKeyAction(e: KeyLike, where: { terminal: boolean; blocked: boolean }): TalkAction {
  if (!isTalkKey(e)) return null;
  return where.terminal && !where.blocked ? "shell" : "talk";
}

/**
 * A `mousedown` handler for a button a pointer must not focus. Its click still
 * fires and focus stays where it was, so no later Space or Enter presses it.
 * Space was push-to-talk until 2026-10-10 and swallowed by the window; since
 * then it presses whatever button has focus, as on any page, and a button the
 * owner has just clicked still has it. So the ones whose second press matters
 * take none: the card's AUTHORIZE and ALWAYS (an aborted click — pressed, slid
 * off — used to leave focus there, and the next Space authorized), the
 * terminal's "Jarvis can read" switch, the folded mic's mode cycle, and the
 * Preview's "Keep its origin" (review of PR #40). Tab still reaches the ones
 * in the Tab order, where a key press is a deliberate one.
 */
export const noPointerFocus = (e: { preventDefault(): void }) => e.preventDefault();

/**
 * Whether keys are going somewhere that types: a text field, a select, an
 * editable element, Monaco or a terminal. A Space there is a character (or
 * opens the select) and says nothing about push-to-talk.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof (el as any).closest !== "function") return false;
  if (el.closest('textarea, select, [contenteditable=""], [contenteditable="true"], .monaco-editor, .termslot, .xterm')) {
    return true;
  }
  if (el.tagName === "INPUT") {
    const type = ((el as HTMLInputElement).type || "text").toLowerCase();
    return !["button", "checkbox", "radio", "range", "color", "file", "image", "reset", "submit"].includes(type);
  }
  return false;
}

/** What the window says, once, the first time Space is pressed outside a
 * typing target after the switch: an owner who holds Space to talk learns why
 * nothing listens. */
export const SPACE_HINT = "PUSH-TO-TALK IS CTRL+D NOW · HOLD IT TO TALK";
export const SPACE_HINT_KEY = "jarvis.hud.spaceHint";

/** Whether a keydown is the one to say SPACE_HINT on: a fresh Space (no
 * modifiers, not auto-repeat) outside a typing target, with no card up. */
export function wantsSpaceHint(
  e: { key: string; code?: string; ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean; repeat?: boolean },
  where: { typing: boolean; blocked: boolean },
): boolean {
  if (e.code !== "Space" && e.key !== " ") return false;
  if (e.ctrlKey || e.altKey || e.metaKey || e.repeat) return false;
  return !where.typing && !where.blocked;
}

/** What the window does with a keydown, given what the hold so far was. */
export type TalkVerdict = "pass" | "monaco" | "swallow" | "press";

/**
 * One Ctrl+D hold, from its first keydown to its keyup (review of PR #40).
 * **A hold is decided where it began**, never one event at a time:
 *  - a hold the window took — push-to-talk, or a press refused under a card —
 *    keeps every **auto-repeat** keydown the window's (`"swallow"`), wherever
 *    focus has gone since: a card answered elsewhere hands focus back to a
 *    terminal, the owner clicks into one while talking, or the window loses
 *    focus to a Preview frame and gets it back. Left to xterm, each repeat
 *    was an end-of-input, and one at an empty prompt closes the shell;
 *  - **a fresh press decides afresh**: a non-repeat Ctrl+D is never swallowed
 *    for an earlier hold, so a keyup the window never saw cannot keep the
 *    shell from its next Ctrl+D;
 *  - **a blur releases the mic but not the hold**: the keyup may never come,
 *    so push-to-talk lets go (`blur()` says to release), while the hold's
 *    repeats stay the window's until D or Ctrl comes up or a fresh press;
 *  - a pending Monaco chord's second key is Monaco's (`"monaco"`: its default
 *    kept from Chrome, Monaco hears it, nothing talks).
 * `"press"` starts push-to-talk; `"pass"` leaves the key alone (not the talk
 * key, or a focused terminal's end of input).
 */
export class TalkHold {
  /** A hold the window took, until D or Ctrl comes up or a fresh press. */
  private holding = false;
  /** Push-to-talk pressed by this hold and not yet let go. */
  private talking = false;

  down(e: KeyLike & { repeat?: boolean }, where: { terminal: boolean; blocked: boolean; chordSecond: boolean }): TalkVerdict {
    if (!isTalkKey(e)) return "pass";
    if (e.repeat) {
      if (this.holding) return "swallow";
    } else {
      this.holding = false;
      if (where.chordSecond) {
        this.holding = true; // its repeats remain the editor's hold across a focus change
        return "monaco";
      }
    }
    if (talkKeyAction(e, where) !== "talk") return "pass";
    if (e.repeat) return "swallow";
    this.holding = true;
    this.talking = true;
    return "press";
  }

  /** A keyup. True when push-to-talk is to be let go: D or Ctrl came up. */
  up(e: { key: string; code?: string }): boolean {
    if (!endsTalk(e)) return false;
    this.holding = false;
    return this.letGo();
  }

  /** The window lost focus. True when push-to-talk is to be let go; the hold stays. */
  blur(): boolean {
    return this.letGo();
  }

  private letGo(): boolean {
    const was = this.talking;
    this.talking = false;
    return was;
  }
}

/** Whether a keyup ends a hold Ctrl+D started: D let go, or Ctrl. */
export function endsTalk(e: { key: string; code?: string }): boolean {
  if (e.key === "Control") return true;
  if (e.code === "KeyD") return true;
  return e.key.length === 1 && e.key.toLowerCase() === "d";
}
