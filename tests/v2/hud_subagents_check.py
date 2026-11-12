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
from jarvis import config, runtime, tools  # noqa: E402
config.CLAUDE_SKILLS_DIR = None  # never read the owner's real ~/.claude/skills
from jarvis.v2 import bridge, session_tools, subagent_panes  # noqa: E402
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


def stub_service(session, rows_by_thread):
    published = []
    seen = []

    def listing(thread_id):
        seen.append(thread_id)
        return [dict(r) for r in rows_by_thread.get(thread_id, [])]
    daemon = SimpleNamespace(_lock=threading.RLock(), _stopping=False, _sessions={"ab12cd34": session},
                             bus=SimpleNamespace(publish=published.append),
                             subagents=SimpleNamespace(list=listing))
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
        self.assertEqual(record["data"], {"sids": [sid(1)]})
        self.assertNotIn("SENTINEL", repr(record))

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
