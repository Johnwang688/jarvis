"""Speak to the HUD's capture only once it is really listening (2026-10-19).

The HUD tests script speech with `window.__hud.mic.feedMs(...)` after
`window.__hud.capture.openFollowUp()`. Whether that speech is claimed depends
on the capture's own `hooks.muted()` and `hooks.suppressed()` (Jarvis speaking,
a card, a picker, Settings, no selected chat, a crash) and on the follow-up
window really opening (`openFollowUp` returns early while muted). Those can lag
whatever the test waited for (a status line, a click), so under load the
utterance arrives first and is dropped as "unclaimed".

`speak_when_listening` checks all of that and feeds in ONE synchronous
`page.evaluate`, which nothing can interleave with, and retries until the
capture agrees. On failure the returned string says why.

One more load-dependent race lives in the detector, not the hooks: the quiet
lead-in can open a short utterance of its own (a blip over a floor the fake
device's frames moved), and whether that blip has closed by the time the
window opens depends on how many device frames the page processed in between.
Opened before the window, it is "unclaimed", and the speech that follows joins
it instead of opening one the window would claim. So the window's opening ends
any such utterance first, with quiet.

Only for speech the test expects to be claimed. A test that feeds on purpose
while muted, suppressed, or with no window open (to prove a drop) keeps its own
`page.evaluate`.

It takes the module's own `until(fn, timeout=...)` rather than importing one,
so this file imports nothing from the other checks. Import it the way
`hud_v2_mock.py` imports its siblings:

    try:
        from tests.face.hud_voice import speak_when_listening
    except ImportError:  # run as a script from tests/face
        from hud_voice import speak_when_listening  # type: ignore[no-redef]
"""

from __future__ import annotations

import re

# The scripted utterance: a quiet pre-roll, the follow-up window, speech, then
# silence long enough to end it. A module whose utterance differs (amplitudes,
# durations, a closeContinuation, a half-spoken utterance a later step ends)
# passes its own JS as `feed_js`.
UTTERANCE = """
const m = window.__hud.mic;
m.feedMs(2000, 0.001);
window.__hud.capture.openFollowUp();
m.feedMs(1600, 0.06); m.feedMs(2800, 0.0005);
"""

# `feed_js` is either a function expression (`() => { ... }`, called here) or a
# body of statements (run as is).
_FUNCTION = re.compile(r"\s*(\(\s*\)\s*=>|function\b)")

_TEMPLATE = """(() => {
  const c = window.__hud.capture;
  if (c.hooks.muted()) return "muted";
  if (c.hooks.suppressed()) return "suppressed: " + (c.hooks.suppressionReason?.() ?? "?");
  // The utterance's own openFollowUp must have opened a window. When it did
  // not, stop before the speech so a retry feeds it whole.
  const open = c.openFollowUp;
  let failed = null;
  c.openFollowUp = function (...a) {
    // The lead-in can open an utterance of its own (a blip over a detector
    // floor that the fake device's frames moved). Opened before the window, it
    // is unclaimed, and the speech then joins it instead of opening one the
    // window would claim. End it first, with quiet.
    for (let i = 0; i < 80 && c.vad && c.vad.open; i++) window.__hud.mic.feedMs(100, 0.0005);
    const r = open.apply(c, a);
    if (!c.conversing()) { failed = "the follow-up window did not open"; throw new Error(failed); }
    return r;
  };
  try {
    %s
  } catch (e) {
    if (failed) return failed;
    throw e;
  } finally {
    c.openFollowUp = open;
  }
  return "fed";
})()"""


def listening_js(feed_js: str = UTTERANCE) -> str:
    body = f"({feed_js.strip()})();" if _FUNCTION.match(feed_js) else feed_js
    return _TEMPLATE % body


def speak_when_listening(page, until, feed_js: str = UTTERANCE, timeout: float = 6.0) -> str:
    """Feed `feed_js` once the capture is listening: returns "fed", or the last
    reason it was not (for the failing check's detail)."""
    js = listening_js(feed_js)
    last = ["not tried"]

    def attempt():
        last[0] = page.evaluate(js)
        return last[0] == "fed"

    until(attempt, timeout=timeout)
    return last[0]
