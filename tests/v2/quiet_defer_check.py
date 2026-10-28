"""Approvals asked during quiet hours (work package QH,
docs/plans/2026-10-28-quiet-hours-deferral.md; `jarvis/v2/quiet_defer.py`
and its hook in `jarvis/v2/approvals.py`).

What it pins (the package's acceptance items):

  - quiet + the owner away: the ask is denied at once, `deferred`, with the
    code-built words, no card, no wait (measured on the calling thread);
  - quiet + the owner present (15 minutes), not quiet, quiet hours off, a
    malformed or linked setting, a Discord command's own ask: asked as usual;
  - the words reach each provider's model: Claude's hook reason, the fast
    path's `Refusal`, the jarvis-mcp bridge's refusal; Codex's decline
    carries none, so its chat log says why (codex_provider_check);
  - a deferred row at rank 4, never markable, code-built title, the card's
    public projection; rank 8 after 24 h, gone after 7 days;
  - Approve and retry grants exactly that call, once, within 30 minutes:
    another argument, thread, task or tool asks; a second identical ask
    asks; an expired grant asks; concurrent identical asks take it once;
  - a HUD-only Browser card is never granted;
  - the chat is told through B1's delivery (via inbox, no desk, queued),
    a blocked task is resumed; Leave it sends nothing;
  - the phone: retry needs its own fresh assertion (none: 401; replayed or
    another card's: refused), Leave it needs the session only;
  - agents cannot write a grant or a record (protected state at v2 layer 1
    and in v1, write_file, SELF_PROTECTED); `owner_json` never reaches a
    row, the store, a log line, the bus or the phone.

Free and hermetic: temporary HOME and data; daemons on ephemeral loopback
ports (never 8402, 8403, 8405 or 8408); every clock a fake, times relative
to a fixed local midnight in a fixed zone.

Run: PYTHONPATH=. .venv/bin/python tests/v2/quiet_defer_check.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ra = _load("remote_answer_check")
ic = ra.ic
rac = ra.rac

from jarvis import config  # noqa: E402
from jarvis.v2 import inbox, quiet_defer, remote_answer  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName as PN, Role, TaskState  # noqa: E402
from jarvis.v2.permissions import L_DEFERRED, PermitContext, build_permit, last_verdict  # noqa: E402
from jarvis.v2.provider import Brief, Decision  # noqa: E402
from jarvis.v2.quiet import QuietHours  # noqa: E402
from jarvis.v2.quiet_defer import DeferredStore, QuietDesk  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

ZONE = timezone(timedelta(hours=-5))
MIDNIGHT = datetime(2026, 3, 10, tzinfo=ZONE)
SENTINEL = "SENTINEL-browser-private-5d1e"       # owner_json only: never a row, log or stream
ARG = "SENTINEL-agent-arg-0c7b"                  # an agent's argument: never a row's own fields


def at(hours: float, days: int = 0) -> datetime:
    return MIDNIGHT + timedelta(days=days, hours=hours)


class Clock:
    """A settable aware clock (the desk's and the store's)."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def utc(self) -> datetime:
        return self.now.astimezone(timezone.utc)


class Mono:
    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def request(command="make deploy", *, tool="Bash", thread_id="th000001", task_id=None, **kw):
    args = kw.pop("args", None)
    provider = kw.pop("provider", "claude")
    return ApprovalRequest(tool=tool, args=args if args is not None else {"command": command},
                           command=command, thread_id=thread_id, task_id=task_id, provider=provider, **kw)


def private(thread_id="th000001"):
    return ApprovalRequest(tool="hud_browser_act", args={"action": "click", "target": SENTINEL},
                           command=json.dumps({"target": SENTINEL}), layer="browser-gate",
                           thread_id=thread_id, provider="claude", hud_only=True,
                           browser={"host": "example.com", "path": "/" + SENTINEL}, max_wait_s=120)


class Night:
    """A broker with a quiet desk on fake clocks, as `attach_quiet_desk`
    builds it, and what it tells the bus and the surfaces."""

    def __init__(self, root: Path, *, hours=23.5, timeout_s=5.0):
        self.clock = Clock(at(hours))
        self.present = False
        self.quiet = QuietHours(root, clock=self.clock)
        self.store = DeferredStore(root, clock=self.clock.utc)
        self.published: list = []
        self.requested: list = []
        self.desk = QuietDesk(self.quiet, lambda: self.present, self.store, self.published.append)
        self.broker = PendingApprovals(timeout_s=timeout_s, on_request=self.requested.append)
        self.broker.quiet_desk = self.desk

    def answer_later(self, decision=Decision.DENY):
        """Answer each card as it comes up (for the asks that do ask)."""
        stop = threading.Event()

        def run():
            seen = set()
            while not stop.is_set():
                for r in self.broker.pending():
                    if r.req_id not in seen:
                        seen.add(r.req_id)
                        try:
                            self.broker.resolve(r.req_id, decision)
                        except ValueError:
                            pass
                time.sleep(0.002)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return stop

    def grant(self, req: ApprovalRequest) -> dict:
        """Defer `req` now, then the owner's Approve and retry on its record."""
        self.assertDeferred(req)
        return self.store.settle(req.req_id, "retry")

    def assertDeferred(self, req):
        decision = self.broker.ask(req)
        assert decision is Decision.DENY and req.outcome and req.outcome[0] == "deferred", (decision, req.outcome)


