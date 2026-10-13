// Monaco's popups under the HUD's CSS zoom (ED-1). The HUD zooms with CSS
// `zoom` on #root (lib/layout.ts), and a menu placed from a screen rect must be
// converted (`toCss`). Monaco's context view — the right-click menu and the
// hover tooltips on its buttons — lays itself out in viewport pixels from a
// size it measures *unzoomed*, then writes CSS lengths the zoom scales a
// second time: at 160% a right-click in the middle of the window opened the
// menu off its right edge, and at 70% one near the bottom flipped it up by
// its unzoomed height, 45 px short of the pointer (review of PR #43). So the
// popup is laid out again here, in viewport pixels with the size as drawn,
// and `monacoBundle.ts` converts the answer to CSS lengths.

/**
 * The product of every CSS zoom from `el` up, across shadow-root hosts (the
 * context menu lives in a shadow root inside the editor). `zoomOf` reads one
 * element's zoom; the computed style by default.
 */
export function effectiveZoom(
  el: Element | null,
  zoomOf: (el: Element) => string = (e) => getComputedStyle(e).zoom,
): number {
  let zoom = 1;
  let at: Element | null = el;
  while (at && at !== at.ownerDocument?.documentElement) {
    const z = parseFloat(zoomOf(at) || "1");
    if (Number.isFinite(z) && z > 0) zoom *= z;
    const root = at.getRootNode();
    at = at.parentElement ?? (root instanceof ShadowRoot ? root.host : null);
  }
  return zoom;
}

export interface Box { left: number; top: number; right: number; bottom: number }

/**
 * How far to move a popup (in its own CSS pixels, under `zoom`) so that its
 * box — in viewport pixels — sits inside a `width` × `height` viewport. A box
 * past the right or bottom edge comes back to it; one larger than the
 * viewport is pinned at the left or top, so its start is never off screen.
 */
export function keepOnScreen(box: Box, width: number, height: number, zoom: number): { dx: number; dy: number } {
  const z = zoom > 0 ? zoom : 1;
  let x = 0;
  let y = 0;
  if (box.right > width) x = width - box.right;
  if (box.left + x < 0) x = -box.left;
  if (box.bottom > height) y = height - box.bottom;
  if (box.top + y < 0) y = -box.top;
  return { dx: x / z, dy: y / z };
}

export interface Rect { left: number; top: number; width: number; height: number }

/**
 * Where a popup goes, in viewport pixels, the way Monaco's own layout places
 * it but with its size as drawn: below the anchor (above when the popup asks
 * for it), on the other side when the first does not fit and the other does;
 * its left edge on the anchor's left (its right edge on the anchor's right
 * when asked), flipped likewise; then kept on screen.
 */
export function placePopup(
  anchor: Rect,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  prefer: { above?: boolean; alignRight?: boolean } = {},
): { left: number; top: number } {
  const fitsY = (t: number) => t >= 0 && t + size.height <= viewport.height;
  const fitsX = (l: number) => l >= 0 && l + size.width <= viewport.width;
  const below = anchor.top + anchor.height;
  const above = anchor.top - size.height;
  const [y1, y2] = prefer.above ? [above, below] : [below, above];
  const top = fitsY(y1) || !fitsY(y2) ? y1 : y2;
  const leftAligned = anchor.left;
  const rightAligned = anchor.left + anchor.width - size.width;
  const [x1, x2] = prefer.alignRight ? [rightAligned, leftAligned] : [leftAligned, rightAligned];
  const left = fitsX(x1) || !fitsX(x2) ? x1 : x2;
  const d = keepOnScreen(
    { left, top, right: left + size.width, bottom: top + size.height }, viewport.width, viewport.height, 1,
  );
  return { left: left + d.dx, top: top + d.dy };
}
