"""The owner's Merge, Update and Hold on an inbox PR row (PM-D of
docs/plans/2026-10-28-pr-inbox-merge.md: `jarvis/v2/pr_merge.py`, the card
on `pr_summary` rows, the `pr:` subject family in `remote_auth`, the
routes on the HUD and the phone).

* `Argv`: the exact merge argv with `--match-head-commit`, squash only when
  the card said so, Update's argv, never `--admin`, `--auto`, `--rebase` or
  `--delete-branch`; the result from a fresh read; the receipt (0600).
* `Stale`: a stale head, a stale card, a summary that changed, a hold set
  after reading (and during the read), a closed PR, a draft, an action no
  longer offered: each a 409 that runs nothing and pokes a re-read.
* `Once`: two merges of one PR at once run once.
* `Holds`: set and clear, desk only; a hold hides every action and shows its
  note; the act re-checks it; a file that cannot be read holds everything.
* `RealGh`: the real `run_bounded` and a fake gh program that logs its argv
  and writes a token to stderr and stdout: the token is never read.
* `Routes`: a real daemon; owner_only on the HUD listener (the API listener
  and an Origin-less script refused), Fix is PM-E's (503 here: not attached), no phone hold route.
* `Phone`: a real daemon, a paired software passkey, §4.1 on a fake system:
  the subject binding, cross-family assertions refused both ways, limited
  plus flagged refused, limited plus unflagged allowed, a reused assertion,
  the challenge budget, the phone scrub sending it to the desk.
* `Boundary`: nothing at `Daemon.start()`, SELF_PROTECTED, the agent-path
  DENY over the phone paths.

Free and hermetic: temp HOME, config and data, fakes for gh, the model and
the passkey; ephemeral ports, never 8402/8403/8405/8408.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env TMPDIR=/var/tmp PYTHONPATH=. .venv/bin/python tests/v2/pr_merge_check.py
"""
from __future__ import annotations

import contextlib
from dataclasses import replace
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config, rules  # noqa: E402
from jarvis.v2 import inbox_prs, pr_facts, pr_merge, pr_summary as S, remote_api, remote_auth  # noqa: E402
from jarvis.v2.inbox_prs import GhFailed  # noqa: E402
from jarvis.v2.pr_facts import FileChange  # noqa: E402
import pr_facts_check as PF  # noqa: E402  (its fakes; its tests are not collected here)

REPO = PF.REPO
N = PF.N
HEAD = PF.HEAD
HEAD2 = "2" * 40
ROW = f"pr:{REPO}#{N}"
DAY = "2026-10-29"
GOOD = "Does: Adds a retry to the uploader.\nBlocked: nothing"
FORBIDDEN = ("--admin", "--auto", "--rebase", "--delete-branch", "-d", "--disable-auto")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def eventually(predicate, timeout=5.0, what="condition"):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError(f"{what} did not settle")
        time.sleep(0.005)


# -- fakes --------------------------------------------------------------------------------------

def gh_kind(argv) -> str:
    if argv[0] != PF.GH:
        return PF.kind(argv)
    if list(argv[1:3]) == ["pr", "merge"]:
        return "merge"
    if argv[1] == "api" and any(str(a).endswith("/update-branch") for a in argv):
        return "update"
    if argv[1] == "api" and any("/rules/branches/" in str(a) for a in argv):
        return "rules"
    if argv[1] == "api" and str(argv[-1]).count("/") == 2 and str(argv[-1]).startswith("repos/"):
        return "owner"
    if list(argv[1:3]) == ["pr", "view"] and "state,headRefOid,autoMergeRequest" in argv:
        return "result"
    if list(argv[1:3]) == ["pr", "view"] and "autoMergeRequest" in argv:
        return "auto"
    return PF.kind(argv)


def owner_answer(kind) -> str:
    return json.dumps({"full_name": REPO, "private": True, "owner": {"login": "Owner", "type": kind}})


FORBIDDEN_403 = GhFailed("exit")       # gh api's exit on "Upgrade to GitHub Pro ..." (HTTP 403)
ACCEPTED = "HTTP/2.0 202 Accepted\r\nContent-Type: application/json\r\n\r\n{\"message\":\"Updating\"}"
UNPROCESSABLE = "HTTP/2.0 422 Unprocessable Entity\r\n\r\n{\"message\":\"expected head sha didn't match\"}"


class GhRun(PF.FakeRun):
    """pr_facts' fake gh, plus merge, update-branch, the auto-merge and
    rules reads and the result's read. A merge marks the PR merged; an
    update moves its head to HEAD2. No auto-merge, no rules by default."""

    def __init__(self, **answers):
        super().__init__(**answers)
        self.merged = False
        self.head_now = HEAD
        self.gate: threading.Event | None = None
        self.answers.setdefault("merge", self._merge)
        self.answers.setdefault("update", self._update)
        self.answers.setdefault("result", self._result)
        self.answers.setdefault("auto", json.dumps({"autoMergeRequest": None}))
        self.answers.setdefault("rules", "[]")
        self.answers.setdefault("owner", owner_answer("Organization"))
        self.auto_after = None              # autoMergeRequest the result read reports
        self._lock = threading.Lock()

    def _merge(self, argv):
        if self.gate is not None:
            self.gate.wait(5)
        self.merged = True
        return "merged it\n"

    def _update(self, argv):
        self.head_now = HEAD2
        return ACCEPTED

    def _result(self, argv):
        return json.dumps({"state": "MERGED" if self.merged else "OPEN", "headRefOid": self.head_now,
                           "autoMergeRequest": self.auto_after})

    def __call__(self, argv, env, *, timeout, cap, stop=None, ok_codes=(0,), keep_tail=False):
        which = gh_kind(argv)
        with self._lock:
            self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout, "cap": cap,
                               "ok_codes": tuple(ok_codes), "keep_tail": keep_tail, "kind": which})
        answer = self.answers.get(which)
        if callable(answer):
            answer = answer(argv)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise GhFailed("exit")
        return answer if isinstance(answer, bytes) else answer.encode()

    def acts(self):
        return [c for c in self.calls if c["kind"] in ("merge", "update")]


class Reply:
    def __init__(self, text):
        self.text, self.cost_usd, self.prompt_tokens, self.completion_tokens = text, 0.0005, 4000, 60


class Chat:
    def __init__(self, answer=GOOD):
        self.answer = answer
        self.calls = []

    def __call__(self, model, messages, **kw):
        self.calls.append(model)
        return Reply(self.answer() if callable(self.answer) else self.answer)


class Bus:
    def __init__(self):
        self.records = []

    def publish(self, record):
        self.records.append(record)


def listing(v, fork=False):
    return {"number": v["number"], "title": v["title"], "headRefName": v["headRefName"],
            "headRefOid": v["headRefOid"], "isDraft": False, "url": f"https://github.com/{REPO}/pull/{v['number']}",
            "statusCheckRollup": v["statusCheckRollup"], "mergeStateStatus": v["mergeStateStatus"],
            "createdAt": "2026-10-20T10:00:00Z", "author": {"login": "johnw"}, "isCrossRepository": fork}


