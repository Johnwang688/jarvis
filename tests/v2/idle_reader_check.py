"""SV-6: the between-turn reader.

Free and offline: the SV-0 recordings (`fixtures/subagents/`) through the real
providers (Claude's `ReplayClient`, Codex's replaying peer over real pipes),
an SDK-shaped scripted client whose buffer holds 100 messages as the SDK's
does, and real daemons on ephemeral ports with temporary state. What is
pinned, in the order it would hurt:

  - **one reader at a time, nothing read twice or lost**: a turn parks the
    idle reader before it writes or reads; what the idle reader held is read
    first by the next turn, in order; an owner turn that starts while the
    idle reader waits mid-read, or before the daemon adopted an offered CLI
    turn, still reads everything once (today's rules for the latter);
  - **between turns the stream moves**: a background child's progress and
    end reach the watcher with no send; the SDK's buffer is drained, so a
    child's hook after a flood is answered (the 2026-10-12 stall); a Codex
    child that outlives the parent's turn is read too;
  - **the CLI's own turn is a turn of the thread**: adopted by the daemon, its
    words and usage under its own `turn_id` (never the next owner turn's),
    usage counted once, every record `self_started`; Discord posts none of it
    and speech says none of it; an owner message waits for it; Stop stops it;
  - **approvals between turns** go to the session's own `permit`, deny-all
    stays deny-all, and nothing answers one by itself; a Codex child's card
    holds the reader, so a send waits for it; their gate records carry no turn;
  - **lifecycle**: close, a model switch, resume and the daemon's shutdown
    stop the idle reader and the watcher; a crashed CLI between turns is
    reported by the next message, as before; Stop of one child works idle.

Run: PYTHONPATH=. .venv/bin/python tests/v2/idle_reader_check.py
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_agent_sdk._internal.message_parser import parse_message  # noqa: E402

import claude_replay as R  # noqa: E402
import codex_provider_check as P  # noqa: E402
import codex_subagents_check as CX  # noqa: E402
import discord_mirror_check as DM  # noqa: E402
import subagent_stop_check as ST  # noqa: E402
from editor_files_check import Daemonised  # noqa: E402
from jarvis import config  # noqa: E402
from jarvis.v2 import daemon as D  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage  # noqa: E402
from jarvis.v2.providers import claude, codex, codex_config  # noqa: E402

OWNER = "stopped by the owner"
SID = "sv6-session"


def wait_for(predicate, timeout=10.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def in_thread(fn, timeout=30.0, what="the call"):
    out, errors, done = [], [], threading.Event()

    def run():
        try:
            out.append(fn())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    if not done.wait(timeout):
        raise AssertionError(f"{what} never finished")
    if errors:
        raise errors[0]
    return out[0]


class Feed:
    """A provider's `watch`, read on a thread of its own, as the daemon does."""

    def __init__(self, provider, handle):
        self.events: list[Event] = []
        self.cv = threading.Condition()
        self.ended = False

        def run():
            try:
                for event in provider.watch(handle):
                    with self.cv:
                        self.events.append(event)
                        self.cv.notify_all()
            finally:
                with self.cv:
                    self.ended = True
                    self.cv.notify_all()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, predicate, timeout=10.0, what="the feed"):
        with self.cv:
            if not self.cv.wait_for(lambda: predicate(list(self.events)), timeout):
                raise AssertionError(f"timed out waiting for {what}: "
                                     f"{[(e.kind.value, e.data) for e in self.events][-12:]}")

    def of(self, kind):
        with self.cv:
            return [e for e in self.events if e.kind == kind]

    def offers(self):
        return [e for e in self.of(K.TURN_STARTED) if e.data.get("self_started")]


def texts(events, kind=K.TEXT):
    return [e.data["text"] for e in events if e.kind == kind]


def child_texts(events):
    return [e.data["event"]["data"].get("text") for e in events
            if e.kind == K.SUBAGENT_EVENT and e.data["event"]["kind"] == "text"]


def ends(events, sid):
    return [(e.data["status"], e.data.get("stop_reason")) for e in events
            if e.kind == K.SUBAGENT and e.data["sid"] == sid and e.data["op"] == "finished"]


def injected_texts(name):
    """The orchestrator's words in each CLI turn the recording's CLI started
    by itself (a result whose origin is not human), in order."""
    out, since = [], []
    for r in R.rows(name):
        d = r["data"]
        if r["src"] != "cli":
            continue
        if d.get("type") == "assistant" and not d.get("parent_tool_use_id"):
            since += [b["text"] for b in d["message"]["content"] if b.get("type") == "text"]
        if d.get("type") == "result":
            if (d.get("origin") or {}).get("kind"):
                out.append(since)
            since = []
    return out


def results(name):
    return [r["data"] for r in R.rows(name) if r["src"] == "cli" and r["data"].get("type") == "result"]


def owner_turn_rows(n, text):
    """A further owner turn for a replay: the recorder's query, its echo, an
    answer and a result (the ReplayClient gives the echo the query's uuid)."""
    return [
        {"seq": 9000 + n, "ms": 0, "src": "harness", "data": {"action": "query", "text": text}},
        {"seq": 9001 + n, "ms": 0, "src": "cli", "data": {
            "type": "user", "message": {"role": "user", "content": text}, "session_id": SID,
            "parent_tool_use_id": None, "uuid": "00000000-0000-4000-8000-00000000000" + str(n),
            "isReplay": True}},
        {"seq": 9002 + n, "ms": 0, "src": "cli", "data": {
            "type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": f"owner answer {n}"}]},
            "parent_tool_use_id": None, "session_id": SID}},
        {"seq": 9003 + n, "ms": 0, "src": "cli", "data": {
            "type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
            "num_turns": 1, "session_id": SID, "total_cost_usd": 0.5,
            "usage": {"input_tokens": 7, "output_tokens": 3}, "result": f"owner answer {n}"}},
    ]


def before_disconnect(extra):
    """An edit: `extra` rows just before the recorder's disconnect."""
    def edit(rows):
        at = next(i for i, r in enumerate(rows) if r["src"] == "harness"
                  and r["data"].get("action") == "disconnect")
        return rows[:at] + extra + rows[at:]
    return edit


# --- Claude, the recordings through the real provider ---------------------------


class ClaudeReplay:
    """A recording played through a real ClaudeProvider that a watcher reads."""

    def __init__(self, name, *, edit=None, permit=None, client=R.ReplayClient, watch=True):
        self.stack = ExitStack()
        recorded = R.rows(name)
        R.ReplayClient.instances = []
        self.stack.enter_context(patch.object(
            claude, "_client_factory", lambda options: client(options, recorded, edit=edit)))
        self.stack.enter_context(patch.object(claude, "resolve_cli", lambda: None))
        self.permit = permit if permit is not None else R.Permit()
        self.provider = claude.ClaudeProvider()
        self.handle = self.provider.start(R.thread(), R.brief(), self.permit)
        self.session = self.handle.native
        self.feed = Feed(self.provider, self.handle) if watch else None
        if watch:
            wait_for(lambda: self.session.watch is not None, what="the watcher")

    @property
    def client(self):
        return R.ReplayClient.instances[-1]

    def send(self, text="turn"):
        return in_thread(lambda: list(self.provider.send(self.handle, UserMessage(text=text))),
                         what=f"the send {text!r}")

    def adopt(self):
        return in_thread(lambda: list(self.provider.adopt(self.handle)), what="the adopted turn")

    def idle_running(self):
        task = self.session.idle
        return task is not None and not task.done()

    def close(self):
        try:
            self.provider.close(self.handle)
        finally:
            self.stack.close()


