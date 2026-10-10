// The effort slider's rules (2026-10-10): the model popover's effort pills as
// one slider, Faster → Smarter, the way Claude's own app draws it. Pure, so
// the free tests grade it; components/EffortSlider.tsx only draws what this
// decides and sends what it says.
//
// The stops are exactly the levels the pills offered (`effortOptions`): the
// effective model's own ladder, or every word the provider takes when the
// ladder is unknown — so `ultra` is a stop on Codex and nowhere else, and a
// model with no reasoning control gets no slider at all. The pills' "default ·
// high" is not a stop of its own: it is the stop it names, marked "Default",
// and choosing that stop clears the effort to null (A4) — exactly what the pill
// did, so an effort change on a default thread still leaves `model` null and
// the request carries the keys it always did (`applyEffort` + `patchBody`).

import { defaultEffort, effective, effortsFor, effortWords, type Choice, type ThreadModels } from "./threadmodel";

/** A keyboard move commits this long after the last key, or at once on blur
 * or when the popover closes. A drag commits on release; a click at once. */
export const SETTLE_MS = 350;

export const HELP = "How long the model thinks before it answers. Higher is slower and smarter, and costs more.";

export interface SliderModel {
  /** Ascending, Faster → Smarter. "" is a default that names no level of its
   * own (`default · none`): it is then the first stop, so the slider can
   * still go back to it. */
  stops: string[];
  /** The stop the thread runs at with no effort chosen: always a stop. */
  defaultIndex: number;
  /** Where the thumb sits for what is stored. */
  index: number;
  /** The thread follows the default effort (`effort: null`). */
  followsDefault: boolean;
  /** A stored effort that is not a stop — kept from an earlier default model
   * this one lacks, or a word the provider does not take (`ultra` off Codex).
   * The thumb sits where it runs (`effective`); the header names both. */
  kept: string | null;
}

const NAMES: Record<string, string> = {
  "": "Default", none: "None", minimal: "Minimal", low: "Low", medium: "Medium",
  high: "High", xhigh: "Extra", max: "Max", ultra: "Ultra",
};

/** `xhigh` → `Extra`, as Claude's app says it; an unknown word capitalised. */
export function levelName(word: string): string {
  return NAMES[word] ?? word.charAt(0).toUpperCase() + word.slice(1);
}

/** The slider for this thread's provider and model, or null for none. */
export function sliderModel(tm: ThreadModels | null, c: Choice): SliderModel | null {
  const eff = effective(tm, c);
  const ladder = effortsFor(tm, c.provider, eff.model);
  if (ladder !== null && ladder.length === 0) return null;
  // Hardest first. Only the provider's own words are stops: the backend
  // refuses any other, and `effective` reads one as no choice — so `ultra`
  // can never be a stop off Codex, whatever a ladder says.
  const words = effortWords(c.provider);
  const levels = (ladder ?? words).filter((w, i, all) => words.includes(w) && all.indexOf(w) === i);
  if (!levels.length) return null;
  const stops = levels.sort((a, b) => words.indexOf(b) - words.indexOf(a));
  // What the "default · X" pill named, computed as `effortOptions` does.
  const dflt = c.model === null
    ? tm?.providers[c.provider]?.default_effort ?? null
    : defaultEffort(tm, c.provider, c.model);
  let defaultIndex = dflt ? stops.indexOf(dflt) : -1;
  if (defaultIndex < 0) {
    stops.unshift("");
    defaultIndex = 0;
  }
  const followsDefault = c.effort === null;
  let index = defaultIndex;
  let kept: string | null = null;
  if (c.effort !== null) {
    const at = c.effort === "" ? -1 : stops.indexOf(c.effort);
    if (at >= 0) {
      index = at;
    } else {
      kept = c.effort;
      const runs = eff.effort ? stops.indexOf(eff.effort) : -1;
      if (runs >= 0) index = runs;
    }
  }
  return { stops, defaultIndex, index, followsDefault, kept };
}

/** What choosing stop `i` stores: null for the default stop, else its level. */
export function effortAt(m: SliderModel, i: number): string | null {
  return i === m.defaultIndex ? null : m.stops[i] ?? null;
}

/** A position along the rail (0 at Faster, 1 at Smarter) → the nearest stop. */
export function snapIndex(fraction: number, n: number): number {
  if (n <= 1) return 0;
  const f = Number.isFinite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
  return Math.round(f * (n - 1));
}

/** Where stop `i` of `n` sits along the rail, 0..1. */
export function stopFraction(i: number, n: number): number {
  return n <= 1 ? 0.5 : i / (n - 1);
}

/**
 * The header and the screen reader's words. `at` is a stop being previewed
 * (a drag, a key not yet settled, a change on its way): it will clear to the
 * default if it is the default stop. With `at` null it is what is stored.
 */
export function describe(m: SliderModel, at: number | null = null): { name: string; note: string; aria: string } {
  if (at === null && m.kept !== null) {
    const runs = levelName(m.stops[m.index]);
    const name = levelName(m.kept);
    return { name, note: `runs as ${runs}`, aria: `${name}, runs as ${runs}` };
  }
  const i = at ?? m.index;
  const word = m.stops[i] ?? "";
  const name = levelName(word);
  const isDefault = at === null ? m.followsDefault : i === m.defaultIndex;
  if (!isDefault || word === "") return { name, note: "", aria: name };
  return { name, note: "default", aria: `${name}, default` };
}

/** One keyboard step from `i`, or null for a key the slider does not take. */
export function keyStep(key: string, i: number, n: number): number | null {
  switch (key) {
    case "ArrowRight":
    case "ArrowUp":
    case "PageUp":
      return Math.min(n - 1, i + 1);
    case "ArrowLeft":
    case "ArrowDown":
    case "PageDown":
      return Math.max(0, i - 1);
    case "Home":
      return 0;
    case "End":
      return n - 1;
    default:
      return null;
  }
}
