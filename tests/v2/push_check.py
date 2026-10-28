"""Web Push to the phone (phone surface design PH-8, §5, §7; work package P-6):
`jarvis/v2/push.py`, its routes on the remote listener and the HUD, and the
sender.

Free and hermetic, on tests/v2/remote_auth_check.py's harness: a real daemon
with its remote listener on an ephemeral loopback port (never 8402, 8403,
8405 or 8408), temporary HOME, data and HUD build, a passkey paired and
signed in with the software authenticator. **No network**: the resolver and
the transport are fakes (`FakeNet`); the real encryption and VAPID signing
run, and the test decrypts what would have been sent with the test
subscription's own private key. Clocks are injected and relative: the
monotonic one starts at an arbitrary value, quiet hours are read from a
fixed local midnight.

What it pins (P-6's acceptance, and the brief's tests):

  - the payload is exactly `{"count": n, "rank": r}`, decrypted from the
    body, with a planted command and title nowhere in the body or headers;
  - coalescing: at most one push per 60 s, the rest folded into one;
  - 404 and 410 delete the subscription; 500 keeps it; the log says the
    status and never the endpoint;
  - quiet hours: nothing pushes inside them, rank 1 included; one push
    when they end if anything still waits; none if nothing does;
  - logout, the desk's End and a revoke delete the subscription;
  - the routes need a session, the Host, the Origin; the API and HUD
    listeners never serve them; the desk's routes are owner-only;
  - endpoints: http, an IP literal, another port, userinfo, a host off the
    list or one resolving to a private address are refused, at subscribe
    and at send;
  - the VAPID key and subscriptions are unreadable and unwritable to
    agents (v2 layer 1 and v1), and the switch unwritable;
  - `Daemon.start()` sends nothing and starts no sender unless `main()`
    attached it.

Run: PYTHONPATH=. .venv/bin/python tests/v2/push_check.py
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("remote_auth_check", Path(__file__).with_name("remote_auth_check.py"))
rac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rac)

import http_ece  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from jarvis import config  # noqa: E402
from jarvis.v2 import push as push_mod  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN, TaskState  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

HOST, ORIGIN, raw = rac.HOST, rac.ORIGIN, rac.raw
SENTINEL = "SENTINEL-push-must-not-carry-7d3a"
ZONE = timezone(timedelta(hours=-5))
MIDNIGHT = datetime(2026, 3, 10, tzinfo=ZONE)


def local(hours: float) -> datetime:
    return MIDNIGHT + timedelta(hours=hours)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Receiver:
    """A phone's push subscription: its own key pair and auth secret."""

    def __init__(self, endpoint: str | None = None):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = secrets.token_bytes(16)
        self.endpoint = endpoint or "https://web.push.apple.com/Q" + secrets.token_urlsafe(40)

    def json(self) -> dict:
        point = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "expirationTime": None,
                "keys": {"p256dh": b64(point), "auth": b64(self.auth)}}

    def decrypt(self, body: bytes) -> bytes:
        return http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm")


