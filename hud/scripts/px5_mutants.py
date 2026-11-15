"""PX-5 mutation evidence (WS-5c's phone half): each mutant of hud/src/mobile/unlock.ts must fail
the phone's vitest (unlock.test.ts, unlockApp.test.ts, pr.test.ts, answer.test.ts). The file is
restored after every mutant, whatever happens.

Run from hud/: python3 scripts/px5_mutants.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HUD = Path(__file__).resolve().parents[1]
TARGET = HUD / "src" / "mobile" / "unlock.ts"
TESTS = ["src/mobile/unlock.test.ts", "src/mobile/unlockApp.test.ts", "src/mobile/pr.test.ts", "src/mobile/answer.test.ts"]

MUTANTS = [
    ("resend twice: a second 423 unlocks and sends again",
     "      if (st.unlockTried || st.sends > 1) return { kind: \"locked-again\" };",
     "      if (st.sends > 2) return { kind: \"locked-again\" };"),
    ("mint a new token on the retry",
     "      if (u !== true) return u;\n      return this.send(op, st, stage);\n    }\n    if (status === 410) {",
     "      if (u !== true) return u;\n      const fresh = this.freeze(op.key, op.url, { ...op.body });\n"
     "      return this.send(fresh ?? op, st, stage);\n    }\n    if (status === 410) {"),
    ("show Done for update_requested",
     "    return s.code === \"update_requested\" ? \"Update requested; check at the desk\" : \"Done\";",
     "    return \"Done\";"),
    ("treat a POST 401 as locked",
     "    if (status === 423) {",
     "    if (status === 423 || status === 401) {"),
    ("POST before unlocking when locked",
     "    held.promise = this.mirror.open() ? this.send(op, held.state, stage) : this.locked(op, held.state, stage);",
     "    held.promise = this.send(op, held.state, stage);"),
    ("a 410 resent automatically",
     "      void this.pool.refill();\n      return { kind: \"expired\" };",
     "      void this.pool.refill();\n      if (st.sends < 2) return this.send(op, st, stage);\n      return { kind: \"expired\" };"),
    ("a retry after an unlock 409",
     "    return { kind: \"unlock-refused\", status: got.status, body: got.body };\n  }",
     "    if (got.status === 409) return true;\n    return { kind: \"unlock-refused\", status: got.status, body: got.body };\n  }"),
    ("the body not detached from its source (an SSE re-read could change it)",
     "    const body = JSON.parse(JSON.stringify(fields)) as Json;",
     "    const body = fields;"),
    ("a duplicate shown as success",
     "    if (status === 409 && body.duplicate === true) return this.recover(op, \"duplicate\");",
     "    if (status === 409 && body.duplicate === true) return { kind: \"done\", status: 200, body };"),
]


def main() -> int:
    original = TARGET.read_text()
    killed = 0
    try:
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  BAD   {name}: the original text is not found exactly once")
                continue
            TARGET.write_text(original.replace(old, new))
            run = subprocess.run(["npx", "vitest", "run", *TESTS], cwd=HUD, capture_output=True, text=True)
            out = run.stdout + run.stderr
            failed = [line.strip() for line in out.splitlines() if line.strip().startswith(("×", "✗"))]
            if run.returncode != 0 and failed:
                killed += 1
                print(f"  KILLED {name}: {len(failed)} failing, first: {failed[0][:140]}")
            else:
                print(f"  SURVIVED {name}")
    finally:
        TARGET.write_text(original)
    print(f"{killed}/{len(MUTANTS)} mutants killed; unlock.ts restored")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
