"""C4 of the chat-to-chat plan: the `chat_send` and `chat_wait` tools.

Plan `docs/plans/2026-10-19-chat-to-chat-plan.md` §4 (all), §5.1, §5.3 "The
explicit wait", §5.4, §6 rows 1 and 6, and §8 "C4"; the decisions file beside
it wins, with the lead's C4 brief applied:

- CC-6: no `typed` slot key and no `runtime.owner_typed()`. `chat_send`
  refuses first with `not runtime.at_desk() or runtime.depth() != 0`,
  `chat_wait` with `runtime.depth() != 0`. A spoken HUD desk turn may send
  (a positive case here); every "spoken"/"typed" refusal row is dropped.
- CC-7: no `RELAY_REFUSED`. A relayed or reply turn is never at the desk, so
  `chat_send` is absent from a fast relayed turn through `DESK_TOOLS` and is
  refused by the tool and the service on Claude and Codex.

Free and hermetic: every daemon runs on port 0 over a temporary V2_DATA_DIR
and temporary config (never 8402/8403/8405, never the owner's state, never a
real ~/.claude or ~/.codex). The fast path is the real `FastPathProvider`
with `llm.chat` scripted; Claude and Codex chats are the scripted `Fake` of
`chat_relay_check.py`, and the Codex fake admits foreground calls through the
real `CodexProvider.admit_foreground_mcp` over a fake native handle. The
daemon's permit is the real §6 permit (`permit_factory=None`). The relay's
clock is a fake, so a wait never times out on its own.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/chat_send_check.py

Each case is its own unittest method (rows of one matrix are subTests): a
missing tool or flag on today's code fails that case alone, and the runner
goes on.


THE INTERFACE ASSUMED (plan §4.1, §4.2, §4.4; brief overrides 1-6)
=================================================================

`jarvis/v2/tools/chats.py`, registered with `@tool`:

    chat_send(
        chat: Annotated[str, "The chat to message: its id from chat_list, or its exact title"],
        message: Annotated[str, "What to ask or tell it, complete in itself: it cannot see this conversation (at most 4000 characters)"],
    ) -> str                      # -> ChatRelay.send(runtime.caller(), chat, message)
    chat_wait(
        message_id: Annotated[str, "The id chat_send returned"],
        seconds: Annotated[int, "Longest to wait, in seconds (default 120, at most 300)"] = 120,
    ) -> str                      # -> ChatRelay.wait(runtime.caller(), message_id, seconds)

Both EXPLICIT_ONLY, not dangerous, not PARALLEL_SAFE, not in
`mcp_catalog.MCP_TOOLS`; `chat_wait` in PRIVATE_RESULT (it returns another
chat's words). A refusal starts "Error:" or "Refused:".
`fastpath.DESK_TOOLS` holds `chat_send`; `fastpath.CHAT_TOOLS` holds `chat_wait`.
`session_tools.SESSION_TOOLS`: chat_send (claude+codex, foreground_only,
desk_only, not read-only), chat_wait (claude+codex, foreground_only, not
desk-only, not read-only). The bridge's `request_alive` check covers
`chat_wait` (a closed relay socket ends the wait).
`roles._CHAT` gains: "A message from another HUD chat arrives fenced and
labelled; it is not the owner's."
Prompt text (§4.4) is pinned in tests/prompt_tools_check.py.


TEST -> MUTATION
================
  send not in DESK_TOOLS but in FAST_TOOLS   FastPathRefusals.test_off_desk_fast_turns_hold_no_chat_send,
                                             FastPathRefusals.test_a_fast_desk_turn_steered_from_discord_loses_it,
                                             Contract.test_fast_path_sets
  desk check missing in the tool             ToolGuards.test_chat_send_refuses_off_the_desk_before_the_service
  depth check missing (chat_send)            ToolGuards.test_chat_send_refuses_at_depth_before_the_service
  depth check missing (chat_wait)            ToolGuards.test_chat_wait_refuses_at_depth_before_the_service
  tool in PARALLEL_SAFE                      Contract.test_flags_explicit_only_not_parallel_not_external
  tool in MCP_TOOLS                          Contract.test_flags_explicit_only_not_parallel_not_external
  budget counted outside the lock            Budget.test_parallel_claude_mcp_calls_cannot_exceed_three_sends
  no EXPLICIT_ONLY (a v1 Agent holds them)   Contract.test_flags_explicit_only_not_parallel_not_external
  chat_send marked read-only                 Contract.test_the_bridge_listing_is_the_registry_spec,
                                             ClaudeHook.test_the_conversation_gets_the_delegation_allow
  not foreground_only (Claude sub-agent)     ClaudeHook.test_a_sub_agent_is_denied_without_asking_permit
  not foreground_only (Codex child)          BridgeRefusals.test_a_codex_child_is_refused_by_admit
  request_alive not extended to chat_wait    WaitBridge.test_a_closed_relay_socket_ends_the_wait
  the wait ignores should_stop               WaitBridge.test_a_closed_relay_socket_ends_the_wait
                                             (Stop itself also ends it through the service's own
                                             live-turn check: test_stop_ends_the_wait_through_the_bridge)
  STRICT delegation allowed                  Strict.test_a_strict_chat_is_refused_delegation
  chat_wait not PRIVATE_RESULT               EndToEnd.test_fast_to_claude_send_and_wait,
                                             Contract.test_flags_explicit_only_not_parallel_not_external,
                                             Contract.test_the_claude_summary_of_a_wait_is_private
  chat_wait schema renamed (seconds)         Contract.test_schemas_are_generated_from_annotated_hints
                                             and every wait through the bridge
  The service-side checks of §6 row 6 (CC-6's desk, no chains, the exact
  turn) are C3's, pinned by tests/v2/chat_relay_check.py NoChains.*; here
  every row also goes through the real tool, the bridge, the Claude hook or
  Codex admission. With the tool's own desk check removed, the BridgeRefusals
  rows still pass on the service's check, by design (checked twice, P-6).
"""
from __future__ import annotations

import contextvars
import inspect
import itertools
import json
import socket
import sys
import tempfile
import threading
import time
import typing
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat_relay_check as CR  # noqa: E402  (helpers and the Fake provider only)
import mcp_session_check as M  # noqa: E402  (helpers only; not its setUpModule)
from jarvis import config, llm, runtime, tools  # noqa: E402
config.CLAUDE_SKILLS_DIR = None  # never read the owner's real ~/.claude/skills
from jarvis.v2 import bridge, session_tools  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2.approvals import PendingApprovals  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, BriefRefused, Decision, SessionHandle, UserMessage  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
SENDS = ("chat_send", "chat_wait")
READS = ("chat_list", "chat_summary", "chat_search", "chat_read")
REFUSED = ("Error:", "Refused:")
MESSAGE_HEADER, REPLY_HEADER, REPORT_LINE = CR.MESSAGE_HEADER, CR.REPLY_HEADER, CR.REPORT_LINE
MID, until, mark, DEADLINE = CR.MID, CR.until, CR.mark, CR.DEADLINE
_reply, _real, _content = CR._reply, CR._real, CR._content
CHAT_SENTENCE = "A message from another HUD chat arrives fenced and labelled; it is not the owner's."