class EditedGatedClient(ST.GatedClient):
    """`subagent_stop_check`'s gated replay, editable."""

    def __init__(self, options, recorded, edit=None):
        super().__init__(options, recorded, edit=edit)


class ClaudeRecordings(unittest.TestCase):
    ALPHA = "cl:toolu_sv0_alpha"

    def play(self, name, **kw):
        run = ClaudeReplay(name, **kw)
        self.addCleanup(run.close)
        return run

    def test_a_background_childs_end_and_the_clis_own_turn_arrive_with_no_send(self):
        run = self.play("one_default")
        first = run.send("turn 1")
        run.feed.wait(lambda ev: any(e.data.get("self_started") for e in ev if e.kind == K.TURN_STARTED),
                      what="the CLI's own turn")
        # The child's end, its words, read while the chat is idle.
        self.assertEqual(ends(run.feed.events, self.ALPHA), [("done", None)])
        self.assertIn("SV0 child alpha: finished.", child_texts(run.feed.events))
        self.assertEqual(ends(first, self.ALPHA), [])
        self.assertTrue(run.provider.cli_turn_pending(run.handle))
        cli = run.adopt()
        self.assertEqual(cli[0].kind, K.TURN_STARTED)
        self.assertEqual(cli[0].data, {"self_started": "background"})
        self.assertEqual(texts(cli), injected_texts("one_default")[0])
        self.assertEqual([e.kind for e in cli if e.kind in (K.USAGE, K.TURN_FINISHED)],
                         [K.USAGE, K.TURN_FINISHED])
        self.assertEqual(cli[-1].data["stop"], "end")
        self.assertFalse(run.provider.cli_turn_pending(run.handle))
        # Never the owner turn's words, and every child event exactly once.
        self.assertNotIn("SV0 parent: one_default step 2 done.", texts(first))
        everything = first + run.feed.events + cli
        self.assertEqual(len(ends(everything, self.ALPHA)), 1)
        self.assertEqual(child_texts(everything).count("SV0 child alpha: finished."), 1)

    def test_each_finished_background_agent_is_a_cli_turn_of_its_own(self):
        run = self.play("parallel_default")
        run.send("turn 1")
        got = []
        for n, want in enumerate(injected_texts("parallel_default"), 1):
            run.feed.wait(lambda ev, n=n: len([e for e in ev if e.kind == K.TURN_STARTED
                                               and e.data.get("self_started")]) >= n,
                          what=f"CLI turn {n}")
            cli = run.adopt()
            self.assertEqual(texts(cli), want)
            got.append(cli)
        self.assertEqual(len(got), 2)

    def test_a_cli_turns_usage_is_counted_once_under_it(self):
        run = self.play("one_default")
        first = run.send("turn 1")
        run.feed.wait(lambda ev: run.provider.cli_turn_pending(run.handle), what="the offer")
        cli = run.adopt()
        recorded = results("one_default")
        usage = [e.data for e in first + cli if e.kind == K.USAGE]
        self.assertEqual([(u["input"], u["output"]) for u in usage],
                         [(r["usage"]["input_tokens"], r["usage"]["output_tokens"]) for r in recorded])
        self.assertAlmostEqual(sum(u["cost_usd"] for u in usage), recorded[-1]["total_cost_usd"])
        self.assertEqual(run.provider.usage(run.handle).input_tokens,
                         sum(r["usage"]["input_tokens"] for r in recorded))
        self.assertEqual([e for e in run.feed.events if e.kind == K.USAGE], [])

    def test_stop_of_one_child_is_confirmed_while_idle(self):
        run = self.play("stop_bg", client=EditedGatedClient)
        child = "cl:toolu_sv0_bgsleeper"
        run.send("turn 1")
        run.provider.stop_subagent(run.handle, child)
        run.feed.wait(lambda ev: ends(ev, child), what="the stopped child")
        self.assertEqual(ends(run.feed.events, child), [("stopped", OWNER)])
        self.assertNotIn(child, run.provider.live_subagents(run.handle))
        self.assertEqual(run.client.interrupts, 0)

    def test_an_owner_send_takes_the_stream_from_an_idle_reader_mid_read(self):
        run = self.play("one_default", edit=before_disconnect(owner_turn_rows(2, "turn 2")))
        run.send("turn 1")
        run.feed.wait(lambda ev: run.provider.cli_turn_pending(run.handle), what="the offer")
        run.adopt()
        # The idle reader now waits inside the stream (the replay holds the
        # next turn's echo until its query): the send parks it there.
        wait_for(run.idle_running, what="the idle reader")
        before = len(run.feed.events)
        second = run.send("turn 2")
        self.assertEqual(texts(second), ["owner answer 2"])
        self.assertEqual(second[-1].kind, K.TURN_FINISHED)
        self.assertEqual(len(run.feed.events), before, "the idle reader read the owner's turn")
        self.assertEqual([x for x in run.session.held if x is not claude._END], [])

    def test_an_owner_send_before_the_daemon_adopts_reads_the_cli_turn_once(self):
        # The fallback: the send starts first, and `_read`'s injected rule
        # reads the CLI's turn inside it, as before SV-6. Nothing is lost or
        # read twice, and the offer is void.
        run = self.play("one_default", edit=before_disconnect(owner_turn_rows(2, "turn 2")))
        first = run.send("turn 1")
        run.feed.wait(lambda ev: run.provider.cli_turn_pending(run.handle), what="the offer")
        second = run.send("turn 2")
        self.assertFalse(run.provider.cli_turn_pending(run.handle))
        self.assertEqual(texts(first + second),
                         ["SV0 parent: one_default step 1 done.", "SV0 parent: one_default step 2 done.",
                          "owner answer 2"])
        self.assertEqual([e.data["stop"] for e in second if e.kind == K.TURN_FINISHED], ["end"])

    def test_without_a_watcher_nothing_is_read_between_turns(self):
        run = self.play("one_default", watch=False)
        run.send("turn 1")
        time.sleep(0.3)
        self.assertIsNone(run.session.idle)
        self.assertFalse(run.provider.cli_turn_pending(run.handle))
        self.assertEqual(list(run.session.held), [])


# --- Claude, an SDK-shaped scripted client -------------------------------------


def msg(raw):
    return parse_message(raw)


def assistant(text=None, *, tool=None, parent=None):
    content = [{"type": "text", "text": text}] if text is not None else []
    if tool is not None:
        content.append({"type": "tool_use", "id": tool[0], "name": tool[1], "input": tool[2]})
    return msg({"type": "assistant", "message": {"model": "m", "content": content},
                "parent_tool_use_id": parent, "session_id": SID})


