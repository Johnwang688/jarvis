// The speech controller (V6): events in, speech out.
//
// It listens to the HUD's one `/events` stream, turns the prose of the threads
// it is to read into chunks (`stream.ts`), queues them (`queue.ts`), has the
// daemon synthesize each (`/say`, local only) and plays them (`player.ts`). It
// holds the controls (pause, resume, skip, stop) and the rules that tie speech
// to the rest of the window:
//
//   - Only the window holding the speech lock builds a queue (`speaker.ts`).
//   - Only threads in `setSpeakers` are read (`policy.speakingThreads`).
//   - A thread leaving that set stops at once; so does an owner send to it
//     (a `user_message` event: the reply it was reading is stale) and an
//     interrupted turn.
//   - A tool call closes the message in progress (its partial sentence is
//     flushed); a reconnect discards partial text it can no longer finish.
//   - A message is held until it closes, because its length decides how it is
//     said (V8, `summary.ts`): up to 600 spoken characters it is read as
//     written; longer, it is replaced by the daemon's summary (no verbatim
//     lead-in), or, if that fails, its opening and "...and more on screen".
//     The summary is asked for the moment the message closes, and a later
//     message of the thread waits behind it (the thread's lane), so order
//     holds. Anything that silences the thread cancels the request.
//   - Nothing here can reach an approval: no import, no route, no event it
//     answers. Speech is output only (decision V-6).
//
// The controller has no timers of its own and touches no DOM: the clock,
// player, synthesizer and speaker gate are injected.

import { SpeechQueue, type Clock, type Player, type QueueView, type SpeechItem } from "./queue";
import { ProseStream, type SpokenChunk } from "./stream";
import { SUMMARY_DEADLINE_MS, needsSummary, spokenText, summaryChunk, truncatedProse, type Summarize } from "./summary";

export interface SpeechEventLike {
  kind?: string;
  thread_id?: string;
  data?: { text?: string; stop?: string; [key: string]: unknown } | undefined;
  text?: string;
  stop?: string;
}

export interface SpeakerGateLike {
  isSpeaker(): boolean;
  subscribe(cb: () => void): () => void;
}

export interface ControllerDeps {
  /** Audio bytes for `text`, or a rejection (the chunk is then skipped). */
  say(text: string, opts: { voice: string | null; signal: AbortSignal }): Promise<ArrayBuffer>;
  player: Player;
  clock: Clock;
  gate: SpeakerGateLike;
  /** A named voice for a thread; null leaves the choice to the daemon (the avatar's). V7 supplies per-thread voices. */
  voiceFor?: (thread: string) => string | null;
  /** A spoken summary of a long message (`POST /speech/summary`). Absent: a long message is read truncated. */
  summarize?: Summarize;
}

/** After the last chunk ends, speech still counts as active this long: the owner's own echo and the next chunk's lead-in. */
export const HANGOVER_MS = 300;

export interface SpeechView extends QueueView {
  /**
   * Speech is in progress for suppression: playing, or buffering between
   * chunks, or within `HANGOVER_MS` of the last one ending. Not while paused.
   */
  active: boolean;
  /** Read-aloud is on (its own switch, not the mic's). */
  enabled: boolean;
  /** This window holds the speech lock. */
  speaker: boolean;
}

interface Open {
  stream: ProseStream;
  /** Message counter for keys. */
  n: number;
  /** The open message's chunks, held until it closes and its length is known. */
  held: SpokenChunk[];
}

/** A summary requested and not yet answered. */
interface Pending {
  thread: string;
  ctl: AbortController;
  /** The client deadline passed: an abort that is not a cancellation. */
  expired: boolean;
}

export class SpeechController {
  private readonly queue: SpeechQueue;
  private open = new Map<string, Open>();
  /** Per thread, the tail of what must come out in order while a summary is awaited. */
  private lanes = new Map<string, Promise<void>>();
  private pending = new Set<Pending>();
  private speakers: ReadonlySet<string> = new Set();
  private on = true;
  private listeners = new Set<() => void>();
  private cached: SpeechView | null = null;
  private unsubs: (() => void)[] = [];
  private wasSpeaker: boolean;
  private wasLive = false;
  private hang: unknown = null;
  private idleListeners = new Set<() => void>();
  private bargePaused = false;

