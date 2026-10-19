// Guarded Web Storage access for the HUD's per-window preferences. Storage can
// be missing or throw (private window, blocked site data, a sandboxed frame
// that refuses even the property access): a failed read is "nothing stored" and
// a failed write is dropped, because a preference is never worth a crash.
//
// Each caller keeps its own parse rule (`=== "1"`, `!== "off"`, JSON ...) and
// its default, applied to what `readKey` returns. A caller whose default for a
// throwing storage differs from its default for a missing key must not use this.

type Reader = Pick<Storage, "getItem">;
type Writer = Pick<Storage, "setItem">;

/**
 * A storage to use, or a function that fetches one. Pass a function for
 * `sessionStorage` (or any other global whose mere access may throw), so the
 * access happens inside the guard.
 */
export type StorageArg<T> = T | (() => T);

function pick<T>(storage: StorageArg<T> | null | undefined, fallback: () => T): T {
  if (storage == null) return fallback();
  return typeof storage === "function" ? (storage as () => T)() : storage;
}

/** The stored string, or null when the key is unset or the storage throws. `storage` defaults to `window.localStorage`. */
export function readKey(key: string, storage?: StorageArg<Reader> | null): string | null {
  try {
    return pick(storage, () => window.localStorage).getItem(key);
  } catch {
    return null;
  }
}

/** Store `value`; a storage that throws is ignored. `storage` defaults to `window.localStorage`. */
export function writeKey(key: string, value: string, storage?: StorageArg<Writer> | null): void {
  try {
    pick(storage, () => window.localStorage).setItem(key, value);
  } catch {
    /* a window with storage blocked still works; it just forgets */
  }
}
