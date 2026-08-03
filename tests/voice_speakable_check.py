"""Synthetic checks for markdown -> speech. Free — no API, no audio.

The HUD renders markdown, so the model writes it; TTS reads the source
string, so "**done**" was spoken as "asterisk asterisk done asterisk
asterisk". `voice.speakable()` strips the syntax, and `voice.tts()` applies
it itself so no speech path can forget.

What it pins down:
  1. every markdown construct the model actually emits loses its syntax
  2. nothing that only *looks* like markdown is damaged — snake_case,
     2 * 3 * 4, and plain prose come back byte-identical
  3. it is idempotent (tts() re-applies it after the face already has)
  4. code fences are dropped rather than read aloud, and a reply that was
     nothing but a fence yields "" — the caller's cue to stay text-only
  5. tts() strips before synthesizing, and refuses an unspeakable reply

Run:  .venv/bin/python tests/voice_speakable_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import voice  # noqa: E402

STRIPPED = [
    ("**Done.** I checked *three* things.", "Done. I checked three things."),
    ("__bold__ and _italic_ and ~~struck~~", "bold and italic and struck"),
    ("***both at once***", "both at once"),
    ("# Report", "Report"),
    ("### Deeper heading", "Deeper heading"),
    ("- one\n- two\n- three", "one\ntwo\nthree"),
    ("1. first\n2) second", "first\nsecond"),
    ("- top\n  - nested", "top\n  nested"),
    ("Run `context.py` first.", "Run context.py first."),
    ("See [the docs](https://example.com) for more.", "See the docs for more."),
    ("![a diagram](https://example.com/x.png)", "a diagram"),
    ("Read <https://example.com/x> now.", "Read https://example.com/x now."),
    ("> quoted **line**", "quoted line"),
    ("---", ""),
    ("| name | qty |\n|---|---|\n| bolt | 12 |", "name, qty\nbolt, 12"),
    (r"a \* literal asterisk", "a * literal asterisk"),
]

# Text that must survive untouched — the failure mode where a stripper eats
# real content. Underscores in identifiers and lone asterisks in arithmetic
# are the two that show up in this codebase's own conversations.
UNTOUCHED = [
    "plain text with no markdown at all",
    "Use snake_case_name and MAX_STEPS here.",
    "That is 2 * 3 * 4 = 24.",
    "The file is jarvis/face/server.py, line 60.",
    "It cost $0.0023 — about 5x the snapshot run.",
    "a_b and c_d",
]


def main() -> int:
    for source, expected in STRIPPED:
        got = voice.speakable(source)
        assert got == expected, f"{source!r} -> {got!r}, wanted {expected!r}"
        assert voice.speakable(got) == got, f"not idempotent: {got!r}"
    print(f"ok  speakable: {len(STRIPPED)} markdown constructs stripped, idempotent")

    for text in UNTOUCHED:
        got = voice.speakable(text)
        assert got == text, f"damaged {text!r} -> {got!r}"
    print(f"ok  speakable: {len(UNTOUCHED)} lookalikes pass through unchanged")

    # A fence is dropped whole — reading source code aloud is worse than
    # silence — and prose around it survives.
    mixed = "Here it is:\n\n```py\nx = 1\nprint(x)\n```\n\nThat is all."
    assert voice.speakable(mixed) == "Here it is:\n\nThat is all.", voice.speakable(mixed)
    assert voice.speakable("```\nonly code\n```") == ""
    assert voice.speakable("~~~\nonly code\n~~~") == ""
    print("ok  speakable: code fences dropped; a fence-only reply speaks nothing")

    # tts() applies it itself: the strip is a property of speaking, not of
    # one call site. Captured by stubbing the backend one level down.
    spoken: list[str] = []

    def fake_local(text, voice_name, speed):
        spoken.append(text)
        return b"RIFF"

    original = voice._local_tts
    voice._local_tts = fake_local
    try:
        voice.tts("**Ready.**")
        assert spoken == ["Ready."], spoken
        for unspeakable in ("```\nx = 1\n```", "---", "   "):
            try:
                voice.tts(unspeakable)
            except ValueError:
                continue
            raise AssertionError(f"synthesized nothing-to-say for {unspeakable!r}")
    finally:
        voice._local_tts = original
    print("ok  tts: strips before synthesis, refuses a reply with nothing to say")

    print("\nall speakable checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