class FakeNet:
    """The resolver and the transport: what would have gone out, and the
    status each send gets (201 unless told otherwise)."""

    def __init__(self):
        self.addresses = {"web.push.apple.com": ["17.253.1.2"], "fcm.googleapis.com": ["142.250.1.2"],
                          "api.push.apple.com": ["17.253.1.3"],
                          # Public and resolvable, but not push services: the
                          # host list is what refuses them.
                          "evil.example.com": ["93.184.216.34"], "push.apple.com.evil.example": ["93.184.216.35"],
                          "apple.com": ["17.253.144.10"], "localhost": ["93.184.216.36"]}
        self.sent: list[dict] = []
        self.statuses: list[int] = []
        self.lock = threading.Lock()

    def resolve(self, host):
        if host not in self.addresses:
            raise OSError("no such host")
        return list(self.addresses[host])

    def post(self, host, addresses, path, headers, body, timeout, **_bounds):
        with self.lock:
            self.sent.append({"host": host, "addresses": addresses, "path": path, "headers": dict(headers),
                              "body": body, "timeout": timeout})
            return self.statuses.pop(0) if self.statuses else 201


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def eventually(check, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class PushBase(rac.Remote):
    def setUp(self):
        super().setUp()
        self.net = FakeNet()
        self.push = self.d.push
        self.push.resolver = self.net.resolve
        self.push.wall = self.wall
        self.mono2 = Clock(7_000.0)
        self.push.mono = self.mono2
        # Daytime unless a test says otherwise.
        self.local = Clock(local(12))
        self.d.quiet.clock = self.local
        self.a = self.pair()
        self.cookie = self.signed_in(self.a)
        self.receiver = Receiver()
        self.asks = []

    def subscribe(self, receiver=None, cookie=None):
        receiver = receiver or self.receiver
        return self.phone("POST", "/m/api/push/subscribe", {"subscription": receiver.json()},
                          cookie=cookie or self.cookie)

    def subscribed(self, receiver=None, cookie=None):
        # As the phone does: the key first, then the subscription.
        status, _, body = self.phone("GET", "/m/api/push", cookie=cookie or self.cookie)
        self.assertEqual(status, 200, body)
        status, _, body = self.subscribe(receiver, cookie)
        self.assertEqual(status, 200, body)

    def attach(self):
        """The fake transport, as `attach_system` would attach the real one."""
        self.push.transport = self.net.post

    def ask(self, command=f"echo {SENTINEL}"):
        request = ApprovalRequest(tool="Bash", args={"command": command}, command=command, provider="claude",
                                  origin=f"task {SENTINEL}")
        threading.Thread(target=self.d.approvals.ask, args=(request,), daemon=True).start()
        eventually(lambda: any(r.req_id == request.req_id and request.req_id for r in self.d.approvals.pending()),
                   "the ask")
        self.asks.append(request)
        return request

    def answer(self, request):
        self.d.approvals.resolve(request.req_id, "deny")
        eventually(lambda: not any(r.req_id == request.req_id for r in self.d.approvals.pending()), "the answer")

    def file(self, name):
        return Path(config.V2_DATA_DIR) / "remote" / name


# -- the payload ------------------------------------------------------------------------


class Payload(PushBase):
    def test_payload_is_content_free(self):
        self.subscribed()
        self.attach()
        self.push.seed()
        self.ask()
        handed = []
        real = self.push.encrypt
        self.push.encrypt = lambda sub, plaintext: (handed.append(plaintext), real(sub, plaintext))[1]
        result = self.push.evaluate()
        self.assertEqual(result, {"sent": 1, "failed": 0, "removed": 0})
        self.assertEqual(len(self.net.sent), 1)
        sent = self.net.sent[0]
        plaintext = self.receiver.decrypt(sent["body"])
        self.assertEqual(json.loads(plaintext), {"count": 1, "rank": 1})
        self.assertEqual(set(json.loads(plaintext)), {"count", "rank"})
        self.assertEqual(handed, [plaintext])
        self.assertNotIn(SENTINEL.encode(), sent["body"])
        self.assertNotIn(SENTINEL, json.dumps(sent["headers"]))
        self.assertNotIn(self.asks[0].req_id, plaintext.decode())
        # Encrypted (aes128gcm), signed (VAPID), to the checked address only.
        self.assertEqual(sent["headers"]["Content-Encoding"], "aes128gcm")
        self.assertTrue(sent["headers"]["Authorization"].startswith("vapid t="), sent["headers"])
        self.assertEqual(sent["headers"]["TTL"], str(push_mod.TTL_S))
        self.assertEqual((sent["host"], sent["addresses"]), ("web.push.apple.com", ["17.253.1.2"]))
        self.assertTrue(self.receiver.endpoint.endswith(sent["path"]))
        # The JWT names the push service as audience and this host as subject.
        token = sent["headers"]["Authorization"].split("t=", 1)[1].split(",", 1)[0]
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        self.assertEqual((claims["aud"], claims["sub"]), ("https://web.push.apple.com", ORIGIN))
        with self.assertRaises(ValueError):
            push_mod.payload(1, 5)
        with self.assertRaises(ValueError):
            push_mod.payload("1", 1)
        print("ok  the payload is exactly {count, rank}, decrypted; no command, title or id in body or headers")

    def test_rank_is_the_most_urgent(self):
        self.subscribed()
        self.attach()
        self.push.seed()
        project = self.d.stores.projects.create("Calc", str(self.root))
        task = self.d.stores.tasks.create(project.id, "brief " + SENTINEL)
        self.d.stores.tasks.save(task)
        self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.d.stores.tasks.transition(task.id, TaskState.BLOCKED)
        self.push.evaluate()
        self.assertEqual(json.loads(self.receiver.decrypt(self.net.sent[-1]["body"])), {"count": 1, "rank": 4})
        print("ok  a blocked task alone pushes rank 4")


# -- when ----------------------------------------------------------------------------------


class SenderBase(PushBase):
    def setUp(self):
        super().setUp()
        self.subscribed()
        self.attach()
        self.waiting = {"ids": set(), "rank": 0}
        self.push.attention = lambda: (set(self.waiting["ids"]), len(self.waiting["ids"]),
                                       self.waiting["rank"] if self.waiting["ids"] else 0)
        self.push.seed()

    def counts(self):
        return [json.loads(self.receiver.decrypt(s["body"])) for s in self.net.sent]

    def wait(self, *ids, rank=1):
        self.waiting = {"ids": set(ids), "rank": rank}
        return self.push.evaluate()


class Sender(SenderBase):
    def test_coalescing_within_60_s(self):
        self.wait("appr:a")
        self.assertEqual(self.counts(), [{"count": 1, "rank": 1}])
        self.mono2.now += 10
        self.wait("appr:a", "appr:b")
        self.mono2.now += 10
        self.wait("appr:a", "appr:b", "q:t:1", rank=1)
        self.assertEqual(len(self.net.sent), 1, "nothing more inside the window")
        self.assertAlmostEqual(self.push.next_wait(), 40.05, places=3)
        self.mono2.now += 39.9
        self.wait("appr:a", "appr:b", "q:t:1")
        self.assertEqual(len(self.net.sent), 1)
        self.mono2.now += 0.2
        self.wait("appr:a", "appr:b", "q:t:1")
        self.assertEqual(self.counts(), [{"count": 1, "rank": 1}, {"count": 3, "rank": 1}],
                         "one push for what arrived in the window, with the count then")
        self.mono2.now += 120
        self.wait("appr:a", "appr:b", "q:t:1")
        self.assertEqual(len(self.net.sent), 2, "nothing new: no push")
        self.wait("appr:a")
        self.wait("appr:a", "appr:c")
        self.assertEqual(self.counts()[-1], {"count": 2, "rank": 1}, "a new row after the window: at once")
        print("ok  coalescing: one push per 60 s; what arrives inside is one push at the window's end")

    def test_coalesced_push_dropped_when_nothing_waits(self):
        self.wait("appr:a")
        self.mono2.now += 5
        self.wait("appr:a", "appr:b")
        self.mono2.now += 5
        self.wait()
        self.mono2.now += 60
        self.wait()
        self.assertEqual(len(self.net.sent), 1)
        print("ok  a coalesced push is dropped if nothing waits when the window ends")

    def test_no_push_for_a_fall_or_a_move(self):
        self.wait("appr:a", "task:t", rank=1)
        self.mono2.now += 100
        self.wait("task:t", rank=4)
        self.wait("task:t", rank=4)
        self.assertEqual(len(self.net.sent), 1)
        print("ok  a count that falls, or rows that stay, push nothing")

    def test_quiet_hours_nothing_then_one_at_the_end(self):
        self.local.now = local(23.5)
        self.wait("appr:a", rank=1)
        self.mono2.now += 100
        self.wait("appr:a", "q:t:1", rank=1)
        self.local.now = local(24 + 3)
        self.mono2.now += 100
        self.wait("appr:a", "q:t:1", "task:x", rank=1)
        self.assertEqual(self.net.sent, [], "nothing in quiet hours, rank 1 included")
        # The sender wakes when quiet hours end.
        self.assertAlmostEqual(self.push.next_wait(), min(push_mod.RECHECK_S, 4 * 3600 + 1))
        self.local.now = local(24 + 6.99)
        self.assertAlmostEqual(self.push.next_wait(), 0.01 * 3600 + 1, places=3)
        self.local.now = local(24 + 7)
        self.mono2.now += 100
        self.wait("appr:a", "q:t:1", rank=1)
        self.assertEqual(self.counts(), [{"count": 2, "rank": 1}], "one push at 07:00 for what still waits")
        self.mono2.now += 100
        self.wait("appr:a", "q:t:1", rank=1)
        self.assertEqual(len(self.net.sent), 1, "only one")
        print("ok  quiet hours: nothing (rank 1 included); one push at the end with the count then")

    def test_quiet_hours_end_with_nothing_waiting(self):
        self.local.now = local(1)
        self.wait("appr:a")
        self.wait()
        self.local.now = local(7.01)
        self.wait()
        self.assertEqual(self.net.sent, [])
        self.wait("appr:b")
        self.assertEqual(len(self.net.sent), 1, "the day goes on as usual")
        print("ok  nothing waits when quiet hours end: no push")

    def test_quiet_hours_off_and_notifications_off(self):
        self.d.quiet.update({"enabled": False})
        self.local.now = local(2)
        self.wait("appr:a")
        self.assertEqual(len(self.net.sent), 1, "quiet hours off: pushed at 02:00")
        self.push.set_enabled(False)
        self.mono2.now += 100
        self.wait("appr:a", "appr:b")
        self.assertEqual(len(self.net.sent), 1, "notifications off: nothing")
        self.push.set_enabled(True)
        self.mono2.now += 100
        self.wait("appr:a", "appr:b")
        self.assertEqual(len(self.net.sent), 1, "turning them on again does not replay")
        print("ok  quiet hours off pushes at night; the switch off pushes nothing")

    def test_statuses_404_410_delete_500_keeps(self):
        other = Receiver()
        cookie2 = self.signed_in(self.a)
        self.subscribed(other, cookie=cookie2)
        endpoints = {self.receiver.endpoint, other.endpoint}
        with self.assertLogs("jarvis.v2.push", level="INFO") as logs:
            self.net.statuses = [500, 503]
            self.wait("appr:a")
            self.assertEqual(len(self.push.subscriptions()), 2, "a 500 keeps the subscription")
            self.net.statuses = [410, 404]
            self.mono2.now += 100
            self.wait("appr:a", "appr:b")
            self.assertEqual(self.push.subscriptions(), [], "404 and 410 delete it")
        text = "\n".join(logs.output)
        self.assertIn("answered 500", text)
        self.assertIn("answered 503", text)
        self.assertIn("gone (410)", text)
        self.assertIn("gone (404)", text)
        for endpoint in endpoints:
            self.assertNotIn(endpoint, text)
            self.assertNotIn(endpoint.rsplit("/", 1)[1], text)
        print("ok  404/410 delete the subscription, 500 keeps it; logs name the status, never the endpoint")

    def test_a_transport_error_logs_its_class_only(self):
        def boom(*_a, **_k):
            raise OSError(f"connect to {self.receiver.endpoint} failed")
        self.push.transport = boom
        with self.assertLogs("jarvis.v2.push", level="WARNING") as logs:
            self.assertEqual(self.wait("appr:a"), {"sent": 0, "failed": 1, "removed": 0})
        self.assertNotIn(self.receiver.endpoint, "\n".join(logs.output))
        self.assertIn("OSError", "\n".join(logs.output))
        self.assertEqual(len(self.push.subscriptions()), 1)
        print("ok  a failed send logs the exception's class, never its text")

    def test_a_private_address_at_send_time_is_refused(self):
        self.net.addresses["web.push.apple.com"] = ["17.253.1.2", "192.168.1.10"]
        result = self.wait("appr:a")
        self.assertEqual(result, {"sent": 0, "failed": 1, "removed": 0})
        self.assertEqual(self.net.sent, [])
        print("ok  a push service that now resolves to a private address is not sent to")

    def test_only_active_passkeys_receive(self):
        rows = self.push.subscriptions()
        self.assertEqual(len(rows), 1)
        # A subscription whose passkey is not active (as if the hook were missed).
        with self.push._lock:
            self.push._save([dict(rows[0], passkey="k-000000000000")])
        self.wait("appr:a")
        self.assertEqual(self.net.sent, [])
        print("ok  only subscriptions of an active passkey are sent to")


class Review325(SenderBase):
    """The review of PR #325: LOW-2 (bounded sends), LOW-3 (retry), the NITs."""

    def test_payload_is_padded_to_a_fixed_length(self):
        for count, rank in ((0, 0), (1, 1), (3, 4), (999, 2), (10 ** 9, 1)):
            data = push_mod.payload(count, rank)
            self.assertEqual(len(data), push_mod.PAYLOAD_BYTES, count)
            self.assertEqual(json.loads(data), {"count": min(count, 999_999), "rank": rank})
        self.wait("appr:a")
        self.assertEqual(len(self.receiver.decrypt(self.net.sent[0]["body"])), push_mod.PAYLOAD_BYTES)
        print("ok  every payload is padded to the same length")

    def test_failed_push_is_retried_with_backoff(self):
        self.net.statuses = [500, 503, 500, 500]
        self.assertEqual(self.wait("appr:a")["failed"], 1)
        self.assertTrue(self.push._pending, "a push that reached nobody is still due")
        self.assertIsNone(self.push._last_sent, "and the window did not start")
        self.assertAlmostEqual(self.push.next_wait(), 60.05, places=3)
        self.mono2.now += 59
        self.assertIsNone(self.wait("appr:a"), "not before the backoff")
        sends = len(self.net.sent)
        for gap in (1.5, 300, 900, 900):
            self.mono2.now += gap
            self.wait("appr:a", "appr:b")
            sends += 1
            self.assertEqual(len(self.net.sent), sends, gap)
        # The last one went through (201): state cleared, the count is now's.
        self.assertFalse(self.push._pending)
        self.assertEqual(json.loads(self.receiver.decrypt(self.net.sent[-1]["body"])), {"count": 2, "rank": 1})
        self.assertEqual((self.push._failures, self.push._retry_at), (0, None))
        print("ok  LOW-3: a push that reached no phone is retried after 1, 5, 15, 15 min, with the count then")

    def test_retry_is_silent_in_quiet_hours(self):
        self.net.statuses = [500]
        self.wait("appr:a")
        self.local.now = local(23.5)
        self.mono2.now += 100
        self.wait("appr:a")
        self.assertEqual(len(self.net.sent), 1, "no retry inside quiet hours")
        self.local.now = local(24 + 7)
        self.mono2.now += 100
        self.wait("appr:a")
        self.assertEqual(len(self.net.sent), 2, "the morning push carries it")
        print("ok  LOW-3: a retry waits out quiet hours; the morning push carries it")

    def test_a_send_is_bounded(self):
        from unittest.mock import patch
        # A resolver that never answers costs RESOLVE_TIMEOUT_S, not forever.
        release = threading.Event()
        self.addCleanup(release.set)
        self.push.resolver = lambda host: (release.wait(30), ["17.253.1.2"])[1]
        with patch.object(push_mod, "RESOLVE_TIMEOUT_S", 0.3):
            start = time.monotonic()
            result = self.wait("appr:a")
            spent = time.monotonic() - start
        self.assertEqual(result, {"sent": 0, "failed": 1, "removed": 0})
        self.assertLess(spent, 2.0)
        release.set()
        # Many slow subscriptions: the whole send stops at its budget.
        self.push.resolver = self.net.resolve
        for _ in range(5):
            self.subscribed(Receiver(), cookie=self.signed_in(self.a))

        def slow(*_a, deadline=None, stop=None, **_k):
            time.sleep(0.25)
            return 201
        self.push.transport = slow
        self.mono2.now += 1000
        with patch.object(push_mod, "SEND_BUDGET_S", 0.6):
            start = time.monotonic()
            result = self.wait("appr:a", "appr:b")
            spent = time.monotonic() - start
        self.assertLess(spent, 1.5)
        self.assertGreater(result["failed"], 0, result)
        self.assertEqual(result["sent"] + result["failed"], 6, result)
        # Stopping: no subscription is tried after it.
        calls = []
        self.push.transport = lambda *a, **k: calls.append(1) or 201
        self.push._stopping.set()
        self.mono2.now += 1000
        self.assertEqual(self.wait("appr:a", "appr:b", "appr:c")["sent"], 0)
        self.assertEqual(calls, [])
        print("ok  LOW-2: a stuck resolution, a slow service and a stop each end the send in time")

    def test_https_post_honours_its_deadline_and_stop(self):
        with self.assertRaises(TimeoutError):
            push_mod.https_post("web.push.apple.com", ["17.253.1.2"], "/x", {}, b"", 10,
                                deadline=time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            push_mod.https_post("web.push.apple.com", ["17.253.1.2"], "/x", {}, b"", 10, stop=lambda: True)
        print("ok  LOW-2: the transport tries no address past its deadline or after a stop")

    def test_value_error_logs_its_class_only(self):
        self.push.encrypt = lambda sub, plaintext: (_ for _ in ()).throw(ValueError("secret " + sub["endpoint"]))
        with self.assertLogs("jarvis.v2.push", level="WARNING") as logs:
            self.wait("appr:a")
        text = "\n".join(logs.output)
        self.assertIn("(ValueError)", text)
        self.assertNotIn(self.receiver.endpoint, text)
        print("ok  a ValueError is logged by its class only")

    def test_next_wait_wakes_for_the_morning_seed(self):
        self.local.now = local(24 + 7.1)
        self.push._seen = None
        self.waiting = {"ids": {"task:t"}, "rank": 4}
        self.push.seed()
        self.assertLess(self.push.next_wait(), 1.0, "the morning push is looked at at once")
        self.wait("task:t", rank=4)
        self.assertEqual(len(self.net.sent), 1)
        self.assertEqual(self.push.next_wait(), push_mod.RECHECK_S)
        print("ok  a start just after quiet hours wakes at once for the morning push")

    def test_a_send_that_cannot_start_is_retried(self):
        real = self.push.send_all
        self.push.send_all = lambda count, rank: (_ for _ in ()).throw(RuntimeError("no"))
        with self.assertLogs("jarvis.v2.push", level="WARNING"):
            self.wait("appr:a")
        self.assertTrue(self.push._pending)
        self.assertIsNotNone(self.push._retry_at)
        self.push.send_all = real
        self.mono2.now += 61
        self.wait("appr:a")
        self.assertEqual(len(self.net.sent), 1)
        print("ok  a send that raises before any subscription is still due and retried")

    def test_a_restart_just_after_quiet_hours_still_pushes_the_morning(self):
        for hours, want in ((24 + 7.2, 1), (24 + 6.5, 0), (24 + 8.0, 0)):
            self.push._seen, self.push._was_quiet = None, False
            self.push._last_sent, self.push._pending = None, False
            self.net.sent.clear()
            self.local.now = local(hours)
            self.waiting = {"ids": {"task:t"}, "rank": 4}
            self.push.seed()
            self.mono2.now += 1000
            self.wait("task:t", rank=4)
            if hours == 24 + 6.5:
                self.local.now = local(24 + 7)
                self.wait("task:t", rank=4)
                want = 1
            self.assertEqual(len(self.net.sent), want, hours)
        print("ok  a restart in quiet hours or within 30 min of their end still sends the morning push")


class Threaded(PushBase):
    def test_stop_waits_for_a_send_under_way(self):
        self.subscribed()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def stuck(*_a, deadline=None, stop=None, **_k):
            started.set()
            while not release.wait(0.05):
                if stop is not None and stop():
                    raise TimeoutError("stopped")
            return 201
        self.push.transport = stuck
        self.push._attached = True
        self.push.start()
        eventually(lambda: self.push._seen is not None, "the seed")
        self.ask()
        eventually(started.is_set, "the send")
        begin = time.monotonic()
        self.push.stop()
        self.assertFalse(self.push._thread.is_alive(), "stop() returns once the send has ended")
        self.assertLess(time.monotonic() - begin, push_mod.SEND_BUDGET_S + 1)
        print("ok  LOW-2: stop() waits for a send under way, which stops at the flag")

    def test_the_sender_thread_pushes_on_inbox_changed(self):
        self.subscribed()
        self.attach()
        self.push._attached = True
        self.push.start()
        self.addCleanup(self.push.stop)
        eventually(lambda: self.push._seen is not None, "the seed")
        self.assertEqual(self.net.sent, [])
        self.ask()
        eventually(lambda: len(self.net.sent) == 1, "the push")
        self.assertEqual(json.loads(self.receiver.decrypt(self.net.sent[0]["body"])), {"count": 1, "rank": 1})
        self.push.stop()
        self.assertFalse(self.push._thread.is_alive())
        print("ok  the sender's thread pushes after inbox_changed, off the bus")


# -- subscriptions and routes --------------------------------------------------------------


class Routes(PushBase):
    def test_subscribe_status_unsubscribe(self):
        status, _, body = self.phone("GET", "/m/api/push", cookie=self.cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"key", "enabled", "subscribed"})
        self.assertEqual(len(base64.urlsafe_b64decode(body["key"] + "=")), 65)
        self.assertEqual((body["enabled"], body["subscribed"]), (True, False))
        self.subscribed()
        self.assertTrue(self.phone("GET", "/m/api/push", cookie=self.cookie)[2]["subscribed"])
        rows = self.push.subscriptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint"], self.receiver.endpoint)
        path = self.file("jarvis_push_subscriptions.json")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.file("jarvis_push_key.json")).st_mode), 0o600)
        # The same key every time (made once).
        self.assertEqual(self.phone("GET", "/m/api/push", cookie=self.cookie)[2]["key"], body["key"])
        # The same session again: replaced, not added.
        again = Receiver()
        self.subscribed(again)
        self.assertEqual([r["endpoint"] for r in self.push.subscriptions()], [again.endpoint])
        status, _, body = self.phone("DELETE", "/m/api/push/subscribe", {}, cookie=self.cookie)
        self.assertEqual((status, body), (200, {"subscribed": False}))
        self.assertEqual(self.push.subscriptions(), [])
        status, headers, _ = self.phone("PUT", "/m/api/push/subscribe", {}, cookie=self.cookie)
        self.assertEqual(status, 405)
        self.assertEqual(self.phone("POST", "/m/api/push", {}, cookie=self.cookie)[0], 405)
        print("ok  GET /m/api/push, subscribe (one per session), DELETE; files 0600 in a 0700 folder")

    def test_count_is_bounded(self):
        cookies = [self.signed_in(self.a) for _ in range(push_mod.MAX_SUBSCRIPTIONS + 2)]
        receivers = []
        for cookie in cookies:
            receivers.append(Receiver())
            self.wall.now += 1
            self.subscribed(receivers[-1], cookie=cookie)
        rows = self.push.subscriptions()
        self.assertEqual(len(rows), push_mod.MAX_SUBSCRIPTIONS)
        self.assertEqual({r["endpoint"] for r in rows}, {r.endpoint for r in receivers[2:]}, "the oldest go")
        print(f"ok  at most {push_mod.MAX_SUBSCRIPTIONS} subscriptions; the oldest go first")

    def test_routes_need_session_host_origin(self):
        body = {"subscription": self.receiver.json()}
        self.assertEqual(self.phone("GET", "/m/api/push")[0], 401)
        self.assertEqual(self.phone("POST", "/m/api/push/subscribe", body)[0], 401)
        self.assertEqual(self.phone("POST", "/m/api/push/subscribe", body, cookie=self.cookie, origin=None)[0], 403)
        self.assertEqual(self.phone("POST", "/m/api/push/subscribe", body, cookie=self.cookie,
                                    origin="https://evil.example")[0], 403)
        self.assertEqual(self.phone("DELETE", "/m/api/push/subscribe", {}, cookie=self.cookie, origin=None)[0], 403)
        self.assertEqual(self.phone("POST", "/m/api/push/subscribe", body, cookie=self.cookie,
                                    host="127.0.0.1")[0], 400)
        self.assertEqual(self.push.subscriptions(), [])
        # Never on the API or HUD listeners, owner or not.
        for port in (self.d.port, self.d.face_port, self.d.workshop_port):
            for method, path in (("GET", "/m/api/push"), ("POST", "/m/api/push/subscribe"),
                                 ("DELETE", "/m/api/push/subscribe")):
                host = f"127.0.0.1:{port}"
                headers = [("Host", host), ("Origin", f"http://{host}")]
                status, _, _ = raw(port, method, path, headers, json.dumps(body).encode())
                self.assertIn(status, (403, 404), (port, method, path))
        self.assertEqual(self.push.subscriptions(), [])
        print("ok  the phone's push routes: 401/403/400 without session, Origin, Host; never on 8405/8402/8403")

    def test_endpoints_refused(self):
        bad = ["http://web.push.apple.com/abc", "https://17.253.1.2/abc", "https://[::1]/abc",
               "https://web.push.apple.com:8443/abc", "https://user:pw@web.push.apple.com/abc",
               "https://evil.example.com/abc", "https://push.apple.com.evil.example/abc",
               "https://apple.com/abc", "https://WEB.PUSH.APPLE.COM/abc", "https://web.push.apple.com/a b",
               "https://web.push.apple.com/abc#frag", "https://web.push.apple.com", "ftp://web.push.apple.com/x",
               "https://web.push.apple.com/" + "a" * 1100, "https://localhost/abc", "https://127.0.0.1:443/x"]
        for endpoint in bad:
            with self.subTest(endpoint=endpoint[:60]):
                status, _, body = self.subscribe(Receiver(endpoint))
                self.assertEqual(status, 400, body)
                self.assertNotIn(endpoint, json.dumps(body))
        # A listed host that resolves to this machine, the LAN or link-local.
        for addresses in (["127.0.0.1"], ["10.0.0.5"], ["192.168.1.2"], ["169.254.169.254"], ["::1"],
                          ["fd00::1"], ["17.253.1.2", "172.16.0.1"], ["100.64.0.1"], ["0.0.0.0"], []):
            with self.subTest(addresses=addresses):
                self.net.addresses["api.push.apple.com"] = addresses
                status, _, body = self.subscribe(Receiver("https://api.push.apple.com/abc"))
                self.assertEqual(status, 400, (addresses, body))
        self.net.addresses.pop("api.push.apple.com")
        self.assertEqual(self.subscribe(Receiver("https://api.push.apple.com/abc"))[0], 400, "unresolvable")
        # Malformed keys.
        for keys in ({}, {"p256dh": "AAAA", "auth": "AAAA"}, {"p256dh": b64(b"\x04" + b"\x01" * 64),
                                                             "auth": b64(b"x" * 16)}):
            sub = dict(self.receiver.json(), keys=keys)
            status, _, _ = self.phone("POST", "/m/api/push/subscribe", {"subscription": sub}, cookie=self.cookie)
            self.assertEqual(status, 400, keys)
        self.assertEqual(self.push.subscriptions(), [])
        # The accepted ones.
        for endpoint in ("https://web.push.apple.com/x", "https://fcm.googleapis.com/fcm/send/x",
                         "https://web.push.apple.com:443/x?y=1"):
            self.assertEqual(push_mod.check_endpoint(endpoint)[0], endpoint.split("/")[2].split(":")[0])
        print("ok  http, IP literals, other ports, userinfo, unlisted hosts and private addresses are refused")

    def test_logout_end_and_revoke_delete_the_subscription(self):
        self.subscribed()
        cookie2 = self.signed_in(self.a)
        other = Receiver()
        self.subscribed(other, cookie=cookie2)
        self.assertEqual(len(self.push.subscriptions()), 2)
        status, _, _ = self.phone("POST", "/m/api/logout", {}, cookie=cookie2)
        self.assertEqual(status, 200)
        self.assertEqual([r["endpoint"] for r in self.push.subscriptions()], [self.receiver.endpoint])
        cookie3 = self.signed_in(self.a)
        self.subscribed(other, cookie=cookie3)
        session = next(r["session"] for r in self.push.subscriptions() if r["endpoint"] == other.endpoint)
        status, body = self.hud("POST", f"/phone/sessions/{session}/end")
        self.assertEqual(status, 200, body)
        self.assertEqual([r["endpoint"] for r in self.push.subscriptions()], [self.receiver.endpoint])
        key = self.push.subscriptions()[0]["passkey"]
        status, body = self.hud("POST", f"/phone/passkeys/{key}/revoke")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.push.subscriptions(), [])
        print("ok  logout, the desk's End and a revoke delete the subscription at once")

    def test_expiry_keeps_the_subscription_and_a_new_session_rebinds_it(self):
        self.subscribed()
        self.wall.now += 13 * 3600       # idle past 12 h: the session is gone
        self.assertEqual(self.me(self.cookie), 401)
        self.assertEqual(len(self.push.subscriptions()), 1)
        cookie = self.signed_in(self.a)
        self.subscribed(cookie=cookie)
        rows = self.push.subscriptions()
        self.assertEqual(len(rows), 1, "the same endpoint moves to the new session")
        self.assertTrue(self.phone("GET", "/m/api/push", cookie=cookie)[2]["subscribed"])
        print("ok  an expired session keeps its subscription; a new one takes it over")


