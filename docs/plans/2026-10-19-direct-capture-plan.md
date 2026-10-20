# Plan: Direct capture in the installed HUD (B-DIRECT-CAPTURE, BR-4/M6), 2026-10-19

**Status: built, gated, not enabled until the owner's probe passes.** The real Windows app-mode prompt and pixels are
the owner's to produce (M6). Synthetic streams in tests are not M6 acceptance.

## What exists

- Remote capture and annotation are installed (BR4). Direct tabs answer Annotate and Send with
  "Open this in Remote to annotate." and an explicit second-gesture Open in Remote.
- The original design (`2026-10-10-hud-browser-plan.md` §5, "How the still is taken") is
  `getDisplayMedia({preferCurrentTab: true})` cropped to the iframe with `CropTarget`, one frame, then the tracks stop.
  Chrome prompts once per capture. Only the URL is attached, as a fenced reference. The HUD cannot read a
  cross-origin frame's title or text.

## The gate: an owner Settings switch, off by default, that a passing probe unlocks

`browser.json` gets one more owner switch, `direct_capture`, **off** by default and fail-closed on a malformed file.
It sits beside the LAN (B-5), sound (B-12) and auto-open switches. Like them, it is protected state, changed only
through the owner-only `PATCH /browser/settings`.

**Turning it on needs a probe that passed on this daemon.** `PATCH {direct_capture: true}` is refused (409) unless the
daemon holds a passing probe record from this run. Turning it off always works. The pass is held in daemon memory
only. The report file is written for the lead to read and is never read back as authority, so a planted file
unlocks nothing. After a restart the switch keeps its saved value; turning it on again means running the probe again.

Why both, not one of them:
- **A switch alone** would let Direct capture turn on with no evidence, which the M6 rule forbids.
- **Self-enabling after a probe** would turn on a new way of capturing the screen without the owner choosing it.
  The existing Browser decisions keep every such widening as an owner switch (B-5, B-12, `local_servers`).
- **Both**: the owner chooses, and the choice is only offered once this machine has shown the evidence.

The daemon also refuses a Direct capture reference while the switch is off. So a HUD that missed the change still
cannot attach a Direct still.

## Capture path (HUD, `lib/directCapture.ts`)

1. **Gesture.** Only the existing Annotate or Send to chat button, as a trusted press (`isTrusted`), on the active,
   drawn tab. The page must be visible. The switch must read on.
2. **Coverage, before the prompt.** The tab must be drawn and uncovered, with no approval card pending (`blocked`).
   Options close and the pane waits for a fresh uncovered receipt, the same path Remote uses. Then the HUD waits two
   animation frames and checks the frame directly. Its rect is non-empty and wholly inside the viewport. Every point
   on a 5×5 grid hit-tests to the frame or its own transparent shield. No dialog, menu, listbox, tooltip, card or
   prompt element (`role` or known HUD selectors) intersects it.
3. **Capture.** First `CropTarget.fromElement(iframe)`. Then `getDisplayMedia({video: {displaySurface: "browser"},
   audio: false, preferCurrentTab: true, selfBrowserSurface: "include", surfaceSwitching: "exclude",
   monitorTypeSurfaces: "exclude"})`, then `track.cropTo(target)`. Then one `ImageCapture.grabFrame()`.
4. **Tracks stop at once on every path.** One `finally` stops every track of the stream. That covers success, the
   prompt being cancelled (no stream), an error at any step, a pixel-check failure, and a card, hide, tab change or
   unmount during any await. A stream that arrives after the gesture went stale is stopped as soon as it resolves.
   The bitmap is closed after use.
5. **Pixel check, before use.**
   - The frame's size equals the iframe's on-screen size × `devicePixelRatio`, within 2 px.
   - It is not the whole viewport's size, which would mean an uncropped tab.
   - `displaySurface` is `browser` when Chrome reports it.
   - The frame is not blank: a sampled 64×64 grid is not uniform and not fully transparent. A page that really is one colour is refused too.
   - Coverage is checked again right after the grab.

   Any failure discards the pixels and shows the existing Open in Remote offer, with a sentence that names the
   failure: cancelled, not cropped, wrong size, blank, or covered.
6. **Hand-off.** The daemon mints a URL-only capture reference: `POST /browser/direct-capture`, owner-only. The URL is
   the registry's own record of the pane's binding, never the HUD's word. It goes through the same credential guard
   as any capture and has no title or text. The frame is downscaled to 1920 px on its long edge and becomes an
   ordinary `CaptureStill`. From there the flow is Remote's: Annotate, or Send stages the picture and the reference
   into the pinned chat draft. Nothing sends a turn.

## Privacy and focus

- A tab capture shows whatever is painted over the frame. Hence the coverage checks before and after.
- The capture is never taken from a hidden or background pane, and never without the owner's press.
- Nothing calls `focus()` on the frame. The tests check that `document.activeElement` is unchanged and that the frame
  guard records no theft.
- A cancelled prompt leaves no still, no stream, no reference and no daemon call.
- **Residual, stated rather than solved:** Remote withholds an image when its page text holds a credential. Direct
  cannot read a cross-origin page's text, so a Direct still has no credential check. This was already true of the
  approved design, which attaches only the URL. The switch's label and the still's note say so. The picture still
  reaches an agent only through the owner's own Send.

## Owner probe (Settings, Browser, "Test Direct capture")

It runs only on the owner's trusted press, and only when `(display-mode: standalone)` matches, so only in the
installed app. It crops to a HUD-made test-pattern iframe inside Settings, not a web page: four solid quadrants and a
centre block, in a sandboxed `srcdoc` frame with no scripts. So its pixel samples are known colours, not page
content, and they prove the crop was really applied.

It records these facts:
- display mode and Chrome major version;
- whether the APIs are present;
- the prompt outcome and the error name;
- track settings (`displaySurface`, width, height, frame rate, cursor);
- crop applied, and the crop error name;
- frame size against the expected size;
- nine colour samples against the expected colours;
- the live tracks left afterwards (it must be 0);
- step timings.

The HUD posts them to `POST /browser/direct-capture/probe`, owner-only. The daemon re-validates every field against a
fixed schema and recomputes pass or fail itself. It then writes `~/.local/share/jarvis/direct-capture-probe.json`
(`JARVIS_DIRECT_CAPTURE_PROBE`; 0600, atomic) for the lead, and keeps the pass in memory. No image is stored.

## Tests

- **Vitest**, with fake `getDisplayMedia`, `CropTarget` and `ImageCapture`:
  - tracks stop on success, cancel, crop error, grab error, a pixel failure and unmount mid-await;
  - refusal under a card, an overlay, a hidden pane and an untrusted press;
  - the fallback for wrong size, uncropped, blank and covered frames;
  - no focus change;
  - the probe's verdict and its pass gating.
- **Python**: the settings default and fail-closed behaviour; probe validation and recomputation; the 409 gate; the
  owner-only Direct reference route (mode, switch, approval hold, binding).
- **Mock HUD section**, over real Chromium with injected fakes: switch on, Direct Annotate gives a still, Send stages
  the pair, a card refuses, and the fallback stays default.
- **Two spot-check mutants**: drop the `finally` stop, and drop the crop-size check. Each must fail its test.

## Not verified here

The real Windows app-mode prompt and pixels, and whether Chrome's sharing bar changes the iframe's size during the
grab. Also whether HUD zoom and DPR agree with the cropped frame. The probe records all three.
