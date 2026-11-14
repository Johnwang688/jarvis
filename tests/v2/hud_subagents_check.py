"""`hud_open_subagents` (owner, 2026-11-12): a chat shows its own native
sub-agents in the owner's HUD panes. The tool reaches `subagent_panes.py`
and nothing else; it publishes one `subagent_open` bus record of ids only.

Bound as `owner_post` is (its tests are the model): Claude and Codex owner
chats, foreground-only, desk-only, not read-only; checked in the tool and
again by the service under the daemon lock.

Free and hermetic: every daemon runs on port 0 over a temporary
V2_DATA_DIR and temporary config (chat_send_check's harness).

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \\
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \\
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \\
    .venv/bin/python tests/v2/hud_subagents_check.py

TEST -> MUTATION
================
  bind it to the fast path                     Contract.test_the_fast_path_never_holds_it
  not foreground-only (Claude sub-agent)       ClaudeHook.test_a_sub_agent_is_denied_without_asking_permit
  no desk check in the tool                    Binding.test_the_tool_refuses_off_the_desk_and_at_depth_first
  no desk check in the service                 ServiceChecks.test_the_service_refuses_what_owner_post_refuses
  another chat's sub-agents reachable          Choice.test_only_this_chats_own_records_are_read
  words on the bus                             Choice.test_the_bus_carries_ids_only
  no cap                                       Choice.test_at_most_eleven
  (report back, 2026-11-13)
  wait under the daemon lock                   ReportBack.test_the_wait_holds_no_daemon_lock
  no deadline / wall clock                     ReportBack.test_the_wait_is_bounded_on_the_monotonic_clock,
                                               ReportBack.test_the_production_clock_is_monotonic
  a second window overrides the first          ReportBack.test_the_first_window_wins_and_bad_reports_are_refused
  route not owner-only / no strict Host        ReportRoute.test_no_one_else_reaches_it
  any window id accepted                       ReportRoute.test_refusals (not live: 409)
"""
from __future__ import annotations

import contextvars
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat_send_check as CS  # noqa: E402  (its harness and hook helpers only)
import mcp_session_check as M  # noqa: E402  (helpers only; not its setUpModule)
import editor_files_check as EF  # noqa: E402  (its Daemonised world only, for the route)
from jarvis import config, runtime, tools  # noqa: E402
config.CLAUDE_SKILLS_DIR = None  # never read the owner's real ~/.claude/skills
from jarvis.v2 import agent_panes, bridge, session_tools, subagent_panes  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role  # noqa: E402
from jarvis.v2.provider import Brief, Decision, UserMessage  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
NAME = "hud_open_subagents"
REFUSED = ("Error:", "Refused:")
SID = "cl:" + "a" * 16


def tool_module():
    from jarvis.v2.tools import hud_subagents
    return hud_subagents


def is_refusal(text) -> bool:
    return isinstance(text, str) and text.startswith(REFUSED)


def row(sid, name="", status="running", turn_id="t" * 32, **over):
    out = {"sid": sid, "name": name, "status": status, "turn_id": turn_id, "depth": 1}
    out.update(over)
    return out


def sid(n: int) -> str:
    return f"cl:{n:016x}"


