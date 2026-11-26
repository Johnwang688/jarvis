"""Orchestrator-owned acceptance tests for A6c-1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the v3 chat's six write tools ask the owner through an in-memory broker that only two owner routes can answer.
  jarvis/v3/approvals.py   light: never imports jarvis.agent/llm/models/tools, jarvis.v3.fastpath, httpx or openai
    ApprovalBroker(*, clock=time.monotonic, timeout_s=120.0, new_id=<secrets.token_urlsafe>, log=None)
      log: path of approvals.jsonl (None: no log). Append-only, 0600, never through a link, parent created 0700 if missing. One JSON row
           per transition, keys exactly {"id", "tool", "digest", "level", "why", "state", "t"} (t: time.time()); never arguments, cards or previews.
           Each request: a "pending" row written before it is listed (if it cannot be written the ask denies), then exactly one final row.
      ask(tool, args, card, *, session_id, turn_id, verdict, preview=None, cancelled=None) -> bool
           blocks the calling turn thread; True only when the owner approved. Waits in real-time slices of at most 0.1 s; expires once
           clock() >= asked + timeout_s; polls cancelled() each slice (one that raises counts as cancelled). Timeout, cancel, shutdown and
           any exception deny. verdict: Policy().explain's {"level", "why"}.
      pending() -> [{"id", "digest", "tool", "card", "level", "why", "asked_at", "expires_in"}]   oldest first, exactly these keys, never args;
           asked_at: time.time() at the ask; expires_in: max(0, asked + timeout_s - clock()) seconds
      answer(id, digest, decision) -> "approved" | "denied"    decision "approve" | "deny", else ValueError. Once, under the lock.
           KeyError: an id this broker never issued. ApprovalConflict(Exception, not a KeyError): issued but no longer pending, or a digest
           that differs. Either refusal leaves the state unchanged.
      cancel(turn_id=None, reason="stopped")   that turn's waiters (None: all) end "cancelled".   shutdown(): idempotent; cancels all; later asks deny at once, no row.
    digest(id, session_id, tool, args, preview) -> sha256 hex of json.dumps({"v": 1, "id", "session_id", "tool", "args_raw": args, "preview": preview},
           sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False), over the RAW arguments; the random id is the salt.
    States: pending -> approved | denied | expired | cancelled, each final. Pending lives in memory only: a new broker (a restart) has none.
  jarvis/v3/approval_card.py builds the card: {"title": str, "rows": [{"label": str, "value": str}, ...]}, every string redacted with v2's card
    rules (jarvis.v2.redact), so a credential-shaped argument never reaches the card. calendar_create's card has a row "no attendees, no emails sent".
  jarvis/v3/chat.py   ChatService(root, *, completion=None, clock=time.time, feed_root=None, approvals=None, calendar=None)
      .approvals: the broker or None. None: exactly A6b-1 (ten tools, deny_all, nothing asks). With a broker the chat also offers GATED_TOOLS:
      vault_add, vault_patch, vault_archive always; calendar_create, calendar_update, calendar_delete only when config.GOOGLE_TOKEN_PATH is a file.
      calendar: None or callable(approve=None) -> CalendarTools, used by every calendar tool (default CalendarTools(token=google_auth.access_token, approve=approve)).
      snapshot() and events() gain "approvals": len(pending) (0 without a broker). No new event kind; tool events name gated tools by name.
      Stop, close() and Daemon.stop() release a waiting approval as "cancelled" within about 0.2 s; the turn then ends as stopped.
  jarvis/v3/fastpath.py   GATED_TOOLS == chat.GATED_TOOLS, registered by register_tools(): dangerous=True, in tools.EXPLICIT_ONLY, never PARALLEL_SAFE.
      V3_TOOLS stays the ten. vault_add(folder, name, description, body, source)  vault_patch(name, old, new)  vault_archive(name, replaced_by="")
      calendar_create(title, start, end, description="", reminders=None)  calendar_update(event_id, title=None, start=None, end=None, description=None)  calendar_delete(event_id)
    ChatApprover(broker, turn): the turn's approver when the chat has a broker (deny_all stays without one). Never None, never jarvis_human_backed.
      __call__(tool, args) -> bool | tools.Refusal: preview first (the calendar event as it is now; the vault entry, its revision and the
      content-scan verdict), then Policy().explain({"kind", "tainted": True, ...}) with kinds calendar_create calendar.block (attendees 0),
      calendar_update calendar.event_change, calendar_delete calendar.event_delete, vault_* vault.update. L0-L2 ask (H10). Refused with no card
      and no row (the model reads "Refused: ..."): L3; a body, description or source the scanner blocks; vault_patch of an entry not read with
      vault_read earlier in the same turn; any failure while building the preview, verdict or card.
    Calendar grant: an approved calendar write sets a one-shot grant passed to that call's CalendarTools as approve=. It returns True at most
      once, only for that action, and only while details["before"] (update/delete) carries the etag the card showed. With no grant (an approver
      bypassed, a direct call) the tool answers "Error: Calendar write requires approval." and sends nothing.
  Routes (owner-only, in owner_routes.ROUTES, A6a's rules): GET /v3/owner/approvals -> 200 {"approvals": pending()} ([] without a broker);
    POST /v3/owner/approvals/answer exactly {"id": str, "digest": str, "decision": "approve" | "deny"} -> 200 {"id", "state"}; 400 malformed;
    404 unknown id (or no broker); 409 ApprovalConflict. In jarvis/v3 only owner_routes.py refers to an `.answer` attribute (approvals.py defines it).
    Daemon(chat=None) builds ChatService(<data_dir>/chats, approvals=ApprovalBroker(log=<data_dir>/approvals.jsonl)) as Daemon._chat.
  Every existing A6b test passes unchanged (gates in the task card). Not here: the hud3 card (A6c-2), shell and file tools (A6c-3).
"""
import ast
import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis import config, runtime
from a6b1_acceptance_check import Base as A6bBase, Model, reply

