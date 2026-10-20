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
import threading
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


def expected(chat, kind="started") -> dict:
    """What a card carries for a turn `chat` started: its relay header title
    (`chat_relay._title`) reduced for the card, its provider, its id, and
    whether that turn made the asking call (or spawned its child: "started")
    or was only running while it asked ("running")."""
    from jarvis.v2.chat_relay import _title
    return {"title": card_title(_title(chat.title)), "provider": R.ProviderName(chat.provider).value,
            "thread_id": chat.id, "kind": kind}


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
        item = SimpleNamespace(sender={"thread_id": "aaaa1111", "title": "Sender chat", "provider": "fast"},
                               target={"thread_id": "bbbb2222", "title": "Target › chat", "provider": "codex"})
        relayed = SimpleNamespace(relay_turn=item, relay_mark={"message_id": "m1", "from": "aaaa1111"})
        reply = SimpleNamespace(relay_turn=item,
                                relay_mark={"message_id": "m1", "from": "bbbb2222", "reply": True})
        self.assertEqual(daemon_mod._relayed_from(relayed),
                         {"title": "Sender chat", "provider": "fast", "thread_id": "aaaa1111", "kind": "started"})
        self.assertEqual(daemon_mod._relayed_from(reply),
                         {"title": "Target _ chat", "provider": "codex", "thread_id": "bbbb2222", "kind": "started"})
        for owner in (SimpleNamespace(relay_turn=None, relay_mark=None),
                      SimpleNamespace(relay_turn=None, relay_mark={"from": "aaaa1111"}),
                      SimpleNamespace(relay_turn=item, relay_mark=None),
                      SimpleNamespace(relay_turn=item, relay_mark={"from": "cccc3333"}),
                      SimpleNamespace(relay_turn=item, relay_mark={"from": "aaaa1111", "reply": True}),
                      SimpleNamespace()):
            self.assertIsNone(daemon_mod._relayed_from(owner), vars(owner))
        # A provider or id of the wrong shape is left out, never the line.
        odd = SimpleNamespace(sender={"thread_id": "NOT-HEX ·", "title": "Odd", "provider": "owner"}, target={})
        out = daemon_mod._relayed_from(SimpleNamespace(relay_turn=odd, relay_mark={"from": "NOT-HEX ·"}))
        self.assertEqual(out, {"title": "Odd", "provider": "", "thread_id": "", "kind": "started"})


def _session(running=True):
    item = SimpleNamespace(sender={"thread_id": "aaaa1111", "title": "Sender", "provider": "fast"},
                           target={"thread_id": "bbbb2222", "title": "Target", "provider": "claude"})
    session = SimpleNamespace(relay_turn=None, relay_mark=None, child_relay={}, child_seen={},
                              retired=False, thread=SimpleNamespace(id="cccc3333"), turn_id="turn-1",
                              subagent_key="k", brief=SimpleNamespace(cwd=None))
    session.item = item

    def relay(on=True):
        session.relay_turn, session.relay_mark = (item, {"from": "aaaa1111"}) if on else (None, None)
    session.relay = relay
    relay(running)
    return session


def _started(sid, parent=None):
    return {"kind": "subagent", "sid": sid, "data": {"parent_sid": parent}}


