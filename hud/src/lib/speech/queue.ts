// The speech queue (V6): chunks of agent prose, one played at a time.
//
// One queue for every thread being read (V7). An item names its `thread`,
// `messageKey` and `seq`, and *which live item plays next* is one replaceable
// function (`PlayOrder`): FIFO by default, and with a `Floor` (floor.ts) the
// shared-queue rules -- a message holds the floor, a slow holder yields after
// 4 s, a backlog collapses -- without touching synthesis, playback or the
// controls below.
//
// Rules this file keeps (plan §3.9):
//   1. One item plays at a time; two never overlap.
//   2. Items play in order. A later one that is ready waits for an earlier one
//      still being synthesized, and a failed synthesis is skipped and marked.
//   3. At most `prefetch` items are synthesized ahead of the one playing
//      (Kokoro serializes on one instance; more would only queue).
//   4. A thread leaving, or an owner send to it, drops its unplayed items and
//      stops its playing one (`dropThread`).
//   5. Pause is the owner's: the sentence in hand restarts on resume (Q13).
//   6. With a `Floor`: a message in progress keeps going before other threads'
//      messages, a stalled one yields, and a long backlog of interim updates
//      collapses into "Skipping N updates" (the `Floor` holds the rules).
//
// Time and audio are injected (`Clock`, `Player`), so every rule is tested with
// a fake clock and a fake player, never a real speaker.

import type { Floor } from "./floor";

/** `note`: the queue's own "Skipping N updates" marker. `notice`: Jarvis's own one-line notice of a sub-agent's end (SV-5). */
export type ItemKind = "verbatim" | "summary" | "fallback" | "note" | "notice";
export type ItemState = "pending" | "synthesizing" | "ready" | "playing" | "done" | "skipped" | "dropped";
/** Why an item was skipped or dropped, for diagnostics and the strip's mark. */
export type ItemMark = "tts_failed" | "tts_timeout" | "play_failed" | "skipped" | "stopped" | "thread_left" | "stale" | "collapsed";

export interface SpeechItem {
  id: number;
  thread: string;
  /** One message of one thread: skip acts on all of its chunks. */
  messageKey: string;
  seq: number;
  /** A named voice, or null for the daemon's own choice (the avatar's). */
  voice: string | null;
  kind: ItemKind;
  text: string;
  state: ItemState;
  mark?: ItemMark;
}

export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface PlayHandle {
  /** Settles once: "ended" at the natural end, "stopped" after `stop()`. */
  done: Promise<"ended" | "stopped">;
  stop(): void;
}

export interface Player {
  /** Bytes from `/say` to something `play` takes. */
  decode(bytes: ArrayBuffer): Promise<unknown>;
  play(audio: unknown): PlayHandle;
  /** The output level, 0..1, for the orb. */
  level(): number;
  /** The platform is holding audio back (an autoplay policy); the owner must interact. */
  blocked(): boolean;
  /** How long `audio` lasts in ms, for the watchdog; unknown skips it. */
  duration?(audio: unknown): number;
  /** Told when `blocked()` may have changed. */
  subscribeBlocked?(cb: () => void): () => void;
}

export type Synth = (item: SpeechItem, signal: AbortSignal) => Promise<unknown>;
/** The live items in the order they are to be played: the head plays next. */
export type PlayOrder = (live: readonly SpeechItem[]) => readonly SpeechItem[];

export const fifo: PlayOrder = (live) => live;

/** `buffering`: items are live and the next is being made: speech has not ended, so nothing may flicker or listen. */
export type QueueState = "idle" | "buffering" | "playing" | "paused";

export interface QueueView {
  state: QueueState;
  /** The thread being spoken: the playing item's, else the head's. */
  thread: string | null;
  /** Live items not yet played, the playing one excluded. */
  queued: number;
  /** Items skipped because their synthesis failed, since the last `stop`/`clearMarks`. */
  failed: number;
  /** Autoplay is blocked until the owner interacts with the page. */
  blocked: boolean;
}

export interface QueueDeps {
  synth: Synth;
  player: Player;
  clock: Clock;
  onChange?: () => void;
  order?: PlayOrder;
  /** The shared-queue rules; its order replaces `order`. */
  floor?: Floor;
  prefetch?: number;
  synthTimeoutMs?: number;
}

export const PREFETCH = 2;
export const SYNTH_TIMEOUT_MS = 20_000;
/** A sound that has not ended this long after its length is stopped and counted as failed. */
export const WATCHDOG_MS = 2_000;
const HISTORY = 40;

