"""Concurrent first-use counterexample for the managed-settings publisher.

No agent, model or network calls; only temporary fixtures and argv building.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from rolebench import sandbox

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


def test_parallel_first_claude_cells_publish_managed_settings() -> None:
    """Two first-use cells share managed-settings.tmp; one rename removes the other's source and aborts argv construction."""
    with tempfile.TemporaryDirectory(prefix="rb-sol-ms-race-") as tmp:
        root = Path(tmp)
        state, work = root / "state", root / "work"
        work.mkdir()
        target = state / "sandbox" / "managed-settings.json"
        rendezvous = threading.Barrier(2)
        original_write = Path.write_text

        def synchronised_write(path, *args, **kwargs):
            result = original_write(path, *args, **kwargs)
            if path == target.with_suffix(".tmp"):
                # Both writes finish before either rename. This is a legal
                # schedule of the real two-thread bench (MAX_CONCURRENT=3).
                try:
                    rendezvous.wait(timeout=1)
                except threading.BrokenBarrierError:
                    # A publisher that serialises callers is valid too: it
                    # need not let both writers reach this scheduling point.
                    pass
            return result

        def build():
            try:
                sandbox.bwrap_argv(sandbox.Cell("claude", work), ["/bin/true"], net="none")
                return None
            except Exception as exc:
                return type(exc).__name__

        with patch.dict(os.environ, {"ROLEBENCH_HOME": str(state)}):
            # Warm the older publisher so this isolates the new managed file.
            sandbox._gitconfig_file()
            with patch.object(Path, "write_text", synchronised_write):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: build(), range(2)))
            check("test_parallel_first_claude_cells_publish_managed_settings",
                  results == [None, None], f"concurrent argv results: {results}")
            check("control: the surviving managed file has the required content",
                  target.read_text() == sandbox.MANAGED_SETTINGS_TEXT)
            check("control: a subsequent serial cell builds",
                  sandbox.MANAGED_PATH in sandbox.bwrap_argv(
                      sandbox.Cell("claude", work), ["/bin/true"], net="none"))


def main() -> int:
    test_parallel_first_claude_cells_publish_managed_settings()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