class Desk(PushBase):
    def test_owner_only_view_switch_and_test(self):
        self.subscribed()
        status, view = self.hud("GET", "/phone/push")
        self.assertEqual(status, 200, view)
        self.assertEqual(view["enabled"], True)
        self.assertFalse(view["running"], "a test daemon never sends")
        self.assertEqual([r["service"] for r in view["subscriptions"]], ["web.push.apple.com"])
        text = json.dumps(view)
        self.assertNotIn(self.receiver.endpoint, text)
        self.assertNotIn(self.receiver.json()["keys"]["auth"], text)
        self.assertEqual(view["quiet"]["start"], "23:00")
        status, body = self.hud("POST", "/phone/push/test")
        self.assertEqual(status, 409, body)
        self.attach()
        status, body = self.hud("POST", "/phone/push/test")
        self.assertEqual((status, body), (200, {"sent": 1, "failed": 0, "removed": 0}))
        self.assertEqual(json.loads(self.receiver.decrypt(self.net.sent[0]["body"])), {"count": 0, "rank": 0})
        status, body = self.hud("POST", "/phone/push/settings", {"enabled": False})
        self.assertEqual((status, body["enabled"]), (200, False))
        self.assertEqual(stat.S_IMODE(os.lstat(self.file("push_settings.json")).st_mode), 0o600)
        self.assertEqual(self.hud("POST", "/phone/push/test")[0], 409)
        self.assertEqual(self.hud("POST", "/phone/push/settings", {"enabled": "no"})[0], 400)
        self.assertFalse(self.phone("GET", "/m/api/push", cookie=self.cookie)[2]["enabled"])
        # Owner-only: not the API listener, not without the HUD's Origin.
        for method, path, value in (("GET", "/phone/push", None), ("POST", "/phone/push/test", {}),
                                    ("POST", "/phone/push/settings", {"enabled": True})):
            self.assertEqual(self.hud(method, path, value, port=self.d.port)[0], 403, path)
            if method == "POST":
                self.assertEqual(self.hud(method, path, value, origin=False)[0], 403, path)
            self.assertEqual(self.phone(method, path, value, cookie=self.cookie)[0], 404, path)
        self.assertFalse(self.push.settings()["enabled"])
        print("ok  /phone/push: owner-only view (service host only), switch and test notification")


