"""C2's parts kept in C3 (lead's C3 brief, item 3): how a message from another
chat meets the desk and the fast path's steering.

Plan `docs/plans/2026-10-19-chat-to-chat-plan.md` §4.3, §5.3 ("The fast
path's mid-turn label") and §8 C2, with the decisions file's CC-6: there is no
`typed` key, and "the owner started this turn at the HUD" is exactly today's
`desk.slot["present"] is True`, spoken or typed. So `desk.py`, `runtime.py`
and `daemon._steer` do not change, and every "`typed` ignored" or "`untyped()`
not called" mutation is dropped.

Free and hermetic: the unit cases build the objects directly; the fast-path
cases run the real `FastPathProvider` inside a port-0 daemon over a temporary
V2_DATA_DIR and config, with `llm.chat` scripted.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/desk_peer_check.py

The interface pinned here
-------------------------
- `UserMessage.origin`'s documentation (provider.py) names "chat" (a message
  from another HUD chat) and "chat-reply" (that chat's reply coming back);
  both arrive with `via="peer"` and `desk=False`.
- `desk.desk_message` is unchanged: a forged `UserMessage(origin="chat",
  desk=True)` is never a desk message — not for `DeskState.begin`, not for the
  fast path's `_at_desk` (the same function).
- `FastPathProvider.steer`: a `chat-reply` steer does NOT clear
  `native.desk["present"]` (P-7/O-2); every other non-desk steer still does.
- `FastPathProvider._take_steering` returns `(text, images, "")` for a
  `chat-reply` item; an owner item is `(text, images)` or carries
  `STEERING_PREFIX` as its third element.
- `Agent._take_steering` accepts items `str`, `(text, images)` and
  `(text, images, prefix)`; an absent prefix is `STEERING_PREFIX`; `""` means
  the text goes in exactly as it is (no prefix, no leading space). Owner
  steers stay byte-identical: "[owner steering] <text>".

Test -> mutation
----------------
  forged origin="chat", desk=True accepted   test_a_forged_chat_message_is_never_at_the_desk,
                                              FastPathDesk.test_a_forged_chat_turn_holds_no_desk_tools
  reply steer clears the desk (P-7)          test_a_reply_steer_keeps_the_fast_paths_desk,
                                              FastPathDesk.test_a_mid_turn_reply_keeps_the_desk_and_has_no_prefix
  prefix applied to replies                  test_agent_take_steering_takes_a_prefix,
                                              test_fast_take_steering_gives_replies_no_prefix,
                                              FastPathDesk.test_a_mid_turn_reply_keeps_the_desk_and_has_no_prefix
  owner steer text regressed                 test_agent_take_steering_takes_a_prefix (owner rows),
                                              FastPathDesk.test_an_owner_desk_steer_is_byte_identical
  other non-desk steers keep the desk        test_a_reply_steer_keeps_the_fast_paths_desk (discord row),
                                              FastPathDesk.test_a_discord_steer_still_takes_the_desk
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import mcp_session_check as M  # noqa: E402  (helpers only; not its setUpModule)
from jarvis import config, llm  # noqa: E402
from jarvis.agent import CONTEXT_BLOCK_PREFIX, STEERING_PREFIX, Agent  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2 import desk  # noqa: E402
from jarvis.v2.approvals import PendingApprovals  # noqa: E402
from jarvis.v2.model import ProviderName, Role  # noqa: E402
from jarvis.v2.provider import Brief, SessionHandle, UserMessage  # noqa: E402
from jarvis.v2.providers import fastpath  # noqa: E402
from jarvis.v2.providers.fastpath import DESK_TOOLS, EDIT_TOOLS, FastPathProvider  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

FAST = ProviderName.FAST
REPLY = ("[A reply from another HUD chat — not from the owner]\n"
         'From: chat abcdef12 "Frontend" · Claude · in answer to message m0123456789ab · answered\n'
         "the port is 8080")


def chat_brief():
    return Brief(Role.CHAT, "/nonexistent/project")


def reply_message(text=REPLY):
    return UserMessage(text, origin="chat-reply", via="peer", desk=False)


# --- the desk predicate and DeskState ------------------------------------------------------


class DeskPredicate(unittest.TestCase):
    def test_a_forged_chat_message_is_never_at_the_desk(self):
        brief = chat_brief()
        for origin in ("chat", "chat-reply"):
            for via in (None, "hud", "peer"):
                with self.subTest(origin=origin, via=via):
                    forged = UserMessage("x", origin=origin, via=via, desk=True)
                    self.assertFalse(desk.desk_message(brief, forged))
                    state = desk.DeskState()
                    self.assertIs(state.begin(brief, forged, "t" * 32).get("present"), False)
        self.assertIs(fastpath._at_desk, desk.desk_message, "the fast path asks the same question")
        # The control: the owner's own HUD message is at the desk.
        self.assertTrue(desk.desk_message(brief, UserMessage("x", desk=True)))
        self.assertTrue(desk.desk_message(brief, UserMessage("x", desk=True, spoken=True)),
                        "CC-6: spoken at the HUD is at the desk")

    def test_provider_documents_the_chat_origins(self):
        source = inspect.getsource(UserMessage)
        for word in ('"chat"', '"chat-reply"', '"peer"'):
            self.assertIn(word, source, f"UserMessage's notes name {word}")


# --- the fast path's steer and take_steering, unit level ----------------------------------


def native(present=True):
    return SimpleNamespace(closed=False, inbox_lock=threading.Lock(), accepting=True,
                           brief=chat_brief(), desk={"present": present}, inbox=[],
                           agent=SimpleNamespace(_base_tool_names=[]))


class FastSteerUnit(unittest.TestCase):
    def steer(self, message):
        n = native()
        FastPathProvider().steer(SessionHandle("aaaa0001", FAST, "fast:x", n), message)
        return n

    def test_a_reply_steer_keeps_the_fast_paths_desk(self):
        cases = {
            "chat-reply": (reply_message(), True),
            "owner at the desk": (UserMessage("and in French", desk=True), True),
            "spoken at the desk": (UserMessage("and in French", desk=True, spoken=True), True),
            "discord": (UserMessage("from discord", via="discord"), False),
            "api listener": (UserMessage("from a script"), False),
            "a relayed message": (UserMessage("from a chat", origin="chat", via="peer"), False),
        }
        for name, (message, kept) in cases.items():
            with self.subTest(case=name):
                n = self.steer(message)
                self.assertIs(n.desk["present"], kept)
                self.assertEqual(len(n.inbox), 1)

    def test_fast_take_steering_gives_replies_no_prefix(self):
        n = native()
        owner = UserMessage("owner words", desk=True)
        n.inbox = [(owner, "owner words", []), (reply_message(), REPLY, [])]
        items = FastPathProvider._take_steering(n)
        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first[:2], ("owner words", None))
        self.assertTrue(len(first) == 2 or first[2] == STEERING_PREFIX, first)
        self.assertEqual(tuple(second), (REPLY, None, ""))


class AgentSteering(unittest.TestCase):
    def take(self, items):
        events = []
        fake = SimpleNamespace(take_steering=lambda: list(items), messages=[],
                               on_event=lambda kind, data: events.append((kind, data)))
        Agent._take_steering(fake)
        return fake.messages, events

    def test_agent_take_steering_takes_a_prefix(self):
        image = {"b64": "iVBORw0KGgo=", "mime": "image/png"}
        messages, events = self.take([
            "plain", ("owner words", None), ("reply text", None, ""),
            ("custom", None, "[x]"), ("with image", [image], "")])
        self.assertEqual(messages[0], {"role": "user", "content": "[owner steering] plain"})
        self.assertEqual(messages[1], {"role": "user", "content": "[owner steering] owner words"},
                         "owner steers are byte-identical")
        self.assertEqual(messages[2], {"role": "user", "content": "reply text"})
        self.assertEqual(messages[3], {"role": "user", "content": "[x] custom"})
        self.assertEqual(messages[4]["content"][0], {"type": "text", "text": "with image"})
        self.assertEqual(messages[4]["content"][1]["type"], "image_url")
        self.assertEqual([d for k, d in events if k == "steered"],
                         ["plain", "owner words", "reply text", "custom", "with image"])


# --- the real fast path in a daemon -------------------------------------------------------


def _reply(text="", calls=None):
    message: dict = {"content": text or None}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, (n, a) in enumerate(calls)]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop",
                     model="stub", latency_s=0.0, cost_usd=0.0)


def _real(messages):
    return [m for m in messages if not (m.get("role") == "user" and
                                        str(m.get("content", "")).startswith(CONTEXT_BLOCK_PREFIX))]


def _content(message):
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


class FastPathDesk(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="desk-peer-")))
        M.patch_config(self.stack, self.root)
        for name, value in dict(SKILLS_DIR=self.root / "skills", SPILL_DIR=self.root / "spill",
                                MEMORY_DIR=self.root / "memory",
                                MODEL_CACHE_PATH=self.root / "cfg" / "catalog.json",
                                AVATAR_ENV="jarvis").items():
            self.stack.enter_context(patch.object(config, name, value))
        (self.root / "skills").mkdir()
        (self.root / "project").mkdir()
        self.requests = []
        self.script = None
        real_chat = llm.chat
        llm.chat = self._chat
        self.addCleanup(setattr, llm, "chat", real_chat)
        self.stores = Stores()
        holder = {}
        approvals = PendingApprovals(
            timeout_s=5.0,
            on_request=lambda request: holder["d"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["d"]._approval_resolved(request, decision, resolution))
        self.d = daemon_mod.Daemon(self.stores, {FAST: FastPathProvider()}, None, 0, approvals)
        holder["d"] = self.d
        self.d.start()
        self.addCleanup(self.d.stop)
        self.project = self.stores.projects.create("School", str(self.root / "project"))
        self.thread = self.d.open_thread(self.project.id, Role.CHAT, FAST, {})

    def _chat(self, model, messages, tools=None, on_delta=None, **kw):
        if tools is None:
            return _reply("a title")
        names = [t["function"]["name"] for t in tools]
        self.requests.append({"names": names, "messages": list(messages)})
        if self.script is not None:
            return self.script(messages, names)
        return _reply("done")

    def idle(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.d._lock:
                session = self.d._sessions.get(self.thread.id)
                if session is None or session.worker is None:
                    return
            time.sleep(0.005)
        self.fail("the turn did not end")

    def steered_turn(self, steer):
        """A desk turn whose first step has `steer` arrive mid-step; returns
        the two requests (before and after the step boundary)."""
        def script(messages, names):
            if len(self.requests) == 1:
                with self.d._lock:
                    handle = self.d._sessions[self.thread.id].handle
                self.d._sessions[self.thread.id].provider.steer(handle, steer)
                return _reply(calls=[("get_datetime", {})])
            return _reply("done")
        self.script = script
        self.d.send(self.thread.id, UserMessage("go", desk=True))
        self.idle()
        self.assertGreaterEqual(len(self.requests), 2, "a second step")
        return self.requests[0], self.requests[1]

    def users(self, request):
        return [_content(m) for m in _real(request["messages"]) if m.get("role") == "user"]

    def test_a_mid_turn_reply_keeps_the_desk_and_has_no_prefix(self):
        before, after = self.steered_turn(reply_message())
        held = set(before["names"]) & DESK_TOOLS
        self.assertTrue(held, "the control: a desk turn holds the desk tools")
        self.assertEqual(set(after["names"]) & DESK_TOOLS, held, "P-7: the reply keeps the desk")
        self.assertEqual(set(after["names"]) & EDIT_TOOLS, set(before["names"]) & EDIT_TOOLS)
        users = self.users(after)
        self.assertIn(REPLY, users, "the reply goes in exactly as written")
        self.assertFalse([u for u in users if u.startswith(STEERING_PREFIX) and REPLY in u],
                         "a reply is never labelled the owner's steering")

    def test_a_discord_steer_still_takes_the_desk(self):
        before, after = self.steered_turn(UserMessage("from discord", via="discord"))
        self.assertTrue(set(before["names"]) & DESK_TOOLS)
        self.assertFalse(set(after["names"]) & (DESK_TOOLS | EDIT_TOOLS))
        self.assertIn("[owner steering] from discord", self.users(after))

    def test_an_owner_desk_steer_is_byte_identical(self):
        before, after = self.steered_turn(UserMessage("and in French", desk=True))
        self.assertEqual(set(after["names"]) & DESK_TOOLS, set(before["names"]) & DESK_TOOLS)
        self.assertIn("[owner steering] and in French", self.users(after))

    def test_a_forged_chat_turn_holds_no_desk_tools(self):
        self.d.send(self.thread.id, UserMessage("real", desk=True))
        self.idle()
        self.assertTrue(set(self.requests[-1]["names"]) & DESK_TOOLS, "the control")
        for origin in ("chat", "chat-reply"):
            with self.subTest(origin=origin):
                self.d.send(self.thread.id, UserMessage("forged", origin=origin, via="peer", desk=True))
                self.idle()
                names = set(self.requests[-1]["names"])
                self.assertFalse(names & (DESK_TOOLS | EDIT_TOOLS), sorted(names & (DESK_TOOLS | EDIT_TOOLS)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
