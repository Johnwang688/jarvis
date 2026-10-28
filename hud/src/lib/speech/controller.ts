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
//     A chat the owner muted (`setMuted`) is not read either, and nothing is
//     read while the mic is OFF (`setMicOff`, policy.MIC_OFF_SILENCES_SPEECH).
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
//     holds. Anything that silences the thread -- a send, Stop, an interrupt,
//     Skip, the pane leaving, read-aloud off, the lock lost -- bumps the
//     thread's epoch, and everything concluded before the bump (the pending
//     summary and each message queued behind it) drops itself at `deliver`.
//     While a summary is awaited speech counts as active (`SpeechView.active`:
//     the detector stays held off, `onIdle` waits) and a barge-in holds the
//     delivery until it ends.
//   - Native sub-agents are silent (SV-5): their prose is `subagent_event`,
//     which no case below reads. The bus's `subagent` record is read by
//     `notices.ts` for a status and a count only, and what it makes is one
//     short sentence of Jarvis's own ("Two sub-agents finished."), queued as
//     a `notice` through the same checks as a reply: the thread must be read
//     (`speaks`), a send or Stop since drops it, a barge-in holds it, and it
//     waits while the owner is talking. Never posted anywhere.
//   - Nothing here can reach an approval: no import, no route, no event it
//     answers. Speech is output only (decision V-6).
//
// The controller has no timers of its own and touches no DOM: the clock,
// player, synthesizer and speaker gate are injected.

import { Floor } from "./floor";
import { NOTICE_STALE_MS, SubagentNotices } from "./notices";
import { SpeechQueue, type Clock, type ItemKind, type Player, type QueueView, type SpeechItem } from "./queue";
import { ProseStream, type SpokenChunk } from "./stream";
import { SUMMARY_DEADLINE_MS, needsSummary, spokenText, summaryChunk, truncatedProse, type Summarize } from "./summary";

export interface SpeechEventLike {
  kind?: string;
  thread_id?: string;
  data?: { text?: string; stop?: string; [key: string]: unknown } | undefined;
  text?: string;
  stop?: string;
  /** A record of a turn the CLI started by itself (SV-6): never spoken (owner, 2026-10-15). */
  self_started?: boolean;
  /** A record of a turn another chat's message started, or of the reply turn
   * that reports its answer (chat-to-chat O-8): never spoken. */
  relayed?: unknown;
  /** A record of a compaction turn (WP-C): never spoken. */
  compaction?: boolean;
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
  /** A spoken summary of a long message (`POST /speech/summary`). Absent: a long message is read truncated. */
  summarize?: Summarize;
  /** The owner is talking (a barge-in holds, a recording is under way): a sub-agent notice waits. Absent: never. */
  ownerBusy?: () => boolean;
}

