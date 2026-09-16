// Wake phrases — ported from jarvis.html's WAKE_PATTERNS / matchesWake and the
// 2026-08-22 recognizer de-dup, with their behaviour intact.
//
// Two rules for any pattern that lands here, from either side. It is matched
// against a *live interim* transcript of a phrase the recognizer may never have
// heard, so it has to tolerate the spellings Chrome guesses. And it is
// `\b`-anchored at both ends, because a wake hit cancels the turn in flight and
// starts claiming audio — an unanchored pattern fires on a substring of
// ordinary conversation and takes the owner's words mid-sentence.
//
// The built-in is duplicated from avatars.DEFAULT in Python and is still the
// fallback if the config call never answers: losing the wake word to a config
// hiccup is worse than a stale name.

export const WAKE_PATTERNS: RegExp[] = [/\bjarvis\b/i];

/** Compile avatar-supplied regex source, anchoring anything that is not. */
export function compileWake(sources: string[] | undefined | null): RegExp[] {
  const out: RegExp[] = [];
  for (const raw of sources || []) {
    if (!raw) continue;
    // Every degradation path has to leave him summonable: a pattern that will
    // not compile is skipped, never thrown.
    let src = raw;
    if (!src.startsWith("\\b")) src = "\\b" + src;
    if (!src.endsWith("\\b")) src = src + "\\b";
    try {
      out.push(new RegExp(src, "i"));
    } catch {
      /* skip */
    }
  }
  return out.length ? out : WAKE_PATTERNS.slice();
}

export function matchesWake(text: string, patterns: RegExp[] = WAKE_PATTERNS): boolean {
  return patterns.some((re) => re.test(text));
}

export const WAKE_REFIRE_MS = 1500;

/**
 * One decision per recognizer result segment, plus a time floor.
 *
 * `recog.onresult` is level-triggered by nature: Chrome delivers the phrase
 * being spoken as a growing interim transcript — one event per revision — then
 * delivers the same words once more as the final result. "Does this transcript
 * contain his name?" answers yes on every one of them. So the de-dup lives at
 * the edge that produces the events, not partway down the handler consuming
 * them: guarding what an event *does* is not guarding how often it fires.
 *
 * Neither gate can wedge the wake word off. Indices restart with each
 * recognition session (onend restarts it every few seconds), so an index lower
 * than the last one fired means a new session and resets the rule.
 */
export class WakeGate {
  firedSegment = -1;
  firedAt = -1e9;

  /** Call on recognition (re)start. */
  restart() {
    this.firedSegment = -1;
  }

  /**
   * @param newest  index of the newest result segment (results.length - 1)
   * @param now     performance.now()
   * @returns true when this is a new phrase that should fire
   */
  shouldFire(newest: number, now: number): boolean {
    if (newest < this.firedSegment) this.firedSegment = -1; // a restarted session
    if (newest <= this.firedSegment) return false; // the same phrase, re-reported
    // Decided once per segment, whichever way it goes: a hit suppressed by the
    // time floor must not fire late when that same segment is revised.
    this.firedSegment = newest;
    if (now - this.firedAt < WAKE_REFIRE_MS) return false;
    this.firedAt = now;
    return true;
  }
}

/** Concatenate from resultIndex: a two-word phrase can straddle a boundary. */
export function transcriptFrom(
  results: { transcript: string }[][] | { [i: number]: { [j: number]: { transcript: string } }; length: number },
  resultIndex: number,
): string {
  let text = "";
  const r = results as any;
  for (let i = resultIndex; i < r.length; i++) text += r[i][0].transcript;
  return text;
}
