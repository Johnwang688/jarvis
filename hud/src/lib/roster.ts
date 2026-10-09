// The fast path's model roster as the Model picker draws it (2026-10-08).
//
// Two defaults, and the picker must never blur them:
//
// - **the default** — what every default-following fast-path thread runs on
//   now. It is the model chosen in this window ("Set as default", stored in
//   models.json, surviving restarts and beating the env var) or, when nothing
//   is chosen, the config default;
// - **the config default** — `JARVIS_ORCHESTRATOR`, used only while nothing
//   is chosen here. "Reset to config default" hands the choice back to it.
//
// Pure, so the free tests grade it. Every string here is text for React to
// render as text: model ids come off the network and this window draws
// authorization cards.

import type { ModelRow } from "../types";

/** The env var the config default comes from. */
export const CONFIG_ENV = "JARVIS_ORCHESTRATOR";

/** `GET /models` (`models.describe()`). */
export interface RosterView {
  models: ModelRow[];
  /** The model chosen in the HUD, or "" when following the config default. */
  selected?: string | null;
  /** The config default (`JARVIS_ORCHESTRATOR`). */
  default?: string;
  /** What the loop runs on now: `selected` if any, else `default`. */
  current?: string;
  default_source?: "hud" | "config";
}

/** The model every default-following thread runs on now. */
export function effectiveDefault(r: RosterView | null): string {
  if (!r) return "";
  return r.current || r.selected || r.default || "";
}

/** True when a model was chosen in the HUD (so Reset has something to undo). */
export function chosenHere(r: RosterView | null): boolean {
  return !!r?.selected;
}

/** The badges one roster row carries: `default` on the model that answers,
 * `config` on the env model. */
export function rowBadges(r: RosterView | null, id: string): string[] {
  const out: string[] = [];
  if (id === effectiveDefault(r)) out.push("default");
  if (r?.default && id === r.default) out.push("config");
  return out;
}

/** The config-default line under the picker's title. */
export function configDefaultLine(r: RosterView | null): string {
  const id = r?.default || "?";
  const listed = !!r?.models?.some((m) => m.id === r?.default);
  const use = chosenHere(r)
    ? "used only when nothing is chosen here"
    : "in use: nothing is chosen here";
  return `config default (${CONFIG_ENV}): ${id} · ${use}${listed ? "" : " · unpinned"}`;
}

/** The ids on the roster, for the catalogue's Pin / Unpin. */
export function rosterIds(r: { models?: ModelRow[] } | null | undefined): string[] {
  return (r?.models || []).map((m) => m.id);
}

/** A refusal, said in the backend's own words after what was being tried.
 * Never empty: an action that failed must never read as one that worked. */
export function refusal(what: string, err: unknown): string {
  const message = (err as { message?: unknown } | null)?.message;
  const text = typeof message === "string" && message.trim() ? message.trim() : "the request failed";
  return `Could not ${what}: ${text}`;
}
