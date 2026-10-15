// The chats' voices as the window knows them (V7, decision V-9).
//
// The daemon assigns a voice to each thread -- sticky, from the installed local
// voices -- and keeps the owner's mute (`/speech/voices`); this is the window's
// copy. It is asked for a voice when a thread starts to be read (`assign`), when
// a chunk is synthesized (`voiceFor`), and told when another window changes one
// (the bus's `thread_voice`). Nothing here speaks or decides who is read.

export interface ThreadVoice { voice: string | null; muted: boolean }

export interface VoicesApi {
  list(): Promise<{ threads: Record<string, ThreadVoice>; pool: string[] }>;
  change(threadId: string, change: { voice?: string; muted?: boolean }): Promise<{ thread_id: string; voice: string | null; muted: boolean }>;
}

/** After a failed assignment the thread speaks in the daemon's own voice this long before it is asked again. */
export const RETRY_MS = 5_000;

const NONE: ThreadVoice = { voice: null, muted: false };

export class ThreadVoices {
  private map = new Map<string, ThreadVoice>();
  private asking = new Map<string, Promise<string | null>>();
  private failedAt = new Map<string, number>();
  /** Mutes this window has set that the daemon has not answered yet: a reload of the map must not undo them. */
  private muting = new Map<string, symbol>();
  private mutingWant = new Map<string, boolean>();
  private listeners = new Set<() => void>();
  private _pool: readonly string[] = [];
  private _version = 0;
  /** V12 (L3): told once per voice when the daemon says a chosen voice could pass for the enrolled owner. */
  onWarning: ((message: string) => void) | null = null;
  private warned = new Set<string>();
  private _mutedKey = "";
  private _muted: ReadonlySet<string> = new Set();

  constructor(private readonly api: VoicesApi, private readonly now: () => number = () => Date.now()) {}

  /** Read the daemon's whole map (on start, and when the event stream reconnects). */
  async load(): Promise<void> {
    try {
      const got = await this.api.list();
      this._pool = got.pool;
      for (const [id, v] of Object.entries(got.threads)) {
        const mine = this.mutingWant.get(id);
        this.map.set(id, { voice: v.voice, muted: mine ?? v.muted });
      }
      this.changed();
    } catch { /* the chat is read in the daemon's own voice until it answers */ }
  }

  /** The voices the chip offers. */
  pool(): readonly string[] { return this._pool; }

  info(thread: string): ThreadVoice { return this.map.get(thread) ?? NONE; }

  /** Changes whenever anything shown changes: for `useSyncExternalStore`. */
  version(): number { return this._version; }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  }

  /** Threads the owner muted; the same object while it is unchanged. */
  muted(): ReadonlySet<string> {
    const ids = [...this.map].filter(([, v]) => v.muted).map(([id]) => id).sort();
    const key = ids.join(",");
    if (key !== this._mutedKey) { this._mutedKey = key; this._muted = new Set(ids); }
    return this._muted;
  }

  /** The thread's voice if known now, else a promise of it (null: the daemon's own). */
  voiceFor = (thread: string): string | null | Promise<string | null> => {
    const known = this.map.get(thread)?.voice;
    return known ? known : this.assign(thread);
  };

  /** Ask the daemon to give the thread a voice (idempotent there). */
  assign(thread: string): Promise<string | null> {
    const known = this.map.get(thread)?.voice;
    if (known) return Promise.resolve(known);
    const running = this.asking.get(thread);
    if (running) return running;
    const failed = this.failedAt.get(thread);
    if (failed !== undefined && this.now() - failed < RETRY_MS) return Promise.resolve(null);
    const p = this.api.change(thread, {}).then((r) => {
      this.apply(r);
      // The daemon answered with no voice (none installed): back off like a failure,
      // or every chunk would ask again.
      if (r.voice) this.failedAt.delete(thread);
      else this.failedAt.set(thread, this.now());
      return r.voice;
    }, () => {
      this.failedAt.set(thread, this.now());
      return null;
    }).finally(() => { this.asking.delete(thread); });
    this.asking.set(thread, p);
    return p;
  }

  /** The owner's choice ("" returns the chat to automatic). */
  async choose(thread: string, voice: string): Promise<void> {
    try { this.apply(await this.api.change(thread, { voice })); } catch { /* the chip keeps showing the voice in force */ }
  }

  async mute(thread: string, muted: boolean): Promise<void> {
    // The window acts at once; the daemon's answer (or another window's change) settles it.
    const before = this.info(thread);
    this.map.set(thread, { ...before, muted });
    // This call's marker: a second toggle in flight replaces it, and the first's end must not clear the second's.
    const mark = Symbol("mute");
    this.muting.set(thread, mark);
    this.mutingWant.set(thread, muted);
    this.changed();
    try {
      this.apply(await this.api.change(thread, { muted }));
    } catch {
      this.map.set(thread, before);
      this.changed();
    } finally {
      if (this.muting.get(thread) === mark) { this.muting.delete(thread); this.mutingWant.delete(thread); }
    }
  }

  /** A record from the daemon: our own call's answer, or another window's `thread_voice`. */
  apply(record: { thread_id?: unknown; voice?: unknown; muted?: unknown; speaker_warning?: unknown } | undefined | null): void {
    if (!record || typeof record.thread_id !== "string") return;
    if (typeof record.speaker_warning === "string" && record.speaker_warning && typeof record.voice === "string" && !this.warned.has(record.voice)) {
      this.warned.add(record.voice);
      try { this.onWarning?.(record.speaker_warning.slice(0, 400)); } catch { /* a notice never breaks speech */ }
    }
    const voice = typeof record.voice === "string" && record.voice ? record.voice : null;
    const next = { voice, muted: record.muted === true };
    const old = this.map.get(record.thread_id);
    if (old && old.voice === next.voice && old.muted === next.muted) return;
    this.map.set(record.thread_id, next);
    this.changed();
  }

  private changed(): void {
    this._version += 1;
    for (const cb of [...this.listeners]) {
      try { cb(); } catch { /* a subscriber never breaks speech */ }
    }
  }
}

/** A voice name as the chip shows it: "bm_george" is George, "pocket:alba" is Alba (pocket). */
export function voiceLabel(name: string | null): string {
  if (!name) return "auto";
  if (name.startsWith("pocket:")) return `${titled(name.slice(7))} · pocket`;
  const m = /^[a-z]{2}_(.+)$/.exec(name);
  return titled(m ? m[1] : name);
}

function titled(raw: string): string {
  return raw.replace(/_/g, " ").replace(/\b[a-z]/g, (c) => c.toUpperCase());
}
