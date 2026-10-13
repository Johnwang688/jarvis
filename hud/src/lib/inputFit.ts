// One queue for the boxes that actually changed. Several panes resizing
// together share each read/write phase instead of interleaving forced layout.
/** The box grows with what is typed, up to this many lines, then scrolls. */
export const MAX_LINES = 15;
/** …and never past this share of its pane's height: in a short pane (a split
 * of rows, a small window at a high zoom) fifteen lines pushed Send and the
 * chips out of the pane and squeezed the conversation to nothing (review of
 * PR #29). */
export const MAX_PANE_SHARE = 0.35;
/** …and never so tall that the conversation above it gets less than this
 * (zoomed px) or this share of the pane, whichever is more: at the pane's
 * minimum, with the bottom panel open, 35% still left the log 36px. */
export const MIN_LOG_PX = 96;
export const MIN_LOG_SHARE = 0.3;
/** But the box itself never stops short of this many lines: a pane that
 * cannot fit the conversation's minimum gets a two-line box that scrolls. */
export const MIN_LINES = 2;


const pending = new Map<HTMLTextAreaElement, () => void>();
let frame: number | null = null;

export function cancelInputFit(el: HTMLTextAreaElement) {
  pending.delete(el);
  if (!pending.size && frame !== null) { cancelAnimationFrame(frame); frame = null; }
}

export function requestInputFit(el: HTMLTextAreaElement, afterFit: () => void) {
  pending.set(el, afterFit);
  if (frame === null) frame = requestAnimationFrame(flush);
}

function flush() {
  frame = null;
  const entries = [...pending]; pending.clear();
  const measured = entries.flatMap(([el, afterFit]) => {
    const pane = el.closest<HTMLElement>(".wpane");
    const paneH = pane?.clientHeight ?? 0;
    // A queued box may have closed or become hidden before this frame.
    if (!el.isConnected || !el.clientWidth || (pane && !paneH)) return [];
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.45;
    const edge = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const lines = (n: number) => line * n + pad + edge;
    return [{ el, afterFit, paneH, log: pane?.querySelector<HTMLElement>('[data-testid="log"]'),
      edge, one: lines(1), two: lines(MIN_LINES), cap: Math.min(lines(MAX_LINES), pane ? paneH * MAX_PANE_SHARE : Infinity), want: 0 }];
  });
  // Fixed parts must be measured at one line. A previously overflowing long
  // box can distort the flex log's height and make the cap too large.
  for (const m of measured) m.el.style.height = `${m.one}px`;
  for (const m of measured) {
    if (m.log) {
      const fixed = m.paneH - m.log.offsetHeight - m.el.offsetHeight;
      m.cap = Math.min(m.cap, m.paneH - fixed - Math.max(MIN_LOG_PX, m.paneH * MIN_LOG_SHARE));
    }
    m.cap = Math.max(m.two, m.cap);
  }
  for (const m of measured) m.el.style.height = "auto";
  for (const m of measured) m.want = m.el.scrollHeight + m.edge;
  for (const m of measured) {
    m.el.style.height = `${Math.min(m.want, m.cap)}px`;
    m.el.style.overflowY = m.want > m.cap ? "auto" : "hidden";
  }
  for (const m of measured) m.afterFit();
}