class ServiceWorker(rac.Remote):
    def test_sw_js_is_the_builds_entry_on_the_phones_listener_only(self):
        dist = Path(config.HUD_DIST)
        self.assertEqual(self.phone("GET", "/m/sw.js")[0], 404, "no build manifest: nothing")
        (dist / "assets").mkdir()
        (dist / "assets" / "sw-AbC123.js").write_text("self.addEventListener('push', () => {});")
        (dist / "build-manifest.json").write_text(json.dumps({"src/mobile/sw.ts": {
            "file": "assets/sw-AbC123.js", "name": "sw", "src": "src/mobile/sw.ts", "isEntry": True}}))
        status, headers, _ = rac.raw(self.port, "GET", "/m/sw.js", [("Host", HOST)])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(rac.raw(self.port, "GET", "/m/sw.js", [("Host", "evil.example")])[0], 400)
        for port in (self.d.port, self.d.face_port, self.d.workshop_port):
            self.assertEqual(rac.raw(port, "GET", "/m/sw.js", [("Host", f"127.0.0.1:{port}")])[0], 404, port)
        # Not reachable under /m/assets/ (the page does not import it), and a
        # manifest naming anything but a plain asset is refused.
        self.assertEqual(self.phone("GET", "/m/assets/sw-AbC123.js")[0], 404)
        for name in ("../index.html", "assets/../mobile.html", "assets/sw.css", "index.html"):
            (dist / "build-manifest.json").write_text(json.dumps({"src/mobile/sw.ts": {"file": name}}))
            self.assertEqual(self.phone("GET", "/m/sw.js")[0], 404, name)
        print("ok  /m/sw.js: the build's service worker, on the phone's listener only, with its headers")


