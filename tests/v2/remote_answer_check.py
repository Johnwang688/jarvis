"""Answering from the phone (phone surface design PH-4, PH-7, PH-10, §4.1;
work package P-4): `jarvis/v2/remote_answer.py`, the per-answer half of
`jarvis/v2/remote_auth.py`, `owner_verified` in `jarvis/v2/approvals.py`
and the §4.1 monitor `jarvis/v2/remote_secure.py`.

Free and hermetic, as tests/v2/remote_auth_check.py: real daemons on
ephemeral loopback ports (never 8402, 8403, 8405 or 8408), temporary HOME and
data, real WebAuthn assertions from a software authenticator verified by the
real `webauthn` library. No Windows command runs: the §4.1 reads are a fake
system (`FakeSystem`) whose answers are the shapes the live machine gave,
and each check is flipped by changing one of them (or, for the two checks
that read the daemon itself, the socket or the passkeys).

What it pins (the package's acceptance items):

  - Allow or Always without a fresh assertion is refused;
  - an Allow assertion replayed as Always is refused, and one for another
    req_id; an expired or a used challenge (409);
  - UV clear is refused, and an assertion from another passkey;
  - Deny works with the session only;
  - Always shows its rule (a dry run) and writes it only after a verified
    answer;
  - a HUD-only card is answerable under §4.1 and refused (409, "answer at
    the desk") when any single check fails: one test per condition;
  - a HUD-only card's private detail never reaches a log line, the bus,
    `/m/api/events` or the inbox list;
  - a card the phone's scrub would change refuses Allow and Always, Deny
    still works;
  - `owner_verified` callers: a grep, and the behaviour (Discord's resolve
    and the inbox dispatch without an assertion cannot satisfy hud_only);
  - questions, task questions and decisions answered on the phone, with
    `via="phone"` where the source takes one and no desk authority;
  - PH-10's 600 s wait; `is_owner` false on the remote listener; the agent
    path's DENY over the new routes;
  - the monitor: each condition's failure, a command that times out, and a
    failure inside the display cache seen at once by the next answer.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_answer_check.py
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rac = _load("remote_auth_check")
ic = _load("inbox_check")

from jarvis import config, permissions as v1_permissions, rules  # noqa: E402
from jarvis.v2 import (approvals as approvals_mod, chat_reads, desk, inbox, projects, remote_answer,  # noqa: E402
                       remote_api, remote_auth, remote_secure)
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import OpenQuestion, ProviderName as PN, Role, Spec, TaskState  # noqa: E402
from jarvis.v2.provider import Brief, Decision, UserMessage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

HOST, ORIGIN, raw = rac.HOST, rac.ORIGIN, rac.raw
SUFFIX = HOST.split(".", 1)[1]
PRIVATE = "SENTINEL-private-browser-detail-0c4d"
# A GitHub-shaped token, assembled at run time (no literal a scanner
# matches is committed): the phone's scrub withholds it.
TOKEN = "ghp_" + "Zq7" * 12
PEM_BODY = "MII" + "EowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
PEM = ("-----BEGIN " + "RSA PRIVATE KEY-----\n" + PEM_BODY + "\n-----END " + "RSA PRIVATE KEY-----\n")
PASSWORD = "hunt" + "er2" + "hunt" + "er2" + "xyzzy"
NUMBER = int("98765" + "43210" + "9876543")


def eventually(predicate, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"{what} did not settle")
        time.sleep(0.005)


class FakeSystem:
    """The external reads one §4.1 check makes, answered as this PC answered
    them (tailscale 1.102, `ss -H -ltne`, `Get-NetTCPConnection`), for this
    test daemon's host and port; `bad` replaces one read's answer (a string
    or an exception)."""

    def __init__(self, daemon):
        self.d = daemon
        self.bad: dict = {}
        self.calls: list[str] = []

    def good(self) -> dict:
        port, host = self.d.remote_port, self.d.remote_host
        ino = os.fstat(self.d.remote_auth.server.socket.fileno()).st_ino
        return {
            "serve": json.dumps({"TCP": {"443": {"HTTPS": True}},
                                 "Web": {f"{host}:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}),
            "status": json.dumps({"BackendState": "Running", "Self": {"DNSName": host + ".", "OS": "windows"},
                                  "MagicDNSSuffix": SUFFIX, "CertDomains": [host]}),
            "ss": f"LISTEN 0      5      127.0.0.1:{port} 0.0.0.0:* uid:1000 ino:{ino} sk:7009 <->\n",
            # As this PC answered (review of PR #313): CRLF lines, then the end line.
            "windows": "127.0.0.1 wslrelay\r\n" + remote_secure.END_OF_LISTENERS + "\r\n",
        }

    def __call__(self, argv, timeout):
        names = {tuple(v): k for k, v in remote_secure.reads(self.d.remote_port).items()}
        name = names[tuple(argv)]
        self.calls.append(name)
        value = self.bad.get(name, self.good()[name])
        if isinstance(value, Exception):
            raise value
        return value


class FakeServer:
    """A listener whose socket reports `address`: what check_loopback reads."""

    def __init__(self, address, port):
        self.server_address = (address, port)
        self.socket = SimpleNamespace(getsockname=lambda: (address, port), family=socket.AF_INET)


class Phone(rac.Remote):
    """A real daemon (inbox_check's question-asking brains and its spy task
    runner), the remote listener bound, one passkey paired and signed in,
    and a fake system under §4.1 on which every check passes."""

    def setUp(self):
        rac.Base.setUp(self)
        self.brain = ic.Brain(PN.FAST)
        self.d = Daemon(Stores(), {name: (self.brain if name == PN.FAST else ic.Brain(name)) for name in PN},
                        lambda *_: lambda *_: Decision.DENY, 0, remote_host=HOST)
        self.runner = ic.SpyRunner(self.d)
        self.d.runner = self.runner
        self.d.start()
        self.addCleanup(self.d.stop)
        self.port = self.d.remote_port
        self.assertIsInstance(self.port, int)
        self.assertFalse({self.d.port, self.d.face_port, self.d.workshop_port, self.port} & rac.LIVE_PORTS)
        self.auth = self.d.remote_auth
        self.wall = rac.Clock(1_900_000_000.0)
        self.mono = rac.Clock(5_000.0)
        self.auth.wall = self.wall
        self.auth.mono = self.mono
        self.system = FakeSystem(self.d)
        self.d.secure.run = self.system
        self.project = self.d.stores.projects.create("Calc", str(self.root))
        self.a = self.pair()
        self.cookie = self.signed_in(self.a)
        self.allowlist = Path(config.ALLOWLIST_PATH)

    # -- approvals -------------------------------------------------------------------

    def ask(self, **kw) -> tuple[ApprovalRequest, dict, threading.Thread]:
        """An approval asked on a background thread, as a permit asks."""
        kw.setdefault("tool", "Bash")
        kw.setdefault("args", {"command": "git status"})
        kw.setdefault("command", kw["args"].get("command") if isinstance(kw["args"], dict) else None)
        kw.setdefault("provider", "claude")
        request = ApprovalRequest(**kw)
        out: dict = {}
        worker = threading.Thread(target=lambda: out.setdefault("decision", self.d.approvals.ask(request)),
                                  daemon=True)
        worker.start()
        eventually(lambda: any(r.req_id == request.req_id and request.req_id
                               for r in self.d.approvals.pending()), what="the ask")
        return request, out, worker

    def private(self, **kw):
        browser = {"tab_id": "0a1b2c3d", "action_id": "f" * 32, "pane": 1, "page_identity": 1,
                   "document_generation": 1, "action": "click", "category": "ordinary",
                   "host": "example.com", "path": "/" + PRIVATE, "target": PRIVATE, "text": "", "rect": None}
        return self.ask(tool="hud_browser_act", args={"action": "click", "pane": "1", "target": PRIVATE,
                                                     "text": "", "file": ""},
                        command=json.dumps({"action": "click", "pane": "1", "target": PRIVATE, "text": "",
                                            "file": ""}),
                        layer="browser-gate", hud_only=True, browser=browser, max_wait_s=120, **kw)

    def row(self, request) -> str:
        return f"appr:{request.req_id}"

    def detail(self, row, cookie=None):
        return self.phone("GET", f"/m/api/inbox/{row}/detail", cookie=cookie or self.cookie)

    def options(self, row, decision, cookie=None):
        return self.phone("POST", f"/m/api/inbox/{row}/answer/options", {"decision": decision},
                          cookie=cookie or self.cookie)

    def post_answer(self, row, body, cookie=None):
        return self.phone("POST", f"/m/api/inbox/{row}/answer", body, cookie=cookie or self.cookie)

    def assertion(self, row, decision, authenticator=None, cookie=None, **kw):
        status, _, body = self.options(row, decision, cookie=cookie)
        self.assertEqual(status, 200, body)
        return (authenticator or self.a).get(body["publicKey"], **kw), body

    def verified(self, row, decision, cookie=None, **kw):
        credential, _ = self.assertion(row, decision, cookie=cookie, **kw)
        return self.post_answer(row, {"decision": decision, "assertion": credential}, cookie=cookie)

    def pending(self, request) -> bool:
        return any(r.req_id == request.req_id for r in self.d.approvals.pending())

    def decided(self, out, worker, want):
        worker.join(5)
        self.assertEqual(out.get("decision"), want)

    def written(self) -> list:
        return v1_permissions.load_allowlist() if self.allowlist.exists() else []

    def secure_ok(self):
        result = self.d.secure.check()
        self.assertTrue(result["ok"], result)


# -- PH-4: per-answer assertions ------------------------------------------------------------


class Assertions(Phone):
    def test_allow_or_always_without_a_fresh_assertion_is_refused(self):
        self.secure_ok()
        request, out, worker = self.ask()
        row = self.row(request)
        for decision in ("allow", "always"):
            status, _, body = self.post_answer(row, {"decision": decision})
            self.assertEqual(status, 401, body)
            # A credential-shaped thing that was never issued a challenge.
            fake = self.a.get({"rpId": HOST, "challenge": remote_auth.b64url(os.urandom(32))})
            status, _, body = self.post_answer(row, {"decision": decision, "assertion": fake})
            self.assertEqual(status, 409, body)
        self.assertTrue(self.pending(request), "nothing was answered")
        status, _, body = self.verified(row, "allow")
        self.assertEqual((status, body["decision"], body["authority"]), (200, "allow", "hud"), body)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  PH-4: Allow and Always need a fresh assertion (401 without, 409 for one never issued)")

    def test_an_allow_assertion_is_never_an_always_nor_another_card(self):
        self.secure_ok()
        one, out, worker = self.ask()
        two, _, _ = self.ask(args={"command": "git log"})
        credential, _ = self.assertion(self.row(one), "allow")
        status, _, body = self.post_answer(self.row(one), {"decision": "always", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertEqual(self.written(), [], "no rule written")
        # The challenge was spent by that attempt: the same assertion, now
        # as the Allow it was, is a used challenge.
        status, _, body = self.post_answer(self.row(one), {"decision": "allow", "assertion": credential})
        self.assertEqual(status, 409, body)
        credential, _ = self.assertion(self.row(one), "allow")
        status, _, body = self.post_answer(self.row(two), {"decision": "allow", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertTrue(self.pending(one) and self.pending(two), "neither card was answered")
        # And Always's assertion is not an Allow either.
        credential, _ = self.assertion(self.row(one), "always")
        status, _, body = self.post_answer(self.row(one), {"decision": "allow", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertTrue(self.pending(one))
        print("ok  PH-4: an Allow assertion replayed as Always, or on another card, is refused (and spent)")

    def test_an_expired_or_used_challenge_is_refused(self):
        self.secure_ok()
        request, out, worker = self.ask()
        row = self.row(request)
        credential, _ = self.assertion(row, "allow")
        self.mono.now += remote_auth.CHALLENGE_S + 1
        status, _, body = self.post_answer(row, {"decision": "allow", "assertion": credential})
        self.assertEqual((status, body), (409, {"error": "challenge expired"}))
        credential, _ = self.assertion(row, "allow")
        self.assertEqual(self.post_answer(row, {"decision": "allow", "assertion": credential})[0], 200)
        self.decided(out, worker, Decision.ALLOW)
        # The same assertion again, on a new card with the same answer.
        again, _, _ = self.ask()
        status, _, body = self.post_answer(self.row(again), {"decision": "allow", "assertion": credential})
        self.assertEqual(status, 409, body)
        self.assertTrue(self.pending(again))
        print("ok  PH-4: an expired (60 s, monotonic) or used challenge is refused (409)")

    def test_uv_clear_or_another_passkey_is_refused(self):
        self.secure_ok()
        request, out, worker = self.ask()
        row = self.row(request)
        credential, _ = self.assertion(row, "allow", uv=False)
        self.assertEqual(self.post_answer(row, {"decision": "allow", "assertion": credential})[0], 401)
        other = self.pair()
        other_cookie = self.signed_in(other)
        # Another paired passkey answering this session's challenge.
        credential, _ = self.assertion(row, "allow", authenticator=other)
        self.assertEqual(self.post_answer(row, {"decision": "allow", "assertion": credential})[0], 401)
        # The other session's own challenge, sent under this session.
        credential, _ = self.assertion(row, "allow", authenticator=other, cookie=other_cookie)
        self.assertEqual(self.post_answer(row, {"decision": "allow", "assertion": credential})[0], 401)
        # A wrong origin, a cross-origin ceremony, a wrong rpId.
        for kw in ({"origin": "https://evil.ts.net"}, {"client_extra": {"crossOrigin": True}},
                   {"rp_id": "evil.ts.net"}):
            credential, _ = self.assertion(row, "allow", **kw)
            self.assertEqual(self.post_answer(row, {"decision": "allow", "assertion": credential})[0], 401, kw)
        self.assertTrue(self.pending(request))
        # The other passkey verifies for its own session.
        credential, _ = self.assertion(row, "allow", authenticator=other, cookie=other_cookie)
        status, _, body = self.post_answer(row, {"decision": "allow", "assertion": credential}, cookie=other_cookie)
        self.assertEqual(status, 200, body)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  PH-4: UV clear, another passkey, another session's challenge, a bad origin or rpId: 401")

    def test_the_counter_rule_holds_per_answer(self):
        self.secure_ok()
        counting = self.pair(rac.Authenticator(step=1))
        cookie = self.signed_in(counting)
        request, _, _ = self.ask()
        row = self.row(request)
        credential, _ = self.assertion(row, "allow", authenticator=counting, cookie=cookie,
                                       count=counting.count)
        status, _, body = self.post_answer(row, {"decision": "allow", "assertion": credential}, cookie=cookie)
        self.assertEqual(status, 401, "a counter that did not rise")
        status, _, body = self.verified(row, "allow", authenticator=counting, cookie=cookie)
        self.assertEqual(status, 200, body)
        print("ok  PH-4: the counter rule (a counter that does not rise is refused)")

    def test_deny_needs_the_session_only(self):
        request, out, worker = self.ask()
        self.d.secure.overrides["serve"] = lambda d, ctx: (_ for _ in ()).throw(remote_secure._Fail("x"))
        status, _, body = self.post_answer(self.row(request), {"decision": "deny"})
        self.assertEqual((status, body["decision"]), (200, "deny"), body)
        self.decided(out, worker, Decision.DENY)
        # A HUD-only card too, limited or not.
        private, out, worker = self.private()
        self.assertEqual(self.post_answer(self.row(private), {"decision": "deny"})[0], 200)
        self.decided(out, worker, Decision.DENY)
        # Without the session, nothing.
        request, out, worker = self.ask()
        self.assertEqual(self.phone("POST", f"/m/api/inbox/{self.row(request)}/answer", {"decision": "deny"})[0], 401)
        self.assertTrue(self.pending(request))
        # A Deny carrying anything else is malformed.
        self.assertEqual(self.post_answer(self.row(request), {"decision": "deny", "assertion": {}})[0], 400)
        print("ok  Deny: the session only (any card, limited or not); 401 without the session")


# -- PH-7: Always shows its rule ------------------------------------------------------------


class Always(Phone):
    def test_always_shows_the_rule_and_writes_it_only_after_a_verified_answer(self):
        self.secure_ok()
        request, out, worker = self.ask(args={"command": "git status --short"})
        row = self.row(request)
        status, _, body = self.options(row, "always")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["rule"], {"tool": "run_command", "prefix": "git"})
        self.assertIn("'git'", body["rule_words"])
        self.assertEqual(body["resolution"], "approved-always")
        self.assertEqual(self.written(), [], "the dry run wrote nothing")
        credential = self.a.get(body["publicKey"])
        status, _, got = self.post_answer(row, {"decision": "always", "assertion": credential})
        self.assertEqual(status, 200, got)
        self.assertEqual(got["allowlisted"], body["rule"], "what was shown is what was written")
        self.assertEqual(self.written(), [body["rule"]])
        self.decided(out, worker, Decision.ALLOW)
        self.assertEqual(self.d.approvals.decisions[-1]["resolution"], "approved-always")
        # A compound command shows that no rule can be written, and writes none.
        request, out, worker = self.ask(args={"command": "git status && rm -rf build"})
        status, _, body = self.options(self.row(request), "always")
        self.assertEqual((status, body["rule"]), (200, None), body)
        self.assertTrue(body["resolution"].startswith("allow (not allowlistable"), body)
        status, _, got = self.post_answer(self.row(request), {"decision": "always",
                                                              "assertion": self.a.get(body["publicKey"])})
        self.assertEqual((status, got["allowlisted"]), (200, None), got)
        self.assertEqual(len(self.written()), 1)
        print("ok  Always: the exact rule first (a dry run), written only by the verified answer")

    def test_the_dry_run_matches_resolve(self):
        cases = [{"tool": "Bash", "args": {"command": "npm test"}},
                 {"tool": "Bash", "args": {"command": "a && b"}},
                 {"tool": "Bash", "args": {"command": "   "}},
                 {"tool": "WebFetch", "args": {"url": "https://example.com"}},
                 {"tool": "mcp__x__exec", "args": {"command": "ls"}},
                 {"tool": "Bash", "args": {"command": "ls"}, "allowlistable": False}]
        for case in cases:
            with self.subTest(case=case):
                said = []
                broker = PendingApprovals(timeout_s=5, on_resolve=lambda r, d, resolution: said.append(resolution))
                request = ApprovalRequest(command=case["args"].get("command"), **case)
                threading.Thread(target=broker.ask, args=(request,), daemon=True).start()
                eventually(lambda: broker.pending())
                preview = broker.always_rule(request.req_id)
                before = list(self.written())
                self.assertEqual(self.written(), before, "the dry run wrote nothing")
                entry = broker.resolve(request.req_id, Decision.ALLOW, always=True)
                self.assertEqual(entry, preview[0])
                self.assertEqual(said, [preview[1]], "the resolution it said it would record")
                if preview[0] is None:
                    self.assertEqual(self.written(), before)
                else:
                    self.assertIn(preview[0], self.written())
        with self.assertRaises(ValueError):
            PendingApprovals().always_rule("nope")
        print("ok  always_rule: the dry run is what resolve(always=True) writes, case by case")

    def test_always_is_refused_where_the_card_takes_none(self):
        self.secure_ok()
        request, _, _ = self.ask(allowlistable=False)
        self.assertEqual(self.options(self.row(request), "always")[2], {"error": remote_answer.NO_ALWAYS})
        private, _, _ = self.private()
        self.assertEqual(self.options(self.row(private), "always")[0], 409)
        self.assertEqual(self.written(), [])
        print("ok  Always: refused on a card that takes none (not allowlistable, HUD-only)")


# -- §4.1 and the HUD-only card ----------------------------------------------------------------


def _flip_loopback(case):
    """The listener's socket reports 0.0.0.0 (the check reads the socket)."""
    real = remote_secure.check_loopback
    case.d.secure.overrides["loopback"] = lambda d, ctx: real(
        SimpleNamespace(remote_auth=SimpleNamespace(server=FakeServer("0.0.0.0", d.remote_port)),
                        remote_port=d.remote_port), ctx)