class Prs:
    """A PR in `daemon`'s inbox: its real PrSource, real PrSummaries over
    pr_facts' fake gh (`GhRun`) and a fake model, and this daemon's
    PrActions on the same fake gh."""

    def __init__(self, case, daemon, *, view=None, run=None, facts=None, chat=None):
        self.case, self.daemon = case, daemon
        self.view = view or PF.view()
        self.run = run or GhRun()
        self.facts = facts                  # crafted facts in place of the read, when given
        self.chat = chat or Chat()
        self.reads = 0
        self.source = daemon.inbox.prs
        self.source.repos = lambda _d: {REPO: ["p1"]}
        self.source.list_repo = lambda repo: json.dumps([listing(self.view)]).encode()
        root = Path(daemon.stores.root)
        engine = S.PrSummaryEngine(S.SummaryFile(root / "inbox" / S.FILE_NAME), chat=self.chat,
                                   deadline_s=5.0, today=lambda: DAY)
        self.summaries = S.PrSummaries(self.source, root=root, engine=engine, read=self.read,
                                       tidy=lambda *a, **k: None)
        self.source.attach_summaries(self.summaries)
        self.summaries.start()
        case.addCleanup(self.summaries.stop)
        self.actions = pr_merge.PrActions(daemon, run=self.run, gh=PF.GH, read=self.read)
        pr_merge.install(daemon, self.actions)

    def set_view(self, **kw):
        self.view = PF.view(**kw)
        self.run.answers["view"] = json.dumps(self.view)

    def read(self, repo, number, *, run=None, gh=None, budget=pr_facts.PASS_TIMEOUT_S, stop=None):
        self.reads += 1
        if self.facts is not None:
            return self.facts
        return pr_facts.read_pr(repo, number, run=self.run, gh=PF.GH, git=PF.GIT,
                                repo_cache=pr_facts.RepoCache(), budget=budget, stop=stop)

    def poll(self, ready=True):
        self.source.poll()
        if ready:
            eventually(lambda: self.detail()["summary_state"] == S.READY and self.detail()["facts"],
                       what="the summary")
        return self.detail()

    def row(self):
        rows = [r for r in self.source.rows() if r["kind"] == "pr"]
        self.case.assertEqual(len(rows), 1, rows)
        return rows[0]

    def detail(self):
        return self.row()["detail"]

    def act(self, action="merge", head=None, card=None, **kw):
        d = self.detail()
        kw.setdefault("via", "hud")
        return self.actions.act(action, ROW, head or d["head"], card or d["card"], **kw)


_ROOT: Path
_STACK = contextlib.ExitStack()


def setUpModule():
    global _ROOT
    _ROOT = Path(tempfile.mkdtemp(prefix="pr-merge-", dir=os.environ.get("TMPDIR") or None))
    home = _ROOT / "home"
    home.mkdir()
    _STACK.enter_context(patch.dict(os.environ, {
        "HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(home / "run"), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nowhere",
        "JARVIS_GH": "", "JARVIS_GIT": "", "JARVIS_CHAT_GIT_MIRRORS": "",
        "JARVIS_PR_SUMMARY_MODEL": "", "GIT_CONFIG_NOSYSTEM": "1"}))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_DIR"):
        os.environ.pop(key, None)
    _STACK.enter_context(patch.object(config, "V2_DATA_DIR", _ROOT / "data" / "v2"))
    _STACK.enter_context(patch.dict(config.TIERS, {"pr_summary": S.DEFAULT_MODEL}))


def tearDownModule():
    _STACK.close()
    subprocess.run(["rm", "-rf", "--", str(_ROOT)], check=False)


class FakeDaemon(SimpleNamespace):
    """Weakly referable, as a Daemon is (`pr_merge._ACTORS`)."""

    __hash__ = object.__hash__


