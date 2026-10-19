// The one list of keys that only modify another (2026-10-18). Two copies used
// to disagree: frameguard also exempted CapsLock and Fn, remote also Super,
// Hyper and FnLock. Both consumers are safety checks, and for each a key on
// this list is *exempt*:
//   - lib/frameguard + state/frameguard: a modifier keydown does not revoke a
//     pointer-over or load grant (the owner typed nothing);
//   - lib/remote + lib/drive: a lone modifier is never sent to the page and
//     never counts as the owner taking over from a driving agent.
// So the list is the keys both copies agreed on (their intersection): a
// longer list would exempt more keys from both checks. CapsLock, Fn, Super,
// Hyper and FnLock now revoke a grant and count as a takeover, as some of them
// already did in one place or the other. (lib/editorkeys.ts has its own list for
// editor chords; that is not a safety check and is left alone.)
export const MODIFIER_KEYS: ReadonlySet<string> = new Set([
  "Shift", "Control", "Alt", "Meta", "AltGraph", "OS",
]);

/** A key that only modifies another: pressing one alone is not "the owner typed something". */
export function isModifierKey(key: unknown): boolean {
  return typeof key === "string" && MODIFIER_KEYS.has(key);
}