class Contract(unittest.TestCase):
    def test_schema_is_generated_from_annotated_hints(self):
        module = tool_module()
        entry = tools.REGISTRY[NAME]
        self.assertIs(entry.func, module.hud_open_subagents)
        schema = entry.schema
        self.assertEqual(schema.get("required", []), [])
        self.assertEqual(schema["properties"]["which"]["items"], {"type": "string"})
        self.assertFalse(schema["additionalProperties"])

    def test_flags_explicit_only_not_parallel_not_dangerous_not_external(self):
        tool_module()
        from jarvis.v2 import mcp, mcp_catalog
        self.assertIn(NAME, tools.EXPLICIT_ONLY)
        self.assertNotIn(NAME, tools.PARALLEL_SAFE)
        self.assertFalse(tools.REGISTRY[NAME].dangerous)
        self.assertNotIn(NAME, mcp_catalog.MCP_TOOLS)
        self.assertNotIn(NAME, mcp.MCP_TOOLS)
        self.assertNotIn(NAME, tools.default_names(), "no v1 surface holds it")

    def test_session_catalog_binds_it_as_owner_post(self):
        entry = session_tools.SESSION_TOOLS[NAME]
        post = session_tools.SESSION_TOOLS["owner_post"]
        self.assertEqual((entry.providers, entry.foreground_only, entry.desk_only, entry.read_only,
                          entry.owner_gated),
                         (post.providers, post.foreground_only, post.desk_only, post.read_only,
                          post.owner_gated))
        self.assertEqual(entry.providers, frozenset({"claude", "codex"}))
        self.assertTrue(entry.foreground_only and entry.desk_only)
        self.assertFalse(entry.read_only)
        for provider in (CLAUDE, CODEX):
            self.assertIn(NAME, {t.name for t in session_tools.for_provider(provider)})
        listed = {t["name"]: t for t in bridge.listing(CLAUDE)}[NAME]
        self.assertEqual(listed["annotations"], {"readOnlyHint": False})
        self.assertNotIn(NAME, session_tools.read_only_claude_names())

    def test_the_fast_path_never_holds_it(self):
        from jarvis.v2.providers import fastpath
        for group in (fastpath.FAST_TOOLS, fastpath.DESK_TOOLS, fastpath.EDIT_TOOLS, fastpath.CHAT_TOOLS):
            self.assertNotIn(NAME, group)


class Strict(unittest.TestCase):
    setUp, brief, decisions = CS.Strict.setUp, CS.Strict.brief, CS.Strict.decisions

    def test_strict_refuses_auto_and_ask_delegate(self):
        tool_module()
        for provider, prefix in (("claude", session_tools.CLAUDE_PREFIX), ("codex", "")):
            with self.subTest(provider=provider, profile="strict"):
                brief = self.brief(PermissionProfile.STRICT)
                asker = M.Asker(Decision.ALLOW)
                permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider), asker)
                self.assertEqual(permit(prefix + NAME, {}, brief), Decision.DENY)
                self.assertEqual(asker.seen, [])
            for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
                with self.subTest(provider=provider, profile=profile.value):
                    brief = self.brief(profile)
                    asker = M.Asker(Decision.DENY)
                    permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider), asker)
                    self.assertEqual(permit(prefix + NAME, {}, brief), Decision.ALLOW)
                    self.assertEqual(asker.seen, [])


class ClaudeHook(unittest.TestCase):
    setUp, start, hook, counted_permit = (CS.ClaudeHook.setUp, CS.ClaudeHook.start, CS.ClaudeHook.hook,
                                          CS.ClaudeHook.counted_permit)

    def test_a_sub_agent_is_denied_without_asking_permit(self):
        tool_module()
        asker = M.Asker(Decision.ALLOW)
        permit, calls = self.counted_permit(asker)
        handle = self.start(permit, "hsub0001")
        out = self.hook(handle, session_tools.CLAUDE_PREFIX + NAME, agent_id="sub-1")
        decided = out.get("hookSpecificOutput", {})
        self.assertEqual(decided.get("permissionDecision"), "deny")
        self.assertEqual(calls, [], "permit is never asked")


# --- the service, on a stub daemon ---------------------------------------------------------


def stub_session(**over):
    values = dict(worker=object(), handle=object(), turn_id="t" * 32, relay_turn=None,
                  cancelled=threading.Event(), closing=False, retired=False, lost=False,
                  brief=Brief(Role.CHAT, "/x"),
                  thread=SimpleNamespace(id="ab12cd34", project_id="p1", provider=CLAUDE, title="x"),
                  desk=SimpleNamespace(slot={"present": True}))
    values.update(over)
    return SimpleNamespace(**values)