class Case(unittest.TestCase):
    """A daemon-shaped object with a real PrSource, its own data folder."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_ROOT))
        data = patch.object(config, "V2_DATA_DIR", self.root / "data")
        data.start()
        self.addCleanup(data.stop)
        self.bus = Bus()
        self.daemon = FakeDaemon(bus=self.bus, stores=SimpleNamespace(root=self.root / "data"),
                                 inbox=SimpleNamespace(prs=None, marks=None))
        self.daemon.inbox.prs = inbox_prs.PrSource(self.daemon)
        self.inbox_dir = self.root / "data" / "inbox"

    def prs(self, **kw) -> Prs:
        return Prs(self, self.daemon, **kw)

    def refused(self, fn, status=409, starts=pr_merge.CHANGED):
        with self.assertRaises(pr_merge.Refused) as caught:
            fn()
        self.assertEqual(caught.exception.status, status, caught.exception.word)
        self.assertTrue(caught.exception.word.startswith(starts) or starts in caught.exception.word,
                        caught.exception.word)
        return caught.exception

    def receipts(self) -> list[dict]:
        path = self.inbox_dir / pr_merge.RECEIPTS_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def results(self) -> list[dict]:
        return [r for r in self.receipts() if r["phase"] == "result"]


# -- the argv -----------------------------------------------------------------------------------

class Argv(Case):
    def test_the_exact_merge_argv(self):
        p = self.prs()
        d = p.poll()
        self.assertEqual((d["actions"], d["method"]), (["merge"], "merge"))
        self.assertRegex(d["card"], r"^[0-9a-f]{64}$")
        seen_at_merge = []

        def merge(argv):
            seen_at_merge.append(self.receipts())      # NIT 7: the intent is on disk before gh runs
            return p.run._merge(argv)
        p.run.answers["merge"] = merge
        out = p.act("merge")
        self.assertEqual((out["ok"], out["result"], out["method"]), (True, "merged", "merge"), out)
        (call,) = p.run.acts()
        self.assertEqual(call["argv"], [PF.GH, "pr", "merge", str(N), "--repo", REPO, "--merge",
                                        "--match-head-commit", HEAD])
        self.assertEqual(call["ok_codes"], (0,))
        # The owner's gh with its cache in a private folder made for the act.
        self.assertTrue(call["env"]["XDG_CACHE_HOME"].startswith(str(pr_facts.gh_root())), call["env"])
        self.assertFalse(Path(call["env"]["XDG_CACHE_HOME"]).parent.exists(), "removed after the act")
        for c in p.run.calls:
            for word in FORBIDDEN:
                self.assertNotIn(word, c["argv"], c["argv"])
        # The result came from a fresh read after the merge.
        kinds = [c["kind"] for c in p.run.calls]
        self.assertEqual(kinds[-5:], ["auto", "owner", "rules", "merge", "result"])
        intent, receipt = self.receipts()
        common = {"repo": REPO, "number": N, "head": HEAD, "action": "merge", "method": "merge",
                  "via": "hud", "limited": False}
        self.assertEqual({k: v for k, v in intent.items() if k != "at"}, {**common, "phase": "intent"})
        self.assertEqual({k: v for k, v in receipt.items() if k != "at"},
                         {**common, "phase": "result", "gh": "ok", "result": "merged"})
        self.assertEqual(seen_at_merge, [[intent]])
        path = self.inbox_dir / pr_merge.RECEIPTS_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.inbox_dir).st_mode), 0o700)
        print("ok  merge: exactly gh pr merge N --repo O/R --merge --match-head-commit <head>; receipt 0600")

    def test_squash_only_when_the_card_said_so(self):
        p = self.prs(run=GhRun(repo=PF.repo_answer(merge=False, squash=True)))
        d = p.poll()
        self.assertEqual(d["method"], "squash")
        self.assertIn({"key": "method", "text": "squash merge", "items": [], "more": 0}, d["facts"])
        p.act("merge")
        (call,) = p.run.acts()
        self.assertEqual(call["argv"], [PF.GH, "pr", "merge", str(N), "--repo", REPO, "--squash",
                                        "--match-head-commit", HEAD])
        # The card said merge commit; merge commits were turned off since:
        # never a squash the owner did not see named.
        q_case = Case("run")
        q_case.setUp()
        try:
            q = Prs(self, q_case.daemon)
            q.poll()
            self.assertEqual(q.detail()["method"], "merge")
            q.run.answers["repo"] = PF.repo_answer(merge=False, squash=True)
            self.refused(lambda: q.act("merge"))
            self.assertEqual(q.run.acts(), [])
        finally:
            q_case.doCleanups()
        print("ok  squash: only when the card's method is squash; a method changed since is a 409")

    def test_update_argv_and_result(self):
        p = self.prs()
        p.set_view(mergeStateStatus="BEHIND")
        p.view = PF.view(mergeStateStatus="BEHIND")
        d = p.poll()
        self.assertEqual((d["actions"], d["method"]), (["update"], None))
        self.refused(lambda: p.act("merge"))
        out = p.act("update")
        self.assertEqual((out["ok"], out["result"], out["method"]), (True, "updated", None))
        (call,) = p.run.acts()
        self.assertEqual(call["argv"], [PF.GH, "api", "--method", "PUT", "--include",
                                        f"repos/{REPO}/pulls/{N}/update-branch", "-f", f"expected_head_sha={HEAD}"])
        self.assertNotIn("-F", call["argv"])
        self.assertEqual(call["ok_codes"], (0, 1))
        self.assertEqual([c["kind"] for c in p.run.calls][-3:], ["auto", "update", "result"],
                         "no rules read for an update")
        self.assertEqual((self.results()[0]["action"], self.results()[0]["gh"]), ("update", "http 202"))
        print("ok  update: exactly gh api --method PUT .../update-branch -f expected_head_sha=<head>; 'updated'")

    def test_update_202_and_422(self):
        view = PF.view(mergeStateStatus="BEHIND")
        p = self.prs(view=view, run=GhRun(view=json.dumps(view)))
        p.poll()
        # 202 and the head not moved yet (GitHub updates asynchronously).
        p.run.answers["update"] = ACCEPTED
        self.assertEqual(p.act("update")["result"], "update requested")
        # 422: the head moved before GitHub took it.
        p.run.answers["update"] = UNPROCESSABLE
        e = self.refused(lambda: p.act("update"))
        self.assertIn("head moved", e.word)
        self.assertEqual((self.results()[-1]["result"], self.results()[-1]["gh"]), ("changed", "http 422"))
        # An answer with no status line is never taken for one.
        p.run.answers["update"] = "{}"
        self.assertEqual(p.act("update")["result"], "not updated")
        self.assertEqual(self.results()[-1]["gh"], "unread")
        print("ok  update: 202 'update requested', 422 'changed since you read it' (409), no status read as none")

    def test_results_come_from_the_fresh_read_never_from_gh(self):
        p = self.prs()
        p.poll()
        p.run.answers["merge"] = GhFailed("exit")
        out = p.act("merge")
        self.assertEqual((out["ok"], out["result"]), (False, "not mergeable"))
        self.assertEqual(self.results()[-1]["gh"], "exit")
        # gh said nothing went wrong, but the head moved before it ran.
        p.run.answers["merge"] = ""
        p.run.head_now = HEAD2
        self.assertEqual(p.act("merge")["result"], "head moved")
        p.run.answers["result"] = GhFailed("timeout")
        self.assertEqual(p.act("merge")["result"], "not read again")
        print("ok  results: not mergeable, head moved, not read again: from the read after, never gh's words")

    def test_merge_as_is_is_its_own_action(self):
        p = self.prs()
        p.set_view(mergeStateStatus="UNSTABLE")
        p.view = PF.view(mergeStateStatus="UNSTABLE")
        d = p.poll()
        self.assertEqual(d["actions"], ["merge_as_is"])
        self.refused(lambda: p.act("merge"))
        self.assertEqual(p.act("merge_as_is")["result"], "merged")
        self.assertIn("--merge", p.run.acts()[0]["argv"])
        print("ok  merge as is: only where offered, the same pinned argv")


class Queue(Case):
    """MED 1 of #336's review: never into a merge queue or auto-merge."""

    def check(self, p, **answers):
        p.run.answers.update(answers)
        e = self.refused(lambda: p.act("merge"), 409, pr_merge.QUEUE)
        self.assertEqual(e.word, pr_merge.QUEUE)
        self.assertEqual(p.run.acts(), [])
        self.assertEqual(self.receipts(), [], "no intent: nothing was run")

    def test_auto_merge_on_is_refused(self):
        p = self.prs()
        p.poll()
        self.check(p, auto=json.dumps({"autoMergeRequest": {"mergeMethod": "SQUASH", "enabledAt": "x"}}))
        print("ok  auto-merge already on: 409, nothing run")

    def test_a_merge_queue_base_is_refused(self):
        p = self.prs()
        p.poll()
        self.check(p, rules=json.dumps([{"type": "pull_request"}, {"type": "merge_queue", "parameters": {}}]))
        (rules,) = [c for c in p.run.calls if c["kind"] == "rules"]
        self.assertEqual(rules["argv"], [PF.GH, "api", "--method", "GET",
                                         f"repos/{REPO}/rules/branches/main?per_page={pr_merge.RULES_MAX}"])
        print("ok  a merge_queue rule on the base: 409, nothing run; the rules read is GET")

    def test_unreadable_is_refused(self):
        for answers in ({"auto": GhFailed("exit")}, {"auto": "{}"}, {"auto": "not json"},
                        {"rules": GhFailed("exit")}, {"rules": "{}"}, {"rules": json.dumps([{"x": 1}])},
                        {"rules": json.dumps([{"type": "pull_request"}] * pr_merge.RULES_MAX)}):
            with self.subTest(answers=list(answers)):
                p = self.prs()
                p.poll()
                self.check(p, **answers)
        print("ok  auto-merge or rules unreadable, malformed or maybe cut off: 409 (fails closed)")

    def test_a_users_private_repo_merges_without_the_rules_read(self):
        """The live probe (review of #336): on a user's private repository
        the rules GET answers 403 without GitHub Pro; merge queues exist only
        on an organization's, so the rules are never read there."""
        p = self.prs(run=GhRun(owner=owner_answer("User"), rules=FORBIDDEN_403))
        p.poll()
        self.assertEqual(p.act("merge")["result"], "merged")
        kinds = [c["kind"] for c in p.run.calls]
        self.assertEqual(kinds[-4:], ["auto", "owner", "merge", "result"])
        (owner,) = [c for c in p.run.calls if c["kind"] == "owner"]
        self.assertEqual(owner["argv"], [PF.GH, "api", "--method", "GET", f"repos/{REPO}"])
        # The owner type is cached for an hour; auto-merge is still read each time.
        p.run.merged = False
        p.act("merge")
        self.assertEqual([c["kind"] for c in p.run.calls].count("owner"), 1)
        self.assertEqual([c["kind"] for c in p.run.calls].count("auto"), 2)
        p.actions.clock = lambda: time.monotonic() + pr_merge.OWNER_CACHE_S + 1
        p.run.merged = False
        p.act("merge")
        self.assertEqual([c["kind"] for c in p.run.calls].count("owner"), 2)
        # Auto-merge on a user's repository still refuses.
        p.run.answers["auto"] = json.dumps({"autoMergeRequest": {"mergeMethod": "MERGE"}})
        self.refused(lambda: p.act("merge"), 409, pr_merge.QUEUE)
        print("ok  a user's private repo (rules 403): merges, owner type cached; auto-merge still refuses")

    def test_an_organizations_repo_reads_its_rules(self):
        for rules in (FORBIDDEN_403, json.dumps([{"type": "merge_queue"}])):
            with self.subTest(rules=rules):
                p = self.prs(run=GhRun(owner=owner_answer("Organization"), rules=rules))
                p.poll()
                self.check(p)
        print("ok  an organization's repo: a 403 or a merge_queue rule refuses")

    def test_an_unreadable_owner_type_refuses(self):
        for owner in (GhFailed("exit"), "not json", json.dumps({"owner": {}}), owner_answer("Bot"),
                      json.dumps({"owner": "User"})):
            with self.subTest(owner=owner):
                p = self.prs(run=GhRun(owner=owner))
                p.poll()
                self.check(p)
                self.assertNotIn("rules", [c["kind"] for c in p.run.calls])
        print("ok  an owner type unread or neither User nor Organization: 409, never cached as one")

    def test_update_checks_auto_merge_only(self):
        view = PF.view(mergeStateStatus="BEHIND")
        p = self.prs(view=view, run=GhRun(view=json.dumps(view)))
        p.poll()
        p.run.answers["rules"] = json.dumps([{"type": "merge_queue"}])
        self.assertEqual(p.act("update")["result"], "updated", "a queue base takes an update")
        p.run.answers["auto"] = json.dumps({"autoMergeRequest": {"mergeMethod": "MERGE"}})
        self.refused(lambda: p.act("update"), 409, pr_merge.QUEUE)
        self.assertEqual(len(p.run.acts()), 1)
        print("ok  update: refused under auto-merge, never asks the rules")

    def test_a_queued_pr_reports_queued(self):
        p = self.prs()
        p.poll()
        p.run.answers["merge"] = ""
        p.run.auto_after = {"mergeMethod": "MERGE"}
        out = p.act("merge")
        self.assertEqual((out["ok"], out["result"]), (False, "queued"))
        print("ok  a PR gh queued instead of merging: 'queued', never 'merged'")


