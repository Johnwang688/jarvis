# B-PREVIEW: can Chrome prove which frame a crop came from? (2026-10-19)

**Verdict: no.** The Chrome the Remote browser runs (Google Chrome 155.0.8059.39
stable, `--headless`, launched by Playwright with `channel="chrome"`) has no way
to prove that a captured image is the frame for a given DOM and layout state.
B-PREVIEW stays blocked. Cards keep **Preview unavailable** and draw no target
outline. Nothing was built.

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

## What was measured

All probes ran against isolated browsers: a temporary profile and HOME, a raw CDP
pipe (no debugging port), `data:` URLs or an in-process fixture with request
interception, each run under `systemd-run --user --scope -p MemoryMax=2G`. They
never touched the live daemon, the owner's profiles or ports 8402/8403/8405. All
runs used `--force-device-scale-factor=1.5`, like the Remote browser. The scripts
are in `~/.cache/jarvis-lead/b-preview-probe/` (`p1`–`p8`) and are not committed.

The test pages move a red target between x = 100 and x = 500 CSS px. A probe reads
the target's position from each image's pixels and compares it with the position
the DOM reported.

| # | Candidate | Chrome 155 result | Verdict |
|---|---|---|---|
| P1 | `HeadlessExperimental.beginFrame` (the caller drives each frame and gets its screenshot) | The domain does not exist: `'HeadlessExperimental.enable' wasn't found`. Same result with `--enable-begin-frame-control`, `--deterministic-mode`, and `--headless=old` (P8; 155 accepts that flag but runs new headless). `Target.createTarget{enableBeginFrameControl}` is accepted and does nothing. | **Unavailable** |
| P2 | `Page.screencastFrame` metadata and ack ordering | The metadata has seven keys: `offsetTop, pageScaleFactor, deviceWidth, deviceHeight, scrollOffsetX, scrollOffsetY, timestamp`. No frame, commit or layout id. The `sessionId` is only the ack counter. The flip test: with a flip every 37 ms, the DOM state at the frame's `timestamp` matched the pixels in 161/161 frames. With a flip every 11 ms it matched in **1 of 183** frames. With a flip every 4 ms, 5 of 22 (the page starved the stream). The timestamp falls 5–23 ms after the change the frame shows and about 3 ms before the frame reaches us, so it is a time stamped after processing, not a capture-state token. It gives an upper bound and no lower bound. | **No identity** |
| P3 | `Page.captureScreenshot` after a rect measurement (no option ties the image to a layout or paint state: `format, quality, clip, fromSurface, captureBeyondViewport, optimizeForSpeed`) | Measure, then capture, with nothing else: the image disagreed with the measurement in 17/30 captures (script flips), 23/30 (a CSS `steps()` animation) and 7/30 (SVG SMIL). | **Races** |
| P3/P5/P6 | Make it hold: freeze the page (`Page.setWebLifecycleState frozen`), pause animations (`Animation.setPlaybackRate 0`), then bracket the capture with an isolated-world MutationObserver and the rect read before and after, accepting only if nothing changed | Freezing stops the page's timers while idle (0 ticks in 500 ms), but **they run while a capture is in flight**: 113 timer ticks during 20 captures (P5). A cross-site iframe (a separate renderer) kept animating (P5). Attack (P6): the page moves the target by toggling a CSSOM rule (`insertRule`/`deleteRule`), which is not a DOM mutation. **29 of 60 captures passed the bracket and 24 of those 29 showed the target away from the measured rect.** Virtual time pause (`Emulation.setVirtualTimePolicy pause`) instead made captures hang on two of three pages (30/30 timeouts). | **Defeated** |
| P4 | Tracing frame tokens | A trace links main-thread frames to presentation (`BeginMainThreadFrame.frameId`, `DrawFrame.frameSeqId`, `PipelineReporter.frame_sequence`), and a `console.timeStamp` made in a rAF lands in it. But headless Chrome emits **no `Screenshot` trace events**, with or without a running screencast. Screencast frames and screenshots carry no `frame_sequence`. So the trace never names the pixels we hold. A trace would also be browser-wide and always on. | **No link to pixels** |
| P4 | `LayerTree` snapshots | `layerTreeDidChange` fires, but `makeSnapshot` refuses every layer, including those with `drawsContent: true`: "Layer does not draw content" (CompositeAfterPaint). Even when they worked, these were per-layer recordings, not the composited frame. | **Unavailable** |