/** A wake hit this soon after a sentence that said his name ended may still be his own voice (recognizer lag, the room's tail). */
export const NAME_GRACE_MS = 2000;

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
  /** A message is out for its summary (or waiting behind one): speech is coming. */
  summarizing: boolean;
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
  /** The next message number of each thread: it outlives `open`, so a key is never reused. */
  private counters = new Map<string, number>();
  /** Per thread, the tail of what must come out in order while a summary is awaited. */
  private lanes = new Map<string, Promise<void>>();
  private pending = new Set<Pending>();
  /** Per thread, how many silencings it has had; with `allEpoch` (silencings of everything) the epoch. */
  private epochs = new Map<string, number>();
  private allEpoch = 0;
  /** A barge-in began with nothing sounding but a summary awaited: deliveries wait for `endBargeIn`. */
  private holdDelivery = false;
  private deferred: (() => void)[] = [];
  /** The hangover ended while a summary was awaited: idle is owed once it is not. */
  private idleOwed = false;
  private speakers: ReadonlySet<string> = new Set();
  private muted: ReadonlySet<string> = new Set();
  private on = true;
  /** The mic is OFF: text only, nothing read (the read-aloud switch keeps its own value). */
  private micOff = false;
  private listeners = new Set<() => void>();
  private cached: SpeechView | null = null;
  private unsubs: (() => void)[] = [];
  private wasSpeaker: boolean;
  private wasLive = false;
  private hang: unknown = null;
  private idleListeners = new Set<() => void>();
  private bargePaused = false;
  /** Does this text contain the wake word? (V9: so a sentence that says his name cannot be mistaken for the owner saying it.) */
  private isName: ((text: string) => boolean) | null = null;
  /** Until when (clock time) a sentence that said his name counts as still being heard; Infinity while it sounds. */
  private nameUntil = -Infinity;
  /** The message a barge-in interrupted: what a send skips (V-7), not whatever is next in line by then. */
  private bargedMessage: { thread: string; messageKey: string } | null = null;
  /** SV-5: "Two sub-agents finished." and the like, from the bus's `subagent` records. */
  private readonly notices: SubagentNotices;
  private noticeN = 0;
  /** Notices handed to the queue and not yet stale, with the timer that drops one that never began to sound. */
  private noticeLive = new Map<string, unknown>();
  private ownerBusy: (() => boolean) | null = null;

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
    this.notices = new SubagentNotices({
      clock: deps.clock,
      canSpeak: (thread) => this.speaks(thread),
      busy: () => (this.ownerBusy ?? this.deps.ownerBusy)?.() ?? false,
      speak: (thread, text) => this.speakNotice(thread, text),
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
    const summarizing = this.lanes.size > 0;
    const active = q.state === "playing" || q.state === "buffering" || this.hang !== null
      || (summarizing && q.state !== "paused" && !this.holdDelivery);
    const old = this.cached;
    if (old && old.active === active && old.summarizing === summarizing && old.state === q.state && old.thread === q.thread && old.queued === q.queued && old.failed === q.failed
      && old.blocked === q.blocked && old.enabled === this.on && old.speaker === speaker) return old;
    return (this.cached = { ...q, active, summarizing, enabled: this.on, speaker });
  }

  /** Called when speech goes idle (after the hangover): the follow-up window opens here. */
  onIdle(cb: () => void): () => void {
    this.idleListeners.add(cb);
    return () => { this.idleListeners.delete(cb); };
  }

  /** The queue changed: keep the hangover that holds `active` just after the last chunk. */
  private settle(): void {
    this.trackName();
    const state = this.queue.view().state;
    const live = state === "playing" || state === "buffering";
    if (live) {
      this.wasLive = true;
      this.idleOwed = false;
      if (this.hang !== null) { this.deps.clock.clearTimeout(this.hang); this.hang = null; }
    } else if (state === "idle" && this.wasLive && this.hang === null) {
      this.wasLive = false;
      this.hang = this.deps.clock.setTimeout(() => {
        this.hang = null;
        this.notify();
        // Not idle while a summary is awaited: the follow-up window must not open before it is said.
        if (this.lanes.size > 0) { this.idleOwed = true; return; }
        this.fireIdle();
      }, HANGOVER_MS);
    } else if (state === "paused") {
      this.wasLive = false;
      if (this.hang !== null) { this.deps.clock.clearTimeout(this.hang); this.hang = null; }
    }
  }

  /**
   * V9: the wake word as a regular expression of the window (`setNameMatcher`). While a sentence that contains
   * it is playing, and for NAME_GRACE_MS after it ended, a wake hit may be the room hearing him say it, so
   * the caller ignores it (`sayingOwnName`). The owner then pauses him with the talk key.
   */
  setNameMatcher(fn: ((text: string) => boolean) | null): void {
    this.isName = fn;
    this.trackName();
  }

  sayingOwnName(): boolean {
    return this.nameUntil === Infinity || this.deps.clock.now() < this.nameUntil;
  }

  private trackName(): void {
    const match = this.isName;
    let sounding = false;
    if (match) {
      for (const i of this.queue.items()) {
        if (i.state !== "playing") continue;
        let hit = false;
        try { hit = match(i.text); } catch { hit = true; }   // fail closed: a matcher that throws hears his name
        if (hit) { sounding = true; break; }
      }
    }
    if (sounding) this.nameUntil = Infinity;
    else if (this.nameUntil === Infinity) this.nameUntil = this.deps.clock.now() + NAME_GRACE_MS;
  }

  private fireIdle(): void {
    this.idleOwed = false;
    for (const cb of [...this.idleListeners]) {
      try { cb(); } catch { /* a listener never breaks speech */ }
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

  /** The sub-agent notices' own switch (Settings -> Voice): off, none is queued, and one waiting is dropped. */
  setNotices(on: boolean): void {
    if (this.notices.isEnabled() === on) return;
    this.notices.setEnabled(on);
    this.notify();
  }

  /** The chat's children, freshly listed (first watched, reconnected): a `stop` this window missed is learned here. */
  noticeSeed(thread: string, rows: readonly unknown[]): void {
    this.notices.seed(thread, rows);
  }

  /** The window's word that the owner is talking (a barge-in holds, a recording is under way): a notice waits. */
  setOwnerBusy(fn: (() => boolean) | null): void {
    this.ownerBusy = fn;
  }

  noticesOn(): boolean {
    return this.notices.isEnabled();
  }

  /** Sub-agent endings waiting to be said in `thread` (for checks). */
  noticesWaiting(thread: string): { done: number; failed: number } {
    return this.notices.waiting(thread);
  }

  /** The threads to read now (every one shown in a chat pane); any thread that left has its speech dropped. */
  setSpeakers(threads: ReadonlySet<string>): void {
    this.speakers = threads;
    this.prune();
    this.notify();
  }

  /**
   * The mic went OFF (or back on). OFF, everything stops and nothing more is
   * queued, as with read-aloud off -- but the read-aloud switch, its Settings
   * row and the daemon's voice-out flag are left as they were.
   */
  setMicOff(off: boolean): void {
    if (this.micOff === off) return;
    this.micOff = off;
    if (off) this.clearAll();
    this.notify();
  }

  /** The chats the owner muted: silent like a thread in no pane, and its speech goes at once. */
  setMuted(threads: ReadonlySet<string>): void {
    this.muted = threads;
    this.prune();
    this.notify();
  }

  private prune(): void {
    const known = new Set<string>([...this.open.keys(), ...this.lanes.keys(), ...this.queue.items().map((i) => i.thread), ...this.notices.threads()]);
    for (const thread of known) if (!this.speaks(thread)) this.leave(thread);
  }

  private leave(thread: string): void {
    this.notices.cancel(thread);
    this.endOpen(thread);
    this.open.delete(thread);
    this.cancel(thread);
    this.queue.dropThread(thread, "thread_left");
  }

  /** The message in progress for `thread` ends here (it will get no more chunks). */
  private endOpen(thread: string): void {
    const s = this.open.get(thread);
    if (s) this.queue.endMessage(`${thread}:${s.n}`);
  }

  private speaks(thread: string): boolean {
    return this.on && !this.micOff && this.deps.gate.isSpeaker() && this.speakers.has(thread) && !this.muted.has(thread);
  }

  private gateChanged(): void {
    const now = this.deps.gate.isSpeaker();
    // Another window took the floor: this one builds no queue and falls silent.
    if (this.wasSpeaker && !now) this.clearAll();
    this.wasSpeaker = now;
    this.notify();
  }

  private clearAll(): void {
    this.notices.cancel();
    this.open.clear();
    this.cancel();
    this.release();
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
    // SV-5: a sub-agent ended. Heard even for a thread not read now (the owner's
    // Stop on a child must silence its ending later); said only if it is read then.
    if (kind === "subagent") {
      this.notices.heard(thread, e.data ?? e);
      return;
    }
    // The owner's main Stop: the children it hit end however the CLI reports them, and none is announced.
    if (kind === "turn_finished" && (e.data ?? e).stop === "interrupted") this.notices.interrupted(thread);
    // A turn the CLI started by itself after a background sub-agent finished
    // is not read aloud (owner, 2026-10-15; SV-6): none of its records is
    // spoken or ends anything being read. Its Stop still silences the
    // children it hit (above); the sub-agents' own notices are not its records.
    if (e.self_started === true) return;
    // Nor is a turn another chat's message started, or the short turn that
    // reports another chat's reply (chat-to-chat O-8): the owner did not ask
    // in this window, so it is not read to him. The daemon marks each such
    // record `relayed: {message_id, from}` beside `kind`.
    if (e.relayed !== null && typeof e.relayed === "object") return;
    // Nor is a compaction: its one line ("Conversation compacted") is for the
    // log, and the owner did not ask for it to be read (WP-C).
    if (e.compaction === true) return;
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
        // Stop pressed: this turn's speech goes too. Otherwise the last message
        // is the reply, which a backlog never collapses -- once it is queued: a
        // reply out for its summary is not in the queue yet, so this waits in
        // the thread's lane behind it.
        if (data.stop === "interrupted") { this.notices.cancel(thread); this.cancel(thread); this.queue.dropThread(thread, "stale"); }
        else {
          const epoch = this.epoch(thread);
          this.sequence(thread, () => { if (epoch === this.epoch(thread)) this.queue.turnEnded(thread); });
        }
        break;
      default:
        break;
    }
  }

  /** A message went to the thread, by any means: the reply being read is stale. */
  messageSent(thread: string): void {
    this.notices.cancel(thread);
    const mid = this.open.get(thread);
    if (mid?.stream.open) this.hush(mid);   // a message in progress; a finished one has nothing left to silence
    this.endOpen(thread);
    this.cancel(thread);
    this.queue.dropThread(thread, "stale");
  }

  private slot(thread: string): Open {
    let s = this.open.get(thread);
    if (!s) this.open.set(thread, (s = { stream: new ProseStream(), n: this.counters.get(thread) ?? 0, held: [] }));
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
    const epoch = this.epoch(thread);
    s.held = [];
    s.n += 1;
    this.counters.set(thread, s.n);
    if (!held.length) return;
    if (!needsSummary(held)) {
      this.sequence(thread, () => this.deliver(thread, key, held, epoch));
      return;
    }
    const wait = this.summary(thread, spokenText(held));
    this.sequence(thread, async () => {
      const got = await wait;
      this.deliver(thread, key, got ?? truncatedProse(held), epoch);
    });
  }

  private epoch(thread: string): number {
    return this.allEpoch + (this.epochs.get(thread) ?? 0);
  }

  /**
   * Queue a message's chunks, if the thread is still to be read and has not
   * been silenced since the message closed (`epoch`). A barge-in that began
   * while a summary was awaited holds the delivery until it ends.
   */
  private deliver(thread: string, key: string, chunks: readonly { text: string; kind: ItemKind }[], epoch: number): void {
    if (chunks[0]?.kind === "notice" && !this.noticeLive.has(key)) return;   // went stale while held
    if (this.holdDelivery) { this.deferred.push(() => this.deliver(thread, key, chunks, epoch)); return; }
    if (epoch !== this.epoch(thread) || !this.speaks(thread)) return;
    // The voice known now rides on the item; the queue asks `voiceFor` again when it synthesizes (V7).
    const known = this.deps.voiceFor?.(thread);
    const voice = typeof known === "string" ? known : null;
    chunks.forEach((chunk, seq) => {
      this.queue.enqueue({ thread, messageKey: key, seq, text: chunk.text, kind: chunk.kind, voice });
    });
    // The whole message is queued at once: no more chunks are coming, so it frees the floor when spoken.
    this.queue.endMessage(key);
  }

  /**
   * A sub-agent notice (SV-5): one sentence of Jarvis's own, queued like a reply
   * (in the thread's order, under its epoch, held by a barge-in) but as its own
   * message of kind `notice`, which the floor never collapses and never takes
   * for the turn's reply.
   */
  private speakNotice(thread: string, text: string): void {
    const key = `${thread}:notice:${++this.noticeN}`;
    const epoch = this.epoch(thread);
    // Not begun within NOTICE_STALE_MS (held behind a long reply, a barge-in, a pause): the owner has moved on.
    this.noticeLive.set(key, this.deps.clock.setTimeout(() => {
      this.noticeLive.delete(key);
      this.queue.dropUnstarted(key, "stale");
    }, NOTICE_STALE_MS));
    this.sequence(thread, () => this.deliver(thread, key, [{ text, kind: "notice" }], epoch));
  }

  /** The deliveries a barge-in held: made now (each still checks its epoch). */
  private release(): void {
    this.holdDelivery = false;
    const later = this.deferred;
    this.deferred = [];
    for (const f of later) f();
  }

  /**
   * The daemon's summary as one chunk to say; null when it failed, did not
   * answer in time or came back unusable (the caller reads truncated prose).
   * A thread silenced meanwhile aborts the request, but what keeps the answer
   * from being said is the epoch, not the abort: a summariser that answers
   * anyway is still not heard.
   */
  private async summary(thread: string, text: string): Promise<SpokenChunk[] | null> {
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
      const chunk = summaryChunk(raw);
      return chunk ? [chunk] : null;
    } catch {
      return null;
    } finally {
      this.deps.clock.clearTimeout(timer);
      this.pending.delete(entry);
    }
  }

  /**
   * Silence what is coming for `thread` (for all of them when none is named):
   * summaries in flight are aborted, and the epoch moves so that everything
   * concluded before now -- the summary, and the messages queued behind it --
   * is dropped when its turn comes.
   */
  private cancel(thread?: string): void {
    for (const p of this.pending) if (thread === undefined || p.thread === thread) p.ctl.abort();
    if (thread === undefined) this.allEpoch += 1;
    else this.epochs.set(thread, (this.epochs.get(thread) ?? 0) + 1);
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
      this.laneDone();
    });
    this.lanes.set(thread, lane);
    this.notify();
  }

  /** A lane finished: the view changed, and an idle that was owed is paid if nothing is coming. */
  private laneDone(): void {
    this.notify();
    if (this.idleOwed && this.lanes.size === 0 && this.queue.view().state === "idle") this.fireIdle();
  }

  // ---- controls -----------------------------------------------------------

  // The owner's own controls. Any of them takes over from a barge-in's pause.
  pause(): void { this.bargePaused = false; this.queue.pause(); this.release(); }
  resume(): void { this.bargePaused = false; this.release(); this.queue.resume(); }

  toggle(): void {
    this.bargePaused = false;
    this.release();
    if (this.queue.view().state === "paused") this.queue.resume();
    else this.queue.pause();
  }

  /**
   * The owner starts talking over him (decision V-7: barge-in pauses, then
   * resumes). Pauses speech that is in progress and remembers that *it* did; a
   * pause the owner made from the strip is theirs, and is left alone. Returns
   * whether there was speech to hold (playing, buffering, or a summary on its
   * way), so a caller knows whether anything waits to be resumed.
   */
  bargeIn(): boolean {
    const state = this.queue.view().state;
    if (state !== "playing" && state !== "buffering") {
      // Nothing sounds, but a summary is on its way: it waits for the owner to finish.
      if (this.lanes.size > 0 && state === "idle") { this.holdDelivery = true; this.bargePaused = true; this.bargedMessage = null; this.notify(); return true; }
      return false;
    }
    this.bargedMessage = this.queue.headMessage();
    this.queue.pause();
    this.bargePaused = true;
    return true;
  }

  /**
   * The utterance that paused him is over, whatever came of it (nothing sent,
   * dropped, failed, placed for review, or sent). Resumes what this barge-in
   * paused, restarting the sentence (Q13); a thread a message went to has
   * already lost its items (`messageSent`), so what resumes is what remains.
   * `skip`: a message went out (V-7), so the message he was in the middle of (if something was sounding) is
   * not resumed either, whichever chat it was for -- the rest of the queue is.
   */
  endBargeIn(opts: { skip?: boolean } = {}): void {
    if (!this.bargePaused) return;
    this.bargePaused = false;
    const interrupted = this.bargedMessage;
    this.bargedMessage = null;
    if (opts.skip) {
      // The message he was in the middle of goes (if a send did not already take it); the queue goes on.
      // With nothing playing when he was held (a summary on its way), the one awaited is skipped.
      // With nothing sounding when he was held (a summary on its way, or nothing), there is nothing to skip: a
      // summary awaited for another chat is not his to cancel.
      if (interrupted) this.silenceIfOpen(this.queue.skipMessage(interrupted.thread, interrupted.messageKey), () => {});
    }
    this.release();
    this.queue.resume();
  }

  /** A barge-in's pause is in force (the owner has not paused, resumed or stopped since). */
  bargeHeld(): boolean {
    return this.bargePaused;
  }

  /** The rest of the current message is not spoken. */
  skip(): void {
    this.silenceIfOpen(this.queue.skip(), () => {
      // Nothing sounded, so the message next in line is the current one: the oldest summary being
      // awaited (with whatever is queued behind it on its thread). Other threads' summaries are
      // left alone. With none awaited, a message still being written is what is skipped.
      const first = this.pending.values().next().value as Pending | undefined;
      if (first) { this.cancel(first.thread); return; }
      for (const [thread, s] of this.open) if (s.stream.open) { this.hush(s); this.cancel(thread); }
    });
  }

  /** One chat's current message (else its next one) is not spoken: the voice chip's "skip this chat". */
  skipThread(thread: string): void {
    this.silenceIfOpen(this.queue.skipThread(thread), () => {
      // Nothing of the chat's is queued: its awaited summary, else the message being written, is the next.
      const s = this.open.get(thread);
      if (s?.stream.open) this.hush(s);
      if (s?.stream.open || [...this.pending].some((p) => p.thread === thread)) this.cancel(thread);
    });
  }

  private silenceIfOpen(skipped: { thread: string; messageKey: string } | null, none: () => void): void {
    if (!skipped) { none(); return; }
    const s = this.open.get(skipped.thread);
    if (s?.stream.open && `${skipped.thread}:${s.n}` === skipped.messageKey) this.hush(s);
  }

  /** Stop speaking and clear the queue; messages still arriving stay quiet until they end. */
  stop(): void {
    this.notices.cancel();
    this.bargePaused = false;
    for (const s of this.open.values()) if (s.stream.open) this.hush(s);
    this.cancel();
    this.release();
    this.queue.stop();
  }

  clearMarks(): void { this.queue.clearMarks(); }

  dispose(): void {
    for (const off of this.unsubs) off();
    this.unsubs = [];
    if (this.hang !== null) this.deps.clock.clearTimeout(this.hang);
    this.hang = null;
    this.idleListeners.clear();
    this.notices.dispose();
    for (const h of this.noticeLive.values()) this.deps.clock.clearTimeout(h);
    this.noticeLive.clear();
    this.clearAll();
    this.listeners.clear();
  }
}