# -- stale ----------------------------------------------------------------------------------------

class Stale(Case):
    def test_a_stale_head_runs_nothing(self):
        p = self.prs()
        d = p.poll()
        p.set_view(headRefOid=HEAD2)
        e = self.refused(lambda: p.act("merge", head=d["head"], card=d["card"]))
        self.assertIn("head moved", e.word)
        self.assertEqual(p.run.acts(), [])
        self.assertEqual(self.receipts(), [])
        self.refused(lambda: p.act("merge", head=HEAD2, card=d["card"]))
        self.assertEqual(p.run.acts(), [])
        print("ok  a stale head: 409, nothing run, no receipt")

    def test_a_stale_card_runs_nothing(self):
        p = self.prs()
        d = p.poll()
        self.refused(lambda: p.act("merge", card="0" * 64))
        # A new failing check arrived (facts changed, same head).
        p.set_view(statusCheckRollup=PF.failing_rollup("test"), mergeStateStatus="UNSTABLE")
        self.refused(lambda: p.act("merge", card=d["card"]))
        self.assertEqual(p.run.acts(), [])
        print("ok  a stale card: 409, nothing run")

    def test_a_summary_that_changed_changes_the_card(self):
        p = self.prs()
        d = p.poll()
        rec = next(iter(p.summaries._prs.values()))
        with p.summaries._lock:
            rec.shown = rec.good = {"does": "Something else entirely.", "blocked": ""}
        p.source.refresh([ROW])
        self.assertNotEqual(p.detail()["card"], d["card"])
        self.refused(lambda: p.act("merge", card=d["card"]))
        self.assertEqual(p.run.acts(), [])
        self.assertEqual(p.act("merge")["result"], "merged", "the card the row shows now")
        print("ok  the summary is in the card: one that changed since the owner read it is a 409")

    def test_closed_or_draft_runs_nothing(self):
        for kw, word in (({"state": "CLOSED"}, "closed"), ({"state": "MERGED"}, "merged"),
                         ({"isDraft": True}, "draft")):
            with self.subTest(kw=kw):
                p = self.prs()
                p.poll()
                p.set_view(**kw)
                e = self.refused(lambda: p.act("merge"))
                self.assertIn(word, e.word)
                self.assertEqual(p.run.acts(), [])
        print("ok  closed, merged or draft since: 409, nothing run")

    def test_a_read_that_fails_runs_nothing(self):
        p = self.prs()
        p.poll()
        p.run.answers["view"] = GhFailed("timeout")
        self.refused(lambda: p.act("merge"))
        p.run.answers["view"] = json.dumps(p.view)
        p.run.answers["repo"] = GhFailed("exit")
        self.refused(lambda: p.act("merge"))
        self.assertEqual(p.run.acts(), [])
        print("ok  a fresh read that fails, wholly or in part: 409, nothing run")

    def test_a_409_pokes_a_re_read(self):
        p = self.prs()
        p.poll()
        rec = next(iter(p.summaries._prs.values()))
        self.assertIsNotNone(rec.asked)
        p.source._wake.clear()
        self.refused(lambda: p.act("merge", card="0" * 64))
        self.assertIsNone(rec.asked, "the next poll reads it again")
        self.assertTrue(p.source._wake.is_set(), "a poll was poked")
        print("ok  a 409 pokes a re-read and a poll")

    def test_bad_input_is_400(self):
        p = self.prs()
        d = p.poll()
        for args in (("rebase", ROW, d["head"], d["card"]), ("merge", ROW, "x", d["card"]),
                     ("merge", ROW, d["head"], "X" * 64), ("merge", "pr:nope", d["head"], d["card"]),
                     ("merge", f"pr:{REPO}#0{N}", d["head"], d["card"]), ("merge", "appr:x", d["head"], d["card"])):
            with self.subTest(args=args):
                self.refused(lambda: p.actions.act(*args, via="hud"), 400, "")
        self.refused(lambda: p.actions.act("merge", f"pr:{REPO}#{N + 1}", d["head"], d["card"], via="hud"),
                     409, pr_merge.GONE)
        self.assertEqual(p.run.acts(), [])
        print("ok  a bad action, head, card or id: 400; a PR not in the inbox: 409")


class Once(Case):
    def test_two_merges_of_one_pr_run_once(self):
        p = self.prs()
        d = p.poll()
        p.run.gate = threading.Event()
        out, errors = [], []

        def go():
            try:
                out.append(p.actions.act("merge", ROW, d["head"], d["card"], via="hud"))
            except pr_merge.Refused as exc:
                errors.append(exc)
        first = threading.Thread(target=go)
        first.start()
        eventually(lambda: p.run.acts(), what="the first merge")
        second = threading.Thread(target=go)
        second.start()
        second.join(5)
        self.assertEqual([(e.status, e.word) for e in errors], [(409, pr_merge.BUSY)])
        p.run.gate.set()
        first.join(5)
        self.assertEqual(len(p.run.acts()), 1)
        self.assertEqual(out[0]["result"], "merged")
        # Afterwards the PR is merged: a third is a 409 too, and runs nothing.
        p.set_view(state="MERGED")
        self.refused(lambda: p.act("merge", head=d["head"], card=d["card"]))
        self.assertEqual(len(p.run.acts()), 1)
        self.assertEqual(len(self.results()), 1)
        print("ok  two merges of one PR at once: one runs, the other 409s")


