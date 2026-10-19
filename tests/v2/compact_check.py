"""WP-A of the HUD Compact button: the daemon, the owner-only route, and the
records, queues and Stop around a compaction.

Plan `docs/plans/2026-10-19-compact-button-plan.md` §1-§4 and §6 WP-A; the
owner answers CB-1..CB-5 at its end win over the plan (CB-1: Stop during a
compaction drops the owner's queued messages too, as Stop does for any turn;
CB-5: nothing is posted to Discord). The fast-path cases are in
tests/v2/fastpath_check.py and tests/context_check.py, the Claude ones in
tests/v2/claude_provider_check.py ("compact" groups).

Free and hermetic: every daemon runs on port 0 (three ephemeral listeners)
over a temporary V2_DATA_DIR and temporary config, never 8402/8403/8405, never
the owner's state, never a real ~/.claude or ~/.codex. The chats are scripted
fake providers (chat_relay_check's `Fake`, which steers, holds and records,
plus a `compact` of its own here) registered as `fast`, `claude` and `codex`;
the `codex` one has no `compact` at all. One case runs the real
FastPathProvider with `llm.chat` scripted. Credential-shaped strings are
built at runtime. The owner-only cases use the real daemon's two listeners:
the API listener (`daemon.port`) and the HUD listener (`daemon.face_port`).

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/compact_check.py

Each case is its own unittest method: on today's code (no `Daemon.compact`)
each fails alone and the runner goes on; the summary line counts them.


THE INTERFACE THE IMPLEMENTER PROVIDES (from the plan; where the plan is
ambiguous the choice made here is marked "chosen")
==============================================================================

Provider protocol (`jarvis/v2/provider.py`, the comment block, after `adopt`;
both optional):

    compact_refusal(h) -> str | None
        Cheap, sends nothing. None when `compact(h)` would run now, else one
        plain sentence (the daemon answers 409 with exactly that sentence).
    compact(h) -> Iterator[Event]
        A turn with no message: TURN_STARTED; a TOOL_STARTED/TOOL_FINISHED
        pair with `name: "compact"`; any USAGE; then TURN_FINISHED
        {"stop": "end"|"error"|"interrupted", "compacted": bool,
         "reason": str | None}. `interrupt(h)` ends it.

Daemon (`jarvis/v2/daemon.py`):

    _Session.compacting: bool = False
    Daemon.compact(thread_id) -> {"status": "started", "turn_id": str}
        `_live_session` first (archived refused; a closed session resumes,
        calling no model). Then, under `_lock`, `DaemonError` (HTTP 409) when:
          - `_sendable` refuses (closing, archived, a stop pending);
          - not a chat or it has a task_id:
                "Tasks are not chats; only a chat compacts."
          - a turn runs (`worker` set), a compaction included:
                "A turn is running; compact when it ends."
          - owner messages or chat_relay items wait on it:
                "Messages are waiting to run first."
          - `_maybe_cli(session, eager=True)` started the CLI's own turn
                (chosen: any sentence; not pinned);
          - `_waiting_on_owner(session)` (a pending approval card or an open
                question; chosen: any sentence; not pinned);
          - the provider has no `compact`:
                "This chat's provider cannot compact its conversation."
          - `compact_refusal(h)` returned a sentence: that sentence, as is.
        In none of these is the provider's `compact` called.
    `_begin_compact` (modelled on `_begin_cli`): clears cancelled /
        owner_stopped / steered / questions, mints `turn_id`, sets
        `compacting = True`, clears the relay fields, `_retire_drive`; logs and
        publishes
            {"kind": "compact_started", "compaction": true, "turn_id",
             "thread_id", "project_id", "at", ...}
        then runs `_turn(session, None, compact=True)` with
        `source = provider.compact(handle)`. No desk, no `_apply_choice`, no
        DriveTurn, no `user` record, no `user_message` event.
    While `compacting`, `_record` stamps every record (log and bus)
        `"compaction": true`: turn_started, tool_started/finished, usage,
        turn_finished. `_finish` does not add to `thread.turns`; usage and
        cost still count (thread.cost_usd and the ledger).
    `_turn`'s finally, under the lock: `compacting` is cleared with `worker`;
        then it logs and publishes
            {"kind": "compacted", "compaction": true, "turn_id", "thread_id",
             "project_id", "at",
             "data": {"ok": bool, "text": str}}
        where (chosen) ok = stop == "end" and compacted is True, and text is
            "Conversation compacted"                    when ok,
            "Compaction stopped"                        when stop == "interrupted",
            "Compaction failed: " + clean_line(scrub_text(reason), 200)  otherwise
        (`approvals.clean_line`, `redact.scrub_text`, as `_idle_error`). Then
        it drains as usual: the owner's queue first, then relays, then held
        replies. The `compacted` record comes after the turn's turn_finished.
    `thread_json` gains a live `compacting` bool, always present (False when
        idle), beside `running`.
    `deliver` queues (never steers) while `compacting`; `chat_relay._hand_over`
        never steers a reply into a compacting sender (`and not
        session.compacting`): the reply is held and runs as a reply turn after.
    Stop is `interrupt`, unchanged: the turn ends `interrupted`, and the
        owner's queued messages are dropped (`queued_dropped` per message, one
        `queue_cleared` with reason "stopped" naming them) (CB-1).

Route (`jarvis/v2/hud_api.route`, beside relays/withdraw):
    POST /threads/{id}/compact
        `projects.owner_only(handler, daemon)` FIRST: before the thread is
        looked up and before any body is read (an API-listener POST with a
        malformed body or an unknown thread id is 403, never 400/404).
        Then an empty query and an empty body ({} or none) -> 202 with
        `daemon.compact(id)`. A body with any field, or a query, -> 400.
        Any other method -> 405 (chosen: asked by the owner on the HUD listener).
    No MCP tool, no bridge route, no Discord command, no CLI verb.

Consumers:
    runner._for_turn skips records marked `compaction`; the Discord mirror
    posts nothing of a compaction turn (CB-5); the titler only reads
    `user`/`user_message`, which a compaction never makes; activity marks a
    compaction seen (a compaction never makes a chat unread);
    `"compacted"` joins `chat_reads._SYSTEM_KINDS` (a system row); the HUD
    transcript (`GET /threads/{id}/transcript`) shows `compacted` as
    {"role": "system", "text": ...}, makes no row of `compact_started`, and
    with `tools=1` shows the `compact` tool row of that turn.


TEST -> MUTATION (plan §6 WP-A negatives 1-11, CB-1, and the extras)
=====================================================================
 1 owner_only removed / after lookup / after the body is read
       RouteOwnerOnly.test_api_listener_is_refused_before_anything_is_read
 1 the route itself (shape, 202/400/405)
       RouteOwnerOnly.test_hud_listener_starts_it_and_the_route_shape_holds
 2 a tool/bridge/MCP/Discord path to compaction; Daemon.compact reached from
   anywhere but the owner route; compact_context leaves FORBIDDEN_TOOLS
       SourceGuard.test_only_the_owner_route_reaches_daemon_compact
       SourceGuard.test_no_tool_bridge_mcp_or_discord_file_names_compact  (pin)
       SourceGuard.test_compact_context_stays_forbidden_on_the_fast_path  (pin)
 3 worker check removed
       Refusals.test_a_running_turn_is_refused_and_compact_is_never_called,
       Refusals.test_a_second_compaction_is_refused_while_one_runs
 4 `_waiting_on_owner` check removed
       Refusals.test_a_pending_approval_card_is_refused
 5 `_sendable` / chat-only checks removed
       Refusals.test_an_archived_thread_is_refused,
       Refusals.test_a_task_thread_is_refused (role and task_id),
       Refusals.test_a_closing_session_is_refused
   queue / relay-pending check removed
       Refusals.test_waiting_owner_messages_are_refused,
       Refusals.test_waiting_relays_are_refused
   `_maybe_cli(eager=True)` check removed
       Refusals.test_a_cli_turn_waiting_runs_first_and_compaction_is_refused
 6 Stop keeps the queue (CB-1) / Stop text wrong
       Running.test_stop_ends_it_interrupted_and_drops_the_owners_queue
 7 deliver steers during compaction
       Running.test_an_owner_message_during_compaction_is_queued_never_steered
 8 relay steers during compaction / relays jump the owner's queue
       Running.test_relays_and_replies_to_a_compacting_chat_wait_then_run_in_order
 9 a provider without compact is called / refusal sentence not used
       Refusals.test_a_provider_without_compact_is_refused_with_the_sentence,
       Refusals.test_compact_refusal_sentence_is_the_409
10 records unmarked / counted as a turn / usage dropped / compacting flag
       Records.test_every_record_of_the_compaction_is_marked_and_ordered,
       Records.test_a_compaction_is_not_a_turn_but_its_cost_counts,
       Records.test_thread_json_says_compacting_while_it_runs,
       Records.test_the_runner_skips_compaction_records,
       Records.test_no_compaction_record_is_a_user_record_for_the_titler,
       Records.test_a_compaction_never_makes_a_chat_unread,
       Records.test_chat_reads_shows_compacted_as_a_system_row,
       Records.test_the_hud_transcript_shows_the_line_and_the_tool_row,
       MirrorSkip.test_discord_posts_nothing_of_a_compaction
11 reason unscrubbed / not one line / uncapped
       Records.test_the_failure_reason_is_scrubbed_one_line_and_capped
   (and the failure text itself) Records.test_a_failed_compaction_says_why
 FastPathDaemon.test_a_real_fast_path_compaction_through_the_daemon: the
   real provider end to end (no turn counted, the v1 session not counted).
"""
from __future__ import annotations

