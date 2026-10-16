// Spoken notices for native sub-agents (SV-5, plan docs/plans/2026-10-13-subagent-view.md D7).
//
// A chat's own Claude/Codex children are silent by construction -- their
// prose is `subagent_event`, which the speech controller never reads. The one
// thing said is that some finished or failed:
//
//   "A sub-agent finished."   "Two sub-agents finished, one failed."
//
// **Only a status and a count.** The bus's `subagent` record carries ids, a
// status, times and counts (`jarvis/v2/subagents.py#bus`) -- never a name, a
// kind, a brief or a word a child wrote -- and this file reads nothing else
// of it: no `name`, no `kind`, no `last_tool`, no `stop_reason`, whatever a
// record might carry. The sentence is made of a fixed vocabulary and a number,
// so a child cannot put a word of its own into the owner's ears. (The plan
// also allowed the child's name; the owner's rule that a child's text is never
// read aloud, and the bus's silence about names, leave it out.)
//
// Who is not announced, because the owner did it or the owner knows:
//   - `stopped` is never spoken (Q5): the owner's Stop on one child, the
//     main Stop, a session that ended;
//   - a child the owner asked to stop (`stop: "stopping" | "unconfirmed"` was
//     heard) is silent however it then ends -- the CLI may report a killed
//     child as failed;
//   - a child already announced is not announced again.
//
// Bursts coalesce: the first ending opens a window of `NOTICE_WINDOW_MS`,
// every ending inside it joins the same sentence, and it is spoken once when
// the window closes. This file holds no audio and no clock of its own; the
// controller injects both, and decides whether the thread is still to be read
// and whether the owner is talking (`busy`, retried for `BUSY_MAX_MS`, then the
// notice is dropped as stale).

import type { Clock } from "./queue";

/** Endings within this long of the first one are one sentence. */
export const NOTICE_WINDOW_MS = 3000;
/** The owner is talking: ask again this often… */
export const BUSY_RETRY_MS = 500;
/** …and give up (the notice is stale) after this long. */
export const BUSY_MAX_MS = 20_000;
/** Per kind of memory: sids announced, sids the owner asked to stop. */
export const SEEN_CAP = 256;

const SID = /^(?:cl|cx):[A-Za-z0-9_-]{1,128}$/;
const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
const word = (n: number) => (n >= 0 && n < WORDS.length ? WORDS[n] : String(n));

/** The sentence for `done` finished and `failed` failed children; "" for none. Fixed vocabulary and numbers only. */
export function noticeText(done: number, failed: number): string {
  if (!(done > 0) && !(failed > 0)) return "";
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  if (done > 0 && failed > 0) {
    return `${cap(word(done))} sub-agent${done === 1 ? "" : "s"} finished, ${word(failed)} failed.`;
  }
  const n = done > 0 ? done : failed;
  const what = done > 0 ? "finished" : "failed";
  return n === 1 ? `A sub-agent ${what}.` : `${cap(word(n))} sub-agents ${what}.`;
}

interface Burst {
  done: number;
  failed: number;
  timer: unknown;
  /** Clock time the window closed and the sentence was due (for the busy give-up). */
  due: number | null;
}

export interface NoticeDeps {
  clock: Clock;
  /** Say `text` in `thread`'s voice. Only called for a thread `canSpeak` still allows. */
  speak(thread: string, text: string): void;
  /** Is `thread` still to be read (read-aloud on, this window the speaker, a chat pane, not muted)? */
  canSpeak(thread: string): boolean;
  /** The owner is talking (a barge-in holds, a recording is in progress): wait. */
  busy?: () => boolean;
}

export class SubagentNotices {
  private on = true;
  private bursts = new Map<string, Burst>();
  /** `thread|sid` already announced as ended; cleared when the child is live again. */
  private announced = new Set<string>();
  /** `thread|sid` the owner asked to stop and has not been heard live without it. */
  private asked = new Set<string>();

  constructor(private readonly deps: NoticeDeps) {}

  setEnabled(on: boolean): void {
    this.on = on;
    if (!on) this.cancel();
  }

  isEnabled(): boolean {
    return this.on;
  }

  /**
   * One bus `subagent` record for `thread`. Reads `sid`, `status`, `op` and
   * `stop` of it and nothing else.
   */
  heard(thread: string, data: unknown): void {
    if (!data || typeof data !== "object") return;
    const rec = data as { sid?: unknown; status?: unknown; op?: unknown; stop?: unknown };
    const sid = rec.sid;
    if (typeof sid !== "string" || !SID.test(sid)) return;
    const key = `${thread}|${sid}`;
    switch (rec.status) {
      case "running":
      case "backgrounded":
        this.announced.delete(key);
        if (rec.stop === "stopping" || rec.stop === "unconfirmed") this.remember(this.asked, key);
        else this.asked.delete(key);
        return;
      case "done":
      case "failed": {
        // Only the daemon's word that it ended counts: a later progress
        // record about an ended child is not a second ending.
        if (rec.op !== "finished" || this.announced.has(key)) return;
        this.remember(this.announced, key);
        if (this.asked.has(key)) { this.asked.delete(key); return; }
        this.add(thread, rec.status);
        return;
      }
      default:
        // stopped (or anything unknown): never spoken.
        return;
    }
  }

  /** Drop what is waiting to be said: for `thread`, or for all of them. */
  cancel(thread?: string): void {
    for (const [t, b] of [...this.bursts]) {
      if (thread !== undefined && t !== thread) continue;
      this.deps.clock.clearTimeout(b.timer);
      this.bursts.delete(t);
    }
  }

  /** The chats with an ending waiting to be said. */
  threads(): string[] {
    return [...this.bursts.keys()];
  }

  /** How many endings wait to be said, per thread (for checks). */
  waiting(thread: string): { done: number; failed: number } {
    const b = this.bursts.get(thread);
    return { done: b?.done ?? 0, failed: b?.failed ?? 0 };
  }

  dispose(): void {
    this.cancel();
    this.announced.clear();
    this.asked.clear();
  }

  private remember(set: Set<string>, key: string): void {
    set.delete(key);
    set.add(key);
    if (set.size > SEEN_CAP) set.delete(set.values().next().value as string);
  }

  private add(thread: string, ending: "done" | "failed"): void {
    if (!this.on) return;
    let b = this.bursts.get(thread);
    if (!b) {
      b = { done: 0, failed: 0, timer: null, due: null };
      this.bursts.set(thread, b);
      const burst = b;
      burst.timer = this.deps.clock.setTimeout(() => this.close(thread, burst), NOTICE_WINDOW_MS);
    }
    if (ending === "done") b.done += 1;
    else b.failed += 1;
  }

  /** The window closed (or a busy retry is due): say it, wait for the owner, or drop it. */
  private close(thread: string, b: Burst): void {
    if (this.bursts.get(thread) !== b) return;
    b.timer = null;
    const now = this.deps.clock.now();
    if (b.due === null) b.due = now;
    if (!this.on || !this.deps.canSpeak(thread)) { this.bursts.delete(thread); return; }
    let busy = false;
    try { busy = !!this.deps.busy?.(); } catch { busy = true; }
    if (busy) {
      if (now - b.due >= BUSY_MAX_MS) { this.bursts.delete(thread); return; }
      b.timer = this.deps.clock.setTimeout(() => this.close(thread, b), BUSY_RETRY_MS);
      return;
    }
    this.bursts.delete(thread);
    const text = noticeText(b.done, b.failed);
    if (text) this.deps.speak(thread, text);
  }
}
