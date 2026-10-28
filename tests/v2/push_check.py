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

    def post(self, host, addresses, path, headers, body, timeout):
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


class Sender(PushBase):
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


class Threaded(PushBase):
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

    def test_agents_cannot_read_or_write_the_key_or_subscriptions(self):
        from jarvis import permissions as v1, rules as v1_rules, runtime, tools
        from jarvis.tools import files, secrets as v1_secrets
        from jarvis.v2 import permissions as v2
        self.subscribed()
        for name in ("jarvis_push_key.json", "jarvis_push_subscriptions.json"):
            target = self.file(name)
            self.assertTrue(target.exists(), name)
            # Reads: v2 layer 1 (Claude's Read, a shell line) and v1.
            self.assertTrue(v2.denied_read("Read", {"file_path": str(target)}), name)
            for command in (f"cat {target}", f"head -c 100 {target}", f"cp {target} /tmp/x",
                            f"python3 -c \"print(open('{target}').read())\"", f"cat {target.parent}/jarvis_push_*",
                            f"base64 {target}"):
                self.assertTrue(v2.denied_command(command), command)
                self.assertTrue(v1_secrets.protected_in_command(command), command)
            with runtime.unconfined():
                out = tools.dispatch("read_file", json.dumps({"path": str(target)}))
            self.assertIn("protected", out.text)
            self.assertNotIn("PRIVATE KEY", out.text)
            self.assertNotIn(self.receiver.endpoint, out.text)
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
        print("ok  the VAPID key and subscriptions are unreadable and unwritable to agents (v1, v2); the switch unwritable")

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
        self.assertTrue(v1_secrets.protected_in_command("cat jarvis_push_*"))
        print("ok  push.py is SELF_PROTECTED; pywebpush imported only when used; 60 s and 8 pinned")


if __name__ == "__main__":
    unittest.main(verbosity=1)
