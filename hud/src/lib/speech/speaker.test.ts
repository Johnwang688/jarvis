import { describe, expect, it } from "vitest";
import { SPEECH_LOCK, SpeakerGate, bindFocus, type LockManagerLike, type LockOptions } from "./speaker";

/** Web Locks semantics for one origin: exclusive, `ifAvailable`, `steal` (the holder's request rejects), and waiting with an abort signal. */
class FakeLocks implements LockManagerLike {
  holder: { reject: (e: unknown) => void; resolve: () => void } | null = null;
  names: string[] = [];
  private waiters: (() => void)[] = [];

  request(name: string, options: LockOptions, callback: (lock: unknown) => unknown): Promise<unknown> {
    this.names.push(name);
    return new Promise((resolve, reject) => {
      const take = async () => {
        this.holder = { reject, resolve: () => resolve(undefined) };
        const held = this.holder;
        try {
          await callback({ name });
        } finally {
          if (this.holder === held) {
            this.holder = null;
            this.waiters.shift()?.();
          }
        }
        resolve(undefined);
      };
      if (!this.holder) { void take(); return; }
      if (options.ifAvailable) { void Promise.resolve(callback(null)).then(() => resolve(undefined)); return; }
      if (options.steal) {
        const old = this.holder;
        this.holder = null;
        old.reject(new DOMException("stolen", "AbortError"));
        void take();
        return;
      }
      // Wait for the holder to let go; the signal gives up the wait.
      const go = () => { void take(); };
      this.waiters.push(go);
      options.signal?.addEventListener("abort", () => {
        this.waiters = this.waiters.filter((w) => w !== go);
        reject(new DOMException("aborted", "AbortError"));
      });
    });
  }
}

const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

describe("one window speaks", () => {
  it("lets exactly one of two windows speak, and focus moves it", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    a.claim();
    await tick();
    expect([a.isSpeaker(), b.isSpeaker()]).toEqual([true, false]);
    b.claim();           // the owner focuses window B
    await tick();
    expect([a.isSpeaker(), b.isSpeaker()]).toEqual([false, true]);
    a.claim();           // and back
    await tick();
    expect([a.isSpeaker(), b.isSpeaker()]).toEqual([true, false]);
    expect(new Set(locks.names)).toEqual(new Set([SPEECH_LOCK]));
  });

  it("an unfocused window takes the floor only when nobody has it", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    a.claim();
    await tick();
    b.offer();
    await tick();
    expect([a.isSpeaker(), b.isSpeaker()]).toEqual([true, false]);
    const c = new SpeakerGate(new FakeLocks());
    c.offer();
    await tick();
    expect(c.isSpeaker()).toBe(true);
  });

  it("tells the window that lost the floor", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    const seen: boolean[] = [];
    a.subscribe(() => seen.push(a.isSpeaker()));
    a.claim();
    await tick();
    b.claim();
    await tick();
    expect(seen).toEqual([true, false]);
  });

  it("a window waiting for the floor gets it when the speaking window closes, with no focus event", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    a.claim();
    await tick();
    b.offer();
    await tick();
    expect(b.isSpeaker()).toBe(false);
    a.dispose();
    await tick();
    expect(b.isSpeaker()).toBe(true);
  });

  it("a waiting window that is then focused takes the floor at once, and the wait does not leak it back", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    a.claim();
    await tick();
    b.offer();
    await tick();
    b.claim();
    await tick();
    expect([a.isSpeaker(), b.isSpeaker()]).toEqual([false, true]);
    b.dispose();
    await tick();
    expect(b.isSpeaker()).toBe(false);
  });

  it("a window that closes gives the floor back for the next to take", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    const b = new SpeakerGate(locks);
    a.claim();
    await tick();
    a.dispose();
    await tick();
    expect(a.isSpeaker()).toBe(false);
    b.offer();
    await tick();
    expect(b.isSpeaker()).toBe(true);
  });

  it("claiming twice does not lose the floor to itself", async () => {
    const locks = new FakeLocks();
    const a = new SpeakerGate(locks);
    a.claim();
    await tick();
    a.claim();
    a.claim();
    await tick();
    expect(a.isSpeaker()).toBe(true);
  });

  it("without Web Locks the window speaks", () => {
    expect(new SpeakerGate(null).isSpeaker()).toBe(true);
  });

  it("binds focus: a focused start claims, an unfocused start offers, a focus event claims", async () => {
    const locks = new FakeLocks();
    const handlers: Record<string, () => void> = {};
    const target = {
      addEventListener: (n: string, f: () => void) => { handlers[n] = f; },
      removeEventListener: (n: string) => { delete handlers[n]; },
    } as unknown as Pick<Window, "addEventListener" | "removeEventListener">;
    const other = new SpeakerGate(locks);
    other.claim();
    await tick();
    const gate = new SpeakerGate(locks);
    const unbind = bindFocus(gate, target, () => false);
    await tick();
    expect(gate.isSpeaker()).toBe(false);
    handlers.focus();
    await tick();
    expect(gate.isSpeaker()).toBe(true);
    expect(other.isSpeaker()).toBe(false);
    unbind();
    expect(handlers.focus).toBeUndefined();
  });
});