REPO = Path(__file__).resolve().parents[2]
A6B = {"get_datetime", "vault_list", "vault_read", "vault_search", "index_search", "skill_view", "skill_names", "calendar_list", "calendar_free", "brief_post"}
CAL = {"calendar_list", "calendar_free"}
SIGNATURES = {"vault_add": ({"folder", "name", "description", "body", "source"}, {"folder", "name", "description", "body", "source"}),
              "vault_patch": ({"name", "old", "new"}, {"name", "old", "new"}), "vault_archive": ({"name", "replaced_by"}, {"name"}),
              "calendar_create": ({"title", "start", "end", "description", "reminders"}, {"title", "start", "end"}),
              "calendar_update": ({"event_id", "title", "start", "end", "description"}, {"event_id"}), "calendar_delete": ({"event_id"}, {"event_id"})}
KEYS = {"id", "digest", "tool", "card", "level", "why", "asked_at", "expires_in"}
FAKE_TOKEN = "ghp" + "_" + "Qx7v" * 9                      # credential-shaped, fake
ADD = {"folder": "facts", "name": "favourite-tea", "description": "What the owner drinks", "body": "The owner prefers green tea.", "source": "owner chat"}
NOW = datetime(2030, 1, 7, 9, tzinfo=ZoneInfo("America/Chicago")).timestamp()
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CREATE = {"title": "Planning session", "start": "2030-01-09T10:00:00-06:00", "end": "2030-01-09T11:00:00-06:00", "description": "Bring the badge. " + FAKE_TOKEN}
SID, CARD, VERDICT, PREVIEW = "a" * 32, {"title": "Add a vault entry", "rows": [{"label": "Body", "value": "note"}]}, {"level": "L2", "why": "hard:tainted"}, {"revision": None, "flags": []}
ANSWER = "/v3/owner/approvals/answer"