def _flip_serve(case):
    good = json.loads(case.system.good()["serve"])
    good["AllowFunnel"] = {f"{HOST}:443": True}
    case.system.bad["serve"] = json.dumps(good)


def _flip_certificate(case):
    good = json.loads(case.system.good()["status"])
    good["CertDomains"] = ["other-box." + SUFFIX]
    case.system.bad["status"] = json.dumps(good)


def _flip_passkey(case):
    guard = patch.object(remote_auth, "REQUIRE_UV", False)
    guard.start()
    case.addCleanup(guard.stop)


def _flip_listeners(case):
    case.system.bad["windows"] = "127.0.0.1 wslrelay\r\n0.0.0.0 node\r\n" + remote_secure.END_OF_LISTENERS + "\r\n"


FLIPS = {"loopback": _flip_loopback, "serve": _flip_serve, "certificate": _flip_certificate,
         "passkey": _flip_passkey, "listeners": _flip_listeners}


class Secured(Phone):
    def test_a_hud_only_card_is_answered_under_41(self):
        self.secure_ok()
        request, out, worker = self.private()
        row = self.row(request)
        status, _, body = self.detail(row)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["private"] and body["secure"]["ok"])
        self.assertEqual(body["can"], {"deny": True, "allow": True, "always": False})
        self.assertEqual(body["card"]["browser"]["target"], PRIVATE, "owner_details, while pending")
        self.assertNotIn("code", body["card"])
        status, _, got = self.verified(row, "allow")
        self.assertEqual((status, got["authority"]), (200, "hud"), got)
        self.decided(out, worker, Decision.ALLOW)
        self.assertEqual(self.detail(row)[0], 409, "the detail only while pending")
        print("ok  §4.1 holds: a HUD-only card's detail, then its Allow, from a verified phone")

    def limited(self, name):
        self.secure_ok()
        ordinary, o_out, o_worker = self.ask()
        private, p_out, p_worker = self.private()
        # An assertion taken while every check held...
        early, _ = self.assertion(self.row(private), "allow")
        FLIPS[name](self)
        result = self.d.secure.check()
        self.assertEqual((result["ok"], result["failed"]), (False, name), result)
        # ...is refused once one fails: answer at the desk.
        status, _, body = self.post_answer(self.row(private), {"decision": "allow", "assertion": early})
        self.assertEqual((status, body["error"]), (409, remote_answer.AT_DESK), body)
        self.assertTrue(self.pending(private), "the card waits for the desk")
        status, _, got = self.detail(self.row(private))
        self.assertEqual((status, got["error"], got["secure"]["failed"]), (409, remote_answer.AT_DESK, name))
        self.assertNotIn(PRIVATE, json.dumps(got))
        self.assertEqual(self.options(self.row(private), "allow")[0], 409)
        self.assertNotIn(PRIVATE, json.dumps(self.phone("GET", "/m/api/inbox", cookie=self.cookie)[2]))
        # Discord's authority on the ordinary card: Allow yes, Always no.
        self.assertEqual(self.options(self.row(ordinary), "always")[0], 409)
        status, _, body = self.detail(self.row(ordinary))
        self.assertEqual((status, body["can"]), (200, {"deny": True, "allow": True, "always": False}), body)
        self.assertTrue(body["why"].startswith("limited: "), body["why"])
        status, _, got = self.verified(self.row(ordinary), "allow")
        self.assertEqual((status, got["authority"]), (200, "limited"), got)
        self.decided(o_out, o_worker, Decision.ALLOW)
        self.assertEqual(self.written(), [])
        # Shown at the desk and on the phone, in fixed words.
        status, _, me = self.phone("GET", "/m/api/me", cookie=self.cookie)
        self.assertEqual((me["secure"]["ok"], me["secure"]["failed"]), (False, name), me)
        host = f"127.0.0.1:{self.d.port}"
        value = json.loads(raw(self.d.port, "GET", "/status", [("Host", host)])[2])
        self.assertEqual(value["remote"]["secure"]["failed"], name)
        self.assertEqual(value["remote"]["secure"]["label"], remote_secure.LABELS[name])
        # Deny still goes.
        self.assertEqual(self.post_answer(self.row(private), {"decision": "deny"})[0], 200)
        self.decided(p_out, p_worker, Decision.DENY)

    def test_limited_loopback(self):
        self.limited("loopback")
        print("ok  §4.1 (1) the listener off loopback: HUD-only answer at the desk, no Always, Allow limited")

    def test_limited_serve(self):
        self.limited("serve")
        print("ok  §4.1 (2) Funnel on: HUD-only answer at the desk, no Always, Allow limited")

    def test_limited_certificate(self):
        self.limited("certificate")
        print("ok  §4.1 (3) no tailnet certificate for the name: HUD-only answer at the desk")

    def test_limited_passkey(self):
        self.limited("passkey")
        print("ok  §4.1 (4) a passkey without required verification: HUD-only answer at the desk")

    def test_limited_listeners(self):
        self.limited("listeners")
        print("ok  §4.1 (5) something else listens on the port on Windows: HUD-only answer at the desk")

    def test_a_failure_in_the_display_cache_is_seen_by_the_next_answer(self):
        self.secure_ok()
        self.assertTrue(self.d.secure.view()["ok"], "shown: secured")
        private, _, _ = self.private()
        early, _ = self.assertion(self.row(private), "allow")
        runs = self.d.secure.runs
        _flip_serve(self)               # nothing re-checked yet: the view still says ok
        self.assertTrue(self.d.secure.view()["ok"])
        status, _, body = self.post_answer(self.row(private), {"decision": "allow", "assertion": early})
        self.assertEqual((status, body["error"]), (409, remote_answer.AT_DESK))
        self.assertGreater(self.d.secure.runs, runs, "the answer ran the checks itself")
        self.assertEqual(self.d.secure.view()["failed"], "serve", "and the failure is shown at once")
        self.assertTrue(self.pending(private))
        # Fixed again: the next answer passes with no setting changed.
        self.system.bad.clear()
        self.assertEqual(self.verified(self.row(private), "allow")[0], 200)
        print("ok  §4.1: a failure after a cached pass is seen by the very next answer, and shown at once")

    def test_the_answers_own_host_and_origin_are_checked(self):
        self.secure_ok()
        request, _, _ = self.private()
        handler = rac.OwnerTest.handler(self, self.auth.server, "POST",
                                        [("Host", HOST), ("Origin", "https://evil.ts.net")])
        self.assertEqual(self.d.secure.check(handler=handler)["failed"], "certificate")
        handler = rac.OwnerTest.handler(self, self.auth.server, "GET", [("Host", HOST)])
        self.assertTrue(self.d.secure.check(handler=handler)["ok"], "a read without Origin")
        handler = rac.OwnerTest.handler(self, self.auth.server, "GET", [("Host", HOST), ("Origin", "https://x")])
        self.assertEqual(self.d.secure.check(handler=handler)["failed"], "certificate")
        # Case does not matter in a host name, as serve's own check reads it.
        handler = rac.OwnerTest.handler(self, self.auth.server, "POST",
                                        [("Host", HOST.upper()), ("Origin", "https://" + HOST.upper())])
        self.assertTrue(self.d.secure.check(handler=handler)["ok"])
        print("ok  §4.1 (3): the answer's own Host and Origin are the tailnet name's")