import copy
import json
import re
import secrets as pysecrets
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat_relay_check as CR  # noqa: E402  (its harness and Fake; not its tests)
import mcp_session_check as M  # noqa: E402  (helpers only)
from jarvis import llm  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2 import projects as P  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.model import ProviderName, Role  # noqa: E402
from jarvis.v2.provider import Decision, Event, EventKind as K, UserMessage  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
until = CR.until
DEADLINE = CR.DEADLINE

NOT_A_CHAT = "Tasks are not chats; only a chat compacts."
RUNNING = "A turn is running; compact when it ends."
WAITING = "Messages are waiting to run first."
NO_COMPACT = "This chat's provider cannot compact its conversation."
OK_TEXT = "Conversation compacted"
STOPPED_TEXT = "Compaction stopped"
FAILED_PREFIX = "Compaction failed: "

# Credential-shaped values, built here so no real one is ever in the file.
GH = "ghp_" + "A" * 36
JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0" + "." + "c2lnbmF0dXJl"
SK = "sk-" + "ant-" + "Q" * 24

COMPACTION_KINDS = ("compact_started", "turn_started", "tool_started", "tool_finished",
                    "usage", "turn_finished", "compacted")


# --- the fake: chat_relay_check's Fake, plus `compact` -----------------------------------


