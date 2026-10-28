"""Inbox B2 (owner inbox design `docs/plans/2026-10-24-owner-inbox-design.md`,
IN-5, IN-6, §3.2, §4, §5 "Check", §6, §7): live checks. `owner_post(kind=
"check")`, the check store, the owner's step marks (the one way a result is
written), the deployed test that gates a mark, the runbook import, the rows.

Free and hermetic: every daemon runs on port 0 over a temporary V2_DATA_DIR
and temporary config (never 8402/8403/8405, never the owner's state); the
deployed test runs on a fake `git` (no network, no real repository); the
Claude and Codex chats are `owner_post_check`'s harness.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \\
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \\
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \\
    .venv/bin/python tests/v2/live_checks_check.py

TEST -> MUTATION
================
  a check posted off the desk                 Binding.test_off_desk_and_between_turns_are_refused
  drop the open-per-chat cap                  Caps.test_five_open_per_chat
  drop a field cap                            Caps.test_field_caps_refuse_never_cut
  drop the scrub                              Scrub.test_every_field_is_scrubbed
  mark without the deployed test              Marks.test_an_unconfirmed_build_refuses_the_mark
  the deployed test ignores the HUD build     Deploy.test_a_hud_change_needs_the_installed_hud
  the deployed test trusts an unknown head    Deploy.test_unknown_is_never_deployed
  the mark route not owner-only               Marks.test_only_the_owner_marks
  a tool or route names the mark              NoResultPath.test_only_the_inbox_names_the_mark
  import follows a symlink / leaves plans/    Import.test_paths_outside_docs_plans_are_refused
  import saves on preview                     Import.test_the_runbook_yields_one_draft_per_heading_unsaved
  O_NOFOLLOW dropped on the checks folder     Store.test_a_symlinked_folder_is_refused
  merged-first order within rank 7            Rows.test_rank_7_merged_first_then_done_at_8
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import owner_post_check as OP  # noqa: E402  (its harness and helpers only)
import chat_relay_check as CR  # noqa: E402
from jarvis import config  # noqa: E402
from jarvis.v2 import checks, decisions, inbox  # noqa: E402
from jarvis.v2.provider import UserMessage  # noqa: E402

CLAUDE, CODEX, FAST = OP.CLAUDE, OP.CODEX, OP.FAST
SENTINEL = "SENTINEL-check-text-41c9"
CHK_ID = re.compile(r"\bc[0-9a-f]{12}\b")
RUNBOOK = ROOT / "docs" / "plans" / "2026-10-16-owner-live-checks.md"
SHA = {name: (name * 40)[:40] for name in "abcdef"}


def check_args(**over):
    base = {"kind": "check", "title": "Quick review in the Diff tab",
            "steps": [{"do": "Press Quick review.", "expect": "A spinner shows."},
                      {"do": "Wait for the result.", "expect": "A panel lists findings."}]}
    base.update(over)
    return base


class FakeGit:
    """`git` for `checks.Deployment`: PR -> merge commit, ancestry pairs, the
    commits that change hud/. Counts calls."""

    def __init__(self, merges=None, ancestors=(), hud=(), fail=False):
        self.merges, self.ancestors, self.hud, self.fail = dict(merges or {}), set(ancestors), set(hud), fail
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.fail:
            return None
        if args[0] == "log":
            grep = next(a for a in args if a.startswith("--grep="))
            n = int(re.search(r"#(\d+) from ", grep).group(1))
            sha = self.merges.get(n)
            return (0, sha + "\n") if sha else (0, "")
        if args[0] == "merge-base":
            return (0, "") if (args[2], args[3]) in self.ancestors else (1, "")
        if args[0] == "diff":
            return (1, "") if args[-3] in self.hud else (0, "")
        raise AssertionError(args)


def deployed(prs=(), *, merges=None, hud=None):
    """A Deployment where every given PR's merge is in the code and the HUD."""
    merges = merges or {n: SHA["a"] for n in prs}
    ancestors = {(sha, SHA["f"]) for sha in merges.values()} | {(sha, SHA["e"]) for sha in merges.values()}
    return checks.Deployment(git=FakeGit(merges, ancestors, hud or ()), code_head=SHA["f"],
                             hud_head=lambda: SHA["e"])


class Base(OP.Base):
    @property
    def cstore(self):
        return self.d.inbox.checks

    def post_check(self, thread, ctx="parent", **over):
        return self.mcp(thread, "owner_post", check_args(**over), ctx)

    def check_posted(self, got) -> str:
        status, body = got
        self.assertEqual(status, 200, body)
        self.assertFalse(body["isError"], body)
        self.assertTrue(body["text"].startswith("Posted check "), body["text"])
        return CHK_ID.search(body["text"]).group(0)

    def service_check(self, thread, **over):
        a = check_args(**over)
        return self.d.owner_posts.post(self.caller(thread), a["kind"], a.get("question"), a.get("options"),
                                       a.get("default"), a.get("why"), title=a.get("title"),
                                       steps=a.get("steps"), prs=a.get("prs"))

    def desk_check(self, provider=CLAUDE, **over):
        thread = self.chat(provider)
        self.turn(thread)
        cid = self.check_posted(self.post_check(thread, **over))
        self.finish(thread)
        return thread, cid

    def no_checks(self):
        self.assertEqual(self.cstore.all(), [])