def delta(text):
    return msg({"type": "stream_event", "uuid": "d", "session_id": SID, "parent_tool_use_id": None,
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def result(text="", *, origin=None, cost=0.1, tokens=(10, 2)):
    raw = {"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
           "is_error": False, "num_turns": 1, "session_id": SID, "total_cost_usd": cost,
           "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}, "result": text}
    if origin:
        raw["origin"] = {"kind": origin}
    return msg(raw)


def task(subtype, task_id, **data):
    return msg({"type": "system", "subtype": subtype, "task_id": task_id, "session_id": SID,
                "uuid": "u-" + task_id, **data})


class Q:
    """Wait until the provider has sent its query number `n` (0-based)."""

    def __init__(self, n):
        self.n = n


class Echo:
    """The CLI's echo of query `n`, under its uuid."""

    def __init__(self, n, text="echo"):
        self.n, self.text = n, text


class Gate:
    def __init__(self, event):
        self.event = event


class Hook:
    """The CLI calling the PreToolUse hook (in the SDK's reader, beside the buffer)."""

    def __init__(self, tool, args, tool_use_id, agent_id=None):
        self.tool, self.args, self.tool_use_id, self.agent_id = tool, args, tool_use_id, agent_id


class Drained:
    """Wait until the reader has taken everything put so far (so a hook that
    follows runs after the provider read what came before it)."""


class Mark:
    """The producer reached this point (a test waits for it)."""

    def __init__(self, event):
        self.event = event


class BufferedClient:
    """What the SDK is to the provider, down to its buffer: a producer (the
    SDK's own reader task) puts each message into a 100-message queue and
    runs hooks beside it, so nobody reading stalls it at the 101st, as
    `_internal/query.py` does; the provider reads the queue."""

    instances: list["BufferedClient"] = []
    BUFFER = 100

    def __init__(self, options, script):
        self.options, self.script = options, list(script)
        self.queries: list = []
        self.uids: list[str] = []
        self.hooks: list = []
        self.interrupts = 0
        self.buffer = None
        self.producer = None
        self.produced = 0
        self.done = False
        BufferedClient.instances.append(self)

    async def connect(self):
        self.buffer = asyncio.Queue(self.BUFFER)
        self.producer = asyncio.ensure_future(self._produce())

    async def disconnect(self):
        if self.producer is not None:
            self.producer.cancel()

    async def interrupt(self):
        self.interrupts += 1

    async def query(self, prompt, session_id="default"):
        items = [m async for m in prompt] if hasattr(prompt, "__aiter__") else [prompt]
        self.queries.append(items)
        for item in items:
            if isinstance(item, dict) and item.get("uuid"):
                self.uids.append(item["uuid"])

    async def _produce(self):
        for step in self.script:
            if isinstance(step, Q):
                while len(self.queries) <= step.n:
                    await asyncio.sleep(0.005)
                continue
            if isinstance(step, Echo):
                while len(self.uids) <= step.n:
                    await asyncio.sleep(0.005)
                step = msg({"type": "user", "message": {"role": "user", "content": step.text},
                            "uuid": self.uids[step.n], "parent_tool_use_id": None,
                            "session_id": SID, "isReplay": True})
            if isinstance(step, Gate):
                while not step.event.is_set():
                    await asyncio.sleep(0.005)
                continue
            if isinstance(step, Mark):
                step.event.set()
                continue
            if isinstance(step, Drained):
                while not self.buffer.empty():
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.05)
                continue
            if isinstance(step, Hook):
                data = {"hook_event_name": "PreToolUse", "tool_name": step.tool,
                        "tool_input": step.args, "tool_use_id": step.tool_use_id}
                if step.agent_id:
                    data["agent_id"] = step.agent_id
                matcher = self.options.hooks["PreToolUse"][0]
                self.hooks.append(await matcher.hooks[0](data, step.tool_use_id, {"signal": None}))
                continue
            await self.buffer.put(step)
            self.produced += 1
        self.done = True
        # The stream stays open, as a live CLI's does between turns.

    async def receive_messages(self):
        while True:
            item = await self.buffer.get()
            if isinstance(item, BaseException):
                raise item                   # the SDK's reader failed (a dead CLI)
            yield item


class Scripted:
    def __init__(self, test, script, *, permit=None, watch=True, brief=None):
        self.stack = ExitStack()
        test.addCleanup(self.close)
        BufferedClient.instances = []
        self.stack.enter_context(patch.object(
            claude, "_client_factory", lambda options: BufferedClient(options, script)))
        self.stack.enter_context(patch.object(claude, "resolve_cli", lambda: None))
        self.permit = permit if permit is not None else R.Permit()
        self.provider = claude.ClaudeProvider()
        self.handle = self.provider.start(R.thread(), brief or R.brief(), self.permit)
        self.session = self.handle.native
        self.feed = None
        if watch:
            self.watch()

    def watch(self):
        self.feed = Feed(self.provider, self.handle)
        wait_for(lambda: self.session.watch is not None, what="the watcher")

    @property
    def client(self):
        return BufferedClient.instances[-1]

    def send(self, text="turn"):
        return in_thread(lambda: list(self.provider.send(self.handle, UserMessage(text=text))),
                         what=f"the send {text!r}")

    def adopt(self):
        return in_thread(lambda: list(self.provider.adopt(self.handle)), what="the adopted turn")

    def close(self):
        try:
            self.provider.close(self.handle)
        finally:
            self.stack.close()


def turn(n, text):
    return [Q(n), Echo(n), delta(text[:3]), assistant(text), result(text)]


CHILD = [task("task_started", "t-c1", task_type="local_agent", tool_use_id="toolu_c1",
              description="checker", is_backgrounded=True)]


class ClaudeScripted(unittest.TestCase):
    C1 = "cl:toolu_c1"

    def test_the_buffer_is_drained_between_turns_so_a_late_hook_is_answered(self):
        # The 2026-10-12 stall (SV-0 `background_flood`): 300 messages after
        # the turn's result fill the SDK's 100-message buffer, and a child's
        # next hook waits behind it. Unwatched, nothing reads: it stalls.
        flood = [task("task_progress", "t-c1", tool_use_id="toolu_c1", description="checking",
                      usage={"total_tokens": i, "tool_uses": 1, "duration_ms": i}) for i in range(300)]
        script = turn(0, "one") + CHILD + flood + [
            Hook("Bash", {"command": "echo late"}, "toolu_c1_bash", agent_id="t-c1")]
        run = Scripted(self, script, watch=False)
        run.send("one")
        time.sleep(0.5)
        self.assertEqual(run.client.hooks, [], "nothing read, yet the hook ran: the stall is not reproduced")
        self.assertLess(run.client.produced, 4 + 1 + 300)
        run.watch()
        wait_for(lambda: run.client.hooks, what="the late hook")
        wait_for(lambda: run.client.done, what="the producer")
        # The child's progress reached the watcher, once each.
        progress = [e for e in run.feed.of(K.SUBAGENT) if e.data["op"] == "progress"]
        self.assertGreaterEqual(len(progress), 300)

    def test_an_approval_between_turns_is_the_threads_permit_and_deny_all_stays_deny_all(self):
        for answer in (Decision.DENY, Decision.ALLOW):
            with self.subTest(answer=answer.value):
                permit = R.Permit(answer)
                script = turn(0, "one") + CHILD + [Drained(),
                    Hook("Bash", {"command": "echo between"}, "toolu_c1_bash", agent_id="t-c1")]
                run = Scripted(self, script, permit=permit)
                run.send("one")
                wait_for(lambda: run.client.hooks, what="the hook")
                self.assertEqual(permit.calls, [("Bash", {"command": "echo between"}, "checker")])
                got = run.client.hooks[0]
                if answer is Decision.DENY:
                    self.assertEqual(got["hookSpecificOutput"]["permissionDecision"], "deny")
                else:
                    self.assertEqual(got, {}, "AUTO: an allow leaves the CLI's classifier to run")
                run.feed.wait(lambda ev: any(e.kind == K.APPROVAL_RESOLVED for e in ev), what="the gate record")
                asked = run.feed.of(K.APPROVAL_REQUESTED)
                self.assertEqual([(e.data["tool"], e.data.get("subagent")) for e in asked], [("Bash", self.C1)])
                self.assertEqual([e.data["decision"] for e in run.feed.of(K.APPROVAL_RESOLVED)], [answer.value])

    def test_a_cli_turn_nobody_sent_is_offered_and_adopted_once(self):
        script = turn(0, "one") + CHILD + [
            task("task_notification", "t-c1", tool_use_id="toolu_c1", status="completed", summary="done",
                 output_file=""),
            msg({"type": "system", "subtype": "init", "session_id": SID}),
            delta("Bac"), assistant("Background work is done."),
            result("Background work is done.", origin="task-notification", cost=0.3)]
        run = Scripted(self, script)
        first = run.send("one")
        run.feed.wait(lambda ev: run.feed.offers(), what="the offer")
        self.assertEqual(ends(run.feed.events, self.C1), [("done", None)])
        cli = run.adopt()
        self.assertEqual(texts(cli), ["Background work is done."])
        self.assertEqual(texts(cli, K.TEXT_DELTA), ["Bac"])
        self.assertEqual([round(e.data["cost_usd"], 6) for e in first + cli if e.kind == K.USAGE], [0.1, 0.2])
        self.assertEqual(len(run.feed.offers()), 1)
        self.assertEqual(run.adopt(), [], "a second adopt found a turn nobody offered")

    def test_one_reader_at_a_time_whenever_the_owner_sends(self):
        # A child talks between turns while the owner sends: every child
        # message is read exactly once, the watcher's share is a prefix of
        # what the CLI said, and the turn reads its own echo and answer.
        n = 150
        for attempt in range(6):
            with self.subTest(attempt=attempt):
                go = threading.Event()
                chatter = [assistant(f"c{i}", parent="toolu_c1") for i in range(n)]
                half = n // 3 * (attempt % 3)
                script = (turn(0, "one") + CHILD + chatter[:half] + [Gate(go)] + chatter[half:]
                          + turn(1, "two"))
                run = Scripted(self, script)
                run.send("one")
                wait_for(lambda: len(child_texts(run.feed.events)) >= half, what="the first chatter")
                go.set()
                if attempt >= 3:
                    time.sleep(0.002 * attempt)
                second = run.send("two")
                watched, turned = child_texts(run.feed.events), child_texts(second)
                self.assertEqual(watched + turned, [f"c{i}" for i in range(n)])
                self.assertEqual(texts(second), ["two"])
                self.assertEqual([e.kind for e in second if e.kind == K.TURN_FINISHED], [K.TURN_FINISHED])

    def test_close_while_idle_leaves_no_reader_and_ends_the_watch(self):
        hold = threading.Event()
        run = Scripted(self, turn(0, "one") + [Gate(hold)])
        run.send("one")
        wait_for(lambda: run.session.idle is not None and not run.session.idle.done(), what="the idle reader")
        task_ = run.session.idle
        run.provider.close(run.handle)
        self.assertTrue(task_.done(), "the idle reader outlived its session")
        self.assertIsNone(run.session.idle)
        # Told at once, not found by a poll.
        wait_for(lambda: run.feed.ended, timeout=0.5, what="the watch to end")
        self.assertFalse(run.session.runner.is_alive())

    def test_a_model_switch_while_idle_parks_the_reader_and_drops_what_the_old_cli_held(self):
        script = turn(0, "one") + [assistant("an unsent CLI turn")]
        run = Scripted(self, script)
        run.send("one")
        run.feed.wait(lambda ev: run.feed.offers(), what="the offer")
        old_task, old_client = run.session.idle, run.client
        run.provider.set_model(run.handle, "claude-other", None)
        self.assertTrue(old_task.done(), "the idle reader outlived its client")
        self.assertFalse(run.provider.cli_turn_pending(run.handle))
        self.assertEqual(list(run.session.held), [])
        self.assertIsNot(run.client, old_client)
        # The new CLI plays the same script from its start.
        second = run.send("two")
        self.assertEqual(texts(second), ["one"])
        self.assertNotIn("an unsent CLI turn", texts(second))
        wait_for(lambda: run.session.idle is not None and run.session.pump.client is run.client,
                 what="the idle reader on the new client")

    def test_a_cli_that_dies_between_turns_is_reported_by_the_next_message_as_before(self):
        run = Scripted(self, turn(0, "one"))
        run.send("one")
        wait_for(lambda: run.session.idle is not None, what="the idle reader")
        asyncio.run_coroutine_threadsafe(
            run.client.buffer.put(RuntimeError("the CLI exited")), run.session.loop).result(5)
        wait_for(lambda: run.session.idle.done(), what="the idle reader to stop")
        self.assertEqual(run.feed.of(K.ERROR), [], "reported between turns rather than by the next message")
        second = run.send("two")
        fatal = [e for e in second if e.kind == K.ERROR and e.data.get("fatal")]
        self.assertEqual(len(fatal), 1, second)
        self.assertIn("the CLI exited", fatal[0].data["message"])


# --- Claude, through a real daemon -------------------------------------------------


class PausingClient(R.ReplayClient):
    """A replay that stops before row `pause_at` until `release` is set."""

    pause_at: int | None = None
    release: threading.Event | None = None
    paused = threading.Event()

    async def receive_messages(self):
        async for item in super().receive_messages():
            yield item
            if self.pause_at is not None and self.pos >= self.pause_at:
                PausingClient.paused.set()
                while not self.release.is_set():
                    await asyncio.sleep(0.01)
                self.pause_at = None


class DaemonClaude(Daemonised):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def chat(self, name, edit=None, client=R.ReplayClient):
        recorded = R.rows(name)
        R.ReplayClient.instances = []
        self.stack.enter_context(patch.object(
            claude, "_client_factory", lambda options: client(options, recorded, edit=edit)))
        self.stack.enter_context(patch.object(claude, "resolve_cli", lambda: None))
        self.daemon.providers[PN.CLAUDE] = claude.ClaudeProvider()
        thread = self.daemon.open_thread(self.project.id, Role.CHAT, PN.CLAUDE, {})
        return thread.id

    def session(self, chat):
        with self.daemon._lock:
            return self.daemon._sessions.get(chat)

    def idle(self, chat):
        def done():
            session = self.session(chat)
            return session is None or session.worker is None
        wait_for(done, what="the turn to finish")

    def log(self, chat):
        return self.daemon.stores.threads.read_log(chat)

    def cli_finished(self, chat, n=1):
        return wait_for(lambda: len([r for r in self.log(chat) if r.get("kind") == "turn_finished"
                                     and r.get("self_started")]) >= n and self.idle_now(chat),
                        what="the CLI's own turn")

    def idle_now(self, chat):
        session = self.session(chat)
        return session is None or session.worker is None

    def test_the_clis_own_turn_is_a_turn_of_the_thread(self):
        chat = self.chat("one_default")
        bus = self.daemon.bus.subscribe()
        owner = self.daemon.send(chat, UserMessage(text="turn 1"))
        self.cli_finished(chat)
        log = self.log(chat)
        opened = [r for r in log if r.get("kind") == "cli_turn"]
        self.assertEqual(len(opened), 1)
        cli = opened[0]["turn_id"]
        self.assertNotEqual(cli, owner)
        self.assertEqual(opened[0]["data"]["text"], D.CLI_TURN_TEXT)
        of = lambda turn: [r for r in log if r.get("turn_id") == turn]  # noqa: E731
        self.assertTrue(all(r.get("self_started") is True for r in of(cli)))
        self.assertFalse(any(r.get("self_started") for r in of(owner)))
        words = lambda turn: [r["data"]["text"] for r in of(turn) if r["kind"] == "text"]  # noqa: E731
        self.assertEqual(words(cli), injected_texts("one_default")[0])
        self.assertNotIn("SV0 parent: one_default step 2 done.", words(owner))
        self.assertEqual([r["kind"] for r in of(cli)].count("usage"), 1)
        # Counted once each, under its own turn.
        ledger = [r for r in self.daemon.router.ledger._rows if r.get("thread_id") == chat]
        usage = {r["turn_id"]: r["event_id"] for r in log if r.get("kind") == "usage"}
        self.assertEqual(set(usage), {owner, cli})
        self.assertEqual(sorted(r["event_id"] for r in ledger), sorted(usage.values()))
        thread = self.daemon.stores.threads.get(chat)
        want = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in results("one_default"))
        self.assertEqual(thread.tokens, want)
        # The child's end, recorded while idle.
        got = self.owner("GET", f"/threads/{chat}/subagents")
        alpha = [r for r in got["subagents"] if r["sid"] == "cl:toolu_sv0_alpha"]
        self.assertEqual([r["status"] for r in alpha], ["done"])
        # The HUD's transcript: a system line, then the CLI's words.
        messages = self.owner("GET", f"/threads/{chat}/transcript")["messages"]
        roles = [(m["role"], m["text"]) for m in messages]
        at = roles.index(("system", D.CLI_TURN_TEXT))
        self.assertEqual(roles[at + 1], ("assistant", "SV0 parent: one_default step 2 done."))
        # On the bus: the line, and every record of the turn marked.
        seen = []
        while not bus.empty():
            seen.append(bus.get_nowait())
        # (A sub-agent's record is not the turn's words: SV-5's notices read it.)
        cli_records = [r for r in seen if r.get("turn_id") == cli and r.get("kind") != "subagent"]
        self.assertTrue(cli_records)
        self.assertTrue(all(r.get("self_started") is True for r in cli_records), cli_records)
        self.assertIn("text_delta", {r["kind"] for r in cli_records})

    def test_an_owner_message_waits_for_the_clis_turn_and_stop_stops_it(self):
        rows = R.rows("one_default")
        at = next(i for i, r in enumerate(rows) if r["src"] == "cli" and r["data"].get("type") == "assistant"
                  and "step 2" in json.dumps(r["data"]))
        PausingClient.pause_at, PausingClient.release = at, threading.Event()
        PausingClient.paused = threading.Event()
        self.addCleanup(PausingClient.release.set)
        chat = self.chat("one_default", client=PausingClient,
                         edit=before_disconnect(owner_turn_rows(2, "turn 2")))
        self.daemon.send(chat, UserMessage(text="turn 1"))
        wait_for(lambda: (self.session(chat).self_started and PausingClient.paused.is_set()),
                 what="the CLI's own turn to run")
        reply = self.daemon.deliver(chat, UserMessage(text="turn 2"))
        self.assertEqual(reply["status"], "queued")
        with self.assertRaises(D.DaemonError):
            self.daemon.send(chat, UserMessage(text="a runner's"))
        cli = self.session(chat).turn_id
        self.assertEqual(self.daemon.interrupt(chat, expected_turn_id=cli), {"ok": True})
        PausingClient.release.set()
        wait_for(lambda: any(r.get("kind") == "turn_finished" and r.get("turn_id") == cli
                             for r in self.log(chat)), what="the stopped CLI turn")
        log = self.log(chat)
        end = next(r for r in log if r.get("kind") == "turn_finished" and r.get("turn_id") == cli)
        self.assertEqual((end["data"]["stop"], end.get("self_started")), ("interrupted", True))
        self.assertGreaterEqual(R.ReplayClient.instances[-1].interrupts, 1)
        # Stop drops what waited, as for any turn (stop means stop).
        self.assertTrue(any(r.get("kind") == "queued_dropped" for r in log))

    def test_a_queued_owner_message_runs_after_the_clis_turn(self):
        rows = R.rows("one_default")
        at = next(i for i, r in enumerate(rows) if r["src"] == "cli" and r["data"].get("type") == "assistant"
                  and "step 2" in json.dumps(r["data"]))
        PausingClient.pause_at, PausingClient.release = at, threading.Event()
        PausingClient.paused = threading.Event()
        self.addCleanup(PausingClient.release.set)
        chat = self.chat("one_default", client=PausingClient,
                         edit=before_disconnect(owner_turn_rows(2, "turn 2")))
        self.daemon.send(chat, UserMessage(text="turn 1"))
        wait_for(lambda: self.session(chat).self_started and PausingClient.paused.is_set(),
                 what="the CLI's own turn to run")
        queued = self.daemon.deliver(chat, UserMessage(text="turn 2"))
        PausingClient.release.set()
        wait_for(lambda: any(r.get("kind") == "text" and r["data"]["text"] == "owner answer 2"
                             for r in self.log(chat)), what="the queued message's answer")
        self.idle(chat)
        log = self.log(chat)
        kinds = [(r.get("kind"), r.get("turn_id")) for r in log]
        cli_end = kinds.index(("turn_finished", next(r["turn_id"] for r in log if r.get("kind") == "cli_turn")))
        started = kinds.index(("queued_started", queued["queued_turn_id"]))
        self.assertLess(cli_end, started)
        answer = next(r for r in log if r.get("kind") == "text" and r["data"]["text"] == "owner answer 2")
        self.assertEqual(answer["turn_id"], queued["queued_turn_id"])
        self.assertFalse(answer.get("self_started"))

    def test_close_resume_and_shutdown_end_the_watcher(self):
        chat = self.chat("one_default")
        self.daemon.send(chat, UserMessage(text="turn 1"))
        self.cli_finished(chat)
        session = self.session(chat)
        watcher = session.watcher
        self.assertTrue(watcher.is_alive())
        self.daemon.close_thread(chat)
        watcher.join(3)
        self.assertFalse(watcher.is_alive(), "the watcher outlived its session")
        idle = session.handle.native.idle
        self.assertTrue(idle is None or idle.done())
        resumed = self.daemon.resume_thread(chat)
        self.assertIsNot(resumed, session)
        self.assertTrue(resumed.watcher.is_alive())
        self.daemon.stop()
        resumed.watcher.join(3)
        self.assertFalse(resumed.watcher.is_alive(), "shutdown left a watcher running")


# --- the daemon's side, with a provider that watches ----------------------------------


class Watching:
    """A provider whose `watch` the test feeds, with a CLI turn to adopt."""

    name = PN.CLAUDE

    def __init__(self):
        self.feeds: list = []
        self.offer = None
        self.adopted: list = []
        self.started = 0
        self.closed = []
        self.cli_events: list = []
        self.release = threading.Event()
        self.release.set()
        self.cli_release = threading.Event()
        self.cli_release.set()

    def health(self):
        return True, "watching"

    def start(self, thread, brief, permit):
        self.started += 1
        feed = __import__("queue").Queue()
        self.feeds.append(feed)
        return SessionHandle(thread.id, PN.CLAUDE, "w:" + thread.id,
                             SimpleNamespace(brief=brief, closed=False, feed=feed))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        yield Event(K.TURN_STARTED, handle.thread_id)
        self.release.wait(5)
        yield Event(K.TEXT, handle.thread_id, {"text": f"answer to {message.text}"})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def watch(self, handle):
        feed = handle.native.feed
        while True:
            item = feed.get()
            if item is None:
                return
            yield item

    def cli_turn_pending(self, handle):
        return self.offer is not None

    def adopt(self, handle):
        origin, self.offer = self.offer, None
        if origin is None:
            return
        self.adopted.append(origin)
        yield Event(K.TURN_STARTED, handle.thread_id, {"self_started": origin})
        self.cli_release.wait(5)
        yield Event(K.TEXT, handle.thread_id, {"text": "the CLI's own words"})
        yield Event(K.USAGE, handle.thread_id, {"input": 5, "output": 1, "cached": 0, "cost_usd": None})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        handle.native.closed = True
        handle.native.feed.put(None)
        self.closed.append(handle.thread_id)


class DaemonWatch(Daemonised):
    def setUp(self):
        super().setUp()
        self.provider = Watching()
        self.daemon.providers[PN.CLAUDE] = self.provider
        self.chat = self.daemon.open_thread(self.project.id, Role.CHAT, PN.CLAUDE, {}).id
        self.feed = self.provider.feeds[-1]

    def session(self):
        with self.daemon._lock:
            return self.daemon._sessions.get(self.chat)

    def log(self):
        return self.daemon.stores.threads.read_log(self.chat)

    def offer(self):
        self.provider.offer = "task-notification"
        self.feed.put(Event(K.TURN_STARTED, self.chat, {"self_started": "task-notification"}))

    def test_a_gate_question_between_turns_is_logged_with_no_turn(self):
        self.daemon.send(self.chat, UserMessage(text="one"))
        wait_for(lambda: self.session().worker is None, what="the turn")
        self.feed.put(Event(K.APPROVAL_REQUESTED, self.chat, {"req_id": "r1", "tool": "Bash",
                                                              "args": {"command": "echo hi"},
                                                              "command": "echo hi", "subagent": "cl:x"}))
        self.feed.put(Event(K.APPROVAL_RESOLVED, self.chat, {"req_id": "r1", "decision": "deny"}))
        rows = wait_for(lambda: [r for r in self.log() if r.get("kind") in ("gate_requested", "gate_resolved")],
                        what="the gate records")
        wait_for(lambda: len([r for r in self.log() if r.get("kind") in ("gate_requested", "gate_resolved")]) == 2)
        rows = [r for r in self.log() if r.get("kind") in ("gate_requested", "gate_resolved")]
        self.assertEqual([r.get("turn_id") for r in rows], [None, None])

    def test_a_sub_agents_event_between_turns_reaches_its_record_not_the_log(self):
        self.daemon.send(self.chat, UserMessage(text="one"))
        wait_for(lambda: self.session().worker is None, what="the turn")
        before = len(self.log())
        self.feed.put(Event(K.SUBAGENT, self.chat, {"sid": "cl:toolu_bg", "op": "started", "status": "backgrounded",
                                                    "depth": 1, "background": True, "name": "bg"}))
        self.feed.put(Event(K.SUBAGENT, self.chat, {"sid": "cl:toolu_bg", "op": "finished", "status": "done",
                                                    "depth": 1, "background": True, "name": "bg"}))
        got = wait_for(lambda: [r for r in self.owner("GET", f"/threads/{self.chat}/subagents")["subagents"]
                                if r["status"] == "done"], what="the record")
        self.assertEqual([r["sid"] for r in got], ["cl:toolu_bg"])
        self.assertEqual(len(self.log()), before)

    def test_an_offer_while_a_turn_runs_goes_first_when_it_ends(self):
        self.provider.release.clear()
        self.daemon.send(self.chat, UserMessage(text="one"))
        wait_for(lambda: self.session().worker is not None, what="the turn")
        self.offer()
        time.sleep(0.2)
        self.assertEqual(self.provider.adopted, [], "adopted while the owner's turn ran")
        reply = self.daemon.deliver(self.chat, UserMessage(text="two"))
        self.assertEqual(reply["status"], "steered" if reply["status"] == "steered" else "queued")
        self.provider.release.set()
        wait_for(lambda: len([r for r in self.log() if r.get("kind") == "turn_finished"]) == 3,
                 what="three turns")
        wait_for(lambda: self.session().worker is None, what="the queue to drain")
        kinds = [(r["kind"], r.get("self_started", False)) for r in self.log()
                 if r["kind"] in ("cli_turn", "queued_started", "turn_finished")]
        self.assertEqual(kinds, [("turn_finished", False), ("cli_turn", True), ("turn_finished", True),
                                 ("queued_started", False), ("turn_finished", False)])

    def test_an_offer_heard_while_a_turn_runs_is_adopted_when_it_ends(self):
        self.provider.release.clear()
        self.daemon.send(self.chat, UserMessage(text="one"))
        wait_for(lambda: self.session().worker is not None, what="the turn")
        self.offer()
        wait_for(lambda: self.session().cli_offered, what="the watcher to pass the offer on")
        self.provider.release.set()
        wait_for(lambda: any(r.get("kind") == "turn_finished" and r.get("self_started") for r in self.log()),
                 what="the CLI turn, with no owner message to prompt it")

    def test_an_offer_with_nothing_running_is_adopted_at_once(self):
        self.offer()
        wait_for(lambda: any(r.get("kind") == "turn_finished" for r in self.log()), what="the CLI turn")
        log = self.log()
        self.assertEqual([r["kind"] for r in log][:2], ["cli_turn", "turn_started"])
        self.assertTrue(all(r.get("self_started") for r in log if r.get("turn_id")))
        thread = self.daemon.stores.threads.get(self.chat)
        self.assertEqual(thread.tokens, 6)

    def test_an_owner_message_meeting_an_unadopted_offer_waits_for_it(self):
        self.provider.offer = "task-notification"       # the watcher has not told the daemon yet
        reply = self.daemon.deliver(self.chat, UserMessage(text="hello"))
        self.assertEqual(reply["status"], "queued")
        wait_for(lambda: len([r for r in self.log() if r.get("kind") == "turn_finished"]) == 2, what="both turns")
        texts_ = [r["data"]["text"] for r in self.log() if r.get("kind") == "text"]
        self.assertEqual(texts_, ["the CLI's own words", "answer to hello"])

    def test_errors_between_turns_are_lines_and_a_fatal_one_drops_the_session(self):
        self.feed.put(Event(K.ERROR, self.chat, {"message": "a child's question was refused", "fatal": False}))
        wait_for(lambda: any(r.get("kind") == "notice" for r in self.log()), what="the note")
        self.assertIsNotNone(self.session())
        self.feed.put(Event(K.ERROR, self.chat, {"message": "identity mismatch", "fatal": True}))
        wait_for(lambda: self.session() is None, what="the session to drop")
        notes = [r for r in self.log() if r.get("kind") == "notice"]
        self.assertEqual([(r["data"]["fatal"], r.get("turn_id")) for r in notes], [(False, None), (True, None)])
        self.assertEqual(self.provider.closed, [self.chat])
        self.daemon.send(self.chat, UserMessage(text="again"))
        wait_for(lambda: self.provider.started == 2, what="the resume")
        messages = self.owner("GET", f"/threads/{self.chat}/transcript")["messages"]
        self.assertIn(("system", "Between turns: a child's question was refused"),
                      [(m["role"], m["text"]) for m in messages])


    def test_a_runners_send_meets_the_clis_own_turn_by_name(self):
        from jarvis.v2 import runner
        self.provider.cli_release.clear()
        self.addCleanup(self.provider.cli_release.set)
        self.offer()
        wait_for(lambda: self.session().self_started, what="the CLI turn")
        with self.assertRaises(D.DaemonError) as caught:
            self.daemon.send(self.chat, UserMessage(text="a task's"))
        self.assertIn(runner.CLI_TURN_RUNNING, str(caught.exception))
        self.assertIn("already running", str(caught.exception))


class RunnerSkips(unittest.TestCase):
    def test_a_cli_turns_records_are_never_a_tasks_turn(self):
        from jarvis.v2 import runner
        self.assertTrue(runner._for_turn({"thread_id": "t", "kind": "text"}, "t"))
        self.assertFalse(runner._for_turn({"thread_id": "t", "kind": "text", "self_started": True}, "t"))
        self.assertFalse(runner._for_turn({"thread_id": "t", "kind": "turn_finished", "self_started": True}, "t"))
        self.assertFalse(runner._for_turn({"thread_id": "t", "kind": "text_delta"}, "t"))
        self.assertFalse(runner._for_turn({"thread_id": "u", "kind": "text"}, "t"))
        self.assertTrue(runner._for_turn({"kind": "shutdown"}, "t"))

    def test_the_runner_waits_out_the_clis_own_turn_and_not_another(self):
        from jarvis.v2 import runner
        for own, waits in ((" (the CLI's own)", True), ("", False)):
            with self.subTest(own=own):
                until = time.monotonic() + 0.4
                calls = []

                def send(thread_id, message, own=own, until=until):
                    calls.append(time.monotonic())
                    if time.monotonic() < until:
                        raise D.DaemonError("a turn is already running on this thread" + own)
                    return "turn-x"
                r = runner.TaskRunner.__new__(runner.TaskRunner)
                r.daemon, r.turn_timeout = SimpleNamespace(send=send), 60
                with patch.object(runner, "SEND_TIMEOUT", 0.1):
                    if waits:
                        self.assertEqual(r._send("t", UserMessage(text="x")), "turn-x")
                    else:
                        with self.assertRaises(D.DaemonError):
                            r._send("t", UserMessage(text="x"))


# --- Discord and speech --------------------------------------------------------------


class SelfStarting(DM.Scripted):
    """The mirror suite's scripted provider, with a CLI that starts a turn."""

    def __init__(self):
        super().__init__()
        self.feeds = []
        self.offer = None

    def start(self, thread, brief, permit):
        handle = super().start(thread, brief, permit)
        feed = __import__("queue").Queue()
        self.feeds.append(feed)
        handle.native = SimpleNamespace(feed=feed)
        return handle

    resume = start

    def watch(self, handle):
        while True:
            item = handle.native.feed.get()
            if item is None:
                return
            yield item

    def cli_turn_pending(self, handle):
        return self.offer is not None

    def adopt(self, handle):
        origin, self.offer = self.offer, None
        if origin is None:
            return
        yield Event(K.TURN_STARTED, handle.thread_id, {"self_started": origin})
        yield Event(K.TEXT, handle.thread_id, {"text": "SELF-STARTED words nobody asked for"})
        yield Event(K.TOOL_STARTED, handle.thread_id, {"call_id": "c", "name": "Bash", "args": {}})
        yield Event(K.TOOL_FINISHED, handle.thread_id, {"call_id": "c", "name": "Bash", "ok": True, "summary": ""})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def close(self, handle):
        handle.native.feed.put(None)


class MirrorSkips(DM.Harness):
    def setUp(self):
        super().setUp()
        self.provider.__class__ = SelfStarting
        SelfStarting.__init__(self.provider)

    def test_discord_posts_nothing_of_a_cli_turn_and_keeps_mirroring(self):
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        self.idle(chat.id)
        self.settled(chat.id)
        self.provider.offer = "task-notification"
        self.provider.feeds[-1].put(Event(K.TURN_STARTED, chat.id, {"self_started": "task-notification"}))
        wait_for(lambda: any(r.get("kind") == "turn_finished" and r.get("self_started")
                             for r in self.stores.threads.read_log(chat.id)), what="the CLI turn")
        self.idle(chat.id)
        self.hud_send(chat, "again")
        self.idle(chat.id)
        self.settled(chat.id)
        posted = json.dumps([c for c in self.transport.calls])
        self.assertNotIn("SELF-STARTED", posted)
        self.assertNotIn("Bash", posted, "a CLI turn's tool reached a footer")
        self.assertEqual(posted.count("the answer"), 2, "the owner's replies around it were not both posted")
        self.assertEqual(self.spoken, [])


# --- Codex, the recordings through the real provider over pipes ------------------------


def leftovers(name):
    """`name`'s parent turn followed by what Codex sent after it (children
    that outlived the turn), all in turn 1's script: what follows the
    parent's `turn/completed` arrives between turns."""
    first, *rest = CX.turns_of(name)
    tail = rest[0][:-3] if rest else []
    return first + json.loads(json.dumps(tail).replace("turn-1", "$TURN"))


def short_turn(name):
    return CX.turns_of(name)[1][-3:]


class CodexRun:
    def __init__(self, test, script, *, permit=None):
        self.stack = ExitStack()
        test.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="sv6-")))
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
        self.brain.script.update(mode="replay", replays=script)
        self.provider = codex.CodexProvider()
        self.permit = permit or (lambda *a, **k: Decision.DENY)
        self.handle = self.provider.start(Thread("sv6", "p", Role.CHAT, PN.CODEX),
                                          Brief(Role.CHAT, str(work), model="fake-model", effort="high"),
                                          self.permit)
        self.stack.callback(self.provider.close, self.handle)
        self.feed = Feed(self.provider, self.handle)

    def send(self, text="go", timeout=30):
        return in_thread(lambda: list(self.provider.send(self.handle, UserMessage(text))),
                         timeout=timeout, what=f"the send {text!r}")


