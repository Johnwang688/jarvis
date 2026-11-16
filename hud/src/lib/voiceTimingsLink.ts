import type { VoiceDiagnostics } from "./voiceDiagnostics";
import type { SpeechEventLike } from "./speech/controller";
import { observeVoiceTiming } from "./voiceTimings";

export const REPLY_TIMING_TTL_MS = 120_000;
/** Turn ids remembered per chat: an older turn cannot still be running. */
const SEEN_TURNS = 32;
/** First-text times kept per pending send, for text that beats the send's answer. */
const EARLY_TURNS = 8;

type TimingEvent = SpeechEventLike & { turn_id?: unknown };

interface Link {
  ids: readonly number[];
  at: number;
  seq: number;
  /** Issued while a reply was live in its chat (order-based judging only). */
  behindTurn: boolean;
  /** Turns seen in the chat before the send: never its reply. */
  before: ReadonlySet<string>;
  /**
   * The send's own reply turn: a string once known (the send's answer, or a
   * `steer_queued` for its steer), null while it has none (a native steer),
   * undefined when unknown, which leaves only order-based judging.
   */
  target?: string | null;
  /** The send's answer is still on its way: id-carrying text is only noted. */
  awaiting: boolean;
  /** The send's own steer message id, from its answer. */
  steerId?: string;
  /** Steer message ids seen after the send and not yet requeued. */
  steers: Set<unknown>;
  /** steer_queued after the send: message id -> its new turn id. */
  requeued: Map<unknown, string>;
  /** First text per turn after the send, for a target learned later. */
  early: Map<string, number>;
  tokenAt?: number;
}

const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

/**
 * One pending voice send per chat; no text, titles, persistence, or pipeline control.
 *
 * The daemon answers a send with the turn that will reply to it, and every
 * turn's events carry that turn's id, so a reply is matched by id. Order-based
 * judging (turn boundaries) remains only for what carries no ids. Unsure means
 * omitted: a lost boundary or an unaccounted steer drops marks, never moves them.
 */
export class VoiceTimingsLink {
  private pending = new Map<string, Link>();
  /**
   * An explicit turn spans tool calls and lasts until `turn_finished` (or an
   * explicitly fatal error): a user message or steer inside it does not start a
   * new reply. Without a turn, text/text_delta delimit one message. `lastAt` is
   * the chat's last observed event: a reply silent for the TTL is forgotten.
   */
  private replies = new Map<string, { seq: number; turn: boolean; lastAt: number }>();
  /** Recent turn ids per chat, oldest first. */
  private seen = new Map<string, string[]>();
  /**
   * Order, not time: two observations can share a `performance.now()` tick, so
   * "the reply began after the send" compares this counter, never the clock.
   */
  private seq = 0;
  constructor(
    private diagnostics: VoiceDiagnostics,
    private now: () => number = () => performance.now(),
    private audioTextStartedAt?: (chat: string) => number | undefined,
  ) {}

  /**
   * Only observation is guarded: the request's result/error passes through
   * unchanged. Its answer is read on a branch of its own, never awaited here.
   */
  issue<T>(chat: string, rows: readonly { diag: number; meta: { mode: string } }[], send: (chat: string) => T, outputEnabled = true): T {
    let link: Link | undefined;
    observeVoiceTiming(() => {
      link = this.start(chat, rows.filter(row => row.meta.mode === "auto").map(row => row.diag));
      if (!outputEnabled) { this.pending.delete(chat); link = undefined; }
    });
    const result = send(chat);
    observeVoiceTiming(() => {
      const mine = link;
      if (!mine) return;
      const then = (result as { then?: unknown } | null | undefined)?.then;
      if (typeof then === "function") {
        mine.awaiting = true;
        (then as (ok: (v: unknown) => void, no: () => void) => unknown).call(result,
          answer => observeVoiceTiming(() => this.answered(chat, mine, answer)),
          // A failed request may not have reached the chat: nothing to time.
          () => observeVoiceTiming(() => { if (this.pending.get(chat) === mine && mine.tokenAt === undefined) this.pending.delete(chat); }));
      } else {
        this.answered(chat, mine, result);
      }
    });
    return result;
  }

