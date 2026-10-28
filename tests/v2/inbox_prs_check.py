"""The owner inbox's PR source, inbox B3 / IN-4
(docs/plans/2026-10-24-owner-inbox-design.md §9; `jarvis/v2/inbox_prs.py`).

* **Reading gh's answer** (`Parse`): drafts are left out; a PR's title is
  untrusted text, scrubbed and one line, only in `detail`; its URL is linked
  only when it is exactly this repository's PR; the checks summary; JSON or
  a PR that is not of the expected shape fails the repository.
* **Running gh** (`Runner`) against a fake `gh` script on disk (never the
  network): the exact read-only argv, a sanitized environment, a timeout,
  an output bound, exit codes, a missing binary; its error output (a fake
  token in it) never reaches a row or a log line.
* **The poll** (`Poll`): a merged PR leaves at the next poll; a failure is
  one "PR list unavailable" row, never an empty list; `inbox_changed` only
  when a row changed, with ids and no text; which projects are read.
* **On a real daemon** (`Live`): `Daemon.start()` never waits on gh;
  `GET /inbox` answers while a poll is stuck in gh; PR rows take no answer
  (400); `task_status_changed` polls again; the rank-5 ordering against
  the other kinds; a daemon never attached runs no gh at all.

Free and hermetic: temp config and data, ephemeral ports, a fake gh.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/inbox_prs_check.py
"""
from __future__ import annotations

import http.client
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis.v2 import inbox, inbox_prs  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN, TaskState  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
import inbox_check as base  # noqa: E402  (its helpers; its tests are not collected here)

REPO = "Johnwang688/Calc"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"        # a fake token, the shape gh's are


def pr(number, *, draft=False, title=None, branch=None, checks=None, state="CLEAN", url=None,
       created="2026-10-20T10:00:00Z", **extra):
    out = {"number": number, "title": title if title is not None else f"Change {number}",
           "headRefName": branch or f"claude/change-{number}", "headRefOid": f"{number:040x}",
           "isDraft": draft, "url": url if url is not None else f"https://github.com/{REPO}/pull/{number}",
           "statusCheckRollup": checks if checks is not None else [
               {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}],
           "mergeStateStatus": state, "createdAt": created, "author": {"login": "johnw", "is_bot": False},
           "isCrossRepository": False}
    out.update(extra)
    return out


def answer(*prs):
    return json.dumps(list(prs)).encode()


