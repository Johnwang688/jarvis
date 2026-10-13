"""V6: the HUD's TypeScript speech port is held to v1's Python by recorded vectors.

`hud/src/lib/speech/vectors.json` records what the Python originals --
`voice.speakable`, `face.server._sentences` and `face.server._stable_chunks` --
answer for a corpus: the markdown constructs of `tests/voice_speakable_check.py`,
the replies of `tests/face/speculation_check.py` (also fed character by
character, the way a model streams), and a few more shapes. `split.test.ts` and
`speakable.test.ts` replay them against the port.

This check fails when the committed file no longer matches the Python, i.e. a
rule changed in `voice.py` / `face/server.py` without the port (and the vectors)
following. Free: nothing is synthesized and no network is touched.

Run:   .venv/bin/python tests/face/speech_vectors_check.py          # verify
       .venv/bin/python tests/face/speech_vectors_check.py --write  # regenerate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "face"))

from jarvis import voice  # noqa: E402
from jarvis.face.server import _sentences, _stable_chunks  # noqa: E402
import speculation_check as spec  # noqa: E402
import voice_speakable_check as speak  # noqa: E402

VECTORS = ROOT / "hud" / "src" / "lib" / "speech" / "vectors.json"

EXTRA_TEXT = [
    "",
    "   ",
    "Hello.",
    "Line one\nLine two\nLine three",
    "First sentence is here. Second sentence is also here, and it is longer than the first. Third.",
    "Sure, I can do that for you right now, and then I will check the remaining three files in the directory carefully.",
    "I found the problem: the approval gate was reading the wrong file, so every request was allowed to pass through.",
    "This is a very long sentence without any punctuation that keeps going on and on and on and on and on and on "
    "and on and on and on and on and on and on and on and on and on and on and on and on and on and on and on "
    "and on and on and on and on and on and on and on and on and on and on and on and on and on until the end",
    "A short one. Another short one. And one more short sentence follows here.",
    "Wait… what? Really! Yes. Fine.",
    "The value is 3.5 percent, e.g. about one in thirty, i.e. small. Done.",
    "- first item\n- second item is a bit longer than the first\n- third",
]
SPEAKABLE_TEXT = [s for s, _ in speak.STRIPPED] + speak.UNTOUCHED + [
    "Here it is:\n\n```py\nx = 1\nprint(x)\n```\n\nThat is all.",
    "```\nonly code\n```",
    "~~~\nonly code\n~~~",
    "Intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nOutro",
    "## Title\n\n> quote **bold** and `code`\n\n1. one\n2. two",
    "Tight   spacing  and trailing spaces   \nnext line",
    "1. Do this:\n    ```bash\n    cat ~/.config/jarvis/.env\n    export KEY=sk-abcdef\n    ```\nDone.",
    "- a\n  - b\n    ```py\n    secret = 1\n    ```\nAfter.",
]


def build() -> dict:
    replies = list(spec.REPLIES) + EXTRA_TEXT[1:]
    stable = []
    for reply in replies:
        declared: list[str] = []
        samples = []
        for i in range(1, len(reply) + 1):
            chunks = _stable_chunks(voice.speakable(reply[:i]))
            for chunk in chunks:
                if chunk not in declared:
                    declared.append(chunk)
            if i % 7 == 0:
                samples.append([i, chunks])
        stable.append({
            "text": reply,
            "final": _sentences(voice.speakable(reply)),
            "declared": declared,
            "samples": samples,
        })
    return {
        "speakable": [{"in": t, "out": voice.speakable(t)} for t in SPEAKABLE_TEXT],
        "sentences": [{"in": t, "out": _sentences(t)} for t in replies + EXTRA_TEXT],
        "stable": stable,
    }


def main() -> int:
    data = build()
    rendered = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if "--write" in sys.argv:
        VECTORS.write_text(rendered)
        print(f"wrote {VECTORS.relative_to(ROOT)}: {len(data['speakable'])} speakable, "
              f"{len(data['sentences'])} sentences, {len(data['stable'])} streamed replies")
        return 0
    if not VECTORS.exists() or VECTORS.read_text() != rendered:
        print("FAIL speech vectors are stale: run tests/face/speech_vectors_check.py --write, "
              "then update hud/src/lib/speech/{split,speakable}.ts to match the Python")
        return 1
    print(f"ok  speech vectors: {len(data['speakable'])} speakable, {len(data['sentences'])} sentences, "
          f"{len(data['stable'])} streamed replies match the Python originals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
