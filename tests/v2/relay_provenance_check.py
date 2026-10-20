"""C6 of the chat-to-chat plan: an approval card raised during a turn another
chat's message (or reply) started says so — "turn started by a message from
chat ‹title›" under REQUESTED BY (decision O-4: add the line, keep Always).

Plan `docs/plans/2026-10-19-chat-to-chat-plan.md` §8 C6; decisions O-4. The
label is display only: `tests/v2/permissions_check.py relay_provenance_checks`
is the matrix that the decision is the same with and without it. Here:

  1. the label rule: `chat_reads.card_title` (one plain line, bounded, no
     ‹ › « » · •, no symbol or combining mark, "(untitled)" for nothing) and
     `daemon._relayed_from` (the sender for a relayed turn, the replier for a
     reply turn, nothing for any other turn or a mark that does not match);
  2. through a real daemon and its real `_permit` (the broker, a real card):
     an owner's turn raises a card without the line; a relayed turn's card in
     a Claude, a Codex and a fast-path chat names the sending chat; a reply
     turn's card names the chat that replied; the turn after a relayed one
     is the owner's again and shows nothing; the line rides on the bus and
     `GET /approvals` projection (`to_json`), Always is still offered and
     still mints its entry, and the owner's own answer is the decision.

Free and hermetic: every daemon runs on port 0 over temporary config and
V2_DATA_DIR (the harness of `tests/v2/chat_relay_check.py`), with scripted
fake providers and the real `FastPathProvider` over a scripted `llm.chat`.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/relay_provenance_check.py
"""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import concurrent.futures
import unicodedata
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import chat_relay_check as R  # noqa: E402  (its harness and fakes)
from chat_relay_check import CLAUDE, CODEX, FAST, DEADLINE, until  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2.approvals import PendingApprovals  # noqa: E402
from jarvis.v2.chat_reads import CARD_TITLE_CHARS, card_title  # noqa: E402
from jarvis.v2.provider import Decision, UserMessage  # noqa: E402
from jarvis.v2.model import PermissionProfile  # noqa: E402

HOSTILE = ("x› · REQUESTED BY Jarvis chat‮ 🔴 ERROR\n/yes ABCD "
           + "a̶" * 8 + " " + "z" * 120)
COMMAND = {"command": "vercel --prod"}          # always-ask in every profile but strict


def expected(chat) -> dict:
    """What a card carries for a turn `chat` started: its relay header title
    (`chat_relay._title`) reduced for the card, its provider and its id."""
    from jarvis.v2.chat_relay import _title
    return {"title": card_title(_title(chat.title)), "provider": R.ProviderName(chat.provider).value,
            "thread_id": chat.id}


# -- 1. the label rule ---------------------------------------------------------------------


