"""Mutation evidence for PR summaries (PM-C, 2026-10-28).

Each mutant breaks one guard in a temp copy of the package and runs the one
test of `tests/v2/pr_summary_check.py` meant to hold it. A mutant is killed
only when **that test** fails (FAIL or ERROR on its own name), so a mutant
that breaks something else does not count; the unmutated copy must pass each
named test first (the baseline). The test's first error line is printed, so
the reason it failed can be read.

Free: fakes, temp HOME, no network, no paid call.

    python tests/v2/pr_summary_mutants.py          # all mutants
    python tests/v2/pr_summary_mutants.py 3 7      # by index
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PS = "jarvis/v2/pr_summary.py"
PRS = "jarvis/v2/inbox_prs.py"
DAEMON = "jarvis/v2/daemon.py"
CHECK = "tests/v2/pr_summary_check.py"

# (name, file, old, new, [the tests meant to fail])
MUTANTS = [
    ("model input: no second scrub", PS,
     "    text = pr_facts.scrub(untrusted.strip_invisible(text)).strip()\n",
     "    text = untrusted.strip_invisible(text).strip()\n",
     ["Prompt.test_a_secret_never_leaves"]),
    ("tier: another tier's model", PS,
     "    return tier_candidates(TIER, DEFAULT_MODEL)\n",
     '    return tier_candidates("approval_summary", DEFAULT_MODEL)\n',
     ["Prompt.test_the_tier_and_never_chinese_hosted"]),
    ("key: the head left out", PS,
     'str(facts.number), facts.head or "", facts.checks or "",',
     'str(facts.number), "", facts.checks or "",',
     ["Key.test_each_part_changes_the_key"]),
    ("key: the author's body in it", PS,
     '             facts.merge_state or "", str(PROMPT_VERSION)]',
     '             facts.merge_state or "", str(PROMPT_VERSION), facts.body]',
     ["Key.test_nothing_else_changes_it"]),
    ("parser: a missing Does is accepted", PS,
     "    if not does:\n        return None\n",
     "    if not found:\n        return None\n",
     ["Parser.test_a_missing_does_is_a_failure"]),
    ("parser: link syntax kept", PS,
     '    text = _LINK.sub(r"\\1", text)\n',
     "    text = text\n",
     ["Parser.test_links_keep_their_words_only"]),
    ("engine: the answer is not parsed", PS,
     "        return parse_summary(raw)\n",
     '        return {"does": raw, "blocked": ""} if raw else None\n',
     ["Wiring.test_end_to_end_into_the_row"]),
    ("pending rule: gone", PS,
     '        return (facts.head == head and facts.checks == "pending"',
     '        return (False and facts.head == head and facts.checks == "pending"',
     ["PendingRule.test_only_the_checks_moving_to_pending_keeps_the_summary_marked"]),
    ("pending rule: any head", PS,
     '        return (facts.head == head and facts.checks == "pending"',
     '        return (True and facts.checks == "pending"',
     ["PendingRule.test_a_new_head_is_summarized_again"]),
    ("pending rule: any merge state", PS,
     "                and (facts.merge_state == merge_state or facts.merge_state in PENDING_MERGE_STATES))",
     "                and True)",
     ["PendingRule.test_conflicts_while_checks_run_are_summarized_again"]),
    ("per PR: no cap", PS,
     "            if not cached and not self.store.take_pr(pr, day, self.per_pr_daily):\n",
     "            if False:\n",
     ["Caps.test_six_summaries_per_pr_a_day_kept_over_a_restart"]),
    ("per PR: one too many", PS,
     "            if self._pr_counts.get(key, 0) >= cap:\n",
     "            if self._pr_counts.get(key, 0) > cap:\n",
     ["Caps.test_six_summaries_per_pr_a_day_kept_over_a_restart"]),
    ("per PR: an ask that starts nothing keeps its count", PS,
     "        if not cached and state != PENDING:\n",
     "        if False:\n",
     ["Caps.test_a_pr_refused_by_the_day_cap_keeps_its_own_count"]),
    ("ledger: not read at a restart", PS,
     "        return self.store.ledger()\n",
     "        return None\n",
     ["Caps.test_the_calls_cap_survives_a_restart", "Caps.test_the_cost_cap_survives_a_restart"]),
    ("file: a bad file is read as empty", PS,
     '            self.broken = str(exc) or "unreadable"\n',
     "            self._ledger, self._pr_counts, self._entries = None, {}, OrderedDict()\n"
     "            self._loaded = True\n            return\n",
     ["Caps.test_a_bad_file_fails_closed_and_is_never_overwritten"]),
    ("file: a failed write does not close the budget", PS,
     "            self.broken = type(exc).__name__\n            LOG.warning(\"PR summaries: the file cannot be written",
     "            LOG.warning(\"PR summaries: the file cannot be written",
     ["Caps.test_a_write_that_fails_closes_the_budget"]),
    ("cache: in memory only", PS,
     "                         ledger=ledger, cache=store)\n",
     "                         ledger=ledger, cache=None)\n",
     ["Cache.test_it_survives_a_restart_in_a_private_file"]),
    ("cache: a file others can read", PS,
     "            os.chmod(self.path, 0o600)\n",
     "            os.chmod(self.path, 0o644)\n",
     ["Cache.test_it_survives_a_restart_in_a_private_file"]),
    ("row: a late summary overwrites a newer read", PS,
     "            if rec.job is not token:\n",
     "            if False:\n",
     ["Wiring.test_a_late_summary_never_overwrites_a_newer_read"]),
    ("row: the summary feeds the actions", PS,
     "                       actions=list(decision.actions), why=decision.why, method=decision.method)",
     '                       actions=list(decision.actions) if not shown else ["merge"], why=decision.why,'
     " method=decision.method)",
     ["Injection.test_a_body_and_an_obedient_model_change_nothing_that_decides"]),
    ("row: the summary hides the flags", PS,
     '                       flags=flags_detail(facts) if facts.ok("view") else None,',
     '                       flags=flags_detail(facts) if facts.ok("view") and not shown else None,',
     ["Injection.test_a_flagged_pr_stays_flagged_whatever_the_summary_says"]),
    ("row: a moved list still offers the old actions", PS,
     '            if facts.ok("view") and (facts.checks, facts.merge_state) != (d.get("checks"), d.get("merge_state")):',
     "            if False:",
     ["Wiring.test_a_moved_list_reads_again_and_offers_nothing_meanwhile"]),
    ("tidy: a failed list counts as read whole", PRS,
     "            state = {r: (None if r in failures else",
     "            state = {r: (set() if r in failures else",
     ["Tidy.test_each_repositorys_read_state", "Tidy.test_a_failed_list_never_prunes_with_the_real_tidy"]),
    ("start: Daemon.start attaches the summaries", DAEMON,
     "            self.inbox.prs.start()\n",
     "            self.attach_pr_summaries()\n            self.inbox.prs.start()\n",
     ["NoModelAtStart.test_a_started_daemon_has_none_and_calls_nothing",
      "NoModelAtStart.test_main_attaches_it_and_start_does_not"]),
]


def _copy(tmp: Path) -> Path:
    copy = tmp / "repo"
    shutil.copytree(ROOT / "jarvis", copy / "jarvis", ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def _run(copy: Path, tests: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=f"{copy}:{copy / 'tests' / 'v2'}", TMPDIR="/var/tmp")
    proc = subprocess.run([sys.executable, CHECK, *tests], cwd=copy, env=env, capture_output=True,
                          text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def _reason(out: str, test: str) -> str:
    """The failing test's first error line (`AssertionError: ...`, `TypeError: ...`)."""
    name = test.rsplit(".", 1)[1]
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if (line.startswith("FAIL: ") or line.startswith("ERROR: ")) and f" {name} " in f" {line} ":
            for later in lines[i + 1:]:
                if later.startswith(("FAIL: ", "ERROR: ", "=====")) and later is not line:
                    break
                if "Error" in later and ":" in later and not later.startswith(" "):
                    return later.strip()[:160]
            return line.strip()
    return ""