def ap():
    from jarvis.v3 import approvals
    return approvals


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Cal:
    """Google Calendar's primary events, faked: one Jarvis-owned event e1 whose etag a test can change (`version`)."""

    def __init__(self):
        self.calls, self.version, self.fail = [], 1, None

    def __call__(self, method, url, *, params=None, json=None):
        self.calls.append((method, url, dict(params or {}), json))
        if self.fail is not None:
            raise self.fail
        event = {"id": "e1", "etag": f'"e1-{self.version}"', "summary": "Dentist checkup", "status": "confirmed", "organizer": {"self": True}, "creator": {"self": True},
                 "start": {"dateTime": "2030-01-08T10:00:00-06:00"}, "end": {"dateTime": "2030-01-08T11:00:00-06:00"}, "extendedProperties": {"private": {"jarvis": "1"}}}
        if url == EVENTS and method == "POST":
            return 200, dict(json, id="new1", etag='"new1-1"')
        if url == EVENTS + "/e1":
            return {"GET": (200, event), "PATCH": (200, {**event, **(json or {})}), "DELETE": (204, {})}.get(method, (405, {}))
        return 404, {}

    def writes(self):
        return [call[0] for call in self.calls if call[0] != "GET"]


class Broker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log, self.clock = self.tmp / "v3" / "approvals.jsonl", Clock()
        self.b = ap().ApprovalBroker(clock=self.clock, timeout_s=120.0, log=self.log)
        self.addCleanup(self.b.shutdown)

    def ask(self, turn="t1", args=None, cancelled=None):
        out, args = [], {"name": "favourite-tea"} if args is None else args
        thread = threading.Thread(target=lambda: out.append(self.b.ask("vault_add", args, CARD, session_id=SID, turn_id=turn, verdict=VERDICT, preview=PREVIEW, cancelled=cancelled)), daemon=True)
        thread.start()
        return thread, out

    def wait(self, n):
        end = time.monotonic() + 10
        while len(self.b.pending()) != n:
            self.assertLess(time.monotonic(), end, "the broker did not list the request")
            time.sleep(0.01)
        return self.b.pending()

    def finished(self, thread, out):
        thread.join(5)
        self.assertFalse(thread.is_alive(), "ask() is still blocked")
        return out[0]

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_digest_uses_the_raw_value_and_nothing_secret_is_listed_or_logged(self):
        args = {"name": "favourite-tea", "body": "note " + FAKE_TOKEN}
        thread, out = self.ask(args=args)
        (item,) = self.wait(1)
        self.assertEqual(set(item), KEYS)
        self.assertEqual((item["tool"], item["card"], item["level"], item["why"]), ("vault_add", CARD, "L2", "hard:tainted"))
        canon = json.dumps({"v": 1, "id": item["id"], "session_id": SID, "tool": "vault_add", "args_raw": args, "preview": PREVIEW}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(item["digest"], hashlib.sha256(canon.encode()).hexdigest())
        self.assertEqual(item["digest"], ap().digest(item["id"], SID, "vault_add", args, PREVIEW))
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.b.pending()))
        self.assertEqual(self.b.answer(item["id"], item["digest"], "approve"), "approved")
        self.assertIs(self.finished(thread, out), True)
        self.assertEqual([(r["id"], r["state"]) for r in self.rows()], [(item["id"], "pending"), (item["id"], "approved")])
        self.assertTrue(all(set(r) == {"id", "tool", "digest", "level", "why", "state", "t"}
                            and r["digest"] == item["digest"] for r in self.rows()))
        self.assertNotIn(FAKE_TOKEN, self.log.read_text())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_an_unanswered_request_expires_on_the_injected_clock(self):
        thread, out = self.ask()
        (item,) = self.wait(1)
        self.assertAlmostEqual(item["expires_in"], 120.0, places=3)
        self.clock.now += 30
        self.assertAlmostEqual(self.b.pending()[0]["expires_in"], 90.0, places=3)
        self.clock.now += 91
        self.assertIs(self.finished(thread, out), False)
        self.assertEqual(self.b.pending(), [])
        with self.assertRaises(ap().ApprovalConflict):
            self.b.answer(item["id"], item["digest"], "approve")
        self.assertEqual([r["state"] for r in self.rows()], ["pending", "expired"])

    def test_answers_are_once_only_and_bound_to_the_digest(self):
        thread, out = self.ask()
        (item,) = self.wait(1)
        with self.assertRaises(ap().ApprovalConflict):
            self.b.answer(item["id"], "0" * 64, "approve")
        with self.assertRaises(ValueError):
            self.b.answer(item["id"], item["digest"], "maybe")
        with self.assertRaises(KeyError):
            self.b.answer("never-issued", item["digest"], "approve")
        self.assertEqual([p["id"] for p in self.b.pending()], [item["id"]])
        self.assertEqual(self.b.answer(item["id"], item["digest"], "deny"), "denied")
        with self.assertRaises(ap().ApprovalConflict):
            self.b.answer(item["id"], item["digest"], "approve")
        self.assertIs(self.finished(thread, out), False)
        self.assertEqual([r["state"] for r in self.rows()], ["pending", "denied"])

    def test_cancel_shutdown_and_a_restart_leave_nothing_to_approve(self):
        first_ask = self.ask("t1")
        (first,) = self.wait(1)
        second_ask = self.ask("t2")
        self.wait(2)
        self.b.cancel("t1")
        self.assertIs(self.finished(*first_ask), False)
        self.assertNotIn(first["id"], [p["id"] for p in self.wait(1)])
        self.b.shutdown()
        self.b.shutdown()
        self.assertIs(self.finished(*second_ask), False)
        self.assertEqual(self.b.pending(), [])
        self.assertIs(self.b.ask("vault_add", {}, CARD, session_id=SID, turn_id="t3", verdict=VERDICT), False)
        self.assertEqual(sorted(r["state"] for r in self.rows()), ["cancelled", "cancelled", "pending", "pending"])
        again = ap().ApprovalBroker(clock=self.clock, log=self.log)
        self.assertEqual(again.pending(), [])
        with self.assertRaises(KeyError):
            again.answer(first["id"], first["digest"], "approve")

    def test_a_cancel_check_that_raises_denies(self):
        self.assertIs(self.finished(*self.ask(cancelled=lambda: 1 / 0)), False)
        self.assertEqual(self.b.pending(), [])