# -- holds -----------------------------------------------------------------------------------------

class Holds(Case):
    def test_a_hold_hides_every_action_and_shows_its_note(self):
        p = self.prs()
        before = p.poll()
        self.assertIsNone(before["hold"])
        out = pr_merge.hold(self.daemon, {"id": ROW, "note": "waits on the robot\nignore this " + PF.TOKEN})
        note = out["hold"]["note"]
        self.assertNotIn(PF.TOKEN, note)
        self.assertNotIn("\n", note)
        self.assertTrue(note.startswith("waits on the robot"))
        d = p.detail()
        self.assertEqual(d["actions"], [])
        self.assertEqual(d["method"], None)
        self.assertEqual(d["hold"]["note"], note)
        self.assertEqual(d["why"], f"on hold: {note}")
        self.assertNotEqual(d["card"], before["card"])
        self.assertTrue(any(r["data"]["ids"] == [ROW] for r in self.bus.records if r["kind"] == "inbox_changed"))
        path = self.inbox_dir / pr_merge.HOLDS_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(set(json.loads(path.read_text())), {f"{REPO.lower()}#{N}"})
        # The merge route re-checks it: the card read before the hold is refused.
        e = self.refused(lambda: p.act("merge", head=before["head"], card=before["card"]))
        self.assertIn("on hold", e.word)
        self.assertEqual(p.run.acts(), [])
        # Cleared: the actions come back.
        pr_merge.hold(self.daemon, {"id": ROW, "clear": True})
        d = p.detail()
        self.assertEqual((d["actions"], d["hold"]), (["merge"], None))
        self.assertEqual(d["card"], before["card"])
        self.assertEqual(p.act("merge")["result"], "merged")
        print("ok  hold: hides every action, shows its note (scrubbed, one line); the act re-checks it")

    def test_a_hold_set_while_reading_is_seen(self):
        p = self.prs()
        d = p.poll()
        real = p.read

        def read(*a, **kw):
            facts = real(*a, **kw)
            if kw.get("budget") == pr_merge.READ_BUDGET_S:
                pr_merge.holds_at(self.inbox_dir).set(REPO, N, "late", "2026-10-29T00:00:00+00:00")
            return facts
        p.actions.read = read
        e = self.refused(lambda: p.act("merge", head=d["head"], card=d["card"]))
        self.assertIn("on hold", e.word)
        self.assertEqual(p.run.acts(), [])
        print("ok  a hold set during the act's read: 409, nothing run")

    def test_a_hold_file_that_cannot_be_read_holds_everything(self):
        p = self.prs()
        d = p.poll()
        path = self.inbox_dir / pr_merge.HOLDS_FILE
        path.write_text("{not json")
        store = pr_merge.holds_at(self.inbox_dir)
        store._holds = None                 # read again
        p.source.refresh([ROW])
        self.assertEqual(p.detail()["actions"], [])
        self.assertEqual(p.detail()["hold"]["note"], pr_merge.UNREADABLE)
        self.refused(lambda: p.act("merge", head=d["head"], card=d["card"]))
        self.refused(lambda: pr_merge.hold(self.daemon, {"id": ROW, "note": "x"}), 503, "")
        self.assertEqual(path.read_text(), "{not json", "never overwritten")
        path.unlink()
        p.source.refresh([ROW])
        self.assertEqual(p.detail()["actions"], ["merge"])
        print("ok  a hold file that cannot be read holds every PR, and is never overwritten")

    def test_shapes(self):
        p = self.prs()
        p.poll()
        for body in ({"id": ROW}, {"id": ROW, "clear": False}, {"id": ROW, "clear": True, "note": "x"},
                     {"id": ROW, "note": 3}, {"id": "pr:bad", "note": "x"}):
            with self.subTest(body=body):
                self.refused(lambda: pr_merge.hold(self.daemon, body), 400, "")
        self.refused(lambda: pr_merge.hold(self.daemon, {"id": f"pr:{REPO}#99", "note": "x"}), 409,
                     pr_merge.GONE)
        # Clearing works on a PR no longer listed.
        self.assertEqual(pr_merge.hold(self.daemon, {"id": f"pr:{REPO}#99", "clear": True}),
                         {"ok": True, "hold": None})
        long = pr_merge.hold(self.daemon, {"id": ROW, "note": "y" * 1000})["hold"]["note"]
        self.assertLessEqual(len(long), pr_merge.NOTE_MAX)
        print("ok  hold shapes: note or clear, a listed PR to set, capped notes")


# -- the real runner ------------------------------------------------------------------------------

FAKE_GH = """#!/bin/sh
printf '%s\\0' "$@" >> "$FAKE_GH_LOG"
printf '\\n' >> "$FAKE_GH_LOG"
echo "stderr {token}" >&2
case "$*" in
  *"--json state,headRefOid,autoMergeRequest") echo '{{"state":"MERGED","headRefOid":"{head}","autoMergeRequest":null}}' ;;
  *"--json autoMergeRequest") echo '{{"autoMergeRequest":null}}' ;;
  *rules/branches/*) echo '[]' ;;
  *"GET repos/{repo}") echo '{{"owner":{{"login":"Owner","type":"Organization"}}}}' ;;
  *) echo "stdout {token}" ;;
esac
exit 0
"""


class RealGh(Case):
    def test_the_real_runner_never_reads_what_gh_says(self):
        p = self.prs()
        d = p.poll()
        facts = p.summaries._prs[S.pr_key(REPO, N)].facts
        log = self.root / "gh.log"
        gh = self.root / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(FAKE_GH.format(token=PF.TOKEN, head=HEAD, repo=REPO))
        gh.chmod(0o755)
        actions = pr_merge.PrActions(self.daemon, gh=str(gh), read=lambda *a, **k: facts)
        pr_merge.install(self.daemon, actions)
        with patch.dict(os.environ, {"FAKE_GH_LOG": str(log)}), \
                patch("jarvis.v2.chat_git._owner_env", lambda: {"PATH": "/usr/bin:/bin",
                                                                "HOME": os.environ["HOME"],
                                                                "FAKE_GH_LOG": str(log)}), \
                self.assertLogs("jarvis.pr_merge", logging.INFO) as logs:
            out = actions.act("merge", ROW, d["head"], d["card"], via="hud")
        self.assertEqual(out["result"], "merged")
        lines = [line.split("\0")[:-1] for line in log.read_text().split("\n") if line]
        self.assertEqual(lines[0], ["pr", "view", str(N), "--repo", REPO, "--json", "autoMergeRequest"])
        self.assertEqual(lines[1], ["api", "--method", "GET", f"repos/{REPO}"])
        self.assertEqual(lines[2], ["api", "--method", "GET",
                                    f"repos/{REPO}/rules/branches/main?per_page={pr_merge.RULES_MAX}"])
        self.assertEqual(lines[3], ["pr", "merge", str(N), "--repo", REPO, "--merge", "--match-head-commit", HEAD])
        self.assertEqual(lines[4], ["pr", "view", str(N), "--repo", REPO, "--json",
                                    "state,headRefOid,autoMergeRequest"])
        self.assertEqual(len(lines), 5)
        seen = json.dumps(out) + json.dumps(self.receipts()) + "\n".join(logs.output)
        self.assertNotIn(PF.TOKEN, seen)
        self.assertNotIn("stderr", seen)
        print("ok  the real runner: the pinned argv on disk; gh's output and error output never read")


