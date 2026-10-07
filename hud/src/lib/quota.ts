// The Usage panel's arithmetic. Two bars, never one: a provider's **reported**
// quota window and the **local** allowance are different measurements of
// different things, and a single bar that sometimes means one and sometimes
// the other is a number nobody can quote safely.
//
// Nothing here invents a quota: `barsFor` returns an empty list when the
// provider reported none, and the panel then says "not reported" — the
// contract's rule that quota is filled only from a provider's own report.

import type { ProviderUsage } from "../types";

export type Level = "ok" | "warn" | "high";

export function clampPct(p: number): number {
  if (!Number.isFinite(p)) return 0;
  return Math.max(0, Math.min(100, p));
}

/** Colour steps at 75% and 90%. Under 75 is green, from 75 yellow, from 90 red. */
export function level(p: number): Level {
  const v = clampPct(p);
  if (v >= 90) return "high";
  if (v >= 75) return "warn";
  return "ok";
}

/**
 * "resets in 3h 12m". A reset already past reads "resets now" rather than a
 * negative duration: a window that has rolled over is the ordinary case a
 * second before the next poll, not an error.
 */
export function resetsIn(iso: string | number | null | undefined, now: number = Date.now()): string {
  // Codex reports a unix timestamp; Claude reports an ISO string. A bare
  // number of seconds is not a millisecond clock.
  const at = typeof iso === "number"
    ? (iso < 1e12 ? iso * 1000 : iso)
    : Date.parse(String(iso || ""));
  if (!Number.isFinite(at)) return "reset time not reported";
  const ms = at - now;
  if (ms <= 0) return "resets now";
  const mins = Math.floor(ms / 60000);
  const days = Math.floor(mins / 1440);
  const hours = Math.floor((mins % 1440) / 60);
  const rest = mins % 60;
  if (days > 0) return `resets in ${days}d ${hours}h`;
  if (hours > 0) return `resets in ${hours}h ${rest}m`;
  if (mins > 0) return `resets in ${mins}m`;
  return "resets in under a minute";
}

export interface Bar {
  name: string;
  percent: number;
  level: Level;
  note: string;
}

/**
 * One named window, or null when the provider did not report it. A missing
 * window is not a bar at 0%: 0% is a real reading, and "unknown" must not
 * look like one.
 */
export function meterFor(p: ProviderUsage | undefined, name: string, now: number = Date.now()): Bar | null {
  const window = (p?.quota?.windows || []).find((w) => w.name === name);
  if (!window) return null;
  return {
    name: window.name,
    percent: clampPct(window.used_percent),
    level: level(window.used_percent),
    note: resetsIn(window.resets_at, now),
  };
}

/** The provider's own reported windows, in the order it reported them. */
export function barsFor(p: ProviderUsage, now: number = Date.now()): Bar[] {
  const windows = p?.quota?.windows || [];
  return windows.map((w) => ({
    name: w.name,
    percent: clampPct(w.used_percent),
    level: level(w.used_percent),
    note: resetsIn(w.resets_at, now),
  }));
}

/**
 * The local allowance — `today.work_tokens / allowance.work_tokens` — which is
 * a ledger of our own and not the provider's word for anything, so it is drawn
 * as a second, thinner bar and labelled. Null when there is no allowance for it
 * to be a fraction of.
 */
export function allowanceBar(p: ProviderUsage): (Bar & { used: number; total: number }) | null {
  const total = p?.allowance?.work_tokens;
  if (!total || total <= 0) return null;
  const used = p?.today?.work_tokens ?? 0;
  const percent = clampPct((used / total) * 100);
  return {
    name: "local allowance",
    percent,
    level: level(percent),
    note: `${used.toLocaleString()} / ${total.toLocaleString()} tok`,
    used,
    total,
  };
}