class Base(A6bBase):
    def setUp(self):
        super().setUp()
        self.log, self.policy_file, self.token = self.v3 / "approvals.jsonl", self.tmp / "policy" / "policy.json", self.tmp / "google_token.json"
        self.enterContext(patch.object(config, "V3_POLICY_FILE", self.policy_file))
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.token))
        self.clock, self.cal = Clock(), Cal()
        self.broker = ap().ApprovalBroker(log=self.log)
        self.addCleanup(self.broker.shutdown)

    def talk(self, *steps):
        """A chat with this test's broker and fake calendar (a6b1's Base keeps `self.chat` for the module)."""
        from jarvis.v3.tools.calendar import CalendarTools
        self.model = Model(*steps)
        return self.service(self.model, approvals=self.broker, calendar=lambda approve=None: CalendarTools(request=self.cal, now=lambda: NOW, approve=approve))

    def next_card(self):
        end = time.monotonic() + 10
        while not self.broker.pending():
            self.assertLess(time.monotonic(), end, "no approval card appeared")
            time.sleep(0.01)
        return self.broker.pending()[0]

    def results(self):
        return [m["content"] for m in self.model.seen[-1]["messages"] if m.get("role") == "tool"]

    def states(self, rid):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [r["state"] for r in rows if rid is None or r["id"] == rid]

    def notes(self):
        vault = self.v3 / "vault"
        return sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md")) if vault.exists() else []

    def vault(self):
        from jarvis.v3.vault import Vault
        return Vault(self.v3 / "vault")


