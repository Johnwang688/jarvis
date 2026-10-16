# Voice rework: follow-ups after the 2026-10-14 push

All sixteen voice packages (V0–V15) from [the voice rework plan](2026-10-13-voice-rework.md)
were built, independently reviewed and merged on 2026-10-14/15. Reviewers accepted the
items below as LOW (or as notes) at merge time. None is a boundary issue. Each is
small enough for one follow-up PR, and several are a line or two. Status rows are in
[the feature map](2026-10-13-current-feature-map.md).

## Acceptance still missing

Every receipt so far uses synthetic speech or Chrome's fake capture device. None of these
has been tried:

- the owner's own voice;
- a physical microphone;
- real speakers or headphones (echo, self-voice gate, V15's duck by ear);
- a real room or TV;
- Windows Chrome, whose CPU and latency are unmeasured; all numbers are from WSL Chromium.

The manual probes in `tests/voice_v0/` (`owner_manual.py`) cover owner WER and the speaker
EER, and they need the owner at the mic.

## Open items by package

| Package | Item | Source |
|---|---|---|
| V2 | The STT error line can keep a stale "Not retried" after a later success. | V2 review |
| V2 | The wording of the oversize-clip message. | V2 review |
| V2 | No test covers `place()` throwing. | V2 review |
| V2 | **Fixed 2026-10-16** in [#227](https://github.com/Johnwang688/jarvis/pull/227), merged 2026-10-16. `/stt` and `/say` had no `owner_only` check. Both are now owner-only, along with `GET /voices`, `POST /voice` and `POST /mute`, which had the same gap. PR #227's review added `POST /model`, `POST /models` and `POST /avatar`. See "The voice pickers are the owner's" in the voice-face notes. | V2 review |
| V6 | Diffs without a header are still read aloud. | V6 review |
| V6 | Barge-in with nothing playing pauses nothing. | V6 review |
| V6 | Buffering counts as active during a synthesis hang. | V6 review |
| V9 | No test combines the self-voice arm guard with V5's 60 s limit lift. Today his "Jarvis" within 1.5 s of his own voice cannot lift a claim cap, which is the safer order. | V9 merge review |
| V10 | "Jarvis stop. Over." is not a command. "over" is not a command filler, so the text goes to the chat as a message. This is a policy choice between V9 and V10. | V10 merge review, LOW 2 |
| V10 | `selfInRec` is conservative. A barge-in, or a reply inside the echo delay, keeps a literal "over"/"cancel" in the text. This is intended. | V10 merge review, LOW 3 |
| V10 | The empty-send `bargeEnd("dropped")` fix has a unit test but no browser test. | V10 builder |
| V11 | A main-thread stall of 2 s or more can trip the sticky "fell behind" failure. Try again restores it, and VadStage behaves the same. | V11 review, LOW 3 |
| V11 | `WAKE_LAG_MS = 1000` has little margin over the measured p95 of 0.64 s plus WSLg latency. | V11 review, LOW 5 |
| V11 | `engine.test.ts` asserts under 15% CPU on wall time, which may flake under load. | V11 review, LOW 7 |
| V11 | The avatar picker still lists each avatar's own wake phrases. Voice wake is "Jarvis" for all (decision V-15), so the list is cosmetic but untrue. | V11 builder |
| V14 | `VoiceDiagnostic` is declared twice in `voiceDiagnostics.ts`; the V13 copy is redundant. There is also a stray blank line. | V14 merge review |

## Test flakes seen during the sweeps

Each passed on rerun or alone, so no product fault was found. They recur under load:

- **`hud_v2_check` dictation section:** "REVIEW transcribes", "AUTO sends". These checks drive synthetic levels with fixed waits.
- **`hud_v2_multichat_check` voice section:** "a long turn in another pane no longer silences the mic".
- **Python suites:**
  - `runner_check` and `browser_auth_*_real`;
  - `devlisten_check`, which parses the live `/proc/net` tables, so a socket opening mid-read fails it;
  - `devurl_check`.