type Slot = { item: SpeechItem; audio?: unknown; abort?: AbortController; timer?: unknown; watch?: unknown };

export class SpeechQueue {
  private slots: Slot[] = [];
  private history: SpeechItem[] = [];
  private current: { slot: Slot; handle: PlayHandle } | null = null;
  private paused = false;
  private failed = 0;
  /** The thread of the latest chunk that could not be made or played: the strip names it while idle. */
  private lastFailed: string | null = null;
  private ids = 0;
  private cached: QueueView | null = null;
  private pumping = false;
  private again = false;
  private readonly order: PlayOrder;
  private readonly prefetch: number;
  private readonly timeout: number;
  /** The unplayed "Skipping N updates" marker of each thread, so a second collapse adds to it. */
  private markers = new Map<string, { slot: Slot; count: number }>();

  constructor(private readonly deps: QueueDeps) {
    this.order = deps.floor ? deps.floor.order : deps.order ?? fifo;
    deps.floor?.bind(() => this.pump());
    this.prefetch = deps.prefetch ?? PREFETCH;
    this.timeout = deps.synthTimeoutMs ?? SYNTH_TIMEOUT_MS;
  }

  // ---- reading ------------------------------------------------------------

  view(): QueueView {
    const live = this.live();
    const state: QueueState = this.current ? "playing" : !live.length ? "idle" : this.paused ? "paused" : "buffering";
    const head = this.current?.slot.item ?? this.order(live)[0];
    const view: QueueView = {
      state,
      thread: head ? head.thread : this.failed > 0 ? this.lastFailed : null,
      queued: live.length - (this.current ? 1 : 0),
      failed: this.failed,
      blocked: this.deps.player.blocked(),
    };
    const old = this.cached;
    if (old && old.state === view.state && old.thread === view.thread && old.queued === view.queued
      && old.failed === view.failed && old.blocked === view.blocked) return old;
    return (this.cached = view);
  }

  /** Items in play order (live), for the test hook and diagnostics. */
  items(): readonly SpeechItem[] {
    return this.order(this.live()).map((i) => ({ ...i }));
  }

  /** The most recent finished or dropped items, newest last. */
  past(): readonly SpeechItem[] {
    return this.history.map((i) => ({ ...i }));
  }

  level(): number {
    return this.current ? this.deps.player.level() : 0;
  }

  // ---- writing ------------------------------------------------------------

  enqueue(entry: Pick<SpeechItem, "thread" | "messageKey" | "seq" | "text"> & Partial<Pick<SpeechItem, "voice" | "kind">>): SpeechItem {
    const item: SpeechItem = {
      id: ++this.ids, voice: null, kind: "verbatim", ...entry, state: "pending",
    };
    this.slots.push({ item });
    this.deps.floor?.added(item);
    this.pump();
    return { ...item };
  }

  /** No more chunks are coming for this message: the floor it held is free once its last chunk is spoken. */
  endMessage(messageKey: string): void {
    this.deps.floor?.end(messageKey);
    this.pump();
  }

  /** The turn finished: the thread's newest message is its reply, which a backlog never collapses. */
  turnEnded(thread: string): void {
    this.deps.floor?.turnEnded(thread);
  }

  pause(): void {
    if (this.paused) return;
    this.paused = true;
    const cur = this.current;
    if (cur) {
      // The sentence in hand restarts on resume (Q13): back to `ready`, audio kept.
      this.current = null;
      cur.slot.item.state = "ready";
      this.unwatch(cur.slot);
      cur.handle.stop();
    }
    this.pump();
  }

  resume(): void {
    if (!this.paused) return;
    this.paused = false;
    this.pump();
  }

  /** Skip the rest of the current message. Returns what was skipped, or null when nothing is live. */
  skip(): { thread: string; messageKey: string } | null {
    const head = this.current?.slot.item ?? this.order(this.live())[0];
    if (!head) return null;
    const { thread, messageKey } = head;
    this.finishWhere((i) => i.thread === thread && i.messageKey === messageKey, "skipped", "skipped");
    this.pump();
    return { thread, messageKey };
  }

  /** The message being said, or else the one next in line: what a barge-in interrupts. */
  headMessage(): { thread: string; messageKey: string } | null {
    const head = this.current?.slot.item ?? this.order(this.live())[0];
    return head ? { thread: head.thread, messageKey: head.messageKey } : null;
  }

