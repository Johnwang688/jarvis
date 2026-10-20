# B-PREVIEW: can Chrome prove which frame a crop came from? (2026-10-20)

**Verdict: not with anything probed.** The Remote browser runs Google Chrome
155.0.8059.39 stable with `--headless`, launched by Playwright with
`channel="chrome"`. None of the mechanisms probed in it proves that a captured
image is the frame for a given DOM and layout state. B-PREVIEW stays blocked.
Cards keep **Preview unavailable** and draw no target outline. Nothing was built.
Several mechanisms were not probed; they are listed [below](#not-probed).

One mechanism did hold in these probes, but only in a different browser:
`chrome-headless-shell` with begin-frame control and virtual time paused. Using
it would reverse several recorded decisions, and it is not proven sound beyond
the cases tried. See [the one candidate](#the-one-candidate-and-why-it-is-not-recommended).
I recommend against it.

## The question

The card should show the pixels for the page state the action will act on, with
the target's outline in the same frame, or show nothing (amendment 1 §B.6 and
§B.9, BR-9c). A proof has to hold while the page changes at the same time, not
only on a quiet page. The page is untrusted and may try to get a false match.

The outline comes from a rect measured in the DOM, so the proof has to tie the
image to the same layout state the rect came from. A timestamp close in time is
not enough, and neither is a capture that "probably" came next.

## How it was measured

**Isolation.** Every probe ran an isolated browser:
- a temporary profile and HOME;
- a raw CDP pipe, so no debugging port was opened;
- `data:` URLs, or an in-process fixture served by request interception;
- each run under `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0`.

The probes never touched the live daemon, the owner's profiles or ports
8402/8403/8405.

**Launch.** The probes launched `/opt/google/chrome/chrome --headless` directly over
a CDP pipe, not through Playwright and not with the product's `launch_args`. Of the
product's command line, only `--headless` and `--force-device-scale-factor=1.5` were
copied; the proxy, the host-resolver rules and the blink settings were not. The
probes also added `--no-first-run --no-default-browser-check --mute-audio
--disable-extensions`, and opened pages in a fresh browser context created with
`Target.createBrowserContext`. A fresh profile's default context did not send
loopback navigations in this environment.

**Scripts and logs.** The scripts are in `~/.cache/jarvis-lead/b-preview-probe/`
(`p1`–`p8`, `cdp.py`, `rerun.sh`) and are not committed. They ran twice:
- **Run 1**, 2026-10-19 23:58 to 2026-10-20 00:26, was not archived.
- **Run 2**, 2026-10-20 00:35 to 00:42, with the same caps and fresh temporary
  profiles, saved its output in `~/.cache/jarvis-lead/b-preview-probe/logs/`
  (one log per probe).

The tables below give run 2 and add run 1 where a figure moved. Each figure comes
from one run of that probe on a loaded 24-core WSL2 machine, so read the figures
as illustrations, not rates.

**Method.** Each test page moves a red target between x = 100 and x = 500 CSS px.
A probe reads the target's position from each image's pixels and compares it with
the position the DOM reported.

## Results in Chrome 155

| # | Candidate | Result (run 2; run 1 where it differs) | Verdict |
|---|---|---|---|
| P1, P8 | `HeadlessExperimental.beginFrame` (the caller drives each frame and gets its screenshot) | The domain is missing: `'HeadlessExperimental.enable' wasn't found`. Same with each of the flag sets tried: none, `--enable-begin-frame-control`, `--enable-begin-frame-control --deterministic-mode`, and `--headless=old` (P8; 155 accepts that flag and still reports HeadlessChrome). `Target.createTarget` with `enableBeginFrameControl: true` returned success. The probe recorded only that, not any effect of the parameter. | **Unavailable** |
| P2 | `Page.screencastFrame` metadata and ack ordering | Seven metadata keys: `offsetTop, pageScaleFactor, deviceWidth, deviceHeight, scrollOffsetX, scrollOffsetY, timestamp`. There is no frame, commit or layout id, and `sessionId` is only the ack counter. The test compares the DOM state at each frame's `timestamp` with the state its pixels show. **Flip every 37 ms:** they matched in 155 of 157 frames (run 1: 161/161). **Every 11 ms:** **2 of 182** (run 1: 1 of 183). **Every 4 ms:** 13 of 22 (run 1: 5 of 22); only 24 frames arrived in 6 s. The timestamp came 5–72 ms after the change the frame shows (11–20 ms at 11 ms flips) and 3–9 ms before the frame reached the probe. At 11 ms the near-total mismatch is anti-correlation, which fits a systematic lag of more than one flip between the shown state and the timestamp. *Interpretation:* the timestamp is taken after the frame is produced, so it gives an upper bound on capture time and no lower bound. Why so few frames came at 4 ms was not investigated. | **No identity** |
| P3 | `Page.captureScreenshot` right after a rect measurement. Its options are `format, quality, clip, fromSurface, captureBeyondViewport, optimizeForSpeed`; none names a layout or paint state. | **Measure, then capture:** the image disagreed with the measurement in 19/30 captures with script flips, 26/30 with a CSS `steps()` animation and 15/30 with SVG SMIL (run 1: 17, 23, 7). **Bracket only** (an isolated-world MutationObserver and the rect read before and after; accept if nothing changed): no script-flip capture was accepted. For CSS, **26 accepted, all 26 false**; for SMIL, **21 accepted, 6 false** (run 1: 9 and 16 accepted, none false). | **Races; bracket defeated** |
| P3, P5, P6 | Freeze the page (`Page.setWebLifecycleState frozen`), pause animations (`Animation.setPlaybackRate 0`), then bracket as above | **CSS and SMIL pages:** 30/30 accepted, none false, in both runs. **Timers:** with no capture running, freezing stopped them (0 ticks in 500 ms, P5). **They ran while captures were in flight:** 98 ticks across 20 captures (run 1: 113), and 10 of the 20 images disagreed with the rect read just before (run 1: 14). **Cross-site iframe:** one in a separate renderer kept changing colour (P5). **Attack (P6):** the page moves the target by toggling a CSSOM rule (`insertRule`/`deleteRule`), which is not a DOM mutation. 28 of 60 captures passed the bracket, and **7 of those 28 showed the target away from the measured rect** (run 1: 24 of 29). **Virtual time pause instead** (`Emulation.setVirtualTimePolicy pause`; the only policy tried): captures timed out 30 of 30 on the script page and 30 of 30 on the CSS page, in both runs. They worked on the SMIL page. | **Defeated** |
| P4 | Tracing frame tokens | A trace links main-thread frames to presentation (`BeginMainThreadFrame.frameId`, `DrawFrame.frameSeqId`, `PipelineReporter.frame_sequence`), and a `console.timeStamp` made in a rAF lands in it. But headless Chrome emitted **no `Screenshot` trace events**, with or without a running screencast. Screencast frames and screenshots carry no `frame_sequence`. So the trace never names the pixels the probe holds. A trace would also be browser-wide and always on. | **No link to pixels** |
| P4 | `LayerTree` snapshots | `layerTreeDidChange` fires. `makeSnapshot` was tried on one layer, the latest one with `drawsContent: true`, and refused it: "Layer does not draw content". That CompositeAfterPaint is the cause is an inference. Even if snapshots worked, they would be per-layer recordings, not the composited frame. | **Refused (one layer tried)** |

**What the bracket misses.** It checks DOM mutations and the target's rect, and a
change can avoid both. The cases below are an open list; adding a check for one
leaves the rest open, so the bracket cannot become a positive proof.
- **Tested:** CSSOM rule toggles (P6, defeated it) and a cross-site iframe that
  kept changing under freeze (P5).
- **Not probed (reasoning only):** `adoptedStyleSheets`, closed shadow roots
  (MutationObserver does not cross them), canvas drawing, media, and resources
  that finish loading.
- **Covered by the bracket:** a viewport scroll moves the measured rect, so the
  bracket would catch one that persists. A scroll that reverts between the two
  reads was not probed.

**Background, not probed:** Playwright's own default launch adds
`--enable-features=CDPScreenshotNewSurface`. From its name, it makes a screenshot
wait for a fresh surface, which would make the image fresher but not tie it to a
state. Jarvis launches with `ignore_default_args=True` and does not use it.

### Not probed

- `Emulation.setScriptExecutionDisabled` and `Debugger.pause`, as other ways to
  stop the page's scripts around a capture.
- `Emulation.setVirtualTimePolicy` with a budget, or any policy other than `pause`.
- `DOMSnapshot.captureSnapshot`, as a layout record to compare with a capture.

Like freezing, these could at best stop some page activity. They would leave the
non-script channels above (CSSOM by other means, iframes, media, loads) and still
give no token linking an image to a layout.

## The one candidate, and why it is not recommended

**chrome-headless-shell** (Playwright's bundled build, HeadlessChrome 151.0.7922.34)
still has `HeadlessExperimental.beginFrame`, with `--enable-begin-frame-control`
and a target created with `enableBeginFrameControl`. P7 measured, called
`beginFrame{screenshot}`, then measured again, against the CSSOM toggle driven three
ways:

| driver | virtual time running (run 2; run 1) | virtual time paused (both runs) |
|---|---|---|
| timer (3 ms) | 19 accepted, 0 false; run 1: 23 accepted, **9 false** | 40 accepted, 0 false |
| MessageChannel loop | 18 accepted, **7 false**; run 1: 21, **10 false** | 40 accepted, 0 false |
| dedicated worker messages | 20 accepted, **10 false**; run 1: 28, **5 false** | 40 accepted, 0 false |

**What the paused column shows.** With virtual time paused, the page ran no tasks
between the measurements and the frame, so nothing changed. The 120 of 120 matches
mostly show that pausing stops these three drivers, and the check was exercised only
at the one position where each page froze. beginFrame alone, with virtual time
running, is defeated the same way captureScreenshot is.

**What adopting it would cost or change.** Each item is a recorded decision, a
measured property or labelled background:

1. **Another browser.** The Remote browser is Chrome stable through
   `channel="chrome"` (B-3), with:
   - persistent signed-in profiles and sync (BD-5);
   - the keyring password store (B-LOGIN);
   - Chrome's sign-in tab.

   The shell is a different, older Chromium build. Background, not probed: it is not
   branded Google Chrome, so Chrome sync and Google sign-in should not be expected.
   Each of those receipts would have to be redone.
2. **New launch flags.** `launch_args` is a curated, reviewed command line.
3. **Every live frame would be driven by the daemon.** In headless-shell under
   begin-frame control, the screencast sent **0 frames** in 2 s of page changes
   until the probe issued BeginFrames (3 frames for 10 BeginFrames, unacked).
   `Page.captureScreenshot` timed out (P1, both runs), and looks and captures use
   it. B-PERF's measured pipeline would have to be rebuilt.
4. **The owner's page freezes for the life of every card** (up to 120 s): with
   virtual time paused, no timers, messages, workers or animations run.
5. **A capture call made for the card.** This is the "No screenshot or capture call
   is created to manufacture that proof" rule in `docs/notes/browser.md` (BR9c). A
   beginFrame screenshot under a paused page is a different kind of evidence, but it
   is still a new capture path, so that rule would need an explicit owner decision.
6. **Not proven sound.** Untested here:
   - out-of-process iframes, which have their own renderer and virtual time;
   - media;
   - network loads that finish during the pause;
   - service workers;
   - whether DevTools' own work (the layout the measurement forces) matches the
     frame in every case.

**Recommendation:** do not pursue it. The preview is a convenience on a card whose
authority is the exact command text, which the owner already gets. Its cost would
be the Remote browser's identity and its live-stream design.

## What would unblock B-PREVIEW

Any one of these would. Each must be measured again against the attacks above
(P2's 11 ms flips, P3's `steps()` animation, P6's CSSOM toggle and a cross-site
iframe) before a plan is written:

- Chrome stable's new headless gets a frame-control API again (beginFrame or a
  successor) that works alongside the screencast.
- A CDP capture returns pixels together with a layout or commit token that a DOM
  call can be checked against, atomically in one lifecycle. For example,
  `DOMSnapshot.captureSnapshot` or `DOM.getBoxModel` would report the same
  generation.
- `Page.screencastFrame` metadata gains a commit or frame id that a rect
  measurement can be checked against.
- The owner decides to accept the headless-shell candidate's costs (items 1–6).
  That is a decision for the owner, not a package.

On each Chrome stable update, re-check `HeadlessExperimental.enable` in new
headless (P1) and the `screencastFrame` metadata keys (P2).
