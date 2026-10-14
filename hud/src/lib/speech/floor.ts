// Who speaks next in the shared queue (V7, plan §3.9 rules 2-4 and 6).
//
// Every open chat pane is read through one queue. Left to arrival order, two
// chats writing at once would interleave sentence by sentence. This is the
// `PlayOrder` the V6 queue was built for, plus the little state it needs:
//
//   2. A message holds the floor: once one of its chunks starts, its later
//      chunks go before any other thread's, so messages never interleave.
//   3. Between messages, the one whose first stable chunk came first goes first.
//   4. Floor timeout: a holder with nothing ready for `FLOOR_TIMEOUT_MS` -- a
//      slow model, a tool running -- yields to whoever is waiting; the rest of
//      its message re-queues behind what is queued by then.
//   6. Backlog: more than `BACKLOG_MS` unplayed in one thread collapses its
//      oldest *interim* messages into one "Skipping N updates". A final reply
//      (the last message of a finished turn), the message being played, the
//      holder, and a thread's newest message (it may still be arriving) are
//      never collapsed.
//
// The floor holds no timers of its own beyond one: the 4 s yield. Time is the
// injected `Clock`, so every rule is tested on a fake one.

import type { Clock, SpeechItem } from "./queue";

export const FLOOR_TIMEOUT_MS = 4_000;
export const BACKLOG_MS = 60_000;
/** Speech rate for estimating how long unplayed text will take (plan §3.8: 600 characters is about 37 s). */
export const CHARS_PER_SECOND = 16;

/** How long `text` takes to say, in ms, roughly. */
export function estimateMs(text: string): number {
  return Math.round((text.length / CHARS_PER_SECOND) * 1000);
}

interface Msg {
  key: string;
  thread: string;
  /** Where the message stands in line: the id of its first chunk, or a later number after a yield. null: the next chunk to arrive sets it. */
  rank: number | null;
  /** The id of the message's first chunk: when it arrived. Unlike `rank`, a yield never moves it. */
  born: number;
  /** No more chunks are coming (the message closed, was skipped or dropped). */
  ended: boolean;
  /** The last message of a finished turn: a reply, never an update. */
  final: boolean;
  /** A "Skipping N updates" marker: never collapsed itself. */
  note: boolean;
}

export class Floor {
  private msgs = new Map<string, Msg>();
  private holder: string | null = null;
  private idleSince: number | null = null;
  private timer: unknown = null;
  private latest = 0;
  private wake: () => void = () => {};

  constructor(private readonly clock: Clock, readonly timeoutMs = FLOOR_TIMEOUT_MS) {}

  /** The queue tells the floor how to ask for another pass (the yield timer fires into it). */
  bind(wake: () => void): void {
    this.wake = wake;
  }

  // ---- what the queue tells it ---------------------------------------------

  added(item: SpeechItem): void {
    this.latest = Math.max(this.latest, item.id);
    let m = this.msgs.get(item.messageKey);
    if (!m) {
      m = { key: item.messageKey, thread: item.thread, rank: item.id, born: item.id, ended: false, final: false, note: item.kind === "note" };
      this.msgs.set(item.messageKey, m);
    } else {
      if (m.rank === null) m.rank = item.id;
      m.ended = false;
    }
  }

  /** A chunk of `item`'s message began to sound: that message holds the floor. */
  started(item: SpeechItem): void {
    this.holder = item.messageKey;
    this.idleSince = null;
    this.disarm();
  }

  /** No more chunks are coming for this message. */
  end(key: string): void {
    const m = this.msgs.get(key);
    if (m) m.ended = true;
  }

  /** The turn finished: the thread's newest message is its reply, not an update. */
  turnEnded(thread: string): void {
    let newest: Msg | null = null;
    for (const m of this.msgs.values()) {
      if (m.thread === thread && !m.note && (newest === null || m.born > newest.born)) newest = m;
    }
    if (newest) newest.final = true;
  }

  /** Rank a marker where the first thing it replaces stood. */
  rankAs(key: string, rank: number): void {
    const m = this.msgs.get(key);
    if (m) m.rank = rank;
  }

  rankOf(key: string): number {
    return this.msgs.get(key)?.rank ?? Number.MAX_SAFE_INTEGER;
  }

  /** Forget everything (stop). */
  reset(): void {
    this.msgs.clear();
    this.holder = null;
    this.idleSince = null;
    this.disarm();
  }

  // ---- the order -----------------------------------------------------------