# -- the card the owner answers from ------------------------------------------------------------


class Cards(Phone):
    def test_the_full_card_not_the_read_views_cut(self):
        self.secure_ok()
        long = "echo " + "x" * 9000
        request, _, _ = self.ask(args={"command": long, "description": "say x", "timeout": 5})
        status, _, body = self.detail(self.row(request))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["command"], long, "the whole command")
        self.assertEqual(body["card"]["args"], {"command": long, "description": "say x", "timeout": 5})
        self.assertFalse(body["withheld"])
        self.assertEqual(set(body["card"]) | set(remote_api.APPROVAL_DETAIL_DROPPED), set(request.to_json()),
                         "the HUD's own projection, minus another surface's handles")
        print("ok  the card is the HUD's own projection, whole (no 4,000-character cut, the arguments shown)")

    def test_a_card_the_phone_scrub_would_change_refuses_allow_and_always(self):
        self.secure_ok()
        # What the desk's card keeps and the phone's scrub withholds (review
        # of PR #308): a private key's body in an argument, a labelled
        # password in the reason, a token in the attribution line.
        cases = [{"tool": "Write", "args": {"file_path": "/tmp/k", "content": PEM}},
                 {"args": {"command": "ls"}, "reason": "password: " + PASSWORD},
                 {"args": {"command": "ls"}, "origin": 'task 00000001 "' + TOKEN + '"'}]
        for case in cases:
            with self.subTest(case=list(case)):
                request, out, worker = self.ask(**case)
                row = self.row(request)
                status, _, body = self.detail(row)
                self.assertEqual(status, 200, body)
                self.assertTrue(body["withheld"])
                for secret in (TOKEN, PEM_BODY, PASSWORD):
                    self.assertNotIn(secret, json.dumps(body), "the phone is sent the scrubbed card")
                self.assertEqual(body["can"], {"deny": True, "allow": False, "always": False})
                self.assertEqual(body["why"], remote_answer.WITHHELD)
                for decision in ("allow", "always"):
                    self.assertEqual(self.options(row, decision)[2], {"error": remote_answer.WITHHELD})
                self.assertEqual(self.post_answer(row, {"decision": "deny"})[0], 200)
                self.decided(out, worker, Decision.DENY)
        print("ok  a card the phone's scrub would change: Allow and Always refused, Deny works")

    def test_structured_secrets_are_withheld(self):
        """Review of PR #313 (F1, MED): a label in a key and its value beside
        it, a nested Authorization header, a credential as a key. Each is
        withheld from the phone (Deny only), on the card and in the list."""
        self.secure_ok()
        bearer = "Bearer " + "abcDEF123456" * 3
        # Straight through phone_card, as the review measured it.
        for card, secret in (({"args": {"password": PASSWORD}}, PASSWORD),
                             ({"args": {"password": [PASSWORD]}}, PASSWORD),
                             ({"args": {"headers": {"Authorization": bearer}}}, bearer.split()[1]),
                             ({"args": {TOKEN: "x"}}, TOKEN),
                             # A number after a label: only the whole-card JSON reading sees it.
                             ({"args": {"password": NUMBER}}, str(NUMBER))):
            with self.subTest(card=list(card["args"])):
                sent, withheld = remote_answer.phone_card(card)
                self.assertTrue(withheld)
                self.assertNotIn(secret, json.dumps(sent))
        # Keys are scrubbed themselves too, wherever the phone's tree scrub runs
        # (P-3's task and inbox reads as well as the card).
        self.assertEqual(remote_api._scrub_tree({TOKEN: "x", "ok": {TOKEN: 1}}),
                         {remote_api._scrub(TOKEN): "x", "ok": {remote_api._scrub(TOKEN): 1}})
        self.assertNotIn(TOKEN, json.dumps(remote_api._scrub_tree({TOKEN: "x"})))
        # And through the routes, end to end.
        for args, secret in (({"headers": {"Authorization": bearer}}, bearer.split()[1]),
                             ({"spec": {"password": [PASSWORD]}}, PASSWORD),
                             ({"command": "ls", TOKEN: "x"}, TOKEN)):
            with self.subTest(args=list(args)):
                request, out, worker = self.ask(tool="mcp__x__exec", args=args, command="ls")
                row = self.row(request)
                status, _, body = self.detail(row)
                self.assertEqual((status, body["withheld"], body["can"]["allow"]), (200, True, False), body)
                self.assertNotIn(secret, json.dumps(body))
                self.assertNotIn(secret, json.dumps(self.phone("GET", "/m/api/inbox", cookie=self.cookie)[2]))
                self.assertEqual(self.options(row, "allow")[2], {"error": remote_answer.WITHHELD})
                self.assertEqual(self.post_answer(row, {"decision": "deny"})[0], 200)
                self.decided(out, worker, Decision.DENY)
        # Ordinary arguments stay as they are and answerable.
        request, _, _ = self.ask(args={"command": "ls -la", "description": "List files", "timeout": 5,
                                       "url": "https://example.com/a?b=c", "cwd": "/tmp"})
        status, _, body = self.detail(self.row(request))
        self.assertEqual((body["withheld"], body["can"]["allow"]), (False, True), body)
        print("ok  structured secrets (a labelled value, a nested Authorization header, a key) are withheld")

    def test_secret_in_an_argument_key(self):
        """The security review's case (PR #313): a token as an argument's key
        is withheld from the phone, and a verified Allow is refused."""
        self.secure_ok()
        request, out, worker = self.ask(tool="mcp__x__exec", args={"command": "ls", TOKEN: "x"}, command="ls")
        row = self.row(request)
        status, _, body = self.detail(row)
        self.assertEqual((status, body["withheld"]), (200, True))
        self.assertNotIn(TOKEN, json.dumps(body))
        status, _, got = self.post_answer(row, {"decision": "allow", "assertion": self.a.get(
            {"rpId": HOST, "challenge": remote_auth.b64url(os.urandom(32))})})
        self.assertNotEqual(status, 200)
        self.assertEqual(self.options(row, "allow")[0], 409)
        self.assertTrue(self.pending(request))
        print("ok  a credential as an argument's key: withheld, no Allow")

    def test_args_nested_past_redaction_depth(self):
        """The security review's case (PR #313): an argument nested past the
        card redaction's depth shows a placeholder nothing judged; the card
        is withheld (Deny only)."""
        self.secure_ok()
        deep = {"run": "rm -rf ~/" + TOKEN}
        for i in range(40):
            deep = {f"l{i}": deep}
        request, out, worker = self.ask(tool="mcp__x__exec", args={"spec": deep}, command=None)
        row = self.row(request)
        status, _, body = self.detail(row)
        self.assertIn(remote_api.TOO_DEEP, json.dumps(body["card"]))
        self.assertEqual((status, body["withheld"], body["can"]), (200, True, {"deny": True, "allow": False,
                                                                               "always": False}))
        self.assertEqual(self.options(row, "allow")[2], {"error": remote_answer.WITHHELD})
        self.assertEqual(self.post_answer(row, {"decision": "deny"})[0], 200)
        self.decided(out, worker, Decision.DENY)
        print("ok  an argument nested past the redaction's depth: withheld, Deny only")

    def test_private_detail_never_leaks(self):
        self.secure_ok()
        seen: list = []
        self.d.bus.observe(seen.append)
        records: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        keep = Keep(level=logging.DEBUG)
        root = logging.getLogger()
        level = root.level
        root.addHandler(keep)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, keep)
        self.addCleanup(root.setLevel, level)
        stream = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(stream.close)
        stream.sendall(f"GET /m/api/events HTTP/1.1\r\nHost: {HOST}\r\nCookie: {self.cookie}\r\n\r\n".encode())
        time.sleep(0.2)
        request, out, worker = self.private()
        row = self.row(request)
        listed = self.phone("GET", "/m/api/inbox", cookie=self.cookie)[2]
        status, _, body = self.detail(row)
        self.assertEqual(status, 200)
        self.assertIn(PRIVATE, json.dumps(body), "the control: the detail itself carries it")
        self.assertEqual(self.verified(row, "allow")[0], 200)
        self.decided(out, worker, Decision.ALLOW)
        time.sleep(1.2)
        stream.settimeout(0.5)
        streamed = b""
        try:
            while True:
                chunk = stream.recv(65536)
                if not chunk:
                    break
                streamed += chunk
        except (socket.timeout, TimeoutError):
            pass
        self.assertIn(b"inbox_changed", streamed, "the control: the stream ran")
        self.assertNotIn(PRIVATE.encode(), streamed, "/m/api/events")
        self.assertNotIn(PRIVATE, json.dumps(listed), "the inbox list")
        self.assertFalse([r for r in seen if PRIVATE in json.dumps(r, default=str)], "the bus")
        logged = "\n".join(r.getMessage() + repr(r.args) for r in records)
        self.assertNotIn(PRIVATE, logged, "a log line")
        # There is no push yet (P-6); the stream's shaper is what a push
        # would read, and it keeps ids only.
        for record in seen:
            shaped = remote_api.stream_record(record)
            self.assertNotIn(PRIVATE, json.dumps(shaped))
        print("ok  a HUD-only card's private detail: never in a log line, the bus, /m/api/events or the list")