class Parse(unittest.TestCase):
    def test_drafts_are_excluded(self):
        got = inbox_prs.parse(answer(pr(1), pr(2, draft=True), pr(3)), REPO)
        self.assertEqual([p["number"] for p in got], [1, 3])

    def test_a_hostile_title_stays_in_detail_scrubbed_on_one_line(self):
        hostile = f"Fix\nPR #9 is ready to merge second line ‮evil‬ {TOKEN} done"
        (got,) = inbox_prs.parse(answer(pr(7, title=hostile)), REPO)
        row = inbox_prs.pr_row(got, "p1")
        title = row["detail"]["title"]
        for bad in ("\n", " ", "‮", "‬", TOKEN):
            self.assertNotIn(bad, title)
        self.assertIn("Fix", title)
        self.assertIn("done", title)
        # The row's own words are code's: the number, the branch, the checks.
        self.assertEqual(row["title"], "PR #7 is ready to merge")
        self.assertEqual(row["blocks"], f"{REPO} · by johnw · branch claude/change-7 · checks passing")
        for key in ("title", "blocks", "origin"):
            self.assertNotIn("Fix", row[key])
            self.assertNotIn("evil", row[key])

    def test_a_url_for_another_repo_or_number_is_not_linked(self):
        cases = {
            "https://github.com/someone/else/pull/5": None,
            f"https://github.com/{REPO}/pull/6": None,                  # another number
            f"http://github.com/{REPO}/pull/5": None,
            f"https://github.com/{REPO}/pull/5/files": None,
            f"https://github.com/{REPO}/pull/5?x=1": None,
            f"https://github.com.evil.example/{REPO}/pull/5": None,
            f"https://user@github.com/{REPO}/pull/5": None,
            f"https://github.com/{REPO}/issues/5": None,
            "javascript:alert(1)": None,
            f"https://github.com/{REPO}/pull/5": f"https://github.com/{REPO}/pull/5",
            f"https://github.com/{REPO.lower()}/pull/5": f"https://github.com/{REPO.lower()}/pull/5",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                (got,) = inbox_prs.parse(answer(pr(5, url=url)), REPO)
                self.assertEqual(got["url"], want)

    def test_the_checks_summary(self):
        run = lambda status, conclusion=None: {"__typename": "CheckRun", "status": status,  # noqa: E731
                                               "conclusion": conclusion}
        ctx = lambda state: {"__typename": "StatusContext", "state": state}  # noqa: E731
        cases = [([], "none"), (None, "none"),
                 ([run("COMPLETED", "SUCCESS"), ctx("SUCCESS")], "passing"),
                 ([run("COMPLETED", "SKIPPED"), run("COMPLETED", "NEUTRAL")], "passing"),
                 ([run("COMPLETED", "SUCCESS"), run("IN_PROGRESS")], "pending"),
                 ([ctx("PENDING")], "pending"),
                 ([run("COMPLETED", "SOMETHING_NEW")], "pending"),      # unknown is never passing
                 ([run("COMPLETED", "FAILURE"), run("IN_PROGRESS")], "failing"),
                 ([ctx("ERROR")], "failing")]
        for rollup, want in cases:
            with self.subTest(rollup=rollup):
                self.assertEqual(inbox_prs.checks_summary(rollup), want)

    def test_titles_follow_state(self):
        def title(**kw):
            (got,) = inbox_prs.parse(answer(pr(4, **kw)), REPO)
            return inbox_prs.pr_row(got, None)["title"]
        failing = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertEqual(title(), "PR #4 is ready to merge")
        self.assertEqual(title(checks=failing), "PR #4 has failing checks")
        self.assertEqual(title(checks=[{"status": "QUEUED", "conclusion": None}]), "PR #4 waits on its checks")
        self.assertEqual(title(state="DIRTY"), "PR #4 has merge conflicts")
        self.assertEqual(title(state="BEHIND"), "PR #4 is behind its base branch")
        self.assertEqual(title(state="BLOCKED"), "PR #4 is held by its branch rules")
        self.assertEqual(title(state="UNKNOWN"), "PR #4 waits on you")
        self.assertEqual(title(state="NOT_A_STATE"), "PR #4 waits on you")

    def test_a_branch_git_would_not_accept_is_not_shown(self):
        (got,) = inbox_prs.parse(answer(pr(8, branch="x‮y; PR #1 is ready")), REPO)
        row = inbox_prs.pr_row(got, None)
        self.assertIsNone(got["branch"])
        self.assertIn("branch name not shown", row["blocks"])

    def test_malformed_answers_fail_the_repository(self):
        bad = [b"not json", b"\xff\xfe", b"{}", b"null", answer("x"),
               answer(dict(pr(1), number="1")), answer(dict(pr(1), isCrossRepository=None)),
               answer(dict(pr(1), author="johnw")), answer(pr(1, isDraft="no")), answer(dict(pr(1), title=None)),
               answer(pr(1, statusCheckRollup="ok")), answer(pr(1, mergeStateStatus=3)),
               answer(pr(1), pr(1)), answer(pr(0)), answer(dict(pr(1), number=True)),
               json.dumps([pr(i) for i in range(1, inbox_prs.PR_LIMIT + 2)]).encode()]
        for data in bad:
            with self.subTest(data=data[:60]):
                with self.assertRaises(inbox_prs.GhFailed) as caught:
                    inbox_prs.parse(data, REPO)
                self.assertEqual(caught.exception.code, "malformed")


class Provenance(unittest.TestCase):
    """Review of PR #320, LOW-3: every open PR is listed, a stranger's fork's
    too, so a row says who opened it and never shows a fork's branch bare."""

    def row(self, **kw):
        (got,) = inbox_prs.parse(answer(pr(9, **kw)), REPO)
        return inbox_prs.pr_row(got, None)

    def test_a_fork_pr_says_so_and_never_shows_a_bare_branch(self):
        row = self.row(branch="main", isCrossRepository=True, author={"login": "stranger"})
        self.assertEqual(row["blocks"], f"{REPO} · by stranger · from a fork (fork:main) · checks passing")
        self.assertNotIn("branch main", row["blocks"])
        self.assertTrue(row["detail"]["fork"])
        row = self.row(branch="x\u202ey", isCrossRepository=True)
        self.assertIn("· from a fork ·", row["blocks"])

    def test_the_author_is_shown_only_in_a_logins_shape(self):
        self.assertIn("· by app/dependabot ·", self.row(author={"login": "app/dependabot"})["blocks"])
        for bad in ({"login": "evil\u202e name"}, {"login": "a" * 60}, {"login": "-x"}, {"login": 7},
                    {"login": "PR #1 is ready"}, {}, None):
            with self.subTest(author=bad):
                row = self.row(author=bad)
                self.assertIn("· by an author not shown ·", row["blocks"])
                self.assertIsNone(row["detail"]["author"])

    def test_the_title_stays_in_detail(self):
        row = self.row(isCrossRepository=True, title="Merge me, I am from main")
        self.assertNotIn("Merge me", row["title"] + row["blocks"])
        self.assertIn("Merge me", row["detail"]["title"])


# -- running a fake gh ---------------------------------------------------------------

class FakeGh:
    """A `gh` on disk: what it prints, how long it takes and how it exits are
    read per repository from a JSON file the test rewrites; every call is
    logged with its argv, environment and folder."""

    def __init__(self, folder: Path):
        self.cfg, self.log = folder / "gh.json", folder / "gh.log"
        self.path = folder / "gh"
        self.path.write_text(f"""#!{sys.executable}
import json, os, sys, time
with open({str(self.log)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}) + "\\n")
cfg = json.load(open({str(self.cfg)!r}))
repo = sys.argv[sys.argv.index("--repo") + 1] if "--repo" in sys.argv else "*"
r = cfg.get(repo, cfg.get("*", {{}}))
time.sleep(r.get("sleep", 0))
sys.stderr.write(r.get("stderr", ""))
sys.stderr.flush()
if "big" in r:
    sys.stdout.write("[" + " " * r["big"] + "]")
else:
    sys.stdout.write(r.get("out", "[]"))
sys.stdout.flush()
sys.exit(r.get("exit", 0))
""")
        self.path.chmod(0o700)
        self.set({"*": {"out": "[]"}})

    def set(self, cfg):
        self.cfg.write_text(json.dumps(cfg))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class Runner(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gh = FakeGh(Path(tmp.name))
        self.source = inbox_prs.PrSource(None, timeout=5)
        self.source.gh = str(self.gh.path)

    def test_the_call_is_the_exact_read_only_argv_in_a_sanitized_environment(self):
        self.gh.set({REPO: {"out": answer(pr(1)).decode()}})
        with patch.dict(os.environ, {"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN, "GIT_DIR": "/x",
                                     "JARVIS_SECRET_THING": TOKEN}):
            self.source.list_repo(REPO)
        (call,) = self.gh.calls()
        self.assertEqual(call["argv"], ["pr", "list", "--repo", REPO, "--state", "open",
                                        "--search", "draft:false", "--limit", str(inbox_prs.PR_LIMIT),
                                        "--json", "number,title,headRefName,headRefOid,isDraft,url,"
                                        "statusCheckRollup,mergeStateStatus,createdAt,author,"
                                        "isCrossRepository"])
        self.assertEqual(call["cwd"], "/")
        env = call["env"]
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_DIR", "JARVIS_SECRET_THING"):
            self.assertNotIn(name, env)
        self.assertNotIn(TOKEN, json.dumps(env))
        self.assertEqual(env.get("GH_PROMPT_DISABLED"), "1")
        self.assertEqual(env.get("HOME"), os.environ.get("HOME"))

    def failure(self, cfg, **kw):
        self.gh.set({"*": cfg})
        for k, v in kw.items():
            setattr(self.source, k, v)
        with self.assertRaises(inbox_prs.GhFailed) as caught:
            self.source.list_repo(REPO)
        return caught.exception.code

    def test_a_timeout_is_a_failure_and_the_process_is_killed(self):
        start = time.monotonic()
        self.assertEqual(self.failure({"sleep": 30}, timeout=0.5), "timeout")
        self.assertLess(time.monotonic() - start, 5)

    def test_oversized_output_is_a_failure(self):
        self.assertEqual(self.failure({"big": 300_000}, cap=100_000), "too_large")

    def test_exit_codes(self):
        self.assertEqual(self.failure({"exit": 4, "stderr": f"auth required {TOKEN}"}), "login")
        self.assertEqual(self.failure({"exit": 1, "stderr": f"HTTP 404 {TOKEN}"}), "exit")

    def test_a_missing_gh_is_a_failure(self):
        self.source.gh = None
        with self.assertRaises(inbox_prs.GhFailed) as caught:
            self.source.list_repo(REPO)
        self.assertEqual(caught.exception.code, "missing")
        self.source.gh = str(self.gh.path) + "-gone"
        with self.assertRaises(inbox_prs.GhFailed) as caught:
            self.source.list_repo(REPO)
        self.assertEqual(caught.exception.code, "missing")

    def test_stop_cuts_a_running_gh_short(self):
        self.gh.set({"*": {"sleep": 30}})
        timer = threading.Timer(0.3, self.source._stop.set)
        timer.start()
        start = time.monotonic()
        with self.assertRaises(inbox_prs.GhFailed):
            self.source.list_repo(REPO)
        self.assertLess(time.monotonic() - start, 3)


# -- the poll ------------------------------------------------------------------------

class Bus:
    def __init__(self):
        self.records = []

    def publish(self, record):
        self.records.append(record)


class Poll(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.daemon = type("D", (), {"bus": self.bus})()
        self.answers = {}
        self.source = inbox_prs.PrSource(self.daemon)
        self.source.repos = lambda _d: {repo: ["p1"] for repo in self.answers}
        self.source.list_repo = self.read

    def read(self, repo):
        out = self.answers[repo]
        if isinstance(out, Exception):
            raise out
        return out

    def ids(self):
        return sorted(r["id"] for r in self.source.rows())

    def test_a_merged_pr_leaves_at_the_next_poll(self):
        self.answers[REPO] = answer(pr(1), pr(2))
        self.assertEqual(self.source.poll(), [f"pr:{REPO}#1", f"pr:{REPO}#2"])
        self.assertEqual(self.ids(), [f"pr:{REPO}#1", f"pr:{REPO}#2"])
        self.answers[REPO] = answer(pr(2))               # #1 merged
        self.assertEqual(self.source.poll(), [f"pr:{REPO}#1"])
        self.assertEqual(self.ids(), [f"pr:{REPO}#2"])
        self.assertEqual(self.bus.records[-1], {"kind": "inbox_changed",
                                                "data": {"ids": [f"pr:{REPO}#1"], "reason": "pr_poll"}})

    def test_inbox_changed_only_when_a_row_changed_and_never_with_text(self):
        self.answers[REPO] = answer(pr(1, title=f"secret words {TOKEN}"))
        self.source.poll()
        self.assertEqual(len(self.bus.records), 1)
        self.source.poll()                                # the same answer: nothing to say
        self.assertEqual(len(self.bus.records), 1)
        self.answers[REPO] = answer(pr(1, title="secret words", state="DIRTY"))
        self.source.poll()                                # a row changed
        self.assertEqual(len(self.bus.records), 2)
        for record in self.bus.records:
            self.assertEqual(set(record["data"]), {"ids", "reason"})
            self.assertNotIn("secret", json.dumps(record))

    def test_a_failure_shows_unavailable_never_nothing_waiting(self):
        self.answers[REPO] = answer(pr(1))
        self.source.poll()
        for code in ("login", "timeout", "too_large", "malformed", "missing", "exit"):
            with self.subTest(code=code):
                self.answers[REPO] = inbox_prs.GhFailed(code)
                self.source.poll()
                rows = self.source.rows()
                # The last good list is dropped, not kept stale; one row says why.
                self.assertEqual([r["id"] for r in rows], ["prx:list"])
                (row,) = rows
                self.assertEqual((row["kind"], row["rank"]), ("pr_unavailable", 5))
                self.assertEqual(row["title"], f"PR list unavailable for {REPO}")
                (repo,) = row["detail"]["repos"]
                self.assertEqual(repo["repo"], REPO)
                self.assertEqual(repo["reason"], inbox_prs.REASONS[code])
                self.assertIsNotNone(repo["last_read"])
        # Any other error, too, is unavailable, never a silent empty list.
        self.answers[REPO] = RuntimeError(TOKEN)
        with self.assertLogs("jarvis.v2.inbox_prs", logging.WARNING) as logs:
            self.source.poll()
        self.assertEqual(self.ids(), ["prx:list"])
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        # Back to good: the unavailable row goes.
        self.answers[REPO] = answer(pr(1))
        self.assertEqual(self.source.poll(), [f"pr:{REPO}#1", "prx:list"])
        self.assertEqual(self.ids(), [f"pr:{REPO}#1"])

    def test_one_failing_repository_leaves_the_others(self):
        self.answers = {REPO: answer(pr(1)), "o/two": inbox_prs.GhFailed("exit"),
                        "o/three": inbox_prs.GhFailed("login")}
        self.source.poll()
        self.assertEqual(self.ids(), [f"pr:{REPO}#1", "prx:list"])
        (row,) = [r for r in self.source.rows() if r["id"] == "prx:list"]
        self.assertEqual(row["title"], "PR list unavailable for 2 repositories")
        self.assertEqual([r["repo"] for r in row["detail"]["repos"]], ["o/three", "o/two"])

    def test_a_full_page_says_the_list_may_be_incomplete(self):
        """LOW-1: exactly PR_LIMIT PRs back means there may be more."""
        self.answers[REPO] = json.dumps([pr(i) for i in range(1, inbox_prs.PR_LIMIT + 1)]).encode()
        self.answers["o/two"] = answer(pr(1))
        self.source.poll()
        rows = {r["id"]: r for r in self.source.rows()}
        self.assertEqual(len([i for i in rows if i.startswith("pr:")]), inbox_prs.PR_LIMIT + 1)
        row = rows["prx:list"]
        self.assertEqual((row["rank"], row["title"]), (5, f"PR list incomplete for {REPO}"))
        self.assertEqual([(r["repo"], r["code"]) for r in row["detail"]["repos"]], [(REPO, "cut_off")])
        # One fewer is a whole list.
        self.answers[REPO] = json.dumps([pr(i) for i in range(1, inbox_prs.PR_LIMIT)]).encode()
        self.source.poll()
        self.assertNotIn("prx:list", self.ids())

    def test_a_failure_beside_a_cut_off_list_reads_unavailable(self):
        self.answers[REPO] = json.dumps([pr(i) for i in range(1, inbox_prs.PR_LIMIT + 1)]).encode()
        self.answers["o/two"] = inbox_prs.GhFailed("login")
        self.source.poll()
        (row,) = [r for r in self.source.rows() if r["id"] == "prx:list"]
        self.assertEqual(row["title"], "PR list unavailable for 2 repositories")

    def test_repositories_past_the_cap_are_named_never_dropped(self):
        """LOW-2."""
        names = [f"o/r{i:03d}" for i in range(inbox_prs.REPOS_MAX + 3)]
        self.answers = {n: answer() for n in names}
        self.source.poll()
        (row,) = self.source.rows()
        self.assertEqual(row["title"], "PR list incomplete for 3 repositories")
        self.assertEqual([(r["repo"], r["code"]) for r in row["detail"]["repos"]],
                         [(n, "skipped") for n in names[inbox_prs.REPOS_MAX:]])

    def test_a_stop_during_gh_publishes_nothing(self):
        """NIT-1: a gh cut short by shutdown is no "timeout" to show."""
        self.answers[REPO] = answer(pr(1))
        self.source.poll()
        published = len(self.bus.records)

        def cut(repo):
            self.source._stop.set()
            raise inbox_prs.GhFailed("timeout")
        self.source.list_repo = cut
        self.assertEqual(self.source.poll(), [])
        self.assertEqual(len(self.bus.records), published)
        self.assertEqual(self.ids(), [f"pr:{REPO}#1"])

    def test_repositories_that_are_gone_are_forgotten(self):
        """NIT-5."""
        self.answers = {REPO: answer(), "o/gone": answer()}
        self.source.poll()
        self.assertEqual(set(self.source._good), {REPO, "o/gone"})
        del self.answers["o/gone"]
        self.source.poll()
        self.assertEqual(set(self.source._good), {REPO})

    def test_only_a_transition_into_done_failed_or_blocked_pokes(self):
        """LOW-4: saves and moves between working states do not poll."""
        pokes = []
        self.source.poke = lambda: pokes.append(1)
        ev = lambda tid, state: self.source.task_changed(  # noqa: E731
            {"kind": "task_status_changed", "task_id": tid, "data": {"state": state}})
        for state in ("intake", "clarifying", "planned", "running", "running", "verifying", "running"):
            ev("t1", state)
        self.assertEqual(pokes, [])
        ev("t1", "done")
        self.assertEqual(len(pokes), 1)
        ev("t1", "done")                                   # a save in the same state
        self.assertEqual(len(pokes), 1)
        ev("t2", "running")
        ev("t2", "blocked")
        ev("t2", "running")
        ev("t2", "failed")
        self.assertEqual(len(pokes), 3)
        for bad in ({}, {"task_id": "t3"}, {"task_id": "t3", "data": {"state": 3}}, "x"):
            self.source.task_changed(bad)
        self.assertEqual(len(pokes), 3)

    def test_start_never_runs_a_second_loop_beside_a_live_one(self):
        """NIT-5."""
        self.source.attached = True
        old = threading.Thread(target=threading.Event().wait, args=(2,), daemon=True)
        old.start()
        self.source._thread = old
        self.source.start()
        self.assertIs(self.source._thread, old)
        old.join()
        self.source.poll = lambda: []
        self.source.interval = 60
        self.source.start()
        self.assertIsNot(self.source._thread, old)
        self.source.stop()

    def test_an_archived_projects_rows_hide_at_read(self):
        self.answers[REPO] = answer(pr(1))
        self.source.poll()
        self.assertEqual(self.source.rows({"p1"}), [])
        self.assertEqual(len(self.source.rows({"p2"})), 1)

    def test_rows_are_copies(self):
        self.answers[REPO] = answer(pr(1))
        self.source.poll()
        self.source.rows()[0]["detail"]["url"] = "https://evil.example/"
        self.assertEqual(self.source.rows()[0]["detail"]["url"], f"https://github.com/{REPO}/pull/1")


class Repos(unittest.TestCase):
    """Which repository each project is: its own git metadata and origin,
    read without running git's hooks, includes or executable config."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in dict(V2_DATA_DIR=self.root / "data").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.stores = Stores(self.root / "data")
        self.daemon = type("D", (), {"stores": self.stores})()

    def repo(self, name, origin=None, extra=""):
        path = self.root / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        if origin or extra:
            with open(path / ".git" / "config", "a") as f:
                if origin:
                    f.write(f'[remote "origin"]\n\turl = {origin}\n')
                f.write(extra)
        return str(path)

    def test_github_projects_only_once_each_and_never_archived(self):
        a = self.stores.projects.create("A", self.repo("a", f"https://github.com/{REPO}.git"))
        b = self.stores.projects.create("B", self.repo("b", f"git@github.com:{REPO}.git"))
        self.stores.projects.create("C", self.repo("c", "https://gitlab.com/o/r.git"))
        self.stores.projects.create("D", self.repo("d"))
        plain = self.root / "plain"
        plain.mkdir()
        self.stores.projects.create("E", str(plain))
        f = self.stores.projects.create("F", self.repo("f", "https://github.com/o/archived.git"))
        f.archived = "2026-10-26T00:00:00+00:00"
        self.stores.projects.save(f)
        got = inbox_prs.project_repos(self.daemon)
        self.assertEqual(got, {REPO: sorted([a.id, b.id])})

    def test_an_include_or_a_hook_is_never_followed(self):
        marker = self.root / "ran"
        inc = self.root / "inc.cfg"
        inc.write_text('[remote "origin"]\n\turl = https://github.com/evil/included.git\n')
        path = self.repo("g", extra=f"[include]\n\tpath = {inc}\n"
                                    f"[core]\n\tfsmonitor = touch {marker}\n")
        hook = Path(path) / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        self.stores.projects.create("G", path)
        self.assertEqual(inbox_prs.project_repos(self.daemon), {})
        self.assertFalse(marker.exists())


# -- on a real daemon ------------------------------------------------------------------

class Live(unittest.TestCase):
    call = base.Live.call

    def setUp(self, *, start=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        (self.root / "repo").mkdir()
        self.stores = Stores()
        self.daemon = Daemon(self.stores, {name: base.Brain(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.runner = base.SpyRunner(self.daemon)
        self.project = self.stores.projects.create("Calc", str(self.root / "repo"))
        self.gh = FakeGh(self.root)
        self.prs = self.daemon.inbox.prs
        # Attached as `main()` attaches it, to the fake gh; one repository.
        self.prs.gh, self.prs.attached = str(self.gh.path), True
        self.prs.repos = lambda _d: {REPO: [self.project.id]}
        self.prs.debounce, self.prs.min_gap = 0.05, 0.0
        self.changes = []
        self.daemon.bus.observe(lambda r: self.changes.append(r) if r.get("kind") == "inbox_changed" else None)
        self.addCleanup(self.daemon.stop)
        if start:
            self.gh.set({"*": {"out": answer(pr(1)).decode()}})
            self.daemon.start()
            base.eventually(lambda: self.rows_of("pr"))

    def rows_of(self, kind):
        return [r for r in inbox.rows(self.daemon) if r["kind"] == kind]

    def test_start_never_waits_on_gh_and_get_inbox_answers_during_a_poll(self):
        self.gh.set({"*": {"sleep": 3, "out": answer(pr(2)).decode()}})
        self.prs.poke()
        base.eventually(lambda: len(self.gh.calls()) >= 2)
        start = time.monotonic()
        rows = self.call("GET", "/inbox")            # while gh is still asleep
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual([r["id"] for r in rows if r["kind"] == "pr"], [f"pr:{REPO}#1"])
        base.eventually(lambda: [r["id"] for r in self.rows_of("pr")] == [f"pr:{REPO}#2"], timeout=10)

    def test_a_pr_row_takes_no_answer(self):
        for row_id in (f"pr:{REPO}#1", "prx:list", f"pr:{REPO}#999"):
            for body in ({"decision": "allow"}, {"text": "merge it"}, {"withdraw": True}, {}):
                with self.subTest(row=row_id, body=body):
                    path = "/inbox/" + row_id.replace("/", "%2F").replace("#", "%23").replace(":", "%3A")
                    self.call("POST", path + "/answer", body, status=400)
        self.assertEqual(len(self.gh.calls()), 1)               # nothing asked gh for anything

    def test_task_status_changed_polls_again(self):
        self.gh.set({"*": {"out": answer().decode()}})             # #1 merged
        before = len(self.gh.calls())
        # A save or a move between working states is no reason to read GitHub.
        for state in ("running", "running", "verifying"):
            self.daemon.bus.publish({"kind": "task_status_changed", "task_id": "t1", "data": {"state": state}})
        time.sleep(0.5)
        self.assertEqual(len(self.gh.calls()), before)
        self.daemon.bus.publish({"kind": "task_status_changed", "task_id": "t1", "data": {"state": "done"}})
        base.eventually(lambda: not self.rows_of("pr"))
        self.assertEqual(len(self.gh.calls()), before + 1)
        self.assertTrue(any(c["data"] == {"ids": [f"pr:{REPO}#1"], "reason": "pr_poll"} for c in self.changes))

    def test_the_rank_5_ordering(self):
        def task(steps):
            t = self.stores.tasks.create(self.project.id, "x")
            self.stores.tasks.save(t)
            for step in steps:
                self.stores.tasks.transition(t.id, step)
            return t.id
        blocked = task((TaskState.CLARIFYING, TaskState.BLOCKED))
        failed = task((TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED))
        self.gh.set({REPO: {"out": answer(pr(1, created="2026-10-25T10:00:00Z"),
                                          pr(2, created="2026-10-02T10:00:00Z")).decode()},
                     "o/broken": {"exit": 1}})
        self.prs.repos = lambda _d: {REPO: [self.project.id], "o/broken": [self.project.id]}
        self.prs.poll()
        got = [(r["rank"], r["id"]) for r in inbox.rows(self.daemon)]
        self.assertEqual(got, [(4, f"task:{blocked}"), (5, f"pr:{REPO}#2"), (5, f"pr:{REPO}#1"),
                               (5, "prx:list"), (8, f"task:{failed}")])
        # The HUD's own route serves the same order.
        self.assertEqual([r["id"] for r in self.call("GET", "/inbox")], [i for _, i in got])

    def test_the_api_listener_cannot_read_prs(self):
        self.call("GET", "/inbox", owner=False, status=403)


class NotAttached(unittest.TestCase):
    """An embedded or test daemon (no `main()`) runs no gh and has no PR rows."""

    def test_no_gh_no_thread_no_rows(self):
        case = Live()
        case._outcome = None
        try:
            Live.setUp(case, start=False)
            case.prs.attached = False
            case.daemon.start()
            time.sleep(0.2)
            self.assertIsNone(case.prs._thread)
            self.assertEqual(case.gh.calls(), [])
            self.assertEqual([r for r in inbox.rows(case.daemon) if r["kind"].startswith("pr")], [])
        finally:
            case.doCleanups()

    def test_start_returns_while_the_first_gh_is_still_running(self):
        case = Live()
        try:
            Live.setUp(case, start=False)
            case.gh.set({"*": {"sleep": 3}})
            begin = time.monotonic()
            case.daemon.start()
            took = time.monotonic() - begin
            base.eventually(lambda: case.gh.calls())
            self.assertLess(took, 2.0)
            self.assertEqual(case.rows_of("pr"), [])
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main(verbosity=2)
