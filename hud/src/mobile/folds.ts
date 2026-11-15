// Viewer convenience only. Explicit choices outlive changing chat activity and sign out.
export const FOLDS_KEY = "m.chats.folded";

export interface ProjectFolds {
  /** true means folded; without an owner choice, active projects open. */
  get(projectId: string, active?: boolean): boolean;
  set(projectId: string, folded: boolean): void;
  subscribe(listener: () => void): () => void;
}

export function createProjectFolds(storage?: Pick<Storage, "getItem" | "setItem"> | null): ProjectFolds {
  const choices = new Map<string, boolean>();
  const listeners = new Set<() => void>();
  // Resolve localStorage inside each try: even accessing the property can throw.
  const target = () => storage === undefined ? window.localStorage : storage;
  try {
    const raw = target()?.getItem(FOLDS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      for (const [id, value] of Object.entries(parsed)) if (typeof value === "boolean") choices.set(id, value);
    }
  } catch { /* blocked or malformed storage: use activity defaults */ }
  return {
    get: (id, active = false) => choices.get(id) ?? !active,
    set(id, folded) {
      if (choices.get(id) === folded) return;
      choices.set(id, folded);
      try { target()?.setItem(FOLDS_KEY, JSON.stringify(Object.fromEntries(choices))); }
      catch { /* the in-memory choice still lasts for this app */ }
      listeners.forEach((listener) => listener());
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}
