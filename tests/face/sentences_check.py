"""Synthetic checks for the TTS sentence splitter. Free — no API.

The first chunk gates time-to-first-speech (synthesis time scales with text
length), so it gets clamped to a clause; nothing may be lost or reordered.

Run:  .venv/bin/python tests/face/sentences_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.face.server import _BINDING, _sentences


def _rejoin_equals(text: str) -> None:
    chunks = _sentences(text)
    assert re.sub(r"\s+", " ", text.strip()) == " ".join(chunks), (text, chunks)


def main() -> int:
    # Short replies pass through whole.
    assert _sentences("Done.") == ["Done."]
    assert _sentences("It's 3pm.") == ["It's 3pm."]

    # A long opener with a clause splits at the comma, early enough to matter.
    long_first = (
        "I went through the whole inbox and grouped everything by urgency, "
        "which took a little digging because two threads were mislabeled. "
        "Here is the summary."
    )
    chunks = _sentences(long_first)
    assert len(chunks[0]) <= 90, chunks[0]
    assert chunks[0].endswith(","), chunks[0]
    assert len(chunks[0]) >= 20, "opener too choppy"
    _rejoin_equals(long_first)

    # No clause boundary: fall back to a word cut, never mid-word.
    no_comma = "The seventeen configuration files were regenerated successfully " \
               "and every single downstream consumer picked the changes up without restarting."
    chunks = _sentences(no_comma)
    assert len(chunks[0]) <= 90 and not chunks[0].endswith("-"), chunks
    assert no_comma.startswith(chunks[0]), chunks[0]
    _rejoin_equals(no_comma)

    # An absurdly early comma is not a split point ("So," alone sounds broken).
    early = "So, the thing you asked about earlier this morning turned out to be caused by the cache."
    chunks = _sentences(early)
    assert len(chunks[0]) >= 20, chunks
    _rejoin_equals(early)

    # Multi-sentence replies keep order and content.
    _rejoin_equals(
        "First point about the schedule. Second, the mail is triaged. "
        "Third — and this matters — the deploy is still red. A tiny one. Done."
    )
    print("ok  sentences: clamped opener, clean cuts, lossless rejoin")

    # --- the opener must not be absorbed (2026-08-18) -----------------------
    # The merge rule that keeps a stray fragment from becoming its own chunk
    # ("A tiny one." glued onto the sentence before it) also fired when the
    # *previous* chunk was short — which is exactly the opener. So a perfect
    # 20-char first chunk was swallowed into a 98-char one, and the clamp
    # below then hacked that apart at the last space, between "is" and
    # "complete.". Measured against local Kokoro: 607ms -> 1633ms to first
    # audio, plus an audible break mid-phrase. A short *first* sentence is the
    # best possible first chunk; it is the whole thing this function optimizes.
    absorbed = (
        "Sure, I can do that. I checked the three files you asked about and "
        "found the migration is complete. The changelog now lists one hundred "
        "and sixty six call sites."
    )
    chunks = _sentences(absorbed)
    assert chunks[0] == "Sure, I can do that.", chunks[0]
    assert "complete." not in chunks[0], chunks
    _rejoin_equals(absorbed)

    # A stray fragment is still glued back on — the rule this narrows, not
    # removes. "Done." is under the opener floor, so it does not stand alone.
    assert _sentences("Done. The tests pass.") == ["Done. The tests pass."]
    assert _sentences("Yes. No. Maybe.") == ["Yes. No. Maybe."]

    # An opener at the floor stands on its own rather than doubling in length.
    stands = "That worked. The seventeen configuration files were regenerated without complaint."
    assert _sentences(stands)[0] == "That worked.", _sentences(stands)
    _rejoin_equals(stands)

    # --- the word-cut fallback backs off past a binding word ----------------
    # With no clause boundary inside the first 90 characters the split falls
    # back to the last space, which lands wherever it lands — including
    # between an auxiliary or article and the word it governs. The cut is
    # permanent and audible; backing off a word or two is not.
    binding = (
        "The migration across every single package in the monorepo turned out "
        "to be entirely straightforward in the end."
    )
    chunks = _sentences(binding)
    assert len(chunks[0]) <= 90, chunks[0]
    assert chunks[0].split()[-1].lower().strip(",;:") not in _BINDING, chunks[0]
    _rejoin_equals(binding)

    print("ok  sentences: opener kept short, fragments still merged, no binding-word cuts")

    print("\nall sentence checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