  constructor(private readonly deps: ControllerDeps) {
    this.queue = new SpeechQueue({
      clock: deps.clock,
      player: deps.player,
      synth: (item, signal) =>
        deps.say(item.text, { voice: item.voice, signal }).then((bytes) => deps.player.decode(bytes)),
      onChange: () => { this.settle(); this.notify(); },
    });
    this.wasSpeaker = deps.gate.isSpeaker();
    this.unsubs.push(deps.gate.subscribe(() => this.gateChanged()));
    const blocked = deps.player.subscribeBlocked?.(() => this.notify());
    if (blocked) this.unsubs.push(blocked);
  }

  // ---- reading ------------------------------------------------------------

  view(): SpeechView {
    const q = this.queue.view();
    const speaker = this.deps.gate.isSpeaker();
    const active = q.state === "playing" || q.state === "buffering" || this.hang !== null;
    const old = this.cached;
    if (old && old.active === active && old.state === q.state && old.thread === q.thread && old.queued === q.queued && old.failed === q.failed
      && old.blocked === q.blocked && old.enabled === this.on && old.speaker === speaker) return old;
    return (this.cached = { ...q, active, enabled: this.on, speaker });
  }

  /** Called when speech goes idle (after the hangover): the follow-up window opens here. */
  onIdle(cb: () => void): () => void {
    this.idleListeners.add(cb);
    return () => { this.idleListeners.delete(cb); };
  }