def stub_service(session, rows_by_thread, windows=(), on_publish=None):
    """`windows`: the live owner windows (none by default: the call then
    publishes without a request id and does not wait). `on_publish(record,
    reports)` runs as each record is published, as a window would answer."""
    published = []
    seen = []
    live = set(windows)

    def listing(thread_id):
        seen.append(thread_id)
        return [dict(r) for r in rows_by_thread.get(thread_id, [])]

    def publish(record):
        published.append(record)
        if on_publish is not None:
            on_publish(record, svc.reports)
    daemon = SimpleNamespace(_lock=threading.RLock(), _stopping=False, _sessions={"ab12cd34": session},
                             bus=SimpleNamespace(publish=publish),
                             subagents=SimpleNamespace(list=listing),
                             hud_window_count=lambda: len(live),
                             hud_window_live=lambda w: w in live)
    svc = subagent_panes.SubagentPanes(daemon)
    svc.closed = False
    return svc, published, seen


CALLER = {"thread_id": "ab12cd34", "turn_id": "t" * 32}


class ServiceChecks(unittest.TestCase):
    def test_the_service_refuses_what_owner_post_refuses(self):
        stopped = threading.Event()
        stopped.set()
        rows = {"the control": ({}, True),
                "the fast path": ({"thread": SimpleNamespace(id="ab12cd34", project_id="p1", provider=FAST,
                                                             title="x")}, False),
                "a task orchestrator": ({"brief": Brief(Role.ORCHESTRATOR, "/x", task_id="k1")}, False),
                "a task's chat brief": ({"brief": Brief(Role.CHAT, "/x", task_id="k1")}, False),
                "a relayed turn": ({"relay_turn": object()}, False),
                "off the desk": ({"desk": SimpleNamespace(slot={"present": False})}, False),
                "between turns": ({"worker": None}, False),
                "another turn": ({"turn_id": "u" * 32}, False),
                "stopped": ({"cancelled": stopped}, False),
                "closing": ({"closing": True}, False),
                "no handle": ({"handle": None}, False)}
        for label, (over, ok) in rows.items():
            with self.subTest(case=label):
                svc, published, _ = stub_service(stub_session(**over), {"ab12cd34": [row(SID, "w")]})
                out = svc.open(dict(CALLER), None)
                self.assertEqual(not is_refusal(out), ok, out)
                self.assertEqual(len(published), 1 if ok else 0)

    def test_a_stopped_service_and_a_bad_caller_refuse(self):
        svc, published, _ = stub_service(stub_session(), {"ab12cd34": [row(SID)]})
        for caller in (None, {}, {"thread_id": "ab12cd34"}, {"thread_id": 1, "turn_id": "t"}):
            self.assertTrue(is_refusal(svc.open(caller, None)))
        svc.closed = True
        self.assertTrue(is_refusal(svc.open(dict(CALLER), None)))
        self.assertEqual(published, [])


