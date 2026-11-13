"""Counterexample: extra_ro inside a read-only work tree needs a mountpoint.

Free, isolated bwrap probes only; no agent, model, or network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

import os
import subprocess
import tempfile
from unittest.mock import patch

from rolebench import config, sandbox

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def test_extra_ro_inside_readonly_work_without_placeholder() -> None:
    """A valid extra_ro under read-only work cannot launch unless its destination already exists."""
    with tempfile.TemporaryDirectory(prefix="rb-sol-ro-") as tmp:
        root = Path(tmp)
        work, deps, account = root / "work", root / "deps", root / "account"
        for directory in (work, deps, account):
            directory.mkdir()
        (deps / "marker").write_text("dependency fixture\n")
        destination = work / "node_modules"
        with patch.object(config, "real_home", return_value=str(account)), patch.dict(
            os.environ,
            {"ROLEBENCH_HOME": str(root / "cache"), "ROLEBENCH_VENV": str(root / "no-venv")},
        ):
            # grade+work_ro exercises the same work mount as judge, without
            # involving a CLI or any credential pipe.
            cell = sandbox.Cell("grade", work, work_ro=True, extra_ro=((deps, destination),))

            def probe() -> subprocess.CompletedProcess:
                argv = sandbox.bwrap_argv(cell, ["/usr/bin/cat", str(destination / "marker")])
                return subprocess.run(argv, capture_output=True, text=True, timeout=10)

            missing = probe()
            destination.mkdir()
            existing = probe()
            check("control: extra_ro works with a pre-existing destination in read-only work",
                  existing.returncode == 0 and existing.stdout == "dependency fixture\n",
                  existing.stderr.strip())
            check("test_extra_ro_inside_readonly_work_without_placeholder",
                  missing.returncode == 0 and missing.stdout == "dependency fixture\n",
                  f"exit={missing.returncode}: {missing.stderr.strip()}")


def main() -> int:
    test_extra_ro_inside_readonly_work_without_placeholder()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
