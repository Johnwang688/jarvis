// Monaco's popups under the HUD's CSS zoom (ED-1). The HUD zooms with CSS
// `zoom` on #root (lib/layout.ts), and a menu placed from a screen rect must be
// converted (`toCss`). Monaco's context view — the right-click menu and the
// hover tooltips on its buttons — places itself in viewport pixels and then
// writes them as CSS lengths, which the zoom scales a second time: at 160% a
// right-click near the middle of the window opened the menu off its right
// edge. These are the pure halves of the correction `monacoBundle.ts` applies.

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
