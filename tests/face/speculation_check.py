"""Synthetic checks for speaking ahead of the turn. Free — no API, no Kokoro.

Time-to-first-speech was the whole synthesis of the first chunk (607ms to
1633ms measured against local Kokoro) and every millisecond of it was spent
*after* the turn had already finished. The text streams the whole time, so the
chunks are built as they appear and are simply waiting when `run_turn`
returns.

The property that makes it safe is that speculation is a **cache keyed on the
chunk text**, so being wrong costs CPU and never correctness. But wrong is not
free: local Kokoro serializes on one model instance, so a wasted synthesis
holds the lock that the chunk actually being waited on needs. So the first
check here is the one that matters — a chunk declared settled must never
change afterwards.

`voice.tts` is replaced by a recorder throughout; nothing is synthesized.

Run:  .venv/bin/python tests/face/speculation_check.py
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import voice  # noqa: E402
from jarvis.face import server  # noqa: E402
from jarvis.face.server import (  # noqa: E402
    SPECULATION_LIMIT,
    _sentences,
    _speculator,  # noqa: F401  (imported so a rename here is caught)
    _stable_chunks,
    _Speculator,
)

REPLIES = [
    "Done. The tests pass.",
    "Sure, I can do that. I checked the three files you asked about and found "
    "the migration is complete. The changelog now lists one hundred and sixty "
    "six call sites. Everything parses cleanly.",
    "Right, here is what I found. The approval gate had seven separate ways "
    "past it, and every one of them was a second, older copy of an idea that "
    "the rules module already had right. I have fixed all seven.",
    "Yes.",
    "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten. Eleven.",
    "I went through the whole inbox and grouped everything by urgency, which "
    "took a little digging because two threads were mislabeled. Here is the "
    "summary. Nothing is on fire.",
]


class Recorder:
    """Stands in for voice.tts and remembers every distinct call."""

    def __init__(self):
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, text: str, *a, **kw) -> bytes:
        with self.lock:
            self.calls.append(text)
        return b"RIFF" + text.encode()


def check_stability() -> None:
    """A settled chunk must survive every later append, byte for byte.

    This is the whole safety argument. If it does not hold, the speculator
    spends the TTS lock on strings that are never spoken, and the chunks the
    owner is waiting on queue up behind them — slower than not speculating at
    all, which is the one outcome that would make this a mistake rather than
    an optimization.
    """
    for reply in REPLIES:
        final = _sentences(voice.speakable(reply))
        declared: list[str] = []
        for i in range(1, len(reply) + 1):
            for chunk in _stable_chunks(voice.speakable(reply[:i])):
                if chunk not in declared:
                    declared.append(chunk)
        stale = [c for c in declared if c not in final]
        assert not stale, f"{reply[:40]!r}: declared settled then changed: {stale}"
        # ...and they are a prefix of the final chunking, in order.
        assert final[: len(declared)] == declared, (declared, final)
    print(f"ok  stability: over {len(REPLIES)} replies fed character by character, "
          f"every chunk declared settled survived to the finished reply")


def check_hits() -> None:
    rec = Recorder()
    voice.tts = rec
    reply = REPLIES[1]
    pool = ThreadPoolExecutor(max_workers=2)
    spec = _Speculator(pool)
    # Stream it the way a model does: a few characters at a time.
    for i in range(0, len(reply), 7):
        spec.feed(reply[i : i + 7])
    final = _sentences(voice.speakable(reply))
    futures = [spec.take(s) or pool.submit(rec, s) for s in final]
    audio = [f.result() for f in futures]
    spec.discard()
    pool.shutdown()

    assert spec.hits >= 1, "nothing was ready when the turn ended"
    assert audio == [b"RIFF" + s.encode() for s in final], "wrong audio, or out of order"
    # Every chunk synthesized exactly once: a hit must not also be re-rendered.
    assert sorted(rec.calls) == sorted(set(rec.calls)), f"duplicate synthesis: {rec.calls}"
    assert set(rec.calls) <= set(final), f"synthesized something never spoken: {rec.calls}"
    print(f"ok  hits: {spec.hits} of {len(final)} chunks were already rendered when the "
          f"turn ended, and nothing was synthesized twice")


def check_interim_discarded() -> None:
    """Text on the way to a tool call is not the answer and is never spoken."""
    rec = Recorder()
    voice.tts = rec
    pool = ThreadPoolExecutor(max_workers=2)
    spec = _Speculator(pool)

    thinking = ("Let me look at that for you. I will check the three files in "
                "the migration directory first. Then the changelog.")
    for i in range(0, len(thinking), 7):
        spec.feed(thinking[i : i + 7])
    assert spec.ready, "nothing was speculated from the interim text"
    spec.restart()
    assert not spec.ready, "interim guesses survived the tool call"

    answer = REPLIES[2]
    for i in range(0, len(answer), 7):
        spec.feed(answer[i : i + 7])
    final = _sentences(voice.speakable(answer))
    claimed = [s for s in final if spec.take(s) is not None]
    spec.discard()
    pool.shutdown()

    assert claimed, "nothing survived from the real answer"
    # Nothing from the thinking-out-loud pass can be claimed as the reply.
    for chunk in _sentences(voice.speakable(thinking)):
        assert chunk not in final or chunk in answer, chunk
    print(f"ok  interim: guesses made before a tool call are dropped, and the answer "
          f"that followed still landed {len(claimed)} chunks ready")


def check_limit_and_failure() -> None:
    rec = Recorder()
    voice.tts = rec
    pool = ThreadPoolExecutor(max_workers=2)
    spec = _Speculator(pool)
    long_reply = " ".join(f"Sentence number {n} is here and reasonably long." for n in range(40))
    for i in range(0, len(long_reply), 11):
        spec.feed(long_reply[i : i + 11])
    assert len(spec.ready) <= SPECULATION_LIMIT, len(spec.ready)
    spec.discard()
    pool.shutdown()
    print(f"ok  limit: a 40-sentence reply pre-renders at most {SPECULATION_LIMIT} chunks, "
          f"not all of it")

    # A bonus must never be why a turn fails.
    def boom(text, *a, **kw):
        raise RuntimeError("kokoro is unhappy")

    voice.tts = boom
    pool = ThreadPoolExecutor(max_workers=2)
    spec = _Speculator(pool)
    for i in range(0, len(REPLIES[1]), 7):
        spec.feed(REPLIES[1][i : i + 7])   # must not raise
    spec.discard()
    pool.shutdown()

    class Angry(str):
        def __add__(self, other):
            raise ValueError("speculation is having a bad day")

    spec = _Speculator(ThreadPoolExecutor(max_workers=1))
    spec.buf = Angry("x")
    spec.feed("more")  # must not raise either
    print("ok  degradation: a failing synthesis and a failing guess both leave the turn alone")


def check_muted_speculates_nothing() -> None:
    """The wiring in _converse, read back from the source.

    A muted turn synthesizes no audio at all, so speculating for one would be
    pure waste — and, on the local backend, waste that holds a lock.
    """
    src = Path(server.__file__).read_text()
    assert "spec = None if voicectl.is_muted() else _Speculator(pool)" in src, \
        "muted turns must not build a speculator"
    assert src.count("spec.discard()") >= 4, \
        "every early return out of _converse must drop its guesses"
    print("ok  wiring: muted turns speculate nothing; every exit path discards")


def main() -> int:
    original = voice.tts
    try:
        check_stability()
        check_hits()
        check_interim_discarded()
        check_limit_and_failure()
        check_muted_speculates_nothing()
    finally:
        voice.tts = original
    print("\nall speculation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