class Choice(unittest.TestCase):
    def open(self, rows, which=None):
        svc, published, seen = stub_service(stub_session(), {"ab12cd34": rows, "other000": [row("cl:" + "f" * 16)]})
        out = svc.open(dict(CALLER), which)
        return out, published, seen

    def sids(self, published):
        self.assertEqual(len(published), 1)
        return published[0]["data"]["sids"]

    def test_default_is_every_running_one(self):
        rows = [row(sid(1), "a"), row(sid(2), "b", status="done"), row(sid(3), "c", status="backgrounded")]
        out, published, _ = self.open(rows)
        self.assertFalse(is_refusal(out), out)
        self.assertEqual(self.sids(published), [sid(1), sid(3)])

    def test_default_falls_back_to_this_turns_when_none_runs(self):
        rows = [row(sid(1), "old", status="done", turn_id="o" * 32),
                row(sid(2), "new", status="done")]
        out, published, _ = self.open(rows)
        self.assertEqual(self.sids(published), [sid(2)])
        out, published, _ = self.open([row(sid(1), status="done", turn_id="o" * 32)])
        self.assertTrue(is_refusal(out))
        self.assertEqual(published, [])

    def test_by_name_or_sid_newest_of_a_name(self):
        rows = [row(sid(1), "Scout", status="done"), row(sid(2), "Builder"), row(sid(3), "scout")]
        out, published, _ = self.open(rows, ["  SCOUT ", sid(2)])
        self.assertFalse(is_refusal(out), out)
        self.assertEqual(self.sids(published), [sid(3), sid(2)])

    def test_an_unknown_name_refuses_and_lists_this_chats_own(self):
        out, published, _ = self.open([row(sid(1), "Scout")], ["Ghost"])
        self.assertTrue(is_refusal(out))
        self.assertIn("Scout", out)
        self.assertEqual(published, [])

    def test_no_sub_agents_refuses(self):
        out, published, _ = self.open([])
        self.assertTrue(is_refusal(out))
        self.assertEqual(published, [])

    def test_nested_and_malformed_rows_are_skipped(self):
        rows = [row(sid(1), "top"), row(sid(2), "nested", parent_sid=sid(1)), row("not a sid!", "bad")]
        out, published, _ = self.open(rows)
        self.assertEqual(self.sids(published), [sid(1)])

    def test_only_this_chats_own_records_are_read(self):
        out, published, seen = self.open([row(sid(1), "mine")], ["cl:" + "f" * 16])
        self.assertTrue(is_refusal(out), "another chat's sid is not this chat's")
        self.assertEqual(seen, ["ab12cd34"])
        self.assertEqual(published, [])

    def test_the_bus_carries_ids_only(self):
        out, published, _ = self.open([row(sid(1), "SENTINEL-name-9f")])
        record = published[0]
        self.assertEqual(set(record), {"kind", "thread_id", "at", "data"})
        self.assertEqual((record["kind"], record["thread_id"]), ("subagent_open", "ab12cd34"))
        self.assertEqual(record["data"], {"sids": [sid(1)]}, "no window open: no request id")
        self.assertNotIn("SENTINEL", repr(record))
        self.assertIn("No HUD window is open", out)
        svc, published, _ = stub_service(stub_session(), {"ab12cd34": [row(sid(1), "SENTINEL-name-9f")]},
                                         windows=[WINDOW], on_publish=answer(1, 0))
        svc.open(dict(CALLER), None)
        data = published[0]["data"]
        self.assertEqual(set(data), {"sids", "request_id"})
        self.assertRegex(data["request_id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("SENTINEL", repr(published[0]))

    def test_at_most_eleven(self):
        self.assertEqual(subagent_panes.OPEN_MAX, 11)
        rows = [row(sid(n), f"w{n}") for n in range(15)]
        out, published, _ = self.open(rows)
        self.assertEqual(self.sids(published), [sid(n) for n in range(4, 15)])
        out, published, _ = self.open(rows, [f"w{n}" for n in range(12)])
        self.assertTrue(is_refusal(out))
        for bad in ([], [""], ["x" * 201], [1], "w1"):
            with self.subTest(which=bad):
                out, published, _ = self.open(rows, bad)
                self.assertTrue(is_refusal(out), out)
                self.assertEqual(published, [])


# --- the window's report back (agent_panes.py, 2026-11-13) ----------------------------------

WINDOW = "a" * 32
OTHER_WINDOW = "b" * 32


def answer(placed, not_placed, window=WINDOW, delay=0.0, seen=None):
    """An `on_publish` that answers as a window would, from another thread."""
    def go(record, reports):
        def post():
            if delay:
                import time
                time.sleep(delay)
            body = {"window": window, "request_id": record["data"]["request_id"], "placed": placed,
                    "not_placed": not_placed}
            try:
                got = reports.report(body)
            except agent_panes.ReportRefused as refusal:
                got = refusal
            if seen is not None:
                seen.append(got)
        threading.Thread(target=post, daemon=True).start()
    return go


class FastClock:
    """A monotonic clock that moves a second per read: a wait times out at once."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1.0
        return self.now


class ReportBack(unittest.TestCase):
    rows = {"ab12cd34": [row(sid(n), f"w{n}") for n in range(3)]}

    def test_every_one_shown(self):
        svc, published, _ = stub_service(stub_session(), self.rows, windows=[WINDOW], on_publish=answer(3, 0))
        out = svc.open(dict(CALLER), None)
        self.assertTrue(out.startswith("Shown 3 of 3 sub-agent(s) in HUD panes"), out)
        self.assertEqual(svc.reports._waiting, {}, "the id is forgotten after the answer")

    def test_partial_says_how_many_had_no_room(self):
        svc, _, _ = stub_service(stub_session(), self.rows, windows=[WINDOW], on_publish=answer(2, 1, delay=0.05))
        out = svc.open(dict(CALLER), None)
        self.assertTrue(out.startswith("Shown 2 of 3"), out)
        self.assertIn("no room for 1", out)
        self.assertFalse(is_refusal(out))

    def test_no_reply_says_so_and_a_late_answer_is_refused(self):
        svc, published, _ = stub_service(stub_session(), self.rows, windows=[WINDOW])
        svc.reports.clock = FastClock()
        out = svc.open(dict(CALLER), None)
        self.assertIn("No HUD window replied within 3 seconds", out)
        self.assertIn("Asked the HUD to open 3", out)
        late = {"window": WINDOW, "request_id": published[0]["data"]["request_id"], "placed": 3, "not_placed": 0}
        with self.assertRaises(agent_panes.ReportRefused) as caught:
            svc.reports.report(late)
        self.assertEqual(caught.exception.status, 404)

    def test_the_production_clock_is_monotonic(self):
        """The wait's deadline is on the monotonic clock by default, the one
        every service builds (review of PR #358, LOW 3: the test below
        injects its clock, so a wall-clock default survived it)."""
        import time
        self.assertIs(agent_panes.PaneReports(SimpleNamespace()).clock, time.monotonic)
        svc, _, _ = stub_service(stub_session(), self.rows)
        self.assertIs(svc.reports.clock, time.monotonic)

    def test_the_wait_is_bounded_on_the_monotonic_clock(self):
        svc, _, _ = stub_service(stub_session(), self.rows, windows=[WINDOW])
        reads = []
        clock = FastClock()

        def counted():
            reads.append(1)
            return clock()
        svc.reports.clock = counted
        svc.open(dict(CALLER), None)
        # 3 s at a second per read: a handful of reads, never a spin or a hang.
        self.assertLessEqual(len(reads), 6)
        self.assertEqual(agent_panes.REPORT_WAIT_S, 3.0)

    def test_the_wait_holds_no_daemon_lock(self):
        held = []

        def probe(record, reports):
            waiting = threading.Event()
            real_wait = reports.wait

            def wait(request_id, timeout=agent_panes.REPORT_WAIT_S):
                waiting.set()
                return real_wait(request_id, timeout)
            reports.wait = wait

            def go():
                # Only once the call is in its wait: a probe that took the lock
                # before the call reached it would prove nothing.
                waiting.wait(5)
                import time
                time.sleep(0.05)
                got = svc.daemon._lock.acquire(timeout=1)
                held.append(got)
                if got:
                    svc.daemon._lock.release()
                reports.report({"window": WINDOW, "request_id": record["data"]["request_id"],
                                "placed": 3, "not_placed": 0})
            threading.Thread(target=go, daemon=True).start()
        svc, _, _ = stub_service(stub_session(), self.rows, windows=[WINDOW], on_publish=probe)
        out = svc.open(dict(CALLER), None)
        self.assertEqual(held, [True], "another thread takes the daemon lock while the call waits")
        self.assertTrue(out.startswith("Shown 3 of 3"), out)

    def test_the_first_window_wins_and_bad_reports_are_refused(self):
        seen = []

        def two(record, reports):
            rid = record["data"]["request_id"]

            def status(body):
                try:
                    reports.report(body)
                    return 200
                except agent_panes.ReportRefused as refusal:
                    return refusal.status
            good = {"window": WINDOW, "request_id": rid, "placed": 1, "not_placed": 2}
            seen.extend([
                ("not live", status({**good, "window": "c" * 32})),
                ("bad window", status({**good, "window": "x"})),
                ("bad id", status({**good, "request_id": "Z" * 32})),
                ("unknown id", status({**good, "request_id": "d" * 32})),
                ("bool count", status({**good, "placed": True, "not_placed": 2})),
                ("too many", status({**good, "placed": 12, "not_placed": 0})),
                ("negative", status({**good, "placed": -1, "not_placed": 4})),
                ("not the ask's count", status({**good, "placed": 1, "not_placed": 1})),
                ("extra key", status({**good, "url": "https://example.com"})),
                ("missing key", status({k: v for k, v in good.items() if k != "placed"})),
                ("not a dict", status([good])),
                ("first", status(good)),
                ("second window", status({**good, "window": OTHER_WINDOW, "placed": 3, "not_placed": 0})),
            ])
        svc, _, _ = stub_service(stub_session(), self.rows, windows=[WINDOW, OTHER_WINDOW], on_publish=two)
        out = svc.open(dict(CALLER), None)
        self.assertEqual(dict(seen), {"not live": 409, "bad window": 400, "bad id": 400, "unknown id": 404,
                                      "bool count": 400, "too many": 400, "negative": 400,
                                      "not the ask's count": 400, "extra key": 400, "missing key": 400,
                                      "not a dict": 400, "first": 200, "second window": 409})
        self.assertTrue(out.startswith("Shown 1 of 3"), out)

    def test_too_many_waiting_publishes_without_an_id_and_does_not_wait(self):
        svc, published, _ = stub_service(stub_session(), self.rows, windows=[WINDOW])
        svc.reports._waiting = {f"{n:032x}": agent_panes._Waiting(1) for n in range(agent_panes.PENDING_MAX)}
        svc.reports.clock = FastClock()
        out = svc.open(dict(CALLER), None)
        self.assertNotIn("request_id", published[0]["data"])
        self.assertIn("No HUD window replied", out)

    def test_a_refused_call_waits_for_nothing(self):
        svc, published, _ = stub_service(stub_session(desk=SimpleNamespace(slot={"present": False})), self.rows,
                                         windows=[WINDOW])
        self.assertTrue(is_refusal(svc.open(dict(CALLER), None)))
        self.assertEqual((published, svc.reports._waiting), ([], {}))


class ReportRoute(EF.Daemonised):
    """`POST /agent-panes/report` on a real daemon: the owner's window only."""

    def setUp(self):
        super().setUp()
        self.window = "e" * 32
        self.daemon._dev_stream_started(self.window)
        self.addCleanup(self.daemon._dev_stream_ended, self.window)

    def body(self, rid, **over):
        out = {"window": self.window, "request_id": rid, "placed": 2, "not_placed": 0}
        out.update(over)
        return out

    def test_the_owners_window_answers_a_waiting_ask(self):
        reports = self.daemon.agent_pane_reports
        rid = reports.expect(2)
        self.assertEqual(self.owner("POST", "/agent-panes/report", self.body(rid)), {"ok": True})
        self.assertEqual(reports.wait(rid, timeout=1), {"placed": 2, "not_placed": 0})
        self.owner("POST", "/agent-panes/report", self.body(rid), status=404)

    def test_no_one_else_reaches_it(self):
        reports = self.daemon.agent_pane_reports
        rid = reports.expect(2)
        api, face, work = self.daemon.port, self.face, self.daemon.workshop_port
        for port, headers in ((api, {}), (api, {"Origin": f"http://127.0.0.1:{api}"}),
                              (face, {}), (face, {"Sec-Fetch-Site": "same-origin"}),
                              (face, {"Origin": "null"}), (face, {"Origin": f"http://127.0.0.1:{work}"}),
                              (face, {"Origin": "https://example.com"}),
                              (face, {"Host": f"evil.example:{face}", "Origin": f"http://evil.example:{face}"})):
            with self.subTest(port=port, headers=headers):
                status, payload = self.raw("POST", "/agent-panes/report", headers, port=port, body=self.body(rid))
                self.assertEqual(status, 403, str(payload)[:200])
        self.assertIsNotNone(reports._waiting[rid])
        self.assertIsNone(reports._waiting[rid].answer, "nothing refused reached the waiter")
        reports.forget(rid)

    def test_refusals(self):
        reports = self.daemon.agent_pane_reports
        rid = reports.expect(2)
        self.owner("POST", "/agent-panes/report", self.body(rid, window="f" * 32), status=409)
        self.owner("POST", "/agent-panes/report", self.body(rid, placed=1), status=400)
        self.owner("POST", "/agent-panes/report?x=1", self.body(rid), status=400)
        self.owner("GET", "/agent-panes/report", status=405)
        self.owner("POST", "/agent-panes/other", self.body(rid), status=404)
        self.owner("POST", "/agent-panes/report", self.body(rid))
        self.owner("POST", "/agent-panes/report", self.body(rid, placed=0, not_placed=2), status=409)
        reports.forget(rid)


# --- through the real daemon and bridge ----------------------------------------------------


class Base(CS.Harness):
    def make_daemon(self):
        tool_module()
        return super().make_daemon()

    def finish(self, *threads):
        for thread in threads:
            if ProviderName(thread.provider) is not FAST:
                self.fakes[ProviderName(thread.provider)].hold.discard(thread.id)
                self.release(thread)
        for thread in threads:
            self.idle(thread)


class Binding(Base):
    def published(self):
        return [r for r in self.bus_records if r.get("kind") == "subagent_open"]

    def setUp(self):
        super().setUp()
        self.bus_records = []
        real = self.d.bus.publish

        def spy(record):
            self.bus_records.append(record)
            return real(record)
        patcher = patch.object(self.d.bus, "publish", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows_for(self, thread):
        real = self.d.subagents.list

        def listing(thread_id, **kw):
            if thread_id == thread.id:
                return [row(SID, "Scout", turn_id="x" * 32)]
            return real(thread_id, **kw)
        return patch.object(self.d.subagents, "list", side_effect=listing)

    def test_a_desk_turn_on_claude_and_codex_opens(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                self.bus_records.clear()
                thread = self.chat(provider)
                self.turn(thread)
                with self.rows_for(thread):
                    status, body = self.mcp(thread, NAME, {}, "parent")
                self.assertEqual(status, 200, body)
                self.assertFalse(body["isError"], body)
                self.assertEqual([r["data"] for r in self.published()], [{"sids": [SID]}])
                self.assertEqual(self.published()[0]["thread_id"], thread.id)
                self.finish(thread)

    def test_off_desk_turns_are_refused(self):
        for provider in (CLAUDE, CODEX):
            for label, message in (("discord", UserMessage("x", via="discord")),
                                   ("schedule", UserMessage("x", origin="system"))):
                with self.subTest(provider=provider.value, case=label):
                    self.bus_records.clear()
                    thread = self.chat(provider)
                    self.turn(thread, message)
                    with self.rows_for(thread):
                        status, body = self.mcp(thread, NAME, {}, "parent")
                    self.assertEqual(status, 200, body)
                    self.assertTrue(body["isError"] and is_refusal(body["text"]), body)
                    self.assertEqual(self.published(), [])
                    self.finish(thread)

    def test_the_tool_refuses_off_the_desk_and_at_depth_first(self):
        thread = self.chat(CLAUDE)
        self.turn(thread)

        def run(caller, desk=None, depth=0):
            def go():
                runtime.bind(depth=depth)
                if caller is not None:
                    runtime.bind(caller=caller)
                if desk is not None:
                    runtime.bind(desk=desk)
                return tool_module().hud_open_subagents()
            return contextvars.Context().run(go)
        with self.rows_for(thread), patch.object(self.d.subagent_panes, "open",
                                                 wraps=self.d.subagent_panes.open) as spy:
            for label, kw in (("off the desk", {"desk": {"present": False}}), ("no desk bound", {}),
                              ("a sub-agent", {"desk": {"present": True}, "depth": 1})):
                with self.subTest(case=label):
                    self.assertTrue(is_refusal(run(self.caller(thread), **kw)))
                    self.assertEqual(spy.call_count, 0, "refused in the tool, first")
            self.assertTrue(is_refusal(run(None, desk={"present": True})), "no caller")
            out = run(self.caller(thread), desk={"present": True})
            self.assertFalse(is_refusal(out), out)
        self.finish(thread)


if __name__ == "__main__":
    unittest.main(verbosity=1)
