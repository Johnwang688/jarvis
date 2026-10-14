// The speech controller (V6): events in, speech out.
//
// It listens to the HUD's one `/events` stream, turns the prose of the threads
// it is to read into chunks (`stream.ts`), queues them (`queue.ts`), has the
// daemon synthesize each (`/say`, local only) and plays them (`player.ts`). It
// holds the controls (pause, resume, skip, stop) and the rules that tie speech
// to the rest of the window:
//
//   - Only the window holding the speech lock builds a queue (`speaker.ts`).
//   - Only threads in `setSpeakers` are read (`policy.speakingThreads`): every
//     thread shown in any chat pane, hidden panes included, through the one
//     queue and its floor rules (`floor.ts`), each in its own voice (`voiceFor`).
//     A chat the owner muted (`setMuted`) is not read either.
//   - A thread leaving that set stops at once; so does an owner send to it
//     (a `user_message` event: the reply it was reading is stale) and an
//     interrupted turn.
//   - A tool call closes the message in progress (its partial sentence is
//     flushed); a reconnect discards partial text it can no longer finish.
//   - Nothing here can reach an approval: no import, no route, no event it
//     answers. Speech is output only (decision V-6).
//
// The controller has no timers of its own and touches no DOM: the clock,
// player, synthesizer and speaker gate are injected.

import { Floor } from "./floor";
import { SpeechQueue, type Clock, type Player, type QueueView, type SpeechItem } from "./queue";
import { ProseStream, type SpokenChunk } from "./stream";

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
  /**
   * The voice for a thread: a named voice, or null to leave the choice to the
   * daemon (the avatar's). Asked when a chunk is synthesized, not when it is
   * queued, so a voice chosen from the chip applies to what has not been made
   * yet, and a thread whose voice is still being assigned waits for it.
   */
  voiceFor?: (thread: string) => string | null | Promise<string | null>;
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
  /** Message counter for keys; chunk counter within the message. */
  n: number;
  seq: number;
}

export class SpeechController {
  private readonly queue: SpeechQueue;
  private open = new Map<string, Open>();
  /** The next message number of each thread: it outlives `open`, so a key is never reused. */
  private counters = new Map<string, number>();
  private speakers: ReadonlySet<string> = new Set();
  private muted: ReadonlySet<string> = new Set();
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
      floor: new Floor(deps.clock),
      synth: (item, signal) => {
        // The thread's voice is asked for now, so a voice chosen since the chunk
        // was queued applies, and one still being assigned is waited for. A
        // voice that is already known costs no extra turn of the event loop.
        const speak = (voice: string | null) => deps.say(item.text, { voice, signal }).then((bytes) => deps.player.decode(bytes));
        let asked: ReturnType<NonNullable<ControllerDeps["voiceFor"]>> | undefined;
        try { asked = deps.voiceFor?.(item.thread); } catch { asked = undefined; }
        if (asked && typeof asked === "object" && "then" in asked) {
          return asked.then((v) => speak(typeof v === "string" ? v : item.voice), () => speak(item.voice));
        }
        return speak(typeof asked === "string" ? asked : item.voice);
      },
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

  /** The threads to read now (every one shown in a chat pane); any thread that left has its speech dropped. */
  setSpeakers(threads: ReadonlySet<string>): void {
    this.speakers = threads;
    this.prune();
    this.notify();
  }

  /** The chats the owner muted: silent like a thread in no pane, and its speech goes at once. */
  setMuted(threads: ReadonlySet<string>): void {
    this.muted = threads;
    this.prune();
    this.notify();
  }

  private prune(): void {
    const known = new Set<string>([...this.open.keys(), ...this.queue.items().map((i) => i.thread)]);
    for (const thread of known) if (!this.speaks(thread)) this.leave(thread);
  }

  private leave(thread: string): void {
    this.endOpen(thread);
    this.open.delete(thread);
    this.queue.dropThread(thread, "thread_left");
  }

  /** The message in progress for `thread` ends here (it will get no more chunks). */
  private endOpen(thread: string): void {
    const s = this.open.get(thread);
    if (s) this.queue.endMessage(`${thread}:${s.n}`);
  }

  private speaks(thread: string): boolean {
    return this.on && this.deps.gate.isSpeaker() && this.speakers.has(thread) && !this.muted.has(thread);
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
        this.emit(thread, this.slot(thread).stream.feed(text));
        break;
      case "text":
        this.emit(thread, this.slot(thread).stream.settle(text));
        this.advance(thread);
        break;
      case "tool_started":
      case "error":
        this.finish(thread);
        break;
      case "turn_finished":
        this.finish(thread);
        // Stop pressed: this turn's speech goes too. Otherwise the last message
        // is the reply, which a backlog never collapses.
        if (data.stop === "interrupted") this.queue.dropThread(thread, "stale");
        else this.queue.turnEnded(thread);
        break;
      default:
        break;
    }
  }

  /** A message went to the thread, by any means: the reply being read is stale. */
  messageSent(thread: string): void {
    const mid = this.open.get(thread)?.stream;
    if (mid?.open) mid.silence();   // a message in progress; a finished one has nothing left to silence
    this.endOpen(thread);
    this.queue.dropThread(thread, "stale");
  }

  private slot(thread: string): Open {
    let s = this.open.get(thread);
    if (!s) this.open.set(thread, (s = { stream: new ProseStream(), n: this.counters.get(thread) ?? 0, seq: 0 }));
    return s;
  }

  private finish(thread: string): void {
    const s = this.open.get(thread);
    if (!s || !s.stream.open) return;
    this.emit(thread, s.stream.close());
    this.advance(thread);
  }

  /** The message is over: the next one is a new key. */
  private advance(thread: string): void {
    const s = this.open.get(thread);
    if (!s) return;
    this.queue.endMessage(`${thread}:${s.n}`);
    s.n += 1;
    s.seq = 0;
    this.counters.set(thread, s.n);
  }

  private emit(thread: string, chunks: SpokenChunk[]): void {
    const s = this.open.get(thread);
    if (!s) return;
    const known = this.deps.voiceFor?.(thread);
    const voice = typeof known === "string" ? known : null;
    for (const chunk of chunks) {
      this.queue.enqueue({
        thread, messageKey: `${thread}:${s.n}`, seq: s.seq++, text: chunk.text, kind: chunk.kind,
        voice: voice ?? null,
      });
    }
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
    this.silenceIfOpen(this.queue.skip());
  }

  /** One chat's current message (else its next one) is not spoken: the voice chip's "skip this chat". */
  skipThread(thread: string): void {
    this.silenceIfOpen(this.queue.skipThread(thread));
  }

  private silenceIfOpen(skipped: { thread: string; messageKey: string } | null): void {
    if (!skipped) return;
    const s = this.open.get(skipped.thread);
    if (s?.stream.open && `${skipped.thread}:${s.n}` === skipped.messageKey) s.stream.silence();
  }

  /** Stop speaking and clear the queue; messages still arriving stay quiet until they end. */
  stop(): void {
    this.bargePaused = false;
    for (const s of this.open.values()) if (s.stream.open) s.stream.silence();
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
