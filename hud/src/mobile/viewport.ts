// The keyboard and the message box (the owner's chat layout, 2026-10-28).
//
// iOS does not shrink the page when the keyboard opens: it shrinks the
// *visual* viewport and pans the page under it, so a box pinned to the
// bottom of the page ends up behind the keyboard. The app is sized to the
// visual viewport instead (`--vv-h`, `--vv-top` on the root, read by
// mobile.css), and while the keyboard is up the root says so
// (`data-keyboard="open"`): the tab bar steps aside and the message bar sits
// right on the keyboard. Values are set through the CSSOM, which C1's CSP
// allows (it refuses style attributes, not this). Where there is no
// `visualViewport`, nothing is set and the layout is the plain 100% one.
//
// A pinch-zoom shrinks the visual viewport too: while zoomed (scale above
// `ZOOM_MAX`) nothing is sized to it and the keyboard is never read as open,
// so zoom neither hides the tab bar nor shrinks the app (review of #331,
// LOW 3). A window resize or a rotation reads it all again (LOW 4).

/** What the app reads of the window (a fake in the tests). */
export interface ViewportWindow {
  innerHeight: number;
  visualViewport?: {
    height: number;
    offsetTop: number;
    scale?: number;
    addEventListener(type: "resize" | "scroll", fn: () => void): void;
  } | null;
  addEventListener?(type: "resize" | "orientationchange", fn: () => void): void;
}

/** The keyboard is taken to be up when the visible part is this much shorter than the window (px). */
export const KEYBOARD_PX = 120;
/** Above this scale the page is pinch-zoomed, not under a keyboard. */
export const ZOOM_MAX = 1.01;

export function followViewport(win: ViewportWindow, root: HTMLElement): () => void {
  const vv = win.visualViewport;
  const update = () => {
    if (!vv) return;
    if ((vv.scale ?? 1) > ZOOM_MAX) {
      root.style.removeProperty("--vv-h");
      root.style.removeProperty("--vv-top");
      root.dataset.keyboard = "closed";
      return;
    }
    root.style.setProperty("--vv-h", `${Math.round(vv.height)}px`);
    root.style.setProperty("--vv-top", `${Math.round(vv.offsetTop)}px`);
    root.dataset.keyboard = win.innerHeight - vv.height > KEYBOARD_PX ? "open" : "closed";
  };
  if (vv) {
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    win.addEventListener?.("resize", update);
    win.addEventListener?.("orientationchange", update);
    update();
  }
  return update;
}
