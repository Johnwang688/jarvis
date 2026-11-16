import type { VoiceDiagnostics } from "./voiceDiagnostics";
import type { SpeechEventLike } from "./speech/controller";
import { observeVoiceTiming } from "./voiceTimings";

export const REPLY_TIMING_TTL_MS = 120_000;

/** One pending voice send per chat; no text, titles, persistence, or pipeline control. */
export class VoiceTimingsLink {
  /**
   * `behindTurn`: the send was issued while a reply was live in its chat, so only
   * that reply's observed end separates its text from the send's own reply.
   * `steers`: steer message ids seen after the send and not yet requeued.
   */
  private pending = new Map<string, {
    ids: readonly number[]; at: number; seq: number; behindTurn: boolean; steers: Set<unknown>; tokenAt?: number;
  }>();
  /**
   * An explicit turn spans tool calls and lasts until `turn_finished` (or an
   * explicitly fatal error): a user message or steer inside it does not start a
   * new reply. Without a turn, text/text_delta delimit one message. `lastAt` is
   * the chat's last observed event: a reply silent for the TTL is forgotten.
   */
  private replies = new Map<string, { seq: number; turn: boolean; lastAt: number }>();
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

  /** Only observation is guarded: the request's result/error passes through unchanged. */
  issue<T>(chat: string, rows: readonly { diag: number; meta: { mode: string } }[], send: (chat: string) => T, outputEnabled = true): T {
    observeVoiceTiming(() => {
      this.sent(chat, rows.filter(row => row.meta.mode === "auto").map(row => row.diag));
      if (!outputEnabled) this.pending.delete(chat);
    });
    return send(chat);
  }

  /** Called at the send request, with AUTO rows only. A batched send can contain several utterances. */
  sent(chat: string, ids: readonly number[]) {
    observeVoiceTiming(() => {
      const at = this.now();
      this.expire(at);
      if (!ids.length) return;
      this.pending.set(chat, { ids: [...ids], at, seq: ++this.seq, behindTurn: this.replies.has(chat), steers: new Set() });
      for (const id of ids) observeVoiceTiming(() => this.diagnostics.mark(id, "sent", at));
    });
  }

  /** Read before speech, including before sent, so an already-open reply cannot claim a new send. */
  event(e: SpeechEventLike) {
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
      if (e.self_started || e.compaction || (e.relayed !== null && typeof e.relayed === "object")) return;
      const live = this.replies.get(chat);
      if (live) live.lastAt = at;
      const data = e.data !== null && typeof e.data === "object" ? e.data : undefined;
      const link = this.pending.get(chat);
      if (e.kind === "turn_started") {
        // A steer still unaccounted for was taken by the turn that just ended,
        // or its fate was missed: this new turn is not its reply.
        if (link?.steers.size && link.tokenAt === undefined) this.pending.delete(chat);
        this.replies.set(chat, { seq: ++this.seq, turn: true, lastAt: at });
        return;
      }
      if (e.kind === "steer_queued") {
        // The daemon requeued a steer as a turn of its own (refused, or not
        // delivered before the turn ended): the next turn answers it.
        link?.steers.delete(data?.message_id);
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
        // `steer_queued` for it says it waits for a turn of its own. The steer may
        // be another message's; omitting this send's marks is then the safe side.
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
      if (link && !link.steers.size && reply.seq > link.seq && link.tokenAt === undefined) {
        for (const id of link.ids) observeVoiceTiming(() => this.diagnostics.mark(id, "firstToken", at));
        link.tokenAt = at;
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
      // The controller supplies the first text time, preserved across synthesis,
      // summaries and queue waits. Playback time alone cannot identify a reply.
      const textAt = this.audioTextStartedAt ? this.audioTextStartedAt(chat) : link.tokenAt;
      if (textAt === undefined || !Number.isFinite(textAt) || textAt < link.at) return;
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
   * behind it, since its continuation could otherwise claim that send.
   */
  private forget(chat: string) {
    this.replies.delete(chat);
    const link = this.pending.get(chat);
    if (link?.behindTurn && link.tokenAt === undefined) this.pending.delete(chat);
  }
}