  /** The queue changed: keep the hangover that holds `active` just after the last chunk. */
  private settle(): void {
    const state = this.queue.view().state;
    const live = state === "playing" || state === "buffering";
    if (live) {
      this.wasLive = true;
      if (this.hang !== null) { this.deps.clock.clearTimeout(this.hang); this.hang = null; }
    } else if (state === "idle" && this.wasLive && this.hang === null) {
      this.wasLive = false;
      this.hang = this.deps.clock.setTimeout(() => {
        this.hang = null;
        this.notify();
        for (const cb of [...this.idleListeners]) {
          try { cb(); } catch { /* a listener never breaks speech */ }
        }
      }, HANGOVER_MS);
    } else if (state === "paused") {
      this.wasLive = false;
      if (this.hang !== null) { this.deps.clock.clearTimeout(this.hang); this.hang = null; }
    }
  }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  }

  /** The output level, 0..1: read per frame by the orb. */
  level(): number {
    return this.queue.level();
  }

  /** Live and recent items, for the test hook. */
  items(): { live: readonly SpeechItem[]; past: readonly SpeechItem[] } {
    return { live: this.queue.items(), past: this.queue.past() };
  }

  private notify(): void {
    for (const cb of [...this.listeners]) {
      try { cb(); } catch { /* a subscriber never breaks speech */ }
    }
  }

  // ---- who is read --------------------------------------------------------

  /** The read-aloud switch. Off, everything stops and nothing more is queued. */
  setEnabled(on: boolean): void {
    if (this.on === on) return;
    this.on = on;
    if (!on) this.clearAll();
    this.notify();
  }

  /** The threads to read now; any thread that left has its speech dropped. */
  setSpeakers(threads: ReadonlySet<string>): void {
    this.speakers = threads;
    const known = new Set<string>([...this.open.keys(), ...this.queue.items().map((i) => i.thread)]);
    for (const thread of known) if (!threads.has(thread)) this.leave(thread);
    this.notify();
  }

  private leave(thread: string): void {
    this.open.delete(thread);
    this.cancel(thread);
    this.queue.dropThread(thread, "thread_left");
  }

  private speaks(thread: string): boolean {
    return this.on && this.deps.gate.isSpeaker() && this.speakers.has(thread);
  }

  private gateChanged(): void {
    const now = this.deps.gate.isSpeaker();
    // Another window took the floor: this one builds no queue and falls silent.
    if (this.wasSpeaker && !now) this.clearAll();
    this.wasSpeaker = now;
    this.notify();
  }

  private clearAll(): void {
    this.open.clear();
    this.cancel();
    this.queue.stop();
  }

  // ---- events -------------------------------------------------------------

  event(e: SpeechEventLike): void {
    const kind = e.kind;
    if (kind === "_connected" || kind === "_disconnected") {
      // The sentence in flight cannot be finished after a gap (`ProseStream.gap`);
      // what is queued still plays.
      for (const [thread, s] of this.open) {
        s.stream.gap();
        if (!s.stream.open) this.open.delete(thread);
      }
      return;
    }
    const thread = e.thread_id;
    if (!thread || !kind) return;
    // A message to the thread, by any means: the reply being read is stale.
    if (kind === "user_message") {
      this.messageSent(thread);
      return;
    }
    if (!this.speaks(thread)) {
      if (this.open.has(thread)) this.open.delete(thread);
      return;
    }
    const data = e.data ?? e;
    const text = typeof data.text === "string" ? data.text : "";
    switch (kind) {
      case "text_delta":
        this.hold(thread, this.slot(thread).stream.feed(text));
        break;
      case "text":
        this.hold(thread, this.slot(thread).stream.settle(text));
        this.conclude(thread);
        break;
      case "tool_started":
      case "error":
        this.finish(thread);
        break;
      case "turn_finished":
        this.finish(thread);
        // Stop pressed: this turn's speech goes too.
        if (data.stop === "interrupted") { this.cancel(thread); this.queue.dropThread(thread, "stale"); }
        break;
      default:
        break;
    }
  }

  /** A message went to the thread, by any means: the reply being read is stale. */
  messageSent(thread: string): void {
    const mid = this.open.get(thread);
    if (mid?.stream.open) this.hush(mid);   // a message in progress; a finished one has nothing left to silence
    this.cancel(thread);
    this.queue.dropThread(thread, "stale");
  }

  private slot(thread: string): Open {
    let s = this.open.get(thread);
    if (!s) this.open.set(thread, (s = { stream: new ProseStream(), n: 0, held: [] }));
    return s;
  }

  private finish(thread: string): void {
    const s = this.open.get(thread);
    if (!s || !s.stream.open) return;
    this.hold(thread, s.stream.close());
    this.conclude(thread);
  }

  /** The message in progress will not be said: what it held goes, and what it still brings is ignored. */
  private hush(s: Open): void {
    s.stream.silence();
    s.held = [];
  }

  private hold(thread: string, chunks: SpokenChunk[]): void {
    const s = this.open.get(thread);
    if (s) s.held.push(...chunks);
  }

  /**
   * The message is over (V8). Up to 600 spoken characters it is said as
   * written; longer, a summary is asked for at once and said in its place.
   * The next message is a new key.
   */
  private conclude(thread: string): void {
    const s = this.open.get(thread);
    if (!s) return;
    const held = s.held;
    const key = `${thread}:${s.n}`;
    s.held = [];
    s.n += 1;
    if (!held.length) return;
    if (!needsSummary(held)) {
      this.sequence(thread, () => this.deliver(thread, key, held));
      return;
    }
    const wait = this.summary(thread, spokenText(held));
    this.sequence(thread, async () => {
      const got = await wait;
      if (got === "cancelled") return;
      this.deliver(thread, key, got ?? truncatedProse(held));
    });
  }

  /** Queue a message's chunks, if the thread is still to be read. */
  private deliver(thread: string, key: string, chunks: readonly SpokenChunk[]): void {
    if (!this.speaks(thread)) return;
    const voice = this.deps.voiceFor?.(thread) ?? null;
    chunks.forEach((chunk, seq) => {
      this.queue.enqueue({ thread, messageKey: key, seq, text: chunk.text, kind: chunk.kind, voice });
    });
  }

  /**
   * The daemon's summary as one chunk to say; null when it failed, did not
   * answer in time or came back unusable (the caller reads truncated prose);
   * "cancelled" when the thread was silenced meanwhile (nothing is said).
   */
  private async summary(thread: string, text: string): Promise<SpokenChunk[] | null | "cancelled"> {
    const summarize = this.deps.summarize;
    if (!summarize) return null;
    const entry: Pending = { thread, ctl: new AbortController(), expired: false };
    this.pending.add(entry);
    let timer: unknown = null;
    const late = new Promise<null>((resolve) => {
      timer = this.deps.clock.setTimeout(() => { entry.expired = true; entry.ctl.abort(); resolve(null); }, SUMMARY_DEADLINE_MS);
    });
    try {
      const raw = await Promise.race([summarize(text, { thread, signal: entry.ctl.signal }), late]);
      if (entry.ctl.signal.aborted && !entry.expired) return "cancelled";
      const chunk = summaryChunk(raw);
      return chunk ? [chunk] : null;
    } catch {
      return entry.ctl.signal.aborted && !entry.expired ? "cancelled" : null;
    } finally {
      this.deps.clock.clearTimeout(timer);
      this.pending.delete(entry);
    }
  }

  /** Cancel the summaries asked for `thread` (all of them when none is named). */
  private cancel(thread?: string): void {
    for (const p of this.pending) if (thread === undefined || p.thread === thread) p.ctl.abort();
  }

  /**
   * Run `job` in the thread's order: at once when no summary is being awaited,
   * else after everything before it. A later short message must not be said
   * ahead of the long one that closed first.
   */
  private sequence(thread: string, job: () => void | Promise<void>): void {
    const tail = this.lanes.get(thread);
    let run: Promise<void>;
    if (!tail) {
      const r = job();
      if (!(r instanceof Promise)) return;
      run = r;
    } else {
      run = tail.then(job);
    }
    const lane: Promise<void> = run.catch(() => undefined).then(() => {
      if (this.lanes.get(thread) === lane) this.lanes.delete(thread);
    });
    this.lanes.set(thread, lane);
  }

  // ---- controls -----------------------------------------------------------

  // The owner's own controls. Any of them takes over from a barge-in's pause.
  pause(): void { this.bargePaused = false; this.queue.pause(); }
  resume(): void { this.bargePaused = false; this.queue.resume(); }

  toggle(): void {
    this.bargePaused = false;
    if (this.queue.view().state === "paused") this.queue.resume();
    else this.queue.pause();
  }

  /**
   * The owner starts talking over him (decision V-7: barge-in pauses, then
   * resumes). Pauses speech that is in progress and remembers that *it* did; a
   * pause the owner made from the strip is theirs, and is left alone.
   */
  bargeIn(): void {
    const state = this.queue.view().state;
    if (state !== "playing" && state !== "buffering") return;
    this.queue.pause();
    this.bargePaused = true;
  }

  /**
   * The utterance that paused him is over, whatever came of it (nothing sent,
   * dropped, failed, placed for review, or sent). Resumes what this barge-in
   * paused, restarting the sentence (Q13); a thread a message went to has
   * already lost its items (`messageSent`), so what resumes is what remains.
   */
  endBargeIn(): void {
    if (!this.bargePaused) return;
    this.bargePaused = false;
    this.queue.resume();
  }

  /** The rest of the current message is not spoken. */
  skip(): void {
    const skipped = this.queue.skip();
    if (!skipped) {
      // Nothing sounded: the message being waited for -- held, or out for a summary -- is the current one.
      for (const s of this.open.values()) if (s.stream.open) this.hush(s);
      this.cancel();
      return;
    }
    const s = this.open.get(skipped.thread);
    if (s?.stream.open && `${skipped.thread}:${s.n}` === skipped.messageKey) this.hush(s);
  }

  /** Stop speaking and clear the queue; messages still arriving stay quiet until they end. */
  stop(): void {
    this.bargePaused = false;
    for (const s of this.open.values()) if (s.stream.open) this.hush(s);
    this.cancel();
    this.queue.stop();
  }

  clearMarks(): void { this.queue.clearMarks(); }

  dispose(): void {
    for (const off of this.unsubs) off();
    this.unsubs = [];
    if (this.hang !== null) this.deps.clock.clearTimeout(this.hang);
    this.hang = null;
    this.idleListeners.clear();
    this.clearAll();
    this.listeners.clear();
  }
}
