"""SV-4: the owner stops one native sub-agent, and nothing else stops.

Free and offline. What is pinned, in the order it would hurt:

  - **the route is the owner's** (`POST /threads/{id}/subagents/{sid}/stop`):
    the API listener, a bare request, a cross-site, opaque (`null`) or
    workshop Origin, and the workshop listener itself are refused before the
    thread is looked up or the body read, and the provider is never asked;
    no other module calls `Daemon.stop_subagent` (no session MCP tool,
    Discord verb or CLI command reaches it);
  - **one child stops, nothing else does**: Claude's `stop_task(task_id)`
    (never `interrupt()`) on the real `ClaudeProvider` replaying SV-0's
    `stop_fg_parallel` — the stopped child ends "stopped by the owner", its
    sibling finishes, the orchestrator's turn ends normally; and between
    turns (`stop_bg`) the CLI's word arrives with the next message; Codex's
    `turn/interrupt` on the child's own thread and turn, over real pipes on
    SV-0's `child_interrupt` recording — the child's turn ends, the
    parent's completes, and a refused answer is never fatal;
  - **the record says "stopping" until the CLI says otherwise**: one
    request however often Stop is pressed while it is on its way; the CLI's
    end clears it; no word within `STOP_CONFIRM_S` is said honestly as
    "unconfirmed" (and Stop may be pressed again); a refusal is a 409 with a
    plain sentence (`stale`, `supported: false`, `unconfirmed`) and leaves
    no mark; the bus carries the state, never a name;
  - **offers**: a live row says `stoppable`, or why not.

Run: PYTHONPATH=. .venv/bin/python tests/v2/subagent_stop_check.py
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from editor_files_check import Daemonised, FAKE_SECRET  # noqa: E402
import claude_replay as R  # noqa: E402
import codex_provider_check as P  # noqa: E402
from jarvis import config  # noqa: E402
from jarvis.v2 import subagents as S  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, Thread  # noqa: E402
from jarvis.v2.provider import (Brief, Decision, Event, EventKind as K, SessionHandle,  # noqa: E402
                                Usage, UserMessage)
from jarvis.v2.providers import claude, codex, codex_config  # noqa: E402
from tests import checklib  # noqa: E402

try:  # absent before SV-4: the checks that need them fail, by name
    from jarvis.v2.provider import SubagentNotLive, SubagentStopUnsupported  # noqa: E402
except ImportError:  # pragma: no cover
    SubagentNotLive = SubagentStopUnsupported = type("Missing", (Exception,), {})
try:  # added in the review of #216
    from jarvis.v2.provider import SubagentStopDeclined  # noqa: E402
except ImportError:  # pragma: no cover
    SubagentStopDeclined = type("Missing", (Exception,), {})

OWNER = "stopped by the owner"
REPO = Path(__file__).resolve().parents[2]


# --- Claude, through the real provider -------------------------------------------


class GatedClient(R.ReplayClient):
    """A replay that waits, where the recorder called `stop_task`, for the
    provider to call it (with the recorded task id) — at most `GATE_S`, so a
    provider that never does fails the check instead of hanging it."""

    GATE_S = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopped: list[str] = []
        self.at_gate = threading.Event()

    async def stop_task(self, task_id):
        self.stopped.append(task_id)

    async def receive_messages(self):
        last = self.pos
        async for item in super().receive_messages():
            skipped = self.rows[last:self.pos]
            last = self.pos
            for row in skipped:
                if row["src"] == "harness" and row["data"].get("action") == "stop_task":
                    want = row["data"]["task_id"]
                    self.at_gate.set()
                    waited = 0.0
                    while want not in self.stopped and waited < self.GATE_S:
                        await asyncio.sleep(0.01)
                        waited += 0.01
            yield item


class ClaudeRun:
    """`name` played through a real ClaudeProvider whose client is gated."""

    def __init__(self, name):
        self.name = name
        self.stack = ExitStack()
        recorded = R.rows(name)
        R.ReplayClient.instances = []
        self.stack.enter_context(patch.object(
            claude, "_client_factory", lambda options: GatedClient(options, recorded)))
        self.stack.enter_context(patch.object(claude, "resolve_cli", lambda: None))
        self.provider = claude.ClaudeProvider()
        self.handle = self.provider.start(R.thread(), R.brief(), R.Permit())
        self.turns: list[list] = []

    @property
    def client(self) -> GatedClient:
        return R.ReplayClient.instances[-1]

    def send_async(self, text="turn"):
        out: list = []
        done = threading.Event()

        def run():
            try:
                out.extend(self.provider.send(self.handle, UserMessage(text=text)))
            finally:
                done.set()
        threading.Thread(target=run, daemon=True).start()
        return out, done

    def send(self, text="turn"):
        out, done = self.send_async(text)
        if not done.wait(30):
            raise AssertionError("the turn never finished")
        self.turns.append(out)
        return out

    def close(self):
        try:
            self.provider.close(self.handle)
        finally:
            self.stack.close()


def life(events, sid):
    return [e.data for e in events if e.kind == K.SUBAGENT and e.data["sid"] == sid]


class ClaudeStop(unittest.TestCase):
    SLEEPER, NAPPER = "cl:toolu_sv0_sleeper", "cl:toolu_sv0_napper"
    SLEEPER_TASK = "a0a35e05af03d57f5"

    def run_parallel(self):
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        self.assertIsNone(run.provider.subagent_stop_refusal(run.handle, self.SLEEPER))
        run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(done.wait(30), "the turn never finished")
        return run, out

    def test_stop_task_stops_one_child_and_the_turn_and_its_sibling_go_on(self):
        run, out = self.run_parallel()
        self.assertEqual(run.client.stopped, [self.SLEEPER_TASK])
        self.assertEqual(run.client.interrupts, 0, "the orchestrator's turn was interrupted")
        sleeper = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in sleeper], [("stopped", OWNER)])
        napper = [d for d in life(out, self.NAPPER) if d["op"] == "finished"]
        self.assertEqual([d["status"] for d in napper], ["done"])
        finished = [e for e in out if e.kind == K.TURN_FINISHED]
        self.assertEqual([e.data.get("stop") for e in finished], ["end"])
        self.assertTrue(any(e.kind == K.TEXT for e in out), "the orchestrator said nothing after the stop")

    def test_stop_between_turns_is_confirmed_by_the_next_messages_reading(self):
        run = ClaudeRun("stop_bg")
        self.addCleanup(run.close)
        child = "cl:toolu_sv0_bgsleeper"
        first = run.send("one")
        self.assertEqual(life(first, child)[-1]["status"], "backgrounded")
        run.provider.stop_subagent(run.handle, child)
        self.assertEqual(run.client.stopped, ["a218409de06f88bc5"])
        self.assertEqual(run.client.interrupts, 0)
        # Unwatched, nobody reads between turns: still live until the CLI's
        # word is read. (Watched, SV-6 reads it at once: idle_reader_check.)
        self.assertIn(child, run.provider.live_subagents(run.handle))
        second = run.send("two")
        ended = [d for d in life(second, child) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])

    def test_refusals_send_nothing(self):
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        p, h = run.provider, run.handle
        with self.assertRaises(SubagentNotLive):
            p.stop_subagent(h, "cl:toolu_nobody")
        self.assertIsInstance(p.subagent_stop_refusal(h, "cl:toolu_nobody"), str)
        with patch.object(claude, "CLAUDE_STOP_VERIFIED", ""):
            with self.assertRaises(SubagentStopUnsupported) as caught:
                p.stop_subagent(h, self.SLEEPER)
            self.assertIn("whole turn", str(caught.exception))
            self.assertIn("whole turn", p.subagent_stop_refusal(h, self.SLEEPER))
        with patch.object(claude, "_known_cli_version", lambda: (2, 1, 290)):
            with self.assertRaises(SubagentStopUnsupported):
                p.stop_subagent(h, self.SLEEPER)
        with patch.object(claude, "_known_cli_version", lambda: (2, 2, 0)):
            self.assertIsNone(p.subagent_stop_refusal(h, self.SLEEPER))
        session = h.native
        with session.mutex:
            session.children[self.SLEEPER].task_id = None
        with self.assertRaises(SubagentStopUnsupported):
            p.stop_subagent(h, self.SLEEPER)
        with session.mutex:
            session.children[self.SLEEPER].task_id = self.SLEEPER_TASK
        self.assertEqual(run.client.stopped, [], "a refusal sent something")
        run.provider.stop_subagent(h, self.SLEEPER)
        self.assertTrue(done.wait(30))
        with self.assertRaises(SubagentNotLive):
            p.stop_subagent(h, self.SLEEPER)          # ended now
        run.provider.close(h)
        with self.assertRaises(SubagentNotLive):
            p.stop_subagent(h, self.NAPPER)

    def test_the_verified_floor_is_this_cli(self):
        self.assertEqual(claude.CLAUDE_STOP_VERIFIED, "2.1.295")
        self.assertLessEqual(claude.STOP_CONTROL_TIMEOUT_S, 10.0)

    def test_an_unanswered_control_request_is_a_timeout_not_a_hang(self):
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)

        async def never(task_id):
            await asyncio.sleep(3600)
        with patch.object(run.client, "stop_task", never), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.2):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
            self.assertLess(time.monotonic() - started, 5)
        run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(done.wait(30))

    def test_a_timed_out_request_is_left_to_the_sdks_own_cleanup(self):
        """Review of #216: cancelling the request on our timeout would skip
        the SDK's own cleanup of its private `pending_control_responses`; it
        is left to finish instead."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        finished = threading.Event()
        real = run.client.stop_task

        async def slow(task_id):
            await asyncio.sleep(0.5)
            finished.set()
            await real(task_id)
        with patch.object(run.client, "stop_task", slow), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(finished.wait(5), "the request was cancelled rather than left to finish")
        self.assertTrue(done.wait(30))
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])

    def test_the_clis_refusal_is_its_own_answer(self):
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)

        async def refuse(task_id):
            raise Exception(f"No task found with ID: {task_id}")    # the SDK's error control response
        with patch.object(run.client, "stop_task", refuse):
            with self.assertRaises(SubagentStopDeclined) as caught:
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertIn("No task found", str(caught.exception))
        self.assertFalse(run.handle.native.children[self.SLEEPER].owner_stop)

        async def lost(task_id):
            raise Exception("Control request timeout: stop_task")    # the SDK's own timeout: not a refusal
        with patch.object(run.client, "stop_task", lost):
            with self.assertRaises(Exception) as caught:
                run.provider.stop_subagent(run.handle, self.SLEEPER)
            self.assertNotIsInstance(caught.exception, SubagentStopDeclined)
        # The CLI may still act on it: the mark stays, as for one that times out late (#220 review, L2).
        self.assertTrue(run.handle.native.children[self.SLEEPER].owner_stop)
        run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(done.wait(30))

    def test_a_late_refusal_takes_the_owners_mark_off(self):
        """Review of #216's delta, LOW 1: the CLI refuses after the route's
        timeout. The child's later end is not "stopped by the owner"."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        refused = threading.Event()

        async def late_refusal(task_id):
            await asyncio.sleep(0.4)
            refused.set()
            raise Exception(f"No task found with ID: {task_id}")    # the CLI's word, late
        with patch.object(run.client, "stop_task", late_refusal), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        child = run.handle.native.children[self.SLEEPER]
        self.assertTrue(child.owner_stop, "the mark came off before the CLI answered")
        checklib.wait_for(lambda: refused.is_set() and not child.owner_stop, what="the late refusal to clear the mark", timeout=10.0)
        self.assertTrue(done.wait(30), "the turn never finished")
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([d["status"] for d in ended], ["stopped"])
        self.assertNotEqual(ended[0].get("stop_reason"), OWNER)

    def test_a_late_refusal_after_a_taken_stop_leaves_the_mark(self):
        """A first Stop timed out, a second taken at once: the first one's
        late refusal does not undo the second."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        refused = threading.Event()

        async def late_refusal(task_id):
            await asyncio.sleep(0.4)
            refused.set()
            raise Exception(f"No task found with ID: {task_id}")

        async def taken(task_id):
            return None                         # taken; the CLI's word comes with the gate
        with patch.object(run.client, "stop_task", late_refusal), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        with patch.object(run.client, "stop_task", taken):
            run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(refused.wait(5))
        self.assertTrue(done.wait(30), "the turn never finished")
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])

    def test_an_undelivered_stop_takes_the_mark_off_in_time_and_late(self):
        """#220 delta review, L2: a transport error (the request never
        reached the CLI) settles as refused by the one rule
        (`_stop_outcome`), whether it comes in time or after our timeout."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        child = run.handle.native.children[self.SLEEPER]

        async def lost(task_id):
            raise ConnectionError("the CLI's pipe closed")
        with patch.object(run.client, "stop_task", lost):
            with self.assertRaises(ConnectionError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertFalse(child.owner_stop, "an undelivered Stop in time left the mark")
        failed = threading.Event()

        async def lost_late(task_id):
            await asyncio.sleep(0.3)
            failed.set()
            raise ConnectionError("the CLI's pipe closed")
        with patch.object(run.client, "stop_task", lost_late), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(child.owner_stop)
        checklib.wait_for(lambda: failed.is_set() and not child.owner_stop,
                 what="the late transport error to take the mark off", timeout=10.0)
        self.assertEqual((child.owner_stops, child.owner_stop_taken), (0, False))
        self.assertTrue(done.wait(30), "the turn never finished")
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertNotEqual(ended[0].get("stop_reason"), OWNER)

    def test_a_late_sdk_timeout_keeps_the_owners_mark(self):
        """The SDK's own "Control request timeout" is not the CLI's refusal:
        the CLI may still act on the request, so the mark stays."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        answered = threading.Event()
        real = run.client.stop_task

        async def lost(task_id):
            await asyncio.sleep(0.3)
            await real(task_id)                 # the CLI acted on it after all
            answered.set()
            raise Exception("Control request timeout: stop_task")
        with patch.object(run.client, "stop_task", lost), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(answered.wait(5))
        self.assertTrue(done.wait(30), "the turn never finished")
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])

    def test_a_refusal_keeps_the_mark_while_another_stop_is_on_its_way(self):
        """A first Stop timed out and still on its way, a second refused at
        once: the first, taken late, still makes the end the owner's."""
        run = ClaudeRun("stop_fg_parallel")
        self.addCleanup(run.close)
        out, done = run.send_async()
        checklib.wait_for(lambda: R.ReplayClient.instances and run.client.at_gate.is_set(), what="the stop gate", timeout=10.0)
        real = run.client.stop_task

        async def slow(task_id):
            await asyncio.sleep(0.5)
            await real(task_id)

        async def refuse(task_id):
            raise Exception(f"No task found with ID: {task_id}")
        with patch.object(run.client, "stop_task", slow), \
                patch.object(claude, "STOP_CONTROL_TIMEOUT_S", 0.1):
            with self.assertRaises(TimeoutError):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        with patch.object(run.client, "stop_task", refuse):
            with self.assertRaises(SubagentStopDeclined):
                run.provider.stop_subagent(run.handle, self.SLEEPER)
        self.assertTrue(run.handle.native.children[self.SLEEPER].owner_stop,
                        "a refusal took off the mark of a Stop still on its way")
        self.assertTrue(done.wait(30), "the turn never finished")
        ended = [d for d in life(out, self.SLEEPER) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])