# -- the routes on a real daemon ----------------------------------------------------------------

rac = _load("remote_auth_check")
rach = _load("remote_answer_check")


class Routes(rac.Remote):
    def setUp(self):
        super().setUp()
        self.prs = Prs(self, self.d)

    def owner(self, path, body):
        return self.hud("POST", path, body)

    def test_owner_only_on_the_hud_listener(self):
        d = self.prs.poll()
        bodies = {"/inbox/pr/merge": {"id": ROW, "head": d["head"], "card": d["card"]},
                  "/inbox/pr/update": {"id": ROW, "head": d["head"], "card": d["card"]},
                  "/inbox/pr/hold": {"id": ROW, "note": "x"}, "/inbox/pr/fix": {"id": ROW}}
        for path, body in bodies.items():
            with self.subTest(path=path):
                # The API listener, every tool's HTTP client; its own Origin too.
                self.assertEqual(self.hud("POST", path, body, port=self.d.port)[0], 403)
                self.assertEqual(self.hud("POST", path, body, port=self.d.port, origin=False)[0], 403)
                # The HUD's listener without its Origin is a script, not the window.
                self.assertEqual(self.hud("POST", path, body, origin=False)[0], 403)
        self.assertEqual(self.prs.run.acts(), [])
        self.assertIsNone(pr_merge.holds_at(Path(self.d.stores.root) / "inbox").get(REPO, N))
        # A rebinding Host on the HUD's port with a matching Origin.
        host = f"evil.example:{self.d.face_port}"
        status, _, _ = rac.raw(self.d.face_port, "POST", "/inbox/pr/merge",
                               [("Host", host), ("Origin", f"http://{host}"), ("Content-Type", "application/json")],
                               json.dumps(bodies["/inbox/pr/merge"]).encode())
        self.assertIn(status, (400, 403))
        self.assertEqual(self.prs.run.acts(), [])
        # The window: merge works.
        status, out = self.owner("/inbox/pr/merge", bodies["/inbox/pr/merge"])
        self.assertEqual((status, out["result"]), (200, "merged"), out)
        print("ok  /inbox/pr/*: owner_only on the HUD listener; the API listener and a script refused")

    def test_the_own_host_check_is_the_routes_own(self):
        """NIT 5 of #336's review: as `POST /approvals`, the route itself
        requires the HUD listener's own Host, not only `check_origin` in
        front of it. Called directly with a handler `owner_only` passes
        (the HUD's port, Origin equal to its Host) but whose Host is not
        `127.0.0.1:<port>` or `localhost:<port>`."""
        from jarvis.v2.daemon import APIError
        d = self.prs.poll()
        body = {"id": ROW, "head": d["head"], "card": d["card"]}
        for host, want in ((f"127.0.0.2:{self.d.face_port}", 403), (f"box.lan:{self.d.face_port}", 403),
                           (f"127.0.0.1:{self.d.face_port}", 200)):
            handler = SimpleNamespace(
                server=SimpleNamespace(server_address=("127.0.0.1", self.d.face_port)), command="POST",
                headers={"Host": host, "Origin": f"http://{host}"}, _body=lambda: dict(body))
            with self.subTest(host=host):
                if want == 403:
                    with self.assertRaises(APIError) as caught:
                        pr_merge.hud_route(handler, self.d, ["inbox", "pr", "merge"], {})
                    self.assertEqual(caught.exception.status, 403)
                    self.assertEqual(self.prs.run.acts(), [])
                else:
                    status, out = pr_merge.hud_route(handler, self.d, ["inbox", "pr", "merge"], {})
                    self.assertEqual((status, out["result"]), (200, "merged"))
        print("ok  the route's own Host check: 127.0.0.2 or a LAN name on the HUD port with its Origin is 403")

    def test_fix_is_pm_es_and_shapes(self):
        d = self.prs.poll()
        # PM-E's route, behind the same tests: no PrFixes attached here.
        self.assertEqual(self.owner("/inbox/pr/fix", {"id": ROW, "head": d["head"], "card": d["card"]})[0], 503)
        self.assertEqual(self.owner("/inbox/pr/fix/cancel", {"id": ROW})[0], 503)
        self.assertEqual(self.owner("/inbox/pr/fix/other", {"id": ROW})[0], 404)
        self.assertEqual(self.hud("GET", "/inbox/pr/merge")[0], 405)
        self.assertEqual(self.owner("/inbox/pr/rebase", {"id": ROW})[0], 404)
        self.assertEqual(self.owner("/inbox/pr/merge", {"id": ROW, "head": d["head"]})[0], 400)
        self.assertEqual(self.owner("/inbox/pr/merge", {"id": ROW, "head": d["head"], "card": d["card"],
                                                        "admin": True})[0], 400)
        self.assertEqual(self.owner("/inbox/pr/merge", {"id": ROW, "head": d["head"], "card": d["card"],
                                                        "action": "update"})[0], 400)
        status, body = self.owner("/inbox/pr/update", {"id": ROW, "head": d["head"], "card": d["card"]})
        self.assertEqual(status, 409, body)
        self.assertTrue(body["error"].startswith(pr_merge.CHANGED), body)
        self.assertEqual(self.prs.run.acts(), [])
        print("ok  Fix is PM-E's (503 unattached), 405, 404, 400 for shapes, 409 for an action not offered")

    def test_hold_at_the_desk_only(self):
        self.prs.poll()
        status, out = self.owner("/inbox/pr/hold", {"id": ROW, "note": "robot"})
        self.assertEqual((status, out["hold"]["note"]), (200, "robot"), out)
        self.assertEqual(self.prs.detail()["actions"], [])
        status, out = self.owner("/inbox/pr/hold", {"id": ROW, "clear": True})
        self.assertEqual((status, out["hold"]), (200, None))
        self.assertEqual(self.prs.detail()["actions"], ["merge"])
        print("ok  hold: set and cleared from the HUD")


class Phone(rach.Phone):
    def setUp(self):
        super().setUp()
        self.secure_ok()

    def wire(self, **kw) -> Prs:
        self.prs = Prs(self, self.d, **kw)
        self.prs.poll()
        return self.prs

    def shown(self) -> dict:
        """What the phone shows of the row: its head and card."""
        d = self.prs.detail()
        return {"head": d["head"], "card": d["card"]}

    def options(self, action, row=ROW, cookie=None, shown=None):
        return self.phone("POST", pr_merge.PHONE_OPTIONS, {"id": row, "action": action, **(shown or self.shown())},
                          cookie=cookie or self.cookie)

    def act(self, body, cookie=None):
        return self.phone("POST", pr_merge.PHONE_ACT, body, cookie=cookie or self.cookie)

    def signed(self, action, cookie=None):
        opts = self.shown()
        status, _, body = self.options(action, cookie=cookie, shown=opts)
        self.assertEqual(status, 200, body)
        return self.a.get(body["publicKey"]), opts

    def verified(self, action, **extra):
        credential, opts = self.signed(action)
        return self.act({"id": ROW, "action": action, "head": opts["head"], "card": opts["card"],
                         "assertion": credential, **extra})


