// Automatic Direct placement (amendment 2 E.1-E.4). Pure decisions only:
// claims and timeouts live in state/devservers; the App supplies a fresh
// workspace snapshot immediately before committing one of these plans.
import { API_PORT, FACE_PORT, WORKSHOP_PORT, judgeBrowserUrl, originOf, type DaemonPorts } from "./browser";
import { BROWSER_TABS_MAX, type PaneNo, type View } from "./workspace";

export const PER_SOURCE_MS = 10_000;
export const WINDOW_MS = 60_000;
export const WINDOW_MAX = 3;
export const WAIT_MS = 10 * 60_000;
export const WAIT_MAX = 4;
export const OPENED_KEPT = 64;
export const POINTER_QUIET_MS = 150;
export const IN_USE_MS = 10_000;

export interface DevSource { kind: "thread" | "task" | "terminal"; id: string }
export interface DevEntry {
  id: string;
  url: string;
  source: DevSource;
  /** Local receipt time, never the daemon's wall clock. */
  at: number;
}
export interface DevOpenState {
  /** Entry IDs, not origins: a restarted server gets a new lifecycle. */
  opened: readonly string[];
  acts: readonly { source: string; at: number }[];
}
export const DEV_OPEN_IDLE: DevOpenState = { opened: [], acts: [] };
export const sourceKey = (source: DevSource): string => source.kind + ":" + source.id;

export interface PaneShows {
  id: PaneNo;
  view: View;
  /** Includes retained Browser tabs even while this pane shows another view. */
  urls: readonly string[];
  tabCount: number;
  source?: DevSource;
  usedAt?: number;
  /** Private panes are Remote-only and never an automatic Direct destination. */
  browserProfile?: string;
  /** Fresh ticketed state only, never persisted with a pane. */
  driving?: boolean;
}
export interface DevWindow {
  enabled: boolean;
  blocked: boolean;
  covered: boolean;
  ports: Partial<DaemonPorts>;
  controlPorts?: readonly number[];
  panes: readonly PaneShows[];
  drawn: readonly PaneNo[];
  focused: PaneNo;
  /** Pane containing activeElement, including a focused iframe. */
  activePane?: PaneNo;
  canSplit: boolean;
  pointerDown: boolean;
  pointerReleasedAt: number;
}
export type SkipWhy = "expired" | "off" | "refused" | "acted" | "duplicate" | "rate";
export type DevOpenPlan =
  | { kind: "split"; source: PaneNo; url: string }
  | { kind: "tab"; pane: PaneNo; url: string; activate: boolean }
  | { kind: "prompt"; url: string }
  | { kind: "wait"; why: "card" | "covered" | "pointer" }
  | { kind: "skip"; why: SkipWhy };

export const CONTROL_PORTS: readonly number[] = [FACE_PORT, 8404, API_PORT, 8406];

/** A second trust boundary after owner-only registry reads. No token query travels. */
export function devUrl(url: string, ports: Partial<DaemonPorts>, controlPorts: readonly number[] = []): string | null {
  const verdict = judgeBrowserUrl(url, ports);
  if (!verdict.ok) return null;
  const u = new URL(verdict.url);
  // Automatic opening is loopback-only. The Direct judge also accepts some
  // non-loopback addresses for explicit owner entry; that is not this path.
  if (u.hostname !== "localhost" && u.hostname !== "127.0.0.1") return null;
  const refused = new Set([...CONTROL_PORTS, ...controlPorts.filter(Number.isInteger),
    ports.hud, ports.api, ports.workshop ?? WORKSHOP_PORT, ports.self]);
  if (refused.has(Number(u.port || (u.protocol === "https:" ? 443 : 80)))) return null;
  return u.origin + u.pathname;
}

function record(s: DevOpenState, entry: DevEntry, now: number): DevOpenState {
  return {
    opened: [...s.opened.filter(id => id !== entry.id), entry.id].slice(-OPENED_KEPT),
    acts: [...s.acts.filter(a => a.at <= now && now - a.at < WINDOW_MS),
      { source: sourceKey(entry.source), at: now }],
  };
}

/** Return prospective state; commit it only when the synchronous action succeeds. */
export function autoOpenPlan(entry: DevEntry, s: DevOpenState, w: DevWindow, now: number):
    { state: DevOpenState; plan: DevOpenPlan } {
  const unchanged = (plan: DevOpenPlan) => ({ state: s, plan });
  if (!w.enabled) return unchanged({ kind: "skip", why: "off" });
  if (now < entry.at || now - entry.at >= WAIT_MS) return unchanged({ kind: "skip", why: "expired" });
  const url = devUrl(entry.url, w.ports, w.controlPorts);
  if (!url) return unchanged({ kind: "skip", why: "refused" });
  if (s.opened.includes(entry.id)) return unchanged({ kind: "skip", why: "acted" });
  if (w.blocked) return unchanged({ kind: "wait", why: "card" });
  if (w.covered) return unchanged({ kind: "wait", why: "covered" });
  const origin = originOf(url);
  if (w.panes.some(p => p.urls.some(u => originOf(u) === origin))) {
    return unchanged({ kind: "skip", why: "duplicate" });
  }
  const recent = s.acts.filter(a => a.at <= now && now - a.at < WINDOW_MS);
  if (recent.length >= WINDOW_MAX || recent.some(a => a.source === sourceKey(entry.source) && now - a.at < PER_SOURCE_MS)) {
    return unchanged({ kind: "skip", why: "rate" });
  }
  const holder = w.panes.find(p => w.drawn.includes(p.id) && p.source && sourceKey(p.source) === sourceKey(entry.source));
  const source = holder?.id ?? (w.drawn.includes(w.focused) ? w.focused : w.drawn[0]);
  if (w.canSplit && source !== undefined) {
    if (w.pointerDown || now - w.pointerReleasedAt < POINTER_QUIET_MS) return unchanged({ kind: "wait", why: "pointer" });
    return { state: record(s, entry, now), plan: { kind: "split", source, url } };
  }
  const browsers = w.panes.filter(p => p.view === "browser" && p.browserProfile !== "private" && w.drawn.includes(p.id) && p.tabCount < BROWSER_TABS_MAX);
  const slot = (id: PaneNo) => w.drawn.indexOf(id);
  const sourceSlot = holder ? slot(holder.id) : -1;
  browsers.sort((a, b) => sourceSlot < 0
    ? (b.usedAt ?? -Infinity) - (a.usedAt ?? -Infinity) || slot(a.id) - slot(b.id)
    : Math.abs(slot(a.id) - sourceSlot) - Math.abs(slot(b.id) - sourceSlot) || slot(b.id) - slot(a.id));
  const target = browsers[0];
  if (target) {
    const inUse = target.driving === true || target.id === w.focused || target.id === w.activePane
      || target.usedAt !== undefined && now >= target.usedAt && now - target.usedAt < IN_USE_MS;
    return { state: record(s, entry, now), plan: { kind: "tab", pane: target.id, url, activate: !inUse } };
  }
  return { state: record(s, entry, now), plan: { kind: "prompt", url } };
}

/** Bounded, stable FIFO for deferred claims. Eviction never dismisses the server. */
export function enqueueDev<T extends { id: string }>(pending: readonly T[], entry: T): T[] {
  if (pending.some(p => p.id === entry.id)) return pending.slice();
  return [...pending, entry].slice(-WAIT_MAX);
}

export function offerLabel(url: string): string {
  try { return "Dev server at " + new URL(url).host; }
  catch { return "Dev server"; }
}