class CodexIdle(unittest.TestCase):
    def child(self):
        return next(iter(CX.children("no_wait")))

    def test_a_child_that_outlives_the_turn_is_read_with_no_send(self):
        run = CodexRun(self, [leftovers("no_wait"), short_turn("no_wait")])
        first = run.send("one")
        sid = "cx:" + self.child()
        run.feed.wait(lambda ev: ends(ev, sid), what="the child's end")
        self.assertEqual(ends(run.feed.events, sid), [("done", None)])
        self.assertEqual(ends(first, sid), [])
        second = run.send("two")
        self.assertEqual([e for e in second if e.kind in (K.SUBAGENT, K.SUBAGENT_EVENT)], [])
        self.assertEqual(second[-1].kind, K.TURN_FINISHED)
        self.assertNotIn(sid, run.provider.live_subagents(run.handle))

    def between(self, extra):
        """`no_wait` with `extra` (the child's) right after the parent's turn."""
        child = self.child()
        script = leftovers("no_wait")
        at = next(i for i, m in enumerate(script) if m.get("method") == "turn/completed"
                  and m["params"]["threadId"] == "$THREAD") + 1
        script[at:at] = json.loads(json.dumps(extra).replace("$CHILD", child))
        return script

    APPROVAL = [
        {"method": "item/started", "params": {"threadId": "$CHILD", "turnId": "ct", "item": {
            "id": "cmd-between", "type": "commandExecution", "command": "echo between", "cwd": "$CWD",
            "status": "inProgress"}}},
        {"id": "ap-between", "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "$CHILD", "turnId": "ct", "itemId": "cmd-between", "command": "echo between",
            "cwd": "$CWD"}},
    ]

    def test_a_childs_approval_between_turns_asks_the_permit_and_nothing_else_answers(self):
        for answer, want in ((Decision.DENY, "decline"), (Decision.ALLOW, "accept")):
            with self.subTest(answer=answer.value):
                calls = []

                def permit(tool, args, brief, **kw):
                    calls.append((tool, args.get("command"), kw.get("widening")))
                    return answer
                run = CodexRun(self, [self.between(self.APPROVAL), short_turn("no_wait")], permit=permit)
                run.send("one")
                run.feed.wait(lambda ev: any(e.kind == K.APPROVAL_RESOLVED for e in ev), what="the decision")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:2], ("shell", "echo between"))
                self.assertIn("Codex sub-agent", calls[0][2], "a child's approval is always a widening")
                replies = [r for r in run.brain.rpcs[-1].replies if r["id"] == "ap-between"]
                self.assertEqual([r["result"]["decision"] for r in replies], [want])
                asked = run.feed.of(K.APPROVAL_REQUESTED)
                self.assertEqual([e.data.get("subagent") for e in asked], ["cx:" + self.child()])

    def test_a_send_waits_for_a_card_the_idle_drain_holds(self):
        release = threading.Event()
        asked = threading.Event()

        def permit(tool, args, brief, **kw):
            asked.set()
            release.wait(20)
            return Decision.DENY
        run = CodexRun(self, [self.between(self.APPROVAL), short_turn("no_wait")], permit=permit)
        run.send("one")
        self.assertTrue(asked.wait(10), "the child's card was never asked")
        done = threading.Event()
        out = []
        threading.Thread(target=lambda: (out.extend(run.provider.send(run.handle, UserMessage("two"))),
                                         done.set()), daemon=True).start()
        time.sleep(0.5)
        self.assertEqual(len(run.brain.calls("turn/start")), 1, "a send read beside the idle drain")
        self.assertFalse(done.is_set())
        release.set()
        self.assertTrue(done.wait(20), "the send never ran")
        self.assertEqual(out[-1].kind, K.TURN_FINISHED)
        self.assertEqual(len(run.brain.calls("turn/start")), 2)

    def test_stop_of_one_child_between_turns(self):
        child = self.child()
        script = leftovers("no_wait")
        # Where the child's turn ends, the peer instead waits for our Stop,
        # answers it, and the child's turn ends interrupted.
        at = next(i for i, m in enumerate(script) if m.get("method") == "turn/completed"
                  and m["params"]["threadId"] == child)
        turn_id = script[at]["params"]["turn"]["id"]
        ended = copy.deepcopy(script[at])
        ended["params"]["turn"]["status"] = "interrupted"
        parent_done = next(i for i, m in enumerate(script) if m.get("method") == "turn/completed"
                           and m["params"]["threadId"] == "$THREAD")
        self.assertLess(parent_done, at, "the recording's child does not outlive the parent's turn")
        script[at:at + 1] = [{"$WAIT": "turn/interrupt", "thread": child, "turn": turn_id}, ended]
        run = CodexRun(self, [script, short_turn("no_wait")])
        run.send("one")
        sid = "cx:" + child
        wait_for(lambda: run.provider.subagent_stop_refusal(run.handle, sid) is None, what="a live child")
        run.provider.stop_subagent(run.handle, sid)
        run.feed.wait(lambda ev: ends(ev, sid), what="the stopped child")
        self.assertEqual(ends(run.feed.events, sid), [("stopped", OWNER)])

    def test_an_unknown_threads_request_between_turns_is_fatal_as_in_a_turn(self):
        stray = [{"id": "stray", "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "01a140cb-0000-7000-8000-unknownthread", "turnId": "x", "itemId": "i",
            "command": "echo hi"}}]
        run = CodexRun(self, [self.between(stray)])
        run.send("one")
        run.feed.wait(lambda ev: any(e.kind == K.ERROR and e.data.get("fatal") for e in ev), what="the fatal error")
        self.assertTrue(run.handle.native.closed.is_set())
        wait_for(lambda: run.feed.ended, what="the watch to end")

    def test_close_ends_the_watch_and_the_transport_failing_idle_is_the_next_sends(self):
        run = CodexRun(self, [leftovers("no_wait"), short_turn("no_wait")])
        run.send("one")
        sid = "cx:" + self.child()
        run.feed.wait(lambda ev: ends(ev, sid), what="the child's end")
        run.handle.native.rpc.process.kill()
        wait_for(lambda: run.feed.ended, what="the watch to stop on a dead transport")
        self.assertEqual(run.feed.of(K.ERROR), [], "the idle drain reported it instead of the next send")
        second = run.send("two")
        self.assertTrue(any(e.kind == K.ERROR and e.data.get("fatal") for e in second), second)

        other = CodexRun(self, [leftovers("no_wait"), short_turn("no_wait")])
        other.send("one")
        other.provider.close(other.handle)
        wait_for(lambda: other.feed.ended, timeout=3, what="the watch to end on close")


if __name__ == "__main__":
    unittest.main(verbosity=2)
