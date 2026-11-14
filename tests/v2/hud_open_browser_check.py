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
window's `restore` path with the tab marked the agent's, never the owner's
navigation (`hud/src/lib/agentpanes.test.ts` "agentBrowserSpec", with its
mutation; `tests/face/hud_v2_agent_browser_check.py`: POST /browser/tabs
without a URL, then /restore, never /navigate). That `restore` never opens
the gate, with real Chrome, is `owner_browser_check.py`
`test_11c_stored_restore_never_opens_gate_or_hits_control_ports`.

**The chat may look at and drive what it opened (D5)** under the usual
switches, as an owner pane it navigated: `AgentTab` (the registry's
resolve and `_effective_drive` with every switch, the drive binding, the
look), with the source-list mutation below.

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
  `agent` left out of look/drive's kinds       AgentTab.test_the_drive_binding_accepts_it_as_an_owner_pane_and_still_refuses_unknown,
                                               AgentTab.test_the_look_accepts_it
  drive granted with a switch off              AgentTab.test_drive_is_refused_when_any_switch_is_off
  (review of #359)
  no drive-navigate pre-judge                  Parity.test_open_path_matches_drive_navigate (credential,
                                               account delete, OAuth, .exe), Review.test_a_credential_*
  no reviewer for CLASSIFY                     Review.test_an_ask_or_deny_from_the_reviewer_refuses_the_whole_call,
                                               Review.test_no_reviewer_model_means_no_page
  no per-turn cap                              Budget.test_one_turn_has_a_bound, Budget.test_the_turn_cap_*
  no hourly cap / wall clock                   Budget.test_the_hourly_cap_is_rolling_and_pinned,
                                               Budget.test_the_production_clock_is_monotonic
  D6: reading needs the switch                 AgentTab.test_with_agents_may_drive_off_the_chat_still_reads_it_*
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


def allow_review(seen=None):
    """A scripted drive-navigate reviewer that clears every address."""
    from jarvis.v2.browser import act_review

    def review(facts, owner_words=""):
        if seen is not None:
            seen.append(facts.nav_url)
        return act_review.ActVerdict(act_review.ALLOW, act_review.ORDINARY, "ok", "model")
    return review


def stub_service(session, resolve=None, windows=(), on_publish=None, review=None, clock=None):
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
    kw = {"clock": clock} if clock is not None else {}
    svc = browser_open.BrowserOpens(daemon, resolve=resolve or resolver(), review=review or allow_review(), **kw)
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


# --- parity with drive navigate's rules, and the budget (review of #359) ----------------------

from jarvis.v2.browser import act_rules  # noqa: E402

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
NAV_CASES = {
    # what drive navigate refuses (H2) or asks for (the ASK floors)
    "a credential in the query": (f"https://collector.example.net/c?t={GHP}", GHP),
    "an account-delete shape": ("https://www.example.com/account/delete?confirm=1", "account/delete"),
    "an OAuth authorize": ("https://github.com/login/oauth/authorize?client_id=abc&scope=repo", "oauth"),
    "an executable download": ("https://dl.example.net/setup.exe", "setup.exe"),
}


def drive_navigate_verdict(url):
    """What drive `navigate` decides first, from the same function it calls."""
    return act_rules.pre_judge(act_rules.Facts(
        action="navigate", nav_url=url, url="https://www.example.org/", card_pending=False, paused=False,
        owner_has_input=False, owner_input_age_s=None, agents_may_drive=True, pane_can_drive=True,
        pane_can_see=True, private=False, tab_drawn=True, direct_mode=False, url_refused=False,
        other_driver=False, judged_this_turn=0, actions_this_hour=0, strict_profile=False, at_desk=True,
        depth=0, sub_agent=False))


class Parity(unittest.TestCase):
    def test_open_path_matches_drive_navigate(self):
        for label, (url, secretish) in NAV_CASES.items():
            with self.subTest(case=label):
                verdict = drive_navigate_verdict(url)
                self.assertIn(verdict.kind, (act_rules.REFUSE, act_rules.ASK), verdict)
                svc, published = stub_service(stub_session())
                out = svc.open(dict(CALLER), [IG, url])
                self.assertTrue(is_refusal(out), out)
                self.assertEqual(published, [], "hud_open_browser published what drive navigate refuses or asks")
                self.assertNotIn(secretish, out, "a refused URL is never echoed back")


class Review(unittest.TestCase):
    """CLASSIFY (every ordinary address) goes to drive navigate's own reviewer;
    anything but its allow refuses the call, and nothing asks the owner."""

    def test_the_reviewer_sees_each_classified_address(self):
        seen = []
        svc, published = stub_service(stub_session(), review=allow_review(seen))
        out = svc.open(dict(CALLER), [IG, "https://www.instagram.com/explore/"])
        self.assertFalse(is_refusal(out), out)
        self.assertEqual(sorted(seen), sorted([IG, "https://www.instagram.com/explore/"]))
        self.assertEqual(len(published), 1)

    def test_an_ask_or_deny_from_the_reviewer_refuses_the_whole_call(self):
        from jarvis.v2.browser import act_review
        for said in (act_review.ASK, act_review.DENY):
            with self.subTest(said=said):
                def review(facts, owner_words=""):
                    if "explore" in facts.nav_url:
                        return act_review.ActVerdict(said, act_review.ORDINARY, "no", "model")
                    return act_review.ActVerdict(act_review.ALLOW, act_review.ORDINARY, "ok", "model")
                svc, published = stub_service(stub_session(), review=review)
                out = svc.open(dict(CALLER), [IG, "https://www.instagram.com/explore/"])
                self.assertTrue(out.startswith("Error: URL 2 was refused"), out)
                self.assertIn("owner's OK", out)
                self.assertNotIn("explore", out)
                self.assertEqual(published, [])

    def test_no_reviewer_model_means_no_page(self):
        # The production reviewer with no model configured answers ask: refused, never published.
        svc, published = stub_service(stub_session())
        svc.review = None
        with patch("jarvis.v2.browser.act_review._model", return_value=None):
            out = svc.open(dict(CALLER), [IG])
        self.assertTrue(is_refusal(out), out)
        self.assertEqual(published, [])

    def test_a_credential_address_never_reaches_the_reviewer_or_dns(self):
        seen, resolve = [], resolver()
        svc, published = stub_service(stub_session(), resolve=resolve, review=allow_review(seen))
        out = svc.open(dict(CALLER), [NAV_CASES["a credential in the query"][0]])
        self.assertTrue(is_refusal(out), out)
        self.assertEqual((seen, resolve.seen, published), ([], [], []))


class Budget(unittest.TestCase):
    def test_one_turn_has_a_bound(self):
        svc, published = stub_service(stub_session())
        urls = [f"https://site{i}.example.net/" for i in range(11)]
        for _ in range(40):
            svc.open(dict(CALLER), urls)
        n = sum(len(r["data"]["urls"]) for r in published)
        self.assertLessEqual(n, browser_open.TURN_MAX)
        self.assertEqual(browser_open.TURN_MAX, 11)

    def test_the_turn_cap_counts_across_calls_and_a_new_turn_starts_fresh(self):
        session = stub_session()
        svc, published = stub_service(session)
        self.assertFalse(is_refusal(svc.open(dict(CALLER), [IG] * 6)))
        out = svc.open(dict(CALLER), [IG] * 6)
        self.assertTrue(out.startswith("Error: one turn opens at most 11"), out)
        self.assertFalse(is_refusal(svc.open(dict(CALLER), [IG] * 5)), "6 + 5 is the cap exactly")
        self.assertTrue(is_refusal(svc.open(dict(CALLER), [IG])))
        session.turn_id = "n" * 32
        self.assertFalse(is_refusal(svc.open({**CALLER, "turn_id": "n" * 32}, [IG])), "a new turn")
        self.assertEqual(sum(len(r["data"]["urls"]) for r in published), 12)

    def test_a_refused_call_charges_nothing(self):
        svc, published = stub_service(stub_session())
        self.assertTrue(is_refusal(svc.open(dict(CALLER), [IG] * 10 + ["http://localhost:8402/"])))
        self.assertFalse(is_refusal(svc.open(dict(CALLER), [IG] * 11)), "the refused call used none of the turn")

    def test_the_hourly_cap_is_rolling_and_pinned(self):
        self.assertEqual((browser_open.HOUR_MAX, browser_open.HOUR_S), (44, 3600.0))
        now = [1000.0]
        svc, published = stub_service(stub_session(), clock=lambda: now[0])
        session = svc.daemon._sessions["ab12cd34"]
        for n in range(4):
            session.turn_id = f"{n:032x}"
            svc.open({**CALLER, "turn_id": f"{n:032x}"}, [IG] * 11)
        self.assertEqual(sum(len(r["data"]["urls"]) for r in published), 44)
        session.turn_id = f"{4:032x}"
        out = svc.open({**CALLER, "turn_id": f"{4:032x}"}, [IG])
        self.assertTrue(out.startswith("Error: at most 44 Browser pages"), out)
        now[0] += 3600.0
        self.assertFalse(is_refusal(svc.open({**CALLER, "turn_id": f"{4:032x}"}, [IG])), "an hour later")

    def test_the_production_clock_is_monotonic(self):
        import time
        self.assertIs(browser_open.BrowserOpens(SimpleNamespace()).clock, time.monotonic)

    # Delta review of #359: refused calls used to pay every reviewer call and
    # charge nothing, and one refused URL left its siblings looked up and reviewed.

    @staticmethod
    def _deny_some(calls):
        from jarvis.v2.browser import act_review
        lock = threading.Lock()

        def review(facts, owner_words=""):
            with lock:
                calls.append(facts.nav_url)
            verdict = act_review.DENY if "denied" in facts.nav_url else act_review.ALLOW
            return act_review.ActVerdict(verdict, act_review.ORDINARY, "x", "model")
        return review

    def test_review_budget_is_pinned(self):
        self.assertEqual((browser_open.REVIEWS_TURN_MAX, browser_open.REVIEWS_HOUR_MAX), (22, 88))

    def test_refused_calls_still_spend_a_bounded_number_of_reviews(self):
        calls = []
        svc, published = stub_service(stub_session(), review=self._deny_some(calls))
        urls = [f"https://site{i}.example.net/" for i in range(10)] + ["https://denied.example.net/"]
        outs = [svc.open(dict(CALLER), urls) for _ in range(30)]
        self.assertEqual(published, [])
        self.assertTrue(all(is_refusal(o) for o in outs))
        self.assertLessEqual(len(calls), browser_open.REVIEWS_TURN_MAX)
        self.assertTrue(outs[-1].startswith("Error: too many Browser pages have been checked"), outs[-1])

    def test_a_url_the_rules_refuse_means_no_lookup_and_no_review_for_any(self):
        calls, seen = [], []

        def resolve(name, port):
            seen.append(name)
            return [PUBLIC_IP]
        svc, published = stub_service(stub_session(), resolve=resolve, review=self._deny_some(calls))
        urls = [f"https://s{i}.example.net/" for i in range(10)] + ["https://ex.example.net/account/delete"]
        out = svc.open(dict(CALLER), urls)
        self.assertTrue(out.startswith("Error: URL 11 was refused"), out)
        self.assertEqual((seen, calls, published), ([], [], []))

    def test_a_failed_lookup_means_no_review(self):
        calls = []
        svc, published = stub_service(stub_session(), resolve=resolver({"bad.example.net": ("10.0.0.5",)}),
                                      review=self._deny_some(calls))
        out = svc.open(dict(CALLER), [IG, "https://bad.example.net/"])
        self.assertTrue(out.startswith("Error: URL 2 was refused"), out)
        self.assertEqual((calls, published), ([], []))

    def test_the_review_hour_is_rolling_across_turns(self):
        now = [1000.0]
        calls = []
        svc, published = stub_service(stub_session(), clock=lambda: now[0], review=self._deny_some(calls))
        session = svc.daemon._sessions["ab12cd34"]
        urls = [IG] * 10 + ["https://denied.example.net/"]
        for n in range(8):
            session.turn_id = f"{n:032x}"
            self.assertTrue(svc.open({**CALLER, "turn_id": f"{n:032x}"}, urls).startswith("Error: URL 11"))
        self.assertEqual(len(calls), 88)
        session.turn_id = f"{8:032x}"
        out = svc.open({**CALLER, "turn_id": f"{8:032x}"}, [IG])
        self.assertTrue(out.startswith("Error: too many Browser pages have been checked"), out)
        self.assertEqual(len(calls), 88)
        now[0] += 3600.0
        self.assertFalse(is_refusal(svc.open({**CALLER, "turn_id": f"{8:032x}"}, [IG])), "an hour later")


# --- the agent's tab in the pane registry, the look and the drive (decision D5) ---------------

from copy import deepcopy  # noqa: E402

from jarvis.v2.browser import drive as drive_mod, look as look_mod  # noqa: E402
from jarvis.v2.browser.panes import BrowserPanes, PaneRefused  # noqa: E402

PW, PROFILE, TAB, LOCAL_ID = "a" * 32, "p-0123abcd", "0123abcd", "abcdef"


class AgentTab(unittest.TestCase):
    """An agent-opened Remote tab (`source {kind: "agent"}`) resolves for a
    look and is driven under exactly the switches an owner pane is: the
    pane's see and drive, Remote, and `agents_may_drive`."""

    def setUp(self):
        self.flags = {"agents_may_drive": True}
        self.rows = {TAB: {"tab_id": TAB, "profile": PROFILE, "ctx": None, "url": IG,
                           "page_identity": 1, "document_generation": 1}}
        self.host = SimpleNamespace(pane_tab=lambda tid: deepcopy(self.rows.get(tid)),
                                    refused_ports=lambda: REFUSED_PORTS, input_held=lambda: False,
                                    loopback_open=lambda port: False)
        settings = SimpleNamespace(value=lambda key: self.flags.get(key))
        self.daemon = SimpleNamespace(browser=self.host, browser_settings=settings,
                                      devservers=SimpleNamespace(source_snapshot=lambda entry: None),
                                      terminals=SimpleNamespace(readable_source=lambda tid: False))
        self.registry = BrowserPanes(self.daemon, clock=lambda: 1.0)
        self.daemon.browser_panes = self.registry
        self.registry.window_started(PW)
        self.seq = 0

    def report(self, *, see=True, drive=True, mode="remote", source=None):
        self.seq += 1
        tab = ({"id": LOCAL_ID, "mode": "remote", "tab_id": TAB, "source": source}
               if mode == "remote" else
               {"id": LOCAL_ID, "mode": "direct", "url": "http://localhost:23456/", "source": source})
        return self.registry.report({
            "window": PW, "seq": self.seq, "focus_seq": self.seq, "focused_slot": 1, "covered": False,
            "panes": [{"pane_id": 2, "slot": 1, "drawn": True, "profile": PROFILE, "ctx": None, "see": see,
                       "drive": drive, "active": LOCAL_ID, "tabs": [tab]}]})

    def test_resolves_as_the_agents_with_drive_under_the_usual_switches(self):
        receipt = self.report(source={"kind": "agent"})
        snap = self.registry.resolve("1")
        self.assertEqual((snap.source.kind, snap.mode, snap.see, snap.drive), ("agent", "remote", True, True))
        self.assertTrue(receipt["panes"][0]["drive"])
        pane = self.registry._windows[PW].panes[2]
        self.assertTrue(self.registry._effective_drive(pane))

    def test_drive_is_refused_when_any_switch_is_off(self):
        for label, kw, flag in (("pane drive off", {"drive": False}, True),
                                ("pane see off", {"see": False}, True),
                                ("agents_may_drive off", {}, False),
                                ("Direct, not Remote", {"mode": "direct"}, True)):
            with self.subTest(case=label):
                self.flags["agents_may_drive"] = flag
                self.report(source={"kind": "agent"}, **kw)
                pane = self.registry._windows[PW].panes[2]
                self.assertFalse(self.registry._effective_drive(pane))
                self.assertFalse(self.registry.resolve("1").drive)
        self.flags["agents_may_drive"] = True
        self.report(source={"kind": "agent"})
        self.assertTrue(self.registry.resolve("1").drive, "all on again: drive")

    def test_a_later_report_never_turns_it_into_the_owners(self):
        self.report(source={"kind": "agent"})
        self.report(source=None)
        self.assertEqual(self.registry.resolve("1").source.kind, "agent")

    def test_a_forged_agent_source_is_refused(self):
        for bad in ({"kind": "agent", "thread_id": "ab12cd34"}, {"kind": "agent", "owner": True},
                    {"kind": "Agent"}):
            with self.subTest(source=bad):
                with self.assertRaises(PaneRefused):
                    self.report(source=bad)

    def test_the_drive_binding_accepts_it_as_an_owner_pane_and_still_refuses_unknown(self):
        self.report(source={"kind": "agent"})
        snap = self.registry.resolve("1")
        stub = SimpleNamespace(daemon=self.daemon)
        stub._url = lambda url: drive_mod.BrowserDrive._url(stub, url)
        self.assertEqual(drive_mod.BrowserDrive._binding(stub, snap).source.kind, "agent")
        self.flags["agents_may_drive"] = False
        self.report(source={"kind": "agent"})
        with self.assertRaises(drive_mod.LookRefused):
            drive_mod.BrowserDrive._binding(stub, self.registry.resolve("1"))
        self.flags["agents_may_drive"] = True
        self.rows[TAB] = dict(self.rows[TAB])
        unknown = BrowserPanes(self.daemon, clock=lambda: 1.0)
        self.daemon.browser_panes = unknown
        unknown.window_started(PW)
        self.registry = unknown
        self.report(source={"kind": "unknown"})
        with self.assertRaises(drive_mod.LookRefused):
            drive_mod.BrowserDrive._binding(stub, unknown.resolve("1"))

    def test_with_agents_may_drive_off_the_chat_still_reads_it_but_never_drives_it(self):
        """D6: opening and reading are separate from driving. With the global
        switch off the page is still opened (the open path never reads it) and
        still read (the pane's see), and never clicked or typed into."""
        self.flags["agents_may_drive"] = False
        self.report(source={"kind": "agent"})
        snap = self.registry.resolve("1")
        self.assertEqual((snap.see, snap.drive), (True, False))
        look = look_mod.BrowserLook(self.daemon)

        def go():
            runtime.bind(depth=0, desk={"present": True})
            look._current(snap)
            return True
        self.assertTrue(contextvars.Context().run(go), "the look needs see, not drive")
        stub = SimpleNamespace(daemon=self.daemon)
        stub._url = lambda url: drive_mod.BrowserDrive._url(stub, url)
        with self.assertRaises(drive_mod.LookRefused):
            drive_mod.BrowserDrive._binding(stub, snap)
        # And opening does not ask the switch at all.
        svc, published = stub_service(stub_session())
        svc.daemon.browser_settings = SimpleNamespace(value=lambda key: False)
        self.assertFalse(is_refusal(svc.open(dict(CALLER), [IG])))
        self.assertEqual(len(published), 1)

    def test_the_look_accepts_it(self):
        self.report(source={"kind": "agent"})
        snap = self.registry.resolve("1")
        look = look_mod.BrowserLook(self.daemon)

        def go():
            runtime.bind(depth=0, desk={"present": True})
            look._current(snap)
            return True
        self.assertTrue(contextvars.Context().run(go))


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
        self.d.browser_opens.review = allow_review()

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