class CompactFake(CR.Fake):
    """`compact(h)` as the plan's protocol says. Per thread: `hold_compact`
    holds it until `release` (or the owner's Stop); `outcome[tid]` is the
    (stop, compacted, reason) it finishes with; `refusal[tid]` is what
    `compact_refusal` says; `cli_pending` makes `cli_turn_pending` true and
    `adopt` run the CLI's own turn (SV-6)."""

    def __init__(self, name):
        super().__init__(name)
        self.compactions, self.refusals_asked, self.adopted = [], [], []
        self.hold_compact, self.cli_pending = set(), set()
        self.outcome, self.refusal = {}, {}

    def compact_refusal(self, h):
        with self.mutex:
            self.refusals_asked.append(h.thread_id)
        return self.refusal.get(h.thread_id)

    def compact(self, h):
        tid, n = h.thread_id, h.native
        with self.mutex:
            self.compactions.append(tid)
        n.stop.clear()
        gate = threading.Event()
        with n.lock:
            n.turn, n.gate = True, gate
        yield Event(K.TURN_STARTED, tid)
        try:
            call = "compact-" + pysecrets.token_hex(4)
            yield Event(K.TOOL_STARTED, tid, {"call_id": call, "name": "compact", "args": {}})
            while tid in self.hold_compact and not gate.wait(0.002):
                if n.stop.is_set():
                    break
            if n.stop.is_set():
                yield Event(K.TOOL_FINISHED, tid, {"call_id": call, "name": "compact", "ok": False,
                                                   "outcome": "failed",
                                                   "summary": "compaction did not finish"})
                yield Event(K.TURN_FINISHED, tid, {"stop": "interrupted", "compacted": False,
                                                   "reason": None})
                return
            stop, compacted, reason = self.outcome.get(tid, ("end", True, None))
            yield Event(K.TOOL_FINISHED, tid, {"call_id": call, "name": "compact", "ok": compacted,
                                               "outcome": "ok" if compacted else "failed",
                                               "summary": "conversation compacted"})
            yield Event(K.USAGE, tid, {"input": 100, "output": 20, "cached": 0, "cost_usd": 0.05})
            yield Event(K.TURN_FINISHED, tid, {"stop": stop, "compacted": compacted,
                                               "reason": reason})
        finally:
            with n.lock:
                n.turn, n.gate = False, None
                n.leftover.extend(n.inbox)
                n.inbox = []

    def cli_turn_pending(self, h):
        return h.thread_id in self.cli_pending

    def adopt(self, h):
        tid = h.thread_id
        if tid not in self.cli_pending:
            return
        self.cli_pending.discard(tid)
        with self.mutex:
            self.adopted.append(tid)
        yield Event(K.TURN_STARTED, tid, {"self_started": "task-notification"})
        yield Event(K.TEXT, tid, {"text": "the CLI's own turn"})
        yield Event(K.TURN_FINISHED, tid, {"stop": "end"})

    def unstick(self):
        self.hold_compact.clear()
        super().unstick()


class Base(CR.Harness):
    """chat_relay_check's harness (temp config, port 0, the real broker),
    with `fast` and `claude` able to compact and `codex` not."""

    def make_daemon(self):
        self.fakes = {FAST: CompactFake(FAST), CLAUDE: CompactFake(CLAUDE), CODEX: CR.Fake(CODEX)}
        return super().make_daemon()

    # -- helpers

    def compact(self, thread, *, hold=False):
        """Start a compaction directly; with `hold`, until it runs held."""
        fake = self.fake(thread)
        if hold:
            fake.hold_compact.add(thread.id)
        out = self.d.compact(thread.id)
        self.assertEqual(out.get("status"), "started", out)
        self.assertIsInstance(out.get("turn_id"), str, out)
        if hold:
            until(lambda: fake.running(thread.id), "the compaction to run")
        return out["turn_id"]

    def finish(self, thread):
        fake = self.fake(thread)
        fake.hold_compact.discard(thread.id)
        fake.release(thread.id)

    def compactions(self, thread):
        with self.fake(thread).mutex:
            return [t for t in self.fake(thread).compactions if t == thread.id]

    def refused(self, thread, sentence=None):
        """`Daemon.compact` refuses with a DaemonError (409), calling nothing."""
        with self.assertRaises(daemon_mod.DaemonError) as caught:
            self.d.compact(thread.id)
        if sentence is not None:
            self.assertEqual(str(caught.exception), sentence)
        fake = self.fakes.get(ProviderName(thread.provider))
        if isinstance(fake, CompactFake):
            self.assertEqual(self.compactions(thread), [], "the provider's compact was called")
        self.assertEqual(self.log(thread, "compact_started"), [], "a compaction was logged")
        return str(caught.exception)

    def compacted(self, thread, turn_id=None):
        rows = [r for r in self.log(thread, "compacted")
                if turn_id is None or r.get("turn_id") == turn_id]
        return rows[-1] if rows else None

    def turn_rows(self, thread, turn_id):
        return [r for r in self.log(thread) if r.get("turn_id") == turn_id]

    def owner(self):
        return {"Origin": f"http://127.0.0.1:{self.d.face_port}"}


# --- 1. the route is the owner's alone ----------------------------------------------------