class ChildRule(unittest.TestCase):
    """`_stamp_child` and `_relayed_from` for native children (reviews of
    #275): a child keeps the relay that spawned it ("started"); any other
    child, or a call nobody attributes, gets the relayed turn running now
    ("running") or nothing."""

    def test_a_child_is_its_spawning_turns_or_the_running_ones(self):
        session = _session(running=True)
        started = daemon_mod._turn_relay(session)
        running = dict(started, kind="running")
        daemon_mod._stamp_child(session, _started("cl:a"))
        session.relay(False)                                     # the relayed turn ends
        daemon_mod._stamp_child(session, _started("cl:b"))       # an owner turn's child
        daemon_mod._stamp_child(session, _started("cl:a1", "cl:a"))
        daemon_mod._stamp_child(session, _started("cl:b1", "cl:b"))
        self.assertEqual(daemon_mod._relayed_from(session, "cl:a"), started, "kept after the turn")
        self.assertEqual(daemon_mod._relayed_from(session, "cl:a1"), started, "a grandchild inherits it")
        self.assertIsNot(session.child_relay["cl:a1"], session.child_relay["cl:a"], "a copy")
        for child in ("cl:b", "cl:b1", "cl:unknown", ""):
            self.assertIsNone(daemon_mod._relayed_from(session, child), f"{child!r}: no relay runs")
        self.assertIsNone(daemon_mod._relayed_from(session), "the orchestrator, between turns")
        session.relay(True)                                      # a later relayed turn re-tasks them
        for sid in ("cl:a", "cl:b"):
            daemon_mod._stamp_child(session, {"kind": "subagent", "sid": sid, "data": {}})   # never restamped
        self.assertEqual(daemon_mod._relayed_from(session, "cl:a"), started)
        for child in ("cl:b", "cl:b1", "cl:unknown", ""):
            self.assertEqual(daemon_mod._relayed_from(session, child), running, f"{child!r}: honest 'running'")
        self.assertEqual(daemon_mod._relayed_from(session), started, "the orchestrator's own turn")

    def test_an_evicted_child_is_never_restamped(self):
        session = _session(running=True)
        daemon_mod._stamp_child(session, _started("cl:old"))
        for n in range(daemon_mod.CHILD_STAMPS + 5):
            daemon_mod._stamp_child(session, _started(f"cl:n{n}"))
        self.assertEqual(len(session.child_relay), daemon_mod.CHILD_STAMPS, "stamps bounded")
        self.assertNotIn("cl:old", session.child_relay)
        session.relay(False)
        daemon_mod._stamp_child(session, _started("cl:old"))     # seen: not restamped from anything
        self.assertNotIn("cl:old", session.child_relay)
        self.assertIsNone(daemon_mod._relayed_from(session, "cl:old"))
        session.relay(True)
        self.assertEqual(daemon_mod._relayed_from(session, "cl:old")["kind"], "running")
        for n in range(daemon_mod.CHILD_SEEN + 5):
            daemon_mod._stamp_child(session, {"kind": "subagent_event", "sid": f"cl:e{n}", "data": {}})
        self.assertEqual(len(session.child_seen), daemon_mod.CHILD_SEEN, "the seen set is bounded too")

    def _record(self, session, *, idle, during_prepare=None):
        fake = SimpleNamespace(_lock=threading.Lock(), subagents=SimpleNamespace(record=lambda *a: None))
        from jarvis.v2.subagents import SubagentRecords
        real = SubagentRecords.prepare

        def prepare(event, cwd):
            if during_prepare is not None:
                during_prepare()
            return real(event, cwd)
        event = R.Event(R.K.SUBAGENT, "cccc3333", {"sid": "cl:x", "op": "started", "name": "x"})
        with R.patch.object(SubagentRecords, "prepare", staticmethod(prepare)):
            daemon_mod.Daemon._record_subagent(fake, session, event, idle=idle)

    def test_a_child_first_seen_between_turns_takes_no_relay(self):
        session = _session(running=True)                # a relayed turn began while it was read
        self._record(session, idle=True)
        self.assertIn("cl:x", session.child_seen)
        self.assertNotIn("cl:x", session.child_relay)

    def test_a_turn_that_began_while_the_record_was_prepared_stamps_nothing(self):
        session = _session(running=False)

        def begin():                                  # `_begin` of a relayed turn, meanwhile
            session.relay(True)
            session.turn_id = "turn-2"
        self._record(session, idle=False, during_prepare=begin)
        self.assertIn("cl:x", session.child_seen)
        self.assertNotIn("cl:x", session.child_relay)
        # The same record in the turn it was read in does stamp it.
        fresh = _session(running=True)
        self._record(fresh, idle=False)
        self.assertEqual(fresh.child_relay["cl:x"]["kind"], "started")


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
    """A native sub-agent's card (reviews of #275): (a) a background child of
    a relayed turn that asks after the turn ended still names the sender,
    "started"; (b) a child of an owner's turn that a later relayed turn
    re-tasks (Claude continuing the agent, Codex sending it input) and that
    asks while that turn runs names it as "running", and nothing once it
    ends; a bridge call while a relayed turn runs is "running" too; and a
    card between turns by an unknown child, or by no child, names nobody.
    Claude (`cl:`) and Codex (`cx:`) alike: both hand the permit the child's
    sid (Codex on every child approval, a widening)."""

    def spawns(self, thread, origin, sid, op="started"):
        """`thread`'s turns of `origin` report child `sid` (`op`)."""
        def script(h, message):
            if message.origin == origin:
                return [R.Event(R.K.SUBAGENT, h.thread_id, {"sid": sid, "op": op,
                                                            "status": "backgrounded",
                                                            "background": True, "name": "bg"})]
            return []
        self.fake(thread).script[thread.id] = script

    def asks(self, thread, tool="Bash", args=COMMAND, **extra):
        """A call on its own thread, as a hook's or the bridge's would be: its card."""
        native = self.fake(thread).handles[thread.id].native
        pool = concurrent.futures.ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown, wait=False)
        future = pool.submit(lambda: Decision(native.permit(tool, dict(args), None, **extra)))
        card = self.card(thread)
        self.d.resolve_approval(card.req_id, Decision.DENY)
        self.assertEqual(future.result(DEADLINE), Decision.DENY, "the owner's answer decides it")
        return card

    def child_asks(self, thread, sid):
        return self.asks(thread, subagent="bg", **({"subagent_sid": sid} if sid else {}))

    def noticed(self, thread, sid):
        until(lambda: sid in self.session(thread).child_seen, f"{sid} seen")

    def relayed_running(self, sender, target):
        """A relayed turn from `sender`, held running in `target`."""
        self.fake(target).hold.add(target.id)
        self.turn(sender)
        self.sent(self.send(sender, target, "anything"))
        self.until_relayed(target)
        self.assertIsNotNone(self.session(target).relay_turn, "a relayed turn is running")

    def end(self, sender, target):
        self.fake(target).hold.discard(target.id)
        self.release(target)
        self.idle(target)
        self.release(sender)
        self.idle(sender)

    def test_a_relayed_turns_background_child_names_the_sender_after_the_turn(self):
        for provider, sid in ((CLAUDE, "cl:bg-relayed"), (CODEX, "cx:bg-relayed")):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.spawns(target, "chat", sid)
                self.turn(sender)
                self.sent(self.send(sender, target, "start a background check"))
                self.idle(target)
                self.noticed(target, sid)
                self.assertIsNone(self.session(target).relay_turn, "the relayed turn has ended")
                card = self.child_asks(target, sid)
                self.assertEqual(card.relayed_from, expected(self.stores.threads.get(sender.id)))
                self.assertEqual(card.to_json()["relayed_from"]["kind"], "started")
                # No turn runs and no known child: nothing.
                self.assertIsNone(self.child_asks(target, provider.value[:2] + ":never-seen").relayed_from)
                self.assertIsNone(self.child_asks(target, None).relayed_from, "an unnamed child")
                self.release(sender)
                self.idle(sender)

    def test_an_owner_turns_child_never_names_a_later_relay(self):
        """...as the turn that started it: while a relayed turn runs, its card
        says so ("running"), and the orchestrator's own card in that turn is
        "started"."""
        for provider, sid in ((CLAUDE, "cl:bg-owner"), (CODEX, "cx:bg-owner")):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.spawns(target, "owner", sid)
                self.d.deliver(target.id, UserMessage("start a background check", desk=True))
                self.idle(target)
                self.noticed(target, sid)
                self.assertNotIn(sid, self.session(target).child_relay, "an owner turn's child: no relay")
                self.relayed_running(sender, target)
                card = self.child_asks(target, sid)
                self.assertEqual(card.relayed_from, expected(self.stores.threads.get(sender.id), "running"))
                self.assertEqual(card.to_json()["relayed_from"]["kind"], "running")
                mine = self.asks(target)
                self.assertEqual(mine.relayed_from, expected(self.stores.threads.get(sender.id)))
                self.end(sender, target)

    def test_a_child_re_tasked_by_a_relayed_turn_says_while_it_runs(self):
        """Owner turn N starts child C; relayed turn N+1 gives C new work (a
        record of C during it: Claude continuing the agent, Codex its input).
        C keeps no relay stamp, so its cards say "running" during N+1 and
        nothing after it."""
        for provider, sid in ((CLAUDE, "cl:re-tasked"), (CODEX, "cx:re-tasked")):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.spawns(target, "owner", sid)
                self.d.deliver(target.id, UserMessage("start a background helper", desk=True))
                self.idle(target)
                self.noticed(target, sid)
                self.spawns(target, "chat", sid, op="progress")     # N+1 re-tasks C
                self.relayed_running(sender, target)
                until(lambda: self.session(target).relay_turn is not None, "the relayed turn")
                self.assertNotIn(sid, self.session(target).child_relay, "never restamped")
                card = self.child_asks(target, sid)
                self.assertEqual(card.relayed_from, expected(self.stores.threads.get(sender.id), "running"))
                self.end(sender, target)
                self.assertIsNone(self.child_asks(target, sid).relayed_from, "after N+1: nothing")

    def test_a_bridge_call_during_a_relayed_turn_is_running(self):
        """The jarvis-mcp bridge's dangerous-tool ask cannot tell a child from
        the orchestrator: never "started", "running" while a relay runs."""
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.relayed_running(sender, target)
                card = self.asks(target, "gmail_send", {"to": "a@example.com", "body": "hi"},
                                 dangerous_tool=True)
                self.assertEqual(card.relayed_from, expected(self.stores.threads.get(sender.id), "running"))
                self.end(sender, target)
                self.assertIsNone(self.asks(target, "gmail_send", {"to": "a@example.com", "body": "hi"},
                                            dangerous_tool=True).relayed_from)

    def test_a_card_between_turns_with_no_child_names_nobody(self):
        for provider in (CLAUDE, CODEX):
            with self.subTest(provider=provider.value):
                sender, target = self.chat(FAST, "Sender desk"), self.chat(provider, "Target")
                self.turn(sender)
                self.sent(self.send(sender, target, "hello"))
                self.idle(target)
                self.assertIsNone(self.asks(target).relayed_from)
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
                                             "thread_id": sender.id, "kind": "started"})
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