  /** The `PlayOrder`: the holder's message first, then messages by rank, each message's chunks in arrival order. */
  order = (live: readonly SpeechItem[]): readonly SpeechItem[] => {
    const rank = (key: string, first: number) => (key === this.holder ? -Infinity : this.msgs.get(key)?.rank ?? first);
    const firstId = new Map<string, number>();
    for (const i of live) if (!firstId.has(i.messageKey)) firstId.set(i.messageKey, i.id);
    return [...live].sort((a, b) =>
      (rank(a.messageKey, firstId.get(a.messageKey)!) - rank(b.messageKey, firstId.get(b.messageKey)!))
      || (a.messageKey === b.messageKey ? a.id - b.id : (firstId.get(a.messageKey)! - firstId.get(b.messageKey)!))
      || a.id - b.id);
  };

  /**
   * Is `item` kept from playing because another message holds the floor? A
   * holder with nothing ready still holds it, until `settle` yields it.
   */
  holds(item: SpeechItem): boolean {
    return this.holder !== null && item.messageKey !== this.holder;
  }

  // ---- yielding ------------------------------------------------------------

  /**
   * Called on every pass. Releases a finished holder, and yields a stalled one
   * when someone is waiting. Returns true when the order changed.
   */
  settle(live: readonly SpeechItem[]): boolean {
    this.forget(live);
    const key = this.holder;
    if (key === null) { this.disarm(); return false; }
    const m = this.msgs.get(key);
    const mine = live.filter((i) => i.messageKey === key);
    if (!m || (mine.length === 0 && m.ended)) {
      // The message is over: whoever is next goes at once, with no wait for a floor nobody uses.
      this.holder = null;
      this.idleSince = null;
      this.disarm();
      return true;
    }
    if (mine.some((i) => i.state === "ready" || i.state === "playing")) {
      this.idleSince = null;
      this.disarm();
      return false;
    }
    const waiting = live.some((i) => i.messageKey !== key);
    const now = this.clock.now();
    if (this.idleSince === null) this.idleSince = now;
    if (!waiting) { this.disarm(); return false; }
    const left = this.idleSince + this.timeoutMs - now;
    if (left > 0) {
      if (this.timer === null) {
        this.timer = this.clock.setTimeout(() => { this.timer = null; this.wake(); }, left);
      }
      return false;
    }
    // Yield. What is already queued for the message goes behind everything queued now;
    // the chunks still to come take their place in line when they arrive.
    m.rank = mine.length ? this.latest + 0.5 : null;
    this.holder = null;
    this.idleSince = null;
    this.disarm();
    return true;
  }

  private disarm(): void {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }

  /** Messages that ended and have nothing left are forgotten, so the map stays small. */
  private forget(live: readonly SpeechItem[]): void {
    const keys = new Set(live.map((i) => i.messageKey));
    for (const [key, m] of this.msgs) if (m.ended && !keys.has(key)) this.msgs.delete(key);
  }

  // ---- backlog -------------------------------------------------------------

  /**
   * Interim messages to collapse so no thread has more than `BACKLOG_MS`
   * unplayed. Oldest first, per thread. A message holding the floor or sounding
   * is spared, as are final replies, markers and each thread's newest message.
   */
  collapse(live: readonly SpeechItem[], maxMs = BACKLOG_MS): string[] {
    const out: string[] = [];
    const threads = new Set(live.map((i) => i.thread));
    for (const thread of threads) {
      const mine = live.filter((i) => i.thread === thread && i.state !== "playing");
      let total = mine.reduce((sum, i) => sum + estimateMs(i.text), 0);
      if (total <= maxMs) continue;
      const keys = [...new Set(this.order(live).filter((i) => i.thread === thread).map((i) => i.messageKey))];
      // The newest by arrival, not by place in line: a yielded older message is behind it.
      const bornOf = (k: string) => (this.msgs.get(k)?.note ? -1 : this.msgs.get(k)?.born ?? -1);
      const newest = keys.reduce<string | null>((best, k) => (best === null || bornOf(k) > bornOf(best) ? k : best), null);
      for (const key of keys) {
        if (total <= maxMs) break;
        const m = this.msgs.get(key);
        if (!m || m.final || m.note || key === this.holder || key === newest) continue;
        const items = live.filter((i) => i.messageKey === key);
        if (items.some((i) => i.state === "playing")) continue;
        total -= items.reduce((sum, i) => sum + estimateMs(i.text), 0);
        out.push(key);
      }
    }
    return out;
  }
}