# --- Codex, through the real provider over real pipes ----------------------------


def codex_script(error=None, answers=None):
    """SV-0's `child_interrupt` parent turn, with the recorder's own
    `turn/interrupt` replaced by a wait for the provider's (the peer answers
    it; aimed at the parent, the parent's turn ends interrupted). `answers`:
    that many of the provider's, all read before any is answered, each with
    its error (None: taken)."""
    rows = [json.loads(line) for line in (P.FIXTURES / "child_interrupt.jsonl").read_text().splitlines()]
    start = next(r["data"] for r in rows if r["src"] == "rpc_out" and r["data"].get("method") == "turn/start")
    thread = start["params"]["threadId"]
    asked = next(r["data"] for r in rows if r["src"] == "rpc_out" and r["data"].get("method") == "turn/interrupt")
    turn, msgs = None, []
    for r in rows:
        d = r["data"]
        if r["src"] == "harness" and str(d.get("action", "")).startswith("turn/interrupt"):
            wait = {"$WAIT": "turn/interrupt", "thread": asked["params"]["threadId"],
                    "turn": asked["params"]["turnId"]}
            if error:
                wait["error"] = error
            if answers is not None:
                wait["answers"] = list(answers)
            msgs.append(wait)
            continue
        if r["src"] != "rpc_in":
            continue
        if "method" not in d:
            if d.get("id") == start["id"]:
                turn = d["result"]["turn"]["id"]
                msgs.append({**d, "id": "$START"})
            continue                                   # the recorder's own answers
        if turn is None:
            continue
        msgs.append(d)
        if d["method"] == "turn/completed" and d["params"]["threadId"] == thread:
            break
    text = json.dumps(msgs).replace(thread, "$THREAD").replace(turn, "$TURN")
    return json.loads(re.sub(r"<TMP>/work-[A-Za-z0-9_]+", "$CWD", text)), asked["params"]