REQUIRED = object()
SCHEMAS = {
    "chat_send": [
        ("chat", "string", REQUIRED,
         "The chat to message: its id from chat_list, or its exact title"),
        ("message", "string", REQUIRED,
         "What to ask or tell it, complete in itself: it cannot see this conversation "
         "(at most 4000 characters)"),
    ],
    "chat_wait": [
        ("message_id", "string", REQUIRED, "The id chat_send returned"),
        ("seconds", "integer", 120, "Longest to wait, in seconds (default 120, at most 300)"),
    ],
}


def chats_module():
    """Imported per test, so a tree without a name fails that test alone."""
    from jarvis.v2.tools import chats
    return chats


def tool_fn(name):
    fn = getattr(chats_module(), name, None)
    if fn is None:
        raise AssertionError(f"jarvis/v2/tools/chats.py has no {name}")
    return fn


def is_refusal(text) -> bool:
    return isinstance(text, str) and text.startswith(REFUSED)


_calls = itertools.count(1)


def native_handle():
    """A Codex native parent turn, always current (as codex_session_mcp_check)."""
    native = SimpleNamespace(thread_id="native-parent", turn_id="native-turn", mcp_calls=set(),
                             mcp_items={}, items={}, mutex=threading.RLock(),
                             closed=threading.Event(), cancelled=threading.Event(),
                             sending=threading.Lock())
    native.sending.acquire()
    return SessionHandle("chat", CODEX, "codex:native-parent", native)


def parent(**kw):
    """A fresh exact-parent Codex call identity (one-shot ids)."""
    return {"thread_id": "native-parent", "turn_id": "native-turn",
            "call_id": f"call-{next(_calls)}", "subagent": False, **kw}


# --- the contract: schemas, flags, catalogs, prompt sentence -------------------------


class Contract(unittest.TestCase):
    def test_schemas_are_generated_from_annotated_hints(self):
        chats = chats_module()
        for name, params in SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertIn(name, tools.REGISTRY)
                entry = tools.REGISTRY[name]
                func = getattr(chats, name)
                self.assertIs(entry.func, func, "registered by @tool from tools/chats.py")
                hints = typing.get_type_hints(func, include_extras=True)
                signature = inspect.signature(func)
                self.assertEqual(list(signature.parameters), [p[0] for p in params])
                for pname, jtype, default, description in params:
                    self.assertIs(typing.get_origin(hints[pname]), typing.Annotated,
                                  f"{name}.{pname} is Annotated (invariant 6)")
                    self.assertEqual(tools._json_type(hints[pname]),
                                     {"type": jtype, "description": description})
                    actual = signature.parameters[pname].default
                    if default is REQUIRED:
                        self.assertIs(actual, inspect.Parameter.empty)
                    else:
                        self.assertEqual(actual, default)
                self.assertEqual(entry.schema, {
                    "type": "object",
                    "properties": {p: {"type": t, "description": d} for p, t, _, d in params},
                    "required": [p for p, _, default, _ in params if default is REQUIRED],
                    "additionalProperties": False,
                }, "the schema is the hints', not hand-written")
                self.assertTrue(entry.description.strip())
                self.assertFalse(entry.dangerous, "the daemon's gates, not a dangerous card")

    def test_flags_explicit_only_not_parallel_not_external(self):
        chats_module()
        from jarvis.v2 import mcp as external
        from jarvis.v2.mcp_catalog import MCP_TOOLS
        defaults = set(tools.default_names())
        listed = {t["name"] for t in external.list_tools()["tools"]}
        for name in SENDS:
            with self.subTest(tool=name):
                self.assertIn(name, tools.REGISTRY)
                self.assertIn(name, tools.EXPLICIT_ONLY, "no v1 surface holds it")
                self.assertNotIn(name, defaults)
                self.assertNotIn(name, tools.PARALLEL_SAFE, "ordering, the budget, a blocking wait")
                self.assertFalse(tools.parallelizable(name))
                self.assertNotIn(name, MCP_TOOLS, "the external jarvis mcp never lists it")
                self.assertNotIn(name, listed)
        self.assertIn("chat_wait", tools.PRIVATE_RESULT, "a wait returns another chat's words")
        secret = mark("WAITED-REPLY")
        self.assertNotIn(secret, tools.private_summary("chat_wait", f"{REPLY_HEADER}\n{secret}"))

    def test_session_catalog_flags(self):
        chats_module()
        expected = {"chat_send": (frozenset({"claude", "codex"}), False, True, True, False),
                    "chat_wait": (frozenset({"claude", "codex"}), False, True, False, False)}
        for name, flags in expected.items():
            with self.subTest(tool=name):
                self.assertIn(name, session_tools.SESSION_TOOLS)
                entry = session_tools.SESSION_TOOLS[name]
                self.assertEqual((entry.providers, entry.read_only, entry.foreground_only,
                                  entry.desk_only, entry.owner_gated), flags)
                for provider in ("claude", "codex"):
                    self.assertIn(name, {t.name for t in session_tools.for_provider(provider)})
                self.assertNotIn(session_tools.CLAUDE_PREFIX + name,
                                 session_tools.read_only_claude_names())

    def test_the_bridge_listing_is_the_registry_spec(self):
        chats_module()
        for provider in ("claude", "codex"):
            listed = {t["name"]: t for t in bridge.listing(provider)}
            for name in SENDS:
                with self.subTest(provider=provider, tool=name):
                    self.assertIn(name, listed)
                    spec = tools.REGISTRY[name].spec()["function"]
                    self.assertEqual(listed[name]["inputSchema"], spec["parameters"])
                    self.assertEqual(listed[name]["description"], spec["description"])
                    self.assertEqual(listed[name]["annotations"], {"readOnlyHint": False})

    def test_codex_server_entry_approves_and_enables_both(self):
        chats_module()
        entry = bridge.server_entry(CODEX, 4242, M.PLACEHOLDER)
        for name in SENDS:
            with self.subTest(tool=name):
                self.assertEqual(entry.get("tools", {}).get(name), {"approval_mode": "approve"})
                self.assertIn(name, entry.get("enabled_tools", []))
        self.assertEqual(entry.get("tool_timeout_sec"), 900, "above chat_wait's 300 s cap")

    def test_fast_path_sets(self):
        from jarvis.v2.providers import fastpath
        self.assertIn("chat_send", fastpath.DESK_TOOLS, "held only at the desk")
        self.assertNotIn("chat_send", fastpath.CHAT_TOOLS)
        self.assertIn("chat_wait", fastpath.CHAT_TOOLS, "held on every turn of an owner's chat")
        self.assertNotIn("chat_wait", fastpath.DESK_TOOLS)
        for name in SENDS:
            self.assertNotIn(name, fastpath.EDIT_TOOLS)

    def test_new_chats_are_told_a_relayed_message_is_not_the_owners(self):
        from jarvis.v2.roles import brief_for
        note = brief_for(Role.CHAT).system_append
        self.assertIn(CHAT_SENTENCE, " ".join(note.split()))
        chats_module()
        named = {n for n in tools.REGISTRY if n.startswith("chat_") and n in note}
        self.assertEqual(named, set(), "the role brief stays tool-free (frozen in brief.json)")

    def test_the_claude_summary_of_a_wait_is_private(self):
        from jarvis.v2.providers import claude, codex
        self.assertIn("chat_wait", tools.PRIVATE_RESULT)
        secret = mark("REPLY-TEXT")
        reply = f"{REPLY_HEADER}\nFrom: chat aaaa0001 \"x\"\n{secret}"
        provider = claude.ClaudeProvider()
        for name in ("chat_wait", "mcp__jarvis__chat_wait"):
            with self.subTest(name=name):
                block = claude.ToolResultBlock(tool_use_id="c", content=reply, is_error=False)
                session = SimpleNamespace(tools_seen={"c": (name, {"message_id": "m" + "1" * 12})},
                                          denied_tools=set(), denial_without_id=False, thread_id="t")
                events = provider._tool_result(session, block)
                self.assertNotIn(secret, json.dumps([e.data for e in events]))
        self.assertTrue(codex._private_result("chat_wait"))