class _Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "data"
        for name, value in dict(V2_DATA_DIR=Path(tmp.name) / "v2", ALLOWLIST_PATH=Path(tmp.name) / "allow.json",
                                MODELS_PATH=Path(tmp.name) / "models.json",
                                PROVIDER_DEFAULTS_PATH=Path(tmp.name) / "provider_defaults.json",
                                V2_ALWAYS_ASK=Path(tmp.name) / "always-ask.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.n = Night(self.root)


# -- the deferral (QH-1 to QH-3) -------------------------------------------------------


class Deferral(_Temp):
    def test_quiet_and_away_is_denied_at_once_with_the_words(self):
        n = Night(self.root, timeout_s=30.0)
        req = request()
        cpu, wall = time.thread_time(), time.monotonic()
        decision = n.broker.ask(req)
        cpu, wall = time.thread_time() - cpu, time.monotonic() - wall
        self.assertIs(decision, Decision.DENY)
        self.assertLess(wall, 2.0, "no wait: the broker's 30 s were never waited")
        self.assertLess(cpu, 1.0, "measured on the calling thread")
        kind, text = req.outcome
        self.assertEqual(kind, "deferred")
        for words in ("asleep until 07:00", "NOT approved", "Do not retry it tonight",
                      "Carry on with work that does not need it"):
            self.assertIn(words, text)
        self.assertEqual(text, quiet_defer.denial_text(at(24 + 7)), "code's words only")
        self.assertEqual(n.requested, [], "no card: on_request was never called")
        self.assertEqual(n.broker.pending(), [])
        self.assertEqual(n.broker.decisions[-1]["resolution"], "deferred")
        (rec,) = n.store.all()
        self.assertEqual((rec["id"], rec["state"], rec["tool"]), (req.req_id, "waiting", "Bash"))
        self.assertEqual(n.published, [{"kind": "inbox_changed",
                                        "data": {"ids": [f"defer:{req.req_id}"], "reason": "deferred"}}])
        print("ok  quiet + away: DENY `deferred` at once (no card, no wait), the code-built words, a record")

    def test_present_not_quiet_off_or_a_discord_command_asks_as_usual(self):
        cases = {
            "present": lambda n: setattr(n, "present", True),
            "not quiet (12:00)": lambda n: setattr(n.clock, "now", at(12)),
            "07:00 is not quiet": lambda n: setattr(n.clock, "now", at(24 + 7)),
            "quiet hours off": lambda n: n.quiet.update({"enabled": False}),
            "presence unknown": lambda n: setattr(n.desk, "present", lambda: 1 / 0),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                n = Night(Path(tempfile.mkdtemp(dir=self.root.parent)), timeout_s=5.0)
                make(n)
                stop = n.answer_later(Decision.ALLOW)
                try:
                    req = request()
                    self.assertIs(n.broker.ask(req), Decision.ALLOW, name)
                finally:
                    stop.set()
                self.assertIsNone(req.outcome)
                self.assertEqual(len(n.requested), 1, "a card went up")
                self.assertEqual(n.store.all(), [])
        n = self.n
        stop = n.answer_later(Decision.ALLOW)
        try:
            req = request(discord_channel_id="123456789")
            self.assertIs(n.broker.ask(req), Decision.ALLOW)
        finally:
            stop.set()
        self.assertIsNone(req.outcome, "the owner typed that Discord command a moment ago")
        print("ok  present, not quiet, off, presence unknown, a Discord command's own ask: asked as usual")

    def test_a_malformed_setting_is_not_quiet_and_a_missing_one_is_the_default(self):
        path = self.root / "quiet.json"
        self.root.mkdir(parents=True, exist_ok=True)
        other = self.root.parent / "other.json"
        other.write_text(json.dumps({"enabled": True, "start": "22:00", "end": "08:00"}))
        for name, make in {"malformed": lambda: path.write_text("{nope"),
                           "extra field": lambda: path.write_text(json.dumps(
                               {"enabled": True, "start": "23:00", "end": "07:00", "x": 1})),
                           "a link": lambda: path.symlink_to(other),
                           "a folder": lambda: path.mkdir()}.items():
            with self.subTest(case=name):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
                make()
                n = Night(self.root)
                self.assertTrue(n.quiet.is_quiet(), "quiet.py reads it as its default (the push's rule)")
                stop = n.answer_later(Decision.DENY)
                try:
                    req = request()
                    n.broker.ask(req)
                finally:
                    stop.set()
                self.assertIsNone(req.outcome, "here it is not quiet: asked as usual")
                self.assertEqual(len(n.requested), 1)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        n = Night(self.root)
        n.assertDeferred(request())
        print("ok  a malformed, linked or odd quiet.json means not quiet (asked); a missing one is the default")

    def test_presence_is_the_owners_input_within_15_minutes(self):
        d = SimpleNamespace(_owner_seen_at=None, _owner_seen_wall=None, _presence_clock=Mono(),
                            _presence_wall=Mono(1_900_000_000.0))
        present = lambda: Daemon.owner_present(d)  # noqa: E731
        self.assertFalse(present(), "never seen: away")
        Daemon.owner_seen(d)
        self.assertTrue(present())
        d._presence_clock.now += quiet_defer.PRESENT_S - 1
        d._presence_wall.now += quiet_defer.PRESENT_S - 1
        self.assertTrue(present())
        d._presence_clock.now += 1
        d._presence_wall.now += 1
        self.assertFalse(present(), "15 minutes on: away")
        d._presence_clock.now -= 10 * quiet_defer.PRESENT_S
        d._presence_wall.now -= 10 * quiet_defer.PRESENT_S
        self.assertFalse(present(), "both clocks gone back is not presence")
        # L4: the larger age decides. A host that slept stops the monotonic
        # clock (WSL2), so the wall clock's hour away is what counts.
        Daemon.owner_seen(d)
        d._presence_wall.now += 3600
        self.assertFalse(present(), "an hour by the wall clock, though the monotonic one stood still")
        Daemon.owner_seen(d)
        d._presence_clock.now += 3600
        d._presence_wall.now -= 3600
        self.assertFalse(present(), "an hour by the monotonic clock, though the wall clock went back")
        self.assertEqual(quiet_defer.PRESENT_S, 15 * 60)
        print("ok  presence: the owner's input within 15 minutes, the larger of the wall and monotonic ages")

    def test_a_desk_that_raises_asks_as_usual(self):
        n = self.n
        n.desk.settle = lambda request: 1 / 0
        stop = n.answer_later(Decision.DENY)
        try:
            req = request()
            self.assertIs(n.broker.ask(req), Decision.DENY)
        finally:
            stop.set()
        self.assertIsNone(req.outcome)
        self.assertEqual(len(n.requested), 1, "a card went up")
        # And an answer the broker does not know is never taken.
        for odd in (("allow", "x"), ("granted", ""), ("deferred",), "deferred", ("granted", "x", "y")):
            n.desk.settle = lambda request, odd=odd: odd
            stop = n.answer_later(Decision.DENY)
            try:
                req = request()
                self.assertIs(n.broker.ask(req), Decision.DENY, odd)
            finally:
                stop.set()
            self.assertIsNone(req.outcome, odd)
        print("ok  a desk that raises, or answers anything else, asks as usual")


# -- the words reach the model -------------------------------------------------------


class Providers(_Temp):
    def permit(self, profile=PermissionProfile.ASK, broker=None):
        brief = Brief(Role.CHAT, str(self.root.parent), profile=profile)
        return build_permit(PermitContext("th000001", brief, provider="claude"), broker or self.n.broker), brief

    def test_the_permit_reports_a_deferral_unasked_with_its_words(self):
        permit, brief = self.permit()
        self.assertIs(permit("Bash", {"command": "terraform apply"}, brief), Decision.DENY)
        verdict = last_verdict()
        self.assertEqual((verdict.layer, verdict.asked), (L_DEFERRED, False))
        self.assertIn("asleep until 07:00", verdict.reason)
        # A grant comes back as the owner's, from the inbox.
        rec = self.n.store.all()[0]
        self.n.store.settle(rec["id"], "retry")
        self.assertIs(permit("Bash", {"command": "terraform apply"}, brief), Decision.ALLOW)
        verdict = last_verdict()
        self.assertEqual(verdict.reason, quiet_defer.GRANTED_TEXT)
        self.assertTrue(verdict.asked, "the owner's yes")
        print("ok  the permit: a deferral is an unasked DENY (layer quiet-deferred) carrying the words")

    def test_the_fast_path_tells_its_model(self):
        from jarvis import tools
        from jarvis.v2.providers.fastpath import FastPathProvider
        permit, brief = self.permit(PermissionProfile.AUTO)
        from jarvis import runtime
        approve = FastPathProvider._approver(brief, permit)
        with runtime.turn_scope(desk={"present": True}):       # an owner's desk turn holds run_command
            answer = approve(SimpleNamespace(name="run_command"), {"command": "terraform apply"})
        self.assertIsInstance(answer, tools.Refusal)
        self.assertFalse(answer)
        self.assertIn("asleep until 07:00", answer.text)
        self.assertIn("Do not retry it tonight", answer.text)
        # A plain no stays the plain False.
        plain = FastPathProvider._approver(brief, lambda *a, **k: Decision.DENY)
        with runtime.turn_scope(desk={"present": True}):
            self.assertIs(plain(SimpleNamespace(name="run_command"), {"command": "x"}), False)
        print("ok  fast path: the model reads the deferral's words (a Refusal), never 'the user declined'")

    def test_the_bridge_tells_a_jarvis_tool_caller(self):
        from jarvis.v2 import bridge
        permit, _brief = self.permit()
        out = bridge._delegated(permit, "gmail_send", {"to": "a@b.c"}, lambda: True)
        self.assertIn("asleep until 07:00", out.text)
        print("ok  jarvis-mcp bridge: a deferred Jarvis tool call's refusal carries the words")

    def test_claudes_hook_tells_its_model(self):
        cl = _load("claude_provider_check")
        from jarvis.v2.model import Thread
        from jarvis.v2.providers import claude
        cwd = self.root.parent / "work"
        cwd.mkdir()
        brief = Brief(Role.CHAT, str(cwd), profile=PermissionProfile.ASK)
        permit = build_permit(PermitContext("clqh0001", brief, provider="claude"), self.n.broker)
        with cl.fake():
            provider = claude.ClaudeProvider()
            handle = provider.start(Thread("clqh0001", "p", Role.CHAT, PN.CLAUDE), brief, permit)
            try:
                options = cl.FakeClient.instances[-1].options
                data = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "terraform apply"}, "tool_use_id": "c1", "session_id": "s",
                        "transcript_path": "", "cwd": str(cwd)}
                coro = options.hooks["PreToolUse"][0].hooks[0](data, "c1", {"signal": None})
                out = claude._await(handle.native.loop, coro, 10)
                hook = out["hookSpecificOutput"]
                self.assertEqual(hook["permissionDecision"], "deny")
                self.assertTrue(hook["permissionDecisionReason"].startswith(claude.HOOK_DENIAL_PREFIX + "Bash. "))
                self.assertIn("asleep until 07:00", hook["permissionDecisionReason"])
                self.assertIn("Do not retry it tonight", hook["permissionDecisionReason"])
                # The SDK backstop (`can_use_tool`, under ask) says the same.
                can = options.can_use_tool
                result = claude._await(handle.native.loop, can("Bash", {"command": "terraform apply"},
                                                                 SimpleNamespace(agent_id=None, tool_use_id="c2")), 10)
                self.assertIn("asleep until 07:00", result.message)
                # An ordinary no keeps the ordinary words.
                self.n.clock.now = at(12)
                stop = self.n.answer_later(Decision.DENY)
                try:
                    coro = options.hooks["PreToolUse"][0].hooks[0](dict(data, tool_use_id="c3"), "c3",
                                                                   {"signal": None})
                    out = claude._await(handle.native.loop, coro, 10)
                finally:
                    stop.set()
                self.assertNotIn("asleep", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertIn("refused this call", out["hookSpecificOutput"]["permissionDecisionReason"])
            finally:
                provider.close(handle)
        print("ok  Claude: the hook's (and can_use_tool's) denial reason carries the deferral's words")


# -- grants (QH-6) -----------------------------------------------------------------------


class Grants(_Temp):
    def test_exactly_that_call_once(self):
        n = self.n
        rec = n.grant(request())
        self.assertEqual(rec["state"], "granted")
        self.assertEqual(quiet_defer._parse(rec["grant_until"]), at(23.5) + timedelta(minutes=30))
        stop = n.answer_later(Decision.DENY)
        try:
            # Each differing call asks (the owner is present now, so a card).
            n.present = True
            for name, other in {"another argument": request("make deploy --prod"),
                                "another thread": request(thread_id="th000002"),
                                "a task": request(task_id="task0001"),
                                "another tool": request(tool="Monitor"),
                                "args beside the command": request(args={"command": "make deploy", "x": 1})}.items():
                self.assertIs(n.broker.ask(other), Decision.DENY, name)
                self.assertIsNone(other.outcome, name)
            asked = len(n.requested)
            self.assertEqual(asked, 5)
            n.present = False
            same = request()
            self.assertIs(n.broker.ask(same), Decision.ALLOW)
            self.assertEqual(same.outcome[0], "granted")
            self.assertEqual(len(n.requested), asked, "no card for the granted call")
            self.assertEqual(n.broker.decisions[-1]["resolution"], "granted from the inbox")
            again = request()
            self.assertIs(n.broker.ask(again), Decision.DENY)
            self.assertEqual(again.outcome[0], "deferred", "used up: the next identical ask is deferred again")
        finally:
            stop.set()
        self.assertEqual(n.store.get(rec["id"])["state"], "used")
        print("ok  a grant: exactly that call (argument, thread, task, tool all count), once")

    def test_the_digest_covers_every_part_of_the_request(self):
        base = request()
        digest = quiet_defer.request_digest(base)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(quiet_defer.request_digest(request()), digest, "the same call, the same digest")
        for name, other in {"thread": request(thread_id="th000002"), "task": request(task_id="task0001"),
                            "tool": request(tool="Monitor"), "args": request(args={"command": "make deploy", "x": 1}),
                            "command": request(args={"command": "make deploy"}, command="make  deploy"),
                            "cwd": request(cwd="/elsewhere"), "headline": request(headline="pushes to main"),
                            "layer": request(layer="always-ask"), "subagent": request(subagent="cl:abc"),
                            "relayed": request(relayed_from={"title": "x", "provider": "claude",
                                                             "thread_id": "t2", "kind": "started"})}.items():
            self.assertNotEqual(quiet_defer.request_digest(other), digest, name)
        self.assertIsNone(quiet_defer.request_digest(request(args={"x": float("nan")})), "not exactly JSON: never grantable")
        self.assertNotEqual(remote_answer.defer_binding("x"), "x", "a binding is never a bare approval id")
        print("ok  the digest covers thread, task, tool, args and command; a binding is never a bare id")

    def test_an_expired_grant_asks(self):
        n = self.n
        rec = n.grant(request())
        n.clock.now += timedelta(minutes=30)
        n.present = True
        stop = n.answer_later(Decision.DENY)
        try:
            late = request()
            self.assertIs(n.broker.ask(late), Decision.DENY)
        finally:
            stop.set()
        self.assertIsNone(late.outcome)
        self.assertEqual(len(n.requested), 1, "asked as usual")
        self.assertEqual(n.store.get(rec["id"])["state"], "granted", "spent by nothing")
        n.clock.now -= timedelta(seconds=1)
        on_time = request()
        self.assertIs(n.broker.ask(on_time), Decision.ALLOW, "29:59 later: still good")
        print("ok  a grant expires 30 minutes after Approve and retry (then the call asks)")

    def test_concurrent_identical_asks_take_it_once(self):
        n = Night(self.root, timeout_s=0.3)
        n.grant(request())
        n.present = True
        barrier = threading.Barrier(8)
        results: list = []
        lock = threading.Lock()

        def one():
            req = request()
            barrier.wait()
            decision = n.broker.ask(req)
            with lock:
                results.append((decision, req.outcome))
        workers = [threading.Thread(target=one, daemon=True) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(10)
        self.assertEqual(len(results), 8)
        granted = [r for r in results if r[1] is not None and r[1][0] == "granted"]
        self.assertEqual(len(granted), 1, results)
        self.assertEqual(sum(1 for d, _ in results if d is Decision.ALLOW), 1, "one yes, the rest time out")
        print("ok  eight identical asks at once: the grant is taken exactly once")

    def test_a_browser_card_is_never_granted(self):
        n = self.n
        req = private()
        n.assertDeferred(req)
        (rec,) = n.store.all()
        self.assertTrue(rec["hud_only"])
        self.assertEqual((rec["grantable"], rec["why_not"]), (False, "browser"))
        with self.assertRaises(ValueError):
            n.store.settle(req.req_id, "retry")
        self.assertEqual(n.store.get(req.req_id)["state"], "waiting")
        # A desk that claimed a grant for one is not believed by the broker.
        n.desk.settle = lambda request: ("granted", "x")
        n.present = True
        stop = n.answer_later(Decision.DENY)
        try:
            again = private()
            self.assertIs(n.broker.ask(again), Decision.DENY)
        finally:
            stop.set()
        self.assertIsNone(again.outcome)
        # A record forged to say "granted" makes the file not ours: nothing matches.
        data = json.loads(n.store.path.read_text())
        data["records"][req.req_id].update(state="granted", grant_until=quiet_defer._iso(at(24)),
                                           digest="0" * 64)
        n.store.path.write_text(json.dumps(data))
        self.assertEqual(n.store.all(), [])
        self.assertIsNone(n.store.take("0" * 64, "th000001"))
        print("ok  a HUD-only Browser card: deferred, never granted (store, broker and file all refuse)")

    def test_retry_over_a_day_late_leave_and_twice(self):
        n = self.n
        one, two, three = request("a"), request("b"), request("c")
        for r in (one, two, three):
            n.assertDeferred(r)
        n.store.settle(two.req_id, "leave")
        with self.assertRaises(ValueError):
            n.store.settle(two.req_id, "retry")
        with self.assertRaises(ValueError):
            n.store.settle(two.req_id, "leave")
        n.clock.now += timedelta(hours=24)
        with self.assertRaises(ValueError):
            n.store.settle(one.req_id, "retry")
        n.store.settle(three.req_id, "leave")
        with self.assertRaises(LookupError):
            n.store.settle("nope", "retry")
        print("ok  Leave it once; Approve and retry refused after Leave it, twice, or a day late")

    def test_an_unreadable_store_matches_nothing(self):
        n = self.n
        n.grant(request())
        n.store.path.write_text("{not json")
        self.assertEqual(n.store.all(), [])
        # A daemon that starts over it (its grants read once, at attach) holds none.
        fresh = Night(self.root)
        fresh.store.load_index()
        fresh.present = True
        stop = fresh.answer_later(Decision.DENY)
        try:
            req = request()
            self.assertIs(fresh.broker.ask(req), Decision.DENY)
        finally:
            stop.set()
        self.assertIsNone(req.outcome)
        self.assertEqual(n.store.path.read_text(), "{not json", "a grant check writes nothing")
        print("ok  a store that is not ours shows nothing and, read at start, holds no grant")

    def test_the_file_is_private_and_bounded(self):
        import stat
        n = self.n
        for i in range(quiet_defer.KEEP + 5):
            n.broker.ask(request(f"c{i}", thread_id=f"th{i % 15:06d}"))
        self.assertEqual(len(n.store.all()), quiet_defer.KEEP)
        self.assertEqual(sum(m["count"] for m in n.store.snapshot()[1].values()), 5,
                         "the five evicted are counted in their chats' 'and N more', never silent")
        self.assertEqual(stat.S_IMODE(os.lstat(n.store.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(n.store.path.parent).st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in n.store.path.parent.iterdir()), ["deferred.json"])
        # A link at the path is never followed.
        n.store.path.unlink()
        target = self.root.parent / "elsewhere.json"
        target.write_text("keep")
        n.store.path.symlink_to(target)
        self.assertEqual(n.store.all(), [])
        n.assertDeferred(request())
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(n.store.path.is_symlink())
        print("ok  deferred.json: 0600 in a 0700 folder, at most KEEP records, never through a link")


# -- the #326 review's findings ------------------------------------------------------------


def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, capture_output=True)


class Review(_Temp):
    def repos(self):
        base = self.root.parent
        bare = base / "origin.git"
        _git("init", "--bare", "-b", "main", str(bare), cwd=base)
        made = {}
        for name, branch in (("A", "topic"), ("B", "main"), ("C", "feature")):
            d = base / name
            _git("clone", str(bare), str(d), cwd=base)
            _git("checkout", "-b", branch, cwd=d)
            (d / "f").write_text(branch)
            _git("add", "f", cwd=d)
            _git("commit", "-m", branch, cwd=d)
            _git("push", "-u", "origin", branch, cwd=d)
            made[name] = d
        return made

    def permit(self):
        brief = Brief(Role.CHAT, str(self.root.parent), profile=PermissionProfile.ASK)
        return build_permit(PermitContext("th000001", brief, provider="claude"), self.n.broker), brief

    def test_h1_a_grant_never_answers_the_same_words_judged_elsewhere(self):
        r = self.repos()
        permit, brief = self.permit()
        args = {"command": "git push", "description": "Push the branch"}
        self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(r["A"])), Decision.DENY)
        (rec,) = self.n.store.all()
        self.assertTrue(rec["grantable"], rec["why_not"])
        self.n.store.settle(rec["id"], "retry")
        self.n.present = True
        stop = self.n.answer_later(Decision.DENY)
        try:
            # The same words in C, another clone on another feature branch: no
            # headline at all, but another folder. Asked, never granted.
            self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(r["C"])), Decision.DENY)
            self.assertNotEqual(last_verdict().reason, quiet_defer.GRANTED_TEXT)
            self.assertEqual(self.n.requested[-1].headline, "")
            # The same words in B (on main): an owner-only push to main. Asked, never granted.
            self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(r["B"])), Decision.DENY)
            self.assertNotEqual(last_verdict().reason, quiet_defer.GRANTED_TEXT)
            # The same folder, its branch changed to main: the gate's headline now; asked.
            _git("fetch", "origin", cwd=r["A"])
            _git("checkout", "-B", "main", "origin/main", cwd=r["A"])
            self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(r["A"])), Decision.DENY)
            self.assertNotEqual(last_verdict().reason, quiet_defer.GRANTED_TEXT)
            asked = len(self.n.requested)
            self.assertEqual(asked, 3, "each was a card, not the grant")
            self.assertTrue(all(c.headline for c in self.n.requested[1:]), [c.headline for c in self.n.requested])
            # Back on topic in A: exactly the call granted, once.
            _git("checkout", "topic", cwd=r["A"])
            self.n.present = False
            self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(r["A"])), Decision.ALLOW)
            self.assertEqual(last_verdict().reason, quiet_defer.GRANTED_TEXT)
        finally:
            stop.set()
        print("ok  H1: a grant never answers the same words judged in another folder or on another branch")

    def test_h1_owner_only_widening_and_codex_asks_are_never_granted(self):
        from jarvis.v2.permissions import L_ALWAYS_ASK, L_WIDENING
        r = self.repos()
        permit, brief = self.permit()
        for name, call in {
                "gh pr merge, no number": lambda: permit("Bash", {"command": "gh pr merge --squash"}, brief,
                                                         shell_cwd=str(r["A"])),
                "push to main": lambda: permit("Bash", {"command": "git push origin HEAD:main"}, brief,
                                               shell_cwd=str(r["A"])),
                "a widening": lambda: permit("Bash", {"command": "docker build ."}, brief,
                                             widening="network on"),
                "a dangerous Jarvis tool": lambda: permit("gmail_send", {"to": "a@b.c"}, brief, dangerous_tool=True),
        }.items():
            with self.subTest(case=name):
                before = {x["id"] for x in self.n.store.all()}
                self.assertIs(call(), Decision.DENY)
                (rec,) = [x for x in self.n.store.all() if x["id"] not in before]
                self.assertEqual((rec["grantable"], rec["why_not"]), (False, "owner-only"), name)
                with self.assertRaises(ValueError):
                    self.n.store.settle(rec["id"], "retry")
        for layer in (L_WIDENING, L_ALWAYS_ASK):
            self.assertEqual(quiet_defer.grant_refusal(request(layer=layer), request(layer=layer).to_json(), True),
                             "owner-only")
        self.assertEqual(quiet_defer.grant_refusal(request(headline="pushes to main"), {}, True), "owner-only")
        codex = request(provider="codex")
        self.n.assertDeferred(codex)
        rec = self.n.store.get(codex.req_id)
        self.assertEqual((rec["grantable"], rec["why_not"]), (False, "codex"))
        print("ok  H1/M1: merge-gate (gh pr merge with no number, a push to main), widening, dangerous-tool "
              "and Codex asks are never grantable")

    def test_h1_the_desk_never_takes_a_grant_for_an_owner_only_ask(self):
        """Even a live grant whose digest matched is never taken for such an ask."""
        n = self.n
        rec = n.grant(request())
        for odd in (request(headline="pushes to main"), request(layer="sandbox-widening"),
                    request(grantable=False), request(provider="codex")):
            digest = quiet_defer.request_digest(odd)
            n.store._live.setdefault(digest, []).append({"id": rec["id"], "thread_id": odd.thread_id,
                                                          "until": rec["grant_until"], "mono": None})
            self.assertEqual(n.desk.settle(odd)[0], "deferred", "never granted")
        print("ok  H1: the desk refuses a grant for an owner-only ask whatever the index holds")

    def test_m2_the_same_call_is_one_row_and_a_thread_is_bounded(self):
        n = self.n
        other = request("make release", thread_id="thOTHER1")
        n.assertDeferred(other)
        for _ in range(5):
            n.clock.now += timedelta(seconds=2)
            n.assertDeferred(request())
        records = n.store.all()
        mine = [r for r in records if r["thread_id"] == "th000001"]
        self.assertEqual((len(mine), mine[0]["count"]), (1, 5), "deduplicated: one row, asked 5 times")
        for i in range(quiet_defer.PER_THREAD + 30):
            n.assertDeferred(request(f"cmd {i}"))
        records, more = n.store.snapshot()
        mine = [r for r in records if r["thread_id"] == "th000001"]
        self.assertEqual(len(mine), quiet_defer.PER_THREAD)
        self.assertEqual(more["th000001"]["count"], 31, "every ask past the limit is counted")
        self.assertIn(other.req_id, {r["id"] for r in records}, "another chat's row is never pushed out")
        print("ok  M2: the same call is one row (asked N times); a chat holds at most 20, the rest counted")

    def test_m2_eviction_is_per_thread_and_by_size(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        n = self.n
        other = request("make release", thread_id="thOTHER1")
        n.assertDeferred(other)
        big = "echo " + "A" * 15000
        for i in range(260):
            n.clock.now += timedelta(seconds=1)           # the quiet chat's row is the oldest
            n.assertDeferred(request(big + str(i), thread_id=f"tb{i % 13:06d}"))
        self.assertLessEqual(os.path.getsize(n.store.path), quiet_defer._WRITE_MAX)
        records, more = n.store.snapshot()
        self.assertIn(other.req_id, {r["id"] for r in records}, "the quiet chat's one row survives")
        small = request("make deploy")
        n.assertDeferred(small)
        self.assertIn(small.req_id, {r["id"] for r in n.store.all()}, "a later deferral is still recorded")
        kept = sum(r["count"] for r in records if r["thread_id"].startswith("tb"))
        counted = sum(m["count"] for k, m in more.items() if k.startswith("tb"))
        self.assertEqual(kept + counted, 260, "every deferral is a row or counted")
        self.assertTrue(all(r["why_not"] == "too-large" for r in records if r["thread_id"].startswith("tb")),
                        "a card longer than the rows show is never grantable (L1)")
        print("ok  M2: eviction is per thread and by size; nothing is silent; long cards are not grantable")

    def test_m2_a_deferral_that_cannot_be_written_still_shows(self):
        n = self.n
        n.store._write = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        req = request()
        self.assertIs(n.broker.ask(req), Decision.DENY)
        self.assertEqual(req.outcome[0], "deferred")
        records, more = n.store.snapshot()
        self.assertEqual(more["th000001"]["count"], 1)
        self.assertEqual(n.published[-1]["data"]["ids"], ["defer:more.th000001"])
        rows = quiet_defer.rows(SimpleNamespace(inbox=SimpleNamespace(deferred=n.store)), inbox._row)
        self.assertEqual([(r["id"], r["rank"], r["detail"]["more"]) for r in rows], [("defer:more.th000001", 4, 1)])
        print("ok  M2: a deferral that cannot be written is counted in memory and still shows a row")

    def test_l1_l2_what_is_never_grantable(self):
        n = self.n
        cases = {"too-large": request("x" * (quiet_defer.SHOWN_TEXT + 1)),
                 "no-chat": request(thread_id=None),
                 "owner-only": request(discord_channel_id=None, layer="always-ask")}
        for why, req in cases.items():
            with self.subTest(why=why):
                n.assertDeferred(req)
                rec = n.store.get(req.req_id)
                self.assertEqual((rec["grantable"], rec["why_not"]), (False, why))
        # An owner-initiated ask (the HUD's new VEX project) is never deferred.
        stop = n.answer_later(Decision.ALLOW)
        try:
            mine = request(owner_initiated=True)
            self.assertIs(n.broker.ask(mine), Decision.ALLOW)
        finally:
            stop.set()
        self.assertIsNone(mine.outcome)
        print("ok  L1/L2: too long, no chat or task, owner-only: never grantable; the owner's own ask is never deferred")

    def test_l6_the_grant_is_taken_from_memory_and_its_use_written(self):
        n = self.n
        rec = n.grant(request())
        reads, real = [], n.store._read

        def guarded(*a):
            reads.append(n.broker._lock.locked())
            return real(*a)
        n.store._read = guarded
        same = request()
        self.assertIs(n.broker.ask(same), Decision.ALLOW)
        del n.store._read
        self.assertNotIn(True, reads, "no read of the file while the broker's lock is held")
        self.assertTrue(reads, "the spend was written (outside the lock)")
        self.assertEqual(n.store.get(rec["id"])["state"], "used", "the spend is written")
        # Reloaded from the file, a used grant stands no more.
        fresh = Night(self.root)
        fresh.store.load_index()
        self.assertEqual(fresh.store._live, {})
        print("ok  L6: the broker takes a grant from memory, reading no file, and the use is written")

    def test_nit_the_grant_also_ends_by_the_monotonic_clock(self):
        n = self.n
        n.store.mono = Mono(500.0)
        n.grant(request())
        n.store.mono.now += quiet_defer.GRANT_S
        n.present = True
        stop = n.answer_later(Decision.DENY)
        try:
            late = request()
            self.assertIs(n.broker.ask(late), Decision.DENY)
        finally:
            stop.set()
        self.assertIsNone(late.outcome, "the wall clock still says 'good', the monotonic one does not")
        print("ok  a grant minted here also ends by the monotonic clock")

    def test_l3_the_escape_hatch_passes_the_deferral_words(self):
        from jarvis.v2.hatch import EscapeHatch, _Declined
        delivered = []
        daemon = SimpleNamespace(stores=SimpleNamespace(threads=SimpleNamespace(get=lambda _id: None)),
                                 deliver=lambda tid, msg: delivered.append(msg))
        hatch = EscapeHatch(daemon, self.n.broker)
        result = hatch._offer(_Declined(thread_id="th000001", command="make deploy", tool="Bash",
                                        args={"command": "make deploy"}, reason="reviewer declined"))
        self.assertEqual(result.decision, "deny")
        self.assertIn("asleep until 07:00", result.message)
        self.assertNotIn("owner declined", result.message)
        self.assertIn("asleep until 07:00", delivered[-1].text)
        print("ok  L3: the escape hatch tells the chat the deferral's words, never 'owner declined'")


class Delta(Review):
    """The #326 delta review's findings."""

    def hatch(self, provider=None):
        from jarvis.v2.hatch import EscapeHatch
        thread = SimpleNamespace(provider=PN(provider), task_id=None) if provider else None
        daemon = SimpleNamespace(stores=SimpleNamespace(threads=SimpleNamespace(get=lambda _id: thread)),
                                 deliver=lambda tid, msg: None)
        hatch = EscapeHatch(daemon, self.n.broker)
        hatch.ran = []
        hatch._run = lambda item, task: hatch.ran.append(item.command) or "[exit 0]"
        return hatch

    def test_m1_an_escape_hatch_ask_is_never_granted(self):
        from jarvis.v2.hatch import _Declined
        action = {"type": "command", "command": "git push origin HEAD", "cwd": "/w/tree"}
        item = lambda: _Declined(thread_id="th000001", command=action["command"], tool="shell",  # noqa: E731
                                 args=dict(action), reason="guardian declined")
        for provider, why in (("codex", "codex"), ("claude", "hatch"), (None, "hatch")):
            with self.subTest(provider=provider):
                hatch = self.hatch(provider)
                before = {r["id"] for r in self.n.store.all()}
                self.assertEqual(hatch._offer(item()).decision, "deny")
                (rec,) = [r for r in self.n.store.all() if r["id"] not in before]
                self.assertEqual((rec["provider"], rec["grantable"], rec["why_not"]), (provider, False, why))
                with self.assertRaises(ValueError):
                    self.n.store.settle(rec["id"], "retry")
                self.n.store.settle(rec["id"], "leave")
        # Whatever the index holds, the desk never takes a grant for a hatch ask.
        hatch = self.hatch("claude")
        seen = []
        real = self.n.broker.ask
        self.n.broker.ask = lambda req, **kw: seen.append(req) or real(req, **kw)
        hatch._offer(item())
        req = seen[-1]
        self.n.store._live[quiet_defer.request_digest(req)] = [
            {"id": req.req_id, "thread_id": req.thread_id, "until": quiet_defer._iso(at(24)), "mono": None}]
        self.assertEqual(hatch._offer(item()).decision, "deny")
        self.assertEqual(hatch.ran, [], "the hatch ran nothing")
        print("ok  MED 1: an escape-hatch ask (a Codex guardian's included) is never granted, never run")

    def test_m2_counters_are_bounded_and_the_store_survives_400_chats(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        n = self.n
        keep = request("make keep", thread_id="thKEEP01")
        n.assertDeferred(keep)
        n.store.settle(keep.req_id, "retry")
        for i in range(420):
            n.clock.now += timedelta(seconds=1)
            n.assertDeferred(request(f"cmd {i}", thread_id=f"tx{i:06d}"))
            raw = json.loads(n.store.path.read_text())
            self.assertIsNotNone(quiet_defer.valid(raw), f"a write left an unreadable file at {i}")
        records, more = n.store.snapshot()
        self.assertLessEqual(len(more), quiet_defer.MORE_KEEP)
        self.assertIn(keep.req_id, {r["id"] for r in records}, "the live grant's record is kept")
        n.store.load_index()
        self.assertTrue(n.store._live, "the grant survives a reload")
        kept = sum(r["count"] for r in records if r["thread_id"].startswith("tx"))
        self.assertEqual(kept + sum(m["count"] for m in more.values()), 420, "every deferral is a row or counted")
        self.assertIn(quiet_defer.ALL, more, "the oldest chats fold into one counter")
        rows = quiet_defer.rows(SimpleNamespace(inbox=SimpleNamespace(deferred=n.store)), inbox._row)
        (folded,) = [r for r in rows if r["id"] == f"defer:{quiet_defer.MORE}{quiet_defer.ALL}"]
        self.assertIn("other chats", folded["title"])
        print("ok  MED 2: 'and N more' counters are bounded (oldest fold into one); 420 chats leave a valid store")

    def test_m2_an_unreadable_file_is_kept_aside_not_replaced(self):
        n = self.n
        n.assertDeferred(request("first"))
        n.store.path.write_text("{not ours")
        n.assertDeferred(request("second", thread_id="th000002"))
        aside = n.store.path.with_name(quiet_defer.UNREADABLE)
        self.assertEqual(aside.read_text(), "{not ours", "the unreadable file is kept aside")
        self.assertIsNotNone(quiet_defer.valid(json.loads(n.store.path.read_text())))
        print("ok  MED 2: a present but unreadable store is kept aside, never silently replaced")

    def test_low3_a_grant_whose_use_cannot_be_written_is_not_used(self):
        n = self.n
        rec = n.grant(request())
        real = n.store._write
        n.store._write = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            first = request()
            self.assertIs(n.broker.ask(first), Decision.DENY, "the use could not be written: not used")
            self.assertEqual(n.broker.decisions[-1]["resolution"], "grant-unrecorded")
        finally:
            n.store._write = real
        # LOW 4: a later write re-reads the file, where it still says "granted";
        # in this process the grant was taken, so it never lives again.
        n.assertDeferred(request("something else", thread_id="th000002"))
        again = request()
        self.assertIs(n.broker.ask(again), Decision.DENY)
        self.assertEqual(again.outcome[0], "deferred", "not granted a second time in this process")
        # After a restart it may be used once (it never ran), and only once.
        fresh = Night(self.root)
        fresh.clock.now = n.clock.now + timedelta(minutes=1)
        fresh.store.load_index()
        self.assertIs(fresh.broker.ask(request()), Decision.ALLOW)
        self.assertIs(fresh.broker.ask(request()), Decision.DENY)
        self.assertEqual(fresh.store.get(rec["id"])["state"], "used")
        print("ok  LOW 3/4: a use that cannot be written denies; a taken grant never lives again in-process")

    def test_low5_a_retargeted_link_or_a_missing_folder_is_another_folder(self):
        r = self.repos()
        permit, brief = self.permit()
        link = self.root.parent / "L"
        os.symlink(r["A"], link)
        args = {"command": "git push", "description": "Push the branch"}
        self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(link)), Decision.DENY)
        (rec,) = self.n.store.all()
        self.assertTrue(rec["grantable"], rec["why_not"])
        self.n.store.settle(rec["id"], "retry")
        os.unlink(link)
        os.symlink(r["C"], link)
        self.n.present = True
        stop = self.n.answer_later(Decision.DENY)
        try:
            self.assertIs(permit("Bash", dict(args), brief, shell_cwd=str(link)), Decision.DENY)
            self.assertNotEqual(last_verdict().reason, quiet_defer.GRANTED_TEXT)
        finally:
            stop.set()
        gone = request(cwd=str(self.root.parent / "nowhere"))
        self.n.present = False
        self.n.assertDeferred(gone)
        self.assertEqual(self.n.store.get(gone.req_id)["why_not"], "folder")
        print("ok  LOW 5: the digest binds the folder itself (real path, device, inode); a missing one is never granted")

    def test_a_deferral_subject_takes_allow_only(self):
        from jarvis.v2 import remote_auth
        self.assertEqual(remote_auth.DEFER_DECISIONS, ("allow",))
        from jarvis.v2 import review
        binding = remote_answer.defer_binding("abcDEF123_-x")
        self.assertTrue(remote_auth._DEFER_SUBJECT.fullmatch(binding))
        self.assertTrue(remote_auth._DEFER_SUBJECT.fullmatch(review.subject([("defer:a", "a" * 64),
                                                                            ("defer:b", "b" * 64)])))
        for odd in ("defer:", "defer:a:b", "defers:abc", "task:0123abcd", "defer:" + "a" * 59):
            self.assertIsNone(remote_auth._DEFER_SUBJECT.fullmatch(odd), odd)
        print("ok  a deferral's subject is defer:<id> or defers:<56 hex>, and takes Allow only")

    def test_low6_relayed_and_subagent_calls_are_not_offered_a_grant(self):
        n = self.n
        relayed = request("make a", relayed_from={"title": "x", "provider": "claude", "thread_id": "t2",
                                                  "kind": "started"})
        child = request("make b", subagent="cl:abc123")
        for req, why in ((relayed, "relayed"), (child, "subagent")):
            n.assertDeferred(req)
            rec = n.store.get(req.req_id)
            self.assertEqual((rec["grantable"], rec["why_not"]), (False, why))
        print("ok  LOW 6: a relayed turn's or a sub-agent's call is not offered Approve and retry")