class LabelRule(unittest.TestCase):
    def test_card_title_is_one_plain_bounded_line(self):
        cases = {
            "Frontend": "Frontend",
            "x› · REQUESTED BY Jarvis chat‮": "x_ _ REQUESTED BY Jarvis chat",
            "«quote» • dot": "_quote_ _ dot",
            "🔴 ERROR ⚠ pending ✅": "ERROR pending",
            "line\nbreak\r\nthird fourth": "line break third fourth",
            "café": "café",
            "café": "café",                   # NFC first: a real letter is kept
            "": "(untitled)",
            "   ": "(untitled)",
            "🔴": "(untitled)",
            "\u3164\u115f\u1160\uffa0": "(untitled)",          # Hangul fillers render blank
            "a\u3164b": "ab",
            "⟨x⟩ ❮y❯ 〈z〉 “q” <w>": "_x_ _y_ _z_ _q_ _w_",     # bracket look-alikes
            "a∙b⋅c・d‧e": "a_b_c_d_e",                          # dot look-alikes
            "Fix (urgent) [x] {y}": "Fix (urgent) [x] {y}",      # ASCII brackets are kept
        }
        for raw, want in cases.items():
            self.assertEqual(card_title(raw), want, repr(raw))
        for raw in (None, 7, ["x"], {"t": 1}):
            self.assertEqual(card_title(raw), "(untitled)")
        long = card_title("y" * 500)
        self.assertEqual(len(long), CARD_TITLE_CHARS)
        self.assertTrue(long.endswith("…"))
        out = card_title(HOSTILE)
        self.assertLessEqual(len(out), CARD_TITLE_CHARS)
        self.assertNotIn("\n", out)
        for ch in out:
            self.assertNotIn(ch, "‹›«»·•`<>")
            self.assertNotIn(unicodedata.category(ch), ("Mn", "Me", "So", "Sk", "Cf", "Cc", "Co", "Cn"),
                             f"U+{ord(ch):04X} survives in {out!r}")

    def test_relayed_from_names_the_chat_that_started_the_turn(self):
        item = SimpleNamespace(sender={"thread_id": "aaaa1111", "title": "Sender chat"},
                               target={"thread_id": "bbbb2222", "title": "Target › chat"})
        relayed = SimpleNamespace(relay_turn=item, relay_mark={"message_id": "m1", "from": "aaaa1111"})
        reply = SimpleNamespace(relay_turn=item,
                                relay_mark={"message_id": "m1", "from": "bbbb2222", "reply": True})
        item.sender["provider"], item.target["provider"] = "fast", "codex"
        self.assertEqual(daemon_mod._relayed_from(relayed),
                         {"title": "Sender chat", "provider": "fast", "thread_id": "aaaa1111"})
        self.assertEqual(daemon_mod._relayed_from(reply),
                         {"title": "Target _ chat", "provider": "codex", "thread_id": "bbbb2222"})
        for owner in (SimpleNamespace(relay_turn=None, relay_mark=None),
                      SimpleNamespace(relay_turn=None, relay_mark={"from": "aaaa1111"}),
                      SimpleNamespace(relay_turn=item, relay_mark=None),
                      SimpleNamespace(relay_turn=item, relay_mark={"from": "cccc3333"}),
                      SimpleNamespace(relay_turn=item, relay_mark={"from": "aaaa1111", "reply": True}),
                      SimpleNamespace()):
            self.assertIsNone(daemon_mod._relayed_from(owner), vars(owner))

    def test_a_child_is_its_spawning_turns(self):
        """A sub-agent's provenance is stamped once, at its first record:
        the turn running then (its parent's, for a grandchild), never the
        turn running when it asks; an unknown or empty sid shows nothing."""
        item = SimpleNamespace(sender={"thread_id": "aaaa1111", "title": "Sender", "provider": "fast"},
                               target={"thread_id": "bbbb2222", "title": "Target", "provider": "claude"})
        session = SimpleNamespace(relay_turn=item, relay_mark={"from": "aaaa1111"}, child_relay={})
        sender = daemon_mod._turn_relay(session)
        daemon_mod._stamp_child(session, {"kind": "subagent", "sid": "cl:a", "data": {"parent_sid": None}})
        session.relay_turn = session.relay_mark = None           # the relayed turn ends
        daemon_mod._stamp_child(session, {"kind": "subagent", "sid": "cl:b", "data": {"parent_sid": None}})
        daemon_mod._stamp_child(session, {"kind": "subagent", "sid": "cl:a1", "data": {"parent_sid": "cl:a"}})
        session.relay_turn, session.relay_mark = item, {"from": "aaaa1111"}   # a later relayed turn
        daemon_mod._stamp_child(session, {"kind": "subagent", "sid": "cl:a", "data": {"parent_sid": None}})
        daemon_mod._stamp_child(session, {"kind": "subagent", "sid": "cl:b", "data": {"parent_sid": None}})
        self.assertEqual(daemon_mod._relayed_from(session, "cl:a"), sender, "a background child keeps it")
        self.assertEqual(daemon_mod._relayed_from(session, "cl:a1"), sender, "and so does its own child")
        self.assertIsNone(daemon_mod._relayed_from(session, "cl:b"), "an owner-time child never takes a relay's")
        self.assertIsNone(daemon_mod._relayed_from(session, "cl:unknown"))
        self.assertIsNone(daemon_mod._relayed_from(session, ""))
        self.assertEqual(daemon_mod._relayed_from(session), sender, "the orchestrator's is the running turn's")
        for n in range(daemon_mod.CHILD_STAMPS + 5):
            daemon_mod._stamp_child(session, {"kind": "subagent_event", "sid": f"cl:n{n}", "data": {}})
        self.assertEqual(len(session.child_relay), daemon_mod.CHILD_STAMPS, "bounded")
        self.assertIsNone(daemon_mod._relayed_from(session, "cl:a"), "a forgotten child shows nothing")


# -- 2. through the daemon -----------------------------------------------------------------