# -- who may pass owner_verified --------------------------------------------------------------


class OwnerVerified(Phone):
    # Every assignment of `owner_verified` in jarvis/, by file: what follows
    # its `=` on that line (spaces removed). Exactly these and no others.
    ALLOWED = {
        "jarvis/v2/approvals.py": ["False):"],                         # resolve's default
        "jarvis/v2/daemon.py": [
            "False)->dict:",                                           # resolve_approval's default
            "owner_verified)",                                         # resolve_approval -> resolve
            'config.own_host_header(self.headers.get("Host",""),daemon.face_port))',  # POST /approvals (HUD)
        ],
        "jarvis/v2/inbox.py": [
            "owner_verified)",                                         # answer -> resolve_approval
            "owner_verified)",                                         # route (HUD) -> answer
            'config.own_host_header(handler.headers.get("Host",""),daemon.face_port)',  # the HUD route's test
        ],
        "jarvis/v2/remote_answer.py": [
            "owner_verified,via=\"phone\")",                            # _dispatch -> inbox.answer
            "False)",                                                  # questions and decisions
            "False)",                                                  # Deny
            "False)",                                                  # a proposal's withdraw (P-5)
            'bool(secure.get("ok"))',                                  # §4.1 after the assertion
            "owner_verified)",                                         # the verified answer
        ],
    }

    def test_only_the_hud_route_and_the_verified_phone_pass_it(self):
        found: dict[str, list[str]] = {}
        for path in sorted((ROOT / "jarvis").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(ROOT))
            uses = [m.group(1).replace(" ", "") for m in
                    re.finditer(r"(?<![`\w])owner_verified\s*(?::\s*bool\s*)?=(?!=)\s*(.*)$", text, re.M)]
            if uses:
                found[rel] = uses
            # Never a literal True in code (a docstring's `owner_verified=True`
            # in backticks is prose).
            self.assertIsNone(re.search(r"(?<!`)owner_verified\s*=\s*True", text), rel)
            # No resolve keeps the old `hud=` keyword.
            self.assertNotRegex(text, r"resolve(?:_approval)?\([^)]*\bhud\s*=", rel)
        self.assertEqual({rel: sorted(uses) for rel, uses in found.items()},
                         {rel: sorted(uses) for rel, uses in self.ALLOWED.items()})
        # The phone's True is the §4.1 result after the assertion, nothing else.
        source = inspect.getsource(remote_answer.answer)
        self.assertLess(source.index("answer_verify("), source.index("check(session=session, handler=handler)"))
        self.assertLess(source.index("check(session=session, handler=handler)"),
                        source.index('owner_verified = bool(secure.get("ok"))'))
        self.assertIn("projects.owner_only", inspect.getsource(inbox.route).replace("from .projects import owner_only",
                                                                                     "projects.owner_only"))
        print("ok  owner_verified: only the HUD's routes (own Host) and the phone after its assertion under §4.1")

    def test_discord_and_the_inbox_dispatch_cannot_satisfy_hud_only(self):
        request, out, worker = self.private()
        # Discord's call: resolve with no owner_verified.
        with self.assertRaises(ValueError):
            self.d.approvals.resolve(request.req_id, Decision.ALLOW)
        with self.assertRaises(ValueError):
            self.d.approvals.resolve(request.req_id, Decision.ALLOW, owner_verified="yes")
        # The inbox dispatch from anything but the HUD route, no assertion.
        from jarvis.v2.daemon import APIError
        with self.assertRaises(APIError):
            inbox.answer(self.d, self.row(request), {"decision": "allow"}, owner_verified=False, via="phone")
        # The API listener's inbox route: refused before anything (owner_only).
        status, _, _ = raw(self.d.port, "POST", f"/inbox/{self.row(request)}/answer",
                           [("Host", f"127.0.0.1:{self.d.port}"), ("Origin", f"http://127.0.0.1:{self.d.port}"),
                            ("Content-Type", "application/json")], b'{"decision": "allow"}')
        self.assertEqual(status, 403)
        # The phone with no assertion.
        self.assertEqual(self.post_answer(self.row(request), {"decision": "allow"})[0], 401)
        self.assertTrue(self.pending(request))
        # The HUD's own route is the control: it answers.
        host = f"127.0.0.1:{self.d.face_port}"
        status, _, _ = raw(self.d.face_port, "POST", f"/inbox/{self.row(request)}/answer",
                           [("Host", host), ("Origin", f"http://{host}"), ("Content-Type", "application/json")],
                           b'{"decision": "allow"}')
        self.assertEqual(status, 200)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  hud_only: Discord's resolve, the inbox dispatch without an assertion, the API listener, "
              "and the phone without a passkey are all refused; the HUD route answers")


