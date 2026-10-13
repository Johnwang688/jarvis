// One window speaks (V6, plan §3.9). The HUD can be open in several windows,
// each hearing the same event stream; if each read it aloud, the owner would
// hear every reply twice, offset. A Web Lock ("jarvis-speech") names the
// speaking window. Focusing a window takes the lock from whichever held it, so
// the window the owner is looking at is the one that talks (open question 15);
// the others build no queue.
//
// Pure: it works against a `LockManagerLike`, so the tests run two "windows"
// against one fake lock manager with the real `steal` semantics (the holder's
// request promise rejects with an AbortError).

export const SPEECH_LOCK = "jarvis-speech";

export interface LockOptions { mode?: "exclusive"; ifAvailable?: boolean; steal?: boolean }
export interface LockManagerLike {
  request(name: string, options: LockOptions, callback: (lock: unknown) => unknown): Promise<unknown>;
}

export class SpeakerGate {
  private held = false;
  private gen = 0;
  private release: (() => void) | null = null;
  private listeners = new Set<() => void>();
  private disposed = false;

  /** `locks` null (an environment without Web Locks): the only window speaks. */
  constructor(private readonly locks: LockManagerLike | null | undefined) {}

  isSpeaker(): boolean {
    return this.locks ? this.held : true;
  }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  }

  /** This window was focused: take the floor from any other. */
  claim(): void {
    if (this.held) return;
    this.acquire({ mode: "exclusive", steal: true });
  }

  /** This window opened unfocused: take the floor only if nobody has it. */
  offer(): void {
    if (this.held) return;
    this.acquire({ mode: "exclusive", ifAvailable: true });
  }

  /** Give the floor up (the window is going away). */
  dispose(): void {
    this.disposed = true;
    this.gen += 1;
    const release = this.release;
    this.release = null;
    this.held = false;
    release?.();
  }

  private notify(): void {
    for (const cb of [...this.listeners]) {
      try { cb(); } catch { /* a listener never breaks the gate */ }
    }
  }

  private acquire(options: LockOptions): void {
    if (!this.locks || this.disposed) return;
    const gen = ++this.gen;
    const lost = () => {
      if (gen !== this.gen) return;
      const was = this.held;
      this.held = false;
      this.release = null;
      if (was) this.notify();
    };
    let request: Promise<unknown>;
    try {
      request = this.locks.request(SPEECH_LOCK, options, (lock) => {
        if (!lock || gen !== this.gen || this.disposed) return undefined; // taken, or superseded
        this.held = true;
        this.notify();
        return new Promise<void>((resolve) => { this.release = resolve; });
      });
    } catch {
      return;
    }
    request.then(lost, lost);
  }
}

/**
 * Claim the floor whenever the window is focused, and at start: for sure when
 * it is focused, only if free when it is not. Returns the unbind.
 */
export function bindFocus(
  gate: SpeakerGate,
  target: Pick<Window, "addEventListener" | "removeEventListener">,
  hasFocus: () => boolean,
): () => void {
  const onFocus = () => gate.claim();
  target.addEventListener("focus", onFocus);
  if (hasFocus()) gate.claim(); else gate.offer();
  return () => target.removeEventListener("focus", onFocus);
}

/** The browser's lock manager, or null where there is none. */
export function browserLocks(): LockManagerLike | null {
  try {
    const locks = (globalThis as { navigator?: { locks?: LockManagerLike } }).navigator?.locks;
    return locks && typeof locks.request === "function" ? locks : null;
  } catch {
    return null;
  }
}