class RealPermit(R.Harness):
    """The chat-relay harness with the daemon's own `_permit` (no factory):
    every permit question is the real §6 callback over the real broker."""

    def make_daemon(self):
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

    def asks_on(self, thread, origins, decided, args=COMMAND):
        """`thread`'s provider asks its permit once per turn of these origins."""
        def ask(h, message):
            if message.origin in origins:
                decided.append(Decision(h.native.permit("Bash", dict(args), None)))
            return []
        self.fake(thread).script[thread.id] = ask

    def card(self, thread):
        (card,) = until(lambda: [r for r in self.d.approvals.pending() if r.thread_id == thread.id],
                        f"the card in {thread.id}")
        return card

    def published(self, card):
        def mine():
            return [e for e in self.bus("approval_requested")
                    if (e.get("data") or {}).get("req_id") == card.req_id]
        events = until(mine, "the card's approval_requested on the bus")
        self.assertEqual(len(events), 1)
        return events[0]["data"]


class Cards(RealPermit):
    def test_an_owner_turn_raises_a_card_without_the_line(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                chat, decided = self.chat(provider, "Plain"), []
                self.asks_on(chat, ("owner",), decided)
                self.d.deliver(chat.id, UserMessage("deploy it", desk=True))
                card = self.card(chat)
                self.assertIsNone(card.relayed_from)
                self.assertNotIn("relayed_from", card.to_json())
                self.assertNotIn("relayed_from", self.published(card))
                self.d.resolve_approval(card.req_id, Decision.DENY)
                until(lambda: decided, "the owner's answer")
                self.assertEqual(decided, [Decision.DENY])
                self.idle(chat)

    def test_a_relayed_turn_card_names_the_sending_chat(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target, decided = self.chat(FAST, HOSTILE), self.chat(provider, "Target"), []
                self.asks_on(target, ("chat", "owner"), decided)
                self.turn(sender)
                self.sent(self.send(sender, target, "please deploy"))
                card = self.card(target)
                want = expected(self.stores.threads.get(sender.id))
                self.assertTrue(want["title"].startswith("x_ _ REQUESTED BY Jarvis chat"), want)
                self.assertEqual((want["provider"], want["thread_id"]), ("fast", sender.id))
                self.assertEqual(card.relayed_from, want)
                self.assertEqual(card.to_json()["relayed_from"], want)
                self.assertEqual(self.published(card)["relayed_from"], want)
                self.assertTrue(card.allowlistable, "Always is kept (O-4)")
                # The owner's answer is the decision, as on any card.
                self.d.resolve_approval(card.req_id, Decision.ALLOW)
                until(lambda: decided, "the relayed turn's answer")
                self.assertEqual(decided, [Decision.ALLOW])
                self.idle(target)
                # The next turn is the owner's: its card says nothing of a chat.
                self.d.deliver(target.id, UserMessage("and again", desk=True))
                after = self.card(target)
                self.assertIsNone(after.relayed_from, "a relay's label never outlives its turn")
                self.assertNotIn("relayed_from", after.to_json())
                self.d.resolve_approval(after.req_id, Decision.DENY)
                until(lambda: len(decided) == 2, "the owner turn's answer")
                self.assertEqual(decided, [Decision.ALLOW, Decision.DENY])
                self.idle(target)
                self.release(sender)
                self.idle(sender)

    def test_a_reply_turn_card_names_the_chat_that_replied(self):
        sender, target, decided = self.chat(CLAUDE, "Sender"), self.chat(CODEX, "Replier ‹x›"), []
        self.fake(target).reply[target.id] = "the port is 5173"
        self.fake(target).hold.add(target.id)
        self.asks_on(sender, ("chat-reply",), decided)
        self.turn(sender)
        self.sent(self.send(sender, target, "what port?"))
        self.until_relayed(target)
        self.release(sender)
        self.idle(sender)                 # the sender is idle: the reply runs as its own turn
        self.release(target)
        card = self.card(sender)
        self.assertEqual(card.relayed_from, expected(self.stores.threads.get(target.id)))
        self.assertEqual(card.relayed_from["title"], "Replier _x_")
        self.assertEqual(card.relayed_from["provider"], "codex")
        self.d.resolve_approval(card.req_id, Decision.DENY)
        until(lambda: decided, "the reply turn's answer")
        self.assertEqual(decided, [Decision.DENY])
        self.idle(sender)
        self.idle(target)

    def test_always_on_a_relayed_card_still_mints_its_entry(self):
        sender, decided = self.chat(FAST, "Sender"), []
        # An ASK chat: an ordinary command reaches the owner, allowlistable.
        target = self.d.open_thread(self.project.id, R.Role.CHAT, CLAUDE, {"profile": "ask"})
        self.asks_on(target, ("chat",), decided, {"command": "docker build ."})
        self.turn(sender)
        self.sent(self.send(sender, target, "build it"))
        card = self.card(target)
        self.assertEqual(card.relayed_from["title"], "Sender")
        self.assertTrue(card.allowlistable, "Always is kept (O-4)")
        out = self.d.resolve_approval(card.req_id, Decision.ALLOW, always=True)
        self.assertTrue(out["allowlisted"], out)
        until(lambda: decided, "the answer")
        self.assertEqual(decided, [Decision.ALLOW])
        self.idle(target)
        self.release(sender)
        self.idle(sender)


class Children(RealPermit):
    """A native sub-agent's card carries its spawning turn's provenance, not
    the running turn's (the review of #275, MED): (a) a background child of
    a relayed turn that asks after the turn ended still names the sender;
    (b) a child of an owner's turn that asks while a later relayed turn runs
    names nobody; and a card raised between turns by an unknown child, or by
    no child, names nobody. Claude (`cl:`) and Codex (`cx:`) alike: both hand
    the permit the child's sid (Codex on every child approval, a widening)."""

    def spawns(self, thread, origin, sid):
        """`thread`'s turns of `origin` start background child `sid`."""
        def script(h, message):
            if message.origin == origin:
                return [R.Event(R.K.SUBAGENT, h.thread_id, {"sid": sid, "op": "started",
                                                            "status": "backgrounded",
                                                            "background": True, "name": "bg"})]
            return []
        self.fake(thread).script[thread.id] = script

    def child_asks(self, thread, sid):
        """The child's call, on its own thread as a hook's would be: a card."""
        native = self.fake(thread).handles[thread.id].native
        extra = {"subagent": "bg"} | ({"subagent_sid": sid} if sid else {})
        pool = concurrent.futures.ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown, wait=False)
        future = pool.submit(lambda: Decision(native.permit("Bash", dict(COMMAND), None, **extra)))
        card = self.card(thread)
        self.d.resolve_approval(card.req_id, Decision.DENY)
        self.assertEqual(future.result(DEADLINE), Decision.DENY, "the owner's answer decides it")
        return card

    def stamped(self, thread, sid):
        until(lambda: sid in self.session(thread).child_relay, f"{sid} stamped")

    def test_a_relayed_turns_background_child_names_the_sender_after_the_turn(self):
        for provider, sid in ((CLAUDE, "cl:bg-relayed"), (CODEX, "cx:bg-relayed")):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.spawns(target, "chat", sid)
                self.turn(sender)
                self.sent(self.send(sender, target, "start a background check"))
                self.idle(target)
                self.stamped(target, sid)
                self.assertIsNone(self.session(target).relay_turn, "the relayed turn has ended")
                card = self.child_asks(target, sid)
                self.assertEqual(card.relayed_from, expected(self.stores.threads.get(sender.id)))
                # No turn runs and no known child: nothing.
                self.assertIsNone(self.child_asks(target, provider.value[:2] + ":never-seen").relayed_from)
                self.assertIsNone(self.child_asks(target, None).relayed_from, "an unnamed child")
                self.release(sender)
                self.idle(sender)

    def test_an_owner_turns_child_never_names_a_later_relay(self):
        for provider, sid in ((CLAUDE, "cl:bg-owner"), (CODEX, "cx:bg-owner")):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.spawns(target, "owner", sid)
                self.d.deliver(target.id, UserMessage("start a background check", desk=True))
                self.idle(target)
                self.stamped(target, sid)
                self.fake(target).hold.add(target.id)
                self.turn(sender)
                self.sent(self.send(sender, target, "anything"))
                self.until_relayed(target)
                self.assertIsNotNone(self.session(target).relay_turn, "a relayed turn is running")
                card = self.child_asks(target, sid)
                self.assertIsNone(card.relayed_from, "the owner's child is not the relay's")
                self.assertNotIn("relayed_from", card.to_json())
                # The orchestrator's own call in that turn does name it.
                native = self.fake(target).handles[target.id].native
                pool = concurrent.futures.ThreadPoolExecutor(1)
                self.addCleanup(pool.shutdown, wait=False)
                own = pool.submit(lambda: native.permit("Bash", dict(COMMAND), None))
                mine = self.card(target)
                self.assertEqual(mine.relayed_from, expected(self.stores.threads.get(sender.id)))
                self.d.resolve_approval(mine.req_id, Decision.DENY)
                own.result(DEADLINE)
                self.fake(target).hold.discard(target.id)
                self.release(target)
                self.idle(target)
                self.release(sender)
                self.idle(sender)

    def test_a_card_between_turns_with_no_child_names_nobody(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.turn(sender)
                self.sent(self.send(sender, target, "hello"))
                self.idle(target)
                native = self.fake(target).handles[target.id].native
                pool = concurrent.futures.ThreadPoolExecutor(1)
                self.addCleanup(pool.shutdown, wait=False)
                asked = pool.submit(lambda: native.permit("Bash", dict(COMMAND), None))
                card = self.card(target)
                self.assertIsNone(card.relayed_from)
                self.d.resolve_approval(card.req_id, Decision.DENY)
                asked.result(DEADLINE)
                self.release(sender)
                self.idle(sender)


class FastPathCards(RealPermit):
    """The real fast path. A relayed or reply turn there is never at the desk,
    so it holds no tool that reaches the permit today (`run_command` and the
    edits are desk-only, and no `dangerous` tool is in its set): pinned here.
    Its session's permit carries the label all the same, so a card raised
    from such a turn — asked here from the turn's own thread, as a tool would
    — names the sender; the owner's own `run_command` card in the same chat
    does not, with the same layer and the same Always."""

    def make_daemon(self):
        from jarvis import llm
        from jarvis.v2.providers.fastpath import FastPathProvider
        self.fakes[FAST] = FastPathProvider()
        self.names, self.decided, self.target = [], [], None
        real_chat = llm.chat
        llm.chat = self._chat
        self.addCleanup(setattr, llm, "chat", real_chat)
        return super().make_daemon()

    def _chat(self, model, messages, tools=None, on_delta=None, **kw):
        if tools is None:
            return R._reply("a title")
        last = R._content([m for m in R._real(messages) if m.get("role") == "user"][-1])
        if R._real(messages)[-1].get("role") == "tool":
            return R._reply("done")
        if last.startswith(R.MESSAGE_HEADER):
            self.names.append({t["function"]["name"] for t in tools})
            session = self.session(self.target)
            asked = R.dataclasses.replace(session.brief, profile=PermissionProfile.ASK)
            self.decided.append(Decision(session.permit("run_command", {"command": "rm draft.txt"}, asked)))
            return R._reply("done")
        if last == "owner deploy":
            return R._reply(calls=[("run_command", {"command": "rm draft.txt", "reason": "tidy"})])
        return R._reply("done")

    def test_a_relayed_fast_turn_card_names_the_sender(self):
        from jarvis import tools as registry
        from jarvis.v2.providers.fastpath import ASKED_TOOLS, EDIT_TOOLS
        target, sender = self.chat(FAST, "Fast target"), self.chat(CLAUDE, "Claude ‹sender›")
        self.target = target
        self.turn(sender)
        self.sent(self.send(sender, target, "tidy up"))
        card = self.card(target)
        self.assertEqual(card.relayed_from, {"title": "Claude _sender_", "provider": "claude",
                                             "thread_id": sender.id})
        self.assertTrue(card.allowlistable)
        self.d.resolve_approval(card.req_id, Decision.DENY)
        until(lambda: self.decided, "the relayed turn's answer")
        self.assertEqual(self.decided, [Decision.DENY])
        self.idle(target)
        (names,) = self.names
        dangerous = {n for n in names if n in registry.REGISTRY and registry.REGISTRY[n].dangerous}
        self.assertEqual((names & (EDIT_TOOLS | ASKED_TOOLS), dangerous), (set(), set()),
                         "a relayed fast turn holds no tool that reaches the permit")
        # An owner's turn in the same chat: the real tool's card, no line.
        self.d.deliver(target.id, UserMessage("owner deploy", desk=True))
        owner = self.card(target)
        self.assertEqual((owner.tool, owner.relayed_from), ("run_command", None))
        self.assertNotIn("relayed_from", owner.to_json())
        self.assertEqual((owner.allowlistable, owner.layer), (card.allowlistable, card.layer))
        self.d.resolve_approval(owner.req_id, Decision.DENY)
        self.idle(target)
        self.release(sender)
        self.idle(sender)


if __name__ == "__main__":
    unittest.main(verbosity=2)
