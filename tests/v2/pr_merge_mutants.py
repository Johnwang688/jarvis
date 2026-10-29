"""Mutation evidence for the owner's PR actions (PM-D, 2026-10-29).

Each mutant breaks one guard in a temp copy of the package and runs the one
test meant to hold it (in `tests/v2/pr_merge_check.py` unless it names
another check). A mutant is killed only when **that test** fails (FAIL or
ERROR on its own name), so a mutant that breaks something else does not
count; the unmutated copy must pass each named test first (the baseline).
The test's first error line is printed, so the reason can be read.

Free: fakes, temp HOME, no network, no paid call.

    python tests/v2/pr_merge_mutants.py          # all mutants
    python tests/v2/pr_merge_mutants.py 3 7      # by index
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PM = "jarvis/v2/pr_merge.py"
PS = "jarvis/v2/pr_summary.py"
RA = "jarvis/v2/remote_auth.py"
API = "jarvis/v2/remote_api.py"
CHECK = "tests/v2/pr_merge_check.py"
PS_CHECK = "tests/v2/pr_summary_check.py"

# (name, file, old, new, [the tests meant to fail], check file)
MUTANTS = [
    # -- the argv ---------------------------------------------------------------------------
    ("argv: --match-head-commit dropped", PM,
     '    return [gh, "pr", "merge", str(number), "--repo", repo, METHOD_FLAGS[method],\n'
     '            "--match-head-commit", head]',
     '    return [gh, "pr", "merge", str(number), "--repo", repo, METHOD_FLAGS[method]]',
     ["Argv.test_the_exact_merge_argv", "RealGh.test_the_real_runner_never_reads_what_gh_says"], CHECK),
    ("argv: --admin added", PM,
     '"--repo", repo, METHOD_FLAGS[method],\n',
     '"--repo", repo, METHOD_FLAGS[method], "--admin",\n',
     ["Argv.test_the_exact_merge_argv"], CHECK),
    ("argv: --auto added", PM,
     '"--repo", repo, METHOD_FLAGS[method],\n',
     '"--repo", repo, METHOD_FLAGS[method], "--auto",\n',
     ["Argv.test_the_exact_merge_argv"], CHECK),
    ("argv: squash whatever the card said", PM,
     "        method = decision.method if action in MERGE_ACTIONS else None\n",
     '        method = "squash" if action in MERGE_ACTIONS else None\n',
     ["Argv.test_the_exact_merge_argv"], CHECK),
    ("argv: update not bound to the head (LOW 3)", PM,
     '            "-f", f"expected_head_sha={head}"]',
     "            ]",
     ["Argv.test_update_argv_and_result"], CHECK),
    ("argv: update's head sent with -F", PM,
     '            "-f", f"expected_head_sha={head}"]',
     '            "-F", f"expected_head_sha={head}"]',
     ["Argv.test_update_argv_and_result"], CHECK),
    ("update: a 422 counted as done", PM,
     '        if status == "422" and result != "updated":\n',
     "        if False:\n",
     ["Argv.test_update_202_and_422"], CHECK),
    ("queue: auto-merge not checked (MED 1)", PM,
     '                or value["autoMergeRequest"] is not None:\n',
     "                or False:\n",
     ["Queue.test_auto_merge_on_is_refused"], CHECK),
    ("queue: a merge_queue rule not seen (MED 1)", PM,
     '        return any(r["type"] == "merge_queue" for r in rules)\n',
     "        return False\n",
     ["Queue.test_a_merge_queue_base_is_refused"], CHECK),
    ("queue: unreadable rules pass (MED 1)", PM,
     "        rules = self._json(*self._call(rules_argv(gh, repo, base), env, RESULT_TIMEOUT_S, RESULT_CAP))\n",
     "        rules = self._json(*self._call(rules_argv(gh, repo, base), env, RESULT_TIMEOUT_S, RESULT_CAP)) or []\n",
     ["Queue.test_unreadable_is_refused"], CHECK),
    ("queue: the check never runs (MED 1)", PM,
     "            if self.queued(env, gh, repo, number, facts.base, merging=merging):\n",
     "            if False:\n",
     ["Queue.test_a_merge_queue_base_is_refused", "Queue.test_auto_merge_on_is_refused"], CHECK),
    ("queue: an organization's rules never read", PM,
     '        if owner == "User":\n',
     '        if owner in ("User", "Organization"):\n',
     ["Queue.test_an_organizations_repo_reads_its_rules"], CHECK),
    ("queue: an unreadable owner type taken for a user", PM,
     '        if owner == "User":\n',
     '        if owner != "Organization":\n',
     ["Queue.test_an_unreadable_owner_type_refuses"], CHECK),
    ("queue: the owner type never cached", PM,
     "        if cached is not None and now - cached[0] < OWNER_CACHE_S:\n",
     "        if False:\n",
     ["Queue.test_a_users_private_repo_merges_without_the_rules_read"], CHECK),
    ("queue: a queued PR reported as not mergeable", PM,
     '            if value["autoMergeRequest"] is not None:\n                return "queued"\n',
     '            if False:\n                return "queued"\n',
     ["Queue.test_a_queued_pr_reports_queued"], CHECK),
    ("receipt: no intent line before gh (NIT 7)", PM,
     '            write_receipt(inbox, {**base, "phase": "intent",\n',
     '            (lambda *a: None)(inbox, {**base, "phase": "intent",\n',
     ["Argv.test_the_exact_merge_argv"], CHECK),
    ("hud: the route's own Host not checked (NIT 5)", PM,
     '    if not config.own_host_header(handler.headers.get("Host", ""), daemon.face_port):\n',
     "    if False:\n",
     ["Routes.test_the_own_host_check_is_the_routes_own"], CHECK),
    ("phone: options bind the server's row, not the phone's (MED 2)", PM,
     '    if (detail.get("head"), detail.get("card")) != (head, card) \\\n            or action',
     "    if action",
     ["PhoneActs.test_options_bind_what_the_phone_showed"], CHECK),
    ("result: gh's exit word, not a fresh read", PM,
     "        result = self._outcome(action, head, seen, data, status)\n",
     '        result = "merged" if word == "ok" else "not mergeable"\n',
     ["Argv.test_results_come_from_the_fresh_read_never_from_gh"], CHECK),
    ("receipt: readable by others", PM,
     "            os.fchmod(fd, 0o600)\n",
     "            os.fchmod(fd, 0o644)\n",
     ["Argv.test_the_exact_merge_argv"], CHECK),
    # -- the binding ------------------------------------------------------------------------
    ("bind: the head not checked", PM,
     '        if facts.head != head:\n            raise _changed("its head moved")',
     '        if False:\n            raise _changed("its head moved")',
     ["Stale.test_a_stale_head_runs_nothing"], CHECK),
    ("bind: the card not checked", PM,
     "        if not hmac.compare_digest(card_key(shown), card):\n",
     "        if False:\n",
     ["Stale.test_a_stale_card_runs_nothing"], CHECK),
    ("bind: the summary left out of the card", PS,
     '"summary": detail.get("summary"),\n',
     '"summary": None,\n',
     ["Stale.test_a_summary_that_changed_changes_the_card"], CHECK),
    ("bind: a closed PR not refused", PM,
     '        if facts.state != "OPEN":\n',
     "        if False:\n",
     ["Stale.test_closed_or_draft_runs_nothing"], CHECK),
    ("bind: an action not offered runs", PM,
     '        if action not in decision.actions:\n            raise _changed("that action is not offered now")',
     '        if False:\n            raise _changed("that action is not offered now")',
     ["Argv.test_merge_as_is_is_its_own_action"], CHECK),
    ("bind: a 409 pokes no re-read", PM,
     "            if exc.status == 409:\n                self.reread(repo, number)\n",
     "            if exc.status == 409:\n                pass\n",
     ["Stale.test_a_409_pokes_a_re_read"], CHECK),
    ("once: a lock per call, not per PR", PM,
     "        lock = self._lock_for(hold_key(repo, number))\n",
     "        lock = threading.Lock()\n",
     ["Once.test_two_merges_of_one_pr_run_once"], CHECK),
    # -- holds ------------------------------------------------------------------------------
    ("hold: the re-check after the read gone", PM,
     '            if holds.get(repo, number) is not None:            # set while it was read\n',
     "            if False:\n",
     ["Holds.test_a_hold_set_while_reading_is_seen"], CHECK),
    ("hold: the store never holds", PM,
     "            hold = self._holds.get(hold_key(repo, number))\n",
     "            hold = None\n",
     ["Holds.test_a_hold_hides_every_action_and_shows_its_note"], CHECK),
    ("hold: the row ignores it", PS,
     '            decision = pr_facts.decide(facts, hold=None if hold is None else hold["note"])\n',
     "            decision = pr_facts.decide(facts)\n",
     ["Holds.test_a_hold_hides_every_action_and_shows_its_note"], CHECK),
    ("hold: an unreadable file holds nothing", PM,
     "            self._holds, self.broken = {}, type(exc).__name__\n",
     "            self._holds, self.broken = {}, None\n",
     ["Holds.test_a_hold_file_that_cannot_be_read_holds_everything"], CHECK),
    # -- the phone --------------------------------------------------------------------------
    ("phone: the assertion not verified", PM,
     "    handler.remote_auth.answer_verify(session, assertion, subject(repo, number, head, card, action), action)\n",
     "    pass\n",
     ["PhoneActs.test_cross_family_assertions_are_refused_both_ways"], CHECK),
    ("phone: the card left out of the subject", PM,
     '    text = "\\n".join((SUBJECT_DOMAIN, repo, str(number), head, card, action))\n',
     '    text = "\\n".join((SUBJECT_DOMAIN, repo, str(number), head, action))\n',
     ["PhoneActs.test_subject_binding"], CHECK),
    ("phone: limited and flagged acts", PM,
     "        if limited and pr_facts.flagged(facts):\n",
     "        if False:\n",
     ["PhoneActs.test_limited_and_flagged_is_refused"], CHECK),
    ("phone: limited and flagged gets a challenge", PM,
     '    if not secure.get("ok") and (not isinstance(flags, dict) or flags.get("flagged") is not False):\n',
     "    if False:\n",
     ["PhoneActs.test_limited_and_flagged_is_refused"], CHECK),
    ("phone: the scrub not re-checked at the act", PM,
     '        if via == "phone" and phone_withheld(parts, files_detail(facts)):\n',
     "        if False:\n",
     ["PhoneActs.test_the_phone_scrub_sends_it_to_the_desk"], CHECK),
    ("auth: the pr family shares the approval budget", RA,
     '                        else "pr" if pr else "approval")',
     '                        else "approval")',
     ["PhoneActs.test_the_challenge_budget_is_its_own"], CHECK),
    ("auth: a pr subject takes an approval's answer", RA,
     "        allowed = (TASK_DECISIONS if task else DEFER_DECISIONS if defer else PR_DECISIONS if pr\n",
     "        allowed = (TASK_DECISIONS if task else DEFER_DECISIONS if defer else PR_DECISIONS + ANSWER_DECISIONS if pr\n",
     ["PhoneActs.test_cross_family_assertions_are_refused_both_ways"], CHECK),
    ("api: the phone keeps the GitHub link", API,
     '"summary_state", "summary_note", "summary_reason", "why", "method", "actions", "card",\n           "hold", "fix"),',
     '"summary_state", "summary_note", "summary_reason", "why", "method", "actions", "card",\n           "hold", "fix", "url"),',
     ["Wiring.test_the_desk_and_phone_projections_keep_their_fields"], PS_CHECK),
]


def _copy(tmp: Path) -> Path:
    copy = tmp / "repo"
    shutil.copytree(ROOT / "jarvis", copy / "jarvis", ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def _run(copy: Path, check: str, tests: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=f"{copy}:{copy / 'tests' / 'v2'}", TMPDIR="/var/tmp")
    proc = subprocess.run([sys.executable, check, *tests], cwd=copy, env=env, capture_output=True,
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
    ok = True
    with tempfile.TemporaryDirectory(prefix="pm-mutant-", dir="/var/tmp") as tmp:
        copy = _copy(Path(tmp))
        for check in sorted({m[5] for m in MUTANTS}):
            tests = sorted({t for m in MUTANTS if m[5] == check for t in m[4]})
            code, out = _run(copy, check, tests)
            passed = code == 0 and "OK" in out.splitlines()[-1]
            print(f"baseline: {len(tests)} named tests of {check} {'pass' if passed else 'FAIL'} unmutated",
                  flush=True)
            if not passed:
                print(out[-2000:])
                ok = False
    return ok


def run(index: int) -> tuple[bool, str]:
    name, rel, old, new, tests, check = MUTANTS[index]
    with tempfile.TemporaryDirectory(prefix="pm-mutant-", dir="/var/tmp") as tmp:
        copy = _copy(Path(tmp))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        reasons = []
        for test in tests:
            code, out = _run(copy, check, [test])
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
