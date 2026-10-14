"""Download and verify the V0 probe models.

    python tests/voice_v0/fetch_models.py            # fetch everything pinned
    python tests/voice_v0/fetch_models.py --record   # print sha256 for unpinned entries
    python tests/voice_v0/fetch_models.py parakeet-tdt-0.6b-v3-int8 ...

Official sources only (GitHub releases of k2-fsa/sherpa-onnx and
snakers4/silero-vad, the pipecat-ai Hugging Face repo, npm). Nothing here runs
in the normal test sweep.
"""

from __future__ import annotations

import sys

import common  # noqa: F401,I001
import models


def main(argv: list[str]) -> int:
    if not common.gated("download the pinned V0 models (about 950 MB)"):
        return 0
    record = "--record" in argv
    names = [a for a in argv if not a.startswith("--")] or list(models.manifest())
    for name in names:
        path = models.fetch(name, record=record)
        print(f"{name}: ok -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