class RouteOwnerOnly(Base):
    def test_api_listener_is_refused_before_anything_is_read(self):
        # Mutation: `owner_only` removed, or moved after the thread lookup or
        # after the body is read.
        t = self.chat()
        path = f"/threads/{t.id}/compact"
        for headers in (None, {"Origin": f"http://127.0.0.1:{self.d.port}"}, self.owner()):
            status, body = M.http(self.d.port, "POST", path, body={}, headers=headers)
            self.assertEqual(status, 403, f"the API listener is never the owner ({headers}): {body}")
        status, _ = M.http(self.d.port, "POST", path, raw=b"{not json")
        self.assertEqual(status, 403, "the owner check comes before the body is read")
        status, _ = M.http(self.d.port, "POST", f"/threads/{'0' * 8}/compact", body={})
        self.assertEqual(status, 403, "the owner check comes before the thread is looked up")
        status, _ = M.http(self.d.face_port, "POST", path, body={})
        self.assertEqual(status, 403, "the HUD listener without the HUD's Origin is not the owner")
        time.sleep(0.05)
        self.assertEqual(self.compactions(t), [], "nothing started")
        self.assertEqual(self.log(t, "compact_started"), [])
        self.assertIsNone(self.session(t).worker)

    def test_hud_listener_starts_it_and_the_route_shape_holds(self):
        t = self.chat()
        path = f"/threads/{t.id}/compact"
        status, _ = M.http(self.d.face_port, "POST", path, body={"focus": "the tests"},
                           headers=self.owner())
        self.assertEqual(status, 400, "a body with any field is refused (focus is a later PR)")
        status, _ = M.http(self.d.face_port, "POST", path + "?now=1", body={}, headers=self.owner())
        self.assertEqual(status, 400, "a query is refused")
        status, _ = M.http(self.d.face_port, "GET", path, headers=self.owner())
        self.assertEqual(status, 405, "any other method is 405")
        self.assertEqual(self.compactions(t), [], "none of those started anything")
        status, body = M.http(self.d.face_port, "POST", path, body={}, headers=self.owner())
        self.assertEqual(status, 202, body)
        self.assertEqual(body.get("status"), "started", body)
        self.idle(t)
        self.assertEqual(len(self.compactions(t)), 1)
        started = self.log(t, "compact_started")
        self.assertEqual([r.get("turn_id") for r in started], [body.get("turn_id")])
        self.assertEqual((self.compacted(t) or {}).get("data"), {"ok": True, "text": OK_TEXT})
        # A refusal over the route is a 409 carrying the daemon's sentence.
        codex = self.chat(CODEX)
        status, body = M.http(self.d.face_port, "POST", f"/threads/{codex.id}/compact", body={},
                              headers=self.owner())
        self.assertEqual((status, (body or {}).get("error")), (409, NO_COMPACT))


# --- 2. only the owner's route reaches it ------------------------------------------------


def _py_files(*parts):
    out = []
    for part in parts:
        path = ROOT / part
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out.extend(sorted(path.rglob("*.py")))
        else:
            out.extend(sorted(path.parent.glob(path.name)))
    return out


class SourceGuard(unittest.TestCase):
    def test_only_the_owner_route_reaches_daemon_compact(self):
        # Mutation: a second caller of `Daemon.compact` (an MCP tool, Discord,
        # the CLI, the bridge). Fails today: there is no caller at all.
        callers = sorted(str(p.relative_to(ROOT)) for p in _py_files("jarvis")
                         if re.search(r"\bdaemon\.compact\(", p.read_text(encoding="utf-8")))
        self.assertEqual(callers, ["jarvis/v2/hud_api.py"])
        source = (ROOT / "jarvis/v2/hud_api.py").read_text(encoding="utf-8")
        at = source.find("daemon.compact(")
        before = source[max(0, at - 1500):at]
        self.assertIn('"compact"', before, "the call sits in the compact route")
        self.assertIn("owner_only(handler, daemon)", before, "behind owner_only")

    def test_no_tool_bridge_mcp_or_discord_file_names_compact(self):
        # Pin (passes today): no tool, the jarvis-mcp bridge, an MCP module or
        # Discord names `/compact` or calls `.compact(`.
        files = _py_files("jarvis/tools", "jarvis/v2/tools", "jarvis/v2/bridge.py",
                          "jarvis/v2/mcp*.py", "jarvis/v2/discord")
        self.assertTrue(files)
        hits = [str(p.relative_to(ROOT)) for p in files
                if re.search(r"/compact\b|\.compact\(", p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])

    def test_compact_context_stays_forbidden_on_the_fast_path(self):
        # Pin (passes today).
        from jarvis.v2.providers import fastpath
        self.assertIn("compact_context", fastpath.FORBIDDEN_TOOLS)
        self.assertNotIn("compact_context", fastpath.FAST_TOOLS)


# --- 3-5, 9: refusals ---------------------------------------------------------------------