  /** Called at the send request, with AUTO rows only. A batched send can contain several utterances. */
  sent(chat: string, ids: readonly number[]) {
    observeVoiceTiming(() => { this.start(chat, ids); });
  }

  private start(chat: string, ids: readonly number[]): Link | undefined {
    const at = this.now();
    this.expire(at);
    if (!ids.length) return undefined;
    const link: Link = {
      ids: [...ids], at, seq: ++this.seq, behindTurn: this.replies.has(chat), awaiting: false,
      before: new Set(this.seen.get(chat) ?? []), steers: new Set(), requeued: new Map(), early: new Map(),
    };
    this.pending.set(chat, link);
    for (const id of ids) observeVoiceTiming(() => this.diagnostics.mark(id, "sent", at));
    return link;
  }

  /** The send's answer (`SendResult`): which turn replies to it, if any. */
  private answered(chat: string, link: Link, answer: unknown) {
    link.awaiting = false;
    if (this.pending.get(chat) !== link || link.tokenAt !== undefined) return;
    if (answer === null || typeof answer !== "object") return;
    const a = answer as Record<string, unknown>;
    if (a.status === "dropped") { this.pending.delete(chat); return; }
    // Queued, or interrupting a turn that could not be steered: its own turn
    // waits under `queued_turn_id` (`turn_id` is then the running turn's).
    const own = a.status === "started" ? str(a.turn_id)
      : a.status === "queued" || (a.status === "steered" && a.mode === "interrupt") ? str(a.queued_turn_id)
      : undefined;
    if (own) { this.target(link, own); return; }
    if (a.status === "steered") {
      // A native steer is answered inside the running turn: no turn of its own
      // unless the daemon requeues it.
      link.target = null;
      link.steerId = str(a.message_id);
      const later = link.steerId ? link.requeued.get(link.steerId) : undefined;
      if (later) this.target(link, later);
    }
  }

  private target(link: Link, turn: string) {
    link.target = turn;
    const at = link.early.get(turn);
    if (at !== undefined) this.token(link, at);
  }

  private token(link: Link, at: number) {
    if (link.tokenAt !== undefined) return;
    for (const id of link.ids) observeVoiceTiming(() => this.diagnostics.mark(id, "firstToken", at));
    link.tokenAt = at;
  }

  private remember(chat: string, turn: string) {
    let turns = this.seen.get(chat);
    if (!turns) this.seen.set(chat, (turns = []));
    if (turns.includes(turn)) return;
    turns.push(turn);
    if (turns.length > SEEN_TURNS) turns.shift();
  }