# --- the contract ---------------------------------------------------------------------------


class Contract(unittest.TestCase):
    def test_the_bounds_are_pinned(self):
        self.assertEqual((checks.OPEN_PER_THREAD, checks.PER_THREAD_DAY, checks.PER_DAY, checks.TITLE_MAX,
                          checks.STEPS_MIN, checks.STEPS_MAX, checks.DO_MAX, checks.EXPECT_MAX, checks.PRS_MAX,
                          checks.NOTE_MAX, checks.KEEP, checks.DONE_WINDOW),
                         (5, 10, 20, 200, 1, 30, 1000, 1000, 10, 1000, 500, timedelta(days=7)))
        self.assertEqual(decisions.KINDS, ("decision", "check"))
        self.assertEqual(checks.VERDICTS, ("pass", "fail", "skip"))
        self.assertEqual(inbox.RANK_CHECK, 7)

    def test_the_module_and_folder_are_protected(self):
        from jarvis.tools import files
        self.assertIn("jarvis/v2/checks.py", files.SELF_PROTECTED)
        self.assertTrue(files._self_protected(config.REPO_ROOT / "jarvis/v2/checks.py"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)):
            paths = files._protected_state_paths()
            self.assertIn(Path(tmp) / "inbox" / "checks", paths)
            target = Path(tmp) / "inbox" / "checks" / "c0123456789ab.json"
            self.assertTrue(files._self_protected(target), "the inbox tree covers checks/")

    def test_agents_cannot_write_the_checks_folder(self):
        from jarvis.v2 import permissions
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)):
            d = Path(tmp)
            (d / "inbox" / "checks").mkdir(parents=True)
            for command in (f"cp /tmp/forged.json {d}/inbox/checks/c0123456789ab.json",
                            f"echo x > {d}/inbox/checks/c0123456789ab.json",
                            f"rm {d}/inbox/checks/*",
                            f"cp x {d}/*/checks/c0123456789ab.json",
                            f"mv {d}/inbox/checks /tmp/x"):
                with self.subTest(command=command):
                    self.assertIsNotNone(permissions._command_writes_protected_state(command), command)
                    self.assertIsNotNone(permissions.denied_command(command), command)


# --- who may post -----------------------------------------------------------------------------