Two more notes:

- Playwright's own default launch adds `--enable-features=CDPScreenshotNewSurface`,
  so a screenshot waits for a fresh surface. That makes it fresher, not tied to a
  state. Jarvis launches with `ignore_default_args=True` and does not use it.
- The bracket's gaps form an open list, not a fixed set: CSSOM, `adoptedStyleSheets`,
  closed shadow roots (MutationObserver does not cross them), canvas, scroll
  position, media, out-of-process iframes, and resources that finish loading.
  Adding a check for one leaves the rest open, so this approach cannot become a
  positive proof.

## The one candidate, and why it is not recommended

**chrome-headless-shell** (Playwright's bundled build, HeadlessChrome 151.0.7922.34)
still has `HeadlessExperimental.beginFrame`, with `--enable-begin-frame-control`
and a target created with `enableBeginFrameControl`. P7 used it as measure, then
`beginFrame{screenshot}`, then measure, against the CSSOM toggle driven three ways:

| driver | virtual time running | virtual time paused |
|---|---|---|
| timer (3 ms) | 23 accepted, **9 false** | 40 accepted, 0 false |
| MessageChannel loop | 21 accepted, **10 false** | 40 accepted, 0 false |
| dedicated worker messages | 28 accepted, **5 false** | 40 accepted, 0 false |

With virtual time paused, the page ran no tasks between the measurements and the
frame, so the frame showed the measured state 120 of 120 times. Beginframe on its
own (virtual time running) is defeated the same way captureScreenshot is.

What adopting it would cost or change. Each item is a recorded decision or a
measured property:

1. **Another browser.** The Remote browser is Chrome stable through
   `channel="chrome"` (B-3), with persistent signed-in profiles, sync (BD-5), the
   keyring password store (B-LOGIN) and Chrome's sign-in tab. The shell is a
   different, older Chromium build with no Chrome sync or sign-in. Every one of
   those receipts would have to be redone, and some cannot carry over.
2. **New launch flags.** `launch_args` is a curated, reviewed command line.
3. **Every live frame would be driven by the daemon.** Under begin-frame control,
   the screencast sent **0 frames** in 2 s of page changes until we issued
   BeginFrames (P1). Separately, `Page.captureScreenshot` timed out (P1), and looks
   and captures use it. B-PERF's measured pipeline would have to be rebuilt.
4. **The owner's page freezes for the life of every card** (up to 120 s): virtual
   time paused means no timers, messages, workers or animations.
5. **A capture call made for the card.** This is the "No screenshot or capture
   call is created to manufacture that proof" rule in `docs/notes/browser.md`
   (BR9c). A beginFrame screenshot taken under a paused page is a different kind
   of evidence, but it is still a new capture path, so that rule would need an
   explicit owner decision.
6. **Not proven sound.** Untested here: out-of-process iframes (their own renderer
   and virtual time), media, network loads that finish during the pause, service
   workers, and whether DevTools' own work (layout forced by the measurement)
   matches the frame in every case. 120 of 120 against three drivers is evidence,
   not a proof.

**Recommendation:** do not pursue it. The preview is a convenience on a card whose
authority is the exact command text, and that text the owner already gets. The
cost is the Remote browser's identity and its live-stream design.

## What would unblock B-PREVIEW

Any one of these, each to be measured again against the attacks above (P2's 11 ms
flips, P6's CSSOM toggle, a cross-site iframe) before a plan is written:

- Chrome stable's new headless gets a frame-control API again (beginFrame or a
  successor) that works alongside the screencast.
- A CDP capture that returns pixels together with a layout or commit token that a
  DOM call can be checked against (for example, `DOMSnapshot.captureSnapshot` or
  `DOM.getBoxModel` reporting the same generation), atomically in one lifecycle.
- `Page.screencastFrame` metadata gains a commit or frame id that a rect
  measurement can be checked against.
- An owner decision to accept the headless-shell candidate's costs (items 1–6).
  That is a decision for the owner, not a package.

Re-check on each Chrome stable update: `HeadlessExperimental.enable` in new
headless (P1), and the `screencastFrame` metadata keys (P2).
