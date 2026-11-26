"""I3a acceptance checks against the private store and a real feed."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import proposals
from jarvis.v3.feed import Feed, _ref
from jarvis.v3.policy import Policy, set_rule
from jarvis.v3.proposals import Draft, ProposalError, ProposalStore, action_sha256


_STATES = {
    "new": {"briefed", "messaged"}, "briefed": {"approved", "dismissed", "edited", "expired"},
    "approved": {"running"}, "running": {"done", "failed", "unknown"},
    "done": set(), "failed": set(), "unknown": set(),
    "dismissed": set(), "edited": set(), "expired": set(), "messaged": set(),
}


def _claim_process(root, pid, barrier, answer):
    barrier.wait(timeout=10)
    answer.send(ProposalStore(root).claim(pid))
    answer.close()


class ProposalChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.v3 = self.home / "v3"
        self.policy_path = self.v3 / "policy.json"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "JARVIS_V3_DIR": str(self.v3)}))
        self.enterContext(patch.multiple(config, V3_DIR=self.v3, V3_POLICY_FILE=self.policy_path))
        self.instant = datetime(2026, 1, 15, 5, 55, tzinfo=UTC)
        self.store = ProposalStore(clock=lambda: self.instant.timestamp())
        self.feed = Feed(self.v3 / "feed", clock=lambda: self.instant)

    def _draft(self, key="first", *, project=None, tainted=False):
        return Draft(
            kind="thread.send", level_proposed="L0", basis=[{"type": "thread", "id": f"fake-{key}"}],
            brief={"kind": "decision", "rank": 3, "ask": "Send a follow-up?",
                   "context": [{"label": "Fake thread", "ref": f"jarvis:thread:fake-{key}"}]},
            action={"tool": "work_send", "args": {"thread_id": f"fake-{key}", "message": "Fake reply"},
                    "preview": {"thread_id": f"fake-{key}", "project": project, "title": "Fake thread"}},
            dedupe_key=f"thread.send:fake-{key}:0", project=project, tainted=tainted,
        )

    def _add_and_brief(self, key="first", **draft_kwargs):
        proposal = self.store.add(self._draft(key, **draft_kwargs))
        self.assertIsNotNone(proposal)
        self.store.brief(proposal["id"], self.feed)
        return proposal

    def _log_rows(self):
        return [json.loads(line) for line in self.store.path.read_bytes().splitlines()]

    def test_01_private_files_links_and_torn_tail(self):
        proposal = self.store.add(self._draft())
        self.assertEqual(self.store.root, self.v3 / "proposals")
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        pid = proposal["id"]
        with self.subTest(link="directory"):
            link = self.tmp / "linked"
            link.symlink_to(self.v3, target_is_directory=True)
            for root in (link / "proposals", self.tmp / "linked-root"):
                if root.name == "linked-root":
                    root.symlink_to(self.store.root, target_is_directory=True)
                with self.assertRaises(ProposalError):
                    ProposalStore(root).get(pid)
                with self.assertRaises(ProposalError):
                    ProposalStore(root).add(self._draft("link"))
        with self.subTest(link="file"):
            fake_root = self.tmp / "fake-store"
            fake_root.mkdir(mode=0o700)
            fake_store = ProposalStore(fake_root)
            real_bytes = self.store.path.read_bytes()
            fake_store.path.symlink_to(self.store.path)
            with self.assertRaises(ProposalError):
                fake_store.get(pid)
            with self.assertRaises(ProposalError):
                fake_store.add(self._draft("link"))
            self.assertEqual(self.store.path.read_bytes(), real_bytes)
            fake_store.path.unlink()
            os.link(self.store.path, fake_store.path)
            try:
                with self.assertRaises(ProposalError):
                    fake_store.add(self._draft("link"))
            finally:
                fake_store.path.unlink()
        with self.subTest(torn="valid JSON without the commit newline"):
            tail = dict(id=pid, state="briefed", at=self.instant.isoformat(), brief_id="a" * 32, result=None)
            with self.store.path.open("ab") as stream:
                stream.write(json.dumps(tail).encode("ascii"))
            self.assertEqual(self.store.get(pid)["state"], "new")
            second = self.store.add(self._draft("second"))
            self.assertEqual(self.store.get(second["id"]), second)
            self.assertEqual(self.store.get(pid)["state"], "new")

    def test_02_active_dedupe_and_revision(self):
        proposal = self._add_and_brief()
        pid = proposal["id"]
        for state in ("briefed", "approved", "running"):
            with self.subTest(state=state):
                before = self.store.path.read_bytes()
                self.assertIsNone(self.store.add(self._draft()))
                self.assertEqual(self.store.path.read_bytes(), before)
            if state == "briefed":
                self.store.mark(pid, "approved")
            elif state == "approved":
                self.assertTrue(self.store.claim(pid))
        revision = self.store.add(replace(self._draft(), revision_of=pid))
        self.assertEqual(revision["revision_of"], pid)
        self.store.brief(revision["id"], self.feed)
        self.store.mark(revision["id"], "edited")
        self.store.mark(pid, "done")
        self.assertIsNotNone(self.store.add(self._draft()))

    def test_03_dismissal_holds_for_thirty_days_from_dismissal(self):
        proposal = self._add_and_brief()
        self.instant += timedelta(days=40)
        self.store.mark(proposal["id"], "dismissed")
        self.instant += timedelta(days=30, seconds=-1)
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.add(self._draft()))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.instant += timedelta(seconds=1)
        self.assertIsNotNone(self.store.add(self._draft()))

    def test_04_ten_briefs_in_chicago_day_revision_exempt(self):
        previous = self._add_and_brief("previous-chicago-day")
        staged = self.store.add(self._draft("staged-previous-day"))
        self.instant += timedelta(minutes=10)  # Same UTC day, next Chicago day.
        self.assertEqual(self.store.briefs_today(), 0)
        self.store.brief(staged["id"], self.feed)
        self.store.mark(staged["id"], "expired")
        waiting = self.store.add(self._draft("waiting"))
        for i in range(9):
            self._add_and_brief(str(i))
        self.assertEqual(self.store.briefs_today(), 10)
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.add(self._draft("eleventh")))
        with self.assertRaises(ProposalError):
            self.store.brief(waiting["id"], self.feed)
        self.assertEqual(self.store.path.read_bytes(), before)
        revision = self.store.add(replace(self._draft("revision"), revision_of=previous["id"]))
        self.assertIsNotNone(revision)
        self.store.brief(revision["id"], self.feed)
        self.assertEqual(self.store.briefs_today(), 10)
        self.instant += timedelta(hours=24)
        self.assertIsNotNone(self.store.add(self._draft("eleventh")))

    def test_05_fourth_proposal_in_one_run_deferred(self):
        for i in range(3):
            self.assertEqual(self.store.add(self._draft(str(i)), run="run-a")["run"], "run-a")
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.add(self._draft("fourth"), run="run-a"))
        self.assertEqual(self.store.path.read_bytes(), before)
        for i in range(4):
            self.assertIsNone(self.store.add(self._draft(f"no-run-{i}"))["run"])
        proposal = self.store.add(self._draft("fourth"), run="run-b")
        self.assertEqual(proposal["run"], "run-b")
        revision = self.store.add(replace(self._draft("fourth"), revision_of=proposal["id"]), run="run-a")
        self.assertIsNotNone(revision)

    def test_06_fourth_open_project_brief_deferred(self):
        waiting = self.store.add(self._draft("waiting", project="fake-project"))
        for i in range(3):
            proposal = self._add_and_brief(str(i), project="fake-project")
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.add(self._draft("fourth", project="fake-project")))
        with self.assertRaises(ProposalError):
            self.store.brief(waiting["id"], self.feed)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNotNone(self.store.add(self._draft("other", project="another-project")))
        for i in range(4):
            self._add_and_brief(f"no-project-{i}")
        self.store.mark(proposal["id"], "approved")
        self.assertIsNotNone(self.store.add(self._draft("fourth", project="fake-project")))
        self.store.brief(waiting["id"], self.feed)

    def test_07_brief_posts_once_and_recovers_after_failed_record(self):
        proposal = self.store.add(self._draft())
        pid = proposal["id"]
        # A feed post can commit just before the proposal transition fails.
        with patch.object(self.store, "_transition", side_effect=OSError):
            with self.assertRaises(ProposalError):
                self.store.brief(pid, self.feed)
        original = self.feed.briefs("open")[0]["id"]
        self.assertEqual(self.store.get(pid)["state"], "new")
        with patch.object(self.feed, "post", wraps=self.feed.post) as post:
            reopened = ProposalStore(self.store.root, clock=lambda: self.instant.timestamp())
            self.assertEqual(reopened.brief(pid, self.feed), original)
            post.assert_called_once()
            self.assertEqual(self.store.brief(pid, self.feed), original)
            post.assert_called_once()
        self.assertEqual([row["id"] for row in self.feed.briefs("open")], [original])
        self.assertEqual(self.store.get(pid)["brief_id"], original)
        self.assertEqual(self.store.get(pid)["state"], "briefed")

    def test_08_feed_keeps_the_exact_proposal_reference(self):
        proposal = self._add_and_brief()
        stored = self.store.get(proposal["id"])
        brief = self.feed.get(stored["brief_id"])
        ref = f"{stored['id']}:{stored['action_sha256']}"
        self.assertEqual(brief["proposal_id"], ref)
        self.assertEqual(_ref(ref), ref)
        self.assertEqual(brief["actor"], "jarvis")
        self.assertIsNone(brief["session"])
        self.assertRegex(stored["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(stored["action_sha256"], r"^[0-9a-f]{64}$")

    def test_09_hash_binds_canonical_action_including_every_argument(self):
        from jarvis.v3.approval_card import build_card

        draft = self._draft(project="fake-project")
        proposal = self.store.add(draft)
        action = proposal["action"]
        self.assertEqual(action, draft.action)
        card = build_card(action["tool"], action["args"], action["preview"])
        self.assertIn({"label": "Message", "value": "Fake reply"}, card["rows"])
        expected = json.dumps(
            {"v": proposal["v"], "id": proposal["id"], "kind": proposal["kind"],
             "tool": action["tool"], "args": action["args"], "preview": action["preview"]},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        signature = hashlib.sha256(expected.encode("ascii")).hexdigest()
        self.assertEqual(proposal["action_sha256"], signature)
        self.assertEqual(action_sha256(proposal), signature)
        for arg in action["args"]:
            with self.subTest(arg=arg):
                changed = deepcopy(proposal)
                changed["action"]["args"][arg] = "different fake argument"
                self.assertNotEqual(action_sha256(changed), signature)
        changed = deepcopy(proposal)
        changed["action"]["preview"]["title"] = "Different title"
        self.assertNotEqual(action_sha256(changed), signature)
        changed["action"] = deepcopy(action)
        changed["action"]["args"] = dict(reversed(list(action["args"].items())))
        self.assertEqual(action_sha256(changed), signature)

    def test_10_claim_once_across_objects_and_processes_and_fsync(self):
        proposal = self._add_and_brief()
        pid = proposal["id"]
        reopened = ProposalStore(self.store.root)
        self.assertFalse(reopened.claim(pid))
        self.store.mark(pid, "approved")
        synced = []
        fsync = os.fsync

        def record_sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                with self.store.path.open("rb") as other, self.assertRaises(BlockingIOError):
                    proposals.fcntl.flock(other, proposals.fcntl.LOCK_SH | proposals.fcntl.LOCK_NB)
                synced.append(os.pread(fd, 4096, 0))
            fsync(fd)

        with patch.object(proposals.os, "fsync", side_effect=record_sync):
            self.assertTrue(reopened.claim(pid))
        self.assertTrue(any(b'"state":"running"' in raw for raw in synced))
        self.assertFalse(self.store.claim(pid))
        self.assertEqual(self.store.get(pid)["state"], "running")

        contender = self._add_and_brief("race")
        self.store.mark(contender["id"], "approved")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(4)
        processes, answers = [], []
        try:
            for _ in range(4):
                read_end, write_end = ctx.Pipe(duplex=False)
                process = ctx.Process(target=_claim_process,
                                      args=(self.store.root, contender["id"], barrier, write_end))
                process.start()
                write_end.close()
                processes.append(process)
                answers.append(read_end)
            results = []
            for answer in answers:
                self.assertTrue(answer.poll(10), "claim process did not return")
                results.append(answer.recv())
            self.assertCountEqual(results, [True, False, False, False])
        finally:
            for answer in answers:
                answer.close()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10)
                process.close()
        self.assertEqual(self.store.get(contender["id"])["state"], "running")

    def test_11_l3_leaves_no_proposal(self):
        blocked = replace(self._draft(), kind="credential.read", action=None)
        self.assertIsNone(self.store.add(blocked))
        self.assertFalse(self.store.root.exists())
        set_rule(self.policy_path, "thread.send", "L3")
        self.assertIsNone(self.store.add(self._draft()))
        self.assertEqual(self.store.proposals(), [])
        self.assertFalse(self.store.root.exists())

    def test_12_thread_send_has_an_explicit_l2_default(self):
        facts = {"kind": "thread.send", "tainted": False}
        self.assertEqual(Policy().level_for(facts), "L2")
        self.assertEqual(Policy().explain(facts)["why"], "default:thread.send")

    def test_ask_only_is_a_news_brief(self):
        proposal = self.store.add(replace(self._draft(), kind="ask", action=None))
        self.assertIsNone(proposal["action"])
        self.assertEqual(proposal["brief"]["kind"], "news")
        brief = self.feed.get(self.store.brief(proposal["id"], self.feed))
        self.assertEqual(brief["kind"], "news")
        self.assertIsNone(brief["default"])
        self.assertEqual(self.store.briefs_today(), 1)

    def test_taint_context_is_first_and_calendar_policy_gets_facts(self):
        draft = replace(self._draft(tainted=True), kind="calendar.block",
                        action={"tool": "calendar_create", "args": {"title": "Fake block"}, "preview": {}})
        tainted = self.store.add(draft)
        self.assertEqual(tainted["level_proposed"], "L0")
        self.assertEqual((tainted["level"], tainted["why"]), ("L2", "hard:tainted"))
        clean = replace(draft, tainted=False, dedupe_key="calendar.block:fake-clean")
        set_rule(self.policy_path, "calendar.block", "L1", {"attendees": 0, "owner_event": False})
        self.assertEqual(self.store.add(clean)["level"], "L1")
        brief = self.feed.get(self.store.brief(tainted["id"], self.feed))
        self.assertEqual(brief["context"], [
            {"label": "Based on untrusted text", "ref": "jarvis:tainted"}, *draft.brief["context"],
        ])

    def test_record_has_exact_schema(self):
        proposal = self._add_and_brief()
        self.store.mark(proposal["id"], "approved")
        keys = set("v id created kind level_proposed level why tainted basis action action_sha256 "
                   "brief brief_id dedupe_key project run state revision_of result".split())
        base, *transitions = self._log_rows()
        self.assertEqual(set(base), keys)
        self.assertEqual(set(self.store.get(proposal["id"])), keys)
        for transition in transitions:
            self.assertEqual(set(transition), {"id", "state", "at", "brief_id", "result"})

    def test_last_transition_time_is_durable(self):
        proposal = self.store.add(self._draft())
        pid = proposal["id"]
        self.assertIsNone(self.store.transition_at(pid))
        self.assertIsNone(self.store.transition_at("0" * 32))
        self.store.brief(pid, self.feed)
        self.instant += timedelta(hours=1)
        self.store.mark(pid, "approved")
        reopened = ProposalStore(self.store.root)
        self.assertEqual(reopened.transition_at(pid), self.instant)
        self.instant += timedelta(hours=2)
        self.assertTrue(self.store.claim(pid))
        self.assertEqual(reopened.transition_at(pid), self.instant)

    def test_messaged_replays_and_is_final_and_idempotent(self):
        pid = self.store.add(self._draft())["id"]
        self.store.message(pid, "fake-thread")
        self.assertEqual(self._log_rows()[-1], {
            "id": pid, "state": "messaged", "at": self.instant.isoformat(),
            "brief_id": None, "result": {"thread_id": "fake-thread"},
        })
        before = self.store.path.read_bytes()
        reopened = ProposalStore(self.store.root, clock=lambda: self.instant.timestamp())
        self.assertEqual(reopened.get(pid)["result"], {"thread_id": "fake-thread"})
        self.assertEqual(reopened.transition_at(pid), self.instant)
        self.assertEqual((reopened.briefs_today(), reopened.messaged_today()), (0, 1))
        self.instant += timedelta(days=31)
        reopened.message(pid, "fake-thread")
        with self.assertRaises(ProposalError):
            reopened.message(pid, "other-thread")
        with self.assertRaises(ProposalError):
            reopened.brief(pid, self.feed)
        self.assertEqual(self.feed.briefs(), [])
        self.assertEqual(self.store.path.read_bytes(), before)
        briefed = self._add_and_brief("briefed")
        with self.assertRaises(ProposalError):
            reopened.message(briefed["id"], "fake-thread")

    def test_malformed_message_records_and_changed_hash_are_refused(self):
        pid = self.store.add(self._draft())["id"]
        before = self.store.path.read_bytes()
        event = dict(id=pid, state="messaged", at=self.instant.isoformat(),
                     brief_id=None, result={"thread_id": "fake-thread"})
        for change in ({"brief_id": "1" * 32}, {"at": "yesterday"}, {"extra": None}, {"result": None},
                       {"result": {"ref": "fake-thread"}}, {"result": {"thread_id": "invalid id"}},
                       {"result": {"thread_id": "fake-thread", "extra": None}}):
            with self.subTest(change=change):
                self.store.path.write_bytes(before + json.dumps(event | change).encode() + b"\n")
                with self.assertRaises(ProposalError):
                    self.store.get(pid)
        base = json.loads(before)
        base["action"]["args"]["message"] = "Changed fake reply"
        self.store.path.write_text(json.dumps(base) + "\n")
        with self.assertRaisesRegex(ProposalError, "action hash differs"):
            self.store.message(pid, "fake-thread")

    def test_messaged_and_briefed_share_a_chicago_day_cap(self):
        staged = self.store.add(self._draft("staged"))
        old = self.store.add(self._draft("previous-chicago-day"))
        self.store.message(old["id"], "fake-thread")
        self.assertEqual(self.store.messaged_today(), 1)
        self.instant += timedelta(minutes=10)  # Same UTC day, next Chicago day.
        self.assertEqual(self.store.messaged_today(), 0)
        waiting = self.store.add(self._draft("waiting"))
        self.store.message(staged["id"], "fake-next-thread")
        for i in range(proposals.CAPS["day"] - 1):
            self._add_and_brief(str(i))
        self.assertEqual(self.store.messaged_today(), 1)
        self.assertEqual(self.store.briefs_today(), proposals.CAPS["day"] - 1)
        self.assertIsNone(self.store.add(self._draft("overflow")))
        with self.assertRaises(ProposalError):
            self.store.message(waiting["id"], "fake-other-thread")
        self.instant += timedelta(days=1)
        self.store.message(waiting["id"], "fake-other-thread")
        self.assertEqual(self.store.messaged_today(), 1)

    def test_messaged_project_cap_and_dedupe_expire_from_delivery(self):
        draft = self._draft(project="fake-project")
        first = self.store.add(draft)
        duplicate = self.store.add(draft)
        self.instant += timedelta(hours=2)
        self.store.message(first["id"], "fake-first-thread")
        with self.assertRaises(ProposalError):
            self.store.message(duplicate["id"], "fake-duplicate-thread")
        self._add_and_brief("active", project=draft.project)
        extra = self.store.add(self._draft("extra", project=draft.project))
        self.store.message(extra["id"], "fake-extra-thread")
        waiting = self._draft("waiting", project=draft.project)
        self.instant += timedelta(hours=72, seconds=-1)
        self.assertIsNone(self.store.add(waiting))
        self.instant += timedelta(seconds=1)
        self.assertIsNotNone(self.store.add(waiting))
        self.instant += timedelta(days=27, seconds=-1)
        self.assertIsNone(self.store.add(draft))
        self.instant += timedelta(seconds=1)
        self.assertIsNotNone(self.store.add(draft))

    def test_every_state_transition(self):
        def transition(pid, state, *, result=None):
            if state == "briefed":
                self.store.brief(pid, self.feed)
            elif state == "messaged":
                self.store.message(pid, "fake-thread")
            elif state == "running":
                self.assertTrue(self.store.claim(pid))
            else:
                self.store.mark(pid, state, result=result)

        def walk(path):
            current = path[-1]
            pid = self.store.add(replace(self._draft(), revision_of="1" * 32))["id"]
            for state in path[1:]:
                transition(pid, state)
            for state in (*_STATES, "unrecognised"):
                if state in _STATES[current] and state not in ("briefed", "messaged", "running"):
                    continue
                with self.subTest(current=current, refused=state):
                    before = self.store.path.read_bytes()
                    with self.assertRaises(ProposalError):
                        self.store.mark(pid, state)
                    self.assertEqual(self.store.path.read_bytes(), before)
            for next_state in sorted(_STATES[current]):
                walk([*path, next_state])

        walk(["new"])
        proposal = self._add_and_brief("result")
        pid = proposal["id"]
        self.store.mark(pid, "approved")
        self.assertTrue(self.store.claim(pid))
        transition(pid, "done", result={"ref": "jarvis:thread:fake-result"})
        self.assertEqual(self.store.get(pid)["result"], {"ref": "jarvis:thread:fake-result"})
        self.assertEqual([row["id"] for row in self.store.proposals("done")][-1], pid)

    def test_feed_failure_does_not_expose_payload(self):
        proposal = self.store.add(self._draft())
        fake_text = "Private fake brief and argument"
        with patch.object(self.feed, "post", side_effect=RuntimeError(fake_text)):
            with self.assertRaises(ProposalError) as caught:
                self.store.brief(proposal["id"], self.feed)
        self.assertNotIn(fake_text, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.store.get(proposal["id"])["state"], "new")

    def test_action_tool_must_match_its_policy_kind(self):
        draft = self._draft()
        for kind, tool in (("thread.start", "work_send"), ("garden.water", None)):
            with self.subTest(kind=kind):
                with self.assertRaises(ProposalError):
                    self.store.add(replace(draft, kind=kind, action=dict(draft.action, tool=tool)))
        self.assertEqual(self.store.proposals(), [])

    def test_import_stays_out_of_the_model_stack_and_work(self):
        script = self.tmp / "import_probe.py"
        script.write_text(
            "import sys\n"
            "from jarvis.v3 import proposals\n"
            "blocked = {'jarvis.agent', 'jarvis.llm', 'jarvis.models', 'jarvis.v2.providers', "
            "'jarvis.v3.work', 'jarvis.v3.claude_jobs', 'jarvis.v3.t3_client'}\n"
            "assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)\n"
        )
        run = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, timeout=15,
                             env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