# --- STRICT and the delegation layer ----------------------------------------------------


class Strict(unittest.TestCase):
    """Plan §4.2: a STRICT Claude (or Codex) chat is refused delegation of
    both; AUTO and ASK delegate them (L_JARVIS_DELEGATE), nobody asked; an
    always-ask rule naming one still asks."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="chat-send-gate-")))
        M.patch_config(self.stack, self.root)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        chats_module()

    def brief(self, profile, **kw):
        kw.setdefault("role", Role.CHAT)
        kw.setdefault("mcp_servers", {"jarvis": {"command": "/bin/true"}})
        return Brief(cwd=str(self.cwd), profile=profile, **kw)

    def decisions(self):
        path = config.V2_DATA_DIR / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_a_strict_chat_is_refused_delegation(self):
        for provider, prefix in (("claude", session_tools.CLAUDE_PREFIX), ("codex", "")):
            for name in SENDS:
                with self.subTest(provider=provider, tool=name):
                    brief = self.brief(PermissionProfile.STRICT)
                    asker = M.Asker(Decision.ALLOW)
                    permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider), asker)
                    self.assertEqual(permit(prefix + name, {}, brief), Decision.DENY)
                    self.assertEqual(asker.seen, [], "refused unasked")
                    self.assertEqual(self.decisions()[-1]["layer"], "jarvis-session-delegate")

    def test_auto_and_ask_delegate_without_asking(self):
        for provider, prefix in (("claude", session_tools.CLAUDE_PREFIX), ("codex", "")):
            for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
                for name in SENDS:
                    with self.subTest(provider=provider, profile=profile.value, tool=name):
                        brief = self.brief(profile)
                        asker = M.Asker(Decision.DENY)
                        permit = M.build_permit(M.PermitContext("abcd1234", brief, provider=provider),
                                                asker)
                        self.assertEqual(permit(prefix + name, {}, brief), Decision.ALLOW)
                        self.assertEqual(asker.seen, [])
                        row = self.decisions()[-1]
                        self.assertEqual((row["layer"], row["decision"]),
                                         ("jarvis-session-delegate", "allow"))

    def test_an_always_ask_rule_naming_chat_send_still_asks(self):
        name = session_tools.CLAUDE_PREFIX + "chat_send"
        brief = self.brief(PermissionProfile.AUTO, always_ask=[name])
        asker = M.Asker(Decision.DENY)
        permit = M.build_permit(M.PermitContext("abcd1234", brief, provider="claude"), asker)
        self.assertEqual(permit(name, {}, brief), Decision.DENY)
        self.assertEqual(len(asker.seen), 1)
        self.assertIn("chat_send", session_tools.SESSION_TOOLS, "and it is a session tool")


# --- Claude's PreToolUse hook -------------------------------------------------------------


class ClaudeHook(unittest.TestCase):
    """§6 row 6: a Claude sub-agent's call (`agent_id`) is denied by the hook
    before `permit` runs (spy: 0 calls); the conversation's own call gets the
    explicit delegation allow, decided by permit, nobody asked."""

    def setUp(self):
        import claude_provider_check as cl
        from jarvis.v2.providers import claude
        self.cl, self.claude = cl, claude
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="chat-send-hook-")))
        M.patch_config(self.stack, self.root)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.stack.enter_context(cl.fake())
        self.provider = claude.ClaudeProvider()
        chats_module()

    def start(self, permit, thread_id):
        brief = Brief(Role.CHAT, str(self.cwd), profile=PermissionProfile.AUTO,
                      mcp_servers={"jarvis": bridge.server_entry(CLAUDE, 4242, M.PLACEHOLDER)})
        handle = self.provider.start(Thread(thread_id, "p", Role.CHAT, CLAUDE), brief, permit)
        self.addCleanup(self.provider.close, handle)
        return handle

    def hook(self, handle, name, agent_id=None):
        options = self.cl.FakeClient.instances[-1].options
        data = {"hook_event_name": "PreToolUse", "tool_name": name,
                "tool_input": {"chat": "aaaa0001", "message": "hi"},
                "tool_use_id": "c1", "session_id": "s", "transcript_path": "", "cwd": str(self.cwd)}
        if agent_id:
            data["agent_id"] = agent_id
        coro = options.hooks["PreToolUse"][0].hooks[0](data, "c1", {"signal": None})
        return self.claude._await(handle.native.loop, coro, 10)

    def counted_permit(self, asker):
        brief = Brief(Role.CHAT, str(self.cwd), mcp_servers={"jarvis": {"command": "/bin/true"}})
        permit = M.build_permit(M.PermitContext("chsend01", brief, provider="claude"), asker)
        calls = []

        def counted(*a, **k):
            calls.append(a)
            return permit(*a, **k)
        return counted, calls

    def test_a_sub_agent_is_denied_without_asking_permit(self):
        asker = M.Asker(Decision.ALLOW)
        permit, calls = self.counted_permit(asker)
        handle = self.start(permit, "chsend01")
        for name in SENDS:
            with self.subTest(tool=name):
                before = len(calls)
                out = self.hook(handle, session_tools.CLAUDE_PREFIX + name, agent_id="sub-1")
                decided = out.get("hookSpecificOutput", {})
                self.assertEqual(decided.get("permissionDecision"), "deny")
                self.assertIn("sub-agent", decided.get("permissionDecisionReason", ""))
                self.assertEqual(len(calls), before, "permit is never asked (spy: 0 calls)")
        self.assertEqual(asker.seen, [])

    def test_the_conversation_gets_the_delegation_allow(self):
        asker = M.Asker(Decision.DENY)
        permit, calls = self.counted_permit(asker)
        handle = self.start(permit, "chsend02")
        for name in SENDS:
            with self.subTest(tool=name):
                before = len(calls)
                decided = self.hook(handle, session_tools.CLAUDE_PREFIX + name).get(
                    "hookSpecificOutput", {})
                self.assertEqual(decided.get("permissionDecision"), "allow")
                self.assertEqual(decided.get("permissionDecisionReason"),
                                 "the daemon checks this Jarvis tool", "not a read-only tool")
                self.assertEqual(len(calls) - before, 1, "permit decided it")
        self.assertEqual(asker.seen, [], "nobody was asked")


# --- the daemon harness -------------------------------------------------------------------


class SlowRelease:
    """The daemon's own RLock, behind a proxy that, while `slow` is set,
    sleeps after each full release: any check-then-count split across the
    lock (a budget counted outside it) is then raced by the waiting threads
    every time, not once in a thousand runs."""

    def __init__(self, inner):
        self.inner = inner
        self.slow = threading.Event()
        self.local = threading.local()

    def acquire(self, *a, **k):
        got = self.inner.acquire(*a, **k)
        if got:
            self.local.depth = getattr(self.local, "depth", 0) + 1
        return got

    def release(self):
        self.inner.release()
        self.local.depth = getattr(self.local, "depth", 1) - 1
        if self.local.depth == 0 and self.slow.is_set():
            time.sleep(0.02)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Harness(CR.Harness):
    """chat_relay_check's harness with the real fast path in FAST, the real §6
    permit, and a Codex fake that admits exact-parent foreground calls."""

    def make_daemon(self):
        from jarvis.v2.providers import codex
        from jarvis.v2.providers.fastpath import FastPathProvider
        chats_module()
        self.fakes[FAST] = FastPathProvider()
        self.requests = []
        self.script = None
        self.script_lock = threading.Lock()
        real_chat = llm.chat
        llm.chat = self._chat
        self.addCleanup(setattr, llm, "chat", real_chat)
        self.codex_native = native_handle()
        admitter = codex.CodexProvider()
        self.fakes[CODEX].admit_foreground_mcp = (
            lambda handle, ctx: admitter.admit_foreground_mcp(self.codex_native, ctx))
        holder = {}
        approvals = PendingApprovals(
            timeout_s=10.0,
            on_request=lambda request: holder["d"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["d"]._approval_resolved(request, decision, resolution))
        d = daemon_mod.Daemon(self.stores, dict(self.fakes), None, 0, approvals)
        holder["d"] = d
        d.start()
        self.addCleanup(d.stop)
        self.addCleanup(self.unstick, d)
        d.chat_relay.clock = self.clock
        return d

    def _chat(self, model, messages, tools=None, on_delta=None, **kw):
        if tools is None:
            return _reply("a title")
        names = [t["function"]["name"] for t in tools]
        record = {"names": names, "messages": list(messages)}
        with self.script_lock:
            self.requests.append(record)
        if self.script is not None:
            return self.script(messages, names)
        return _reply("done")

    # -- the bridge, as a Claude or Codex CLI would call it

    def token(self, thread):
        return M.token_of(self.fakes[ProviderName(thread.provider)].briefs[thread.id])

    def mcp(self, thread, name, arguments, ctx="parent"):
        body = {"name": name, "arguments": arguments}
        if ProviderName(thread.provider) is CODEX:
            if ctx == "parent":
                ctx = parent()
            if ctx is not None:
                body["codex_context"] = ctx
        return M.http(self.d.port, "POST", "/mcp/call", token=self.token(thread), body=body)

    def bridge_send(self, sender, target, text="please do the thing", ctx="parent"):
        return self.mcp(sender, "chat_send",
                        {"chat": target.id if hasattr(target, "id") else target, "message": text},
                        ctx)

    def bridge_sent(self, got):
        status, body = got
        self.assertEqual(status, 200, body)
        self.assertFalse(body["isError"], body)
        return self.sent(body["text"])

    def bridge_refused(self, got, sender):
        status, body = got
        self.assertEqual(status, 200, f"the tool is offered and runs: {body}")
        self.assertTrue(body["isError"], body)
        self.assertTrue(is_refusal(body["text"]), body["text"])
        self.assertNotIn("Sent message", body["text"])
        return body["text"]

    def nothing_sent(self, sender, before):
        with self.d._lock:
            self.assertEqual(len(self.svc.items), before, "no relay item")
        self.assertEqual(self.log(sender, "relay_out"), [], "no relay_out in the sender's log")

    # -- the fast path

    def fast_deliver(self, thread, message):
        return self.d.deliver(thread.id, message)


# --- the tool's own first check -------------------------------------------------------------


class ToolGuards(Harness):
    """The tool refuses before reaching the service (§4.1: "as
    hud_browser_view.py:21 does"): a live desk turn the service would accept,
    called with a runtime that says otherwise."""

    def run_tool(self, name, arguments, *, caller=None, depth=0, desk=None, should_stop=None):
        def run():
            runtime.bind(depth=depth)
            if should_stop is not None:
                runtime.bind(should_stop=should_stop)
            if caller is not None:
                runtime.bind(caller=caller)
            if desk is not None:
                runtime.bind(desk=desk)
            return tool_fn(name)(**arguments)
        return contextvars.Context().run(run)

    def desk_turn(self):
        sender, target = self.chat(CLAUDE, "Sender"), self.chat(CODEX, "Target")
        self.fakes[CODEX].hold.add(target.id)
        self.turn(sender)                     # typed at the HUD: the service's slot is present
        with self.d._lock:
            self.assertIs(self.session(sender).desk.slot.get("present"), True)
        return sender, target

    def finish(self, *threads):
        for thread in threads:
            self.fakes[ProviderName(thread.provider)].hold.discard(thread.id)
            self.release(thread)
        for thread in threads:
            self.idle(thread)

    def test_chat_send_refuses_off_the_desk_before_the_service(self):
        sender, target = self.desk_turn()
        args = {"chat": target.id, "message": "hello"}
        with patch.object(self.svc, "send", wraps=self.svc.send) as spy:
            for desk in ({"present": False}, {}):
                with self.subTest(desk=desk):
                    out = self.run_tool("chat_send", args, caller=self.caller(sender), desk=desk)
                    self.assertTrue(is_refusal(out), out)
                    self.assertEqual(spy.call_count, 0, "refused in the tool, first")
            with self.subTest(desk="unbound"):
                out = self.run_tool("chat_send", args, caller=self.caller(sender))
                self.assertTrue(is_refusal(out), out)
                self.assertEqual(spy.call_count, 0)
            # The control: the same call at the desk reaches the service and sends.
            out = self.run_tool("chat_send", args, caller=self.caller(sender), desk={"present": True})
            self.sent(out)
            self.assertEqual(spy.call_count, 1)
        self.finish(sender, target)

    def test_chat_send_refuses_at_depth_before_the_service(self):
        sender, target = self.desk_turn()
        args = {"chat": target.id, "message": "hello"}
        with patch.object(self.svc, "send", wraps=self.svc.send) as spy:
            for depth in (1, 2):
                with self.subTest(depth=depth):
                    out = self.run_tool("chat_send", args, caller=self.caller(sender),
                                        desk={"present": True}, depth=depth)
                    self.assertTrue(is_refusal(out), out)
                    self.assertEqual(spy.call_count, 0, "a sub-agent never reaches the service")
        with self.d._lock:
            self.assertEqual(len(self.svc.items), 0)
        self.finish(sender, target)

    def test_chat_wait_refuses_at_depth_before_the_service(self):
        sender, target = self.desk_turn()
        mid = self.sent(self.run_tool("chat_send", {"chat": target.id, "message": "q"},
                                      caller=self.caller(sender), desk={"present": True}))
        # A real clock here, so a wait that wrongly reaches the service ends
        # after its 1 s deadline instead of hanging the run.
        self.svc.clock = time.monotonic
        with patch.object(self.svc, "wait", wraps=self.svc.wait) as spy:
            started = time.monotonic()
            out = self.run_tool("chat_wait", {"message_id": mid, "seconds": 1},
                                caller=self.caller(sender), desk={"present": True}, depth=1)
            self.assertTrue(is_refusal(out), out)
            self.assertEqual(spy.call_count, 0, "a sub-agent never reaches the service")
            self.assertLess(time.monotonic() - started, 0.9, "refused at once, not waited")
            # The control: at depth 0 it reaches the service, off the desk too
            # (chat_wait is not desk-only), and says where the message is.
            out = self.run_tool("chat_wait", {"message_id": mid, "seconds": 1},
                                caller=self.caller(sender), desk={"present": False})
            self.assertEqual(spy.call_count, 1)
            self.assertIn(mid, out)
            self.assertFalse(is_refusal(out), out)
        self.finish(sender, target)

    def test_a_stale_turn_or_no_caller_is_refused(self):
        sender, target = self.desk_turn()
        args = {"chat": target.id, "message": "hello"}
        for label, caller in (("stale turn", self.caller(sender, "0" * 32)), ("no caller", None)):
            with self.subTest(case=label):
                out = self.run_tool("chat_send", args, caller=caller, desk={"present": True})
                self.assertTrue(is_refusal(out), out)
                self.nothing_sent(sender, 0)
        for label, caller in (("stale turn", self.caller(sender, "0" * 32)), ("no caller", None)):
            with self.subTest(case="wait, " + label):
                out = self.run_tool("chat_wait", {"message_id": "m" + "0" * 12},
                                    caller=caller, desk={"present": True})
                self.assertTrue(is_refusal(out), out)
        self.finish(sender, target)


class NoDaemon(unittest.TestCase):
    def test_no_service_is_a_refusal(self):
        for name, args in (("chat_send", {"chat": "aaaa0002", "message": "hi"}),
                           ("chat_wait", {"message_id": "m" + "1" * 12})):
            with self.subTest(tool=name):
                def run():
                    runtime.bind(depth=0, desk={"present": True})
                    runtime.bind_caller("aaaa0001", "p1", "claude", "turn")
                    return tool_fn(name)(**args)
                out = contextvars.Context().run(run)
                self.assertTrue(is_refusal(out), out)


# --- §6 row 6 through the fast path's real tools ----------------------------------------


class FastPathRefusals(Harness):
    """The fast path holds `chat_send` only on a desk turn of an owner's chat
    (DESK_TOOLS) and `chat_wait` on every turn of one (CHAT_TOOLS). Off the
    desk the request carries no chat_send schema and dispatch refuses it."""

    def calling(self, target_id, results):
        """Each turn calls chat_send once, then answers."""
        def script(messages, names):
            last = _real(messages)[-1]
            if last.get("role") == "tool":
                results.append((names, _content(last)))
                return _reply("done")
            if _content(last).startswith(REPLY_HEADER):
                return _reply("noted")          # a reply (a step or a reply turn): no new send
            return _reply(calls=[("chat_send", {"chat": target_id, "message": "do the thing"})])
        return script

    def fast_case(self, message):
        sender, target = self.chat(FAST), self.chat(CODEX)
        self.fakes[CODEX].hold.add(target.id)   # its reply waits until the sender is done
        results = []
        self.script = self.calling(target.id, results)
        count = len(self.requests)
        self.fast_deliver(sender, message)
        until(lambda: results, "the fast turn's chat_send result")
        self.idle(sender)
        names = set(self.requests[count]["names"])
        return sender, target, names, results[0][1]

    def test_typed_and_spoken_desk_turns_may_send(self):
        for label, message in (("typed", UserMessage("ask the other chat", desk=True)),
                               ("spoken", UserMessage("ask the other chat", desk=True,
                                                      spoken=True))):
            with self.subTest(case=label):
                sender, target, names, result = self.fast_case(message)
                self.assertLessEqual(set(SENDS), names, sorted(names))
                self.sent(result)
                self.until_relayed(target)
                self.fakes[CODEX].hold.discard(target.id)
                self.release(target)
                self.idle(target)
                self.idle(sender)
                self.assertEqual(len(self.log(sender, "relay_out")), 1)

    def test_off_desk_fast_turns_hold_no_chat_send(self):
        cases = {
            "discord": UserMessage("ask the other chat", via="discord"),
            "dm": UserMessage("ask the other chat", via="dm"),
            "api listener (desk=False)": UserMessage("ask the other chat"),
            "schedule": UserMessage("ask the other chat", origin="system"),
            "escape hatch": UserMessage("[owner ran: ls]", origin="owner-ran"),
        }
        for label, message in cases.items():
            with self.subTest(case=label):
                with self.d._lock:
                    before = len(self.svc.items)
                sender, target, names, result = self.fast_case(message)
                self.assertNotIn("chat_send", names, "absent from the request")
                self.assertIn("chat_wait", names, "an owner chat holds chat_wait on every turn")
                self.assertIn("not available to this agent", result, result)
                self.nothing_sent(sender, before)
                self.assertEqual(self.relayed(target), [])

    def test_a_fast_desk_turn_steered_from_discord_loses_it(self):
        sender, target = self.chat(FAST), self.chat(CODEX)
        state = {"step": 0}
        entered, go = threading.Event(), threading.Event()
        results = []

        def script(messages, names):
            state["step"] += 1
            if state["step"] == 1:
                entered.set()
                go.wait(DEADLINE)
                return _reply(calls=[("get_datetime", {})])
            last = _real(messages)[-1]
            if last.get("role") == "tool" and state["step"] > 2:
                results.append((names, _content(last)))
                return _reply("done")
            return _reply(calls=[("chat_send", {"chat": target.id, "message": "do it"})])
        self.script = script
        count = len(self.requests)
        self.fast_deliver(sender, UserMessage("start at the desk", desk=True))
        self.assertTrue(entered.wait(DEADLINE))
        self.assertIn("chat_send", self.requests[count]["names"], "the desk turn held it")
        self.assertEqual(self.fast_deliver(sender, UserMessage("from discord", via="discord"))["status"],
                         "steered")
        go.set()
        until(lambda: results, "the steered step's chat_send")
        self.idle(sender)
        names, result = results[0]
        self.assertNotIn("chat_send", names, "a foreign steer takes the desk tools away")
        self.assertIn("not available to this agent", result, result)
        self.nothing_sent(sender, 0)

    def test_a_relayed_fast_turn_cannot_send(self):
        sender, target, far = self.chat(CLAUDE, "Asker"), self.chat(FAST, "Helper"), self.chat(CODEX)
        results = []

        def script(messages, names):
            last = _real(messages)[-1]
            if last.get("role") == "tool":
                results.append((names, _content(last)))
                return _reply("done")
            if _content(last).startswith(MESSAGE_HEADER):
                return _reply(calls=[("chat_send", {"chat": far.id, "message": "pass it on"})])
            return _reply("done")
        self.script = script
        self.turn(sender)
        self.bridge_sent(self.bridge_send(sender, target, "ask the far chat for me"))
        until(lambda: results, "the relayed fast turn's chat_send")
        self.idle(target)
        names, result = results[0]
        self.assertNotIn("chat_send", names, "a relayed turn is never at the desk")
        self.assertIn("not available to this agent", result, result)
        self.assertEqual(self.relayed(far), [], "no chains")
        self.assertEqual(self.log(target, "relay_out"), [])
        self.release(sender)
        self.idle(sender)

    def test_a_fast_reply_turn_cannot_send(self):
        sender, target, far = self.chat(FAST, "Asker"), self.chat(CLAUDE, "Helper"), self.chat(CODEX)
        self.fakes[CLAUDE].hold.add(target.id)
        results, sends = [], []

        def script(messages, names):
            last = _real(messages)[-1]
            text = _content(last)
            if last.get("role") == "tool":
                (results if REPLY_HEADER in str(_real(messages)) else sends).append((names, text))
                return _reply("done")
            if text.startswith(REPLY_HEADER):
                return _reply(calls=[("chat_send", {"chat": far.id, "message": "and onward"})])
            return _reply(calls=[("chat_send", {"chat": target.id, "message": "what is the port?"})])
        self.script = script
        self.fast_deliver(sender, UserMessage("ask the helper", desk=True))
        until(lambda: sends, "the desk turn's send")
        self.sent(sends[0][1])
        self.idle(sender)
        self.release(target)
        until(lambda: results, "the reply turn's chat_send")
        self.idle(sender)
        self.idle(target)
        names, result = results[0]
        self.assertNotIn("chat_send", names)
        self.assertIn("not available to this agent", result, result)
        self.assertEqual(self.relayed(far), [], "no chains")
        self.assertEqual(len(self.log(sender, "relay_out")), 1, "only the desk turn's")

    def test_a_task_thread_holds_neither_and_cannot_be_given_them(self):
        from jarvis.v2.providers.fastpath import FastPathProvider
        provider = FastPathProvider()

        def start(brief, tid):
            return provider.start(Thread(tid, self.project.id, Role.CHAT, FAST), brief,
                                  lambda *a, **k: Decision.ALLOW)
        cwd = str(self.root / "project")
        owner = start(Brief(Role.CHAT, cwd), "fsowner1")
        self.assertLessEqual(set(SENDS), set(owner.native.tool_names), "an owner's chat brief")
        task = start(Brief(Role.CHAT, cwd, task_id="task1"), "fstask01")
        self.assertFalse(set(SENDS) & set(task.native.tool_names))
        for name in SENDS:
            with self.subTest(tool=name):
                with self.assertRaises(BriefRefused):
                    start(Brief(Role.CHAT, cwd, task_id="task1", allowed_tools=[name]), "fstask02")


# --- §6 row 6 through the bridge (Claude and Codex) -----------------------------------------


class BridgeRefusals(Harness):
    def row(self, provider, message):
        sender, target = self.chat(provider), self.chat(FAST)
        self.turn(sender, message)
        got = self.bridge_send(sender, target)
        return sender, target, got

    def finish(self, *threads):
        for thread in threads:
            if ProviderName(thread.provider) is not FAST:
                self.fakes[ProviderName(thread.provider)].hold.discard(thread.id)
                self.release(thread)
        for thread in threads:
            self.idle(thread)

    def test_off_desk_turns_are_refused(self):
        cases = {
            "discord": UserMessage("from discord", via="discord"),
            "dm": UserMessage("from the dm", via="dm"),
            "api listener (desk=False)": UserMessage("a script on the API listener"),
            "schedule": UserMessage("a schedule", origin="system"),
            "escape hatch": UserMessage("[owner ran: ls]", origin="owner-ran"),
        }
        for provider in (CLAUDE, CODEX):
            for label, message in cases.items():
                with self.subTest(provider=provider.value, case=label):
                    with self.d._lock:
                        before = len(self.svc.items)
                    sender, target, got = self.row(provider, message)
                    self.bridge_refused(got, sender)
                    self.nothing_sent(sender, before)
                    self.finish(sender)

    def test_a_desk_turn_steered_from_discord_is_refused(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(provider), self.chat(FAST)
                self.turn(sender)
                self.assertEqual(self.d.deliver(sender.id, UserMessage("from discord", via="discord"))
                                 ["status"], "steered")
                with self.d._lock:
                    before = len(self.svc.items)
                self.bridge_refused(self.bridge_send(sender, target), sender)
                self.nothing_sent(sender, before)
                self.finish(sender)

    def test_typed_and_spoken_desk_turns_may_send(self):
        for provider in (CLAUDE, CODEX):
            for label, message in (("typed", UserMessage("typed", desk=True)),
                                   ("spoken", UserMessage("spoken", desk=True, spoken=True))):
                with self.subTest(provider=provider.value, case=label):
                    sender, target = self.chat(provider), self.chat(CODEX)
                    self.turn(sender, message)
                    mid = self.bridge_sent(self.bridge_send(sender, target))
                    self.until_relayed(target)
                    (record,) = self.log(sender, "relay_out")
                    self.assertEqual(record["data"]["message_id"], mid)
                    self.finish(sender, target)

    def test_between_turns_is_refused(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(provider), self.chat(FAST)
                self.turn(sender)
                self.finish(sender)
                with self.d._lock:
                    before = len(self.svc.items)
                self.bridge_refused(self.bridge_send(sender, target), sender)
                self.nothing_sent(sender, before)

    def test_a_relayed_turn_is_refused(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                asker, middle, far = self.chat(CODEX), self.chat(provider), self.chat(FAST)
                self.fakes[provider].hold.add(middle.id)
                self.turn(asker)
                self.bridge_sent(self.bridge_send(asker, middle, "pass it on"))
                self.until_relayed(middle)
                with self.d._lock:
                    before = len(self.svc.items)
                self.bridge_refused(self.bridge_send(middle, far, "onward"), middle)
                self.nothing_sent(middle, before)
                self.finish(middle, asker)

    def test_a_reply_turn_is_refused(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target, far = self.chat(provider), self.chat(CODEX), self.chat(FAST)
                self.fakes[CODEX].hold.add(target.id)
                self.fakes[provider].hold_replies.add(sender.id)
                self.turn(sender)
                self.bridge_sent(self.bridge_send(sender, target, "answer me"))
                self.until_relayed(target)
                self.release(sender)
                until(lambda: self.session(sender).worker is None, "the sending turn to end")
                self.release(target)
                until(lambda: self.reply_turns(sender) and self.fakes[provider].running(sender.id),
                      "the reply turn")
                with self.d._lock:
                    before = len(self.svc.items)
                got = self.bridge_send(sender, far, "and onward")
                self.bridge_refused(got, sender)
                with self.d._lock:
                    self.assertEqual(len(self.svc.items), before)
                self.assertEqual(len(self.log(sender, "relay_out")), 1, "only the desk turn's")
                self.fakes[provider].hold_replies.discard(sender.id)
                self.finish(sender, target)

    def test_a_codex_child_is_refused_by_admit(self):
        sender, target = self.chat(CODEX), self.chat(FAST)
        self.turn(sender)
        with patch.object(self.svc, "send", wraps=self.svc.send) as spy:
            for label, ctx in (("no identity", None), ("a child thread", parent(thread_id="child")),
                               ("a sub-agent", parent(subagent=True))):
                with self.subTest(case=label):
                    text = self.bridge_refused(self.bridge_send(sender, target, ctx=ctx), sender)
                    self.assertIn("foreground chat turn", text)
                    self.assertEqual(spy.call_count, 0, "refused before the tool runs")
            for label, name, args in (("chat_wait, a sub-agent", "chat_wait",
                                       {"message_id": "m" + "1" * 12}),):
                with self.subTest(case=label):
                    status, body = self.mcp(sender, name, args, parent(subagent=True))
                    self.assertEqual(status, 200, body)
                    self.assertTrue(body["isError"])
                    self.assertIn("foreground chat turn", body["text"])
            # The control: the exact parent sends.
            self.bridge_sent(self.bridge_send(sender, target))
            self.assertEqual(spy.call_count, 1)
        self.finish(sender)
        self.idle(target)


# --- end to end, one per provider pair ------------------------------------------------------


class EndToEnd(Harness):
    def test_fast_to_claude_send_and_wait(self):
        sender, target = self.chat(FAST, "Planner"), self.chat(CLAUDE, "Frontend")
        answer = mark("CLAUDE-ANSWER")
        self.fakes[CLAUDE].reply[target.id] = answer
        self.fakes[CLAUDE].hold.add(target.id)
        seen = {}

        def script(messages, names):
            last = _real(messages)[-1]
            if last.get("role") != "tool":
                return _reply(calls=[("chat_send", {"chat": target.id, "message": "what port?"})])
            text = _content(last)
            if "mid" not in seen:
                seen["sent"] = text
                found = MID.search(text)
                seen["mid"] = found.group(0) if found else "m" + "0" * 12
                return _reply(calls=[("chat_wait", {"message_id": seen["mid"], "seconds": 60})])
            seen["waited"] = text
            return _reply("the port is known")
        self.script = script
        self.fast_deliver(sender, UserMessage("ask the frontend chat", desk=True))
        until(lambda: "mid" in seen, "the fast send")
        mid = self.sent(seen["sent"])
        until(lambda: self.svc.waiters.get(mid) is not None, "the wait to register")
        self.release(target)
        until(lambda: "waited" in seen, "the waited reply")
        self.idle(sender)
        self.idle(target)
        waited = seen["waited"]
        self.assertTrue(waited.startswith(REPLY_HEADER), waited[:200])
        self.assertIn(answer, waited)
        self.assertIn(f"in answer to message {mid}", waited)
        self.assertEqual(self.item(mid).delivery, "wait")
        (message,) = self.relayed(target)
        self.assertEqual((message.origin, message.via, message.desk), ("chat", "peer", False))
        self.assertTrue(message.text.startswith(MESSAGE_HEADER))
        self.assertEqual(len(self.log(sender, "relay_out", mid)), 1)
        self.assertEqual(len(self.log(sender, "relay_reply", mid)), 1)
        tool_rows = [r for r in self.log(sender) if r.get("kind") in ("tool_started", "tool_finished")]
        self.assertTrue(any((r.get("data") or {}).get("name") == "chat_wait" for r in tool_rows))
        self.assertNotIn(answer, json.dumps(tool_rows), "chat_wait's summary is private")
        finished = [r for r in tool_rows if r.get("kind") == "tool_finished"
                    and (r.get("data") or {}).get("name") == "chat_wait"]
        self.assertEqual(len(finished), 1, tool_rows)
        summary = str(finished[0]["data"].get("summary"))
        self.assertNotIn("untrusted chat text", summary, "fixed words, never the wrapper")
        self.assertNotIn(REPLY_HEADER, summary)

    def test_claude_to_codex_send_and_wait(self):
        sender, target = self.chat(CLAUDE, "Planner"), self.chat(CODEX, "Backend")
        answer = mark("CODEX-ANSWER")
        self.fakes[CODEX].reply[target.id] = answer
        self.fakes[CODEX].hold.add(target.id)
        self.turn(sender)
        mid = self.bridge_sent(self.bridge_send(sender, target, "what is the schema?"))
        box = []
        waiter = threading.Thread(target=lambda: box.append(
            self.mcp(sender, "chat_wait", {"message_id": mid, "seconds": 60})), daemon=True)
        waiter.start()
        until(lambda: self.svc.waiters.get(mid) is not None, "the wait to register")
        self.release(target)
        waiter.join(DEADLINE)
        self.assertFalse(waiter.is_alive())
        status, body = box[0]
        self.assertEqual(status, 200, body)
        self.assertFalse(body["isError"], body)
        self.assertTrue(body["text"].startswith(REPLY_HEADER), body["text"][:200])
        self.assertIn(answer, body["text"])
        self.assertEqual(self.item(mid).delivery, "wait")
        self.assertEqual(self.relayed(target)[0].origin, "chat")
        self.release(sender)
        self.idle(sender)
        self.idle(target)
        self.assertEqual(self.reply_turns(sender), [], "delivered once")

    def test_claude_to_codex_reply_comes_back_as_a_turn(self):
        sender, target = self.chat(CLAUDE, "Planner"), self.chat(CODEX, "Backend")
        answer = mark("LATER-ANSWER")
        self.fakes[CODEX].reply[target.id] = answer
        self.fakes[CODEX].hold.add(target.id)
        self.turn(sender)
        mid = self.bridge_sent(self.bridge_send(sender, target, "tell me later"))
        self.until_relayed(target)
        self.release(sender)
        until(lambda: self.session(sender).worker is None, "the sending turn to end")
        self.release(target)
        until(lambda: self.reply_turns(sender), "the reply turn")
        self.idle(sender)
        (message,) = self.reply_turns(sender)
        self.assertEqual((message.origin, message.via, message.desk), ("chat-reply", "peer", False))
        self.assertTrue(message.text.startswith(REPLY_HEADER))
        self.assertIn(answer, message.text)
        self.assertTrue(message.text.endswith(REPORT_LINE))
        self.assertEqual(self.item(mid).delivery, "turn")

    def test_codex_to_fast_send_and_wait(self):
        sender, target = self.chat(CODEX, "Planner"), self.chat(FAST, "Helper")
        answer = mark("FAST-ANSWER")
        go = threading.Event()

        def script(messages, names):
            last = _real(messages)[-1]
            if _content(last).startswith(MESSAGE_HEADER):
                go.wait(DEADLINE)
                return _reply(answer)
            return _reply("done")
        self.script = script
        self.turn(sender)
        mid = self.bridge_sent(self.bridge_send(sender, target, "summarise the notes"))
        box = []
        waiter = threading.Thread(target=lambda: box.append(
            self.mcp(sender, "chat_wait", {"message_id": mid, "seconds": 60})), daemon=True)
        waiter.start()
        until(lambda: self.svc.waiters.get(mid) is not None, "the wait to register")
        go.set()
        waiter.join(DEADLINE)
        self.assertFalse(waiter.is_alive())
        status, body = box[0]
        self.assertEqual(status, 200, body)
        self.assertFalse(body["isError"], body)
        self.assertTrue(body["text"].startswith(REPLY_HEADER), body["text"][:200])
        self.assertIn(answer, body["text"])
        self.assertEqual(self.item(mid).delivery, "wait")
        self.idle(target)
        self.assertEqual(self.log(target, "user"), [], "relay_in, never the owner's")
        self.assertEqual(len(self.log(target, "relay_in", mid)), 1)
        self.release(sender)
        self.idle(sender)


# --- chat_wait through the bridge: Stop and a closed relay socket ----------------------------


class WaitBridge(Harness):
    def waiting(self):
        sender, target = self.chat(CLAUDE, "Planner"), self.chat(CODEX, "Backend")
        self.answer = mark("NOT-YET")
        self.fakes[CODEX].reply[target.id] = self.answer
        self.fakes[CODEX].hold.add(target.id)
        self.turn(sender)
        mid = self.bridge_sent(self.bridge_send(sender, target, "q"))
        self.until_relayed(target)
        return sender, target, mid

    def test_stop_ends_the_wait_through_the_bridge(self):
        sender, target, mid = self.waiting()
        box = []
        waiter = threading.Thread(target=lambda: box.append(
            self.mcp(sender, "chat_wait", {"message_id": mid, "seconds": 300})), daemon=True)
        waiter.start()
        until(lambda: self.svc.waiters.get(mid) is not None, "the wait to register")
        started = time.monotonic()
        self.d.interrupt(sender.id)
        waiter.join(DEADLINE)
        self.assertFalse(waiter.is_alive(), "the owner's Stop ends the wait")
        self.assertLess(time.monotonic() - started, DEADLINE)
        status, body = box[0]
        self.assertEqual(status, 200, body)
        self.assertIn("stopped", body["text"].lower())
        self.assertNotIn(self.answer, body["text"])
        self.assertIsNone(self.svc.waiters.get(mid))
        self.release(target)
        self.idle(target)
        self.idle(sender)

    def test_a_closed_relay_socket_ends_the_wait(self):
        sender, target, mid = self.waiting()
        payload = json.dumps({"name": "chat_wait",
                              "arguments": {"message_id": mid, "seconds": 300}}).encode()
        head = (f"POST /mcp/call HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                f"Content-Type: application/json\r\nAuthorization: Bearer {self.token(sender)}\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n").encode()
        conn = socket.create_connection(("127.0.0.1", self.d.port), timeout=DEADLINE)
        try:
            conn.sendall(head + payload)
            until(lambda: self.svc.waiters.get(mid) is not None, "the wait to register")
        finally:
            conn.close()                      # the CLI's relay went away
        until(lambda: self.svc.waiters.get(mid) is None, "the wait to end with its socket")
        self.assertIsNone(self.item(mid).delivery, "the reply was not taken by a dead wait")
        # Delivery goes on: the sending turn still runs, so the reply is its next step.
        self.release(target)
        until(lambda: self.item(mid).delivery is not None, "the reply's delivery")
        self.assertEqual(self.item(mid).delivery, "step")
        self.release(sender)
        self.idle(sender)
        self.idle(target)


# --- the per-turn budget under parallel MCP calls --------------------------------------------


class Budget(Harness):
    def test_parallel_claude_mcp_calls_cannot_exceed_three_sends(self):
        CRm = CR.relay_module()
        self.assertEqual(CRm.SEND_BUDGET, 3)
        sender = self.chat(CLAUDE, "Fanout")
        targets = [self.chat(CODEX, f"T{n}") for n in range(8)]
        for target in targets:
            self.fakes[CODEX].hold.add(target.id)
        self.turn(sender)
        proxy = SlowRelease(self.d._lock)
        self.d._lock = proxy
        self.addCleanup(setattr, self.d, "_lock", proxy.inner)
        barrier = threading.Barrier(len(targets))
        results = [None] * len(targets)

        def call(index):
            barrier.wait(DEADLINE)
            results[index] = self.bridge_send(sender, targets[index], f"job {index}")
        workers = [threading.Thread(target=call, args=(i,), daemon=True) for i in range(len(targets))]
        proxy.slow.set()
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        finally:
            proxy.slow.clear()
        self.assertFalse(any(w.is_alive() for w in workers))
        for status, body in results:
            self.assertEqual(status, 200, body)
        sent = [body for _, body in results if not body["isError"]]
        refused = [body for _, body in results if body["isError"]]
        self.assertEqual(len(sent), 3, f"exactly the budget: {[r[1]['text'][:80] for r in results]}")
        self.assertEqual(len(refused), 5)
        self.assertTrue(all(is_refusal(b["text"]) for b in refused))
        with self.d._lock:
            self.assertEqual(len(self.svc.items), 3)
        self.assertEqual(len(self.log(sender, "relay_out")), 3)
        for target in targets:
            self.fakes[CODEX].hold.discard(target.id)
            self.release(target)
        self.release(sender)
        self.idle(sender)
        for target in targets:
            self.idle(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
