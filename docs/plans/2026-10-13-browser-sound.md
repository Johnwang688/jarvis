# Browser sound (BR-10)

**Status:** implemented and independently reviewed in draft [PR #137](https://github.com/Johnwang688/jarvis/pull/137); lead merge/deployment pending, 2026-10-13.

It replaces amendment 1 §F's "how on works" where they differ. The switch's protected-state rule and the `browser_sound_check.py` outline (§H) carry forward. The earlier bounded M3 platform probe established headless WSLg routing and exact owned-stream mute/readback. Its original receipt has no Git head; physical audibility, echo and timing remain unmeasured. Route C is accepted; no Xvfb or leak-free Pulse subscription claim is made.

## Accepted implementation and verification

The owner/lead selected route C: memory-only permission for the current profile instance, with an explicit all-tabs label. New tabs in an already-enabled instance inherit that scope; navigation cannot independently re-mute one tab. Closing the last tab, changing profile, ordinary fresh/restarted instances, daemon restart and master OFF clear grants. Only an exact coordinator replacement may transfer the still-current explicit request needed to apply that gesture. The master defaults to allowed; enabling it never restores old grants. Private ON is refused while its session exists. Hidden/covered playback continues; controls remain inert under cards, annotation, hidden/offline/unknown state and during unsafe work.

Runtime freeze 502f62b6 passed the production build, 1,901 HUD unit tests and the complete 3,217/3,217 mock HUD sweep. The immutable backend sweep was 124/125; its original sole stale-fixture failure is retained. Reviewed fixture-only 3655a15 passed the entire 36-test Browser routes suite, completing coverage of all 125 canonical suites. Subsequent main integration changes only docs and that fixture; runtime remains byte-identical to the tested freeze. Independent source reviews and assertion mutants passed. Physical speaker/mic acceptance remains separate.

The voice queue package V15 owns speaking/ducking integration. This release adds no unconsumed HTTP/App speaking hook and claims no ducking. The unselected alternatives below remain design context, not implemented capabilities.

## For the owner

- **The rule.** Every Remote profile instance starts muted. Only the owner's explicit speaker gesture grants sound; agents and pages cannot grant it. The grant covers every tab in that instance, including new tabs and later sites.
- **Playback/grants start muted** every time the daemon starts. The master permission defaults to allowed and does not itself enable playback.
- **Muting** takes one click. Existing owned streams receive an immediate native mute attempt; structural replacement completes at idle. Pending or failed application stays visible.
- **Where sound comes out.**
  - Remote pages play through your speakers by way of WSLg. The pane carries the picture only, so sound and video are not synchronised.
  - Direct pages (your local dev servers) play inside the HUD window under the browser's own rule: a page plays only after you have clicked in it.
- **The accepted scope.** Per-profile sound uses an idle-only Chrome replacement; there is no per-tab or automatic cross-site re-mute.
- **What isn't proven yet.** Physical audibility, echo and the one-second response target need owner-assisted measurement. Technical routing/mute is established by the earlier M3 platform probe.

## Decisions recorded

- **DS-0. Owner, 2026-10-13: sound is off until the owner clicks something. No autoplay.**
  - This refines B-12 ("off by default until the owner turns it on") and amendment 1 §F.
  - "Clicks something" is read as **an explicit sound control the owner presses**, not any click in a page. A click inside a Remote page cannot be told apart from an agent's click: both arrive as CDP `Input.dispatch*` events, and both give Chrome user activation.
- **DS-1. Off stays structural.**
  - Chrome launches with `--mute-audio` (`jarvis/v2/browser/host.py:211`).
  - Its environment is built from nothing, with no `PULSE_SERVER` (`host.py:230-243`; the module note at `host.py:63`).
  - Every route below keeps that as the state on every launch, until the owner's gesture.
- **DS-2. No agent route.**
  - `hud_browser_act` gains no sound action.
  - No session MCP, workflow or Discord surface reaches the control.
  - The control is an owner-only route (HUD listener, strict Host), like `PATCH /browser/settings`.

## What the system does today (verified)

- **Remote frames carry no audio.** Each Remote tab streams CDP screencast frames over a ticketed socket (`jarvis/v2/browser/stream.py`). CDP screencast has no audio track, so Remote sound can only leave through Chrome's own audio output.
- **That output would be WSLg's PulseServer** at `unix:/mnt/wslg/PulseServer`, which is full duplex. `libpulse0` is installed; Chrome dlopens it (CLAUDE.md, Environment).
- **Chrome runs one instance per open profile, plus one Private instance**, with contexts inside it. Instances are headless (`host.py:184`, `1011`). `--mute-audio` therefore applies per instance and can only be changed by relaunching.
- **M3 technical evidence exists.** The earlier isolated synthetic WAV advanced from 0.061902 to 0.821803 seconds. One exact owned float32le mono 44100 Hz Pulse stream was unmuted/uncorked; ownership was rechecked before mute and readback confirmed it. This original receipt has no Git head and does not certify this release or physical audibility. See [browser notes](../notes/browser.md).
- **Direct frames have no autoplay delegation.**
  - A Direct pane is an iframe with `sandbox="allow-scripts allow-forms"` (plus `allow-same-origin` only for a live grant), and no `allow` attribute (`hud/src/components/FrameHost.tsx:50-58`, `hud/src/lib/browser.ts:63-64`).
  - So the opaque or cross-origin frame does not inherit the HUD's autoplay permission. Chrome's policy then blocks sound until there is user activation inside that frame.
  - Launch args do not set an autoplay-policy override.
  - Agents never act in Direct (H6).
- **Jarvis's voice.** The HUD fetches `/say` (`hud/src/api.ts:374-377`; the endpoint is at `jarvis/v2/hud_api.py:1420-1425`) and plays it in the HUD window. That is a separate path from Remote Chrome's audio, and nothing coordinates the two today.

## Design

**The owner's control.**

- A speaker button in each Remote tab's toolbar, which replaces §F's "opens the setting" mark.
  - **Muted** is the resting state.
  - **On** shows a visible indicator on the tab chip and the pane, so a playing pane is findable even when it is not focused.
- The button needs a real pointer or keyboard activation in the HUD.
- It is disabled and inert under an approval card, while annotating, and in a hidden pane. It never takes focus by itself.
- The click calls `POST /browser/tabs/<id>/sound {on}`, which is owner-only.
- **State is memory-only in the daemon** and is never written to workspace storage or `browser.json`. It clears on:
  - the last tab in that profile closing;
  - profile change;
  - daemon restart or ordinary fresh/restarted instances (exact coordinator application may transfer the current gesture);
  - the end of a Private session (Q1 covers what else clears it).

**The Settings switch.** §F's `browser.json` `sound` key stays protected state.

- Accepted Q2: a master switch allows speaker buttons, **default allowed**. Malformed state fails closed; unrelated repair never enables it. OFF hides buttons, clears grants and requests structural mute. Ordinary/fresh launches remain structurally muted.
- One direct speaker gesture grants the current profile scope.
- Turning the master back on never restores earlier playback grants.

**The route: C selected; A/B below are unselected alternatives.**

- **A. Per tab, through Chrome.**
  - Launch without `--mute-audio`, with `PULSE_SERVER` set. Every target is muted from its creation, before its first document, through the existing DevTools pipe. The owner's click unmutes that one tab.
  - **This needs a per-tab mute method in Chrome 155's DevTools protocol.** None is known to exist; extensions are disabled (`--disable-extensions`). S0-b checks the installed protocol. If none exists, A is dropped.
- **B. Per instance, through PulseAudio.**
  - Launch with `PULSE_SERVER` and `PULSE_PROP` tagging the instance, without `--mute-audio`.
  - The daemon mutes each new sink-input from that instance with `set-sink-input-mute`, until the owner unmutes that profile.
  - **This is acceptable only if S0-c shows that no audible samples play before the mute lands.** A subscription race that leaks even one buffer fails the "no autoplay" rule, and B is dropped.
  - The granularity is the whole profile instance: every tab of Main at once.
- **C. Per instance, by relaunch** (amendment 1 §F).
  - The owner's first click relaunches that profile's Chrome without `--mute-audio`, at the next idle moment (no card, no action in flight).
  - Persistent tabs reopen at their URLs.
  - **Private cannot relaunch while a Private pane is open,** because its session would be lost. The button says "Close Private panes to allow sound".
  - Existing owned-stream native mute is attempted immediately; idle structural replacement completes the request. Stream mute alone never admits new pages into an audible-but-revoked instance.
- **If M3.1 fails** (headless cannot play through WSLg): BD-8's headed Chrome under Xvfb needs a fresh owner decision (Q5). BR-10 stops, and the button reads "Sound isn't available on this machine yet".

**Re-muting (accepted Q1).**

- Route C binds the grant to the current profile instance, not one tab/site.
- Cross-site navigation does not re-mute; all-tabs/new-tabs/hidden-playback scope is disclosed.
- Per-tab/site re-mute would require route A and is not claimed here.

**Hidden and covered panes.**

- A hidden, covered or disconnected pane closes its stream (BR3b), but Chrome keeps running.
- Accepted Q3: an enabled profile **keeps playing** when hidden or covered, like any browser, with the indicator still visible.
- **Under an approval card**, Browser sound is not paused. The card's own chime (`Approvals.tsx:24`) plays in the HUD.

**Direct mode (Q4).**

- Jarvis cannot mute a cross-origin iframe from the parent.
- Accepted Q4: keep the browser's own rule (sound only after the owner clicks inside that page), and do not add `allow="autoplay"`.
- A test pins the absence of any autoplay delegation, including through `FrameHost`'s sandbox string.

**Private.** Private follows the same per-tab or per-instance rule, and its sound state never outlives the Private session.

**Jarvis's speech and the mic (hook only).** A voice-queue redesign is in progress separately.

- Deferred to V15: add a consumed speaking-state interface with the voice queue package. No HTTP/App hook is implemented in this release.
- With route A or B, the daemon can **duck** unmuted Browser audio, for example lowering the Pulse sink-input volume, and restore it afterwards. It never unmutes anything.
- Direct audio cannot be ducked.
- Also: under hands-free listening, Browser audio from the speakers can reach the WSLg mic and the VAD. Push-to-talk is unaffected. Whether to duck, pause or do nothing is Q-V below.

## Packages and order

1. **S0, measurement.** A spike in `scripts/browser_measure.py`; manual, temporary HOME, a local WAV fixture, never in a suite.
   - a. M3.1, M3.2 and M3.3.
   - b. List the CDP domains of Chrome 155 over the private pipe and look for a per-target audio mute.
   - c. With `PULSE_PROP` tagging, the time from stream creation to mute, measured with a Pulse monitor recording.
   - d. Relaunch cost per profile, against the BR7 font-cache figures.

   Report the numbers in the browser notes. **The route is then chosen by the lead, and by the owner if Xvfb is involved.**
2. **BR-10a, daemon.**
   - The owner route and memory state.
   - The chosen route in `host.py`, plus `jarvis/v2/browser/audio.py`, as §F named it.
   - The `sound` setting and the clean environment's `PULSE_SERVER` only when allowed.
   - Route C disclosure instead of an unsupported cross-site re-mute.
   - Speaking/ducking hook deferred to V15.
3. **BR-10b, HUD.** The speaker button, indicators, the Settings switch, and blocking under cards, annotation and hidden panes.
4. **Review and docs.**
   - Independent review with mutation evidence, then the memory-capped sweep.
   - Docs: the browser notes, `hud-api.md`, and the plan §9 "Sound is not in the pane" limit, updated.

## Tests (free): `tests/v2/browser_sound_check.py` plus HUD tests

| Check | Mutation it must catch |
|---|---|
| Default launch: `--mute-audio` present and no `PULSE_SERVER` until an owner gesture (or the chosen route's equivalent muted state) | `PULSE_SERVER` leaked while off |
| The sound route is owner-only: 403 on the API listener, with a foreign Origin, or with a bad Host; no session MCP or `hud_browser_act` path reaches it | route on the API listener |
| Cold/fresh instances, ordinary restarts, new Private sessions and profile changes are muted; new tabs in an already-on instance inherit disclosed scope | stale/cold grant restored |
| Route C discloses all-tabs/new-tabs/hidden playback and does not claim cross-site re-mute | false per-tab/site guarantee |
| Route C: the relaunch waits for no card and no action in flight; Private is refused while a Private pane is open | relaunch mid-action |
| `browser.json` writes are refused to `write_file` and to v2 layer 1 | not protected |
| HUD: the button is inert under a card, while annotating and in a hidden pane; it never autofocuses; the indicator renders as text | active under a card |
| Direct frames carry no `allow` attribute and no autoplay delegation | `allow="autoplay"` added |
| V15 speaking/ducking is pending; no unconsumed hook or ducking is claimed | speculative hook presented as implemented |

Real-Chrome audio is not asserted in the suite, because the suite has no audio device. The leak window is a measurement (S0-c), not a test.

## Owner-assisted live checks (unmeasured)

- With the owner's speakers, on a fixture page that autoplays on load: silence until the click.
- Sound within about 1 s of the click; silence within about 1 s of mute.
- An agent click in a muted tab stays silent.
- Confirm disclosed route C scope: cross-site navigation and new tabs in an enabled profile retain sound.
- A hidden pane behaves per Q3.
- A Direct dev page is silent until the owner clicks in it.
- Jarvis speaking while a Remote video plays: per Q-V.
- Measurements: the CPU of Chrome's audio process while playing, and S0's numbers.

## Owner decisions and remaining voice/platform work

1. **How long does an unmute last?** Accepted: per profile instance until mute, last-tab close, profile change or ordinary restart. Never saved; exact coordinator application may transfer the current gesture.
2. **One click or two?** Accepted one: the pane's speaker button. The Settings switch becomes a master "allow Browser sound", default allowed, and OFF clears grants and requests structural mute; pending/failed application stays visible.
3. **Should a hidden or covered pane keep playing?** Accepted **yes**, with a visible "sound on" mark on its tab, so music can play while you work elsewhere.
4. **Direct pages: accept the browser's own rule (sound after you click in the page)?** Accepted **yes**. Jarvis cannot mute a local dev page framed in the HUD, and agents can't act there.
5. **If only per-profile control works (routes B and C), or only headed Chrome under Xvfb can play:** Per-profile control is accepted with a clear "Sound on for Main" label. Recommend asking again before Xvfb, which is BD-8's extra lever for same-user programs.
- **Q-V. When Jarvis speaks while a Browser page plays sound:** Recommend **ducking the Browser** (never unmuting it), and leaving the exact behaviour to the voice-queue redesign. Do nothing until that lands.
