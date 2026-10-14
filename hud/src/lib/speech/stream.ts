// One message's prose, from deltas to chunks worth synthesizing (V6, plan §3.7).
//
// The filter (`prose.ts`) turns the deltas into speakable text; v1's splitter
// (`split.ts`) cuts it into chunks. A chunk is returned the moment it is
// *settled* (the text after it can no longer merge it back), so speech starts
// while the model is still writing; the rest comes out when the message closes.

import { ProseFilter } from "./prose";
import { sentences, stableChunks } from "./split";

/**
 * A safety stop, not the design: until `/speech/summary` exists (V8) a very
 * long reply would be read to the end. Past this many spoken characters a
 * message says `MORE_ON_SCREEN` and stops. V8 replaces it with the plan's
 * 600-character rule and a summary.
 */
export const SPOKEN_CAP_CHARS = 1200;
export const MORE_ON_SCREEN = "…and more on screen.";

/** The fast path appends this to a reply that ran into its token limit. */
const CUT_OFF_TAIL = /\s*\[cut off at the token limit\]\s*$/;

export interface SpokenChunk {
  text: string;
  /** `fallback`: words of Jarvis's own, not the agent's. */
  kind: "verbatim" | "fallback";
}

export class ProseStream {
  private filter = new ProseFilter();
  private raw = "";
  private speech = "";
  private emitted = 0;
  private spoken = 0;
  private capped = false;
  private quiet = false;
  private gapped = false;

  constructor(private readonly cap = SPOKEN_CAP_CHARS) {}

  /** Anything fed since the message opened. */
  get open(): boolean {
    return this.raw !== "" || this.speech !== "";
  }

  /** The message was skipped or stopped: nothing more of it is spoken. */
  silence(): void {
    this.quiet = true;
  }

  get silenced(): boolean {
    return this.quiet;
  }

  /**
   * The event stream dropped and came back. Deltas in the gap are lost, so the
   * sentence in progress cannot be finished: it is forgotten at the next delta
   * or at a close, but kept for the settled `text`, whose remainder after what
   * already streamed is exactly what the gap swallowed (nothing is said twice).
   */
  gap(): void {
    if (this.open) this.gapped = true;
  }

  /** One `text_delta`: the chunks that just became settled. */
  feed(delta: string): SpokenChunk[] {
    if (this.quiet || !delta) return [];
    if (this.gapped) {
      this.gapped = false;
      this.filter.discardLine();
    }
    this.raw += delta;
    this.speech += this.filter.push(delta);
    return this.take(false);
  }

  /**
   * The settled `text` of the message. When the deltas already carried it, only
   * its unspoken remainder is fed (the fast path's joined continuations are the
   * case); a text that differs from what streamed adds nothing, so no sentence
   * is spoken twice. The trailing "[cut off ...]" is never spoken.
   */
  settle(text: string): SpokenChunk[] {
    this.gapped = false;   // the whole text is in hand: its remainder continues the line
    const body = (text || "").replace(CUT_OFF_TAIL, "");
    if (!this.quiet) {
      let rest = "";
      if (!this.raw) rest = body;
      else if (body.startsWith(this.raw)) rest = body.slice(this.raw.length);
      if (rest) {
        this.raw += rest;
        this.speech += this.filter.push(rest);
      }
    }
    return this.close();
  }

  /** The message ends (a tool call starts, the turn finishes): speak the rest, then start afresh. */
  close(): SpokenChunk[] {
    let out: SpokenChunk[] = [];
    if (!this.quiet) {
      if (this.gapped) this.filter.discardLine();
      this.speech += this.filter.end();
      out = this.take(true);
    }
    this.gapped = false;
    this.filter = new ProseFilter();
    this.raw = "";
    this.speech = "";
    this.emitted = 0;
    this.spoken = 0;
    this.capped = false;
    this.quiet = false;
    return out;
  }

  private take(final: boolean): SpokenChunk[] {
    if (this.capped) return [];
    const all = (final ? sentences(this.speech) : stableChunks(this.speech)).filter((c) => c.trim());
    const fresh = all.slice(this.emitted);
    const out: SpokenChunk[] = [];
    for (const text of fresh) {
      if (this.spoken + text.length > this.cap && this.spoken > 0) {
        this.capped = true;
        out.push({ text: MORE_ON_SCREEN, kind: "fallback" });
        break;
      }
      this.spoken += text.length;
      out.push({ text, kind: "verbatim" });
    }
    this.emitted = all.length;
    return out;
  }
}