class Binding(Base):
    def test_claude_and_codex_desk_turns_post_a_check(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                thread = self.chat(provider)
                self.turn(thread)
                cid = self.check_posted(self.post_check(thread, prs=[301]))
                record = self.cstore.get(cid)
                self.assertEqual((record["thread_id"], record["project_id"], record["prs"], record["results"],
                                  record["imported_from"], record["deployed_sha"]),
                                 (thread.id, self.project.id, [301], [], None, None))
                self.assertEqual(record["steps"][0], {"do": "Press Quick review.", "expect": "A spinner shows."})
                self.finish(thread)

    def test_off_desk_and_between_turns_are_refused(self):
        for provider in (CLAUDE, CODEX):
            for label, message in (("discord", UserMessage("x", via="discord")),
                                   ("schedule", UserMessage("x", origin="system")),
                                   ("a decision's answer", UserMessage("x", via="inbox"))):
                with self.subTest(provider=provider.value, case=label):
                    thread = self.chat(provider)
                    self.turn(thread, message)
                    self.refused_call(self.post_check(thread))
                    self.finish(thread)
            thread = self.chat(provider)
            self.turn(thread)
            self.finish(thread)
            self.refused_call(self.post_check(thread))
        self.no_checks()

    def test_a_fast_thread_is_refused_by_the_service(self):
        fast = self.chat(FAST)
        out = self.service_check(fast)
        self.assertTrue(out.startswith("Error:"), out)
        self.no_checks()


# --- caps and fields --------------------------------------------------------------------------


class Caps(Base):
    def setUp(self):
        super().setUp()
        self.thread = self.chat(CLAUDE)
        self.turn(self.thread)
        self.addCleanup(self.finish, self.thread)

    def test_five_open_per_chat(self):
        ids = []
        for i in range(5):
            out = self.service_check(self.thread, title=f"t{i}")
            self.assertTrue(out.startswith("Posted check"), out)
            ids.append(CHK_ID.search(out).group(0))
        out = self.service_check(self.thread, title="t5")
        self.assertTrue(out.startswith("Error:") and "5 checks waiting" in out, out)
        # A done check frees its slot (the owner marked every step).
        for step in range(2):
            self.cstore.mark(ids[0], step, "pass", "", sha=None)
        self.assertTrue(self.service_check(self.thread, title="t6").startswith("Posted check"))
        self.assertEqual(len(self.cstore.all()), 6)
        self.assertEqual(decisions.DecisionStore(self.cstore.root).all(), [], "a decision is not a check")

    def test_field_caps_refuse_never_cut(self):
        at_cap = dict(title="t" * 200, steps=[{"do": "d" * 1000, "expect": "e" * 1000}] * 30, prs=list(range(1, 11)))
        out = self.service_check(self.thread, **at_cap)
        self.assertTrue(out.startswith("Posted check"), out)
        record = self.cstore.get(CHK_ID.search(out).group(0))
        self.assertEqual((len(record["title"]), len(record["steps"]), len(record["steps"][0]["do"]),
                          len(record["steps"][0]["expect"])), (200, 30, 1000, 1000))
        for label, over in (("title", {"title": "t" * 201}),
                            ("do", {"steps": [{"do": "d" * 1001, "expect": "e"}]}),
                            ("expect", {"steps": [{"do": "d", "expect": "e" * 1001}]}),
                            ("steps", {"steps": [{"do": "d", "expect": "e"}] * 31}),
                            ("prs", {"prs": list(range(1, 12))})):
            with self.subTest(over=label):
                out = self.service_check(self.thread, **over)
                self.assertTrue(out.startswith("Error:"), out)
        self.assertEqual(len(self.cstore.all()), 1, "nothing over a cap was stored, cut or not")

    def test_malformed_and_mixed_arguments_are_refused(self):
        bad = {"no title": {"title": None}, "empty title": {"title": "  "}, "title not text": {"title": 5},
               "no steps": {"steps": None}, "zero steps": {"steps": []}, "steps text": {"steps": "do it"},
               "step not object": {"steps": ["do it"]}, "step extra key": {"steps": [{"do": "a", "expect": "b",
                                                                                     "run": "rm -rf /"}]},
               "step no expect": {"steps": [{"do": "a"}]}, "step empty do": {"steps": [{"do": " ", "expect": "b"}]},
               "step empty expect": {"steps": [{"do": "a", "expect": ""}]},
               "pr text": {"prs": ["301"]}, "pr zero": {"prs": [0]}, "pr bool": {"prs": [True]},
               "pr twice": {"prs": [3, 3]}, "prs not list": {"prs": 301},
               "a decision's question": {"question": "q?"}, "a decision's options": {"options": ["a", "b"]},
               "a decision's why": {"why": "w"}, "a decision's default": {"default": 0},
               "cleaned to nothing": {"title": "​⁣"}}
        for label, over in bad.items():
            with self.subTest(case=label):
                out = self.service_check(self.thread, **over)
                self.assertTrue(out.startswith("Error:"), out)
        self.no_checks()
        for label, body in (("extra", dict(check_args(), action="run_command")),
                            ("missing steps", {k: v for k, v in check_args().items() if k != "steps"})):
            with self.subTest(bridge=label):
                self.refused_call(self.mcp(self.thread, "owner_post", body))
        self.no_checks()


class Scrub(Base):
    def test_every_field_is_scrubbed(self):
        from jarvis.tools import secrets
        planted = "planted-owner-credential-7c6b5a4d"
        thread = self.chat(CLAUDE)
        self.turn(thread)
        with patch.object(secrets, "secret_values", return_value=[planted]):
            cid = self.check_posted(self.post_check(
                thread, title=f"Check {CR.GH}",
                steps=[{"do": f"Run with {planted}", "expect": f"It prints {CR.AKIA}"},
                       {"do": f"Then {CR.SKP}", "expect": "Done."}]))
        raw = (self.cstore.dir / f"{cid}.json").read_text()
        rows = json.dumps(self.rows())
        for token in (CR.GH, CR.SKP, CR.AKIA, planted):
            with self.subTest(token=token[:10]):
                self.assertNotIn(token, raw)
                self.assertNotIn(token, rows)
        self.assertIn("Run with ", raw)
        self.finish(thread)

    def test_a_scrub_that_fails_refuses_the_post(self):
        from jarvis.v2 import redact
        thread = self.chat(CLAUDE)
        self.turn(thread)
        with patch.object(redact, "scrub_text", side_effect=RuntimeError("no")):
            text = self.refused_call(self.post_check(thread))
        self.assertIn("could not be checked for credentials", text)
        self.no_checks()
        self.finish(thread)


# --- the store -------------------------------------------------------------------------------


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = OP.Clock()
        self.store = checks.CheckStore(self.root, clock=self.clock)

    def create(self, thread_id="ab12cd34", **over):
        a = dict(title="T", prs=[], steps=[{"do": "d", "expect": "e"}], imported_from=None)
        a.update(over)
        return self.store.create(thread_id=thread_id, project_id="p1", task_id=None, created_by="Claude · chat",
                                 **a)

    def test_modes_whatever_the_umask(self):
        old = os.umask(0)
        try:
            record = self.create()
        finally:
            os.umask(old)
        self.assertEqual(OP.mode(self.root / "inbox"), 0o700)
        self.assertEqual(OP.mode(self.store.dir), 0o700)
        self.assertEqual(OP.mode(self.store.dir / f"{record['id']}.json"), 0o600)
        self.assertEqual([p.name for p in self.store.dir.iterdir()], [f"{record['id']}.json"], "no temp left")

    def test_a_symlinked_folder_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "inbox").mkdir(mode=0o700)
        os.symlink(elsewhere, self.root / "inbox" / "checks")
        with self.assertRaises(OSError):
            self.create()
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing written through the link")
        self.assertEqual(self.store.all(), [])

    def test_a_symlinked_or_fifo_file_is_never_read(self):
        record = self.create()
        target = self.store.dir / f"{record['id']}.json"
        real = self.root / "real.json"
        shutil.move(target, real)
        os.symlink(real, target)
        self.assertIsNone(self.store.get(record["id"]))
        os.unlink(target)
        os.mkfifo(target)
        self.assertEqual(self.store.all(), [], "a FIFO never blocks and is never a check")

    def test_malformed_files_are_never_checks(self):
        good = self.create()
        path = self.store.dir / f"{good['id']}.json"
        base = json.loads(path.read_text())
        for label, change in (("kind", {"kind": "decision"}), ("extra", {"x": 1}),
                              ("verdict", {"results": [{"step": 0, "verdict": "ok", "note": "", "at": base["created_at"]}]}),
                              ("step range", {"results": [{"step": 3, "verdict": "pass", "note": "", "at": base["created_at"]}]}),
                              ("no steps", {"steps": []}), ("bad sha", {"deployed_sha": "zz"}),
                              ("both origins", {"imported_from": "/x.md"}),
                              ("no origin", {"thread_id": None})):
            with self.subTest(case=label):
                path.write_text(json.dumps(dict(base, **change)))
                self.assertEqual(self.store.all(), [])
                with self.assertRaises(LookupError):
                    self.store.mark(good["id"], 0, "pass", "", sha=None)
        path.write_text(json.dumps(base))
        self.assertEqual(len(self.store.all()), 1)

    def test_mark_replaces_and_done(self):
        record = self.create(steps=[{"do": "a", "expect": "b"}, {"do": "c", "expect": ""}],
                             thread_id=None, imported_from="/p/docs/plans/x.md")
        self.store.mark(record["id"], 1, "fail", "broke", sha=SHA["f"])
        self.store.mark(record["id"], 1, "pass", "fixed", sha=SHA["f"])
        got = self.store.get(record["id"])
        self.assertEqual([(r["step"], r["verdict"], r["note"]) for r in got["results"]], [(1, "pass", "fixed")])
        self.assertFalse(checks.is_done(got))
        self.assertEqual(got["deployed_sha"], SHA["f"])
        got = self.store.mark(record["id"], 0, "skip", "", sha=None)
        self.assertTrue(checks.is_done(got))
        with self.assertRaises(IndexError):
            self.store.mark(record["id"], 2, "pass", "", sha=None)
        with self.assertRaises(ValueError):
            self.store.mark(record["id"], 0, "maybe", "", sha=None)
        # An id not of the store's shape never names a file, even beside one.
        (self.store.dir.parent / f"{record['id']}.json").write_text(json.dumps(self.store.get(record["id"])))
        for bad in (f"../{record['id']}", f"{record['id']}/", record["id"].upper()):
            with self.subTest(bad=bad), self.assertRaises(LookupError):
                self.store.mark(bad, 0, "pass", "", sha=None)

    def test_the_store_is_bounded_dropping_done_first(self):
        with patch.object(checks, "KEEP", 3):
            first = self.create(thread_id=None, imported_from="/p/docs/plans/a.md")
            self.store.mark(first["id"], 0, "pass", "", sha=None)
            self.create(thread_id=None, imported_from="/p/docs/plans/b.md")
            self.create(thread_id=None, imported_from="/p/docs/plans/c.md")
            self.create(thread_id=None, imported_from="/p/docs/plans/d.md")
            self.assertIsNone(self.store.get(first["id"]), "the done one went first")
            with self.assertRaises(decisions.CapReached):
                self.create(thread_id=None, imported_from="/p/docs/plans/e.md")