  /** Skip one named message, if it is still live (a barge-in's interrupted message after a send). */
  skipMessage(thread: string, messageKey: string): { thread: string; messageKey: string } | null {
    if (!this.slots.some((s) => s.item.thread === thread && s.item.messageKey === messageKey)) return null;
    this.finishWhere((i) => i.thread === thread && i.messageKey === messageKey, "skipped", "skipped");
    this.pump();
    return { thread, messageKey };
  }

  /** Skip one thread's current message (else its first queued one): the chip's "skip this chat". */
  skipThread(thread: string): { thread: string; messageKey: string } | null {
    const playing = this.current?.slot.item;
    const target = playing && playing.thread === thread ? playing : this.order(this.live()).find((i) => i.thread === thread);
    if (!target) return null;
    const { messageKey } = target;
    this.finishWhere((i) => i.thread === thread && i.messageKey === messageKey, "skipped", "skipped");
    this.pump();
    return { thread, messageKey };
  }

  /** Stop and clear everything. */
  stop(): void {
    this.finishWhere(() => true, "dropped", "stopped");
    this.deps.floor?.reset();
    this.markers.clear();
    this.paused = false;
    this.failed = 0;
    this.lastFailed = null;
    this.pump();
  }

  /** A thread's unplayed items go, and its playing one stops (pane closed; the owner sent). */
  dropThread(thread: string, mark: ItemMark = "thread_left"): void {
    this.finishWhere((i) => i.thread === thread, "dropped", mark);
    this.pump();
  }

  clearMarks(): void {
    this.failed = 0;
    this.lastFailed = null;
    this.notify();
  }

  // ---- the machine --------------------------------------------------------

  private live(): SpeechItem[] {
    return this.slots.map((s) => s.item);
  }

  private notify(): void {
    this.deps.onChange?.();
  }

  private retire(slot: Slot, state: "done" | "skipped" | "dropped", mark?: ItemMark): void {
    slot.item.state = state;
    if (mark) slot.item.mark = mark;
    if (mark === "tts_failed" || mark === "tts_timeout" || mark === "play_failed") this.lastFailed = slot.item.thread;
    slot.abort?.abort();
    if (slot.timer !== undefined) this.deps.clock.clearTimeout(slot.timer);
    this.unwatch(slot);
    slot.abort = undefined;
    slot.timer = undefined;
    slot.audio = undefined;
    this.slots = this.slots.filter((s) => s !== slot);
    this.history.push(slot.item);
    if (this.history.length > HISTORY) this.history.shift();
  }

  private unwatch(slot: Slot): void {
    if (slot.watch !== undefined) this.deps.clock.clearTimeout(slot.watch);
    slot.watch = undefined;
  }

  /** A sound that never ended: stop it and count it, so one stuck source cannot hold the queue (and the mic) forever. */
  private watchdog(slot: Slot, ms: number): void {
    slot.watch = undefined;
    if (this.current?.slot !== slot) return;
    // Waiting for the owner's first gesture is not a stuck source.
    if (this.deps.player.blocked()) { this.arm(slot, ms); return; }
    const { handle } = this.current;
    this.current = null;
    this.failed += 1;
    this.retire(slot, "skipped", "play_failed");
    handle.stop();
    this.pump();
  }

  private arm(slot: Slot, ms: number): void {
    slot.watch = this.deps.clock.setTimeout(() => this.watchdog(slot, ms), ms);
  }

  private finishWhere(match: (i: SpeechItem) => boolean, state: "skipped" | "dropped", mark: ItemMark): void {
    for (const slot of [...this.slots]) {
      if (!match(slot.item)) continue;
      // Whatever goes, the message it belonged to is over: nothing more is coming for it.
      this.deps.floor?.end(slot.item.messageKey);
      if (this.current?.slot === slot) {
        const { handle } = this.current;
        this.current = null;
        this.retire(slot, state, mark);
        handle.stop();
      } else {
        this.retire(slot, state, mark);
      }
    }
  }

  private pump(): void {
    // Re-entrant calls (a synchronous synth, a settled promise) fold into one more pass.
    if (this.pumping) { this.again = true; return; }
    this.pumping = true;
    try {
      do {
        this.again = false;
        this.step();
      } while (this.again);
    } finally {
      this.pumping = false;
    }
    this.notify();
  }

