"""Mutation evidence for the owner-only voice pickers (voice follow-ups, V2 row).

Each mutant removes or weakens the `owner_only` check on one route, in a temp
copy of the package, and runs `hud_backend_check.py
Backend.test_voice_pickers_are_the_owners_alone` against the copy. Every mutant
must make that test fail; one that survives is a route no test guards. Free:
fake audio and fake voices, temp config inside the check, ephemeral ports.

    python tests/v2/voice_owner_mutants.py [index …]
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HUD_API = "jarvis/v2/hud_api.py"
CHECK = ["tests/v2/hud_backend_check.py", "Backend.test_voice_pickers_are_the_owners_alone"]
SET = 'OWNER_VOICE_PICKERS = frozenset({"stt", "say", "voice", "voices", "mute"})'
GATE = '    if name in OWNER_VOICE_PICKERS:\n        owner_only(handler, daemon)\n'


def without(name: str) -> str:
    names = [n for n in ("stt", "say", "voice", "voices", "mute") if n != name]
    return "OWNER_VOICE_PICKERS = frozenset({%s})" % ", ".join(f'"{n}"' for n in names)


MUTANTS = [
    ("/stt not owner-only", HUD_API, SET, without("stt")),
    ("/say not owner-only", HUD_API, SET, without("say")),
    ("/voice not owner-only", HUD_API, SET, without("voice")),
    ("/voices not owner-only", HUD_API, SET, without("voices")),
    ("/mute not owner-only", HUD_API, SET, without("mute")),
    ("the gate removed from every voice picker", HUD_API, GATE,
     '    if name in OWNER_VOICE_PICKERS:\n        pass\n'),
    ("the body read before the gate", HUD_API, GATE,
     '    if name in OWNER_VOICE_PICKERS:\n        handler._raw_body()\n        owner_only(handler, daemon)\n'),
    ("reads not gated (GET /voices open)", HUD_API, GATE,
     '    if name in OWNER_VOICE_PICKERS and handler.command != "GET":\n        owner_only(handler, daemon)\n'),
]


def run(mutant, python: str) -> tuple[bool, str]:
    name, rel, old, new = mutant
    with tempfile.TemporaryDirectory(prefix="vo-mutant-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT / "jarvis", copy / "jarvis", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        proc = subprocess.run([python, *CHECK], cwd=copy, env=dict(os.environ, PYTHONPATH=str(copy)),
                              capture_output=True, text=True, timeout=600)
        out = proc.stdout + proc.stderr
        tail = [l for l in out.splitlines() if l.startswith("AssertionError")][:1] or out.strip().splitlines()[-1:]
        return proc.returncode != 0 and ("FAIL" in out or "Error" in out), (tail[0][:140] if tail else "")


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}")
        if not killed:
            survived.append(index)
    print(f"\n{'all %d mutants killed' % len(picks) if not survived else '%d of %d mutants survived' % (len(survived), len(picks))}")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