# --- the deployed test ------------------------------------------------------------------------


class Deploy(unittest.TestCase):
    def test_no_prs_needs_no_test(self):
        status = checks.Deployment().status([])
        self.assertEqual(status["state"], checks.NO_PRS)
        self.assertTrue(checks.Deployment.markable(status))

    def test_unknown_is_never_deployed(self):
        cases = {
            "unattached": checks.Deployment(),
            "no code head": checks.Deployment(git=FakeGit({5: SHA["a"]}, {(SHA["a"], SHA["f"])}), code_head=None),
            "git cannot say": checks.Deployment(git=FakeGit(fail=True), code_head=SHA["f"]),
            "no merge commit (squash, not fetched)": checks.Deployment(git=FakeGit({}), code_head=SHA["f"]),
            "hud change, dev hud": checks.Deployment(git=FakeGit({5: SHA["a"]}, {(SHA["a"], SHA["f"])}, {SHA["a"]}),
                                                     code_head=SHA["f"], hud_head=lambda: None),
            "git raises": checks.Deployment(git=lambda *a: 1 / 0, code_head=SHA["f"]),
        }
        for label, deploy in cases.items():
            with self.subTest(case=label):
                status = deploy.status([5])
                self.assertEqual(status["state"], checks.UNCONFIRMED, status)
                self.assertFalse(checks.Deployment.markable(status))
                self.assertIn("Not confirmed deployed", status["text"])

    def test_merged_but_not_in_the_running_daemon(self):
        deploy = checks.Deployment(git=FakeGit({5: SHA["a"]}, set()), code_head=SHA["f"])
        status = deploy.status([5])
        self.assertEqual((status["state"], status["merged"]), (checks.NOT_DEPLOYED, True))
        self.assertFalse(checks.Deployment.markable(status))

    def test_a_python_only_pr_in_the_daemon_is_deployed(self):
        status = deployed([5]).status([5])
        self.assertEqual((status["state"], status["sha"]), (checks.DEPLOYED, SHA["f"]))

    def test_a_hud_change_needs_the_installed_hud(self):
        git = FakeGit({5: SHA["a"]}, {(SHA["a"], SHA["f"])}, {SHA["a"]})
        deploy = checks.Deployment(git=git, code_head=SHA["f"], hud_head=lambda: SHA["e"])
        self.assertEqual(deploy.status([5])["state"], checks.NOT_DEPLOYED, "the HUD build predates it")
        git.ancestors.add((SHA["a"], SHA["e"]))
        deploy._cache.clear()
        self.assertEqual(deploy.status([5])["state"], checks.DEPLOYED)

    def test_every_pr_must_be_in(self):
        git = FakeGit({5: SHA["a"], 6: SHA["b"]}, {(SHA["a"], SHA["f"])})
        status = checks.Deployment(git=git, code_head=SHA["f"]).status([5, 6])
        self.assertEqual(status["state"], checks.NOT_DEPLOYED)
        self.assertIn("#6", status["text"])
        self.assertNotIn("#5", status["text"])

    def test_the_merge_grep_is_exact_and_local(self):
        git = FakeGit({31: SHA["a"]}, {(SHA["a"], SHA["f"])})
        checks.Deployment(git=git, code_head=SHA["f"]).merge_commit(31)
        (call,) = git.calls
        self.assertIn("--grep=Merge pull request #31 from ", call)
        self.assertIn("--fixed-strings", call)
        self.assertFalse(any(a in ("fetch", "pull", "ls-remote") for a in call))

    def test_results_are_cached_briefly(self):
        now = [0.0]
        git = FakeGit({5: SHA["a"]}, {(SHA["a"], SHA["f"])})
        deploy = checks.Deployment(git=git, code_head=SHA["f"], clock=lambda: now[0])
        deploy.status([5])
        calls = len(git.calls)
        deploy.status([5])
        self.assertEqual(len(git.calls), calls)
        now[0] += 61
        deploy.status([5])
        self.assertGreater(len(git.calls), calls)


