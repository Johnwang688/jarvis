// Dev servers open by themselves (BR-2; amendment 1 §D.2, decisions B-2 and
// BD-7). When a server in a HUD terminal prints its local address, the daemon
// sends that terminal's attached socket one `{"type": "dev_url", "url"}` frame
// (BR-8a), and the window opens it in a Browser pane beside the terminal —
// without ever moving what the owner is using. This is the pure half: what to
// do with a URL, given the window as it is. BR-8b wires it into App.tsx and
// Terminal.tsx. Nothing here is wired in yet.
//
// `autoOpenPlan` answers one of:
//   - `split`: a new Browser pane after `source` — G2's `splitPane(ws, source,
//     id, spec, {focus: false})`, so neither DOM focus, `ws.focused` nor the
//     selected chat moves (W-6);
//   - `focused`: no room to split, so the focused pane switches to Browser —
//     unless it is busy: a chat with words in its box or a running turn, a
//     File pane with an unsaved edit, the very terminal that printed the URL,
//     or a Browser pane already showing a page (switching it would throw the
//     owner's page away);
//   - `offer`: then a chip in the terminal's bar instead, "Dev server at
//     localhost:5173 · Open in Browser";
//   - `wait`: a card is up — nothing in the layout changes under it. The URL
//     waits (the newest one only) and expires 30 s after it arrived;
//   - `skip`: the switch is off; the WP-E judge refuses the URL; that origin
//     was auto-opened (or offered) before in this page, or a drawn or hidden
//     Browser pane already shows it; or the rate limit — one per 10 s per
//     terminal, three a minute per window — is spent.
// It opens Direct: a loopback URL the judge allows always does (B-2).
//
// The state is the page's, in memory only (a ref in App): what was opened, when,
// and the one URL waiting. A reload forgets it, as it forgets every terminal
// socket's history; the daemon's sniffer reads live output only, so a reload's
// ring replay never sends a URL again.

import { judgePreviewUrl, originOf, type DaemonPorts } from "./preview";
import type { PaneNo, View } from "./workspace";

/** One open per terminal in this long. */
export const PER_TERMINAL_MS = 10_000;
/** At most WINDOW_MAX opens per window in this long. */
export const WINDOW_MS = 60_000;
export const WINDOW_MAX = 3;
/** A URL that arrived under a card waits this long, then is dropped. */
export const WAIT_MS = 30_000;
/** Origins remembered as already opened (the newest). */
export const OPENED_KEPT = 64;

export interface DevOpenState {
  /** Origins opened or offered in this page, oldest first. */
  opened: string[];
  /** Opens and offers within the last minute. */
  acts: { terminalId: string; at: number }[];
  /** The newest URL not acted on yet. */
  pending: { url: string; terminalId: string; at: number } | null;
}

export const DEV_OPEN_IDLE: DevOpenState = { opened: [], acts: [], pending: null };

/** A `dev_url` frame arrived on a terminal's socket: it is the one waiting now (the newest wins). */
export function devUrlArrived(s: DevOpenState, url: unknown, terminalId: string, now: number): DevOpenState {
  if (typeof url !== "string" || !url || url.length > 2048 || !terminalId) return s;
  return { ...s, pending: { url, terminalId, at: now } };
}

/** What a pane in the workspace shows, as far as this needs to know. */
export interface PaneShows {
  id: PaneNo;
  view: View;
  /** A Browser (Preview) pane's URL. */
  url?: string;
  /** A terminal pane's terminal. */
  terminalId?: string | null;
}

