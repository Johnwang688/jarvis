// Spoken summaries (V8, voice plan §3.8 and decisions V-4 / open question 2).
//
// The owner's answer: a message of up to 600 spoken characters is read as
// written; a longer one is *replaced* by a short summary from the daemon
// (`POST /speech/summary`), with no verbatim lead-in. When the summary cannot
// be had (a failure, or the deadline), the opening is read and the rest is
// marked ("...and more on screen") -- decision V-4.
//
// "Spoken characters" are what the speech filter lets through (`prose.ts`):
// code, diffs, paths and the rest never count, and are never sent to the
// summariser either: only the filtered prose is.

import type { SpokenChunk } from "./stream";

/** A message of this many spoken characters or fewer is read verbatim (about 35-40 s at 1.2x). */
export const VERBATIM_MAX_CHARS = 600;

/** On a failed summary, the opening read before the mark. */
export const FALLBACK_CHARS = 400;

/** The HUD gives the daemon (6 s deadline) a little longer than its own, then reads the fallback. */
export const SUMMARY_DEADLINE_MS = 8_000;

/** A summary longer than the daemon promises (300) is not trusted to be one. */
export const SUMMARY_MAX_CHARS = 400;

export const MORE_ON_SCREEN = "…and more on screen.";

/** The summariser: the prose of one long message in, a short spoken summary out (or a rejection). */
export type Summarize = (text: string, opts: { thread: string; signal: AbortSignal }) => Promise<string>;

/** The spoken text of a message, as the summariser is given it. */
export function spokenText(chunks: readonly SpokenChunk[]): string {
  return chunks.map((c) => c.text).join(" ");
}

/** Is this message too long to read as written? */
export function needsSummary(chunks: readonly SpokenChunk[]): boolean {
  return spokenText(chunks).length > VERBATIM_MAX_CHARS;
}

function clip(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return (space > limit / 2 ? cut.slice(0, space) : cut).trim();
}

/** The fallback: whole sentences up to `FALLBACK_CHARS`, then the mark. Never empty. */
export function truncatedProse(chunks: readonly SpokenChunk[]): SpokenChunk[] {
  const out: SpokenChunk[] = [];
  let used = 0;
  for (const chunk of chunks) {
    if (!out.length && chunk.text.length > FALLBACK_CHARS) {
      out.push({ text: clip(chunk.text, FALLBACK_CHARS), kind: "verbatim" });
      break;
    }
    if (used + chunk.text.length > FALLBACK_CHARS) break;
    out.push(chunk);
    used += chunk.text.length + 1;
  }
  out.push({ text: MORE_ON_SCREEN, kind: "fallback" });
  return out;
}

/** The daemon's answer as one chunk to say, or null when it is not usable. */
export function summaryChunk(raw: unknown): SpokenChunk | null {
  if (typeof raw !== "string") return null;
  const text = raw.replace(/\s+/g, " ").trim();
  if (!text || text.length > SUMMARY_MAX_CHARS) return null;
  return { text, kind: "summary" };
}
