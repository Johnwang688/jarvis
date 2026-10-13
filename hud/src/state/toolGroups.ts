/** Window-local, bounded disclosure state. Never persisted or exported as transcript content. */
const opened = new Map<string, true>();
const listeners = new Map<string, Set<() => void>>();
const changed = (key: string) => { for (const fn of listeners.get(key) ?? []) fn(); };
export const toolGroups = {
  key: (thread: string, firstCall: string, turn = "") => `${thread}|${turn}|${firstCall}`,
  get: (key: string) => opened.has(key),
  subscribe(key: string, fn: () => void) {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key)!.add(fn);
    return () => { const set = listeners.get(key); set?.delete(fn); if (!set?.size) listeners.delete(key); };
  },
  toggle(key: string) {
    if (opened.has(key)) opened.delete(key);
    else {
      opened.set(key, true);
      if (opened.size > 500) { const oldest = opened.keys().next().value!; opened.delete(oldest); changed(oldest); }
    }
    changed(key);
  },
  keys: () => Array.from(opened.keys()),
};