class Refusals(Base):
    def test_a_running_turn_is_refused_and_compact_is_never_called(self):
        t = self.chat()
        self.turn(t)
        self.refused(t, RUNNING)
        status, body = M.http(self.d.face_port, "POST", f"/threads/{t.id}/compact", body={},
                              headers=self.owner())
        self.assertEqual((status, (body or {}).get("error")), (409, RUNNING))
        self.fake(t).hold.discard(t.id)
        self.release(t)
        self.idle(t)
        self.assertEqual(self.compactions(t), [])

    def test_a_second_compaction_is_refused_while_one_runs(self):
        t = self.chat()
        self.compact(t, hold=True)
        with self.assertRaises(daemon_mod.DaemonError) as caught:
            self.d.compact(t.id)
        self.assertEqual(str(caught.exception), RUNNING)
        self.assertEqual(len(self.compactions(t)), 1)
        self.finish(t)
        self.idle(t)

    def test_a_pending_approval_card_is_refused(self):
        # Mutation: the `_waiting_on_owner` check removed. No turn runs: the
        # card is the only thing in the way.
        t = self.chat()
        asked = threading.Thread(target=self.d.approvals.ask, daemon=True, args=(
            ApprovalRequest(tool="run_command", args={"command": "ls"}, command="ls",
                            thread_id=t.id),))
        asked.start()
        until(lambda: any(r.thread_id == t.id for r in self.d.approvals.pending()), "the card")
        self.assertIsNone(self.session(t).worker)
        self.refused(t)
        for request in self.d.approvals.pending():
            self.d.resolve_approval(request.req_id, Decision.DENY)
        asked.join(DEADLINE)

    def test_an_archived_thread_is_refused(self):
        t = self.chat()
        P.archive_thread(self.d, t.id)
        self.refused(t)

    def test_a_task_thread_is_refused(self):
        worker = self.d.open_thread(self.project.id, Role.IMPLEMENTER, FAST, {})
        self.refused(worker, NOT_A_CHAT)
        task = self.stores.tasks.create(self.project.id, "do it")
        tasked = self.d.open_thread(self.project.id, Role.CHAT, FAST, {"task_id": task.id})
        self.refused(tasked, NOT_A_CHAT)

    def test_a_closing_session_is_refused(self):
        t = self.chat()
        session = self.session(t)
        with self.d._lock:
            session.closing = True
        try:
            self.refused(t)
        finally:
            with self.d._lock:
                session.closing = False

    def test_waiting_owner_messages_are_refused(self):
        t = self.chat()
        item = daemon_mod._Queued(UserMessage("waiting"), {"text": "waiting"}, "m" + "1" * 12,
                                  "q" * 32)
        with self.d._lock:
            self.d._queues.setdefault(t.id, deque()).append(item)
        try:
            self.refused(t, WAITING)
        finally:
            with self.d._lock:
                self.d._queues.pop(t.id, None)

    def test_waiting_relays_are_refused(self):
        t, other = self.chat(), self.chat()
        relay = CR.relay_module()
        item = relay.RelayItem("m" + "2" * 12,
                               {"thread_id": other.id, "project_id": other.project_id,
                                "provider": "fast", "turn_id": "x" * 32, "title": "o", "project": "p"},
                               {"thread_id": t.id, "project_id": t.project_id, "provider": "fast",
                                "title": "t"}, "body", "queued", self.clock())
        with self.d._lock:
            self.svc.inbound.setdefault(t.id, deque()).append(item)
        try:
            self.refused(t, WAITING)
        finally:
            with self.d._lock:
                self.svc.inbound.pop(t.id, None)

    def test_a_cli_turn_waiting_runs_first_and_compaction_is_refused(self):
        t = self.chat(CLAUDE)
        fake = self.fake(t)
        fake.cli_pending.add(t.id)
        self.refused(t)
        until(lambda: fake.adopted, "the CLI's own turn")
        self.idle(t)
        self.assertEqual(len(self.log(t, "cli_turn")), 1)
        self.assertEqual(self.compactions(t), [])

    def test_a_provider_without_compact_is_refused_with_the_sentence(self):
        t = self.chat(CODEX)
        self.refused(t, NO_COMPACT)

    def test_compact_refusal_sentence_is_the_409(self):
        t = self.chat(CLAUDE)
        sentence = "Claude Code is finishing a turn of its own; compact when it ends."
        self.fake(t).refusal[t.id] = sentence
        self.refused(t, sentence)
        self.assertIn(t.id, self.fake(t).refusals_asked)
        self.fake(t).refusal.pop(t.id)
        self.compact(t)
        self.idle(t)
        self.assertEqual(len(self.compactions(t)), 1, "with no refusal it runs")


# --- 6-8: while it runs --------------------------------------------------------------------