class CodexStop(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="sv4-")))
        auth = root / "owner"
        (auth / "skills").mkdir(parents=True)
        (auth / "auth.json").write_text("FAKE AUTH: must never be read by provider")
        work = root / "work"
        work.mkdir()
        self.brain = P.Brain(root)
        self.stack.enter_context(patch.object(config, "V2_DATA_DIR", root / "data"))
        self.stack.enter_context(patch.object(config, "CHAT_WORKTREES_DIR", root / "chat-worktrees"))
        self.stack.enter_context(patch.object(codex_config, "owner_home", return_value=auth))
        self.stack.enter_context(patch.object(codex, "RpcProcess", self.brain.rpc))
        self.stack.enter_context(patch.object(codex.CodexProvider, "_probe", return_value=("/fake/codex", "fake")))
        self.provider = codex.CodexProvider()
        self.work = work

    def play(self, error=None, stop=True):
        script, asked = codex_script(error)
        self.child = asked["threadId"]
        self.sid = "cx:" + self.child
        self.brain.script.update(mode="replay", replay=script)
        h = self.provider.start(Thread("sv4", "p", Role.CHAT, PN.CODEX),
                                Brief(Role.CHAT, str(self.work), model="fake-model", effort="high"),
                                lambda *a, **k: Decision.DENY)
        self.addCleanup(self.provider.close, h)
        out, seen, done = [], threading.Event(), threading.Event()

        def consume():
            try:
                for event in self.provider.send(h, UserMessage("go")):
                    out.append(event)
                    if (event.kind == K.SUBAGENT_EVENT and event.data["sid"] == self.sid
                            and event.data["event"]["kind"] == "tool_started"):
                        seen.set()
            finally:
                done.set()
        threading.Thread(target=consume, daemon=True).start()
        self.assertTrue(seen.wait(20), "the child's command never started")
        if stop:
            self.assertIsNone(self.provider.subagent_stop_refusal(h, self.sid))
            self.provider.stop_subagent(h, self.sid)
        if not done.wait(30):
            self.provider.close(h)
            self.fail("the parent's turn never finished")
        return h, out, asked

    def test_turn_interrupt_goes_to_the_child_and_the_parent_completes(self):
        h, out, asked = self.play()
        sent = self.brain.calls("turn/interrupt")
        self.assertEqual([c["params"] for c in sent], [{"threadId": self.child, "turnId": asked["turnId"]}])
        ended = [e.data for e in out if e.kind == K.SUBAGENT and e.data["sid"] == self.sid
                 and e.data["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])
        finished = [e.data for e in out if e.kind == K.TURN_FINISHED]
        self.assertEqual([d.get("stop") for d in finished], ["end"], "the parent's turn did not complete")
        self.assertFalse([e for e in out if e.kind == K.ERROR and e.data.get("fatal")])
        self.assertTrue(any(e.kind == K.TEXT for e in out), "the orchestrator's answer is missing")
        self.assertIsInstance(self.provider.subagent_stop_refusal(h, self.sid), str)
        with self.assertRaises(SubagentNotLive):
            self.provider.stop_subagent(h, self.sid)

    def test_a_refused_answer_is_never_the_parents_failure(self):
        h, out, _ = self.play(error={"code": -32600, "message": "no active turn"})
        self.assertFalse([e for e in out if e.kind == K.ERROR and e.data.get("fatal")],
                         "Codex refusing the child's stop ended the orchestrator's turn")
        self.assertEqual([e.data.get("stop") for e in out if e.kind == K.TURN_FINISHED], ["end"])
        # Refused, so the child's later end is not the owner's (review of #216).
        ended = [e.data for e in out if e.kind == K.SUBAGENT and e.data["sid"] == self.sid
                 and e.data["op"] == "finished"]
        self.assertEqual([d.get("stop_reason") for d in ended], ["its turn was interrupted"])

    def test_a_refused_stop_leaves_the_mark_of_one_still_on_its_way(self):
        """Review of #216's delta, LOW 2: two Stops on their way, the first
        refused and the second taken. The child's end is the owner's."""
        self.two_stops([{"code": -32600, "message": "busy"}, None])

    def test_a_refusal_after_a_taken_stop_leaves_the_mark(self):
        """The same two answers the other way round: the taken one first."""
        self.two_stops([None, {"code": -32600, "message": "busy"}])

    def two_stops(self, answers):
        script, asked = codex_script(answers=answers)
        self.child, self.sid = asked["threadId"], "cx:" + asked["threadId"]
        self.brain.script.update(mode="replay", replay=script)
        h = self.provider.start(Thread("sv4", "p", Role.CHAT, PN.CODEX),
                                Brief(Role.CHAT, str(self.work), model="fake-model", effort="high"),
                                lambda *a, **k: Decision.DENY)
        self.addCleanup(self.provider.close, h)
        out, seen, done = [], threading.Event(), threading.Event()

        def consume():
            try:
                for event in self.provider.send(h, UserMessage("go")):
                    out.append(event)
                    if (event.kind == K.SUBAGENT_EVENT and event.data["sid"] == self.sid
                            and event.data["event"]["kind"] == "tool_started"):
                        seen.set()
            finally:
                done.set()
        threading.Thread(target=consume, daemon=True).start()
        self.assertTrue(seen.wait(20), "the child's command never started")
        self.provider.stop_subagent(h, self.sid)
        self.provider.stop_subagent(h, self.sid)
        if not done.wait(30):
            self.provider.close(h)
            self.fail("the parent's turn never finished")
        self.assertEqual(len(self.brain.calls("turn/interrupt")), 2)
        self.assertFalse([e for e in out if e.kind == K.ERROR and e.data.get("fatal")])
        ended = [e.data for e in out if e.kind == K.SUBAGENT and e.data["sid"] == self.sid
                 and e.data["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in ended], [("stopped", OWNER)])

    def test_an_answer_for_an_earlier_turn_never_touches_the_next(self):
        """#220 review, L1: `child_stops` is keyed by (sid, turn). A Stop of
        turn T1 answered after T2 began (and was Stopped) settles nothing
        on T2: a late refusal leaves T2's mark, a late take does not count
        as T2's."""
        def session(*pending):
            view = codex._ChildView(sid="cx:c", name="c", turn="T2", owner_stop=True)
            return SimpleNamespace(mutex=threading.Lock(), child_views={"c": view},
                                   child_stops={rid: ("cx:c", turn) for rid, turn in pending}), view
        refused = {"code": -32600, "message": "no active turn"}
        s, view = session((1, "T1"), (2, "T2"))
        self.assertTrue(codex.CodexProvider._child_stop_answered(s, {"id": 1, "error": refused}))
        self.assertTrue(view.owner_stop, "T1's late refusal took T2's mark off")
        s, view = session((1, "T1"), (2, "T2"))
        codex.CodexProvider._child_stop_answered(s, {"id": 1, "result": {}})
        self.assertFalse(view.owner_stop_taken, "T1's late take counted as T2's")
        codex.CodexProvider._child_stop_answered(s, {"id": 2, "error": refused})
        self.assertFalse(view.owner_stop, "T2's own refusal left the mark")

    def test_refusals_send_nothing(self):
        script, asked = codex_script()
        self.brain.script.update(mode="replay", replay=script)
        h = self.provider.start(Thread("sv4", "p", Role.CHAT, PN.CODEX),
                                Brief(Role.CHAT, str(self.work), model="fake-model", effort="high"),
                                lambda *a, **k: Decision.DENY)
        self.addCleanup(self.provider.close, h)
        for sid in ("cx:nobody", "cx:" + asked["threadId"]):        # not yet announced
            with self.subTest(sid=sid), self.assertRaises(SubagentNotLive):
                self.provider.stop_subagent(h, sid)
        self.provider.close(h)
        with self.assertRaises(SubagentNotLive):
            self.provider.stop_subagent(h, "cx:" + asked["threadId"])
        self.assertEqual(self.brain.calls("turn/interrupt"), [])


# --- the daemon and the owner's route ----------------------------------------------


class Stoppable:
    """A provider with two live Claude-shaped children, `cl:toolu_a` and
    `cl:toolu_b`. Its turn waits on `go`; a child the owner stopped then ends
    "stopped by the owner" (unless `confirm` is False), and the orchestrator
    says one more line. `mode` decides how `stop_subagent` answers."""

    def __init__(self):
        self.go = threading.Event()
        self.started = threading.Event()
        self.calls: list[str] = []
        self.interrupts = 0
        self.mode = "ok"
        self.confirm = True
        self.live = {"cl:toolu_a", "cl:toolu_b"}

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, PN.CLAUDE, "fake:" + thread.id, SimpleNamespace(closed=False))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        t = handle.thread_id
        for sid, task in (("cl:toolu_a", "ta"), ("cl:toolu_b", "tb")):
            yield Event(K.SUBAGENT, t, {"sid": sid, "op": "started", "status": "running",
                                        "task_id": task, "name": "Child " + sid[-1], "kind": "general-purpose"})
        self.started.set()
        self.go.wait(10)
        for sid in list(self.calls):
            if self.confirm and sid in self.live:
                self.live.discard(sid)
                yield Event(K.SUBAGENT, t, {"sid": sid, "op": "finished", "status": "stopped",
                                            "stop_reason": OWNER})
        yield Event(K.TEXT, t, {"text": "the orchestrator goes on"})
        yield Event(K.TURN_FINISHED, t, {"stop": "end"})

    def interrupt(self, handle):
        self.interrupts += 1

    def close(self, handle):
        pass

    def subagent_stop_refusal(self, handle, sid):
        return None if sid in self.live else "This sub-agent is not running in this chat's session."

    def stop_subagent(self, handle, sid):
        self.calls.append(sid)
        if self.mode == "unsupported":
            raise SubagentStopUnsupported("Claude Code can stop this sub-agent only by stopping the whole turn.")
        if self.mode == "notlive":
            raise SubagentNotLive("This sub-agent is not running in this chat's session.")
        if self.mode == "timeout":
            raise TimeoutError("Claude Code did not answer the stop within 10 s")
        if self.mode == "declined":
            raise SubagentStopDeclined(f"No task found with ID: ta\nkey {FAKE_SECRET} " + "x" * 400)


class Route(Daemonised):
    def setUp(self):
        super().setUp()
        self.provider = Stoppable()
        self.daemon.providers[PN.CLAUDE] = self.provider
        self.chat = self.daemon.open_thread(self.project.id, Role.CHAT, PN.CLAUDE, {})
        self.everything = self.daemon.bus.subscribe()
        self.addCleanup(self.provider.go.set)

    def path(self, sid="cl:toolu_a", thread=None):
        return f"/threads/{thread or self.chat.id}/subagents/{sid}/stop"

    def begin(self):
        self.daemon.send(self.chat.id, UserMessage(text="go"))
        self.assertTrue(self.provider.started.wait(10))
        checklib.wait_for(lambda: len(self.rows()) == 2, what="both children recorded", timeout=10.0)

    def rows(self):
        return {r["sid"]: r for r in self.owner("GET", f"/threads/{self.chat.id}/subagents")["subagents"]}

    def idle(self):
        def quiet():
            with self.daemon._lock:
                session = self.daemon._sessions.get(self.chat.id)
                return session is None or session.worker is None
        checklib.wait_for(quiet, what="the turn to finish", timeout=10.0)

    def bus(self):
        out = []
        while True:
            try:
                out.append(self.everything.get_nowait())
            except Exception:
                return out

    def test_the_route_is_the_owners_before_anything_else(self):
        self.begin()
        api, face, work = self.daemon.port, self.face, self.daemon.workshop_port
        for path in (self.path(), self.path(thread="deadbeef"), self.path("not-a-sid")):
            for port, headers in ((api, {}), (api, {"Origin": f"http://127.0.0.1:{api}"}),
                                  (api, {"Sec-Fetch-Site": "same-origin"}),
                                  (face, {}), (face, {"Sec-Fetch-Site": "same-origin"}),
                                  (face, {"Sec-Fetch-Site": "cross-site"}),
                                  (face, {"Origin": "null"}),
                                  (face, {"Origin": f"http://127.0.0.1:{work}"}),
                                  (face, {"Origin": "http://evil.example"})):
                with self.subTest(path=path, port=port, headers=headers):
                    status, payload = self.raw("POST", path, headers, port=port, body={})
                    self.assertEqual(status, 403, str(payload)[:200])
        # The workshop (preview) listener serves none of the daemon's routes.
        status, _ = self.raw("POST", self.path(), {"Origin": f"http://127.0.0.1:{work}"}, port=work, body={})
        self.assertNotEqual(status, 200)
        self.assertEqual(self.provider.calls, [], "a refused request reached the provider")
        self.assertNotIn("stop", self.rows()["cl:toolu_a"])

    def test_validation(self):
        self.begin()
        for bad in ("toolu_a", "cl:", "xx:abc", "cl:a%2Fb", "cl:" + "a" * 129):
            with self.subTest(bad=bad):
                self.owner("POST", self.path(bad), {}, status=400)
        self.owner("GET", self.path(), status=405)
        self.owner("POST", self.path(), {"sid": "cl:toolu_b"}, status=400)
        self.owner("POST", self.path() + "?now=1", {}, status=400)
        self.owner("POST", self.path(thread="deadbeef"), {}, status=404)
        self.owner("POST", self.path("cl:toolu_nobody"), {}, status=404)
        self.assertEqual(self.provider.calls, [])

    def test_one_click_stops_one_child_and_the_turn_goes_on(self):
        self.begin()
        self.bus()
        got = self.owner("POST", self.path(), {})
        self.assertEqual(got, {"ok": True, "stop": "stopping"})
        rows = self.rows()
        self.assertEqual(rows["cl:toolu_a"]["stop"], "stopping")
        self.assertTrue(rows["cl:toolu_a"]["stop_requested_at"])
        self.assertEqual(rows["cl:toolu_a"]["status"], "running")
        self.assertNotIn("stop", rows["cl:toolu_b"])
        # Pressed again while on its way: nothing more is sent.
        self.assertEqual(self.owner("POST", self.path("cl%3Atoolu_a"), {}),
                         {"ok": True, "stop": "stopping", "already": True})
        self.assertEqual(self.provider.calls, ["cl:toolu_a"])
        records = [r for r in self.bus() if r.get("kind") == "subagent"]
        self.assertTrue(any(r["data"].get("stop") == "stopping" for r in records))
        for r in records:
            self.assertLessEqual(set(r["data"]), S_BUS_KEYS, r)
            self.assertNotIn("Child", json.dumps(r))
        self.provider.go.set()
        self.idle()
        rows = self.rows()
        self.assertEqual((rows["cl:toolu_a"]["status"], rows["cl:toolu_a"]["stop_reason"]), ("stopped", OWNER))
        self.assertNotIn("stop", rows["cl:toolu_a"])
        self.assertEqual(rows["cl:toolu_b"]["status"], "running")
        self.assertEqual(self.provider.interrupts, 0, "the thread's turn was interrupted")
        log = self.daemon.stores.threads.read_log(self.chat.id)
        self.assertTrue(any(r.get("kind") == "text" and "goes on" in json.dumps(r) for r in log))
        finished = [r for r in log if r.get("kind") == "turn_finished"]
        self.assertEqual([r["data"].get("stop") for r in finished], ["end"])
        # Ended: a later press is stale, and nothing is sent.
        status, payload = self.raw("POST", self.path(), {"Origin": f"http://127.0.0.1:{self.face}"}, body={})
        self.assertEqual(status, 409)
        self.assertTrue(payload.get("stale"))
        self.assertEqual(self.provider.calls, ["cl:toolu_a"])

    def refused(self, sid="cl:toolu_a"):
        status, payload = self.raw("POST", self.path(sid), {"Origin": f"http://127.0.0.1:{self.face}"}, body={})
        self.assertEqual(status, 409, payload)
        self.assertIsInstance(payload.get("error"), str)
        self.assertTrue(payload["error"].endswith("."), payload)
        return payload

    def test_refusals_are_a_sentence_and_leave_no_mark(self):
        self.begin()
        self.provider.mode = "unsupported"
        payload = self.refused()
        self.assertEqual((payload["supported"], payload["reason"]),
                         (False, "Claude Code can stop this sub-agent only by stopping the whole turn."))
        self.assertNotIn("stop", self.rows()["cl:toolu_a"])
        self.provider.mode = "notlive"
        self.assertTrue(self.refused()["stale"])
        self.assertNotIn("stop", self.rows()["cl:toolu_a"])
        self.provider.mode = "timeout"
        payload = self.refused()
        self.assertTrue(payload["unconfirmed"])
        self.assertEqual(self.rows()["cl:toolu_a"]["stop"], "unconfirmed")
        # Unconfirmed may be pressed again: it is sent again.
        self.provider.mode = "ok"
        self.assertEqual(self.owner("POST", self.path(), {})["stop"], "stopping")
        self.assertEqual(self.provider.calls, ["cl:toolu_a"] * 4)

    def test_the_clis_refusal_is_its_own_409_and_mark(self):
        """Review of #216: the CLI's explicit refusal is neither "unconfirmed"
        nor offered again as if it timed out."""
        self.begin()
        self.bus()
        self.provider.mode = "declined"
        payload = self.refused()
        self.assertTrue(payload.get("refused"))
        self.assertNotIn("unconfirmed", payload)
        self.assertTrue(payload["error"].startswith("The CLI refused the stop: No task found with ID: ta"), payload)
        self.assertNotIn(FAKE_SECRET, json.dumps(payload))
        self.assertNotIn("\n", payload["error"])
        self.assertLessEqual(len(payload["error"]), 160)
        row = self.rows()["cl:toolu_a"]
        self.assertEqual(row["stop"], "refused")
        self.assertIn("No task found", row["stop_error"])
        self.assertNotIn(FAKE_SECRET, json.dumps(row))
        records = [r for r in self.bus() if r.get("kind") == "subagent"]
        self.assertTrue(any(r["data"].get("stop") == "refused" for r in records))
        self.assertNotIn("No task found", json.dumps(records))       # the reason never rides the bus
        for r in records:
            self.assertLessEqual(set(r["data"]), S_BUS_KEYS, r)
        self.provider.go.set()
        self.idle()

    def test_without_a_provider_stop_or_a_session(self):
        self.begin()
        del Stoppable.stop_subagent           # restored below
        try:
            payload = self.refused()
            self.assertIs(payload["supported"], False)
            self.assertIn("stop_unavailable", self.rows()["cl:toolu_a"])
            self.assertIs(self.rows()["cl:toolu_a"]["stoppable"], False)
        finally:
            Stoppable.stop_subagent = _stop_subagent
        self.provider.go.set()
        self.idle()
        self.daemon.close_thread(self.chat.id)
        self.assertTrue(self.refused("cl:toolu_b").get("stale"))

    def test_no_word_from_the_cli_is_said_as_unconfirmed(self):
        self.begin()
        self.provider.confirm = False
        records = []

        def unconfirmed():
            records.extend(self.bus())
            return self.rows()["cl:toolu_a"].get("stop") == "unconfirmed"
        with patch.object(S, "STOP_CONFIRM_S", 0.2):
            self.owner("POST", self.path(), {})
            checklib.wait_for(unconfirmed, what="unconfirmed", timeout=10.0)
        records.extend(self.bus())
        self.assertTrue(any(r.get("kind") == "subagent" and r["data"].get("stop") == "unconfirmed"
                            for r in records))
        self.assertEqual(self.rows()["cl:toolu_a"]["status"], "running", "an end was inferred from a stop")
        self.assertEqual(self.owner("POST", self.path(), {})["stop"], "stopping")
        self.assertEqual(self.provider.calls, ["cl:toolu_a", "cl:toolu_a"])

    def test_a_confirmed_stop_never_turns_unconfirmed(self):
        self.begin()
        with patch.object(S, "STOP_CONFIRM_S", 0.3):
            self.owner("POST", self.path(), {})
            self.provider.go.set()
            self.idle()
            time.sleep(0.6)
        row = self.rows()["cl:toolu_a"]
        self.assertEqual(row["status"], "stopped")
        self.assertNotIn("stop", row)

    def test_offers(self):
        self.begin()
        rows = self.rows()
        self.assertIs(rows["cl:toolu_a"]["stoppable"], True)
        self.assertNotIn("stop_unavailable", rows["cl:toolu_a"])
        self.provider.live.discard("cl:toolu_b")
        rows = self.rows()
        self.assertIs(rows["cl:toolu_b"]["stoppable"], False)
        self.assertTrue(rows["cl:toolu_b"]["stop_unavailable"].endswith("."))
        got = self.owner("GET", f"/threads/{self.chat.id}/subagents/cl:toolu_a/transcript")
        self.assertIs(got["subagent"]["stoppable"], True)
        self.assertNotIn("_key", json.dumps(got))
        self.assertNotIn("_key", json.dumps(self.rows()))


_stop_subagent = Stoppable.stop_subagent
S_BUS_KEYS = {"sid", "op", "status", "depth", "background", "started_at", "entries",
              "task_id", "parent_sid", "ended_at", "declined", "stop", "stop_requested_at"}  # never stop_error


class Record(unittest.TestCase):
    def test_a_restart_reads_a_stopping_child_back_stopped_with_no_mark(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            stores = SimpleNamespace(threads=SimpleNamespace(
                path=lambda tid: base / tid / "log.jsonl", get=lambda tid: None))
            (base / "t").mkdir()
            thread = SimpleNamespace(id="t")
            rec = S.SubagentRecords(stores, lambda record: None, lambda tid: "k1")
            prepared = S.SubagentRecords.prepare(Event(K.SUBAGENT, "t", {
                "sid": "cl:a", "op": "started", "status": "running"}), None)
            rec.record(thread, None, "k1", prepared)
            self.assertEqual(rec.begin_stop(thread, "cl:a", "k1")[0], "started")
            self.assertEqual(rec.begin_stop(thread, "cl:a", "k1")[0], "stopping")
            self.assertEqual(rec.begin_stop(thread, "cl:a", "k2")[0], "elsewhere")
            self.assertEqual(rec.list("t")[0]["stop"], "stopping")
            rec.close()
            again = S.SubagentRecords(stores, lambda record: None, lambda tid: None)
            row = again.list("t")[0]
            self.assertEqual(row["status"], "stopped")
            self.assertNotIn("stop", row)
            self.assertEqual(again.begin_stop(thread, "cl:a", None)[0], "ended")
            self.assertEqual(again.begin_stop(thread, "cl:zz", None)[0], "unknown")

    def test_nothing_is_asked_or_written_once_closed(self):
        """Review of #216: after `close()` (the daemon stopping) no Stop is
        begun and a late provider answer writes nothing."""
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            stores = SimpleNamespace(threads=SimpleNamespace(
                path=lambda tid: base / tid / "log.jsonl", get=lambda tid: None))
            (base / "t").mkdir()
            thread = SimpleNamespace(id="t")
            published = []
            rec = S.SubagentRecords(stores, published.append, lambda tid: "k1")
            for sid in ("cl:a", "cl:b"):
                rec.record(thread, None, "k1", S.SubagentRecords.prepare(Event(K.SUBAGENT, "t", {
                    "sid": sid, "op": "started", "status": "running"}), None))
            outcome, token = rec.begin_stop(thread, "cl:a", "k1")
            self.assertEqual(outcome, "started")
            rec.close()
            size, sent = (base / "t" / S.FILE).stat().st_size, len(published)
            self.assertEqual(rec.begin_stop(thread, "cl:b", "k1")[0], "closed")
            rec.stop_failed(thread, "cl:a", token, refused="late")
            rec.stop_failed(thread, "cl:a", token, unconfirmed=True)
            rec.stop_sent(thread, "cl:a", token)
            self.assertEqual(((base / "t" / S.FILE).stat().st_size, len(published)), (size, sent))
            self.assertNotIn("stop", rec.list("t")[1])

    def test_a_snapshot_refusal_is_cleaned_as_a_live_one_is(self):
        """Review of #216's delta, NIT-3: a snapshot line's `stop_error`
        (an older or hand-edited file) is one cleaned, capped line, as
        `_apply` makes a live line's."""
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            stores = SimpleNamespace(threads=SimpleNamespace(
                path=lambda tid: base / tid / "log.jsonl", get=lambda tid: None))
            (base / "t").mkdir()
            raw = "No task\nfound\u202e `x` " + "y" * 400
            line = {"kind": "snapshot", "sid": "cl:a", "at": "2026-10-15T10:00:00+00:00", "data": {
                "sid": "cl:a", "status": "running", "key": "k1", "stop": "refused", "stop_error": raw}}
            (base / "t" / S.FILE).write_text(json.dumps(line) + "\n")
            rec = S.SubagentRecords(stores, lambda record: None, lambda tid: "k1")
            row = rec.list("t")[0]
            self.assertEqual(row["stop"], "refused")
            self.assertEqual(row["stop_error"], S.clean_line(raw, S.STOP_ERROR_CAP))
            self.assertLessEqual(len(row["stop_error"]), S.STOP_ERROR_CAP)
            self.assertNotRegex(row["stop_error"], "[\n\u202e`]")
            rec.close()

    def test_a_provider_event_cannot_set_the_mark(self):
        prepared = S.SubagentRecords.prepare(Event(K.SUBAGENT, "t", {
            "sid": "cl:a", "op": "progress", "status": "running", "stop": "stopping"}), None)
        self.assertNotIn("stop", prepared["data"])


class Reach(unittest.TestCase):
    def test_only_the_owners_route_calls_it(self):
        """No session MCP tool, Discord verb, CLI command or API route calls
        `Daemon.stop_subagent`: the owner's HUD route is the only caller."""
        callers = []
        for path in (REPO / "jarvis").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\.stop_subagent\(", text):
                callers.append(str(path.relative_to(REPO)))
        self.assertEqual(sorted(set(callers)), ["jarvis/v2/hud_api.py"])
        hud = (REPO / "jarvis/v2/hud_api.py").read_text()
        self.assertEqual(hud.count("daemon.stop_subagent("), 1)
        route = hud[hud.index("def subagent_route"):hud.index("def route(")]
        self.assertLess(route.index("owner_only(handler, daemon)"), route.index("daemon.stop_subagent("))
        self.assertLess(route.index("owner_only(handler, daemon)"), route.index("handler._body()"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