# -- the daemon: rows, the HUD route, delivery -------------------------------------------


class ResumingRunner(ic.SpyRunner):
    def resume(self, task_id, provider=None):
        self.calls.append(("resume", task_id))
        return self.daemon.stores.tasks.transition(task_id, TaskState.CLARIFYING, reason="owner resumed")


class Live(unittest.TestCase):
    """A real daemon on ephemeral ports with fake brains, its quiet desk on
    fake clocks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        (self.root / "repo").mkdir()
        self.stores = Stores()
        self.brain = ic.Brain(PN.FAST)
        self.daemon = Daemon(self.stores, {name: (self.brain if name == PN.FAST else ic.Brain(name)) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.assertIsNone(self.daemon.approvals.quiet_desk, "an embedded daemon never defers")
        self.runner = ResumingRunner(self.daemon)
        self.daemon.runner = self.runner
        self.clock, self.mono = Clock(at(23.5)), Mono()
        self.daemon.quiet.clock = self.clock
        self.daemon.inbox.deferred.clock = self.clock.utc
        self.daemon._presence_clock = self.mono
        self.daemon.attach_quiet_desk()
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.project = self.stores.projects.create("Calc", str(self.root / "repo"))
        self.records: list = []
        self.daemon.bus.observe(self.records.append)

    def call(self, method, path, body=None, status=200, *, owner=True):
        import http.client
        port = self.daemon.face_port if owner else self.daemon.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if owner:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:400])
            return json.loads(content) if content else None
        finally:
            conn.close()

    def chat(self):
        return self.daemon.open_thread(self.project.id, Role.CHAT, PN.FAST, {})

    def deferred(self, **kw):
        self.mono.now += quiet_defer.PRESENT_S      # the owner's last answer is a quarter hour old
        req = request(**kw)
        self.assertIs(self.daemon.approvals.ask(req), Decision.DENY)
        self.assertEqual(req.outcome[0], "deferred")
        return req

    def test_a_deferred_row_at_rank_4_never_markable(self):
        thread = self.chat()
        req = self.deferred(thread_id=thread.id, args={"command": f"make deploy {ARG}"},
                            command=f"make deploy {ARG}")
        rows = self.call("GET", "/inbox?all=1")
        (row,) = [r for r in rows if r["kind"] == "deferred"]
        self.assertEqual((row["id"], row["rank"], row["markable"]), (f"defer:{req.req_id}", 4, False))
        self.assertEqual(row["title"], "Bash is waiting for your morning approval")
        self.assertEqual(row["blocks"], f"chat {thread.id} was told to carry on without it")
        for key in ("title", "blocks", "origin"):
            self.assertNotIn(ARG, row[key])
        card = row["detail"]["card"]
        self.assertEqual(card["command"], f"make deploy {ARG}", "the card's public projection, shown in full")
        self.assertNotIn("code", card)
        self.assertTrue(row["detail"]["grantable"])
        self.assertNotIn("digest", json.dumps(row))
        self.assertTrue(any(r.get("kind") == "inbox_changed" and r["data"]["ids"] == [row["id"]]
                            for r in self.records))
        self.assertFalse(any(r.get("kind") in ("approval_requested", "approval_resolved") for r in self.records),
                         "no card went up or came down, so no surface (Discord included) heard of it")
        self.call("POST", f"/inbox/{row['id']}/mark", {"dismiss": True}, 409)
        # A day later it is information (rank 8), still unmarkable, retry refused; 7 days, gone.
        self.clock.now += timedelta(hours=24)
        (row,) = [r for r in self.call("GET", "/inbox?all=1") if r["kind"] == "deferred"]
        self.assertEqual((row["rank"], row["markable"], row["detail"]["grantable"]), (8, False, False))
        self.call("POST", f"/inbox/{row['id']}/answer", {"action": "retry"}, 409)
        self.clock.now += timedelta(days=6)
        self.assertEqual([r for r in self.call("GET", "/inbox?all=1") if r["kind"] == "deferred"], [])
        print("ok  a deferred row: rank 4, kind deferred, unmarkable, code-built words; rank 8 after 24 h")

    def test_approve_and_retry_tells_the_chat_and_grants_once(self):
        thread = self.chat()
        req = self.deferred(thread_id=thread.id)
        told = []
        real = self.daemon.deliver_queued

        def spy(thread_id, message, **kw):
            told.append((thread_id, message, kw))
            return real(thread_id, message, **kw)
        self.daemon.deliver_queued = spy
        self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry", "x": 1}, 400)
        self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "always"}, 400)
        self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry"}, 403, owner=False)
        out = self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry"})
        self.assertIn(out["delivery"], ("started", "queued"))
        (tid, message, _kw), = told
        self.assertEqual(tid, thread.id)
        self.assertEqual((message.origin, message.via, message.desk), ("owner", "inbox", False))
        self.assertIn("approved", message.text)
        self.assertIn(req.req_id, message.text)
        self.assertIn("Only the identical call", message.text)
        self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry"}, 409)
        self.assertNotIn(f"defer:{req.req_id}", [r["id"] for r in self.call("GET", "/inbox")])
        self.assertTrue(self.daemon.owner_present(), "the owner's answer is presence")
        self.mono.now += quiet_defer.PRESENT_S
        same = request(thread_id=thread.id)
        self.assertIs(self.daemon.approvals.ask(same), Decision.ALLOW)
        self.assertEqual(same.outcome[0], "granted")
        print("ok  Approve and retry (HUD): the chat is told via inbox, no desk; the call is granted once")

    def test_a_blocked_task_is_resumed_and_leave_sends_nothing(self):
        task = self.stores.tasks.create(self.project.id, "migrate")
        self.stores.tasks.save(task)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        task = self.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="deferred overnight")
        req = self.deferred(thread_id="thtask01", task_id=task.id)
        out = self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry"})
        self.assertEqual(out["delivery"], "resumed")
        self.assertIn(("resume", task.id), self.runner.calls)
        # Q2: a task that is not blocked gets no grant: "it is running; open the task", row kept.
        req = self.deferred(thread_id="thtask01", task_id=task.id, command="other",
                            args={"command": "other"})
        out = self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "retry"}, 409)
        self.assertEqual(out["error"], quiet_defer.RUNNING)
        self.assertEqual(self.runner.calls.count(("resume", task.id)), 1)
        self.assertEqual(self.daemon.inbox.deferred.get(req.req_id)["state"], "waiting")
        self.assertIn(f"defer:{req.req_id}", [r["id"] for r in self.call("GET", "/inbox")])
        self.assertEqual(self.daemon.inbox.deferred._live.get(
            self.daemon.inbox.deferred.get(req.req_id)["digest"], []), [], "no grant minted")
        # Leave it: nothing sent, the row goes.
        thread = self.chat()
        req = self.deferred(thread_id=thread.id, command="third", args={"command": "third"})
        told = []
        self.daemon.deliver_queued = lambda *a, **k: told.append(a)
        out = self.call("POST", f"/inbox/defer:{req.req_id}/answer", {"action": "leave"})
        self.assertEqual(out["delivery"], "left")
        self.assertEqual(told, [])
        self.assertNotIn(f"defer:{req.req_id}", [r["id"] for r in self.call("GET", "/inbox")])
        self.mono.now += quiet_defer.PRESENT_S
        again = request(thread_id=thread.id, command="third", args={"command": "third"})
        self.daemon.approvals.ask(again)
        self.assertEqual(again.outcome[0], "deferred", "Leave it grants nothing")
        print("ok  a blocked task is resumed (TaskControl), a running one is not told; Leave it sends nothing")

    def test_approve_and_retry_is_one_id_in_one_result_out(self):
        """For the morning review: one function per id, never raising for a
        refusal, so several ids under one owner confirm get one result each."""
        thread = self.chat()
        good = self.deferred(thread_id=thread.id)
        left = self.deferred(thread_id=thread.id, command="b", args={"command": "b"})
        self.assertTrue(quiet_defer.leave(self.daemon, left.req_id)["ok"])
        results = [quiet_defer.approve_and_retry(self.daemon, i) for i in (good.req_id, left.req_id, "no/such")]
        self.assertEqual([(r["ok"], r["id"]) for r in results],
                         [(True, f"defer:{good.req_id}"), (False, f"defer:{left.req_id}"), (False, "defer:no/such")])
        self.assertEqual((results[1]["status"], results[2]["status"]), (409, 400))
        self.assertIn(results[0]["delivery"], ("started", "queued"))
        refused = quiet_defer.approve_and_retry(self.daemon, good.req_id, via="phone")
        self.assertEqual((refused["ok"], refused["status"]), (False, 401), "the phone needs its assertion")
        # The phone's binding: one id is `defer:<id>`; a set is the morning
        # review's subject over every id and its tag (MR-6, review.subject).
        from jarvis.v2 import review
        self.assertEqual(remote_answer.defer_binding("abc"), "defer:abc")
        pair = [("defer:a", "a" * 64), ("defer:b", "b" * 64)]
        self.assertEqual(review.subject(pair), review.subject(list(reversed(pair))))
        self.assertNotEqual(review.subject(pair), review.subject([("defer:a", "a" * 64), ("defer:c", "b" * 64)]))
        self.assertTrue(review.subject(pair).startswith("defers:") and len(review.subject(pair)) <= 64)
        self.assertNotIn(":", good.req_id, "an approval's id can never be a binding")
        print("ok  approve_and_retry/leave: one id in, one result out (never raising); a set binds review.subject")

    def test_a_browser_row_offers_no_grant(self):
        thread = self.chat()
        req = private(thread_id=thread.id)
        self.assertIs(self.daemon.approvals.ask(req), Decision.DENY)
        (row,) = [r for r in self.call("GET", "/inbox") if r["kind"] == "deferred"]
        self.assertFalse(row["detail"]["grantable"])
        self.assertTrue(row["detail"]["hud_only"])
        self.call("POST", f"/inbox/{row['id']}/answer", {"action": "retry"}, 409)
        self.assertEqual(self.call("POST", f"/inbox/{row['id']}/answer", {"action": "leave"})["delivery"], "left")
        print("ok  a Browser card's deferred row: Leave it only (retry 409)")

    def test_owner_json_never_reaches_a_row_the_store_a_log_or_the_bus(self):
        lines: list[str] = []

        class Catch(logging.Handler):
            def emit(self, record):
                lines.append(self.format(record))
        handler = Catch(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))
        thread = self.chat()
        req = private(thread_id=thread.id)
        self.assertIs(self.daemon.approvals.ask(req), Decision.DENY)
        self.assertIn(SENTINEL, json.dumps(req.owner_json()), "the sentinel is in owner_json")
        rows = self.call("GET", "/inbox?all=1")
        self.assertTrue(any(r["kind"] == "deferred" for r in rows))
        self.assertNotIn(SENTINEL, json.dumps(rows))
        self.assertNotIn(SENTINEL, self.daemon.inbox.deferred.path.read_text())
        self.assertNotIn(SENTINEL, json.dumps(self.records, default=str))
        self.assertNotIn(SENTINEL, "\n".join(lines))
        print("ok  a Browser card's private detail never reaches a row, the store, a log line or the bus")

    def test_an_embedded_daemon_never_defers(self):
        d = Daemon(Stores(self.root / "other"), {name: ic.Brain(name) for name in PN},
                   lambda *_: lambda *_: Decision.DENY, 0)
        self.assertIsNone(d.approvals.quiet_desk)
        source = inspect.getsource(sys.modules["jarvis.v2.daemon"].main)
        self.assertIn("daemon.attach_quiet_desk()", source)
        self.assertNotIn("attach_quiet_desk(", inspect.getsource(Daemon.__init__))
        self.assertNotIn("attach_quiet_desk(", inspect.getsource(Daemon.start))
        print("ok  only daemon2's main() attaches the quiet desk")


# -- the phone (QH-6 on the phone: a fresh passkey assertion) -----------------------------


class PhoneRetry(ra.Phone):
    def setUp(self):
        super().setUp()
        self.clock = Clock(at(23.5))
        self.d.quiet.clock = self.clock
        self.d.inbox.deferred.clock = self.clock.utc
        self.mono2 = Mono()
        self.d._presence_clock = self.mono2
        self.d.attach_quiet_desk()

    def deferred(self, command="make deploy", **kw):
        self.mono2.now += quiet_defer.PRESENT_S     # the owner's last answer is a quarter hour old
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        req = request(command, thread_id=thread.id, args={"command": command}, **kw)
        self.assertIs(self.d.approvals.ask(req), Decision.DENY)
        self.assertEqual(req.outcome[0], "deferred")
        return req, f"defer:{req.req_id}"

    def test_retry_needs_a_fresh_assertion_of_its_own(self):
        req, row = self.deferred()
        status, _, body = self.post_answer(row, {"action": "retry"})
        self.assertEqual(status, 401, body)
        status, _, body = self.post_answer(row, {"action": "retry", "assertion": "x"})
        self.assertEqual(status, 401, body)
        self.assertEqual(self.d.inbox.deferred.get(req.req_id)["state"], "waiting")
        # The inbox dispatch with the phone's `via` cannot grant either.
        with self.assertRaises(Exception) as caught:
            inbox.answer(self.d, row, {"action": "retry"}, owner_verified=False, via="phone")
        self.assertEqual(getattr(caught.exception, "status", None), 401)
        # Always is not an answer here.
        status, _, body = self.options(row, "always")
        self.assertEqual(status, 409, body)
        credential, _ = self.assertion(row, "allow")
        status, _, body = self.post_answer(row, {"action": "retry", "assertion": credential})
        self.assertEqual(status, 200, body)
        self.assertIn(body["delivery"], ("started", "queued"))
        self.assertEqual(self.d.inbox.deferred.get(req.req_id)["state"], "granted")
        # Replayed, on another deferral: the challenge is spent (409), nothing granted.
        other, other_row = self.deferred("make other")
        status, _, body = self.post_answer(other_row, {"action": "retry", "assertion": credential})
        self.assertIn(status, (401, 409), body)
        self.assertEqual(self.d.inbox.deferred.get(other.req_id)["state"], "waiting")
        # One issued for that deferral, sent for another: refused (401), spent.
        credential, _ = self.assertion(other_row, "allow")
        third, third_row = self.deferred("make third")
        status, _, body = self.post_answer(third_row, {"action": "retry", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertEqual(self.d.inbox.deferred.get(third.req_id)["state"], "waiting")
        print("ok  phone: Approve and retry needs its own fresh assertion (none 401, replayed or another's refused)")

    def test_an_approvals_assertion_is_never_a_deferrals(self):
        self.mono2.now = 0.0
        self.d._owner_seen_at = None
        self.clock.now = at(12)                 # an ordinary card, asked in the day
        approval, _out, _worker = self.ask()
        credential, _ = self.assertion(self.row(approval), "allow")
        self.clock.now = at(23.5)
        req, row = self.deferred()
        status, _, body = self.post_answer(row, {"action": "retry", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertEqual(self.d.inbox.deferred.get(req.req_id)["state"], "waiting")
        self.assertTrue(self.pending(approval), "and the card was not answered either")
        print("ok  phone: an approval's assertion is never a deferral's (bound to defer:<id>)")

    def test_leave_needs_the_session_only_and_a_browser_row_takes_no_retry(self):
        req, row = self.deferred()
        status, _, body = self.post_answer(row, {"action": "leave"})
        self.assertEqual((status, body.get("delivery")), (200, "left"), body)
        status, _, body = self.post_answer(row, {"action": "leave"}, cookie="__Host-jarvis_m=nope")
        self.assertEqual(status, 401, body)
        self.assertFalse(self.d.owner_present(), "a Leave it from the phone (no passkey) is not presence")
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        browser = private(thread_id=thread.id)
        self.d.approvals.ask(browser)
        status, _, body = self.options(f"defer:{browser.req_id}", "allow")
        self.assertEqual(status, 409, body)
        status, _, rows = self.phone("GET", "/m/api/inbox", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(SENTINEL, json.dumps(rows))
        (mine,) = [r for r in rows["rows"] if r["id"] == f"defer:{browser.req_id}"]
        self.assertEqual((mine["kind"], mine["rank"], mine["detail"]["grantable"]), ("deferred", 4, False))
        print("ok  phone: Leave it with the session only; a Browser row offers no retry; no private detail")

    def test_l5_retry_runs_the_41_checks_as_allow_does(self):
        req, row = self.deferred()
        secure = self.d.secure
        self.d.secure = None
        status, _, body = self.options(row, "allow")
        self.assertEqual(status, 409, body)
        self.d.secure = secure
        credential, _ = self.assertion(row, "allow")
        self.d.secure = None
        status, _, body = self.post_answer(row, {"action": "retry", "assertion": credential})
        self.assertEqual(status, 409, body)
        self.assertTrue(body["error"].startswith("limited"), body)
        self.assertEqual(self.d.inbox.deferred.get(req.req_id)["state"], "waiting", "nothing granted")
        self.d.secure = secure
        checked = []
        real = secure.check
        secure.check = lambda **kw: checked.append(kw) or real(**kw)
        credential, _ = self.assertion(row, "allow")
        status, _, body = self.post_answer(row, {"action": "retry", "assertion": credential})
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(checked), 2, "options and the answer each ran §4.1")
        source = inspect.getsource(remote_answer._answer_deferred)
        self.assertLess(source.index("answer_verify("), source.index("_monitor(daemon).check("))
        self.assertLess(source.index("_monitor(daemon).check("), source.index("verified = True"))
        print("ok  L5: the phone's retry runs §4.1 and the Allow's gate; no monitor, no grant")

    def test_a_card_the_phone_would_scrub_is_refused(self):
        # What the desk's card keeps and the phone's scrub withholds (review
        # of PR #308): a labelled password in the reason.
        req, row = self.deferred("ls", reason="password: " + ra.PASSWORD)
        status, _, body = self.options(row, "allow")
        self.assertEqual((status, body.get("error")), (409, remote_answer.WITHHELD), body)
        status, _, rows = self.phone("GET", "/m/api/inbox", cookie=self.cookie)
        self.assertNotIn(ra.PASSWORD, json.dumps(rows))
        self.assertEqual(self.post_answer(row, {"action": "leave"})[0], 200, "Leave it still works")
        print("ok  phone: a deferred card the phone's scrub would change takes no retry there")


# -- agents cannot reach it ------------------------------------------------------------


class Boundary(unittest.TestCase):
    def test_the_store_is_protected_state(self):
        from jarvis import permissions as v1, rules as v1_rules, runtime, tools
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, \
                patch.object(config, "V2_DATA_DIR", Path(tmp)):
            target = Path(tmp) / "inbox" / "deferred.json"
            self.assertIn(target, files._protected_state_paths())
            self.assertTrue(files._self_protected(target), "v1 write tools refuse it")
            self.assertTrue(v2.denied_file(str(target)))
            for command in (f"cp /tmp/x {target}", f"echo '{{}}' > {target}", f"rm {target}",
                            f"tee {target} < /tmp/x", f"mv /tmp/x {Path(tmp)}/inbox/deferred.json",
                            f"cp x {tmp}/*/deferred.json", f"cp x {tmp}/inbox/deferre[d].json",
                            f"python3 -c \"open('{target}', 'w').write('x')\""):
                self.assertTrue(v2.denied_command(command), command)
                self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
            with runtime.unconfined():
                out = tools.dispatch("write_file", json.dumps({"path": str(target), "content": "{}"}))
            self.assertFalse(target.exists(), out.text)
        self.assertIn("jarvis/v2/quiet_defer.py", files.SELF_PROTECTED)
        print("ok  deferred.json is protected state (v2 layer 1, v1 never ALLOW, write_file refused); "
              "quiet_defer.py is SELF_PROTECTED")

    def test_only_the_verified_phone_passes_verified(self):
        found = {}
        for path in sorted((ROOT / "jarvis").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            uses = re.findall(r"(?<![`\w])verified\s*=\s*(True|verified)\b", text)
            if uses:
                found[str(path.relative_to(ROOT))] = uses
        self.assertEqual(found, {"jarvis/v2/remote_answer.py": ["True", "verified"],
                                 "jarvis/v2/quiet_defer.py": ["verified", "verified"],
                                 # The morning review's group (MR-11): only after the set's assertion.
                                 "jarvis/v2/review.py": ["verified", "True"]}, found)
        source = inspect.getsource(remote_answer._answer_deferred)
        self.assertLess(source.index("answer_verify("), source.index("verified = True"))
        self.assertIn("verified=False", inspect.getsource(inbox.answer))
        print("ok  quiet_defer.answer(verified=True): only remote_answer, after answer_verify")

    def test_the_broker_hook_runs_under_its_lock(self):
        source = inspect.getsource(PendingApprovals.ask)
        lock = source.index("with self._lock:")
        settle = source.index("self._settle_quietly(desk, request)")
        pending = source.index("self._pending[request.req_id] = item")
        self.assertLess(lock, settle)
        self.assertLess(settle, pending)
        print("ok  the grant is settled under the broker's lock, before any card is admitted")


if __name__ == "__main__":
    unittest.main(verbosity=1)