class ChatFlow(Base):
    def test_an_approved_vault_add_writes_once_and_is_logged(self):
        from jarvis.v3 import fastpath
        approvers = []
        svc = self.talk(lambda messages: (approvers.append(runtime.approver()), reply(calls=[("vault_add", ADD)]))[1], reply("Saved."))
        svc.send("Remember that I like green tea.")
        item = self.next_card()
        self.assertEqual((item["tool"], item["level"], item["why"]), ("vault_add", "L0", "auto-off"))
        self.assertIn("The owner prefers green tea.", json.dumps(item["card"]))
        self.assertEqual((svc.snapshot()["approvals"], svc.events(0)["approvals"]), (1, 1))
        self.assertEqual(self.broker.answer(item["id"], item["digest"], "approve"), "approved")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.notes(), ["facts/favourite-tea.md"])
        self.assertEqual(self.states(item["id"]), ["pending", "approved"])
        self.assertIn("favourite-tea", self.results()[-1])
        self.assertFalse(self.results()[-1].startswith(("Error", "Refused", "The user declined")))
        self.assertEqual([(e["name"], e["ok"]) for e in svc.events(0)["events"] if e["kind"] == "tool_end"], [("vault_add", True)])
        self.assertIsInstance(approvers[0], fastpath.ChatApprover)
        self.assertFalse(getattr(approvers[0], "jarvis_human_backed", False))
        self.assertEqual(svc.snapshot()["approvals"], 0)

    def test_a_denied_write_changes_nothing(self):
        self.vault().add("facts", "favourite-tea", "What the owner drinks", "The owner prefers green tea.", "owner chat")
        svc = self.talk(reply(calls=[("vault_archive", {"name": "favourite-tea"})]), reply("Kept."))
        svc.send("Archive the tea note.")
        item = self.next_card()
        self.assertEqual(self.broker.answer(item["id"], item["digest"], "deny"), "denied")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("The user declined"), self.results())
        self.assertEqual(self.vault().read("favourite-tea").status, "active")
        self.assertEqual(self.states(item["id"]), ["pending", "denied"])

    def test_stop_and_close_release_a_waiting_approval(self):
        for how in ("stop", "close"):
            with self.subTest(how):
                svc = self.talk(reply(calls=[("vault_add", ADD)]), reply("never reached"))
                svc.send("Remember my tea.")
                item, begun = self.next_card(), time.monotonic()
                if how == "stop":
                    svc.stop()
                    self.assertEqual(self.wait_idle(svc), "idle")
                    self.assertIn("stopped", self.kinds(svc))
                else:
                    self.assertTrue(svc.close(grace_s=5.0))
                self.assertLess(time.monotonic() - begun, 4)
                self.assertEqual(self.broker.pending(), [])
                self.assertEqual(self.states(item["id"]), ["pending", "cancelled"])
                self.assertEqual(self.notes(), [])

    def test_scanner_blocks_and_policy_l3_are_refused_without_a_card(self):
        from jarvis.v3 import policy
        svc = self.talk(reply(calls=[("vault_add", dict(ADD, name="token-note", body="token " + FAKE_TOKEN))]), reply("Refused."),
                        reply(calls=[("vault_add", ADD)]), reply("Refused again."))
        svc.send("Save this note.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Refused:"), self.results())
        policy.set_rule(self.policy_file, "vault.update", "L3")
        svc.send("Save my tea.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Refused:"), self.results())
        self.assertEqual((self.states(None), self.notes()), ([], []))

    def test_a_patch_needs_a_read_in_the_same_turn_and_shows_old_and_new(self):
        self.vault().add("facts", "favourite-tea", "What the owner drinks", "The owner prefers green tea.", "owner chat")
        change = {"name": "favourite-tea", "old": "green", "new": "oolong"}
        svc = self.talk(reply(calls=[("vault_patch", change)]), reply("I must read it first."),
                        reply(calls=[("vault_read", {"name": "favourite-tea"})]), reply(calls=[("vault_patch", change)]), reply("Patched."))
        svc.send("Change green to oolong.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Refused:"), self.results())
        self.assertEqual(self.states(None), [])
        svc.send("Read it, then change it.")
        item = self.next_card()
        self.assertTrue(all(word in json.dumps(item["card"]) for word in ("green", "oolong")), item["card"])
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIn("oolong", self.vault().read("favourite-tea").body)
        self.assertEqual(self.states(item["id"]), ["pending", "approved"])

    def test_read_tools_never_ask_and_a_chat_without_a_broker_is_unchanged(self):
        svc = self.talk(reply(calls=[("vault_list", {}), ("vault_search", {"query": "tea"}), ("skill_names", {}), ("get_datetime", {}),
                                     ("index_search", {"query": "tea"}), ("brief_post", {"kind": "news", "ask": "A test brief"})]), reply("Done."))
        svc.send("Look around.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.states(None), [])
        self.assertFalse(any(text.startswith(("The user declined", "Refused:")) for text in self.results()), self.results())
        offered = A6B - CAL | {"vault_add", "vault_patch", "vault_archive"}                 # no Google grant here
        self.assertEqual((set(self.model.seen[0]["tools"]), set(svc.snapshot()["tools"])), (offered, offered))
        plain = self.service(Model(reply("Hi.")))
        self.assertEqual((set(plain.snapshot()["tools"]), plain.snapshot()["approvals"], plain.approvals), (A6B - CAL, 0, None))

    def test_failures_leave_nothing_pending(self):
        self.token.write_text("fake-token")
        self.cal.fail = RuntimeError("private fixture detail")
        svc = self.talk(reply(calls=[("calendar_update", {"event_id": "e1", "title": "Orthodontist visit"})]), RuntimeError("provider failed"))
        svc.send("Rename my appointment.")
        self.assertEqual(self.wait_idle(svc), "error")
        self.assertTrue(self.results()[-1].startswith("Refused:"), self.results())
        self.assertEqual((self.broker.pending(), svc.snapshot()["approvals"], self.states(None), self.cal.writes()), ([], 0, [], []))


class CalendarFlow(Base):
    def setUp(self):
        super().setUp()
        self.token.write_text("fake-token")

    def test_an_event_changed_after_approval_is_not_written(self):
        rename = ("calendar_update", {"event_id": "e1", "title": "Orthodontist visit"})
        svc = self.talk(reply(calls=[rename]), reply("It changed."), reply(calls=[rename]), reply("Renamed."))
        svc.send("Rename my appointment.")
        item = self.next_card()
        self.assertTrue(all(part in json.dumps(item["card"]) for part in ("Dentist checkup", "Orthodontist visit")), item["card"])
        self.assertEqual((item["level"], item["why"]), ("L1", "auto-off"))
        self.cal.version = 2                                         # someone edits the event after the owner saw the card
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.cal.writes(), [])
        self.assertTrue(self.results()[-1].startswith("Error"), self.results())
        svc.send("Try again.")
        item = self.next_card()
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.cal.writes(), ["PATCH"])

    def test_delete_asks_with_the_event_as_it_is_now(self):
        svc = self.talk(reply(calls=[("calendar_delete", {"event_id": "e1"})]), reply("Deleted."))
        svc.send("Delete my appointment.")
        item = self.next_card()
        self.assertIn("Dentist checkup", json.dumps(item["card"]))
        self.assertEqual((item["tool"], item["why"]), ("calendar_delete", "default:calendar.event_delete"))
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.cal.writes(), ["DELETE"])

    def test_without_a_grant_the_calendar_sends_nothing_even_past_the_approver(self):
        from jarvis.v3 import fastpath
        with patch.object(fastpath.ChatApprover, "__call__", lambda self, tool, args: True):
            svc = self.talk(reply(calls=[("calendar_create", CREATE), ("calendar_delete", {"event_id": "e1"})]), reply("Done."))
            svc.send("Book and delete.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.cal.writes(), [])
        self.assertTrue(all(text.startswith("Error") for text in self.results()), self.results())
        self.assertEqual(self.states(None), [])


class Routes(Base):
    def start(self, *steps):
        from jarvis.v3 import daemon
        self.token.write_text("fake-token")
        self.svc = self.talk(*steps)
        self.d = daemon.Daemon(port=0, data_dir=self.v3, hud_dir=self.tmp / "hud3", jobs=[], chat=self.svc)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.host = f"127.0.0.1:{self.d.port}"

    def call(self, method, path, body=None, owner=True, headers=None):
        sent = {"Host": self.host}
        if owner:
            sent.update({"Origin": f"http://{self.host}"} if method == "POST" else {"Sec-Fetch-Site": "same-origin"})
        sent.update(headers or {})
        data = json.dumps(body).encode() if method == "POST" else None
        if data is not None:
            sent.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request(method, path, body=data, headers=sent)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, (json.loads(text) if text else None)

    def test_listing_counts_redaction_and_an_approved_create(self):
        self.start(reply(calls=[("calendar_create", CREATE)]), reply("Created."))
        self.assertEqual(self.call("POST", "/v3/owner/chat/send", {"text": "Book a planning session."})[0], 202)
        item = self.next_card()
        status, out = self.call("GET", "/v3/owner/approvals")
        (shown,) = out["approvals"]
        self.assertEqual((status, set(shown)), (200, KEYS))
        self.assertEqual((shown["id"], shown["tool"], shown["level"], shown["why"]), (item["id"], "calendar_create", "L0", "auto-off"))
        self.assertIsInstance(shown["card"]["title"], str)
        self.assertTrue(all(set(row) == {"label", "value"} and all(isinstance(v, str) for v in row.values()) for row in shown["card"]["rows"]))
        self.assertNotIn(FAKE_TOKEN, json.dumps(out))
        self.assertIn("planning session", json.dumps(out).lower())
        self.assertIn("no attendees, no emails sent", json.dumps(out).lower())
        self.assertEqual((self.call("GET", "/v3/owner/chat")[1]["approvals"], self.call("POST", "/v3/owner/chat/events", {"after": 0})[1]["approvals"]), (1, 1))
        decision = {"id": item["id"], "digest": item["digest"], "decision": "approve"}
        self.assertEqual(self.call("POST", ANSWER, decision), (200, {"id": item["id"], "state": "approved"}))
        self.assertEqual(self.wait_idle(self.svc), "idle")
        self.assertEqual(self.call("GET", "/v3/owner/chat")[1]["approvals"], 0)
        (post,) = [c for c in self.cal.calls if c[0] != "GET"]
        self.assertEqual((post[0], post[2]["sendUpdates"], post[3]["extendedProperties"]), ("POST", "none", {"private": {"jarvis": "1"}}))
        self.assertNotIn(FAKE_TOKEN, self.log.read_text())
        self.assertEqual(self.states(item["id"]), ["pending", "approved"])
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_answer_refusals_leave_the_request_as_it_was(self):
        self.start(reply(calls=[("vault_add", ADD)]), reply("Not saved."))
        self.svc.send("Remember my tea.")
        item = self.next_card()
        good = {"id": item["id"], "digest": item["digest"], "decision": "approve"}
        for body in ({}, {"id": item["id"], "digest": item["digest"]}, {**good, "extra": 1}, {**good, "decision": "yes"}, {**good, "id": 5}, {**good, "digest": None}):
            self.assertEqual(self.call("POST", ANSWER, body)[0], 400, body)
        self.assertEqual(self.call("POST", ANSWER, good, owner=False)[0], 403)
        self.assertEqual(self.call("POST", ANSWER, good, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("GET", "/v3/owner/approvals", owner=False)[0], 403)
        self.assertEqual(self.call("POST", ANSWER, {**good, "id": "never-issued"})[0], 404)
        self.assertEqual(self.call("POST", ANSWER, {**good, "digest": "0" * 64})[0], 409)
        self.assertEqual([p["id"] for p in self.broker.pending()], [item["id"]])
        self.assertEqual(self.call("POST", ANSWER, {**good, "decision": "deny"}), (200, {"id": item["id"], "state": "denied"}))
        self.assertEqual(self.call("POST", ANSWER, good)[0], 409)
        self.assertEqual(self.wait_idle(self.svc), "idle")
        self.assertTrue(self.results()[-1].startswith("The user declined"), self.results())
        self.assertEqual((self.notes(), self.states(item["id"])), ([], ["pending", "denied"]))

    def test_an_answer_after_expiry_is_409_and_nothing_is_written(self):
        self.broker = ap().ApprovalBroker(clock=self.clock, timeout_s=120.0, log=self.log)
        self.start(reply(calls=[("vault_add", ADD)]), reply("Too late."))
        self.svc.send("Remember my tea.")
        item = self.next_card()
        self.clock.now += 121
        self.assertEqual(self.wait_idle(self.svc), "idle")
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual(self.call("POST", ANSWER, {"id": item["id"], "digest": item["digest"], "decision": "approve"})[0], 409)
        self.assertTrue(self.results()[-1].startswith("The user declined"), self.results())
        self.assertEqual((self.notes(), self.states(item["id"])), ([], ["pending", "expired"]))

    def test_daemon_stop_releases_a_pending_approval_and_builds_its_own_broker(self):
        from jarvis.v3 import daemon
        self.start(reply(calls=[("vault_add", ADD)]), reply("never reached"))
        self.svc.send("Remember my tea.")
        item, begun = self.next_card(), time.monotonic()
        self.d.stop()
        self.assertLess(time.monotonic() - begun, 4)
        self.assertEqual((self.broker.pending(), self.notes(), self.states(item["id"])), ([], [], ["pending", "cancelled"]))
        built = daemon.Daemon(port=0, data_dir=self.tmp / "other", hud_dir=self.tmp / "hud3", jobs=[])
        self.assertIsInstance(built._chat.approvals, ap().ApprovalBroker)


class Static(unittest.TestCase):
    def test_gated_tools_are_dangerous_explicit_and_serial(self):
        from jarvis import tools
        from jarvis.v3 import chat, fastpath
        fastpath.register_tools()
        fastpath.register_tools()
        self.assertEqual((set(chat.GATED_TOOLS), set(fastpath.GATED_TOOLS), fastpath.V3_TOOLS), (set(SIGNATURES), set(SIGNATURES), frozenset(A6B)))
        defaults = set(tools.default_names())
        for name, (props, required) in SIGNATURES.items():
            entry = tools.REGISTRY[name]
            self.assertTrue(entry.dangerous and name in tools.EXPLICIT_ONLY and name not in tools.PARALLEL_SAFE and name not in defaults, name)
            self.assertFalse(tools.parallelizable(name), name)
            self.assertEqual((set(entry.schema["properties"]), set(entry.schema["required"])), (props, required), name)
        self.assertFalse(getattr(fastpath.ChatApprover, "jarvis_human_backed", False))

    def test_only_the_owner_route_refers_to_answer(self):
        from jarvis.v3 import owner_routes
        self.assertTrue(owner_routes.ROUTES[("GET", "/v3/owner/approvals")].owner and owner_routes.ROUTES[("POST", ANSWER)].owner)
        users = set()
        for path in (REPO / "jarvis" / "v3").rglob("*.py"):
            if path != REPO / "jarvis" / "v3" / "approvals.py":
                if any(isinstance(node, ast.Attribute) and node.attr == "answer" for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
                    users.add(path.relative_to(REPO).as_posix())
        self.assertEqual(users, {"jarvis/v3/owner_routes.py"})

    def test_agentd_stays_light(self):
        code = ("import sys\nfrom pathlib import Path\nfrom jarvis.v3 import daemon, chat, approvals\nroot = Path(sys.argv[1])\n"
                "broker = approvals.ApprovalBroker(log=root / 'approvals.jsonl')\nassert chat.ChatService(root / 'chats', approvals=broker).snapshot()['approvals'] == 0\n"
                "print(sorted(m for m in sys.modules if m in ('jarvis.agent', 'jarvis.llm', 'jarvis.models', 'jarvis.tools', 'jarvis.v3.fastpath', 'jarvis.v3.tools.calendar', 'httpx', 'openai')))")
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-c", code, home], capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(REPO), "HOME": home, "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "[]"), out.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