class PhoneActs(Phone):
    def test_subject_binding(self):
        p = self.wire()
        d = p.detail()
        status, _, body = self.options("merge")
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"action", "publicKey"}, "never echoes a head or card (MED 2)")
        want = pr_merge.subject(REPO, N, d["head"], d["card"], "merge")
        self.assertRegex(want, r"^pr:[0-9a-f]{56}$")
        entries = [c for c in self.auth._challenges.values() if c.get("req_id") == want]
        self.assertEqual([(c["decision"], c["family"]) for c in entries], [("merge", "pr")])
        credential = self.a.get(body["publicKey"])
        # Bound to the head, the card and the action: another of any is refused.
        for change in ({"head": HEAD2}, {"card": "0" * 64}, {"action": "update"}):
            with self.subTest(change=change):
                credential, opts = self.signed("merge")
                status, _, got = self.act({"id": ROW, "action": "merge", "head": d["head"], "card": d["card"],
                                           "assertion": credential, **change})
                self.assertEqual(status, 401, got)
        self.assertEqual(p.run.acts(), [])
        status, _, got = self.verified("merge")
        self.assertEqual((status, got["result"], got["authority"]), (200, "merged", "hud"), got)
        self.assertEqual(self.receipts_via(), ["phone"])
        print("ok  phone: one assertion per action, bound to pr:<56 hex> of repo, number, head, card, action")

    def test_options_bind_what_the_phone_showed(self):
        """MED 2 of #336's review: a push between the phone's view and its
        options is a 409, never a challenge for the new head."""
        p = self.wire()
        seen = self.shown()
        p.set_view(headRefOid=HEAD2)
        p.view = PF.view(headRefOid=HEAD2)
        p.source.poll()
        eventually(lambda: p.detail()["head"] == HEAD2 and p.detail()["actions"] == ["merge"],
                   what="the new head's row")
        self.assertNotEqual(self.shown()["card"], seen["card"])
        status, _, body = self.options("merge", shown=seen)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], pr_merge.CHANGED)
        status, _, body = self.options("merge", shown={**seen, "card": self.shown()["card"]})
        self.assertEqual(status, 409, ("the new card with the old head", body))
        for bad in ({"head": seen["head"]}, {"card": seen["card"]}, {}):
            st = self.phone("POST", pr_merge.PHONE_OPTIONS, {"id": ROW, "action": "merge", **bad},
                            cookie=self.cookie)[0]
            self.assertEqual(st, 400, bad)
        self.assertFalse([c for c in self.auth._challenges.values() if c.get("family") == "pr"])
        status, _, body = self.options("merge")
        self.assertEqual(status, 200, body)
        print("ok  phone options: bound to the head and card the phone showed; a push since is 409")

    def receipts_via(self):
        path = Path(self.d.stores.root) / "inbox" / pr_merge.RECEIPTS_FILE
        return ([json.loads(x)["via"] for x in path.read_text().splitlines()
                 if json.loads(x)["phase"] == "result"] if path.exists() else [])

    def test_cross_family_assertions_are_refused_both_ways(self):
        self.wire()
        request, out, worker = self.ask()
        approval_row = f"appr:{request.req_id}"
        # An approval's Allow assertion is never a PR action.
        st, _, opts = self.phone("POST", f"/m/api/inbox/{approval_row}/answer/options", {"decision": "allow"},
                                 cookie=self.cookie)
        self.assertEqual(st, 200, opts)
        allow = self.a.get(opts["publicKey"])
        d = self.prs.detail()
        status, _, got = self.act({"id": ROW, "action": "merge", "head": d["head"], "card": d["card"],
                                   "assertion": allow})
        self.assertEqual(status, 401, got)
        self.assertEqual(self.prs.run.acts(), [])
        # A PR action's assertion never answers an approval.
        merge, _ = self.signed("merge")
        status, _, got = self.phone("POST", f"/m/api/inbox/{approval_row}/answer",
                                    {"decision": "allow", "assertion": merge}, cookie=self.cookie)
        self.assertEqual(status, 401, got)
        self.assertTrue(self.pending(request))
        # Nor can a subject of one family ask for another's answer.
        session = next(iter(self.auth._sessions.values()))
        subject = pr_merge.subject(REPO, N, HEAD, "a" * 64, "merge")
        for req_id, decision in ((subject, "allow"), (subject, "cancel"), (request.req_id, "merge"),
                                 ("task:0123abcd", "merge"), (f"defer:{request.req_id}", "merge"),
                                 ("pr:" + "a" * 55, "merge"), ("pr:" + "A" * 56, "merge")):
            with self.subTest(req_id=req_id, decision=decision):
                with self.assertRaises(remote_auth.AuthError) as caught:
                    self.auth.answer_options({"token_sha256": session["token_sha256"]}, req_id, decision)
                self.assertEqual(caught.exception.status, 400)
        self.post_answer(approval_row, {"decision": "deny"})
        worker.join(5)
        print("ok  cross-family: an approval's assertion is no PR action, a PR's answers no approval")

    def limited(self):
        rach._flip_serve(self)
        result = self.d.secure.check()
        self.assertFalse(result["ok"], result)

    def test_limited_and_flagged_is_refused(self):
        view = PF.view(files=[{"path": "CLAUDE.md", "additions": 1, "deletions": 0}])
        run = GhRun(view=json.dumps(view), diff=PF.diff_of(("CLAUDE.md", "-x\n+y")))
        p = self.wire(view=view, run=run)
        self.assertTrue(p.detail()["flags"]["flagged"])
        credential, opts = self.signed("merge")          # taken while every check held
        self.limited()
        status, _, got = self.act({"id": ROW, "action": "merge", "head": opts["head"], "card": opts["card"],
                                   "assertion": credential})
        self.assertEqual(status, 409, got)
        self.assertTrue(got["error"].endswith(pr_merge.AT_DESK), got)
        status, _, got = self.options("merge")
        self.assertEqual(status, 409, got)
        self.assertTrue(got["error"].endswith(pr_merge.AT_DESK), got)
        self.assertEqual(p.run.acts(), [])
        # A fully checked phone may merge it.
        self.system.bad.clear()
        status, _, got = self.verified("merge")
        self.assertEqual((status, got["authority"]), (200, "hud"), got)
        print("ok  limited phone + safety files: answer it at the desk; a fully checked phone may")

    def test_limited_and_unflagged_is_allowed(self):
        p = self.wire()
        self.assertFalse(p.detail()["flags"]["flagged"])
        self.limited()
        status, _, got = self.verified("merge")
        self.assertEqual((status, got["result"], got["authority"]), (200, "merged", "limited"), got)
        path = Path(self.d.stores.root) / "inbox" / pr_merge.RECEIPTS_FILE
        receipt = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual((receipt["via"], receipt["limited"]), ("phone", True))
        print("ok  limited phone, no safety files: Merge goes, recorded as limited")

    def test_a_reused_assertion_is_refused(self):
        view = PF.view(mergeStateStatus="BEHIND")
        p = self.wire(view=view, run=GhRun(view=json.dumps(view)))
        self.assertEqual(p.detail()["actions"], ["update"])
        credential, opts = self.signed("update")
        body = {"id": ROW, "action": "update", "head": opts["head"], "card": opts["card"], "assertion": credential}
        self.assertEqual(self.act(body)[0], 200)
        status, _, got = self.act(body)
        self.assertEqual(status, 409, got)
        self.assertEqual(len(p.run.acts()), 1)
        print("ok  a reused assertion: 409 (its challenge was spent), nothing run again")

    def test_the_challenge_budget_is_its_own(self):
        self.wire()
        cap = remote_auth.MAX_ANSWER_CHALLENGES_PER_SESSION
        for _ in range(cap * 3):              # the same PR and action replaces its own
            self.assertEqual(self.options("merge")[0], 200)
        # Another family's budget is untouched by PR prompts...
        request, _, worker = self.ask()
        st = self.phone("POST", f"/m/api/inbox/appr:{request.req_id}/answer/options", {"decision": "allow"},
                        cookie=self.cookie)[0]
        self.assertEqual(st, 200)
        # ...and the PR family is capped per session: distinct subjects past it 429.
        session = next(iter(self.auth._sessions.values()))
        s = {"token_sha256": session["token_sha256"]}
        for i in range(cap - 1):
            self.auth.answer_options(s, pr_merge.subject(REPO, N, HEAD, f"{i:064x}", "merge"), "merge")
        with self.assertRaises(remote_auth.AuthError) as caught:
            self.auth.answer_options(s, pr_merge.subject(REPO, N, HEAD, "f" * 64, "merge"), "merge")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("pr", remote_auth.ANSWER_FAMILIES)
        self.post_answer(f"appr:{request.req_id}", {"decision": "deny"})
        worker.join(5)
        print("ok  the pr family's own budget: 4 per session, apart from approvals")

    def test_the_phone_scrub_sends_it_to_the_desk(self):
        secret = "password=" + rach.PASSWORD
        facts = replace(PF.F(), files=(FileChange(f"docs/{secret}.txt", 1, 0, (), False),))
        p = self.wire(facts=facts)
        self.assertEqual(p.detail()["actions"], ["merge"])
        status, _, got = self.options("merge")
        self.assertEqual(status, 409, got)
        self.assertTrue(got["error"].endswith(pr_merge.AT_DESK), got)
        # The act re-checks it with the fresh facts.
        d = p.detail()
        session = next(iter(self.auth._sessions.values()))
        bound = pr_merge.subject(REPO, N, d["head"], d["card"], "merge")
        opts = self.auth.answer_options({"token_sha256": session["token_sha256"]}, bound, "merge")
        status, _, got = self.act({"id": ROW, "action": "merge", "head": d["head"], "card": d["card"],
                                   "assertion": self.a.get(opts)})
        self.assertEqual(status, 409, got)
        self.assertTrue(got["error"].endswith(pr_merge.AT_DESK), got)
        self.assertEqual(p.run.acts(), [])
        # The phone's inbox shows the scrubbed path, never the value.
        listing_ = json.dumps(self.phone("GET", "/m/api/inbox", cookie=self.cookie)[2])
        self.assertNotIn(rach.PASSWORD, listing_)
        # At the desk it merges.
        self.assertEqual(p.actions.act("merge", ROW, d["head"], d["card"], via="hud")["result"], "merged")
        print("ok  a facts or file list the phone's scrub would change: answer it at the desk")

    def test_no_hold_and_no_fix_on_the_phone(self):
        self.wire()
        self.assertEqual(self.phone("POST", "/m/api/inbox/pr/hold", {"id": ROW, "note": "x"},
                                    cookie=self.cookie)[0], 404)
        self.assertEqual(self.options("fix")[0], 409)       # Fix is not offered on this row
        self.assertEqual(self.options("hold")[0], 400)
        self.assertEqual(self.phone("POST", pr_merge.PHONE_OPTIONS, {"id": ROW, "action": "merge", "note": "x"},
                                    cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("GET", pr_merge.PHONE_OPTIONS, cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("POST", pr_merge.PHONE_OPTIONS, {"id": ROW, "action": "merge"})[0], 401)
        d = self.prs.detail()
        status, _, got = self.act({"id": ROW, "action": "merge", "head": d["head"], "card": d["card"]})
        self.assertEqual(status, 401, got)
        self.assertIsNone(pr_merge.holds_at(Path(self.d.stores.root) / "inbox").get(REPO, N))
        print("ok  phone: no hold route, Fix only where offered, no session 401, no assertion 401")

    def test_a_held_pr_offers_nothing_on_the_phone(self):
        self.wire()
        credential, opts = self.signed("merge")
        status, _ = self.hud("POST", "/inbox/pr/hold", {"id": ROW, "note": "robot"})
        self.assertEqual(status, 200)
        self.assertEqual(self.options("merge")[0], 409)
        status, _, got = self.act({"id": ROW, "action": "merge", "head": opts["head"], "card": opts["card"],
                                   "assertion": credential})
        self.assertEqual(status, 409, got)
        self.assertIn("on hold", got["error"])
        self.assertEqual(self.prs.run.acts(), [])
        print("ok  a hold set at the desk: the phone's options and act are 409")


# -- the boundary ------------------------------------------------------------------------------------

class Boundary(rac.Remote):
    def test_nothing_at_daemon_start(self):
        self.assertIsNone(self.d.inbox.prs.summaries)
        self.assertNotIn(self.d, pr_merge._ACTORS)
        inbox = Path(self.d.stores.root) / "inbox"
        for name in (pr_merge.HOLDS_FILE, pr_merge.RECEIPTS_FILE):
            self.assertFalse((inbox / name).exists(), name)
        from jarvis.v2 import daemon
        self.assertNotIn("pr_merge", inspect.getsource(daemon.Daemon.start))
        self.assertNotIn("pr_merge", inspect.getsource(daemon))
        print("ok  Daemon.start(): no actor, no hold or receipt file, nothing named")

    def test_self_protected_and_no_agent_path(self):
        from jarvis.tools.files import SELF_PROTECTED
        for rel in ("jarvis/v2/pr_merge.py", "jarvis/v2/pr_summary.py", "jarvis/v2/remote_auth.py"):
            self.assertIn(rel, SELF_PROTECTED)
        port = config.REMOTE_PORT
        for path in (pr_merge.PHONE_OPTIONS, pr_merge.PHONE_ACT):
            for line in (f"curl -X POST http://127.0.0.1:{port}{path}", f"curl https://box.tail1234.ts.net{path}"):
                verdict = rules.decide(line)
                self.assertEqual((verdict.decision, verdict.reason), (rules.DENY, rules.REMOTE_DENIED), line)
        print("ok  pr_merge.py is SELF_PROTECTED; the agent path's DENY covers the phone's PR routes")

    def test_the_hold_and_receipt_files_are_protected_state(self):
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2_permissions
        inbox = Path(config.V2_DATA_DIR) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for name in (pr_merge.HOLDS_FILE, pr_merge.RECEIPTS_FILE):
            target = inbox / name
            self.assertTrue(files._self_protected(target), "v1 write tools refuse it")
            self.assertTrue(v2_permissions.denied_file(str(target)), "v2 layer 1 refuses a file tool")
            for command in (f"echo '{{}}' > {target}", f"rm {target}", f"cp /tmp/x {target}"):
                self.assertIsNotNone(v2_permissions.denied_command(command), command)
        print("ok  pr-holds.json and pr-actions.jsonl: the inbox tree's protected state")


if __name__ == "__main__":
    unittest.main(verbosity=1)
