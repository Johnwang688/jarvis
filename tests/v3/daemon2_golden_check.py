"""daemon2's gate verdicts are frozen: v3 work may never loosen or reword them.

Orchestrator-owned. The fixture holds, for each command, the decision, a hash
of the reason and `kill_unsure`, recorded from 9d374b43 (before A3b touched the
gate). It covers patterns around the regex cost threshold and anchored with
`$`: at a9a0641b, 584 of 3,267 such vectors were weakened and no suite noticed.

Nothing is executed: every check judges a string in process. Re-record only
for a deliberate, owner-approved change:
  python tests/v3/daemon2_golden_check.py --record
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from jarvis import config, protected_state as ps, rules

FIXTURE = Path(__file__).parent / "fixtures" / "daemon2_golden.json"


def commands():
    out = []
    for word in ("daemon2", "daemo[n]2", "daemon.", "n2", "jarvis d", "jarvis daemon2"):
        for lead in ("", ".*", ".*.*", "^", "(.*)*"):
            for anchor in ("", "$"):
                p = f"{lead}{word}{anchor}"
                out += [f"pkill -f '{p}'", f"kill $(pgrep -f '{p}')", f"killall -r '{p}'",
                        f"ps aux | grep -E '{p}' | awk '{{print $2}}' | xargs kill",
                        f"ss -ltnp | grep -E '{p}' | xargs kill"]
    for n in (0, 1, 5, 8, 9, 10, 11, 12, 13, 16, 20, 21, 22, 30, 39):
        for m in (0, 2, 4, 8):
            for anchor in ("", "$"):
                for word in ("daemon2", "n2", "8405"):
                    p = f".*.{{0,{n}}}{word}.{{0,{m}}}{anchor}"
                    out += [f"pkill -f '{p}'", f"kill $(pgrep -f '{p}')",
                            f"ss -ltnp | grep -E '{p}' | awk '{{print $2}}' | xargs kill"]
    for port in ("8405", "8402", "8403", "8408", "8400-8409"):
        out += [f"fuser -k {port}/tcp", f"kill $(lsof -t -i:{port})", f"lsof -i:{port}"]
    for unit in ("jarvis-daemon2", "jarvis-daemon2.service", "'jarvis-*'", "'*daemon*'", "app.slice", "pipewire"):
        for verb in ("stop", "restart", "kill", "mask", "disable --now", "enable --now", "start", "status",
                     "is-active", "show -p MainPID", "cat", "reset-failed"):
            out.append(f"systemctl --user {verb} {unit}")
    slice_ = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"
    for unit in ("jarvis-daemon2.service", "jarvis-daem*.service", "pipewire.service"):
        for name in ("cgroup.procs", "cgroup.threads"):
            out += [f"cat {slice_}/{unit}/{name} | xargs -r kill", f"pkill --pidfile {slice_}/{unit}/{name}",
                    f"pgrep --cgroup {slice_}/{unit}/{name} | xargs -r kill"]
    for path in ("/proc/*/cmdline", "/proc/[1-9]*/comm", "/proc/123/status"):
        out.append(f"grep -l daemon2 {path} | cut -d/ -f3 | xargs -r kill")
    return list(dict.fromkeys(out))


def judge():
    # The checkout path is part of the matched command line, so pin it.
    with patch.object(config, "REPO_ROOT", Path("/home/u/projects/Jarvis")):
        out = {}
        for command in commands():
            v = rules.decide(command)
            out[command] = [v.decision, hashlib.sha256(v.reason.encode()).hexdigest()[:12],
                            bool(ps.kill_unsure(command))]
        return out


class Golden(unittest.TestCase):
    maxDiff = 2000

    def test_daemon2_verdicts_and_reasons_are_unchanged(self):
        want, got = json.loads(FIXTURE.read_text()), judge()
        self.assertEqual(set(got), set(want))
        changed = [(c, want[c], got[c]) for c in want if got[c] != want[c]]
        self.assertEqual(changed[:5], [], f"{len(changed)} of {len(want)} daemon2 vectors changed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(judge(), indent=0, sort_keys=True) + "\n")
    else:
        unittest.main()