/** The window as it is when the plan is made. */
export interface DevWindow {
  /** "Open dev servers automatically" (Settings → Browser; `browser.json`). */
  enabled: boolean;
  /** An approval card is up. */
  blocked: boolean;
  /** The daemon's ports as `/status` reported them, and this window's own (the judge's input). */
  ports: Partial<DaemonPorts>;
  /** Every pane in the workspace: drawn, and hidden by ⊞. */
  panes: readonly PaneShows[];
  /** The panes drawn now, in reading order. */
  drawn: readonly PaneNo[];
  focused: PaneNo;
  /** G2's `splitVerdict(...).ok`: one more pane fits and an id is free. */
  canSplit: boolean;
  /** Panes that must not change: a chat with words in its box or a running turn, a File pane with an unsaved edit. */
  busy: readonly PaneNo[];
}

export type SkipWhy = "none" | "expired" | "off" | "refused" | "duplicate" | "rate";

export type DevOpenPlan =
  | { kind: "split"; source: PaneNo; url: string }
  | { kind: "focused"; pane: PaneNo; url: string }
  | { kind: "offer"; terminalId: string; url: string }
  | { kind: "wait" }
  | { kind: "skip"; why: SkipWhy };

/** The pane kind that shows a web page — "preview" until BR-3a renames it "browser". */
export function isBrowserView(view: string): boolean {
  return view === "preview" || view === "browser";
}

function recordAct(s: DevOpenState, origin: string, terminalId: string, now: number): DevOpenState {
  const opened = [...s.opened.filter((o) => o !== origin), origin].slice(-OPENED_KEPT);
  const acts = [...s.acts.filter((a) => now - a.at < WINDOW_MS), { terminalId, at: now }];
  return { opened, acts, pending: null };
}

/**
 * What to do with the URL waiting, if any, in the window as it is. The state
 * that comes back has the URL consumed — except under a card, where it waits.
 */
export function autoOpenPlan(s: DevOpenState, w: DevWindow, now: number): { state: DevOpenState; plan: DevOpenPlan } {
  const p = s.pending;
  const drop = (why: SkipWhy) => ({ state: { ...s, pending: null }, plan: { kind: "skip", why } as DevOpenPlan });
  if (!p) return { state: s, plan: { kind: "skip", why: "none" } };
  if (now - p.at > WAIT_MS || now < p.at) return drop("expired");
  if (!w.enabled) return drop("off");
  const verdict = judgePreviewUrl(p.url, w.ports);
  if (!verdict.ok) return drop("refused");
  if (w.blocked) return { state: s, plan: { kind: "wait" } };
  const origin = originOf(verdict.url);
  if (!origin) return drop("refused");
  const shown = w.panes.some((pane) => isBrowserView(pane.view) && !!pane.url && originOf(pane.url) === origin);
  if (shown || s.opened.includes(origin)) return drop("duplicate");
  const recent = s.acts.filter((a) => now - a.at < WINDOW_MS && a.at <= now);
  if (recent.length >= WINDOW_MAX) return drop("rate");
  if (recent.some((a) => a.terminalId === p.terminalId && now - a.at < PER_TERMINAL_MS)) return drop("rate");

  const state = recordAct(s, origin, p.terminalId, now);
  const url = verdict.url;
  const holder = w.panes.find((pane) => pane.view === "terminal" && pane.terminalId === p.terminalId
    && w.drawn.includes(pane.id));
  if (w.canSplit) {
    const source = holder?.id ?? (w.drawn.includes(w.focused) ? w.focused : w.drawn[0]);
    if (source !== undefined) return { state, plan: { kind: "split", source, url } };
  }
  const focused = w.panes.find((pane) => pane.id === w.focused);
  const free = !!focused && w.drawn.includes(focused.id) && !w.busy.includes(focused.id)
    && !(focused.view === "terminal" && focused.terminalId === p.terminalId)
    && !(isBrowserView(focused.view) && !!focused.url);
  if (free) return { state, plan: { kind: "focused", pane: focused!.id, url } };
  return { state, plan: { kind: "offer", terminalId: p.terminalId, url } };
}

/** The offer chip's words: "Dev server at localhost:5173". */
export function offerLabel(url: string): string {
  try {
    const u = new URL(url);
    return `Dev server at ${u.host}`;
  } catch {
    return "Dev server";
  }
}
