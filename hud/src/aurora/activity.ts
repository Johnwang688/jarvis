// Chat activity stirs the aurora (AG-2, owner D-11): "a soft ripple from a chat pane while that chat is
// streaming a reply". The HUD calls `chatStreaming(pane)` for each streamed text chunk (App's SSE handler);
// this module rate-limits it and finds where on screen to ripple from, then hands a rect to the sink
// (the aurora controller). Wash colours only: the controller picks the wash channel, never amber or red.
//
// Limits, so several streaming panes stay subtle: one ripple per pane per PANE_MIN_MS, and at most
// TOTAL_MAX ripples in any TOTAL_WINDOW_MS across all panes.

export type Rect = { left: number; top: number; width: number; height: number };
export type RippleSink = (rect: Rect) => boolean;

export const PANE_MIN_MS = 900;
export const TOTAL_WINDOW_MS = 2000;
export const TOTAL_MAX = 3;

export interface ActivityHost {
  now?: () => number;
  /** The on-screen rect to ripple from for a pane, or null when it is not drawn. */
  rectOf?: (pane: number) => Rect | null;
}

/** The input bar (else the pane) of a pane's element: where a chat's reply is being worked on. */
export function paneRect(pane: number, doc: Document = document): Rect | null {
  const el = doc.querySelector<HTMLElement>(`[data-testid="pane-${pane}"]`);
  if (!el) return null;
  const input = el.querySelector<HTMLElement>('[data-testid="input"]');
  const r = (input ?? el).getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

export function createChatActivity(host: ActivityHost = {}) {
  const now = host.now ?? (() => performance.now());
  const rectOf = host.rectOf ?? ((p: number) => paneRect(p));
  let sink: RippleSink | null = null;
  const lastByPane = new Map<number, number>();
  let recent: number[] = [];
  return {
    setSink(s: RippleSink | null) {
      sink = s;
    },
    /** A chunk of a reply streamed into `pane`. Returns whether a ripple was made. */
    chatStreaming(pane: number): boolean {
      if (!sink) return false;
      const t = now();
      const last = lastByPane.get(pane);
      if (last !== undefined && t - last < PANE_MIN_MS) return false;
      recent = recent.filter((x) => t - x < TOTAL_WINDOW_MS);
      if (recent.length >= TOTAL_MAX) return false;
      const rect = rectOf(pane);
      if (!rect) return false;
      if (!sink(rect)) return false;
      lastByPane.set(pane, t);
      recent.push(t);
      return true;
    },
  };
}

/** The window's one chat-activity channel. */
export const chatActivity = createChatActivity();
export const chatStreaming = (pane: number): boolean => chatActivity.chatStreaming(pane);
