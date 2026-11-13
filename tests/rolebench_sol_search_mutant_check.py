"""Counterexample to the PR's search-switch test: live search passes it.

The production function is mutated in memory only and restored afterwards.
Both PR test scripts run their normal main functions with the test guard on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

import contextlib
import io
from unittest.mock import patch

import rolebench_egress_check as acceptance
import rolebench_egress_extra_check as extra
from rolebench import runners

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


def test_pr_checks_reject_live_search_mutant() -> None:
    """The PR's own tests pass even when the new Codex switch enables live search."""
    original = runners.codex_argv

    def live_search(*args, **kwargs):
        return ['web_search="live"' if arg == 'web_search="disabled"' else arg
                for arg in original(*args, **kwargs)]

    def run_suites():
        results = []
        for suite in (acceptance, extra):
            suite.PASSED = 0
            suite.FAILED.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                result = suite.main()
            results.append((suite.__name__, result, suite.PASSED))
        return results

    baseline = run_suites()
    check("control: the PR's two suites pass without mutation",
          all(result == 0 for _, result, _ in baseline), str(baseline))
    with patch.object(runners, "codex_argv", live_search):
        argv = runners.codex_argv("gpt-6.1-sol", "/work", Path("/last.txt"))
        assert 'web_search="live"' in argv and 'web_search="disabled"' not in argv
        mutant = run_suites()
    check("test_pr_checks_reject_live_search_mutant",
          any(result != 0 for _, result, _ in mutant),
          f"disabled -> live survived both PR suites: {mutant}")


def main() -> int:
    test_pr_checks_reject_live_search_mutant()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