class Running(Base):
    def test_stop_ends_it_interrupted_and_drops_the_owners_queue(self):
        # CB-1. Mutation: Stop keeps the queue during a compaction; the stop
        # text or the turn's end is wrong.
        t = self.chat()
        turn_id = self.compact(t, hold=True)
        reply = self.d.deliver(t.id, UserMessage("later, please", desk=True))
        self.assertEqual(reply.get("status"), "queued", reply)
        mid = reply["message_id"]
        self.d.interrupt(t.id)
        self.idle(t)
        self.assertIn(t.id, self.fake(t).interrupted)
        finished = [r for r in self.turn_rows(t, turn_id) if r.get("kind") == "turn_finished"]
        self.assertEqual([r["data"].get("stop") for r in finished], ["interrupted"])
        self.assertEqual((self.compacted(t, turn_id) or {}).get("data"),
                         {"ok": False, "text": STOPPED_TEXT})
        cleared = self.bus("queue_cleared", t)
        self.assertEqual(len(cleared), 1, cleared)
        self.assertEqual(cleared[0]["data"]["reason"], "stopped")
        self.assertEqual([m["message_id"] for m in cleared[0]["data"]["messages"]], [mid])
        self.assertEqual([r["data"]["message_id"] for r in self.log(t, "queued_dropped")], [mid])
        time.sleep(0.05)
        self.assertEqual([m.text for m in self.fake(t).of("messages", t.id)], [],
                         "a dropped message never runs")
        self.assertEqual(self.fake(t).of("steers", t.id), [])

    def test_an_owner_message_during_compaction_is_queued_never_steered(self):
        # Mutation: `deliver` steers into a compaction.
        t = self.chat()
        turn_id = self.compact(t, hold=True)
        reply = self.d.deliver(t.id, UserMessage("next, please", desk=True))
        self.assertEqual(reply.get("status"), "queued", reply)
        self.assertEqual(self.fake(t).of("steers", t.id), [], "steer was called")
        self.assertEqual(self.fake(t).of("messages", t.id), [], "it waits for the compaction")
        self.finish(t)
        until(lambda: self.fake(t).of("messages", t.id), "the queued message to run")
        self.idle(t)
        self.assertEqual([m.text for m in self.fake(t).of("messages", t.id)], ["next, please"])
        self.assertEqual(self.fake(t).of("steers", t.id), [])
        kinds = [r.get("kind") for r in self.log(t)]
        self.assertLess(kinds.index("compacted"), kinds.index("queued_started"),
                        "it ran after the compaction ended")
        self.assertEqual((self.compacted(t, turn_id) or {}).get("data"), {"ok": True, "text": OK_TEXT})

    def test_relays_and_replies_to_a_compacting_chat_wait_then_run_in_order(self):
        # Mutation: chat_relay steers a reply into a compaction; a relay or a
        # reply runs before the owner's queue.
        asker, helper, other = self.chat(FAST, "Asker"), self.chat(FAST, "Helper"), self.chat(CLAUDE, "Other")
        fa = self.fake(asker)
        # The asker sends to the helper during an owner turn, and that turn ends.
        self.fake(helper).hold.add(helper.id)
        self.turn(asker)
        mid = self.sent(self.send(asker, helper, "what is the port?"))
        self.until_relayed(helper)
        fa.hold.discard(asker.id)
        self.release(asker)
        until(lambda: self.session(asker).worker is None, "the asker's turn to end")
        # The asker compacts; the owner queues a message behind it.
        self.compact(asker, hold=True)
        queued = self.d.deliver(asker.id, UserMessage("owner next", desk=True))
        self.assertEqual(queued.get("status"), "queued", queued)
        # Another chat messages the compacting asker: it waits.
        self.turn(other)
        later = self.sent(self.send(other, asker, "a note from Other"))
        self.assertEqual(self.item(later).state, "queued")
        # The helper answers while the asker compacts: held, never steered.
        self.fake(helper).hold.discard(helper.id)
        self.release(helper)
        until(lambda: self.item(mid).delivery is not None or self.svc.held.get(asker.id),
              "the helper's reply to be handed over")
        self.assertIsNone(self.item(mid).delivery, "the reply went somewhere other than held")
        time.sleep(0.05)
        self.assertEqual(self.reply_steers(asker), [], "a reply was steered into the compaction")
        self.assertEqual(fa.of("steers", asker.id), [])
        self.assertEqual(fa.of("messages", asker.id)[1:], [], "nothing ran during the compaction")
        # The compaction ends: the owner's message, then the relay, then the reply.
        self.finish(asker)
        until(lambda: len(fa.of("messages", asker.id)) >= 4, "the three waiting turns")
        self.idle(asker)
        ran = fa.of("messages", asker.id)[1:]
        self.assertEqual([m.origin for m in ran], ["owner", "chat", "chat-reply"], [m.text[:40] for m in ran])
        self.assertEqual(ran[0].text, "owner next")
        self.assertEqual(fa.of("steers", asker.id), [])
        self.fake(other).hold.discard(other.id)
        self.release(other)
        self.idle(other)
        self.idle(helper)


# --- 10, 11: records and consumers --------------------------------------------------------