# --- the owner's marks -----------------------------------------------------------------------


class Marks(Base):
    def mark(self, cid, step, body, status=200, **kw):
        return self.call("POST", f"/inbox/checks/{cid}/steps/{step}", body, status, **kw)

    def test_only_the_owner_marks(self):
        thread, cid = self.desk_check()
        api, hud = self.d.port, self.d.face_port
        for kw in ({"owner": False}, {"owner": False, "origin": f"http://127.0.0.1:{api}"},
                   {"owner": False, "port": hud}, {"owner": False, "port": hud, "origin": "null"}):
            with self.subTest(kw=kw):
                self.mark(cid, 0, {"verdict": "pass"}, 403, **kw)
                self.call("POST", "/inbox/checks/import", {"path": str(RUNBOOK)}, 403, **kw)
        self.assertEqual(self.cstore.get(cid)["results"], [], "refused means unmarked")

    def test_marks_then_done(self):
        seen = []
        self.d.bus.observe(lambda r: seen.append(r) if r.get("kind") in ("inbox_changed", "check_posted",
                                                                         "check_marked") else None)
        thread, cid = self.desk_check(title=f"T {SENTINEL}",
                                      steps=[{"do": f"D {SENTINEL}", "expect": f"E {SENTINEL}"},
                                             {"do": "D2", "expect": "E2"}])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertEqual((row["rank"], row["kind"]), (7, "check"))
        got = self.mark(cid, 0, {"verdict": "fail", "note": f"  saw {CR.GH} instead  "})
        self.assertEqual((got["ok"], got["done"]), (True, False))
        result = self.cstore.get(cid)["results"][0]
        self.assertEqual((result["step"], result["verdict"]), (0, "fail"))
        self.assertNotIn(CR.GH, result["note"], "the note is scrubbed")
        self.assertTrue(result["note"].startswith("saw "))
        self.assertTrue(self.mark(cid, 1, {"verdict": "skip"})["done"])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertEqual(row["rank"], 8, "done: the receipt band")
        self.assertIn("0 passed, 1 failed, 1 skipped", row["title"])
        self.assertTrue(row["detail"]["done"])
        changes = [r for r in seen if r["kind"] == "inbox_changed" and f"chk:{cid}" in r["data"]["ids"]]
        self.assertEqual([c["data"]["reason"] for c in changes], ["check_posted", "check_marked", "check_marked"])
        for record in seen:
            self.assertNotIn(SENTINEL, json.dumps(record))
            self.assertNotIn(CR.GH, json.dumps(record))

    def test_malformed_and_unknown_are_refused(self):
        thread, cid = self.desk_check()
        for body in ({}, {"verdict": "ok"}, {"verdict": "pass", "x": 1}, {"verdict": "pass", "note": 5},
                     {"verdict": "pass", "note": "n" * 1001}, [1], {"verdict": True}):
            with self.subTest(body=body):
                self.mark(cid, 0, body, 400)
        for step in ("-1", "01", "x", "1000", "0x1"):
            with self.subTest(step=step):
                self.mark(cid, step, {"verdict": "pass"}, 400)
        self.mark(cid, 2, {"verdict": "pass"}, 404)
        self.mark("c0000000000ee", 0, {"verdict": "pass"}, 404)
        self.mark("nope", 0, {"verdict": "pass"}, 400)
        self.call("POST", f"/inbox/checks/{cid}/steps/0?x=1", {"verdict": "pass"}, 400)
        self.call("POST", f"/inbox/chk:{cid}/answer", {"verdict": "pass"}, 400)
        self.assertEqual(self.cstore.get(cid)["results"], [])

    def test_an_unconfirmed_build_refuses_the_mark(self):
        thread, cid = self.desk_check(prs=[301])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertEqual(row["detail"]["deploy"]["markable"], False)
        self.assertIn("Not confirmed deployed", row["blocks"])
        self.mark(cid, 0, {"verdict": "pass"}, 409)
        self.assertEqual(self.cstore.get(cid)["results"], [], "not deployed: nothing recorded")
        # Merged but not in the running daemon: the same.
        self.d.inbox.deploy = checks.Deployment(git=FakeGit({301: SHA["a"]}), code_head=SHA["f"])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertIn("Not deployed yet: #301", row["blocks"])
        self.mark(cid, 0, {"verdict": "pass"}, 409)
        # Deployed: marked, and the receipt names the build.
        self.d.inbox.deploy = deployed([301])
        self.mark(cid, 0, {"verdict": "pass"})
        self.assertEqual(self.cstore.get(cid)["deployed_sha"], SHA["f"])


