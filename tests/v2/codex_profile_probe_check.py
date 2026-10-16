"""The CG-2 offline probe as a check: the real Codex app-server under Jarvis's
permission profile (plan 2026-10-15 §5; `fixtures/cg2_probe.py` says what it
checks). Hermetic: a temporary HOME with fake credential files, a scripted
local model, a rootless network namespace with loopback alone. Nothing of the
owner's is opened.

Skipped when the Codex CLI or a usable `unshare -Urn` is absent. Run it on
every Codex upgrade (the feature it rests on is "under development" in 0.161).
Set TMPDIR under /var/tmp, not /tmp: the profile makes /tmp writable, and the
probe checks that its own temporary folder is not.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PROBE = HERE / "fixtures" / "cg2_probe.py"
CODEX = Path("/home/johnw/.local/bin/codex")


def _unshare_ok() -> bool:
    if shutil.which("unshare") is None or not Path("/usr/sbin/ip").exists():
        return False
    try:
        return subprocess.run(["unshare", "-Urn", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(CODEX.exists(), "Codex CLI absent")
@unittest.skipUnless(_unshare_ok(), "no rootless network namespace (unshare -Urn)")
class ProbeCheck(unittest.TestCase):
    def test_the_real_app_server_honours_the_profile(self):
        base = os.environ.get("TMPDIR", "/var/tmp")
        if base == "/tmp" or base.startswith("/tmp/"):
            base = "/var/tmp"
        with tempfile.TemporaryDirectory(prefix="cg2-check-", dir=base) as temp:
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": temp, "TMPDIR": temp, "LANG": "C.UTF-8"}
            proc = subprocess.run(["unshare", "-Urn", sys.executable, "-I", str(PROBE)],
                                  capture_output=True, text=True, env=env, timeout=600, cwd=temp)
        lines = proc.stdout.splitlines()
        tail = "\n".join(l for l in lines if l.startswith(("PASS", "FAIL", "ALL", "FAILED"))
                         or l.split(" ", 1)[0].count("/") == 1)
        self.assertEqual(proc.returncode, 0, tail + "\n" + proc.stderr[-2000:])
        self.assertEqual(lines[-1], "ALL PASS", tail)
        self.assertFalse(any(l.startswith("FAIL") for l in lines), tail)
        print("\n" + tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