class Records(Base):
    def test_every_record_of_the_compaction_is_marked_and_ordered(self):
        # Mutation: `_record` does not stamp `compaction` while compacting;
        # compacted logged before turn_finished; a `user` record made.
        t = self.chat()
        turn_id = self.compact(t)
        self.idle(t)
        rows = self.turn_rows(t, turn_id)
        kinds = [r.get("kind") for r in rows]
        self.assertEqual([k for k in kinds if k in COMPACTION_KINDS], list(COMPACTION_KINDS), kinds)
        self.assertNotIn("user", kinds)
        unmarked = [r.get("kind") for r in rows if r.get("compaction") is not True]
        self.assertEqual(unmarked, [], "every record of the compaction says so")
        started = rows[0]
        self.assertEqual((started.get("kind"), started.get("thread_id")), ("compact_started", t.id))
        bus = [e for e in self.bus(thread=t) if e.get("turn_id") == turn_id]
        self.assertEqual([e.get("kind") for e in bus if e.get("kind") in COMPACTION_KINDS],
                         list(COMPACTION_KINDS))
        self.assertEqual([e.get("kind") for e in bus if e.get("compaction") is not True], [])
        # The next owner turn is not a compaction.
        self.d.deliver(t.id, UserMessage("hello again", desk=True))
        until(lambda: self.fake(t).of("messages", t.id), "the owner turn")
        self.idle(t)
        later = [r for r in self.log(t) if r.get("turn_id") not in (None, turn_id)]
        self.assertTrue(later)
        self.assertEqual([r.get("kind") for r in later if "compaction" in r], [])

    def test_a_compaction_is_not_a_turn_but_its_cost_counts(self):
        # Mutation: `_finish` counts a compaction in thread.turns; usage dropped.
        t = self.chat()
        before = self.stores.threads.get(t.id)
        self.compact(t)
        self.idle(t)
        after = self.stores.threads.get(t.id)
        self.assertEqual(after.turns, before.turns, "a compaction is not a turn")
        self.assertAlmostEqual(after.cost_usd - before.cost_usd, 0.05, places=6)
        self.d.deliver(t.id, UserMessage("hello", desk=True))
        until(lambda: self.fake(t).of("messages", t.id), "the owner turn")
        self.idle(t)
        self.assertEqual(self.stores.threads.get(t.id).turns, before.turns + 1, "the control")

    def test_thread_json_says_compacting_while_it_runs(self):
        t = self.chat()
        self.assertIs(self.d.thread_json(self.stores.threads.get(t.id)).get("compacting"), False)
        self.compact(t, hold=True)
        live = self.d.thread_json(self.stores.threads.get(t.id))
        self.assertIs(live.get("compacting"), True, live)
        self.assertIs(live.get("running"), True)
        self.assertIs(self.session(t).compacting, True)
        self.finish(t)
        self.idle(t)
        self.assertIs(self.d.thread_json(self.stores.threads.get(t.id)).get("compacting"), False)
        self.assertIs(self.session(t).compacting, False)

    def test_the_runner_skips_compaction_records(self):
        from jarvis.v2 import runner
        record = {"thread_id": "aaaa0001", "kind": "turn_finished", "data": {"stop": "end"},
                  "compaction": True}
        self.assertFalse(runner._for_turn(record, "aaaa0001"))
        plain = {k: v for k, v in record.items() if k != "compaction"}
        self.assertTrue(runner._for_turn(plain, "aaaa0001"), "the control")

    def test_no_compaction_record_is_a_user_record_for_the_titler(self):
        # The titler reads only `user_message` (and `user` records for its
        # text): a compaction makes neither, so it never titles one.
        t = self.chat()
        turn_id = self.compact(t)
        self.idle(t)
        rows = self.turn_rows(t, turn_id)
        self.assertTrue(rows)
        self.assertEqual([r for r in rows if r.get("kind") in ("user", "user_message")], [])
        bus = [e for e in self.bus(thread=t) if e.get("turn_id") == turn_id]
        self.assertTrue(bus)
        self.assertEqual([e for e in bus if e.get("kind") in ("user", "user_message")], [])

    def test_a_compaction_never_makes_a_chat_unread(self):
        # Mutation: activity treats a compaction's turn_finished as a reply.
        t = self.chat()
        self.d.deliver(t.id, UserMessage("hello", desk=True))
        until(lambda: self.fake(t).of("messages", t.id), "the owner turn")
        self.idle(t)
        until(lambda: self.d.activity.thread_status(t.id) == "unread", "unread after a reply")
        self.d.activity.seen_thread(t.id)
        self.assertNotEqual(self.d.activity.thread_status(t.id), "unread")
        self.compact(t)
        self.idle(t)
        time.sleep(0.05)
        self.assertNotEqual(self.d.activity.thread_status(t.id), "unread")
        self.assertNotEqual(self.d.activity.thread_status(t.id), "working")

    def test_chat_reads_shows_compacted_as_a_system_row(self):
        from jarvis.v2 import chat_reads
        rows = [{"kind": "compact_started", "at": "2026-10-19T00:00:00+00:00", "turn_id": "c" * 32,
                 "thread_id": "aaaa0001", "compaction": True, "data": {}},
                {"kind": "compacted", "at": "2026-10-19T00:00:01+00:00", "turn_id": "c" * 32,
                 "thread_id": "aaaa0001", "compaction": True,
                 "data": {"ok": True, "text": OK_TEXT}}]
        out = chat_reads.transcript(rows, "claude")
        self.assertEqual([(r.role, r.text) for r in out], [("system", OK_TEXT)])

    def test_the_hud_transcript_shows_the_line_and_the_tool_row(self):
        t = self.chat()
        turn_id = self.compact(t)
        self.idle(t)
        status, body = M.http(self.d.port, "GET", f"/threads/{t.id}/transcript")
        self.assertEqual(status, 200, body)
        rows = body["messages"]
        self.assertEqual([(m.get("role"), m.get("text")) for m in rows], [("system", OK_TEXT)], rows)
        status, body = M.http(self.d.port, "GET", f"/threads/{t.id}/transcript?tools=1")
        self.assertEqual(status, 200, body)
        rows = body["messages"]
        tools = [m for m in rows if m.get("role") == "tool"]
        self.assertEqual([(m.get("name"), m.get("outcome"), m.get("turn_id")) for m in tools],
                         [("compact", "ok", turn_id)], rows)
        systems = [m for m in rows if m.get("role") == "system"]
        self.assertEqual([(m.get("text"), m.get("turn_id")) for m in systems], [(OK_TEXT, turn_id)])
        self.assertEqual([m for m in rows if m.get("role") == "user"], [])

    def test_a_failed_compaction_says_why(self):
        t = self.chat()
        self.fake(t).outcome[t.id] = ("error", False, "the CLI said no")
        turn_id = self.compact(t)
        self.idle(t)
        self.assertEqual((self.compacted(t, turn_id) or {}).get("data"),
                         {"ok": False, "text": FAILED_PREFIX + "the CLI said no"})

    def test_the_failure_reason_is_scrubbed_one_line_and_capped(self):
        # Mutation: the reason logged or published unscrubbed, multi-line or uncapped.
        from jarvis.v2.approvals import clean_line
        from jarvis.v2.redact import scrub_text
        t = self.chat()
        reason = (f"upstream rejected token {GH}\nthen: curl -H 'Authorization: Bearer "
                  f"{JWT}' with key {SK}\n" + "more detail " * 60)
        self.fake(t).outcome[t.id] = ("error", False, reason)
        turn_id = self.compact(t)
        self.idle(t)
        want = FAILED_PREFIX + clean_line(scrub_text(reason), 200)
        logged = (self.compacted(t, turn_id) or {}).get("data") or {}
        published = [e for e in self.bus("compacted", t) if e.get("turn_id") == turn_id]
        self.assertEqual(len(published), 1, published)
        for where, data in (("log", logged), ("bus", published[0].get("data") or {})):
            text = data.get("text") or ""
            self.assertEqual(text, want, where)
            self.assertFalse(data.get("ok"), where)
            for raw in (GH, JWT, SK):
                self.assertNotIn(raw, text, where)
            self.assertNotIn("\n", text, where)
            self.assertLessEqual(len(text), len(FAILED_PREFIX) + 200, where)


