"""Mutation evidence for a PR's Fix (PM-E, 2026-10-29).

Each mutant breaks one guard in a temp copy of the package and runs the
tests of `tests/v2/pr_fix_check.py` meant to hold it. A mutant is killed only
when **each named test** fails (FAIL or ERROR on its own name), so a mutant
that breaks something else does not count; the unmutated copy must pass every
named test first (the baseline). Every DENY family of the PR scope has its
own mutant.

Free: fakes, temp HOME, real git on temp repositories only, no network, no
paid call.

    python tests/v2/pr_fix_mutants.py          # all mutants
    python tests/v2/pr_fix_mutants.py 3 7      # by index
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FX = "jarvis/v2/pr_fix.py"
PERM = "jarvis/v2/permissions.py"
STORES = "jarvis/v2/stores.py"
RUNNER = "jarvis/v2/runner.py"
WT = "jarvis/v2/worktrees.py"
DAEMON = "jarvis/v2/daemon.py"
INBOX = "jarvis/v2/inbox.py"
CHECK = "tests/v2/pr_fix_check.py"

SCOPE_CI = "Scope.test_ci_and_scanner_config_writes_are_refused"
SCOPE_ASK = "Scope.test_a_push_the_gate_cannot_read_is_asked"

# (name, file, old, new, [the tests meant to fail])
MUTANTS = [
    # -- the PR scope's DENY families (PM-20) ------------------------------------------------------
    ("DENY destination: any refspec destination", FX,
     '        if dst in (f"refs/heads/{branch}", branch, f"heads/{branch}"):\n            return\n',
     "        if True:\n            return\n",
     ["Scope.test_another_destination_is_refused"]),
    ("DENY destination: a bare ref that is not the branch", FX,
     '            self.deny(S_DEST, f"pushes {_shown(src)}, not the PR\'s branch {branch}")\n',
     "            pass\n",
     ["Scope.test_another_destination_is_refused"]),
    ("DENY remote: a remote that points at another repository", FX,
     "                if found is None or found.lower() != self.repo.lower():\n",
     "                if found is None:\n",
     ["Scope.test_remote_tricks_are_refused"]),
    ("DENY remote: a name that is no remote", FX,
     "            if not urls:\n                self.deny(",
     "            if not urls:\n                return True\n                self.deny(",
     ["Scope.test_remote_tricks_are_refused"]),
    ("DENY remote: pushurl ignored", FX,
     '            urls = [v for k, v in mine if k == "pushurl"] or [v for k, v in mine if k == "url"]\n',
     '            urls = [v for k, v in mine if k == "url"]\n',
     ["Scope.test_remote_tricks_are_refused"]),
    ("DENY remote: --repo allowed", FX,
     '    "repo": S_REMOTE, "receive-pack": S_PROGRAM,',
     '    "repo": "value", "receive-pack": S_PROGRAM,',
     ["Scope.test_remote_tricks_are_refused"]),
    ("DENY all: --all, --mirror, --tags allowed", FX,
     '    "all": S_ALL, "branches": S_ALL, "mirror": S_ALL, "tags": S_ALL, "follow-tags": S_ALL,\n',
     '    "all": None, "branches": None, "mirror": None, "tags": None, "follow-tags": None,\n',
     ["Scope.test_every_ref_at_once_is_refused"]),
    ("DENY all: a mirror remote", FX,
     '            if any(k == "mirror" and v.lower()',
     '            if False and any(k == "mirror" and v.lower()',
     ["Scope.test_every_ref_at_once_is_refused"]),
    ("DENY force: the long options", FX,
     '    "force": S_FORCE, "force-with-lease": S_FORCE, "force-if-includes": S_FORCE,\n',
     '    "force": None, "force-with-lease": None, "force-if-includes": None,\n',
     ["Scope.test_force_is_refused"]),
    ("DENY force: -f", FX,
     '                if c == "f":\n', '                if c == "F":\n',
     ["Scope.test_force_is_refused"]),
    ("DENY force: a + refspec", FX,
     '        if spec.startswith("+"):\n', '        if spec.startswith("++"):\n',
     ["Scope.test_force_is_refused"]),
    ("DENY delete: the long options", FX,
     '    "delete": S_DELETE, "prune": S_DELETE,\n', '    "delete": None, "prune": None,\n',
     ["Scope.test_delete_is_refused"]),
    ("DENY delete: -d", FX,
     '                if c == "d":\n', '                if c == "D":\n',
     ["Scope.test_delete_is_refused"]),
    ("DENY delete: an empty source", FX,
     "        if colon and not src:\n", "        if False:\n",
     ["Scope.test_delete_is_refused"]),
    ("DENY program: --receive-pack and --exec", FX,
     '"receive-pack": S_PROGRAM, "exec": S_PROGRAM,', '"receive-pack": "value", "exec": "value",',
     ["Scope.test_push_programs_are_refused"]),
    ("DENY program: send-pack and http-push", FX,
     '        elif sub in ("send-pack", "http-push"):\n', "        elif sub in ():\n",
     ["Scope.test_push_programs_are_refused"]),
    ("DENY gh pr verbs", FX,
     '_GH_PR_DENIED = frozenset({"merge", "close", "edit", "ready", "review"})\n',
     "_GH_PR_DENIED = frozenset()\n",
     ["Scope.test_gh_pr_verbs_are_refused"]),
    ("DENY gh api: an explicit method", FX,
     '            if method.strip().upper() != "GET":\n', "            if False:\n",
     ["Scope.test_gh_api_writes_are_refused"]),
    ("DENY gh api: fields, --input and graphql", FX,
     '        if fields or (endpoint is not None and endpoint.strip("/").lower() == "graphql"):\n',
     "        if False:\n",
     ["Scope.test_gh_api_writes_are_refused"]),
    ("DENY gh writes: repo sync", FX,
     '        elif cmd == "repo" and verb == "sync":\n', "        elif False:\n",
     ["Scope.test_gh_writes_that_silence_ci_are_refused"]),
    ("DENY CI: the path test", FX,
     "        if ci_path(target):\n            self.deny(S_CI,",
     "        if False:\n            self.deny(S_CI,",
     [SCOPE_CI]),
    ("DENY CI: redirects not read", FX,
     "        for match in mg._REDIRECT_TARGET.finditer(text):\n            self.file(match.group(1))\n",
     "",
     [SCOPE_CI]),
    ("DENY Codex", FX,
     '    if provider == "codex":\n        j.deny(S_CODEX,', '    if provider == "codex-x":\n        j.deny(S_CODEX,',
     ["Scope.test_a_codex_session_is_refused_every_call", "Permit.test_codex_is_refused_for_a_fix"]),
    ("DENY broken scope: judged anyway", FX,
     "    if not valid_scope(scope):\n        return ScopeHit(DENY, S_BROKEN,",
     "    if False:\n        return ScopeHit(DENY, S_BROKEN,",
     ["Scope.test_a_scope_that_cannot_be_read_refuses_everything"]),
    # -- what is asked ----------------------------------------------------------------------------
    ("ASK: a line that changes config", FX,
     "        if changes_config:\n            self.ask(", "        if False:\n            self.ask(",
     [SCOPE_ASK]),
    ("ASK: text the gate cannot read", FX,
     "        if line.runs_text and _PUSHISH.search(text):\n", "        if False:\n",
     [SCOPE_ASK]),
    ("ASK: a git setting on the line", FX,
     "            if risky:\n                self.ask(", "            if False:\n                self.ask(",
     [SCOPE_ASK]),
    ("ASK: a program the walk does not follow starts git", FX,
     "                    and any(mg._stem_could_be(a, _GIT_NAMES)[0] in _GIT_NAMES for a in args)\n",
     "                    and False\n",
     [SCOPE_ASK]),
    ("ASK: insteadOf rewrites", FX,
     '            if any(sec == "url" and kv.split("=", 1)[0] in ("insteadof", "pushinsteadof")',
     '            if False and any(sec == "url" and kv.split("=", 1)[0] in ("insteadof", "pushinsteadof")',
     ["Scope.test_config_that_rewrites_or_includes_is_asked"]),
    # -- the permit -------------------------------------------------------------------------------
    ("permit: the scope not read", PERM,
     "        scoped = _pr_scope_hit(ctx, tool, args, judged, also_commands, cwd, shell_cwd)\n",
     "        scoped = None\n",
     ["Permit.test_a_deny_is_never_asked_and_beats_every_allow"]),
    ("permit: a DENY asked instead", PERM,
     '            if scoped.decision != "ask":\n', "            if False:\n",
     ["Permit.test_a_deny_is_never_asked_and_beats_every_allow"]),
    ("permit: an unreadable fix record opens up", FX,
     "    except StoreBroken:\n        return {}\n", "    except StoreBroken:\n        return None\n",
     ["Permit.test_a_task_that_cannot_be_read_takes_its_fixs_scope_or_fails_closed"]),
    ("permit: a judge that breaks allows", PERM,
     "        return pr_fix.ScopeHit(pr_fix.DENY, pr_fix.S_BROKEN,\n",
     "        return None and pr_fix.ScopeHit(pr_fix.DENY, pr_fix.S_BROKEN,\n",
     ["Permit.test_a_judge_that_breaks_refuses"]),
    # -- the task record and the runner -----------------------------------------------------------
    ("store: a save drops the scope", STORES,
     "                obj.pr_scope = previous.pr_scope\n", "                pass\n",
     ["TaskRecord.test_the_scope_is_set_at_create_and_kept_by_every_save"]),
    ("runner: a fix's thread opens on Codex", RUNNER,
     "        if task.pr_scope is not None and provider != ProviderName.CLAUDE:\n",
     "        if False:\n",
     ["TaskRecord.test_the_runner_never_opens_a_fixs_thread_on_codex"]),
    ("runner: a fix resumes on Codex", RUNNER,
     "        if provider is not None and task.pr_scope is not None and ProviderName(provider) != ProviderName.CLAUDE:\n",
     "        if False:\n",
     ["TaskRecord.test_a_fix_is_never_resumed_on_codex"]),
    # -- the brief (PM-19) ------------------------------------------------------------------------
    ("brief: the model's summary fed in", FX,
     "            brief = build_brief(facts)\n",
     "            brief = build_brief(facts) + json.dumps(detail.get(\"summary\"))\n",
     ["Start.test_the_brief_holds_no_model_text"]),
    ("brief: the PR's title fed in", FX,
     '        f"- Repository: {facts.repo}",\n', '        f"- Repository: {facts.repo} {facts.title}",\n',
     ["Brief_.test_built_from_code_facts_only"]),
    ("brief: a routing override let through", FX,
     "    if _USE.search(clean):\n", "    if False:\n",
     ["Brief_.test_a_name_that_reads_as_a_routing_override_is_withheld"]),
    # -- start (PM-17, PM-21) ---------------------------------------------------------------------
    ("start: two at a time", FX,
     "                if live:\n", "                if False:\n",
     ["Start.test_one_fix_at_a_time"]),
    ("start: a fourth fix", FX,
     "            if _counted(fixes) >= MAX_FIXES:\n", "            if _counted(fixes) > MAX_FIXES:\n",
     ["Start.test_at_most_three"]),
    ("start: two projects", FX,
     "        if len(ids) > 1:\n", "        if False:\n",
     ["Start.test_refused_without_exactly_one_project"]),
    ("start: no Claude in the roster", FX,
     '            if ProviderName.CLAUDE not in getattr(self.daemon, "providers", {}):\n',
     "            if False:\n",
     ["Start.test_refused_when_only_codex_could_run_it"]),
    ("start: the hold not read", FX,
     "            decision = decide(facts, self.hold(repo, number), limited)\n",
     "            decision = decide(facts, None, limited)\n",
     ["Start.test_a_hold_hides_fix"]),
    ("start: auto-merge not read", FX,
     "            if self._auto(repo, number):\n", "            if False:\n",
     ["Start.test_auto_merge_on_is_refused"]),
    ("start: a phone counts as fully checked", FX,
     '        limited = (via != "hud") if limited is None else bool(limited)\n',
     "        limited = False if limited is None else bool(limited)\n",
     ["Start.test_a_limited_phone_may_fix_except_a_flagged_pr"]),
    ("start: the card not checked", FX,
     "            if not hmac.compare_digest(str(detail.get(\"card\") or \"\"), card):\n", "            if False:\n",
     ["Start.test_a_stale_head_or_card_is_refused"]),
    ("start: no ceilings", FX,
     "                                              ceilings=dict(CEILINGS),\n",
     "                                              ceilings={},\n",
     ["Start.test_one_call_makes_a_claude_task_under_the_scope"]),
    ("start: not pinned to Claude", FX,
     "                                              provider_override=ProviderName.CLAUDE)\n",
     "                                              provider_override=None)\n",
     ["Start.test_one_call_makes_a_claude_task_under_the_scope"]),
    ("start: Fix offered while one runs", FX,
     "            if FIX in actions and any(self._live_cached(f) for f in fixes):\n",
     "            if False:\n",
     ["Start.test_one_call_makes_a_claude_task_under_the_scope"]),
    # -- the ends (PM-22, PM-23) ------------------------------------------------------------------
    ("done: a rewritten branch gets a card", FX,
     "        if ev.get(\"ancestor\") is False:\n", "        if False:\n",
     ["Done.test_a_rewritten_branch_gets_no_card"]),
    ("done: a cancelled task is still running", FX,
     '        if info.state not in ("done", "failed", "cancelled", "blocked"):\n',
     '        if info.state not in ("done", "failed", "blocked"):\n',
     ["Done.test_a_cancelled_task_that_pushed_still_gets_its_card"]),
    ("interdiff: hunk headers and context compared", FX,
     '        if line.startswith(("+", "-")) or line.startswith(_KEEP_PREFIXES):\n',
     "        if True:\n",
     ["Done.test_a_merge_of_the_base_reads_as_nothing"]),
    ("interdiff: CI change not seen", FX,
     "    ci = any(ci_path(p) or FLAG_CI in path_flags(p) or FLAG_SCANNER in path_flags(p) for p in paths)\n",
     "    ci = False\n",
     ["Done.test_ci_change_is_amber"]),
    ("interdiff: not scrubbed", FX,
     "    return Interdiff(tuple(paths), scrub(text), complete,",
     "    return Interdiff(tuple(paths), text, complete,",
     ["Done.test_a_secret_in_the_interdiff_never_reaches_the_model"]),
    ("compare: the base's commits counted", FX,
     "        at = parents[at][0] if parents[at] else None\n",
     "        at = parents[at][-1] if parents[at] else None\n",
     ["Done.test_parse_compare_counts_the_fixs_own_commits"]),
    # -- the push (Q2, PM-27) ---------------------------------------------------------------------
    ("push: an ended fix stays rank 5", FX,
     '                row["rank"] = 4\n', '                row["rank"] = 5\n',
     ["Quiet.test_an_ended_fix_pushes_once_and_never_at_night", "Done.test_stopped_with_no_push"]),
    ("push: rank 4 for good", FX,
     "                    self.clock() - _parse_utc(ended) < ATTENTION_S:\n",
     "                    self.clock() - _parse_utc(ended) < ATTENTION_S * 100:\n",
     ["Quiet.test_an_ended_fix_pushes_once_and_never_at_night"]),
    ("push: a start that failed pushes", FX,
     "            if ended and view[\"state\"] not in (_LIVE - {BLOCKED}) | {START_FAILED} and \\\n",
     "            if ended and view[\"state\"] not in (_LIVE - {BLOCKED}) and \\\n",
     ["Start.test_a_start_that_fails_makes_no_fix_and_counts_none"]),
    # -- the worktree (PM-18) ---------------------------------------------------------------------
    ("worktree: the head not checked", WT,
     "        if got != head:\n            _hardened(root, \"update-ref\", \"-d\", ref)\n",
     "        if False:\n            _hardened(root, \"update-ref\", \"-d\", ref)\n",
     ["Worktree.test_a_head_that_is_not_the_bound_one_makes_nothing"]),
    ("worktree: risky config allowed", WT,
     "        if risk:\n            raise WorktreeError(", "        if False:\n            raise WorktreeError(",
     ["Worktree.test_a_repository_whose_config_names_a_program_is_refused"]),
    ("worktree: plain git, hooks on", WT,
     "    code, out, err = gitops._git(root, list(args), timeout=timeout)\n",
     "    _p = subprocess.run([\"git\", *args], cwd=root, capture_output=True, text=True)\n"
     "    code, out, err = _p.returncode, _p.stdout, _p.stderr\n",
     ["Worktree.test_no_hook_runs"]),
    ("mirror: a moved head accepted", FX,
     "        if got != head:\n            raise FixRefused(409, \"the pull request's head moved since you read it\")\n",
     "        if False:\n            raise FixRefused(409, \"the pull request's head moved since you read it\")\n",
     ["Worktree.test_fill_mirror_refuses_a_moved_head"]),
    # -- no agent path, nothing at start ----------------------------------------------------------
    ("routes: mounted before owner_only", INBOX,
     "    from .projects import owner_only\n    owner_only(handler, daemon)\n",
     "    from .projects import owner_only\n    from . import pr_fix as _early\n"
     "    _hit = _early.hud_route(handler, daemon, parts, query)\n    if _hit is not None:\n"
     "        return _hit\n    owner_only(handler, daemon)\n",
     ["NoAgentPath.test_only_the_owners_hud_reaches_the_routes"]),
    ("start: Daemon.start attaches the fixes", DAEMON,
     "            self.inbox.prs.start()\n",
     "            self.attach_pr_fixes()\n            self.inbox.prs.start()\n",
     ["NoStart.test_main_attaches_it_and_start_does_not",
      "NoStart.test_a_started_daemon_has_none_and_the_routes_say_so"]),
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
    with tempfile.TemporaryDirectory(prefix="fx-mutant-", dir="/var/tmp") as tmp:
        code, out = _run(_copy(Path(tmp)), tests)
    ok = code == 0 and "OK" in out.splitlines()[-1]
    print(f"baseline: {len(tests)} named tests {'pass' if ok else 'FAIL'} unmutated", flush=True)
    if not ok:
        print(out[-2000:])
    return ok


def run(index: int) -> tuple[bool, str]:
    name, rel, old, new, tests = MUTANTS[index]
    with tempfile.TemporaryDirectory(prefix="fx-mutant-", dir="/var/tmp") as tmp:
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
