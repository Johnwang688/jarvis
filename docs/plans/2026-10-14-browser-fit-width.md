# Remote Browser: fit wide pages to a narrow pane (2026-10-14)

## Problem

The owner reported that Remote pages are cut off on the right in narrow panes
(4- and 8-pane layouts, or after dragging a divider). Spotify was the example.

What was measured before planning:

- **The resize path works.** A real headless Chrome behind the daemon
  (`tests/v2/browser_routes_check.BrowserStream` harness) followed every
  `{t:"resize"}`: `innerWidth`, `devicePixelRatio` and the screencast frame
  size all matched each message, and a burst of 22 resizes ended on the last
  one.
- **The page is wider than its viewport.** open.spotify.com sets
  `body { min-width: 800px }`. At a 730 px viewport its `scrollWidth` is 812;
  at 500 px it is still 812. A desktop browser would show a horizontal
  scrollbar. Headless Chrome hides scrollbars, so the overflow is invisible
  and the right side is simply cut off. Any site with a min-width does the
  same.

## Fix: lay the page out at its content width, draw it scaled to the pane

The HUD already handles a frame wider than its canvas: `placeFrame` scales it
down to fit and never stretches it past 1:1, and `clientToPage` maps input
through that scale. So the fix lives in the daemon (`jarvis/v2/browser/host.py`).

1. **Fit.** The tab keeps the window's requested viewport
   (`stream.viewport`, its pane size W×H at scale S) and an effective one.
   When the page's content width C is greater than W, the effective viewport
   becomes C × round(H·C/W) at scale S·W/C. The aspect stays the pane's, and
   the frame keeps the same device pixels as the canvas, so it costs no extra
   bandwidth and is no blurrier than the canvas allows. The window draws it at
   scale W/C, and the whole page fits.
2. **Measure without page JS.** Use CDP `Page.getLayoutMetrics`
   (`cssContentSize.width`). A page cannot run code in the measurement. A
   page that lies about its size can only make its own picture smaller.
3. **Bounds.** Never wider than 3×W (scale ≥ 1/3) or `VIEW_MAX` (4096), and
   within the device-pixel cap. Ignore overflow of 1% or less (Spotify
   overflows by 12 px even at 1400). Widen only toward the measured width: a
   `width: 100vw + margin` page that keeps growing stops at the cap instead
   of looping.
4. **When to measure.**
   - On every resize from the window: reset to W, then measure and fit.
   - On main-frame navigation, including same-document navigation: reset,
     then fit.
   - On screencast frames, throttled to at most 2 per second. Frames arrive
     only on repaint, so an idle page costs nothing. This catches SPAs that
     set their min-width after hydration (Spotify does). Between resets a
     frame check may only widen. Narrowing waits for the next reset, so the
     layout cannot oscillate.
5. **Everything downstream reads the effective viewport.** That means
   `_view()` (screencast `maxWidth`/`maxHeight` and the restart-on-size
   check), anything else that reads `stream.viewport` for page geometry (owner
   tools, look capture, agent snapshots: audit them), and the frame metadata
   the HUD already uses. Coordinates the window sends stay page CSS px of the
   frame's viewport, which is what `clientToPage` already produces.
6. **No new settings, no new wire messages.** Frame metadata already carries
   the effective size. The stream's `resize` validation is unchanged.

Not done (considered):

- **Showing scrollbars** (dropping headless `--hide-scrollbars`): the owner
  asked for contents to stay within the pane, not to scroll sideways.
- **`mobile: true` emulation** (Chrome's own zoom-out-to-fit): it changes the
  UA's mobile behaviors (viewport meta, text autosizing, touch) for every
  page.

## Tests

- **New real-Chrome check `tests/v2/browser_fit_check.py`**, using the
  `BrowserStream` harness with a fixture server and no external network:
  - A `body{min-width:800px}` page in a 500×600 pane gives frames of
    800×960, page `innerWidth` 800, DPR scaled by 500/800.
  - A page with no min-width is not widened.
  - Resizing back to a wide pane resets to the pane size.
  - A `100vw + 50px` page stops at the 3× cap.
  - A min-width added after load (simulated hydration) is fitted on the next
    frames.
  - Navigating to a page that fits resets the viewport.
  - Mutation evidence: with the fit disabled, the check fails.
- The existing suites still pass: `browser_routes_check`,
  `browser_owner_tools_chrome_check`, and the HUD's `hud_v2_remote_check` and
  `remote.test.ts`.
- If the input-mapping coverage in `remote.test.ts` does not already include
  a frame wider than the canvas, add a `clientToPage` test for one.

## Docs

- Add a short paragraph to the Browser notes (`docs/notes/browser.md`) on the
  fit, its bounds and its triggers.
