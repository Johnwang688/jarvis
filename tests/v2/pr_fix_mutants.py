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
MG = "jarvis/v2/merge_gate.py"
CHECK = "tests/v2/pr_fix_check.py"

SCOPE_CI = "Scope.test_ci_and_scanner_config_writes_are_refused"
SCOPE_ASK = "Scope.test_a_push_the_gate_cannot_read_is_asked"
SC = "Scope.test_ci_moves_and_removals_are_refused"
CR = "Scope.test_credential_folders_beyond_literal_paths"
IN = "Scope.test_inline_code_in_any_spelling"
PIN = "Scope.test_the_credential_helper_is_pinned"

# (name, file, old, new, [the tests meant to fail])
# Two guards have no mutant of their own because a later guard asks the same
# lines (equivalent mutants): "a line that changes config" before a push (the
# config or remote write itself is asked, M-5) and "a program the walk does
# not follow starts git" (`sudo git push`: an unread program beside a push).
# Two more have no mutant because the literal credential-path check on the
# raw line already refuses every hermetic case: `_reach` on the path as
# written (`/proc/self/cwd/..`) and `~user` (which reads the real passwd
# entry, not the test's HOME).
# Since #339 one more is equivalent: `.gitconfig` in the git-config list,
# because `~/.gitconfig` is a credential store and the credential rule takes
# it first; and the H-1 mutant is killed by the scope's own test, since the
# permit's layer 1 now refuses a read tool's credential path before the scope.
MUTANTS = [
    # -- the PR scope's DENY families (PM-20) ------------------------------------------------------
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
    ("DENY Codex", FX,
     '    if provider == "codex":\n        j.deny(S_CODEX,', '    if provider == "codex-x":\n        j.deny(S_CODEX,',
     ["Scope.test_a_codex_session_is_refused_every_call", "Permit.test_codex_is_refused_for_a_fix"]),
    ("DENY broken scope: judged anyway", FX,
     "    if not valid_scope(scope):\n        return ScopeHit(DENY, S_BROKEN,",
     "    if False:\n        return ScopeHit(DENY, S_BROKEN,",
     ["Scope.test_a_scope_that_cannot_be_read_refuses_everything"]),
    # -- what is asked ----------------------------------------------------------------------------
    ("ASK: text the gate cannot read", FX,
     "        if line.runs_text and _PUSHISH.search(text):\n", "        if False:\n",
     [SCOPE_ASK]),
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
    # -- the review of #337 (2026-10-29): each new guard -------------------------------------------
    ("M-3: any full destination", FX,
     '        if dst == f"refs/heads/{branch}":\n            return\n',
     "        if dst.startswith(\"refs/heads/\"):\n            return\n",
     ["Scope.test_another_destination_is_refused"]),
    ("M-3: a short destination accepted", FX,
     '        if dst == f"refs/heads/{branch}":\n            return\n',
     '        if dst in (f"refs/heads/{branch}", branch):\n            return\n',
     ["Scope.test_another_destination_is_refused"]),
    ("M-3: a bare name accepted", FX,
     '            self.deny(S_DEST, f"{_shown(spec)} does not name the destination in full")\n            return\n',
     "            return\n",
     ["Scope.test_another_destination_is_refused"]),
    ("DENY delete: an empty or all-zero source", FX,
     '        if colon and (not src or re.fullmatch(r"0{4,64}", src)):\n',
     "        if colon and not src:\n",
     ["Scope.test_delete_is_refused"]),
    ("DENY gh api: fields, --input and graphql", FX,
     '        if fields or (endpoint is not None and endpoint.strip("/").lower().endswith("graphql")):\n',
     "        if False:\n",
     ["Scope.test_gh_api_writes_are_refused"]),
    ("DENY gh api: abbreviations not read", FX,
     "            word = _gh_api_long(str(arg))\n", "            word = str(arg)\n",
     ["Scope.test_gh_api_writes_are_refused"]),
    ("DENY gh api: a method-override header", FX,
     '                if "method" in value.lower() or mg._wild(rest[k + 1] if k + 1 < len(rest) else ""):\n',
     "                if False:\n",
     ["Scope.test_gh_api_writes_are_refused"]),
    ("M-4: any gh command allowed", FX,
     "        elif cmd not in _GH_READS:\n", "        elif False:\n",
     ["Scope.test_gh_reads_only"]),
    ("M-4: any verb of a read command allowed", FX,
     "        elif _GH_READS[cmd] is not None and verb is not None and verb not in _GH_READS[cmd]:\n",
     "        elif False:\n",
     ["Scope.test_gh_reads_only"]),
    ("M-4: gh browse opens a browser", FX,
     '            if not any(str(w) in ("-n", "--no-browser") for w in rest):\n', "            if False:\n",
     ["Scope.test_gh_reads_only"]),
    ("M-2: .github itself is not CI", FX,
     'CI_TOP_DIRS = frozenset({".github", ".circleci", ".buildkite"})\n',
     'CI_TOP_DIRS = frozenset({".circleci", ".buildkite"})\nCI_DIRS = None\n',
     [SC]),
    ("M-2: removals not read", FX,
     '            if stem in ("rm", "unlink", "rmdir", "mv", "shred", "truncate"):\n', "            if False:\n",
     [SC]),
    ("M-2: git rm/mv/checkout/restore/clean not read", FX,
     "            self._git_paths(sub, words, dirs, moved)\n", "            pass\n",
     [SC]),
    ("M-2: the worktree itself is not a folder that holds .github", FX,
     '            if ci_path(rel) or ci_path(target) or (destroys and rel in (".", "")):\n',
     "            if ci_path(rel) or ci_path(target):\n",
     [SC]),
    ("M-2: relative targets not placed", FX,
     "            bases = [Path(os.path.normpath(Path(d) / text)) for d in dirs]\n",
     "            bases = [Path(os.path.normpath(Path(\"/nowhere\") / text))]\n",
     [SC]),
    ("M-2: shell escapes kept", FX,
     '    for parts in (_parts(path), _parts(str(path).replace("\\\\", ""))):   # a Windows path, or shell escapes\n',
     "    for parts in (_parts(path),):\n",
     [SC]),
    ("M-2: redirects not read", FX,
     "        for match in mg._REDIRECT_TARGET.finditer(text):\n            self.file(match.group(1), line.dirs)\n",
     "", [SC, "Scope.test_ci_and_scanner_config_writes_are_refused"]),
    ("M-2: a line that writes and names CI is not asked", FX,
     "        if (writer or interpreter or mg._REDIRECT_TARGET.search(text)) and any(\n",
     "        if False and any(\n",
     [SC]),
    ("M-2: ci_changed by the new name only", FX,
     "            names.update(name for name, _families in patch.flags)\n", "            pass\n",
     ["Done.test_ci_moved_away_is_amber"]),
    ("interdiff: CI change not seen", FX,
     "    ci = any(ci_path(p) or FLAG_CI in path_flags(p) or FLAG_SCANNER in path_flags(p) for p in names)\n",
     "    ci = False\n",
     ["Done.test_ci_change_is_amber"]),
    ("H-1: a credential folder read", FX,
     "        if text and (_under_credential_dir(text) or (self.root is not None",
     "        if False and (_under_credential_dir(text) or (self.root is not None",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: read tools not judged", FX,
     '    if tool in READ_TOOLS or tool == "LS":\n        j.read(tool, args)\n', "",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: a credential folder on a line", FX,
     "            if fragment in text or fragment in flat:\n", "            if False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: git credential", FX,
     '        elif sub.startswith("credential"):\n', "        elif False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: gh auth", FX,
     '        if cmd == "auth":\n            self.deny(S_CRED,', '        if cmd == "auth-x":\n            self.deny(S_CRED,',
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: credential config", FX,
     '            if any("credential" in w.lower() for w in words):\n', "            if False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: a credential helper set with -c", FX,
     '                elif key.lower().startswith("credential"):\n', "                elif False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: traces", FX,
     "        if _TRACE_ENV.search(text):\n", "        if False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("H-1: fetchers to GitHub", FX,
     "        if (fetcher or interpreter) and _GITHUB_HOST.search(flat):\n", "        if False:\n",
     ["Scope.test_the_owners_github_credential_is_out_of_reach"]),
    ("M-1: any repository may take the push", FX,
     "        if not repos or any(tuple(Path(p).resolve() for p in pair)\n",
     "        if not repos or False and any(tuple(Path(p).resolve() for p in pair)\n",
     ["Scope.test_another_repository_takes_no_push"]),
    ("M-1: git pointed elsewhere", FX,
     "            if name in _GIT_ELSEWHERE:\n", "            if False:\n",
     ["Scope.test_another_repository_takes_no_push"]),
    ("M-1: GIT_DIR and kin", FX,
     "        if mg._GIT_ENV.search(text):\n", "        if False:\n",
     ["Scope.test_another_repository_takes_no_push"]),
    ("M-1: merge_gate misses a bare repository", MG,
     "            elif _is_git_dir(folder):\n", "            elif False:\n",
     ["Scope.test_merge_gate_finds_a_bare_repository_as_git_does",
      "Scope.test_another_repository_takes_no_push"]),
    ("M-1: merge_gate reads --bare as the folder's repository", MG,
     '        if text == "--bare":\n            dirs = UNKNOWN', '        if False:\n            dirs = UNKNOWN',
     ["Scope.test_merge_gate_finds_a_bare_repository_as_git_does"]),
    ("M-5: config that runs a program", FX,
     '                if (sec == "url" or (sec == "core" and key in ("hookspath", "sshcommand", "gitproxy"))\n',
     "                if False and (sec == \"url\" or (sec == \"core\" and key in (\"hookspath\",))\n",
     ["Scope.test_config_that_runs_programs_or_rewrites_refuses_the_push"]),
    ("M-5: helpers not compared", FX,
     "            if want is not None and helper_pin(entries)[0] != want:\n", "            if False:\n",
     ["Scope.test_credential_helpers_changed_since_the_fix_refuse_the_push"]),
    ("M-5: config writes not asked", FX,
     '            elif self._git_changes_config(["config", *words]):\n', "            elif False:\n",
     ["Scope.test_git_config_and_remote_writes_are_asked"]),
    ("M-5: git's own files", FX,
     "        if git_internal(target) or any(git_internal(r) for r in (self._rel(target, dirs) or [])):\n",
     "        if False:\n",
     ["Scope.test_writes_to_gits_own_files_are_refused"]),
    ("L-4: git subcommands that run a line", FX,
     "        elif sub in _GIT_RUNS and (_GIT_RUNS[sub] is None or any(\n",
     "        elif False and (_GIT_RUNS[sub] is None or any(\n",
     ["Scope.test_programs_that_run_a_line_or_outlive_the_call_are_asked"]),
    ("L-4: detachers", FX,
     "        if stem in _DETACHERS:\n", "        if False:\n",
     ["Scope.test_programs_that_run_a_line_or_outlive_the_call_are_asked"]),
    ("L-4: pager and editor overrides", FX,
     "        if _EDITOR_ENV.search(text):\n", "        if False:\n",
     ["Scope.test_programs_that_run_a_line_or_outlive_the_call_are_asked"]),
    ("L-4: a git -c that may run a program", FX,
     "                elif _EDITOR_KEYS.match(key) or not mg._benign_setting(key, str(value).partition(\"=\")[2]):\n",
     "                elif False:\n",
     ["Scope.test_programs_that_run_a_line_or_outlive_the_call_are_asked"]),
    ("ASK: an unread program beside a push", FX,
     "        elif executor and _PUSHISH.search(text):\n", "        elif False:\n",
     [SCOPE_ASK]),
    ("ASK: a runner's script named for a push", FX,
     "            if stem in rules._RUNNER_SPECS and _PUSHISH.search(\" \".join(words)):\n",
     "            if False:\n",
     [SCOPE_ASK]),
    ("L-1: a default or release branch", FX,
     "            if protected:\n                raise FixRefused(409,", "            if False:\n                raise FixRefused(409,",
     ["Start.test_a_default_or_release_branch_gets_no_fix"]),
    ("L-1: the remote's default branch", FX,
     "        if isinstance(found, tuple) and branch in mg._remote_heads(found[1]):\n", "        if False:\n",
     ["Start.test_a_default_or_release_branch_gets_no_fix"]),
    ("L-2: the task id recorded after the start", FX,
     "            self.store.update(key, started, task_id=task.id)\n            ensure = self._ensure or ensure_pr\n",
     "            ensure = self._ensure or ensure_pr\n",
     ["Start.test_the_task_id_is_recorded_before_the_task_starts"]),
    ("L-3: a failed start keeps its worktree", FX,
     "                worktrees.remove(task, stores, force=True)\n", "                pass\n",
     ["Start.test_a_failed_start_removes_its_worktree_and_branch"]),
    ("NIT: the fallback reads the default root", FX,
     "    if root is None and _ROOT:\n        root = _ROOT[-1]\n", "",
     ["Start.test_the_permits_fallback_reads_the_fixes_root"]),

    # -- the delta review of #337 (2026-10-29) ------------------------------------------------------
    ("delta H-1: inside a credential folder by a glob or after cd", FX,
     '                    if kind == "inside" or (kind == "ancestor" and recursive):\n',
     '                    if kind == "ancestor" and recursive:\n', [CR]),
    ("delta H-1: an ancestor under a recursive reader", FX,
     '                    if kind == "inside" or (kind == "ancestor" and recursive):\n',
     '                    if kind == "inside":\n', [CR]),
    ("delta H-1: globs not matched", FX,
     "                    kind = (_glob_reach(path) if _GLOB_CHARS.search(base)\n",
     "                    kind = (_reach(path) if _GLOB_CHARS.search(base)\n", [CR]),
    ("delta H-1: braces not expanded", FX,
     "            variants = protected_state._brace_expand(text)\n", "            variants = [text]\n", [CR]),
    ("delta H-1: relative paths not placed", FX,
     "                         [str(Path(d) / variant) for d in dirs])\n", "                         [])\n", [CR]),
    ("delta H-1: home not expanded", FX,
     "            text = _expand_home(raw)\n", "            text = raw\n", [CR]),
    ("delta H-1: same-line variables not read", FX,
     "                if again != text:\n                    self.line(again, where, _again=True)\n", "", [CR]),
    ("delta H-1: a command-substituted path not asked", FX,
     '                if "/" in raw or reader:\n                    self.ask("a path built by a command substitution',
     '                if reader:\n                    self.ask("a path built by a command substitution', [CR]),
    ("delta H-1: recursive readers", FX,
     "    if stem in _RECURSIVE_READERS:\n        return True\n", "", [CR]),
    ("delta H-1: recursive flags", FX,
     "        elif w.startswith(\"-\") and any(c in letters for c in w[1:]):\n", "        elif False:\n", [CR]),
    ("delta H-1: Grep and Glob on an ancestor", FX,
     '            if kind == "inside" or (kind == "ancestor" and tool in ("Grep", "Glob")):\n',
     '            if kind == "inside":\n', [CR]),
    ("delta H-1: a Glob pattern not joined to its path", FX,
     "                kind = _glob_reach(joined) or kind\n", "                pass\n", [CR]),
    ("delta H-1: fetchers forcing an address", FX,
     '                if any(w.startswith(("--resolve", "--connect-to", "--proxy", "-x")) for w in low) or any(\n',
     "                if False and any(\n", ["Scope.test_fetchers_by_any_spelling"]),
    ("delta H-1: percent escapes not read", FX,
     "        if _GITHUB_HOST.search(unquote(flat)):\n", "        if _GITHUB_HOST.search(flat):\n",
     ["Scope.test_fetchers_by_any_spelling"]),
    ("delta H-1: an address literal not asked", FX,
     "                    self.ask(\"a fetch to an address the gate cannot read\")\n", "                    pass\n",
     ["Scope.test_fetchers_by_any_spelling"]),
    ("delta MED: python -c in a cluster", FX,
     '                if c == "c":\n                    return "inline code (-c)"\n',
     '                if w == "-c":\n                    return "inline code (-c)"\n', [IN]),
    ("delta MED: perl and ruby -e in a cluster", FX,
     '                    if c in "eE":\n                        return "inline code (-e)"\n',
     '                    if w in ("-e", "-E"):\n                        return "inline code (-e)"\n', [IN]),
    ("delta MED: node -e/-p in a cluster", FX,
     '                    _shorts(w) and any(c in "ep" for c in _shorts(w)[:2])):\n', "                    False):\n", [IN]),
    ("delta MED: powershell -Command", FX,
     '                    "-command".startswith(low) or "-encodedcommand".startswith(low) or low == "-e")):\n',
     "                    False)):\n", [IN]),
    ("delta MED: awk programs", FX,
     '                else "an awk program (it may call system())")\n', "                else None)\n", [IN]),
    ("delta MED: a shell fed a program", FX,
     '    return "a script read from a file" if files else "a program read from stdin"\n', "    return None\n", [IN]),
    ("delta MED: source", FX,
     '    if stem in ("source", "."):\n        return "a script sourced from a file"\n', "", [IN]),
    ("delta MED: eval of an expansion", FX,
     '        if stem == "eval" and any("$" in w or "`" in w for w in words):\n', "        if False:\n", [IN]),
    ("delta MED: python reading stdin", FX,
     '        return "a program read from stdin"\n    if stem in ("perl", "ruby"):\n',
     '        return None\n    if stem in ("perl", "ruby"):\n', [IN]),
    ("delta MED: the helper's content not pinned", FX,
     "    pins = [f\"{p}\\n{_file_hash(p) if not p.startswith('unresolved:') else ''}\" for p in programs]\n",
     "    pins = []\n", [PIN]),
    ("delta MED: the helper itself writable", FX,
     "        if path == helper or real == helper:\n            return True\n", "", [PIN]),
    ("delta MED: a shadow ahead on PATH", FX,
     "        if any(path == d / helper.name or real == Path(os.path.realpath(d)) / helper.name for d in ahead):\n",
     "        if False:\n", [PIN]),
    ("delta MED: ~/.local/bin and ~/bin writable", FX,
     '    return [home / ".local" / "bin", home / "bin"]\n', "    return []\n", [PIN]),
    ("delta LOW: tree writers", FX,
     "        if stem in _UNPACKERS and _unpacks(stem, words):\n", "        if False:\n",
     ["Scope.test_tree_writers_are_asked"]),
    ("delta LOW: git apply, am, cherry-pick, reset --hard, stash pop", FX,
     '        elif sub in ("apply", "am", "cherry-pick") or (sub == "reset" and "--hard" in words) or (\n',
     "        elif False and (\n", ["Scope.test_tree_writers_are_asked"]),
    ("delta NIT: gh --web", FX,
     '        if any(str(w) in ("--web", "-w") or str(w).startswith("--web=") for w in rest):\n',
     "        if False:\n", ["Scope.test_gh_reads_only"]),
    ("delta NIT: prod branches", FX,
     '("main", "master", "release*", "prod*", "production*")', '("main", "master", "release*")',
     ["Start.test_a_default_or_release_branch_gets_no_fix"]),
    ("delta: git config listings", FX,
     '            elif any(w in ("-l", "--list", "--get-regexp", "--show-origin") or w.startswith("--get-regexp")\n',
     "            elif False and any(w in (\"-l\",)\n", ["Scope.test_git_config_and_remote_writes_are_asked"]),
    ("delta: gh api three-letter abbreviations", FX,
     '    if not word.startswith("--") or len(word) < 3:\n', '    if not word.startswith("--") or len(word) < 4:\n',
     ["Scope.test_gh_api_writes_are_refused"]),

    ("last: any merge source", FX,
     "        self.ask(f\"git {sub} of {_shown(' '.join(sources) or 'its upstream')}: a fix merges in \"\n",
     "        return\n        self.ask(f\"git {sub} of {_shown(' '.join(sources) or 'its upstream')}: a fix merges in \"\n",
     ["Scope.test_only_the_base_is_merged_in"]),
    ("last: FETCH_HEAD without the base fetched", FX,
     '                                        or (sources == ["FETCH_HEAD"] and self.fetched_base)):\n',
     '                                        or (sources == ["FETCH_HEAD"])):\n', ["Scope.test_only_the_base_is_merged_in"]),
    ("last: a pull that rebases", FX,
     '                self.ask("a pull that rebases; the brief merges the base in")\n                return\n',
     "                pass\n", ["Scope.test_only_the_base_is_merged_in"]),
    ("last: the base merge asks", FX,
     '        if base and sub == "pull" and sources == ["origin", base]:\n            return\n', "", ["Scope.test_only_the_base_is_merged_in"]),
    ("last: read-tree -u and checkout-index -a", FX,
     '                sub == "read-tree" and any(w in ("-u", "--reset")',
     '                False and any(w in ("-u", "--reset")', ["Scope.test_tree_writers_are_asked"]),
    ("last: checkout-index", FX,
     '                sub == "checkout-index" and any(w in ("-a", "--all", "-f", "--force") or (',
     '                False and any(w in ("-a", "--all", "-f", "--force") or (', ["Scope.test_tree_writers_are_asked"]),

    # -- the review's last delta (2026-10-29) ---------------------------------------------------
    ("last: xargs' replace string read as a name", FX,
     '                raw = raw.replace(replace, "*")\n', "                pass\n", ["Scope.test_paths_the_gate_cannot_read_are_asked"]),
    ("last: a substitution in a reader's path", FX,
     '                if "/" in raw or reader:\n', '                if "/" in raw:\n', ["Scope.test_paths_the_gate_cannot_read_are_asked"]),
    ("last: a variable in a reader's path", FX,
     '                if reader:\n                    # A variable the gate cannot read',
     '                if False:\n                    # A variable the gate cannot read', ["Scope.test_paths_the_gate_cannot_read_are_asked"]),
    ("last: xargs", FX,
     '        if stem == "xargs":\n', '        if False:\n', ["Scope.test_paths_the_gate_cannot_read_are_asked"]),
    ("last: fetch settings files writable", FX,
     "        if fetch_rc(target):\n", "        if False:\n", ["Scope.test_fetch_settings_files"]),
    ("last: fetch config options", FX,
     "                if fetch_config(stem, words):\n", "                if False:\n", ["Scope.test_fetch_settings_files"]),
    ("last: CURL_HOME and kin", FX,
     '        if re.search(r"(?<![A-Za-z0-9_])(CURL_HOME|NETRC|WGETRC|CURLOPT_[A-Z_]+)\\s*=", text):\n',
     "        if False:\n", ["Scope.test_fetch_settings_files"]),
    ("last: sed e and s///e", FX,
     '        if _SED_E.search(script) or any("e" in m.group(2) for m in _SED_S.finditer(script)):\n',
     "        if False:\n", ["Scope.test_sed_that_runs_commands"]),
    ("last: sed -f", FX,
     '        if w in ("-f", "--file") or w.startswith(("--file=", "-f")) and not w.startswith("--f") or (\n',
     "        if False and (\n", ["Scope.test_sed_that_runs_commands"]),
    ("last: awk -f", FX,
     '        return ("an awk program file (it may call system())"\n', '        return (None\n',
     ["Scope.test_inline_code_in_any_spelling"]),
    ("last: git update-ref", FX,
     '            self.ask("git update-ref may make another commit pass for the base")\n', "            pass\n",
     ["Scope.test_merge_sources_cannot_be_spoofed"]),
    ("last: a tag or branch named origin/", FX,
     '        elif sub in ("tag", "branch") and any(w.lstrip("+").startswith(("origin/", "refs/remotes/"))\n',
     '        elif False and any(w.lstrip("+").startswith(("origin/", "refs/remotes/"))\n', ["Scope.test_merge_sources_cannot_be_spoofed"]),
    ("last: rebase", FX,
     '            self.ask("a fix merges the base in; it never rebases")\n', "            pass\n", ["Scope.test_merge_sources_cannot_be_spoofed"]),
    ("last: a local fetch", FX,
     '            self.ask("a fetch from a local path or a URL may pass for the base")\n            return\n',
     "            return\n", ["Scope.test_merge_sources_cannot_be_spoofed"]),
    ("last: a fetch into a remote-tracking ref", FX,
     '                self.ask(f"a fetch into {_shown(dst)} may make another commit pass for the base")\n',
     "                pass\n", ["Scope.test_merge_sources_cannot_be_spoofed"]),
    ("last: -c pull.rebase", FX,
     'r"remote\\..*|include.*|pull\\..*|rebase\\..*|branch\\..*)$", re.I)',
     'r"remote\\..*|include.*)$", re.I)', ["Scope.test_merge_sources_cannot_be_spoofed"]),

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
