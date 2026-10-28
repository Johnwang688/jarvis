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

/** What the app reads of the window (a fake in the tests). */
export interface ViewportWindow {
  innerHeight: number;
  visualViewport?: {
    height: number;
    offsetTop: number;
    addEventListener(type: "resize" | "scroll", fn: () => void): void;
  } | null;
}

/** The keyboard is taken to be up when the visible part is this much shorter than the window (px). */
export const KEYBOARD_PX = 120;

export function followViewport(win: ViewportWindow, root: HTMLElement): () => void {
  const vv = win.visualViewport;
  const update = () => {
    if (!vv) return;
    root.style.setProperty("--vv-h", `${Math.round(vv.height)}px`);
    root.style.setProperty("--vv-top", `${Math.round(vv.offsetTop)}px`);
    root.dataset.keyboard = win.innerHeight - vv.height > KEYBOARD_PX ? "open" : "closed";
  };
  if (vv) {
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    update();
  }
  return update;
}