# --- the Discord mirror (CB-5) -------------------------------------------------------------

try:
    import discord_mirror_check as DM  # noqa: E402
except Exception as exc:  # noqa: BLE001 — the mirror case fails alone, never the file
    DM, _DM_ERROR = None, exc


class MirrorSkip(DM.Harness if DM is not None else unittest.TestCase):
    def test_discord_posts_nothing_of_a_compaction(self):
        # Mutation: the mirror does not skip records marked `compaction`.
        if DM is None:
            self.fail(f"discord_mirror_check did not import: {_DM_ERROR!r}")
        with self.transport.lock:
            before = len(self.transport.calls)
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        self.idle(chat.id)
        self.settled(chat.id)
        turn = pysecrets.token_hex(16)
        for kind, data in (("compact_started", {}), ("turn_started", {}),
                           ("tool_started", {"call_id": "compact-1", "name": "compact", "args": {}}),
                           ("text", {"text": "COMPACTED-WORDS of a summary"}),
                           ("tool_finished", {"call_id": "compact-1", "name": "compact", "ok": True,
                                              "outcome": "ok", "summary": "conversation compacted"}),
                           ("turn_finished", {"stop": "end", "compacted": True, "reason": None}),
                           ("compacted", {"ok": True, "text": OK_TEXT})):
            self.stores.threads._append(chat.id, "log.jsonl", {
                "kind": kind, "at": "2026-10-19T00:00:00+00:00", "turn_id": turn,
                "thread_id": chat.id, "event_id": pysecrets.token_hex(16), "data": data,
                "compaction": True})
        self.hud_send(chat, "again")
        self.idle(chat.id)
        self.settled(chat.id)
        with self.transport.lock:
            posted = json.dumps(self.transport.calls[before:])
        self.assertNotIn("COMPACTED-WORDS", posted)
        self.assertNotIn(OK_TEXT, posted)
        self.assertEqual(posted.count("the answer"), 2, "the owner's replies around it were posted")


# --- the real fast path through the daemon --------------------------------------------------


def _reply(text):
    return llm.Reply(message={"content": text}, finish_reason="stop", model="stub",
                     latency_s=0.0, cost_usd=0.0)


class FastPathDaemon(Base):
    def make_daemon(self):
        from jarvis.v2.providers.fastpath import FastPathProvider
        real_chat = llm.chat
        self.summaries = []

        def chat(model, messages, tools=None, on_delta=None, **kw):
            if tools is None:
                self.summaries.append(model)
                return _reply("GOAL: the drivetrain\nFAILED: none")
            return _reply("done")
        llm.chat = chat
        self.addCleanup(setattr, llm, "chat", real_chat)
        self.fakes = {FAST: FastPathProvider(), CLAUDE: CompactFake(CLAUDE), CODEX: CR.Fake(CODEX)}
        return CR.Harness.make_daemon(self)

    def test_a_real_fast_path_compaction_through_the_daemon(self):
        from jarvis import context
        t = self.chat(FAST)
        native = self.session(t).handle.native
        agent = native.agent
        goal = {"role": "user", "content": "build the VEX drivetrain"}
        messages = [agent.messages[0], goal]
        for i in range(10):
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "get_datetime", "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "get_datetime",
                             "content": "x" * 300})
            messages.append({"role": "user", "content": f"step {i}"})
        agent.messages = messages
        agent.policy = context.ContextPolicy(keep_recent_messages=4, spill_dir=self.root / "spill")
        before_turns = self.stores.threads.get(t.id).turns
        session_turns = native.session.turns
        turn_id = self.d.compact(t.id)["turn_id"]
        self.idle(t)
        self.assertEqual((self.compacted(t, turn_id) or {}).get("data"),
                         {"ok": True, "text": OK_TEXT})
        self.assertTrue(self.summaries, "the summary was asked for")
        self.assertEqual(agent.messages[1], goal, "messages[1] is pinned")
        self.assertTrue(str(agent.messages[2]["content"]).startswith(context.SUMMARY_PREFIX))
        self.assertEqual(self.stores.threads.get(t.id).turns, before_turns)
        self.assertEqual(native.session.turns, session_turns, "the v1 session counts no turn")
        saved = native.session.restore_messages()
        self.assertTrue(any(str(m.get("content", "")).startswith(context.SUMMARY_PREFIX)
                            for m in saved), "the compacted transcript was saved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