# -- the boundary ---------------------------------------------------------------------------


class Boundary(PushBase):
    def test_start_sends_nothing_and_needs_main(self):
        self.assertIsNone(self.push._thread, "a test daemon's start() starts no sender")
        self.assertIsNone(self.push.transport)
        source = (ROOT / "jarvis" / "v2" / "daemon.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("push.attach_system()"), 1)
        main = source[source.index("\ndef main()"):source.index("\ndef shut_down(")]
        self.assertIn("daemon.push.attach_system()", main)
        # A daemon attached before start, with something already waiting.
        other = Daemon(Stores(self.root / "other"), {name: rac.Fake(name) for name in PN},
                       lambda *_: lambda *_: Decision.DENY, 0, remote_host=HOST)
        net = FakeNet()
        other.push.resolver = net.resolve
        project = other.stores.projects.create("P", str(self.root))
        task = other.stores.tasks.create(project.id, "x")
        other.stores.tasks.save(task)
        other.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        other.stores.tasks.transition(task.id, TaskState.BLOCKED)
        other.push._save([{"id": "p-0123456789ab", "endpoint": self.receiver.endpoint,
                           "p256dh": self.receiver.json()["keys"]["p256dh"],
                           "auth": self.receiver.json()["keys"]["auth"], "session": "s-0123456789abcdef",
                           "passkey": "k-0123456789ab", "created": 1.0, "ua": ""}])
        other.push.transport = net.post
        other.push._attached = True
        other.start()
        self.addCleanup(other.stop)
        eventually(lambda: other.push._seen is not None, "the seed")
        self.assertEqual(other.push._seen, {f"task:{task.id}"})
        self.assertEqual(net.sent, [], "start() sends nothing for what already waits")
        print("ok  Daemon.start() sends nothing; only main() attaches the sender")

    def permit_table(self, profile):
        """Each read of the phone's folder through the v2 gate (`build_permit`),
        as a Claude chat's tools ask it: (decision, layer, asked) per probe."""
        from jarvis.v2.model import Role
        from jarvis.v2.permissions import PermitContext, build_permit
        from jarvis.v2.provider import Brief

        class Owner:
            human_backed = True

            def __init__(self):
                self.seen = []

            def ask(self, request):
                self.seen.append(request)
                return Decision.DENY

        remote = Path(config.V2_DATA_DIR) / "remote"
        key = remote / "jarvis_push_key.json"
        brief = Brief(role=Role.IMPLEMENTER, cwd=str(self.root), profile=profile)
        owner = Owner()
        permit = build_permit(PermitContext(thread_id="abcd1234", brief=brief, provider="claude"), owner)
        probes = [("Bash", {"command": f"cat {remote}/*"}), ("Bash", {"command": f"grep -r pem {remote}"}),
                  ("Bash", {"command": f"cat {key}"}), ("Bash", {"command": f"cd {remote} && cat *"}),
                  ("Grep", {"pattern": "pem", "path": str(remote)}),
                  ("Grep", {"pattern": "pem", "path": str(Path(config.V2_DATA_DIR))}),
                  ("Read", {"file_path": str(key)}),
                  ("Read", {"file_path": str(remote / "sessions.json")}),
                  ("Glob", {"pattern": "*", "path": str(remote)})]
        out = []
        for tool, args in probes:
            before = len(owner.seen)
            decision = permit(tool, args, brief)
            out.append((tool, json.dumps(args), decision, len(owner.seen) > before))
        return out

    def test_agents_cannot_read_the_phone_folder_by_any_spelling(self):
        """MED-1 (review of PR #325): `<data root>/remote` is a credential
        folder. Every read of it is refused or put to the owner, under AUTO
        and ASK, v2 and v1; no file in it is exempt."""
        from jarvis import config as cfg, runtime, tools
        from jarvis.tools import files, shell
        from jarvis.v2 import permissions as v2
        from jarvis.v2.model import PermissionProfile as P
        self.subscribed()
        remote = Path(config.V2_DATA_DIR) / "remote"
        self.assertIn(str(remote), cfg.credential_dirs())
        self.assertTrue((remote / "jarvis_push_key.json").exists())
        for profile in (P.AUTO, P.ASK):
            for tool, args, decision, asked in self.permit_table(profile):
                with self.subTest(profile=profile.value, tool=tool, args=args[-60:]):
                    self.assertEqual(decision, Decision.DENY, f"asked={asked}")
                    if tool == "Bash" and "jarvis_push_" not in args:
                        self.assertTrue(asked, "a shell read of the folder is put to the owner")
                    if "jarvis_push_" in args:
                        self.assertFalse(asked, "a push file named outright is refused, never asked (MED-A)")
        # v2's read tools are refused there outright (layer 1).
        for args in ({"file_path": str(remote / "jarvis_push_key.json")}, {"path": str(remote)},
                     {"file_path": str(remote / "x" / ".." / "jarvis_push_subscriptions.json")}):
            self.assertTrue(v2.denied_read("Read" if "file_path" in args else "Grep", args), args)
        self.assertIsNone(v2.denied_read("Grep", {"path": str(self.root / "home")}))
        # v1: the file tools and run_readonly refuse every spelling; none of
        # the folder's files is "readable config".
        for name in ("jarvis_push_key.json", "jarvis_push_subscriptions.json", "push_settings.json",
                     "passkeys.json", "sessions.json"):
            target = remote / name
            self.assertTrue(files._credential_folder(target), name)
            with runtime.unconfined():
                out = tools.dispatch("read_file", json.dumps({"path": str(target)}))
            self.assertTrue("credential folder" in out.text or "is protected" in out.text, name)
            self.assertNotIn("PRIVATE KEY", out.text)
            self.assertNotIn(self.receiver.endpoint, out.text)
        for command in (f"cat {remote}/*", f"grep -r pem {remote}", f"cat {remote}/jarvis_push_key.json",
                        f"head -c 100 {remote}/jarvis_push_subscriptions.json", f"ls {remote}",
                        f"rg pem {Path(config.V2_DATA_DIR)}"):
            out = shell.run_readonly(command)
            self.assertTrue(out.startswith("Error"), f"{command}: {out[:120]}")
            self.assertNotIn("PRIVATE KEY", out)
        with runtime.unconfined():
            out = tools.dispatch("grep_files", json.dumps({"pattern": "pem", "path": str(remote)}))
        self.assertNotIn("PRIVATE KEY", out.text)
        self.assertNotIn(self.receiver.endpoint, out.text)
        print("ok  MED-1: the phone's folder is a credential folder: every read refused or asked (v2 AUTO/ASK, v1)")

    def test_meda_the_push_files_are_refused_by_name_anywhere(self):
        """MED-A (delta review of PR #325): the folder rule covers only
        `<data root>/remote`, so the two secret files are refused by their
        exact name too (a copy elsewhere, v1's run_command, v2's shell DENY,
        never asked), and what a command prints of them is scrubbed."""
        import shutil
        from jarvis import runtime, tools
        from jarvis.tools import secrets as v1_secrets, shell
        from jarvis.v2 import permissions as v2
        from jarvis.v2.model import PermissionProfile as P, Role
        from jarvis.v2.permissions import PermitContext, build_permit
        from jarvis.v2.provider import Brief
        self.subscribed()
        remote = Path(config.V2_DATA_DIR) / "remote"
        key = remote / "jarvis_push_key.json"
        subs = remote / "jarvis_push_subscriptions.json"
        copy = self.root / "copy2"
        shutil.copytree(Path(config.V2_DATA_DIR), copy)
        copied = copy / "remote" / "jarvis_push_key.json"
        pem_lines = [line for line in json.loads(key.read_text())["pem"].splitlines() if not line.startswith("-----")]

        class Owner:
            human_backed = True
            seen: list = []

            def ask(self, request):
                self.seen.append(request)
                return Decision.ALLOW

        for profile in (P.AUTO, P.ASK):
            brief = Brief(role=Role.IMPLEMENTER, cwd=str(self.root), profile=profile)
            owner = Owner()
            owner.seen = []
            permit = build_permit(PermitContext(thread_id="abcd1234", brief=brief, provider="claude"), owner)
            for command in (f"cat {key}", f"cat {copied}", f"cat {subs}", f"base64 {copied}",
                            f"cp {copied} /tmp/x", f"python3 -c \"print(open('{copied}').read())\""):
                with self.subTest(profile=profile.value, command=command[-50:]):
                    self.assertEqual(permit("Bash", {"command": command}, brief), Decision.DENY)
                    self.assertEqual(owner.seen, [], "refused, never put to the owner")
            for path in (copied, copy / "remote" / "jarvis_push_subscriptions.json"):
                self.assertEqual(permit("Read", {"file_path": str(path)}, brief), Decision.DENY, path)
        for command in (f"cat {key}", f"cat {copied}", f"head {subs}", f"cat {copy}/remote/jarvis_push_key.json"):
            self.assertTrue(v1_secrets.protected_in_command(command), command)
            self.assertTrue(v2.denied_command(command), command)
            out = shell.run_command(command, "probe")
            self.assertTrue(out.startswith("Error"), out[:120])
            self.assertFalse(any(line in out for line in pem_lines))
        with runtime.unconfined():
            out = tools.dispatch("read_file", json.dumps({"path": str(copied)}))
        self.assertIn("protected", out.text)
        # The scrub: a spelling no name rule sees (a glob onto the copy),
        # printed raw or as the file's JSON, comes back without the key's
        # lines, the endpoint or the auth secret.
        for text in (key.read_text(), json.loads(key.read_text())["pem"], subs.read_text()):
            scrubbed = v1_secrets.scrub(text)
            self.assertFalse(any(line in scrubbed for line in pem_lines))
            self.assertNotIn(self.receiver.endpoint, scrubbed)
            self.assertNotIn(self.receiver.json()["keys"]["auth"], scrubbed)
        out = shell.run_command(f"cat {copy}/remote/jarvis_push_*", "probe")
        self.assertFalse(any(line in v1_secrets.scrub(out) for line in pem_lines))
        print("ok  MED-A: the push files are refused by exact name anywhere (v1 run_command, v2 DENY) and scrubbed")

    def test_meda_the_scrub_never_raises_on_a_malformed_push_file(self):
        """Delta check of PR #325, LOW: `scrub()` runs on every tool output,
        so a corrupt or hand-edited push file must give no values, never an
        exception (a non-list `subscriptions`, JSON nested past the
        recursion limit)."""
        from jarvis.tools import secrets as v1_secrets
        remote = Path(config.V2_DATA_DIR) / "remote"
        remote.mkdir(parents=True, exist_ok=True)
        deep = "[" * 100_000
        for subs_text, key_text in (('{"subscriptions": 5}', '{"pem": 5}'),
                                    ("true", "true"),
                                    ('{"subscriptions": {"a": 1}}', "[]"),
                                    (deep, deep)):
            with self.subTest(subs=subs_text[:30]):
                (remote / "jarvis_push_subscriptions.json").write_text(subs_text)
                (remote / "jarvis_push_key.json").write_text(key_text)
                self.assertEqual(v1_secrets._push_values(), ())
                self.assertEqual(v1_secrets.scrub("plain text"), "plain text")
        print("ok  MED-A: a malformed push file gives no values and the scrub never raises")

    def test_lowa_folders_holding_it_below_home_are_refused(self):
        """LOW-A: with the data root under $HOME (the real layout), Read, Grep
        and Glob at every folder that holds the phone's folder below $HOME,
        or a symlink to one, are refused; $HOME itself and a project are not."""
        from unittest.mock import patch
        from jarvis.v2 import permissions as v2
        home = Path(os.environ["HOME"])
        data = home / ".local" / "share" / "jarvis" / "v2"
        (data / "remote").mkdir(parents=True)
        (data / "remote" / "jarvis_push_key.json").write_text("{}")
        (home / "projects" / "calc").mkdir(parents=True)
        link = home / "projects" / "sharelink"
        link.symlink_to(home / ".local" / "share" / "jarvis")
        with patch.object(config, "V2_DATA_DIR", data):
            for path in (data, data.parent, data.parent.parent, home / ".local", link,
                         data / "remote", data / "remote" / "x.json"):
                for tool, key in (("Grep", "path"), ("Glob", "path"), ("Read", "file_path")):
                    self.assertTrue(v2.denied_read(tool, {key: str(path)}), (tool, path))
            for path in (home, home / "projects" / "calc", home / ".local" / "share" / "jarvis" / "chat-worktrees" / "w",
                         self.root):
                self.assertIsNone(v2.denied_read("Grep", {"path": str(path)}), path)
        print("ok  LOW-A: every folder holding the phone's folder below $HOME is refused; $HOME and projects are not")

    def test_agents_cannot_write_the_push_files(self):
        from jarvis import permissions as v1, rules as v1_rules, runtime, tools
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2
        self.subscribed()
        for name in ("jarvis_push_key.json", "jarvis_push_subscriptions.json", "push_settings.json"):
            target = self.file(name)
            self.assertIn(target.resolve(), files._protected_state(), name)
            self.assertIn(target.resolve(), v2.protected_paths(), name)
            self.assertTrue(v2.denied_file(str(target)), name)
            for command in (f"cp /tmp/x {target}", f"echo x > {target}", f"rm {target}"):
                self.assertTrue(v2.denied_command(command), command)
                self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
            before = target.read_bytes() if target.exists() else None
            with runtime.unconfined():
                tools.dispatch("write_file", json.dumps({"path": str(target), "content": "{}"}))
            self.assertEqual(target.read_bytes() if target.exists() else None, before, name)
        print("ok  the push files are protected state: no agent write, v1 or v2")

    def test_low1_globs_are_back_to_mains_verdict(self):
        """LOW-1: the push files are no credential bundles by name, so no
        glob is refused for matching them (main's verdict: not refused)."""
        from jarvis.tools import secrets as v1_secrets
        from jarvis.v2 import permissions as v2
        self.assertFalse({"jarvis_push_key.json", "jarvis_push_subscriptions.json"} & v1_secrets.PROTECTED_NAMES)
        for command in ("ls j*", "ls jarvis*", "find . -name 'j*'", "rg -g 'jarvis*' x", "rg -g '*key.json' x",
                        "find . -name '*_key.json'", "ls *push*", "rg -g '*push*' x", "ls *subscription*",
                        "git tag -l 'v*'", "ls *.pem", "ls p*"):
            self.assertIsNone(v1_secrets.protected_in_command(command), command)
            self.assertIsNone(v2.denied_command(command), command)
        # Main's bundles are unchanged.
        self.assertTrue(v1_secrets.protected_in_command("cat google_token.*"))
        print("ok  LOW-1: ls j*, rg -g '*key.json', ls *push* and the rest are back to main's verdict")

    def test_key_never_in_logs_status_or_answers(self):
        with self.assertLogs("jarvis.v2.push", level="INFO") as logs:
            self.subscribed()
        pem = json.loads(self.file("jarvis_push_key.json").read_text())["pem"]
        body = pem.split("-----")[2].replace("\n", "")
        status_text = raw(self.d.port, "GET", "/status", [("Host", f"127.0.0.1:{self.d.port}")])[2].decode()
        phone_text = json.dumps(self.phone("GET", "/m/api/push", cookie=self.cookie)[2])
        hud_text = json.dumps(self.hud("GET", "/phone/push")[1])
        for text in ("\n".join(logs.output), status_text, phone_text, hud_text):
            self.assertNotIn(body[20:60], text)
            self.assertNotIn(self.receiver.endpoint, text)
        print("ok  the private key and the endpoint are in no log line, /status or route answer")

    def test_source(self):
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/push.py", SELF_PROTECTED)
        text = (ROOT / "jarvis" / "v2" / "push.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^(import pywebpush|from pywebpush|import py_vapid|from py_vapid)",
                            "imported only when used")
        self.assertEqual((push_mod.COALESCE_S, push_mod.MAX_SUBSCRIPTIONS), (60.0, 8))
        self.assertEqual(push_mod.ATTENTION_RANKS, frozenset({1, 2, 3, 4}))
        # The credential names are judged as globs too: common ones stay usable.
        from jarvis.tools import secrets as v1_secrets
        for command in ("git tag -l 'v*'", "ls *.pem", "ls p*", "ls *.key"):
            self.assertIsNone(v1_secrets.protected_in_command(command), command)
        print("ok  push.py is SELF_PROTECTED; pywebpush imported only when used; 60 s and 8 pinned")


if __name__ == "__main__":
    unittest.main(verbosity=1)