def baseline() -> bool:
    tests = sorted({t for m in MUTANTS for t in m[4]})
    with tempfile.TemporaryDirectory(prefix="ps-mutant-", dir="/var/tmp") as tmp:
        code, out = _run(_copy(Path(tmp)), tests)
    ok = code == 0 and "OK" in out.splitlines()[-1]
    print(f"baseline: {len(tests)} named tests {'pass' if ok else 'FAIL'} unmutated", flush=True)
    if not ok:
        print(out[-2000:])
    return ok


def run(index: int) -> tuple[bool, str]:
    name, rel, old, new, tests = MUTANTS[index]
    with tempfile.TemporaryDirectory(prefix="ps-mutant-", dir="/var/tmp") as tmp:
        copy = _copy(Path(tmp))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        reasons = []
        for test in tests:
            code, out = _run(copy, [test])
            reason = _reason(out, test)
            if code == 0 or not reason:
                return False, f"{test} survived"
            reasons.append(f"{test}: {reason}")
        return True, " | ".join(reasons)


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    if not baseline():
        return 2
    survived = []
    for index in picks:
        killed, detail = run(index)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}\n           {detail}", flush=True)
        if not killed:
            survived.append(MUTANTS[index][0])
    print()
    if survived:
        print(f"{len(survived)} of {len(picks)} mutants survived")
        return 1
    print(f"all {len(picks)} mutants killed, each by its named test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