# --- no result path but the owner's -----------------------------------------------------------


class NoResultPath(Base):
    NEEDLES = ("mark_check_step", "CheckStore", ".mark(", "/steps/", "checks/import", "import_checks",
               "check_marked", "deployed_sha")

    def test_only_the_inbox_names_the_mark(self):
        """Grep: across all of `jarvis/` only the inbox (its owner-only route)
        and the store itself name the mark, its route or its event."""
        allowed = {"jarvis/v2/inbox.py", "jarvis/v2/checks.py"}
        found = {}
        for path in sorted((ROOT / "jarvis").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(errors="replace")
            for needle in ("mark_check_step", "CheckStore", "/steps/", "checks/import", "import_checks",
                           "check_marked"):
                if needle in text and rel not in allowed:
                    found.setdefault(rel, []).append(needle)
        # `daemon.py` builds the store through the inbox only; `files.py` names the folder to protect it.
        self.assertEqual(found, {}, found)

    def test_the_post_side_and_every_tool_name_nothing_of_it(self):
        from jarvis import tools
        from jarvis.v2 import bridge, chat_relay, mcp, mcp_catalog, session_tools
        from jarvis.v2.providers import fastpath
        import jarvis.v2.tools as v2tools
        OP.tool_module()
        sources = [inspect.getsource(OP.tool_module()), inspect.getsource(decisions.OwnerPosts),
                   inspect.getsource(decisions.installed)]
        for module in (v2tools, mcp, mcp_catalog, session_tools, bridge, chat_relay, fastpath):
            sources.append(inspect.getsource(module))
        for name, entry in tools.REGISTRY.items():
            func = getattr(entry, "func", None)
            module = inspect.getmodule(func) if func is not None else None
            if module is not None and module.__name__ not in ("jarvis.v2.inbox", "jarvis.v2.checks"):
                sources.append(inspect.getsource(module))
        for text in sources:
            for needle in self.NEEDLES:
                self.assertNotIn(needle, text)

    def test_a_sweep_of_every_session_tool_never_marks(self):
        """Every tool a Claude or Codex owner chat holds, called with a check
        id and a verdict in every string argument, never reaches the mark."""
        from jarvis import tools
        from jarvis.v2 import session_tools
        thread, cid = self.desk_check()
        chat = self.chat(CLAUDE)
        self.turn(chat)
        names = sorted({t.name for t in session_tools.for_provider(CLAUDE)}
                       | {t.name for t in session_tools.for_provider(CODEX)})
        self.assertIn("owner_post", names)
        with patch.object(checks.CheckStore, "mark", side_effect=AssertionError("a tool marked a step")) as spy, \
                patch.object(inbox, "mark_check_step", side_effect=AssertionError("a tool reached the route")) as route:
            for name in names:
                schema = tools.REGISTRY[name].schema
                arguments = {}
                for key, prop in schema["properties"].items():
                    kind = prop.get("type")
                    arguments[key] = (f"/inbox/checks/{cid}/steps/0" if kind == "string" else
                                      [{"do": cid, "expect": "pass"}] if kind == "array" else
                                      {"verdict": "pass"} if kind == "object" else 0)
                with self.subTest(tool=name):
                    self.mcp(chat, name, arguments)
            self.assertEqual((spy.call_count, route.call_count), (0, 0))
        self.finish(chat)
        self.assertEqual(self.cstore.get(cid)["results"], [])

    def test_the_api_listener_has_no_mark(self):
        thread, cid = self.desk_check()
        status, _ = OP.M.http(self.d.port, "POST", f"/inbox/checks/{cid}/steps/0", body={"verdict": "pass"})
        self.assertEqual(status, 403)
        self.assertEqual(self.cstore.get(cid)["results"], [])


# --- rows ---------------------------------------------------------------------------------------


class Rows(Base):
    def test_title_and_blocks_are_code_built(self):
        thread, cid = self.desk_check(title=f"T {SENTINEL}", steps=[{"do": f"D {SENTINEL}", "expect": "E"}])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertEqual(row["title"], f"Live check {cid} from chat {thread.id}: 0 of 1 steps marked")
        self.assertEqual(row["blocks"], "nothing waits; your marks are the receipt")
        self.assertNotIn(SENTINEL, row["title"] + row["blocks"] + row["origin"])
        self.assertEqual(row["detail"]["title"], f"T {SENTINEL}")
        self.assertEqual(row["thread_id"], thread.id)

    def test_rank_7_merged_first_then_done_at_8(self):
        clock = OP.Clock(datetime.now(timezone.utc).isoformat())
        self.cstore.clock = clock
        _, old = self.desk_check(title="old, unmerged", prs=[11])
        clock.advance(minutes=1)
        _, new = self.desk_check(title="new, merged", prs=[12])
        clock.advance(minutes=1)
        _, plain = self.desk_check(title="no prs")
        self.d.inbox.deploy = checks.Deployment(git=FakeGit({12: SHA["a"]}), code_head=SHA["f"])
        order = [r["id"] for r in self.rows() if r["kind"] == "check"]
        self.assertEqual(order, [f"chk:{new}", f"chk:{old}", f"chk:{plain}"])
        self.assertTrue(all(r["rank"] == 7 for r in self.rows() if r["kind"] == "check"))
        for step in range(2):
            self.cstore.mark(plain, step, "pass", "", sha=None)
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{plain}"]
        self.assertEqual(row["rank"], 8)
        clock.advance(days=7, minutes=1)
        self.assertNotIn(f"chk:{plain}", [r["id"] for r in self.rows()], "done drops off after 7 days")

    def test_one_bad_file_never_takes_the_inbox_down(self):
        _, cid = self.desk_check()
        (self.cstore.dir / "c0000000000ff.json").write_text('{"id": "c0000000000ff"}')
        self.assertIn(f"chk:{cid}", [r["id"] for r in self.rows()])

    def test_the_phone_keeps_a_read_only_projection(self):
        from jarvis.v2 import remote_api
        _, cid = self.desk_check()
        (row,) = [r for r in inbox.rows(self.d) if r["id"] == f"chk:{cid}"]
        phone = remote_api.inbox_row(row)
        self.assertEqual(set(phone["detail"]), set(remote_api.DETAIL_FIELDS["check"]))
        self.assertLessEqual(set(remote_api.DETAIL_FIELDS["check"]), set(row["detail"]))


# --- the runbook import (§6) --------------------------------------------------------------------


class Import(Base):
    def setUp(self):
        super().setUp()
        self.plans = Path(self.project.root) / "docs" / "plans"
        self.plans.mkdir(parents=True)
        self.runbook = self.plans / RUNBOOK.name
        shutil.copyfile(RUNBOOK, self.runbook)

    def preview(self, path=None, status=200):
        return self.call("POST", "/inbox/checks/import", {"path": str(path or self.runbook)}, status)

    def test_the_runbook_yields_one_draft_per_heading_unsaved(self):
        got = self.preview()
        headings = [line[3:].strip() for line in RUNBOOK.read_text().splitlines() if line.startswith("## ")]
        self.assertEqual(len(got["drafts"]), len(headings))
        self.assertEqual(len(got["drafts"]), 10)
        self.assertEqual([len(d["steps"]) for d in got["drafts"]], [17, 9, 7, 0, 4, 8, 5, 5, 9, 4])
        first = got["drafts"][0]
        self.assertEqual(first["prs"], [223, 224])
        # Backticks go, as on every line of agent text (`approvals.clean_line`).
        self.assertEqual(first["steps"][0]["do"], "(Part A) Open a new Codex chat in ~/projects/Jarvis.")
        self.assertTrue(first["steps"][0]["expect"].startswith("it starts."))
        self.assertTrue(first["savable"])
        vex = got["drafts"][3]
        self.assertEqual((vex["savable"], vex["reason"]), (False, "it has no numbered steps"))
        self.no_checks()
        self.assertEqual(self.cstore.all(), [], "a preview saves nothing")

    def test_the_owners_confirm_saves_one_draft(self):
        got = self.preview()
        saved = self.call("POST", "/inbox/checks/import", {"path": str(self.runbook), "digest": got["digest"],
                                                            "save": 2})
        record = self.cstore.get(saved["check_id"])
        self.assertEqual((record["thread_id"], record["imported_from"], record["prs"], len(record["steps"])),
                         (None, str(self.runbook), [219], 7))
        self.assertTrue(record["created_by"].startswith("Imported by you from docs/plans/"))
        self.assertEqual(len(self.cstore.all()), 1, "only the confirmed draft")
        self.call("POST", "/inbox/checks/import", {"path": str(self.runbook), "digest": got["digest"], "save": 2}, 409)
        again = self.preview()
        self.assertEqual((again["drafts"][2]["savable"], again["drafts"][2]["reason"]), (False, "already imported"))
        self.call("POST", "/inbox/checks/import", {"path": str(self.runbook), "digest": got["digest"], "save": 3}, 400)
        (row,) = [r for r in self.rows() if r["kind"] == "check"]
        self.assertTrue(row["title"].endswith("from you (imported): 0 of 7 steps marked"), row["title"])

    def test_a_changed_file_needs_a_new_preview(self):
        got = self.preview()
        with open(self.runbook, "a") as f:
            f.write("\n## New\n\n1. Do it. Expect: done.\n")
        self.call("POST", "/inbox/checks/import", {"path": str(self.runbook), "digest": got["digest"], "save": 0}, 409)
        self.no_checks()
        for body, status in (({"path": str(self.runbook), "save": 0}, 409),
                             ({"path": str(self.runbook), "digest": "x"}, 400),
                             ({"path": str(self.runbook), "digest": got["digest"], "save": True}, 400),
                             ({"path": str(self.runbook), "digest": got["digest"], "save": 99}, 400),
                             ({"path": str(self.runbook), "x": 1}, 400), ({}, 400)):
            with self.subTest(body=body):
                self.call("POST", "/inbox/checks/import", body, status)

    def test_paths_outside_docs_plans_are_refused(self):
        root = Path(self.project.root)
        (root / "docs" / "plans" / "sub").mkdir()
        (root / "docs" / "plans" / "sub" / "x.md").write_text("## A\n1. x\n")
        (root / "notes.md").write_text("## A\n1. x\n")
        (root / "docs" / "plans" / "a.txt").write_text("## A\n1. x\n")
        (root / "docs" / "plans" / ".env.md").write_text("## A\n1. x\n")
        outside = Path(self.root) / "outside"
        outside.mkdir()
        (outside / "x.md").write_text("## A\n1. x\n")
        os.symlink(outside / "x.md", root / "docs" / "plans" / "link.md")
        os.mkfifo(root / "docs" / "plans" / "fifo.md")
        other = Path(self.root) / "other"
        (other / "docs").mkdir(parents=True)
        os.symlink(outside, other / "docs" / "plans")
        linked = self.stores.projects.create("Other", str(other))
        self.assertTrue(linked.id)
        for raw in ("docs/plans/" + RUNBOOK.name, f"{root}/docs/plans/../plans/{RUNBOOK.name}",
                    f"{root}/docs/./plans/{RUNBOOK.name}", f"{root}/docs/plans/sub/x.md", f"{root}/notes.md",
                    f"{root}/docs/plans/a.txt", f"{root}/docs/plans/.env.md", f"{outside}/x.md",
                    f"{root}/docs/plans/link.md", f"{root}/docs/plans/fifo.md", f"{other}/docs/plans/x.md",
                    f"{root}/docs/plans/missing.md", f"{root}/docs/plans/{RUNBOOK.name}‮",
                    f"{root}/docs/plans/x\n.md", "~/x.md", "", 5):
            with self.subTest(path=raw):
                self.call("POST", "/inbox/checks/import", {"path": raw}, 400)
        self.no_checks()

    def test_an_archived_projects_runbook_is_refused(self):
        import dataclasses
        self.stores.projects.save(dataclasses.replace(self.stores.projects.get(self.project.id),
                                                      archived=datetime.now(timezone.utc).isoformat()))
        self.preview(status=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