  /** Interim updates beyond a minute of backlog become one spoken "Skipping N updates" (floor.ts rule 6). */
  private collapseBacklog(): void {
    const floor = this.deps.floor;
    if (!floor) return;
    const keys = floor.collapse(this.live());
    if (!keys.length) return;
    const made = new Map<string, { n: number; rank: number }>();
    for (const key of keys) {
      const thread = this.slots.find((s) => s.item.messageKey === key)?.item.thread;
      if (thread === undefined) continue;
      const rank = floor.rankOf(key);
      this.finishWhere((i) => i.messageKey === key, "dropped", "collapsed");
      const at = made.get(thread);
      made.set(thread, { n: (at?.n ?? 0) + 1, rank: Math.min(at?.rank ?? Infinity, rank) });
    }
    for (const [thread, { n, rank }] of made) {
      const words = (count: number) => `Skipping ${count} update${count === 1 ? "" : "s"}.`;
      const old = this.markers.get(thread);
      if (old && this.slots.includes(old.slot) && old.slot.item.state === "pending") {
        old.count += n;
        old.slot.item.text = words(old.count);
        continue;
      }
      const item: SpeechItem = {
        id: ++this.ids, thread, messageKey: `${thread}:skipped:${this.ids}`, seq: 0, voice: null, kind: "note",
        text: words(n), state: "pending",
      };
      const slot: Slot = { item };
      this.slots.push(slot);
      floor.added(item);
      floor.end(item.messageKey);
      floor.rankAs(item.messageKey, rank);
      this.markers.set(thread, { slot, count: n });
    }
  }

  private step(): void {
    this.deps.floor?.settle(this.live());
    this.collapseBacklog();
    const ordered = this.order(this.live());
    if (!ordered.length) this.paused = false;
    const slotOf = (item: SpeechItem) => this.slots.find((s) => s.item === item)!;

    // Synthesis: up to `prefetch` ahead of the one playing, in play order.
    let ahead = ordered.filter((i) => i.state === "synthesizing" || i.state === "ready").length;
    for (const item of ordered) {
      if (ahead >= this.prefetch) break;
      if (item.state !== "pending") continue;
      ahead += 1;
      this.synthesize(slotOf(item));
    }

    // The synthesis deadline runs for the oldest request only: the daemon makes
    // one at a time, so a younger one waits its turn and is not timed while it does.
    const oldest = this.order(this.live()).find((i) => i.state === "synthesizing");
    if (oldest) {
      const slot = slotOf(oldest);
      if (slot.timer === undefined) {
        slot.timer = this.deps.clock.setTimeout(() => { slot.timer = undefined; this.synthFailed(slot, "tts_timeout"); }, this.timeout);
      }
    }

    // Playback: the head, once it is ready, and only when nothing plays.
    if (this.current || this.paused) return;
    const head = this.order(this.live())[0];
    if (!head || head.state !== "ready") return;
    // Another message holds the floor and has not yielded: this one waits (floor.ts).
    if (this.deps.floor?.holds(head)) return;
    const slot = slotOf(head);
    let handle: PlayHandle;
    try {
      handle = this.deps.player.play(slot.audio);
    } catch {
      this.failed += 1;
      this.retire(slot, "skipped", "play_failed");
      this.again = true;
      return;
    }
    head.state = "playing";
    this.deps.floor?.started(head);
    this.current = { slot, handle };
    const length = this.deps.player.duration?.(slot.audio);
    if (typeof length === "number" && Number.isFinite(length)) this.arm(slot, length + WATCHDOG_MS);
    handle.done.then((how) => {
      if (this.current?.slot !== slot || how !== "ended") return;
      this.current = null;
      this.retire(slot, "done");
      this.pump();
    }, () => {
      if (this.current?.slot !== slot) return;
      this.current = null;
      this.failed += 1;
      this.retire(slot, "skipped", "play_failed");
      this.pump();
    });
  }

  private synthFailed(slot: Slot, mark: ItemMark): void {
    if (!this.slots.includes(slot) || slot.item.state !== "synthesizing") return;
    this.failed += 1;
    this.retire(slot, "skipped", mark);
    this.pump();
  }

  private synthesize(slot: Slot): void {
    const { item } = slot;
    item.state = "synthesizing";
    const abort = new AbortController();
    slot.abort = abort;
    const fail = (mark: ItemMark) => this.synthFailed(slot, mark);
    let started: Promise<unknown>;
    try {
      started = Promise.resolve(this.deps.synth({ ...item }, abort.signal));
    } catch (e) {
      started = Promise.reject(e);
    }
    started.then((audio) => {
      if (!this.slots.includes(slot) || item.state !== "synthesizing") return;
      if (slot.timer !== undefined) this.deps.clock.clearTimeout(slot.timer);
      slot.timer = undefined;
      slot.audio = audio;
      item.state = "ready";
      this.pump();
    }, () => fail("tts_failed"));
  }
}
