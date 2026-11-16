import type { VoiceDiagnostics } from "./voiceDiagnostics";
import type { SpeechEventLike } from "./speech/controller";

export const REPLY_TIMING_TTL_MS = 120_000;

/** One pending voice send per chat; no text, titles, persistence, or pipeline control. */
export class VoiceTimingsLink {
  private pending = new Map<string, { ids: readonly number[]; at: number }>();
  constructor(private diagnostics: VoiceDiagnostics, private now: () => number = () => performance.now()) {}

  /** Wrap only the existing request call: REVIEW/manual sends have no timing rows. */
  issue<T>(chat: string, rows: readonly { diag: number; meta: { mode: string } }[], send: (chat: string) => T, outputEnabled = true): T {
    this.sent(chat, rows.filter(row => row.meta.mode === "auto").map(row => row.diag));
    if (!outputEnabled) this.pending.delete(chat);
    return send(chat);
  }

  /** Called at the send request, with AUTO rows only. A batched send can contain several utterances. */
  sent(chat: string, ids: readonly number[]) {
    const at = this.now();
    this.expire(at);
    if (!ids.length) return;
    for (const id of ids) this.diagnostics.mark(id, "sent", at);
    this.pending.set(chat, { ids: [...ids], at });
  }

  /** Read the event before speech queues it. Another chat, empty text and non-reply records are inert. */
  event(e: SpeechEventLike) {
    const at = this.now();
    this.expire(at);
    if (e.kind !== "text_delta" && e.kind !== "text") return;
    if (e.self_started || e.compaction || (e.relayed !== null && typeof e.relayed === "object")) return;
    const text = (e.data ?? e).text;
    if (typeof text !== "string" || !text.length || !e.thread_id) return;
    const link = this.pending.get(e.thread_id);
    if (link) for (const id of link.ids) this.diagnostics.mark(id, "firstToken", at);
  }

  firstAudio(chat: string) {
    const at = this.now();
    this.expire(at);
    const link = this.pending.get(chat);
    if (!link) return;
    for (const id of link.ids) this.diagnostics.mark(id, "firstAudio", at);
    this.pending.delete(chat);
  }

  /** Output off/muted drops its pending reply correlation. */
  retain(canSpeak: (chat: string) => boolean) {
    this.expire(this.now());
    for (const chat of this.pending.keys()) if (!canSpeak(chat)) this.pending.delete(chat);
  }

  private expire(at: number) {
    for (const [chat, link] of this.pending) if (at - link.at >= REPLY_TIMING_TTL_MS) this.pending.delete(chat);
  }
}
