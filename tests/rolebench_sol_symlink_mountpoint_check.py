"""A dangling symlink is not an existing read-only bind mountpoint.

Isolated grade-cell bwrap probes only; no credentials, CLI or network.
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


def test_dangling_in_tree_symlink_is_refused_before_launch() -> None:
    """lexists accepts a dangling destination symlink, so the missing read-only mountpoint still reaches bwrap and fails there."""
    with tempfile.TemporaryDirectory(prefix="rb-sol-link-") as tmp:
        root = Path(tmp)
        work, deps, account = root / "work", root / "state" / "deps", root / "account"
        for directory in (work, deps, account):
            directory.mkdir(parents=True)
        (deps / "marker").write_text("dependency fixture\n")
        (work / "vendor").mkdir()
        destination = work / "node_modules"
        destination.symlink_to("vendor/node_modules", target_is_directory=True)
        with patch.object(config, "real_home", return_value=str(account)), patch.dict(
            os.environ, {"ROLEBENCH_HOME": str(root / "state"), "ROLEBENCH_VENV": str(root / "no-venv")}
        ):
            cell = sandbox.Cell("grade", work, work_ro=True, extra_ro=((deps, destination),))
            refused = False
            detail = ""
            try:
                argv = sandbox.bwrap_argv(cell, ["/usr/bin/cat", str(destination / "marker")])
            except sandbox.SandboxError as exc:
                refused = "does not exist" in str(exc)
            else:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                detail = f"argv accepted; bwrap rc={done.returncode}: {done.stderr.strip()}"
            check("test_dangling_in_tree_symlink_is_refused_before_launch", refused, detail)
            (work / "vendor" / "node_modules").mkdir()
            argv = sandbox.bwrap_argv(cell, ["/usr/bin/cat", str(destination / "marker")])
            done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            check("control: the same symlink works after its target is created",
                  done.returncode == 0 and done.stdout == "dependency fixture\n", done.stderr.strip())


def main() -> int:
    test_dangling_in_tree_symlink_is_refused_before_launch()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
