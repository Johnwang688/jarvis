"""`hud_open_browser` (owner, 2026-11-13, decisions D2-D4 of
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`): a chat opens public
web pages in new Remote Browser panes of the owner's HUD. The tool reaches
`browser_open.py` and nothing else; every URL is judged daemon-side first
and one refusal refuses the whole call; it publishes one `browser_open` bus
record and waits for the window's counts (`agent_panes.py`).

Bound as `hud_open_subagents` is (`hud_subagents_check.py` is the model):
Claude and Codex owner chats, foreground-only, desk-only, not read-only;
checked in the tool and again by the service under the daemon lock.

**It never admits a loopback port.** Two halves: loopback, LAN and Jarvis
URLs are refused here (`Urls`), and the HUD opens each page through the
window's `restore` path with the tab marked not the owner's
(`hud/src/lib/agentpanes.test.ts` "agentBrowserSpec", with its mutation;
`tests/face/hud_v2_agent_browser_check.py`: POST /browser/tabs without a URL,
then /restore, never /navigate). That `restore` never opens the gate, with
real Chrome, is `owner_browser_check.py`
`test_11c_stored_restore_never_opens_gate_or_hits_control_ports`.

Free and hermetic: no DNS (every resolver here is a stand-in), every daemon
on port 0 over a temporary V2_DATA_DIR and temporary config.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \\
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \\
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \\
    .venv/bin/python tests/v2/hud_open_browser_check.py

TEST -> MUTATION
================
  bind it to the fast path                     Contract.test_the_fast_path_never_holds_it
  not foreground-only (Claude sub-agent)       ClaudeHook.test_a_sub_agent_is_denied_without_asking_permit
  no desk check in the tool                    Binding.test_the_tool_refuses_off_the_desk_and_at_depth_first
  no desk check in the service                 ServiceChecks.test_the_service_refuses_what_owner_post_refuses
  no recheck after the lookups                 ServiceChecks.test_a_turn_stopped_during_the_lookups_publishes_nothing
  loopback / LAN / Jarvis URL accepted         Urls.test_refused_urls
  a name resolving to a private address        Urls.test_a_name_must_resolve_to_public_addresses_only
  one bad URL publishes the good ones          Urls.test_one_refusal_refuses_the_whole_call
  words or names on the bus                    Publish.test_the_bus_carries_the_judged_urls_and_an_id_only
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
import hud_subagents_check as HS  # noqa: E402  (helpers only, as a module: none of its tests run here)
import mcp_session_check as M  # noqa: E402  (helpers only; not its setUpModule)
from jarvis import config, runtime, tools  # noqa: E402
config.CLAUDE_SKILLS_DIR = None  # never read the owner's real ~/.claude/skills
from jarvis.v2 import agent_panes, bridge, browser_open, session_tools  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role  # noqa: E402
from jarvis.v2.provider import Brief, Decision, UserMessage  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
NAME = "hud_open_browser"
PUBLIC_IP = "93.184.216.34"
IG = "https://www.instagram.com/"
WINDOW = HS.WINDOW
CALLER = dict(HS.CALLER)
REFUSED_PORTS = frozenset({8402, 8403, 8404, 8405, 8406, 8408, 48200})
is_refusal = HS.is_refusal


def tool_module():
    from jarvis.v2.tools import hud_open_browser
    return hud_open_browser


def resolver(table=None, default=(PUBLIC_IP,)):
    """A stand-in for DNS: `table` name -> addresses (an Exception to raise)."""
    table = table or {}
    seen = []

    def resolve(name, port):
        seen.append((name, port))
        got = table.get(name, list(default))
        if isinstance(got, Exception):
            raise got
        return list(got)
    resolve.seen = seen
    return resolve


def judge(url, resolve=None):
    return browser_open.judge_public_url(url, refused=REFUSED_PORTS, resolve=resolve or resolver())


class Contract(unittest.TestCase):
    def test_schema_is_generated_from_annotated_hints(self):
        module = tool_module()
        entry = tools.REGISTRY[NAME]
        self.assertIs(entry.func, module.hud_open_browser)
        self.assertEqual(entry.schema["required"], ["urls"])
        self.assertEqual(entry.schema["properties"]["urls"]["items"], {"type": "string"})
        self.assertFalse(entry.schema["additionalProperties"])

    def test_flags_explicit_only_not_parallel_not_dangerous_not_external(self):
        tool_module()
        from jarvis.v2 import mcp, mcp_catalog
        self.assertIn(NAME, tools.EXPLICIT_ONLY)
        self.assertNotIn(NAME, tools.PARALLEL_SAFE)
        self.assertFalse(tools.REGISTRY[NAME].dangerous)
        self.assertNotIn(NAME, mcp_catalog.MCP_TOOLS)
        self.assertNotIn(NAME, mcp.MCP_TOOLS)
        self.assertNotIn(NAME, tools.default_names(), "no v1 surface holds it")

    def test_session_catalog_binds_it_as_hud_open_subagents(self):
        entry = session_tools.SESSION_TOOLS[NAME]
        model = session_tools.SESSION_TOOLS["hud_open_subagents"]
        self.assertEqual((entry.providers, entry.foreground_only, entry.desk_only, entry.read_only,
                          entry.owner_gated),
                         (model.providers, model.foreground_only, model.desk_only, model.read_only,
                          model.owner_gated))
        self.assertEqual(entry.providers, frozenset({"claude", "codex"}))
        self.assertTrue(entry.foreground_only and entry.desk_only)
        self.assertFalse(entry.read_only or entry.owner_gated)
        for provider in (CLAUDE, CODEX):
            self.assertIn(NAME, {t.name for t in session_tools.for_provider(provider)})
        listed = {t["name"]: t for t in bridge.listing(CLAUDE)}[NAME]
        self.assertEqual(listed["annotations"], {"readOnlyHint": False})
        self.assertNotIn(NAME, session_tools.read_only_claude_names())

    def test_the_fast_path_never_holds_it(self):
        from jarvis.v2.providers import fastpath
        for group in (fastpath.FAST_TOOLS, fastpath.DESK_TOOLS, fastpath.EDIT_TOOLS, fastpath.CHAT_TOOLS):
            self.assertNotIn(NAME, group)

    def test_no_prompt_paragraph_names_it(self):
        self.assertNotIn(NAME, config.SYSTEM_PROMPT)


class Strict(unittest.TestCase):
    setUp, brief, decisions = CS.Strict.setUp, CS.Strict.brief, CS.Strict.decisions

    def test_strict_refuses_auto_and_ask_delegate(self):
        tool_module()
        for provider, prefix in (("claude", session_tools.CLAUDE_PREFIX), ("codex", "")):
            with self.subTest(provider=provider, profile="strict"):
                brief = self.brief(PermissionProfile.STRICT)
                asker = M.Asker(Decision.ALLOW)
                permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider), asker)
                self.assertEqual(permit(prefix + NAME, {"urls": [IG]}, brief), Decision.DENY)
                self.assertEqual(asker.seen, [])
            for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
                with self.subTest(provider=provider, profile=profile.value):
                    brief = self.brief(profile)
                    asker = M.Asker(Decision.DENY)
                    permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider), asker)
                    self.assertEqual(permit(prefix + NAME, {"urls": [IG]}, brief), Decision.ALLOW)
                    self.assertEqual(asker.seen, [])


class ClaudeHook(unittest.TestCase):
    setUp, start, hook, counted_permit = (CS.ClaudeHook.setUp, CS.ClaudeHook.start, CS.ClaudeHook.hook,
                                          CS.ClaudeHook.counted_permit)

    def test_a_sub_agent_is_denied_without_asking_permit(self):
        tool_module()
        asker = M.Asker(Decision.ALLOW)
        permit, calls = self.counted_permit(asker)
        handle = self.start(permit, "hsub0002")
        out = self.hook(handle, session_tools.CLAUDE_PREFIX + NAME, agent_id="sub-1")
        self.assertEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(calls, [], "permit is never asked")


# --- the URL judge ----------------------------------------------------------------------------


class Urls(unittest.TestCase):
    def refused(self, url, resolve=None):
        with self.assertRaises(browser_open.NotPublic, msg=repr(url)[:80]) as caught:
            judge(url, resolve)
        self.assertNotIn(str(url)[:40] or "\0", str(caught.exception), "a refusal never repeats the URL")
        return caught.exception

    def test_public_pages_pass_as_judged(self):
        self.assertEqual(judge(IG), IG)
        self.assertEqual(judge("http://93.184.216.34/x?y=1"), "http://93.184.216.34/x?y=1")
        self.assertEqual(judge("  https://instagram.com/explore  "), "https://instagram.com/explore")

    def test_refused_urls(self):
        for url in (
                # this machine, every spelling, Jarvis's ports and others
                "http://localhost:8402/", "http://localhost:3000/", "http://127.0.0.1/", "http://127.0.0.1:8405/",
                "http://127.1.2.3/", "http://0.0.0.0:5173/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                "http://2130706433/", "http://0x7f.1/", "http://app.localhost/", "http://localhost./",
                # the home network, link-local, cloud metadata, CGNAT, unique-local
                "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/",
                "http://100.64.0.1/", "http://[fd00::1]/", "http://[fe80::1]/",
                # names that never mean a public site
                "http://router/", "http://nas.local/", "http://printer.lan/", "http://x.internal/",
                "http://box.home.arpa/",
                # a Jarvis port on any host
                "https://www.instagram.com:8402/", "http://93.184.216.34:8408/",
                # other schemes
                "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "ftp://example.com/",
                "chrome://settings", "about:blank", "view-source:https://instagram.com/",
                # redirect-shaped and two-parser tricks
                "https://instagram.com@127.0.0.1:8402/", "https://user:pw@instagram.com/",
                "http://127.0.0.1\\@instagram.com/", "http://instagram.com\\@127.0.0.1/",
                # malformed
                "", "   ", "instagram.com", "https://", "https://instagram.com/‮", "https://ig.com/\x00",
                "https://" + "a" * 2050 + ".com/", 5, None):
            with self.subTest(url=str(url)[:60]):
                self.refused(url)

    def test_a_name_must_resolve_to_public_addresses_only(self):
        for addresses in (["127.0.0.1"], ["10.0.0.7"], [PUBLIC_IP, "192.168.0.2"], ["::1"], ["fe80::1%eth0"],
                          ["169.254.169.254"], [], ["not an address"]):
            with self.subTest(addresses=addresses):
                self.refused("https://rebind.example.org/", resolver({"rebind.example.org": addresses}))
        self.refused("https://gone.example.org/", resolver({"gone.example.org": OSError("no such name")}))
        seen = resolver({"ok.example.org": [PUBLIC_IP, "2606:2800:220:1:248:1893:25c8:1946"]})
        self.assertEqual(judge("https://ok.example.org/", seen), "https://ok.example.org/")
        self.assertEqual(seen.seen, [("ok.example.org", 443)])

    def test_a_slow_lookup_is_refused_in_time(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(name, port):
            gate.wait(5)
            return [PUBLIC_IP]
        with self.assertRaises(browser_open.NotPublic):
            browser_open.judge_public_url(IG, refused=REFUSED_PORTS, resolve=slow, timeout=0.05)

    def test_one_refusal_refuses_the_whole_call(self):
        svc, published = stub_service(stub_session(), resolve=resolver())
        out = svc.open(dict(CALLER), [IG, "http://localhost:8402/", "https://example.org/"])
        self.assertTrue(out.startswith("Error: URL 2 was refused, so nothing was opened"), out)
        self.assertNotIn("localhost", out)
        self.assertEqual(published, [])


# --- the service, on a stub daemon ------------------------------------------------------------


def stub_session(**over):
    return HS.stub_session(**over)


def stub_service(session, resolve=None, windows=(), on_publish=None):
    published = []
    live = set(windows)

    def publish(record):
        published.append(record)
        if on_publish is not None:
            on_publish(record, svc.reports)
    daemon = SimpleNamespace(_lock=threading.RLock(), _stopping=False, _sessions={"ab12cd34": session},
                             bus=SimpleNamespace(publish=publish),
                             browser=SimpleNamespace(refused_ports=lambda: REFUSED_PORTS),
                             hud_window_count=lambda: len(live), hud_window_live=lambda w: w in live)
    svc = browser_open.BrowserOpens(daemon, resolve=resolve or resolver())
    svc.closed = False
    return svc, published


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
                "retired": ({"retired": True}, False),
                "no handle": ({"handle": None}, False)}
        for label, (over, ok) in rows.items():
            with self.subTest(case=label):
                svc, published = stub_service(stub_session(**over))
                out = svc.open(dict(CALLER), [IG])
                self.assertEqual(not is_refusal(out), ok, out)
                self.assertEqual(len(published), 1 if ok else 0)

    def test_a_turn_stopped_during_the_lookups_publishes_nothing(self):
        session = stub_session()

        def stop_mid_lookup(name, port):
            session.cancelled.set()
            return [PUBLIC_IP]
        svc, published = stub_service(session, resolve=stop_mid_lookup)
        out = svc.open(dict(CALLER), [IG])
        self.assertTrue(is_refusal(out), out)
        self.assertEqual(published, [])

    def test_a_stopped_service_a_bad_caller_and_bad_urls_refuse(self):
        svc, published = stub_service(stub_session())
        for caller in (None, {}, {"thread_id": "ab12cd34"}, {"thread_id": 1, "turn_id": "t"}):
            self.assertTrue(is_refusal(svc.open(caller, [IG])))
        for urls in (None, [], "https://x.org/", [IG] * 12, [IG, ""], [IG, 5]):
            with self.subTest(urls=str(urls)[:40]):
                self.assertTrue(is_refusal(svc.open(dict(CALLER), urls)))
        svc.closed = True
        self.assertTrue(is_refusal(svc.open(dict(CALLER), [IG])))
        self.assertEqual(published, [])

    def test_the_lookups_run_outside_the_daemon_lock(self):
        held = []

        def probe(name, port):
            got = svc.daemon._lock.acquire(timeout=1)
            held.append(got)
            if got:
                svc.daemon._lock.release()
            return [PUBLIC_IP]
        svc, published = stub_service(stub_session(), resolve=lambda n, p: probe(n, p))
        # The lookup runs on a pool thread: a lock held by the calling thread would block it.
        out = svc.open(dict(CALLER), [IG])
        self.assertEqual(held, [True], out)


class Publish(unittest.TestCase):
    def test_the_bus_carries_the_judged_urls_and_an_id_only(self):
        svc, published = stub_service(stub_session(), windows=[WINDOW], on_publish=HS.answer(2, 0))
        out = svc.open(dict(CALLER), ["  https://www.instagram.com/  ", "https://www.instagram.com/explore/"])
        record = published[0]
        self.assertEqual(set(record), {"kind", "thread_id", "at", "data"})
        self.assertEqual((record["kind"], record["thread_id"]), ("browser_open", "ab12cd34"))
        self.assertEqual(set(record["data"]), {"urls", "request_id"})
        self.assertEqual(record["data"]["urls"], [IG, "https://www.instagram.com/explore/"])
        self.assertRegex(record["data"]["request_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(out.startswith("Opened 2 of 2 Browser pane(s) at instagram.com"), out)

    def test_partial_timeout_and_no_window(self):
        svc, _ = stub_service(stub_session(), windows=[WINDOW], on_publish=HS.answer(1, 2))
        out = svc.open(dict(CALLER), [IG, "https://a.example.org/", "https://b.example.org/"])
        self.assertTrue(out.startswith("Opened 1 of 3"), out)
        self.assertIn("no room for 2", out)
        svc, published = stub_service(stub_session(), windows=[WINDOW])
        svc.reports.clock = HS.FastClock()
        out = svc.open(dict(CALLER), [IG])
        self.assertIn("No HUD window replied within 3 seconds", out)
        svc, published = stub_service(stub_session())
        out = svc.open(dict(CALLER), [IG])
        self.assertIn("No HUD window is open", out)
        self.assertNotIn("request_id", published[0]["data"])

    def test_it_shares_the_daemons_report_waiter(self):
        daemon = SimpleNamespace(agent_pane_reports=agent_panes.PaneReports(SimpleNamespace()))
        self.assertIs(browser_open.BrowserOpens(daemon).reports, daemon.agent_pane_reports)


# --- through the real daemon and bridge ------------------------------------------------------


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
        return [r for r in self.bus_records if r.get("kind") == "browser_open"]

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
        self.d.browser_opens.resolve = resolver()

    def test_the_daemon_installs_it_and_stops_it(self):
        self.assertIs(browser_open.installed(), self.d.browser_opens)
        self.assertIs(self.d.browser_opens.reports, self.d.agent_pane_reports)

    def test_a_desk_turn_on_claude_and_codex_opens(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                self.bus_records.clear()
                thread = self.chat(provider)
                self.turn(thread)
                status, body = self.mcp(thread, NAME, {"urls": [IG]}, "parent")
                self.assertEqual(status, 200, body)
                self.assertFalse(body["isError"], body)
                self.assertEqual([r["data"] for r in self.published()], [{"urls": [IG]}])
                self.assertEqual(self.published()[0]["thread_id"], thread.id)
                self.finish(thread)

    def test_a_refused_url_through_the_bridge_publishes_nothing(self):
        thread = self.chat(CLAUDE)
        self.turn(thread)
        status, body = self.mcp(thread, NAME, {"urls": [IG, "http://127.0.0.1:8402/"]}, "parent")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["isError"] and is_refusal(body["text"]), body)
        self.assertEqual(self.published(), [])
        self.finish(thread)

    def test_off_desk_turns_are_refused(self):
        for provider in (CLAUDE, CODEX):
            for label, message in (("discord", UserMessage("x", via="discord")),
                                   ("phone", UserMessage("x", via="phone")),
                                   ("schedule", UserMessage("x", origin="system"))):
                with self.subTest(provider=provider.value, case=label):
                    self.bus_records.clear()
                    thread = self.chat(provider)
                    self.turn(thread, message)
                    status, body = self.mcp(thread, NAME, {"urls": [IG]}, "parent")
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
                return tool_module().hud_open_browser([IG])
            return contextvars.Context().run(go)
        with patch.object(self.d.browser_opens, "open", wraps=self.d.browser_opens.open) as spy:
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
