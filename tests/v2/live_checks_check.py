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


def merge(n, sha):
    """A first-parent merge commit with GitHub's subject for PR `n`."""
    return (sha, f"Merge pull request #{n} from me/branch-{n}")


class FakeGit:
    """`git` for `checks.Deployment`: each head's first-parent merge commits
    as (sha, subject), and the merge commits that change hud/. Counts calls."""

    def __init__(self, lines=None, hud=(), fail=False):
        self.lines, self.hud, self.fail = {k: list(v) for k, v in (lines or {}).items()}, set(hud), fail
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.fail:
            return None
        if args[0] == "log":
            assert "--first-parent" in args and "--merges" in args and args[-1] == "--", args
            head = args[-2]
            return (0, "".join(f"{sha}\t{subject}\n" for sha, subject in self.lines.get(head, [])))
        if args[0] == "diff":
            return (1, "") if args[-3] in self.hud else (0, "")
        raise AssertionError(args)


def deployed(prs=(), *, hud=()):
    """A Deployment where every given PR is merged on the running line and the HUD's."""
    line = [merge(n, SHA["a"][:-len(str(n))] + str(n)) for n in prs]
    return checks.Deployment(git=FakeGit({SHA["f"]: line, SHA["e"]: line, "HEAD": line}, hud),
                             code_head=SHA["f"], hud_head=lambda: (SHA["e"], False))


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
        with patch.object(redact, "shapes", side_effect=RuntimeError("no")):
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


# --- size: every record the stores write reads back (review of PR #321, MED 1) ------------


EXPANDS = "\u0958"          # one character typed, two after NFC (and 6 bytes)


class Size(Base):
    def test_text_that_expands_when_cleaned_is_refused_at_post(self):
        """The reviewer's probe p2: 30 steps of 1000 U+0958 passed the caps
        on the raw text and wrote a file too large to read: never counted,
        never pruned. The caps now hold on what is stored."""
        thread = self.chat(CLAUDE)
        self.turn(thread)
        big = EXPANDS * 1000
        for _ in range(8):
            out = self.service_check(thread, steps=[{"do": big, "expect": big}] * 30)
            self.assertTrue(out.startswith("Error: once cleaned,"), out)
        out = self.service_check(thread, title=EXPANDS * 150)
        self.assertTrue(out.startswith("Error: once cleaned,"), out)
        self.no_checks()
        self.assertFalse(self.cstore.dir.exists() and any(self.cstore.dir.iterdir()), "nothing on disk")
        self.finish(thread)

    def test_what_is_posted_is_what_is_counted(self):
        thread = self.chat(CLAUDE)
        self.turn(thread)
        half = EXPANDS * 500               # 1000 characters once cleaned: at the cap
        for i in range(5):
            out = self.service_check(thread, title=f"t{i}", steps=[{"do": half, "expect": "e"}] * 3)
            self.assertTrue(out.startswith("Posted check"), out)
        self.assertEqual(len(self.cstore.all()), 5)
        out = self.service_check(thread, title="t5")
        self.assertIn("5 checks waiting", out)
        self.assertEqual(len(os.listdir(self.cstore.dir)), 5, "every file written is one the store reads")
        self.finish(thread)


class SizeStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_reviewers_probe_p1(self):
        """Cleaned text past the field caps is never written (valid() holds the caps)."""
        store = checks.CheckStore(self.root)
        cleaned = decisions.display_text(decisions._clean_field(EXPANDS * 1000, True), lines=True)
        self.assertEqual(len(cleaned), 2000)
        with self.assertRaises(ValueError):
            store.create(thread_id="ab12cd34", project_id=None, task_id=None, created_by="x", title="T", prs=[],
                         steps=[{"do": cleaned, "expect": cleaned}] * 30)
        self.assertEqual(os.listdir(store.dir), [])

    def test_a_check_past_its_byte_budget_is_refused_and_the_worst_marks_still_read_back(self):
        store = checks.CheckStore(self.root)
        wide = "\u20ac" * 1000                 # 3 bytes each, within every character cap
        with self.assertRaises(decisions.CapReached):
            store.create(thread_id="ab12cd34", project_id=None, task_id=None, created_by="x", title="T", prs=[],
                         steps=[{"do": wide, "expect": wide}] * 30)
        self.assertEqual(os.listdir(store.dir), [])
        # The largest check that fits, then every step marked with the widest note: it still reads back.
        steps = [{"do": wide, "expect": "\u20ac" * 400}] * 30
        record = store.create(thread_id="ab12cd34", project_id=None, task_id=None, created_by="x" * 200,
                              title="\U0001d538" * 200, prs=list(range(1, 11)), steps=steps)
        for step in range(30):
            store.mark(record["id"], step, "fail", "\U0001d538" * 1000, sha=SHA["f"])
        got = store.get(record["id"])
        self.assertIsNotNone(got, "every file this store writes reads back")
        self.assertTrue(checks.is_done(got))
        self.assertLess(os.path.getsize(store.dir / f"{record['id']}.json"), checks._FILE_MAX // 2)

    def test_the_store_never_writes_what_it_cannot_read(self):
        for store in (checks.CheckStore(self.root), decisions.DecisionStore(self.root)):
            with self.subTest(store=type(store).__name__):
                self.assertLessEqual(store._WRITE_MAX * 2, store._MAX)

    def test_a_worst_case_decision_reads_back(self):
        """The same property for decisions: the widest fields at their caps
        (4-byte characters, and text that grows under NFC) are far under the
        decision store's limits."""
        store = decisions.DecisionStore(self.root)
        wide = "\U0001d538"
        record = store.create(thread_id="ab12cd34", project_id=None, task_id=None, origin="o" * 160,
                              question=wide * 1000, options=[f"{i}" + wide * 239 for i in range(6)],
                              default=0, why=wide * 2000)
        self.assertIsNotNone(store.get(record["id"]))
        size = os.path.getsize(store.dir / f"{record['id']}.json")
        self.assertLess(size, store._WRITE_MAX, size)
        store.settle(record["id"], choice=None, text=wide * decisions.ANSWER_MAX)
        self.assertIsNotNone(store.get(record["id"]))


class SizeDecisions(Base):
    def test_a_decision_that_grows_when_cleaned_is_posted_and_reads_back(self):
        thread = self.chat(CLAUDE)
        self.turn(thread)
        out = self.service_post_decision(thread)
        did = OP.DEC_ID.search(out).group(0)
        record = self.store.get(did)
        self.assertIsNotNone(record, "it reads back")
        self.assertEqual(len(record["question"]), 1000, "NFC doubled it")
        self.finish(thread)

    def service_post_decision(self, thread):
        return self.d.owner_posts.post(self.caller(thread), "decision", EXPANDS * 500,
                                       [EXPANDS * 120, "b" * 120], 0, EXPANDS * 1000)


# --- the deployed test ------------------------------------------------------------------------


class Deploy(unittest.TestCase):
    F, E = SHA["f"], SHA["e"]

    def test_no_prs_needs_no_test(self):
        status = checks.Deployment().status([])
        self.assertEqual(status["state"], checks.NO_PRS)
        self.assertTrue(checks.Deployment.markable(status))

    def test_unknown_is_never_deployed(self):
        F, E, A = self.F, self.E, SHA["a"]
        hud_line = {F: [merge(5, A)], E: [merge(5, A)]}
        cases = {
            "unattached": checks.Deployment(),
            "no code head": checks.Deployment(git=FakeGit({F: [merge(5, A)]}), code_head=None),
            "git cannot say": checks.Deployment(git=FakeGit(fail=True), code_head=F),
            "no merge commit (squash, not fetched)": checks.Deployment(git=FakeGit({}), code_head=F),
            "two merge commits for one PR": checks.Deployment(git=FakeGit({F: [merge(5, A), merge(5, SHA["b"])]}),
                                                              code_head=F),
            "hud change, dev hud": checks.Deployment(git=FakeGit(hud_line, {A}), code_head=F,
                                                     hud_head=lambda: (None, None)),
            "hud change, hud built dirty": checks.Deployment(git=FakeGit(hud_line, {A}), code_head=F,
                                                             hud_head=lambda: (E, True)),
            "hud change, dirty unknown": checks.Deployment(git=FakeGit(hud_line, {A}), code_head=F,
                                                           hud_head=lambda: (E, None)),
            "git raises": checks.Deployment(git=lambda *a: 1 / 0, code_head=F),
            "no budget left": checks.Deployment(git=FakeGit({F: [merge(5, A)]}), code_head=F),
        }
        for label, deploy in cases.items():
            with self.subTest(case=label):
                status = deploy.status([5], [0] if label == "no budget left" else None)
                self.assertEqual(status["state"], checks.UNCONFIRMED, status)
                self.assertFalse(checks.Deployment.markable(status))
                self.assertIn("Not confirmed deployed", status["text"])
        # A clean HUD build is the control.
        clean = checks.Deployment(git=FakeGit(hud_line, {A}), code_head=F, hud_head=lambda: (E, False))
        self.assertEqual(clean.status([5])["state"], checks.DEPLOYED)

    def test_merged_but_not_in_the_running_daemon(self):
        deploy = checks.Deployment(git=FakeGit({self.F: [], "HEAD": [merge(5, SHA["a"])]}), code_head=self.F)
        status = deploy.status([5])
        self.assertEqual((status["state"], status["merged"]), (checks.NOT_DEPLOYED, True))
        self.assertFalse(checks.Deployment.markable(status))

    def test_a_python_only_pr_in_the_daemon_is_deployed(self):
        status = deployed([5]).status([5])
        self.assertEqual((status["state"], status["sha"]), (checks.DEPLOYED, SHA["f"]))

    def test_a_hud_change_needs_the_installed_hud(self):
        git = FakeGit({self.F: [merge(5, SHA["a"])], self.E: []}, {SHA["a"]})
        deploy = checks.Deployment(git=git, code_head=self.F, hud_head=lambda: (self.E, False))
        self.assertEqual(deploy.status([5])["state"], checks.NOT_DEPLOYED, "the HUD build predates it")
        git.lines[self.E] = [merge(5, SHA["a"])]
        deploy._maps.clear()
        self.assertEqual(deploy.status([5])["state"], checks.DEPLOYED)

    def test_every_pr_must_be_in(self):
        git = FakeGit({self.F: [merge(5, SHA["a"])], "HEAD": [merge(5, SHA["a"]), merge(6, SHA["b"])]})
        status = checks.Deployment(git=git, code_head=self.F).status([5, 6])
        self.assertEqual(status["state"], checks.NOT_DEPLOYED)
        self.assertIn("#6", status["text"])
        self.assertNotIn("#5", status["text"])

    def test_only_an_exact_subject_counts(self):
        """Review of PR #321, MED 2: a revert, a subject that only mentions the
        PR, a longer number and a branch-merge subject never count."""
        for subject in ('Revert "Merge pull request #5 from me/x"', "Merge pull request #55 from me/x",
                        "Merge pull request #5", "merge pull request #5 from me/x",
                        "Merge branch 'main' into feature", "Fix #5 from the review",
                        "Merge pull request #5 from me/x and more",
                        # Delta review: only git's line ending is taken off.
                        "Merge pull request #5 from me/x\r", "Merge pull request #5 from me/x ",
                        " Merge pull request #5 from me/x", "Merge pull request #5 from me/x\t"):
            with self.subTest(subject=subject):
                deploy = checks.Deployment(git=FakeGit({self.F: [(SHA["a"], subject)], "HEAD": [(SHA["a"], subject)]}),
                                           code_head=self.F)
                self.assertEqual(deploy.status([5])["state"], checks.UNCONFIRMED)

    def test_real_git_reads_the_first_parent_subject_only(self):
        """The reviewer's probe p3 on a real, local repository: a PR body
        naming #999, a merge carried inside a branch, a revert's subject. No
        network: a temporary repository and the hardened `gitops._git`."""
        import subprocess
        from jarvis import gitops
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a",
                       GIT_COMMITTER_EMAIL="a@b", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

            def g(*a):
                return subprocess.run(["git", *a], cwd=r, env=env, check=True, capture_output=True, text=True).stdout
            g("init", "-q", "-b", "main")
            g("commit", "-q", "--allow-empty", "-m", "root")
            # A merge commit inside a feature branch (#777), then the branch merged as #400 whose body names #999.
            g("checkout", "-q", "-b", "inner")
            g("commit", "-q", "--allow-empty", "-m", "inner work")
            g("checkout", "-q", "-b", "feat", "main")
            g("commit", "-q", "--allow-empty", "-m", "work")
            g("merge", "-q", "--no-ff", "inner", "-m", "Merge pull request #777 from me/inner")
            g("checkout", "-q", "main")
            g("merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #400 from me/feat\n\n"
              "Merge pull request #999 from x/y")
            g("commit", "-q", "--allow-empty", "-m", 'Revert "Merge pull request #888 from me/z"')
            head = g("rev-parse", "HEAD").strip()

            def git(*args):
                code, out, _ = gitops._git(r, list(args), timeout=30)
                return code, out
            deploy = checks.Deployment(git=git, code_head=head, hud_head=lambda: (None, None))
            self.assertEqual(deploy.status([400])["state"], checks.DEPLOYED, "the control: a real first-parent merge")
            for n in (999, 777, 888, 9, 40):
                with self.subTest(pr=n):
                    self.assertEqual(deploy.status([n])["state"], checks.UNCONFIRMED)

    def test_git_runs_once_per_line_and_reads_are_bounded(self):
        """Review of PR #321, LOW 2: the running line's map is kept by SHA, so
        later reads run no git; a read with no budget left runs none and keeps
        nothing."""
        now = [0.0]
        git = FakeGit({self.F: [merge(5, SHA["a"]), merge(6, SHA["b"])]})
        deploy = checks.Deployment(git=git, code_head=self.F, clock=lambda: now[0])
        self.assertEqual(deploy.status([5], [0])["state"], checks.UNCONFIRMED)
        self.assertEqual(git.calls, [], "no budget: no git")
        deploy.status([5], [8])
        deploy.status([6], [8])
        deploy.status([5, 6], [8])
        calls = len(git.calls)
        self.assertEqual(sum(1 for c in git.calls if c[0] == "log"), 1, "one log for the running line")
        now[0] += 3600
        deploy.status([5, 6], [8])
        self.assertEqual(len(git.calls), calls, "kept by SHA, whatever the time")


class DeployDelta(unittest.TestCase):
    """Delta review of PR #321: failures are kept, git runs are short, and
    one inbox read's budget is spent round-robin."""
    F, E = SHA["f"], SHA["e"]

    def test_a_failing_git_call_is_kept_and_stops_spending_the_budget(self):
        now = [0.0]
        git = FakeGit(fail=True)
        deploy = checks.Deployment(git=git, code_head=self.F, clock=lambda: now[0])
        for _ in range(5):
            self.assertEqual(deploy.status([5], [8])["state"], checks.UNCONFIRMED)
        self.assertEqual(len(git.calls), 1, "a failed line read is kept for the cache period")
        now[0] += 61
        deploy.status([5], [8])
        self.assertEqual(len(git.calls), 2, "and tried again after it")
        # A failing diff (whether a merge touches hud/) is kept the same way.
        class DiffFails(FakeGit):
            def __call__(self, *args):
                if args[0] == "diff":
                    self.calls.append(args)
                    return None
                return super().__call__(*args)
        git = DiffFails({self.F: [merge(5, SHA["a"])]})
        deploy = checks.Deployment(git=git, code_head=self.F, clock=lambda: now[0])
        for _ in range(5):
            self.assertEqual(deploy.status([5], [8])["state"], checks.UNCONFIRMED)
        self.assertEqual(sum(1 for c in git.calls if c[0] == "diff"), 1)

    def test_attached_git_runs_with_a_short_timeout_throughout(self):
        from jarvis import gitops
        from jarvis.v2 import hud_install
        seen = []
        with patch.object(hud_install, "git_head", return_value=self.F), \
                patch.object(hud_install, "_git", side_effect=lambda *a, **kw: seen.append(kw) or (0, "")):
            deploy = checks.Deployment()
            deploy.attach_git()
            deploy.status([5], [8])
        self.assertTrue(seen)
        self.assertTrue(all(kw.get("timeout") == checks.GIT_TIMEOUT_S for kw in seen), seen)
        self.assertLessEqual(checks.GIT_TIMEOUT_S, 5)
        # And `hud_install._git` hands it to every git process it starts,
        # the config checks before the command included.
        timeouts = []
        with patch.object(gitops, "_git", side_effect=lambda repo, args, timeout=None: timeouts.append(timeout)
                          or (0, "", "")):
            hud_install._git("log", "-1", timeout=checks.GIT_TIMEOUT_S)
        self.assertGreaterEqual(len(timeouts), 3, timeouts)
        self.assertEqual(set(timeouts), {checks.GIT_TIMEOUT_S})


# --- the owner's marks -----------------------------------------------------------------------


class Marks(Base):
    F, E = SHA["f"], SHA["e"]

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

    def test_the_mark_reads_the_hud_again(self):
        """Delta review of PR #321 (LOW 1): a HUD rolled back, or rebuilt from
        uncommitted hud/, inside the rows' 60-second window refuses the mark."""
        thread, cid = self.desk_check(prs=[301])
        A = SHA["a"]
        hud = {"head": (self.E, False)}
        git = FakeGit({self.F: [merge(301, A)], self.E: [merge(301, A)], SHA["d"]: []}, hud={A})
        now = [0.0]
        self.d.inbox.deploy = checks.Deployment(git=git, code_head=self.F, hud_head=lambda: hud["head"],
                                                clock=lambda: now[0])
        (row,) = [r for r in self.rows() if r["id"] == f"chk:{cid}"]
        self.assertTrue(row["detail"]["deploy"]["markable"], "the control: deployed")
        for label, head in (("rolled back", (SHA["d"], False)), ("rebuilt dirty", (self.E, True))):
            with self.subTest(case=label):
                hud["head"] = head
                now[0] += 1                     # well inside the 60 s the rows keep
                self.mark(cid, 0, {"verdict": "pass"}, 409)
                self.assertEqual(self.cstore.get(cid)["results"], [])
        hud["head"] = (self.E, False)
        self.mark(cid, 0, {"verdict": "pass"})

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
        self.d.inbox.deploy = checks.Deployment(git=FakeGit({SHA["f"]: [], "HEAD": [merge(301, SHA["a"])]}),
                                                code_head=SHA["f"])
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


# --- the answer path never reads deploy state (review of PR #321, LOW 2) ------------------


class AnswerPath(Base):
    def test_answering_never_runs_the_deployed_test(self):
        _, cid = self.desk_check(prs=[301])
        git = FakeGit({SHA["f"]: [merge(301, SHA["a"])]})
        self.d.inbox.deploy = checks.Deployment(git=git, code_head=SHA["f"])
        with patch.object(self.d.inbox.deploy, "status", side_effect=AssertionError("deploy read")) as spy:
            for row_id, body in (("q:ab12cd34:r1", {"text": "x"}), ("prop:k0000001", {"withdraw": True}),
                                 ("tq:k0000001:0", {"text": "x"}), ("appr:R1", {"decision": "allow"})):
                with self.subTest(row=row_id):
                    self.call("POST", f"/inbox/{row_id}/answer", body, 409)
            self.assertEqual(spy.call_count, 0)
        self.assertEqual(git.calls, [])

    def test_older_checks_never_starve_newer_ones(self):
        """Twelve checks, each needing its own git run; the older eleven's
        always fail. With the budget spent round-robin the newest is reached
        within two reads, whatever the order (delta review of PR #321)."""
        made = OP.Clock(datetime.now(timezone.utc).isoformat())
        self.cstore.clock = made           # oldest first: the newest is the last one read
        for i in range(12):
            made.advance(minutes=1)
            self.cstore.create(thread_id=None, project_id=None, task_id=None, created_by="you", title=f"i{i}",
                               prs=[100 + i], steps=[{"do": "a", "expect": ""}],
                               imported_from=f"/p/docs/plans/{i}.md")
        line = [merge(100 + i, (SHA["a"][:-3] + f"{i:03d}")) for i in range(12)]
        newest = line[-1][0]

        class OldFail(FakeGit):
            def __call__(self, *args):
                if args[0] == "diff" and args[-3] != newest:
                    self.calls.append(args)
                    return None
                return super().__call__(*args)
        now = [0.0]
        git = OldFail({SHA["f"]: line})
        self.d.inbox.deploy = checks.Deployment(git=git, code_head=SHA["f"], clock=lambda: now[0])
        states = []
        for _ in range(3):
            row = next(r for r in inbox.rows(self.d) if r["detail"].get("prs") == [111])
            states.append(row["detail"]["deploy"]["state"])
            now[0] += 61                  # every failure is due again
        self.assertIn(checks.DEPLOYED, states[:2], states)


    def test_one_inbox_read_runs_a_bounded_amount_of_git(self):
        for i in range(12):
            self.desk_check(prs=[100 + i]) if i < 5 else None
        # More checks than one chat may post: add the rest as imports, straight to the store.
        for i in range(5, 12):
            self.cstore.create(thread_id=None, project_id=None, task_id=None, created_by="you", title=f"i{i}",
                               prs=[100 + i], steps=[{"do": "a", "expect": ""}],
                               imported_from=f"/p/docs/plans/{i}.md")

        class Slow(FakeGit):
            def __call__(self, *args):
                if args[0] == "log" and args[-2] == "HEAD":
                    self.calls.append(args)
                    return (0, "")
                return super().__call__(*args)
        line = [merge(100 + i, (SHA["a"][:-3] + f"{i:03d}")) for i in range(12)]
        git = Slow({SHA["f"]: line}, hud={sha for sha, _ in line})
        self.d.inbox.deploy = checks.Deployment(git=git, code_head=SHA["f"], hud_head=lambda: (SHA["e"], False))
        self.rows()
        self.assertLessEqual(len(git.calls), checks.GIT_BUDGET)
        self.assertGreater(len(git.calls), 1, "the control: it does read")


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
        self.d.inbox.deploy = checks.Deployment(git=FakeGit({SHA["f"]: [], "HEAD": [merge(12, SHA["a"])]}),
                                                code_head=SHA["f"])
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

    def test_a_folder_swapped_for_a_symlink_after_the_check_is_refused(self):
        """The reviewer's probe p5: `plans/` swapped for a symlink between the
        path's judgement and the read. The read walks folder descriptors with
        O_NOFOLLOW, so the swap is refused, never followed."""
        outside = Path(self.root) / "elsewhere"
        outside.mkdir()
        (outside / self.runbook.name).write_text("## SECRET NOTES\n1. outside the project\n")
        project = self.stores.projects.get(self.project.id)
        path, owner = checks.runbook_path(str(self.runbook), [project])
        plans = Path(project.root) / "docs" / "plans"
        os.rename(plans, plans.with_name("plans.old"))
        os.symlink(outside, plans)
        with self.assertRaises(checks.ImportRefused):
            checks.read_runbook(path, owner)
        # And `docs/` swapped the same way.
        os.unlink(plans)
        os.rename(plans.with_name("plans.old"), plans)
        docs = plans.parent
        os.rename(docs, docs.with_name("docs.old"))
        (outside / "plans").mkdir()
        (outside / "plans" / self.runbook.name).write_text("## X\n1. y\n")
        os.symlink(outside, docs)
        with self.assertRaises(checks.ImportRefused):
            checks.read_runbook(path, owner)
        os.unlink(docs)
        os.rename(docs.with_name("docs.old"), docs)
        self.assertTrue(checks.read_runbook(path, owner).startswith(b"# Owner live checks"), "the control")

    def test_an_archived_projects_runbook_is_refused(self):
        import dataclasses
        self.stores.projects.save(dataclasses.replace(self.stores.projects.get(self.project.id),
                                                      archived=datetime.now(timezone.utc).isoformat()))
        self.preview(status=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
