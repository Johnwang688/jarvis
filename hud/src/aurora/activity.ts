// Chat activity stirs the aurora (AG-2, owner D-11): "a soft ripple from a chat pane while that chat is
// streaming a reply". The HUD calls `chatStreaming(pane)` for each streamed text chunk (App's SSE handler);
// this module rate-limits it and finds where on screen to ripple from, then hands a rect to the sink
// (the aurora controller). Wash colours only: the controller picks the wash channel, never amber or red.
//
// Limits, so several streaming panes stay subtle: one ripple per pane per PANE_MIN_MS, and at most
// TOTAL_MAX ripples in any TOTAL_WINDOW_MS across all panes.

export type Rect = { left: number; top: number; width: number; height: number };
export type RippleSink = (rect: Rect) => boolean;
/** Whether a ripple could be drawn at all right now (setting on, fluid running): O(1), reads no layout. */
export type RippleEligible = () => boolean;

export const PANE_MIN_MS = 900;
export const TOTAL_WINDOW_MS = 2000;
export const TOTAL_MAX = 3;

export interface ActivityHost {
  now?: () => number;
  /** The on-screen rect to ripple from for a pane, or null when it is not drawn. */
  rectOf?: (pane: number) => Rect | null;
}

/**
 * The input bar of a pane's element: where a chat's reply is being worked on. Null unless that chat is
 * what the pane shows (its message box exists), the pane is drawn and not inert (also not under an
 * approval card or a modal), and the box is on screen with a size.
 */
export function paneRect(pane: number, doc: Document = document): Rect | null {
  const el = doc.querySelector<HTMLElement>(`[data-testid="pane-${pane}"]`);
  if (!el) return null;
  const input = el.querySelector<HTMLElement>('[data-testid="input"]');
  if (!input) return null; // the pane shows another view (Tasks, a file, ...), not the chat
  if (el.closest('[inert]')) return null;
  const view = doc.defaultView;
  const r = input.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null; // display:none or collapsed
  const vw = view?.innerWidth ?? 0;
  const vh = view?.innerHeight ?? 0;
  if (vw > 0 && vh > 0 && (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh)) return null;
  // Something covering it (a card's veil, a modal) is what a point there hits, not the pane.
  if (typeof doc.elementFromPoint === 'function' && vw > 0) {
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
    const hit = doc.elementFromPoint(cx, cy);
    if (hit && !el.contains(hit)) return null;
  }
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

export function createChatActivity(host: ActivityHost = {}) {
  const now = host.now ?? (() => performance.now());
  const rectOf = host.rectOf ?? ((p: number) => paneRect(p));
  let sink: RippleSink | null = null;
  let eligible: RippleEligible | null = null;
  const lastByPane = new Map<number, number>();
  let recent: number[] = [];
  return {
    setSink(s: RippleSink | null, canRipple: RippleEligible | null = null) {
      sink = s;
      eligible = canRipple;
    },
    /** A chunk of a reply streamed into `pane`. Returns whether a ripple was made. */
    chatStreaming(pane: number): boolean {
      if (!sink) return false;
      // Cheapest checks first, and no DOM read until a ripple could really be drawn.
      if (eligible && !eligible()) return false;
      const t = now();
      const last = lastByPane.get(pane);
      if (last !== undefined && t - last < PANE_MIN_MS) return false;
      recent = recent.filter((x) => t - x < TOTAL_WINDOW_MS);
      if (recent.length >= TOTAL_MAX) return false;
      // An attempt that finds nothing to ripple from (pane not shown, covered) is throttled like a
      // ripple, so a stream into a hidden pane costs one measure per PANE_MIN_MS, not one per chunk.
      lastByPane.set(pane, t);
      const rect = rectOf(pane);
      if (!rect) return false;
      if (!sink(rect)) return false;
      recent.push(t);
      return true;
    },
  };
}

/** The window's one chat-activity channel. */
export const chatActivity = createChatActivity();
export const chatStreaming = (pane: number): boolean => chatActivity.chatStreaming(pane);