  /** Read before speech, including before sent, so an already-open reply cannot claim a new send. */
  event(e: TimingEvent) {
    observeVoiceTiming(() => {
      const at = this.now();
      this.expire(at);
      if (e.kind === "_connected" || e.kind === "_disconnected") {
        // Whatever was published in a stream gap is not replayed, so every live
        // reply's end may be lost: forget them all, and the sends waiting behind them.
        for (const chat of [...this.replies.keys()]) this.forget(chat);
        return;
      }
      const chat = e.thread_id;
      if (!chat) return;
      const turn = str(e.turn_id);
      if (turn) this.remember(chat, turn);
      if (e.self_started || e.compaction || (e.relayed !== null && typeof e.relayed === "object")) return;
      const live = this.replies.get(chat);
      if (live) live.lastAt = at;
      const data = e.data !== null && typeof e.data === "object" ? e.data : undefined;
      const link = this.pending.get(chat);
      if (e.kind === "turn_started") {
        // A steer still unaccounted for was taken by the turn that just ended,
        // or its fate was missed: this new turn is not its reply.
        if (link && link.tokenAt === undefined && typeof link.target !== "string"
            && (link.steers.size || link.target === null)) this.pending.delete(chat);
        this.replies.set(chat, { seq: ++this.seq, turn: true, lastAt: at });
        return;
      }
      if (e.kind === "steer_queued") {
        // The daemon requeued a steer as a turn of its own (refused, or not
        // delivered before the turn ended); `data.turn_id` is that turn. It is
        // not necessarily the next turn: other requeued messages may run first.
        if (!link || link.tokenAt !== undefined) return;
        const message = data?.message_id, next = str(data?.turn_id);
        if (next && link.requeued.size < EARLY_TURNS) link.requeued.set(message, next);
        const ours = link.steerId !== undefined ? message === link.steerId : link.steers.has(message);
        link.steers.delete(message);
        if (ours && next && typeof link.target !== "string") this.target(link, next);
        return;
      }
      // Only an explicit `fatal: true` ends a turn; unknown severity leaves it live
      // (a missed end costs marks, a false one mis-attributes the old turn's text).
      const fatal = e.kind === "error" && data?.fatal === true;
      if (e.kind === "turn_finished" || fatal) {
        this.replies.delete(chat);
        return;
      }
      if (e.kind === "user_message" && data?.steer === true && link && link.tokenAt === undefined) {
        // A steer is answered inside the running turn, whose text cannot be told
        // apart from what the turn was already saying: nothing is marked until a
        // `steer_queued` for it names a turn of its own. The steer may be another
        // message's; omitting this send's marks is then the safe side.
        link.steers.add(data.message_id);
      }
      if (e.kind === "user_message" || e.kind === "error") {
        // Queued, steering or refused-steer messages and nonfatal errors leave a live
        // turn running; they only close a turn-less message.
        if (!this.replies.get(chat)?.turn) this.replies.delete(chat);
        return;
      }
      if (e.kind !== "text_delta" && e.kind !== "text") return;
      const text = (e.data ?? e).text;
      if (typeof text !== "string" || !text.length) return;
      let reply = this.replies.get(chat);
      if (!reply) this.replies.set(chat, (reply = { seq: ++this.seq, turn: false, lastAt: at }));
      if (link && link.tokenAt === undefined) {
        if (turn) {
          // By id: the send's own turn, once known. Before that, a turn that
          // was already there at the send is never its reply.
          if (!link.before.has(turn) && !link.early.has(turn) && link.early.size < EARLY_TURNS) link.early.set(turn, at);
          if (typeof link.target === "string") { if (turn === link.target) this.token(link, at); }
          else if (link.target === undefined && !link.awaiting && !link.steers.size && !link.before.has(turn) && reply.seq > link.seq) {
            this.token(link, at);
          }
        } else if (link.target !== null && !link.steers.size && reply.seq > link.seq) {
          // No ids (an older daemon): judged by order.
          this.token(link, at);
        }
      }
      if (e.kind === "text" && !reply.turn) this.replies.delete(chat);
    });
  }

  firstAudio(chat: string) {
    observeVoiceTiming(() => {
      const at = this.now();
      this.expire(at);
      const link = this.pending.get(chat);
      if (!link || link.tokenAt === undefined) return;
      // The controller supplies the first text time of the playing message,
      // preserved across synthesis, summaries and queue waits. Playback time
      // alone cannot identify a reply; a message whose text began before this
      // send's first token is an older one.
      const textAt = this.audioTextStartedAt ? this.audioTextStartedAt(chat) : link.tokenAt;
      if (textAt === undefined || !Number.isFinite(textAt) || textAt < link.at || textAt < link.tokenAt) return;
      const rows = this.diagnostics.snapshot().rows;
      for (const id of link.ids) {
        if (rows.find(row => row.id === id)?.marks?.firstToken === undefined) continue;
        observeVoiceTiming(() => this.diagnostics.mark(id, "firstAudio", at));
      }
      this.pending.delete(chat);
    });
  }

  /** Output off/muted drops its pending reply correlation. */
  retain(canSpeak: (chat: string) => boolean) {
    observeVoiceTiming(() => {
      this.expire(this.now());
      for (const chat of this.pending.keys()) if (!canSpeak(chat)) this.pending.delete(chat);
    });
  }

  private expire(at: number) {
    for (const [chat, link] of this.pending) if (at - link.at >= REPLY_TIMING_TTL_MS) this.pending.delete(chat);
    for (const [chat, reply] of this.replies) if (at - reply.lastAt >= REPLY_TIMING_TTL_MS) this.forget(chat);
  }

  /**
   * A live reply whose end was not observed: drop it, and drop a send queued
   * behind it whose own turn is unknown, since the old reply's continuation
   * could otherwise claim it.
   */
  private forget(chat: string) {
    this.replies.delete(chat);
    const link = this.pending.get(chat);
    if (link?.behindTurn && link.tokenAt === undefined && typeof link.target !== "string") this.pending.delete(chat);
  }
}
