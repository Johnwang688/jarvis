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

/** Whether a keyup ends a hold Ctrl+D started: D let go, or Ctrl. */
export function endsTalk(e: { key: string; code?: string }): boolean {
  if (e.key === "Control") return true;
  if (e.code === "KeyD") return true;
  return e.key.length === 1 && e.key.toLowerCase() === "d";
}
