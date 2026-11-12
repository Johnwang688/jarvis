// The HUD's per-window appearance preferences (AG-2: motion and stirring; AG-3 adds the colours).
//
// Stored in `localStorage['jarvis.hud.appearance']` as JSON, read and written behind try/catch, a corrupt
// record falling back to the defaults field by field. A per-window convenience: nothing is sent to the
// daemon and nothing in it is trusted (every field is validated on read).
//
// The shape is `{motion, stir, activity}`, with room for AG-3's `accent` and `washes`: those two are kept
// verbatim (when they are strings) so a record written by a later HUD survives a round trip through this one,
// but nothing here reads or applies them.

import { readKey, writeKey } from "./storage";

export const APPEARANCE_KEY = "jarvis.hud.appearance";
/** Fired on `window` after a change is saved, so the aurora (and AG-3's colours) re-read at once. */
export const APPEARANCE_EVENT = "hud-appearance-change";

export const MOTIONS = ["fluid", "flow", "still"] as const;
export type Motion = (typeof MOTIONS)[number];

export interface Appearance {
  /** Fluid (default), the lighter Flow, or Still (one frame). */
  motion: Motion;
  /** The pointer stirs the fluid (default on). */
  stir: boolean;
  /** A chat ripples the fluid while it streams a reply (default on). */
  activity: boolean;
  /** AG-3: the accent, `#rrggbb`. Kept as stored; not applied here. */
  accent?: string;
  /** AG-3: the three wash colours. Kept as stored; not applied here. */
  washes?: string[];
}

export const DEFAULT_APPEARANCE: Appearance = { motion: "fluid", stir: true, activity: true };

/** The preferences from a stored string; any field that is missing or wrong is its default. Never throws. */
export function parseAppearance(raw: string | null): Appearance {
  const out: Appearance = { ...DEFAULT_APPEARANCE };
  if (!raw) return out;
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch {
    return out;
  }
  if (typeof v !== "object" || v === null || Array.isArray(v)) return out;
  const r = v as Record<string, unknown>;
  if (typeof r.motion === "string" && (MOTIONS as readonly string[]).includes(r.motion)) out.motion = r.motion as Motion;
  if (typeof r.stir === "boolean") out.stir = r.stir;
  if (typeof r.activity === "boolean") out.activity = r.activity;
  if (typeof r.accent === "string") out.accent = r.accent;
  if (Array.isArray(r.washes) && r.washes.every((w) => typeof w === "string")) out.washes = r.washes as string[];
  return out;
}

export function loadAppearance(storage?: Pick<Storage, "getItem">): Appearance {
  return parseAppearance(readKey(APPEARANCE_KEY, storage));
}

/** Saves `patch` over what is stored (so unknown-to-this-HUD fields are not lost) and tells the window. */
export function saveAppearance(patch: Partial<Appearance>, storage?: Pick<Storage, "getItem" | "setItem">): Appearance {
  const next = { ...loadAppearance(storage), ...patch };
  writeKey(APPEARANCE_KEY, JSON.stringify(next), storage);
  try {
    window.dispatchEvent(new Event(APPEARANCE_EVENT));
  } catch {
    /* no window: nothing is listening */
  }
  return next;
}