# -- questions and decisions --------------------------------------------------------------------


class Recording(ic.Brain):
    def __init__(self, name):
        super().__init__(name)
        self.messages: list = []

    def send(self, handle, message):
        self.messages.append(message)
        return super().send(handle, message)


class Questions(Phone):
    def setUp(self):
        super().setUp()

    def test_a_chat_question_from_the_phone(self):
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        self.brain.ask = True
        self.d.send(thread.id, UserMessage("go"))
        eventually(lambda: any(r["kind"] == "question" for r in inbox.rows(self.d)), what="the question")
        (row,) = [r for r in inbox.rows(self.d) if r["kind"] == "question"]
        status, _, body = self.post_answer(row["id"], {"text": "pnpm"})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual([a[1] for a in self.brain.answers], [row["detail"]["req_id"]])
        eventually(lambda: any(r.get("kind") == "question_answered"
                               for r in self.d.stores.threads.read_log(thread.id)))
        (record,) = [r for r in self.d.stores.threads.read_log(thread.id) if r.get("kind") == "question_answered"]
        self.assertEqual(record["data"], {"req_id": row["detail"]["req_id"], "via": "phone"})
        self.assertEqual(self.post_answer(row["id"], {"text": "npm"})[0], 409, "once")
        print("ok  a chat question answered on the phone: the session only, its log record names the phone")

    def test_a_task_question_from_the_phone(self):
        task = self.d.stores.tasks.create(self.project.id, "add multiply")
        self.d.stores.tasks.save(task)
        task = self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        task.spec = Spec(goal="g", questions=[OpenQuestion("which db?", blocking=True, options=["pg"])])
        self.d.stores.tasks.save(task)
        status, _, body = self.post_answer(f"tq:{task.id}:0", {"text": "pg"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runner.calls, [("answer_question", task.id, 0, "pg")])
        print("ok  a task question answered on the phone (the task runner's own call; it takes no via)")

    def test_a_proposal_is_withdrawn_from_the_phone(self):
        # P-5 brought the withdraw to the phone (tests/v2/remote_write_check.py
        # holds the rest): the session only, the inbox's own call.
        task = self.d.stores.tasks.create(self.project.id, "add multiply")
        self.d.stores.tasks.save(task)
        self.d.stores.tasks.journal(task.id, "proposed_by_fastpath", brief="b", thread_id=None, turn_id=None,
                                    reply="", not_before=self.d.router.clock() + 60)
        status, _, body = self.post_answer(f"prop:{task.id}", {"withdraw": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runner.calls, [("cancel", task.id)])
        print("ok  a proposal's withdraw from the phone (P-5): the session only")

    def test_a_decision_from_the_phone_is_via_phone_with_no_desk_authority(self):
        brain = Recording(PN.FAST)
        self.d.providers[PN.FAST] = brain
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        store = self.d.inbox.decisions
        record = store.create(thread_id=thread.id, project_id=self.project.id, task_id=None, origin="",
                              question="Which store?", options=["sqlite", "postgres"], default=None, why="w")
        status, _, body = self.phone("POST", f"/m/api/inbox/decisions/{record['id']}", {"choice": 1},
                                     cookie=self.cookie)
        self.assertEqual((status, body["delivery"]), (200, "started"), body)
        eventually(lambda: brain.messages, what="the answer's turn")
        (message,) = brain.messages
        self.assertEqual((message.origin, message.via, message.desk), ("owner", "phone", False))
        self.assertEqual(message.text, inbox.decision_message(store.get(record["id"])), "B1's framing")
        self.assertFalse(desk.desk_message(Brief(Role.CHAT, "/x"), message), "no desk authority")
        eventually(lambda: any(r.get("kind") == "user" for r in self.d.stores.threads.read_log(thread.id)))
        (user,) = [r for r in self.d.stores.threads.read_log(thread.id) if r.get("kind") == "user"]
        self.assertEqual(user["data"]["via"], "phone")
        rows = chat_reads.projected(self.d.stores.threads.read_log(thread.id), PN.FAST)
        self.assertIn("via the phone's inbox (not typed in this chat)", [m for _, r in rows for m in r.marks])
        self.assertEqual(self.phone("POST", f"/m/api/inbox/decisions/{record['id']}", {"choice": 0},
                                    cookie=self.cookie)[0], 409, "once")
        # And through the generic answer route, the same function.
        second = store.create(thread_id=thread.id, project_id=self.project.id, task_id=None, origin="",
                              question="Which db?", options=["a", "b"], default=None, why="w")
        status, _, body = self.post_answer(f"dec:{second['id']}", {"text": "neither"})
        self.assertEqual(status, 200, body)
        print("ok  a decision answered on the phone: UserMessage(origin owner, via phone, desk False), B1's frame")

    def test_the_mirror_and_the_bypass_exclusion_know_phone(self):
        from jarvis.v2.discord import mirror
        from jarvis.v2.providers import claude
        source = inspect.getsource(claude)
        self.assertIn('not in ("inbox", "phone")', source, "owner_turn excludes a phone answer (Bypass)")
        self.assertIn('via in ("inbox", "phone")', inspect.getsource(mirror))
        print("ok  Bypass's owner turn and the Discord mirror treat a phone answer as an inbox one")


# -- PH-10 ----------------------------------------------------------------------------------------


class Timeouts(Phone):
    def test_600_s_while_a_phone_was_active_in_the_last_24_h(self):
        broker = self.d.approvals
        self.assertEqual(broker.timeout_s, approvals_mod.LOCAL_TIMEOUT_S)
        self.assertEqual(broker.wait_s(), approvals_mod.REMOTE_TIMEOUT_S, "a session was just seen")
        request, _, _ = self.ask()
        self.assertEqual(request.timeout_s, approvals_mod.REMOTE_TIMEOUT_S, "the card says so")
        private, _, _ = self.private()
        self.assertLessEqual(private.timeout_s, approvals_mod.LOCAL_TIMEOUT_S, "a HUD-only card keeps its cap")
        self.wall.now += remote_auth.ACTIVE_S + 1
        self.assertEqual(broker.wait_s(), approvals_mod.LOCAL_TIMEOUT_S, "a day without the phone")
        # Seen again (a read), and it is back.
        self.assertEqual(self.me(self.cookie), 401, "the session itself idled out meanwhile")
        cookie = self.signed_in(self.a)
        self.assertEqual(self.me(cookie), 200)
        self.assertEqual(broker.wait_s(), approvals_mod.REMOTE_TIMEOUT_S)
        # A broker given its own wait keeps it; Discord's is remote anyway.
        own = PendingApprovals(timeout_s=0.2)
        own.phone_active = lambda: True
        self.assertEqual(own.wait_s(), 0.2)
        remote = PendingApprovals(remote=True)
        remote.phone_active = lambda: False
        self.assertEqual(remote.wait_s(), approvals_mod.REMOTE_TIMEOUT_S)
        broken = PendingApprovals()
        broken.phone_active = lambda: 1 / 0
        self.assertEqual(broken.wait_s(), approvals_mod.LOCAL_TIMEOUT_S, "an error is no phone")
        print("ok  PH-10: 600 s while a phone session was seen in 24 h; 120 s otherwise; HUD-only cards capped")

    def test_no_phone_no_long_wait(self):
        auth = remote_auth.RemoteAuth(self.root / "fresh", HOST)
        self.assertFalse(auth.recently_active())
        print("ok  PH-10: never paired, never active")

    def test_a_revoked_phone_is_not_a_phone(self):
        """Review of PR #313 (F5): with every passkey revoked, the remote wait
        ends at once, not 24 h later."""
        self.assertTrue(self.auth.recently_active())
        self.assertEqual(self.d.approvals.wait_s(), approvals_mod.REMOTE_TIMEOUT_S)
        self.auth.revoke(self.auth.status()["passkeys"][0]["key"])
        self.assertFalse(self.auth.recently_active())
        self.assertEqual(self.d.approvals.wait_s(), approvals_mod.LOCAL_TIMEOUT_S)
        print("ok  PH-10: only while a passkey is paired (a revoke ends the long wait)")

    def test_a_private_card_keeps_its_120_s_cap(self):
        """Review of PR #313 (F6): a HUD-only card with no wait of its own
        still waits 120 s while the phone makes the broker wait 600 s."""
        self.assertEqual(self.d.approvals.wait_s(), approvals_mod.REMOTE_TIMEOUT_S)
        request, _, _ = self.ask(tool="hud_browser_act", args={}, command=None, layer="browser-gate",
                                 hud_only=True, browser={"kind": "x"})
        self.assertIsNone(request.max_wait_s)
        self.assertEqual(request.timeout_s, approvals_mod.LOCAL_TIMEOUT_S)
        print("ok  PH-10: a HUD-only card keeps its 120 s cap with no wait of its own")


# -- the boundary ---------------------------------------------------------------------------------


class Boundary(Phone):
    def test_is_owner_stays_false_on_the_remote_listener(self):
        handler = rac.OwnerTest.handler(self, self.auth.server, "POST",
                                        [("Host", HOST), ("Origin", ORIGIN), ("Cookie", self.cookie)])
        self.assertTrue(remote_auth.is_remote_owner(handler))
        self.assertFalse(projects.is_owner(handler, self.d))
        for module in (remote_answer, remote_secure):
            source = inspect.getsource(module)
            for word in ("is_owner(", "owner_only(", "owner_json("):
                self.assertNotIn(word, source, (module.__name__, word))
        print("ok  is_owner stays false on the remote listener; the answer route never asks it")

    def test_agent_path_deny_covers_the_new_routes(self):
        port = config.REMOTE_PORT
        for path in ("/m/api/inbox/appr:x/answer", "/m/api/inbox/appr:x/answer/options",
                     "/m/api/inbox/appr:x/detail", "/m/api/inbox/decisions/x"):
            for line in (f"curl -X POST http://127.0.0.1:{port}{path}", f"curl http://localhost:{port}{path}",
                         f"curl https://box.tail1234.ts.net{path}", f"wget -qO- http://127.0.0.1:8402{path}"):
                verdict = rules.decide(line)
                self.assertEqual((verdict.decision, verdict.reason), (rules.DENY, rules.REMOTE_DENIED), line)
        print("ok  the agent-path DENY covers the answer routes, on loopback and the ts.net name")

    def test_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        for rel in ("jarvis/v2/remote_answer.py", "jarvis/v2/remote_secure.py", "jarvis/v2/approvals.py"):
            self.assertIn(rel, SELF_PROTECTED)
        print("ok  remote_answer.py, remote_secure.py and approvals.py are SELF_PROTECTED")

    def test_methods_and_shapes(self):
        request, _, _ = self.ask()
        row = self.row(request)
        self.assertEqual(self.phone("POST", f"/m/api/inbox/{row}/detail", {}, cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("GET", f"/m/api/inbox/{row}/answer/options", cookie=self.cookie)[0], 405)
        self.assertEqual(self.options(row, "deny")[0], 400)
        self.assertEqual(self.options(row, "ALLOW")[0], 400)
        self.assertEqual(self.post_answer(row, {"decision": "maybe"})[0], 400)
        self.assertEqual(self.post_answer("missed:x", {"decision": "deny"})[0], 400)
        self.assertEqual(self.post_answer("appr:nope", {"decision": "deny"})[0], 409)
        self.assertEqual(self.detail("q:x:y")[0], 400)
        self.assertEqual(self.phone("POST", f"/m/api/inbox/{row}/answer", {"decision": "deny"},
                                    cookie=self.cookie, origin="https://evil.ts.net")[0], 403)
        print("ok  methods and shapes: 405, 400 for a bad decision or kind, 409 when gone, 403 without the Origin")

    def test_the_answer_challenge_pool_is_per_session(self):
        self.secure_ok()
        cap = remote_auth.MAX_ANSWER_CHALLENGES_PER_SESSION
        cards = [self.ask(args={"command": f"echo {i}"})[0] for i in range(cap + 1)]
        for card in cards[:cap]:
            self.assertEqual(self.options(self.row(card), "allow")[0], 200)
        status, _, body = self.options(self.row(cards[cap]), "allow")
        self.assertEqual(status, 429, body)
        # Another session is never refused for this one's abandoned prompts.
        other = self.pair()
        other_cookie = self.signed_in(other)
        self.assertEqual(self.options(self.row(cards[cap]), "allow", cookie=other_cookie)[0], 200)
        self.assertEqual(self.phone("POST", "/m/api/login/options", {})[0], 200, "sign-in is not crowded out")
        # The same session asking again for the same card and answer replaces
        # its earlier challenge: never a 429, and the old one is spent.
        old, _ = self.assertion(self.row(cards[0]), "allow")
        for _ in range(cap * 3):
            self.assertEqual(self.options(self.row(cards[0]), "allow")[0], 200)
        status, _, body = self.post_answer(self.row(cards[0]), {"decision": "allow", "assertion": old})
        self.assertEqual(status, 409, body)
        credential, _ = self.assertion(self.row(cards[0]), "allow")
        self.assertEqual(self.post_answer(self.row(cards[0]), {"decision": "allow", "assertion": credential})[0], 200)
        print("ok  per-answer challenges: 4 per session (429 past it), never across sessions; a repeat replaces")

    def test_pool_exhaustion_by_another_session(self):
        """The security review's case (PR #313): another signed-in session
        asking again and again, and one session's abandoned prompts, never
        lock the owner's session out."""
        self.secure_ok()
        request, _, _ = self.ask()
        row = self.row(request)
        other = self.pair()
        other_cookie = self.signed_in(other)
        for _ in range(16):
            self.assertEqual(self.options(row, "allow", cookie=other_cookie)[0], 200)
        self.assertEqual(self.options(row, "allow")[0], 200, "the owner's own session")
        for _ in range(16):
            self.options(row, "allow")
        self.assertEqual(self.options(row, "allow", cookie=other_cookie)[0], 200)
        print("ok  per-answer challenges: another session's prompts never 429 this one")


# -- the monitor ----------------------------------------------------------------------------------


class Monitor(Phone):
    def test_every_check_passes_on_the_machines_own_shapes(self):
        result = self.d.secure.check()
        self.assertEqual((result["ok"], result["failed"], result["failures"]), (True, None, []), result)
        self.assertEqual(sorted(set(self.system.calls)), ["serve", "ss", "status", "windows"])
        print("ok  monitor: all five pass on the shapes this PC gave (serve, status, ss, Get-NetTCPConnection)")

    def failing(self, name, why):
        result = self.d.secure.check()
        self.assertEqual((result["ok"], result["failed"]), (False, name), result)
        self.assertIn(why, result["reason"])
        return result

    def test_loopback(self):
        _flip_loopback(self)
        self.failing("loopback", "beyond loopback")
        self.d.secure.overrides.clear()
        real = self.auth.server
        self.auth.server = None
        self.addCleanup(setattr, self.auth, "server", real)
        self.failing("loopback", "not running")
        print("ok  monitor (1): a socket off loopback, or no listener, fails")

    def test_serve(self):
        good = json.loads(self.system.good()["serve"])
        cases = {
            "Funnel is on": dict(good, AllowFunnel={f"{HOST}:443": True}),
            "more than one handler": dict(good, Web={f"{HOST}:443": {"Handlers": {
                "/": {"Proxy": f"http://127.0.0.1:{self.port}"}, "/x": {"Proxy": "http://127.0.0.1:9"}}}}),
            "points elsewhere": dict(good, Web={f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}),
            "not a proxy": dict(good, Web={f"{HOST}:443": {"Handlers": {"/": {"Path": "/srv"}}}}),
            "more than HTTPS 443": dict(good, TCP={"443": {"HTTPS": True}, "22": {"TCPForward": "127.0.0.1:22"}}),
            "another kind": dict(good, Services={"svc:x": {}}),
            "no handler": dict(good, Web={}),
        }
        for why, value in cases.items():
            with self.subTest(why=why):
                self.system.bad["serve"] = json.dumps(value)
                self.failing("serve", why)
        for bad, why in (("not json", "not JSON"), (remote_secure.ReadFailed("failed"), "could not be read")):
            self.system.bad["serve"] = bad
            self.failing("serve", why)
        print("ok  monitor (2): Funnel, a second handler, another target or kind, extra TCP, unreadable: fail")

    def test_certificate(self):
        good = json.loads(self.system.good()["status"])
        cases = {"not running": dict(good, BackendState="Stopped"),
                 "this machine's tailnet name": dict(good, Self={"DNSName": "other." + SUFFIX + "."}),
                 "under the tailnet": dict(good, MagicDNSSuffix="example.com"),
                 "issues no certificate": dict(good, CertDomains=[])}
        for why, value in cases.items():
            with self.subTest(why=why):
                self.system.bad["status"] = json.dumps(value)
                self.failing("certificate", why)
        self.system.bad.clear()
        serve = json.loads(self.system.good()["serve"])
        serve["TCP"] = {"443": {"HTTP": True}}
        self.system.bad["serve"] = json.dumps(serve)
        result = self.d.secure.check()
        self.assertEqual(result["failures"][:2], ["serve", "certificate"], result)
        print("ok  monitor (3): not running, another name, no ts.net suffix, no certificate, no HTTPS: fail")

    def test_passkey(self):
        _flip_passkey(self)
        self.failing("passkey", "user verification")
        print("ok  monitor (4): a passkey without required verification fails")

    def test_passkey_none_paired(self):
        key = self.auth.status()["passkeys"][0]["key"]
        self.auth.revoke(key)
        self.failing("passkey", "no passkey is paired")
        print("ok  monitor (4): no paired passkey fails")

    def test_listeners(self):
        port = self.port
        good = self.system.good()["ss"]
        ino = os.fstat(self.auth.server.socket.fileno()).st_ino
        cases = {
            "more than one listener": good + f"LISTEN 0 5 0.0.0.0:{port} 0.0.0.0:* ino:1 sk:2\n",
            "not on loopback": f"LISTEN 0 5 0.0.0.0:{port} 0.0.0.0:* ino:{ino} sk:2\n",
            "not this daemon's": f"LISTEN 0 5 127.0.0.1:{port} 0.0.0.0:* ino:1 sk:2\n",
            "no listener": "",
        }
        for why, value in cases.items():
            with self.subTest(wsl=why):
                self.system.bad["ss"] = value
                self.failing("listeners", why)
        self.system.bad.clear()
        end = remote_secure.END_OF_LISTENERS + "\r\n"
        for value in ("0.0.0.0 wslrelay\r\n", "127.0.0.1 node\r\n", "127.0.0.1 wslrelay\r\n::1 python\r\n",
                      "garbled\r\n"):
            with self.subTest(windows=value):
                self.system.bad["windows"] = value + end
                self.failing("listeners", "on Windows")
        # A read that did not finish (no end line), or the end line not last.
        for value in ("", "127.0.0.1 wslrelay\r\n", end + "0.0.0.0 node\r\n"):
            with self.subTest(unfinished=value):
                self.system.bad["windows"] = value
                self.failing("listeners", "could not be read" if not value.startswith(end) else "no end line")
        # Nothing listening on Windows (an unused port's real answer: the end line alone) passes.
        self.system.bad["windows"] = end
        self.assertTrue(self.d.secure.check()["ok"], "nothing on Windows is not something else")
        print("ok  monitor (5): a second, non-loopback or foreign WSL listener, or anything but wslrelay on Windows")

    def test_a_command_that_times_out_is_limited(self):
        with patch.object(remote_secure, "PROBE_TIMEOUT_S", 0.3):
            start = time.monotonic()
            self.d.secure.run = lambda argv, timeout: remote_secure.run_command(["sleep", "5"], timeout)
            result = self.d.secure.check()
            took = time.monotonic() - start
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["reason"])
        self.assertLess(took, 3.0, "bounded: the reads run at once and each is cut")
        self.d.secure.run = lambda argv, timeout: remote_secure.run_command(["false"], timeout)
        self.assertIn("failed", self.d.secure.check()["reason"])
        print(f"ok  monitor: a read that times out is limited, within its bound ({took:.2f} s)")

    def test_not_attached_is_limited_and_reads_nothing(self):
        monitor = remote_secure.SecureMonitor(self.d)
        result = monitor.check()
        self.assertEqual((result["ok"], result["failed"]), (False, "serve"))
        self.assertIn("not attached", result["reason"])
        self.assertEqual(monitor.view()["failed"], "serve")
        fresh = remote_secure.SecureMonitor(self.d)
        self.assertEqual(fresh.view()["failed"], "pending", "shown as not checked until it runs")
        print("ok  monitor: an embedded or test daemon reads nothing and is limited")

    def test_the_schedule(self):
        monitor = remote_secure.SecureMonitor(self.d, run=self.system)
        with patch.object(remote_secure, "INTERVAL_S", 0.05):
            monitor.start()
            eventually(lambda: monitor.runs >= 3, what="checks every interval")
            monitor.stop()
        runs = monitor.runs
        time.sleep(0.2)
        self.assertEqual(monitor.runs, runs, "stopped")
        self.assertTrue(monitor.view()["ok"])
        self.assertIn("remote_secure", inspect.getsource(Daemon.__init__))
        self.assertIn("secure.start()", inspect.getsource(Daemon.start))
        self.assertIn("secure.stop()", inspect.getsource(Daemon.stop))
        from jarvis.v2 import daemon as daemon_mod
        self.assertIn("daemon.secure.attach_system()", inspect.getsource(daemon_mod.main))
        print("ok  monitor: at start and every interval, stopped with the daemon; attached by main() only")

    def test_the_reads_are_fixed_and_read_only(self):
        argvs = remote_secure.reads(8408)
        self.assertEqual(argvs["serve"][1:], ["serve", "status", "--json"])
        self.assertEqual(argvs["status"][1:], ["status", "--json"])
        # `ss` by full path, never through PATH (review of PR #313).
        self.assertEqual(argvs["ss"][:7], ["/usr/bin/ss", "-H", "-l", "-t", "-n", "-e", "sport"])
        self.assertTrue(os.path.isfile(remote_secure.SS) and os.access(remote_secure.SS, os.X_OK))
        self.assertTrue(all(argv[0].startswith("/") for argv in argvs.values()), argvs)
        script = argvs["windows"][-1]
        self.assertIn("Get-NetTCPConnection -State Listen -LocalPort 8408", script)
        self.assertIn("-ne 'ObjectNotFound') { throw }", script, "only 'no such connection' is caught")
        self.assertTrue(script.endswith(f"'{remote_secure.END_OF_LISTENERS}'"))
        for word in ("Set-", "Remove-", "Stop-", "New-", "Invoke-", "set ", "reset", "off", "funnel"):
            for argv in argvs.values():
                self.assertNotIn(word, " ".join(argv[1:]), (word, argv))
        print("ok  monitor: four fixed argvs, no shell, nothing that writes")


if __name__ == "__main__":
    unittest.main(verbosity=1)
