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

from jarvis.face.server import _sentences


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
    print("\nall sentence checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
