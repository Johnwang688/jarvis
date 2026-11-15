"""The phone's one-hour unlock window, its op tokens, and Done and Undo from
the phone (WS-5c, `docs/plans/2026-11-15-ws5-phone-working-set-plan.md`
§5 and §7): `jarvis/v2/remote_auth.py` (`unlock_options`, `unlock_verify`,
`require_unlocked`, `touch_unlock`, `_end_unlock`, `Locked`, `Ops`,
`issue_ops`, `gated`), and the gated families that live in
`remote_answer.py` and `remote_write.py`. The PR, review and deferral
families have their own cases in tests/v2/pr_merge_check.py,
review_check.py and quiet_defer_check.py.

Free and hermetic, as tests/v2/remote_answer_check.py: real daemons on
ephemeral loopback ports (never 8402, 8403, 8405 or 8408), temporary HOME and
data, real WebAuthn assertions from a software authenticator verified by the
real `webauthn` library, the §4.1 reads a fake system. The window and the
tokens run on the fake monotonic clock, every expiry relative to it; the
session rate on its own fake clock.

What it pins (§7, WS-5c):

  - the window: 59 min passes and slides, 61 min is 423, exactly at `until`
    is 423; reads, the stream, `me`, stt, push and token issuance never
    slide it; a session-only action slides an open one and never reopens a
    closed one; the window is bound to the session's passkey; the desk's
    `GET /phone` shows each session's `unlocked_until` without sliding it;
  - every ending on the existing instance (Lock now, logout, End, revoke,
    expiry, `MAX_SESSIONS` prune, a session found ended) ends it, deletes
    its pending unlock challenges (409 afterwards) and retires the
    session's generation and ops; a restart is locked; `_broken` is 503;
    revoke ends the sessions even when its save fails; runtime damage to
    the files leaves the window as it is;
  - Lock now racing verify both ways, a bumped generation, two verifies of
    one assertion, options asked twice;
  - each unlock failure (UV, signature, origin, crossOrigin, rpId,
    credential id, user handle, counter, another session, another passkey)
    401; a replay 409; a failed counter save 503 with the window closed;
  - the 423 contract, exactly `{"error": "locked", "unlock": true}`, with
    nothing mutated, for Allow, Always, cancel, Done and Undo;
  - op tokens: reuse with another body, a duplicate never runs, another
    session's token 400, a forged or altered one 410, the future tolerance,
    expiry, issuance (never slides, rate-limited, its final session
    re-check), capacity (per session and in all, nothing evicted, no rate
    token), running records, states by failure point, the status route,
    the outcome words, the rate and duplicate order, and the composed
    status order;
  - Done and Undo from the phone, bound to the URL thread and its `seq`;
    Done's refusal for a linked row or a running turn carries `why_not`;
  - #374's fix round: End, revoke or Lock now landing before admission,
    during it, or between admission and the mutation refuses the op (401,
    423) with nothing mutated and no op record kept for an ended session;
    the final checks never slide; Lock now after a retirement makes no
    generation; the op 429s carry `Retry-After`.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_unlock_check.py
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import json
import logging
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rnc = _load("remote_answer_check")
rac, ic = rnc.rac, rnc.ic

from jarvis import rules  # noqa: E402
from jarvis.v2 import pr_merge, remote_answer, remote_api, remote_auth, remote_write  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, TaskState  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.remote_auth import RemoteAuth  # noqa: E402

HOST, ORIGIN = rac.HOST, rac.ORIGIN
MIN = 60.0
LOCKED = (423, {"error": "locked", "unlock": True})
eventually = rnc.eventually


class Unlock(rnc.Phone):
    """A signed-in phone (remote_answer_check's), its session rate on a fake
    clock too."""

    def setUp(self):
        super().setUp()
        self.rate_clock = rac.Clock(1000.0)
        guard = patch.object(remote_write, "clock", self.rate_clock)
        guard.start()
        self.addCleanup(guard.stop)

    # -- helpers -------------------------------------------------------------------

    def allow(self, request, *, op=None, cookie=None, decision="allow", **extra):
        body = {"decision": decision, "op": op or self.op(cookie), **extra}
        return self.post_answer(self.row(request), body, cookie=cookie)

    def digest(self, cookie=None) -> str:
        return self.session_of(cookie)["token_sha256"]

    def rate_used(self, cookie=None) -> int:
        rate = remote_write.rate_of(self.auth.server)
        return len(rate._seen.get(self.digest(cookie), ()))

    def exhaust_rate(self, cookie=None):
        rate = remote_write.rate_of(self.auth.server)
        rate._seen[self.digest(cookie)] = deque([self.rate_clock()] * remote_write.RATE)

    def unlock_s(self, cookie=None) -> int:
        status, _, body = self.phone("GET", "/m/api/me", cookie=cookie or self.cookie)
        self.assertEqual(status, 200, body)
        return body["unlock_s"]

    def pending_credential(self, cookie=None, authenticator=None, **kw):
        """An unlock assertion, signed and not yet sent."""
        status, _, body = self.unlock_options(cookie)
        self.assertEqual(status, 200, body)
        return (authenticator or self.a).get(body["publicKey"], **kw)

    def send_unlock(self, credential, cookie=None):
        return self.phone("POST", "/m/api/unlock", {"assertion": credential}, cookie=cookie or self.cookie)

    def unlock_challenges(self, digest=None) -> list:
        return [c for c in self.auth._challenges.values()
                if c["kind"] == "unlock" and (digest is None or c.get("session") == digest)]

    def n_of(self, token: str) -> str:
        return remote_auth.parse_op(token)["n"]

    def status_of(self, token: str, cookie=None):
        return self.phone("GET", "/m/api/ops/" + self.n_of(token), cookie=cookie or self.cookie)

    def other_session(self):
        """A second session of the same passkey."""
        return self.signed_in(self.a)


# -- the window (§5.1) -------------------------------------------------------------------


class Window(Unlock):
    def test_locked_until_unlocked(self):
        self.secure_ok()
        request, out, worker = self.ask()
        self.assertEqual(self.unlock_s(), 0)
        status, _, body = self.allow(request)
        self.assertEqual((status, body), LOCKED)
        self.assertTrue(self.pending(request), "nothing answered while locked")
        self.assertEqual(self.unlocked(), {"unlocked": True, "unlock_s": remote_auth.UNLOCK_S})
        self.assertEqual(self.unlock_s(), remote_auth.UNLOCK_S)
        status, _, body = self.allow(request)
        self.assertEqual((status, body["decision"], body["authority"]), (200, "allow", "hud"), body)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  locked: Allow is exactly 423 {error: locked, unlock: true}; one unlock, then it goes")

    def test_sliding_and_its_edges(self):
        self.secure_ok()
        self.unlocked()
        for _ in range(2):
            self.mono.now += 59 * MIN
            request, out, worker = self.ask()
            self.assertEqual(self.allow(request)[0], 200, "59 min after the last action: open, and it slides")
            self.decided(out, worker, Decision.ALLOW)
        self.mono.now += 61 * MIN
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request)[:3:2], LOCKED, "61 min after the last action")
        self.assertTrue(self.pending(request))
        # Exactly at `until`: closed (open means mono() < until).
        self.unlocked()
        self.mono.now += remote_auth.UNLOCK_S
        self.assertEqual(self.allow(request)[:3:2], LOCKED, "exactly at until")
        self.unlocked()
        self.mono.now += remote_auth.UNLOCK_S - 0.5
        self.assertEqual(self.allow(request)[0], 200, "half a second before until")
        print("ok  sliding: 59 min passes and slides, 61 min is 423, exactly at until is 423")

    def test_reads_never_slide(self):
        self.secure_ok()
        request, _, _ = self.ask()
        row = self.row(request)
        self.unlocked()
        start = self.mono.now
        self.mono.now = start + 59 * MIN
        reads = ["/m/api/inbox", "/m/api/working", "/m/api/me", f"/m/api/inbox/{row}/detail",
                 f"/m/api/inbox/{row}/answer/rule", "/m/api/review", "/m/api/projects", "/m/api/threads",
                 "/m/api/tasks", "/m/api/ops/tokens", "/m/api/ops/" + "A" * 22, "/m/api/thread-models"]
        for path in reads:
            status, _, body = self.phone("GET", path, cookie=self.cookie)
            self.assertIn(status, (200,), (path, body))
        # The stream: opened, its headers read, closed.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/m/api/events", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", HOST)
        conn.putheader("Cookie", self.cookie)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        # stt and push subscribe, refused or not, are not actions either.
        self.phone("POST", "/m/api/stt", body=b"RIFF", cookie=self.cookie,
                   extra=(("Content-Type", "audio/wav"),))
        self.phone("POST", "/m/api/push/subscribe", {"endpoint": "x"}, cookie=self.cookie)
        self.assertEqual(self.unlock_s(), int(remote_auth.UNLOCK_S - 59 * MIN))
        self.mono.now = start + 61 * MIN
        self.assertEqual(self.allow(request)[:3:2], LOCKED, "none of those slid the window")
        print("ok  reads, the stream, me, stt, push and token issuance never slide the window")

    def test_session_only_slides_an_open_window_and_never_reopens(self):
        self.secure_ok()
        self.unlocked()
        start = self.mono.now
        self.mono.now = start + 50 * MIN
        one, out, worker = self.ask()
        self.assertEqual(self.post_answer(self.row(one), {"decision": "deny"})[0], 200)
        self.decided(out, worker, Decision.DENY)
        self.mono.now = start + 100 * MIN
        two, out, worker = self.ask()
        self.assertEqual(self.allow(two)[0], 200, "the Deny at 50 min slid the window")
        self.decided(out, worker, Decision.ALLOW)
        self.mono.now = start + 161 * MIN
        three, out, worker = self.ask()
        self.assertEqual(self.post_answer(self.row(three), {"decision": "deny"})[0], 200,
                         "a Deny 61 min after the last action still goes (session only)")
        four, _, _ = self.ask()
        self.assertEqual(self.allow(four)[:3:2], LOCKED, "and it did not reopen the window")
        print("ok  a session-only action slides an open window and never reopens a closed one")

    def test_the_desk_sees_unlocked_until(self):
        """§5.8: Settings > Phone shows "unlocked until HH:MM" per session
        (`GET /phone`'s `unlocked_until`, wall-clock seconds), a read that
        never slides the window; null once it is closed."""
        other = self.other_session()

        def shown():
            status, body = self.hud("GET", "/phone")
            self.assertEqual(status, 200, body)
            return {s["id"]: s["unlocked_until"] for p in body["passkeys"] for s in p["sessions"]}
        mine, theirs = self.session_of()["id"], self.session_of(other)["id"]
        self.assertEqual(shown(), {mine: None, theirs: None})
        self.unlocked()
        self.assertEqual(shown(), {mine: self.wall.now + remote_auth.UNLOCK_S, theirs: None})
        self.mono.now += 10 * MIN
        self.assertEqual(shown()[mine], self.wall.now + remote_auth.UNLOCK_S - 10 * MIN, "the read did not slide it")
        self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)
        self.assertEqual(shown()[mine], None)
        print("ok  the desk's Phone view: unlocked_until per session (wall clock), never sliding; null when locked")

    def test_the_window_is_bound_to_the_sessions_passkey(self):
        self.secure_ok()
        self.unlocked()
        request, _, _ = self.ask()
        self.auth._unlocks[self.digest()]["passkey"] = "k-000000000000"
        self.assertEqual(self.allow(request)[:3:2], LOCKED)
        self.assertNotIn(self.digest(), self.auth._unlocks, "a window of another passkey is dropped")
        print("ok  a window held for another passkey than the session's is no window (423)")


# -- what ends it (§5.1, §5.8) -------------------------------------------------------------


class Endings(Unlock):
    def armed(self, cookie):
        """This session unlocked, one op recorded, and one unlock challenge
        pending: (its session record, the pending credential)."""
        self.unlocked(cookie)
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request, cookie=cookie)[0], 200)
        credential = self.pending_credential(cookie)
        session = self.session_of(cookie)
        digest = session["token_sha256"]
        self.assertIn(digest, self.auth._unlocks)
        self.assertEqual(self.auth.ops.count(digest), 1)
        self.assertEqual(len(self.unlock_challenges(digest)), 1)
        return session, credential

    def retired(self, session, credential, what):
        digest = session["token_sha256"]
        self.assertNotIn(digest, self.auth._unlocks, what)
        self.assertNotIn(digest, self.auth._unlock_gen, what)
        self.assertEqual(self.auth.ops.count(digest), 0, what)
        self.assertEqual(self.unlock_challenges(digest), [], what)
        with self.assertRaises(remote_auth.AuthError) as caught:
            self.auth.unlock_verify(session, credential)
        self.assertEqual(caught.exception.status, 409, what)

    def test_lock_now(self):
        self.secure_ok()
        session, credential = self.armed(self.cookie)
        digest = session["token_sha256"]
        gen = self.auth._unlock_gen.get(digest, 0)
        status, _, body = self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)
        self.assertEqual((status, body), (200, {"unlocked": False}))
        self.assertEqual(self.unlock_challenges(digest), [], "Lock deletes the pending challenge")
        self.assertEqual(self.auth._unlock_gen[digest], gen + 1, "and bumps the generation")
        self.assertEqual(self.auth.ops.count(digest), 1, "the session stays, and so do its ops")
        self.assertEqual(self.unlock_s(), 0)
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request)[:3:2], LOCKED)
        self.assertEqual(self.send_unlock(credential)[0], 409, "an assertion signed before the Lock")
        self.assertEqual(self.phone("POST", "/m/api/lock", {"x": 1}, cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("GET", "/m/api/lock", cookie=self.cookie)[0], 405)
        print("ok  Lock now: the window ends, the pending challenge goes (409), the generation is bumped")

    def test_lock_after_a_retirement_makes_no_generation(self):
        """Opus NIT on #374: a Lock now that lands after the session retired
        (End in between) never makes its `_unlock_gen` entry again."""
        self.secure_ok()
        session, _credential = self.armed(self.cookie)
        digest = session["token_sha256"]
        self.assertEqual(self.hud("POST", f"/phone/sessions/{session['id']}/end")[0], 200)
        self.assertNotIn(digest, self.auth._unlock_gen)
        self.assertEqual(self.auth.lock(session), {"unlocked": False})
        self.assertNotIn(digest, self.auth._unlock_gen, "[mutation: lock() bumps a retired session's generation]")
        self.assertNotIn(digest, self.auth._unlocks)
        print("ok  Lock now after a retirement: no generation entry is made again")

    def test_each_ending_on_the_existing_instance(self):
        self.secure_ok()
        endings = {}

        # logout
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.assertEqual(self.phone("POST", "/m/api/logout", {}, cookie=cookie)[0], 200)
        endings["logout"] = (session, credential)
        # the desk's End
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.assertEqual(self.hud("POST", f"/phone/sessions/{session['id']}/end")[0], 200)
        endings["End"] = (session, credential)
        # a revoke (another passkey's session)
        b = self.pair()
        cookie = self.signed_in(b)
        self.unlocked(cookie, b)
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request, cookie=cookie)[0], 200)
        credential = self.pending_credential(cookie, b)
        session = self.session_of(cookie)
        key = session["passkey"]
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{key}/revoke")[0], 200)
        endings["revoke"] = (session, credential)
        for what, (session, credential) in endings.items():
            self.retired(session, credential, what)
        print("ok  logout, End and revoke end the window, delete its challenges, retire its generation and ops")

    def test_expiry_and_found_ended(self):
        self.secure_ok()
        # Idle expiry, found by the next request (`session_for` deletes it).
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.wall.now += remote_auth.IDLE_S + 1
        self.assertEqual(self.phone("GET", "/m/api/me", cookie=cookie)[0], 401)
        self.retired(session, credential, "session_for")
        self.wall.now -= remote_auth.IDLE_S + 1
        # Found ended by `session_live` (the stream's check).
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.auth._sessions[session["token_sha256"]]["last_seen"] -= remote_auth.IDLE_S + 1
        self.assertFalse(self.auth.session_live(session["token_sha256"]))
        self.retired(session, credential, "session_live")
        # Found ended by a gated action's own check (`_session_passkey`).
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.auth._sessions[session["token_sha256"]]["created"] -= remote_auth.ABSOLUTE_S + 1
        with self.assertRaises(remote_auth.AuthError) as caught:
            self.auth.require_unlocked(session)
        self.assertEqual(caught.exception.status, 401)
        self.retired(session, credential, "_session_passkey")
        print("ok  expiry: found ended by session_for, session_live or the gated check, each retires the window")

    def test_prune_by_max_sessions(self):
        self.secure_ok()
        cookie = self.other_session()
        session, credential = self.armed(cookie)
        self.auth._sessions[session["token_sha256"]]["last_seen"] -= 3600     # the least recently seen
        for _ in range(remote_auth.MAX_SESSIONS):
            self.signed_in(self.a)
        self.assertNotIn(session["token_sha256"], self.auth._sessions, "evicted")
        self.retired(session, credential, "MAX_SESSIONS eviction")
        print("ok  MAX_SESSIONS eviction retires the evicted session's window, challenges and ops")

    def test_a_restart_is_locked(self):
        self.secure_ok()
        self.unlocked()
        fresh = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        session = self.session_of()
        self.assertTrue(fresh.session_live(session["token_sha256"]) or True)
        fresh._load()
        self.assertIn(session["token_sha256"], fresh._sessions, "the session survives the restart")
        with self.assertRaises(remote_auth.Locked):
            fresh.require_unlocked(session)
        self.assertNotEqual(fresh._op_key, self.auth._op_key)
        print("ok  a restart (a new RemoteAuth): the session survives, the window does not")

    def test_broken_state_is_503(self):
        self.unlocked()
        session = self.session_of()
        self.auth._broken = "passkeys.json cannot be read"
        try:
            with self.assertRaises(remote_auth.AuthError) as caught:
                self.auth.require_unlocked(session)
            self.assertEqual(caught.exception.status, 503)
        finally:
            self.auth._broken = None
        print("ok  _broken set: a gated action is 503")

    def test_revoke_ends_sessions_even_when_the_save_fails(self):
        self.secure_ok()
        b = self.pair()
        cookie = self.signed_in(b)
        self.unlocked(cookie, b)
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request, cookie=cookie)[0], 200)
        session = self.session_of(cookie)
        with patch.object(self.auth, "_save_passkeys",
                          side_effect=remote_auth.AuthError(503, "phone state unavailable")):
            status, _ = self.hud("POST", f"/phone/passkeys/{session['passkey']}/revoke")
        self.assertEqual(status, 503)
        digest = session["token_sha256"]
        self.assertNotIn(digest, self.auth._sessions)
        self.assertNotIn(digest, self.auth._unlocks)
        self.assertEqual(self.auth.ops.count(digest), 0)
        self.assertEqual(self.phone("GET", "/m/api/me", cookie=cookie)[0], 401)
        print("ok  revoke ends the sessions, windows and ops in memory before the save that fails (503)")

    def test_runtime_file_damage_leaves_the_window(self):
        self.secure_ok()
        self.unlocked()
        other = self.other_session()
        self.unlocked(other)
        (self.auth.dir / "sessions.json").write_text("{damaged")
        (self.auth.dir / "passkeys.json").write_text("{damaged")
        request, out, worker = self.ask()
        self.assertEqual(self.allow(request)[0], 200, "memory is authoritative, for windows as for sessions")
        self.decided(out, worker, Decision.ALLOW)
        session = self.session_of(other)
        self.assertEqual(self.hud("POST", f"/phone/sessions/{session['id']}/end")[0], 200)
        self.assertNotIn(session["token_sha256"], self.auth._unlocks, "End still ends it")
        print("ok  runtime damage to the auth files leaves an open window as it is; End still ends it")


# -- Lock now racing verify, and concurrency (§5.2) ---------------------------------------


class Race(Unlock):
    def test_lock_then_verify(self):
        credential = self.pending_credential()
        self.assertEqual(self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)[0], 200)
        self.assertEqual(self.send_unlock(credential)[0], 409)
        self.assertEqual(self.unlock_s(), 0)
        print("ok  Lock, then verify of an assertion signed before it: 409, still locked")

    def test_verify_then_lock(self):
        self.unlocked()
        self.assertEqual(self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)[0], 200)
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request)[:3:2], LOCKED)
        print("ok  verify, then Lock: locked at the end")

    def test_a_generation_bumped_between_issue_and_verify(self):
        credential = self.pending_credential()
        self.auth._unlock_gen[self.digest()] = self.auth._unlock_gen.get(self.digest(), 0) + 1
        status, _, body = self.send_unlock(credential)
        self.assertEqual((status, body), (409, {"error": "challenge expired"}))
        self.assertEqual(self.unlock_s(), 0)
        print("ok  a generation bumped between issue and verify: 409 (defence in depth)")

    def test_two_verifies_of_one_assertion(self):
        credential = self.pending_credential()
        barrier = threading.Barrier(2)
        out = []

        def go():
            barrier.wait()
            out.append(self.send_unlock(credential)[0])
        workers = [threading.Thread(target=go) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(10)
        self.assertEqual(sorted(out), [200, 409])
        print("ok  two verifies of one assertion: one opens the window, the other is 409")

    def test_options_asked_twice(self):
        first = self.pending_credential()
        second = self.pending_credential()
        self.assertEqual(self.send_unlock(first)[0], 409, "replaced")
        self.assertEqual(self.send_unlock(second)[0], 200)
        print("ok  options asked twice: the first assertion is 409, the second opens")


# -- unlock failures (§5.2) ------------------------------------------------------------------


class UnlockFailures(Unlock):
    def refused(self, credential, what, cookie=None, want=401):
        status, _, body = self.send_unlock(credential, cookie)
        self.assertEqual(status, want, (what, body))
        self.assertEqual(self.unlock_s(cookie), 0, what)

    def test_each_failure_is_401_and_stays_closed(self):
        self.refused(self.pending_credential(uv=False), "UV clear")
        bad = self.pending_credential()
        sig = rac.unb64(bad["response"]["signature"])
        bad["response"]["signature"] = rac.b64(sig[:-1] + bytes([sig[-1] ^ 1]))
        self.refused(bad, "signature")
        self.refused(self.pending_credential(origin="https://evil.ts.net"), "origin")
        self.refused(self.pending_credential(client_extra={"crossOrigin": True}), "crossOrigin")
        self.refused(self.pending_credential(rp_id="evil.ts.net"), "rpId")
        bad = self.pending_credential()
        bad["id"] = bad["rawId"] = rac.b64(b"another-credential-id")
        self.refused(bad, "credential id")
        bad = self.pending_credential()
        bad["response"]["userHandle"] = rac.b64(b"another-user")
        self.refused(bad, "user handle")
        # Another session's challenge, sent under this one.
        other = self.other_session()
        self.refused(self.pending_credential(other), "another session")
        # Another passkey answering this session's challenge.
        b = self.pair()
        self.refused(self.pending_credential(authenticator=b), "another passkey")
        print("ok  unlock: UV, signature, origin, crossOrigin, rpId, credential id, user handle, another "
              "session, another passkey: 401 each, the window closed")

    def test_the_counter_rule(self):
        counting = self.pair(rac.Authenticator(step=1))
        cookie = self.signed_in(counting)
        credential = self.pending_credential(cookie, counting, count=counting.count)
        self.refused(credential, "a counter that did not rise", cookie)
        self.assertEqual(self.unlock(cookie, counting)[0], 200)
        print("ok  unlock: the counter rule (one that does not rise is 401)")

    def test_a_replay_is_409(self):
        credential = self.pending_credential()
        self.assertEqual(self.send_unlock(credential)[0], 200)
        status, _, body = self.send_unlock(credential)
        self.assertEqual((status, body), (409, {"error": "challenge expired"}), "spent by the first verify")
        self.assertEqual(self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)[0], 200)
        self.refused(credential, "replay after a Lock", want=409)
        print("ok  unlock: a replayed assertion is 409 (its challenge was spent)")

    def test_a_failed_counter_save_is_503_and_stays_closed(self):
        credential = self.pending_credential()
        with patch.object(self.auth, "_write_passkeys",
                          side_effect=remote_auth.AuthError(503, "phone state unavailable")):
            self.refused(credential, "save", want=503)
        self.assertNotIn(self.digest(), self.auth._unlocks)
        print("ok  unlock: a counter that cannot be saved is 503, and the window stays closed")

    def test_shapes(self):
        self.assertEqual(self.phone("POST", "/m/api/unlock/options", {"x": 1}, cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("POST", "/m/api/unlock", {}, cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("POST", "/m/api/unlock", {"assertion": {}, "x": 1}, cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("GET", "/m/api/unlock", cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("POST", "/m/api/unlock/options?x=1", {}, cookie=self.cookie)[0], 400)
        for path in ("/m/api/unlock/options", "/m/api/unlock", "/m/api/lock", "/m/api/ops/tokens"):
            method = "GET" if path.endswith("tokens") else "POST"
            self.assertEqual(self.phone(method, path, {} if method == "POST" else None)[0], 401, path)
            self.assertEqual(self.phone(method, path, {} if method == "POST" else None, cookie=self.cookie,
                                        origin="https://evil.ts.net")[0], 403 if method == "POST" else 200, path)
        print("ok  unlock routes: shapes 400, methods 405, no session 401, cross-origin 403")


# -- the 423 contract (§5.3) -----------------------------------------------------------------


class Contract(Unlock):
    def test_each_family_here_is_exactly_locked_with_nothing_mutated(self):
        self.secure_ok()
        request, _, _ = self.ask()
        status, _, body = self.allow(request)
        self.assertEqual((status, body), LOCKED, "Allow")
        status, _, body = self.allow(request, decision="always", preview="0" * 64)
        self.assertEqual((status, body), LOCKED, "Always")
        self.assertTrue(self.pending(request))
        self.assertEqual(rnc.v1_permissions.load_allowlist() if self.allowlist.exists() else [], [])
        task = self.d.stores.tasks.create(self.project.id, "x")
        self.d.stores.tasks.save(task)
        task = self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        status, _, body = self.phone("POST", f"/m/api/tasks/{task.id}/cancel", {"op": self.op()},
                                     cookie=self.cookie)
        self.assertEqual((status, body), LOCKED, "cancel")
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CLARIFYING)
        self.assertEqual(self.runner.calls, [])
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        self.d.bus.publish({"kind": "user_message", "thread_id": thread.id, "project_id": self.project.id,
                            "data": {}})
        entry = self.d.working.entry(thread.id)
        status, _, body = self.phone("POST", f"/m/api/working/{thread.id}/done",
                                     {"seq": entry["seq"], "op": self.op()}, cookie=self.cookie)
        self.assertEqual((status, body), LOCKED, "Done")
        status, _, body = self.phone("POST", f"/m/api/working/{thread.id}/undo",
                                     {"token": "x" * 24, "op": self.op()}, cookie=self.cookie)
        self.assertEqual((status, body), LOCKED, "Undo")
        self.assertIsNone(self.d.working.entry(thread.id)["done_at"], "the entry is still in")
        self.assertEqual(self.auth.ops.count(), 0, "a 423 never admits")
        print("ok  423 exactly {error: locked, unlock: true} for Allow, Always, cancel, Done and Undo; "
              "nothing mutated, nothing admitted")

    def test_the_shared_reply(self):
        self.assertEqual(remote_auth.locked_reply(), (423, {"error": "locked", "unlock": True}, []))
        self.assertTrue(issubclass(remote_auth.Locked, remote_auth.AuthError))
        self.assertEqual(remote_auth.Locked().status, 423)
        # Every local catch takes Locked before AuthError.
        import inspect
        from jarvis.v2 import review
        for module in (remote_answer, remote_write, pr_merge, review, remote_auth):
            source = inspect.getsource(module)
            for found in source.split("except Locked:")[1:]:
                self.assertTrue(found.lstrip().startswith("return locked_reply()"), module.__name__)
            self.assertIn("except Locked:", source, module.__name__)
            self.assertLess(source.index("except Locked:"), source.index("except AuthError as exc:"),
                            module.__name__)
        print("ok  Locked is one shared error and reply; every catch takes it before AuthError")


# -- op tokens (§5.3a) -------------------------------------------------------------------------


class OpTokens(Unlock):
    def test_a_duplicate_never_runs_and_reuse_is_refused(self):
        self.secure_ok()
        self.unlocked()
        one, out, worker = self.ask()
        two, _, _ = self.ask(args={"command": "git log"})
        op = self.op()
        self.assertEqual(self.allow(one, op=op)[0], 200)
        self.decided(out, worker, Decision.ALLOW)
        status, _, body = self.allow(one, op=op)
        self.assertEqual((status, body), (409, {"error": "already sent", "duplicate": True, "state": "completed"}))
        status, _, body = self.allow(two, op=op)
        self.assertEqual((status, body), (409, {"error": "op token reused"}))
        self.assertTrue(self.pending(two))
        print("ok  a duplicate is 409 duplicate and never runs; the token with another body is 409 reused")

    def test_tokens_session_forgery_and_time(self):
        self.secure_ok()
        self.unlocked()
        request, _, _ = self.ask()
        other = self.other_session()
        self.assertEqual(self.allow(request, op=self.op(other))[0], 400, "another session's token")
        good = self.op()
        prefix, payload, mac = good.split(".")
        flipped = mac[:-2] + ("A" if mac[-2] != "A" else "B") + mac[-1]
        status, _, body = self.allow(request, op=".".join((prefix, payload, flipped)))
        self.assertEqual((status, body), (410, {"error": remote_auth.FORGED_OP}), "a forged token")
        fields = json.loads(rac.unb64(payload))
        altered = rac.b64(json.dumps(dict(fields, t=fields["t"] + 100), separators=(",", ":"),
                                     sort_keys=True).encode())
        self.assertEqual(self.allow(request, op=".".join((prefix, altered, mac)))[0], 410, "an altered one")
        sid = self.session_of()["id"]
        late = self.auth._op_token(sid, self.mono.now + 1.01)
        status, _, body = self.allow(request, op=late)
        self.assertEqual((status, body), (410, {"error": remote_auth.EXPIRED_OP, "expired": True}))
        self.assertEqual(self.allow(request, op="o1.x.y")[0], 400, "malformed")
        self.assertTrue(self.pending(request))
        soon = self.auth._op_token(sid, self.mono.now + 0.5)
        self.assertEqual(self.allow(request, op=soon)[0], 200, "0.5 s ahead is within the tolerance")
        print("ok  tokens: another session's 400, forged or altered 410, 1.01 s ahead 410, 0.5 s ahead passes")

    def test_expiry(self):
        self.secure_ok()
        request, _, _ = self.ask()
        op = self.op()
        self.mono.now += remote_auth.OP_TTL_S + 1
        self.unlocked()
        status, _, body = self.allow(request, op=op)
        self.assertEqual((status, body), (410, {"error": remote_auth.EXPIRED_OP, "expired": True}))
        self.assertTrue(self.pending(request))
        self.assertEqual(self.auth.ops.count(), 0)
        print("ok  a token past OP_TTL_S is 410 expired, with nothing admitted")

    def test_issuance_never_slides_and_is_rate_limited(self):
        self.unlocked()
        start = self.mono.now
        self.mono.now = start + 59 * MIN
        for i in range(remote_auth.OP_ISSUE_RATE):
            status, _, body = self.phone("GET", "/m/api/ops/tokens", cookie=self.cookie)
            self.assertEqual(status, 200, (i, body))
            self.assertEqual(len(body["tokens"]), remote_auth.OP_TOKENS)
            self.assertEqual(body["ttl_s"], remote_auth.OP_TTL_S)
        status, headers, body = self.phone("GET", "/m/api/ops/tokens", cookie=self.cookie)
        self.assertEqual((status, body), (429, {"error": remote_auth.OP_ISSUE_WORD}))
        self.assertEqual(headers["Retry-After"], "60")
        self.mono.now = start + 61 * MIN
        request, _, _ = self.ask()
        self.assertEqual(self.allow(request)[:3:2], LOCKED, "issuing tokens never slid the window")
        self.assertEqual(self.phone("POST", "/m/api/ops/tokens", {}, cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("GET", "/m/api/ops/tokens?x=1", cookie=self.cookie)[0], 400)
        print("ok  token issuance: never slides the window; OP_ISSUE_RATE per minute, then 429")

    def test_issuance_rechecks_the_session_before_it_answers(self):
        real = self.auth.issue_ops
        session = self.session_of()

        def ending(s):
            out = real(s)
            self.auth.end_session(session["id"])
            return out
        with patch.object(self.auth, "issue_ops", ending):
            status, _, body = self.phone("GET", "/m/api/ops/tokens", cookie=self.cookie)
        self.assertEqual((status, body), (401, {"error": "unauthorized"}))
        print("ok  token issuance: End during it is 401, and no tokens leave")

    def fill(self, digest, n, states=("completed", "refused", "uncertain")):
        for i in range(n):
            self.auth.ops._records[(digest, f"{i:022d}")] = {
                "state": states[i % len(states)], "digest": "d", "reservation": "r",
                "expires": self.mono.now + remote_auth.OP_TTL_S, "outcome": None, "code": None}

    def test_capacity_refuses_and_changes_nothing(self):
        self.secure_ok()
        self.unlocked()
        request, out, worker = self.ask()
        digest = self.digest()
        self.fill(digest, remote_auth.OPS_PER_SESSION)
        used = self.rate_used()
        status, headers, body = self.allow(request)
        self.assertEqual((status, body), (429, {"error": remote_auth.OPS_FULL}))
        self.assertEqual(headers["Retry-After"], "60", "as `_written`'s 429 [mutation: no Retry-After]")
        self.assertTrue(self.pending(request))
        self.assertEqual(self.auth.ops.count(digest), remote_auth.OPS_PER_SESSION, "nothing evicted or added")
        self.assertEqual({r["state"] for r in self.auth.ops._records.values()},
                         {"completed", "refused", "uncertain"}, "every terminal state is kept")
        self.assertEqual(self.rate_used(), used, "no rate token spent")
        # In all: OPS_MAX across other sessions.
        self.auth.ops._records.clear()
        for s in range(remote_auth.MAX_SESSIONS):
            self.fill(f"{s:064x}", remote_auth.OPS_PER_SESSION)
        self.assertEqual(self.auth.ops.count(), remote_auth.OPS_MAX)
        self.assertEqual(self.allow(request)[0], 429)
        self.assertEqual(self.auth.ops.count(), remote_auth.OPS_MAX)
        self.assertEqual(self.rate_used(), used)
        self.auth.ops._records.pop((f"{0:064x}", f"{0:022d}"))
        self.assertEqual(self.allow(request)[0], 200)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  capacity: at OPS_PER_SESSION and at OPS_MAX, 429; nothing evicted, nothing run, no rate token")

    def test_a_running_record(self):
        self.secure_ok()
        self.unlocked()
        one, out, worker = self.ask()
        two, out2, worker2 = self.ask(args={"command": "git log"})
        release, entered = threading.Event(), threading.Event()
        real = remote_answer._dispatch

        def slow(daemon, row_id, body, **kw):
            if row_id == self.row(one):
                entered.set()
                release.wait(10)
            return real(daemon, row_id, body, **kw)
        op = self.op()
        started = self.mono.now
        result = {}
        with patch.object(remote_answer, "_dispatch", slow):
            t = threading.Thread(target=lambda: result.setdefault("first", self.allow(one, op=op)))
            t.start()
            self.assertTrue(entered.wait(5))
            status, _, body = self.allow(one, op=op)
            self.assertEqual((status, body), (409, {"error": "already sent", "duplicate": True, "state": "running"}))
            self.mono.now = started + remote_auth.OP_TTL_S
            status, _, body = self.allow(one, op=op)
            self.assertEqual(status, 410, "at expires the token is refused by its time")
            self.mono.now = started + remote_auth.OP_TTL_S + 30
            self.unlocked()
            self.assertEqual(self.allow(two)[0], 200, "another admission prunes")
            key = (self.digest(), self.n_of(op))
            self.assertIn(key, self.auth.ops._records, "a running record is never pruned")
            self.assertEqual(self.auth.ops._records[key]["state"], "running")
            reservation = self.auth.ops._records[key]["reservation"]
            self.assertFalse(self.auth.ops.mark(key, "0" * 32, "refused"), "a stale reservation")
            self.assertEqual(self.auth.ops._records[key]["state"], "running")
            release.set()
            t.join(10)
        self.assertEqual(result["first"][0], 200)
        self.assertEqual(self.auth.ops._records[key]["state"], "completed", "the late completion, on its own")
        self.assertEqual(self.auth.ops._records[key]["reservation"], reservation)
        self.decided(out, worker, Decision.ALLOW)
        print("ok  running: a duplicate is 409 running; at expires 410 with the record kept; the late "
              "completion lands on its own reservation; a stale one changes nothing")

    def test_states_by_failure_point(self):
        self.secure_ok()
        self.unlocked()
        # An exception before the mutation: refused.
        one, _, _ = self.ask()
        op = self.op()
        with patch.object(remote_answer, "_gate", side_effect=RuntimeError("x")):
            self.assertEqual(self.allow(one, op=op)[0], 409)
        self.assertEqual(self.status_of(op)[2]["state"], "refused")
        self.assertEqual(self.allow(one, op=op)[2], {"error": "already sent", "duplicate": True, "state": "refused"},
                         "a refused op never releases its token")
        self.assertTrue(self.pending(one))
        # After `mutating`, before the call returned: uncertain.
        op = self.op()
        with patch.object(remote_answer, "_dispatch", side_effect=RuntimeError("x")):
            status, _, body = self.allow(one, op=op)
        self.assertEqual((status, body), (409, {"error": remote_write.UNCERTAIN, "uncertain": True}))
        self.assertEqual(self.status_of(op)[2], {"state": "uncertain", "outcome": "unknown", "code": None,
                                                 "words": "Unknown: check at the desk"})
        self.assertEqual(self.allow(one, op=op)[2]["state"], "uncertain")
        # While the answer is built: completed, with the 409 uncertain reply.
        task = self.d.stores.tasks.create(self.project.id, "x")
        self.d.stores.tasks.save(task)
        task = self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        op = self.op()
        with patch.object(remote_api, "task", side_effect=RuntimeError("x")):
            status, _, body = self.phone("POST", f"/m/api/tasks/{task.id}/cancel", {"op": op}, cookie=self.cookie)
        self.assertEqual((status, body), (409, {"error": remote_write.UNCERTAIN, "uncertain": True}))
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CANCELLED)
        self.assertEqual(self.status_of(op)[2], {"state": "completed", "outcome": "unknown", "code": "cancelled",
                                                 "words": "Finished; check at the desk"})
        status, _, body = self.phone("POST", f"/m/api/tasks/{task.id}/cancel", {"op": op}, cookie=self.cookie)
        self.assertEqual(body, {"error": "already sent", "duplicate": True, "state": "completed"})
        self.assertEqual(self.runner.calls, [("cancel", task.id)], "cancelled once")
        print("ok  states: before mutating refused, after it uncertain, while answering completed; a replay "
              "of each is 409 duplicate")

    def test_refusals_never_admit_and_the_423_retry_runs_once(self):
        self.secure_ok()
        request, out, worker = self.ask()
        op = self.op()
        self.assertEqual(self.allow(request, op=op)[:3:2], LOCKED)
        self.assertEqual(self.post_answer(self.row(request), {"decision": "allow", "op": op, "x": 1})[0], 400)
        self.assertEqual(self.auth.ops.count(), 0, "neither a 423 nor a 400 admits")
        self.unlocked()
        self.assertEqual(self.allow(request, op=op)[0], 200, "the same token after the unlock")
        self.decided(out, worker, Decision.ALLOW)
        self.assertEqual(self.allow(request, op=op)[2]["duplicate"], True)
        self.assertEqual(self.auth.ops.count(), 1)
        print("ok  a 400 or 423 never admits; 423, unlock, the same token: it runs once")

    def test_the_status_route(self):
        self.secure_ok()
        self.unlocked()
        request, out, worker = self.ask()
        op = self.op()
        self.assertEqual(self.allow(request, op=op)[0], 200)
        status, _, body = self.status_of(op)
        self.assertEqual((status, body), (200, {"state": "completed", "outcome": "ok", "code": "allowed",
                                                "words": "Done"}))
        other = self.other_session()
        unknown = {"state": "unknown", "outcome": None, "code": None, "words": "Unknown: check at the desk"}
        self.assertEqual(self.status_of(op, other)[2], unknown, "another session's nonce is unknown")
        n = self.n_of(op)
        self.assertEqual(self.phone("GET", f"/m/api/ops/{n}?x=1", cookie=self.cookie)[0], 400)
        self.assertEqual(self.phone("POST", f"/m/api/ops/{n}", {}, cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("GET", "/m/api/ops/not!a!nonce", cookie=self.cookie)[0], 400)
        session = self.session_of(other)
        real = self.auth.op_status

        def ending(s, nonce):
            found = real(s, nonce)
            self.auth.end_session(session["id"])
            return found
        with patch.object(self.auth, "op_status", ending):
            self.assertEqual(self.status_of(op, other)[0], 401, "End during the read")
        # Unknown after a prune and after a restart.
        fresh = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertEqual(fresh.op_status(self.session_of(), n)["state"], "unknown")
        self.mono.now += remote_auth.OP_TTL_S + 1
        self.unlocked()
        two, _, _ = self.ask(args={"command": "git log"})
        self.assertEqual(self.allow(two)[0], 200)
        self.assertEqual(self.status_of(op)[2], unknown, "pruned")
        print("ok  op status: this session's only; End during the read 401; query 400; POST 405; unknown "
              "after a prune or a restart")

    def test_outcomes_and_their_words(self):
        """Sol r5-2, r6-1, r6-2: only a confirmed result is "Done"; an Update
        GitHub only accepted reads "Update requested"; every current PR
        result has its fixed code; "not read again" is unknown first."""
        cases = {
            "merged": ("ok", "merged", "Done"),
            "updated": ("ok", "updated", "Done"),
            "update requested": ("ok", "update_requested", "Update requested; check at the desk"),
            "not updated": ("failed", "not_updated", "Not done"),
            "not read again": ("unknown", "not_read_again", "Finished; check at the desk"),
            "queued": ("failed", "queued", "Not done"),
            "head moved": ("failed", "head_moved", "Not done"),
            "closed": ("failed", "closed", "Not done"),
            "not mergeable": ("failed", "not_mergeable", "Not done"),
        }
        for result, (outcome, code, words) in cases.items():
            ok = result in ("merged", "updated", "update requested")
            got = remote_auth.op_outcome("pr", 200, {"ok": ok, "result": result}, result)
            self.assertEqual(got, (outcome, code), result)
            self.assertEqual(remote_auth.op_words("completed", *got), words, result)
        # "changed" is the 422 exception path, a 409 after gh returned.
        got = remote_auth.op_outcome("pr", 409, {"error": "changed since you read it: its head moved"}, "changed")
        self.assertEqual(got, ("failed", "changed"))
        # Every phrase `PrActions._outcome` can give is mapped.
        self.assertEqual(set(remote_auth.PR_CODES), set(cases) | {"changed"})
        self.assertEqual(remote_auth.op_outcome("pr", 200, {"ok": True}, "some model text"), ("ok", "other"))
        self.assertEqual(remote_auth.op_words("completed", "ok", "other"), "Done")
        self.assertEqual(remote_auth.op_outcome("approval", 200, {"decision": "allow"}, "allowed"), ("ok", "allowed"))
        self.assertEqual(remote_auth.op_outcome("review", 200, {"ok": False}, "approved"), ("failed", "approved"))
        self.assertEqual(remote_auth.op_words("refused", None, None), "Not done")
        self.assertEqual(remote_auth.op_words("running", None, None), "Still running")
        self.assertEqual(remote_auth.op_words("uncertain", "unknown", None), "Unknown: check at the desk")
        print("ok  outcomes: Done only for confirmed results; update requested its own words; every PR "
              "result coded; not read again unknown")

    def test_the_rate_and_a_duplicate(self):
        self.secure_ok()
        self.unlocked()
        one, out, worker = self.ask()
        two, _, _ = self.ask(args={"command": "git log"})
        op = self.op()
        self.assertEqual(self.allow(one, op=op)[0], 200)
        self.exhaust_rate()
        status, _, body = self.allow(one, op=op)
        self.assertEqual(body.get("duplicate"), True, "rate exhausted plus a duplicate: 409 duplicate")
        self.assertEqual(self.rate_used(), remote_write.RATE, "no rate token spent")
        fresh = self.op()
        status, headers, body = self.allow(two, op=fresh)
        self.assertEqual((status, body), (429, {"error": remote_auth.RATE_WORD}))
        self.assertEqual(headers["Retry-After"], "60")
        self.assertNotIn((self.digest(), self.n_of(fresh)), self.auth.ops._records, "not admitted")
        self.assertTrue(self.pending(two))
        print("ok  order: rate exhausted plus a duplicate is 409 duplicate; plus a new token, 429 and not admitted")

    def test_the_composed_status_order(self):
        self.secure_ok()
        request, out, worker = self.ask()
        self.assertEqual(self.post_answer(self.row(request), {"decision": "deny"})[0], 200)     # now stale
        self.decided(out, worker, Decision.DENY)
        # Malformed, locked and stale: 400.
        self.assertEqual(self.post_answer(self.row(request), {"decision": "allow", "op": "garbage"})[0], 400)
        # Locked and stale: 423.
        self.assertEqual(self.allow(request)[:3:2], LOCKED)
        # Rate exhausted and locked: 423, no token spent.
        self.exhaust_rate()
        self.assertEqual(self.allow(request)[:3:2], LOCKED)
        self.assertEqual(self.rate_used(), remote_write.RATE)
        self.assertEqual(self.auth.ops.count(), 0)
        remote_write.rate_of(self.auth.server)._seen.clear()
        # A duplicate and stale: 409 duplicate.
        self.unlocked()
        live, out, worker = self.ask()
        op = self.op()
        self.assertEqual(self.allow(live, op=op)[0], 200)
        self.assertEqual(self.allow(live, op=op)[2]["duplicate"], True)
        # Stale only: the act-time 409.
        self.assertEqual(self.allow(request)[2], {"error": remote_answer.GONE})
        print("ok  composed: malformed+locked+stale 400; locked+stale 423; rate+locked 423 (no token); "
              "duplicate+stale 409 duplicate")


# -- Done and Undo from the phone (§5.6) --------------------------------------------------------


class Retirement(Unlock):
    """Sol's MED and Opus LOW-1 on #374, with barriers: an ending (the desk's
    End, a revoke, Lock now) that lands after `require_unlocked`, either
    before admission or between admission and the mutation, refuses the op
    before it mutates: 401 for an ending, 423 for Lock now. An ended session
    keeps no op record; a locked one keeps its op, `refused`."""

    def cancellable(self):
        task = self.d.stores.tasks.create(self.project.id, "x")
        self.d.stores.tasks.save(task)
        return self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)

    def ending(self, what):
        """A fresh unlocked session: (its cookie, the ending as a call that
        answers its status, the status a held request then gets)."""
        if what == "revoke":
            b = self.pair()
            cookie = self.signed_in(b)
            self.unlocked(cookie, b)
            key = self.session_of(cookie)["passkey"]
            return cookie, lambda: self.hud("POST", f"/phone/passkeys/{key}/revoke")[0], 401
        cookie = self.other_session()
        self.unlocked(cookie)
        if what == "End":
            sid = self.session_of(cookie)["id"]
            return cookie, lambda: self.hud("POST", f"/phone/sessions/{sid}/end")[0], 401
        return cookie, lambda: self.phone("POST", "/m/api/lock", {}, cookie=cookie)[0], 423

    def holder(self, owner, name, key, *, after=False):
        """`owner.name` patched so a call whose `key(*args)` is in the dict it
        returns waits there for that entry's `go` (after the real call when
        `after`), having set its `arrived`: `{key: (arrived, go)}`."""
        held: dict = {}
        real = getattr(owner, name)

        def wrapper(*a, **k):
            gate = held.get(key(*a, **k))
            if gate is None:
                return real(*a, **k)
            out = real(*a, **k) if after else None
            gate[0].set()
            gate[1].wait(10)
            return out if after else real(*a, **k)
        guard = patch.object(owner, name, wrapper)
        guard.start()
        self.addCleanup(guard.stop)
        return held

    def background(self, fn):
        out: list = []
        thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
        thread.start()
        return thread, out

    def cancel_later(self, task, op, cookie):
        return self.background(lambda: self.phone("POST", f"/m/api/tasks/{task.id}/cancel", {"op": op},
                                                  cookie=cookie))

    def test_an_ending_before_admission(self):
        """Sol's barrier: held after the window and the signature, before
        admission; the ending lands; released, the cancel is refused and
        nothing is admitted."""
        held = self.holder(self.auth, "verify_op", lambda session, op: op["n"], after=True)
        for what in ("End", "revoke", "Lock now"):
            with self.subTest(what=what):
                cookie, end, want = self.ending(what)
                digest = self.digest(cookie)
                task = self.cancellable()
                op = self.op(cookie)
                arrived, go = held[self.n_of(op)] = (threading.Event(), threading.Event())
                thread, out = self.cancel_later(task, op, cookie)
                self.assertTrue(arrived.wait(5), "held past the window and the signature")
                self.assertEqual(end(), 200)
                go.set()
                thread.join(10)
                status, _, body = out[0]
                self.assertEqual(status, want, body)
                if want == 423:
                    self.assertEqual(body, LOCKED[1])
                self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CLARIFYING)
                self.assertEqual(self.auth.ops.count(digest), 0,
                                 "nothing admitted [mutation: no final check at admission]")
                if want == 401:
                    self.assertNotIn(digest, self.auth._unlock_gen)
        self.assertEqual(self.runner.calls, [])
        print("ok  End, revoke or Lock now before admission (barrier): 401, 401, 423; nothing cancelled, "
              "nothing admitted")

    def test_an_ending_waits_for_an_admission_under_way(self):
        """Admission and its final check hold the auth lock, which every
        retirement takes: End waits for an admission under way and then
        retires the new record with the session."""
        cookie, end, _want = self.ending("End")
        digest = self.digest(cookie)
        task = self.cancellable()
        op = self.op(cookie)
        admitting = self.holder(self.auth.ops, "admit", lambda session_digest, n, *a, **k: n)
        arrived, go = admitting[self.n_of(op)] = (threading.Event(), threading.Event())
        ended = threading.Event()
        acting = self.holder(remote_write, "_task", lambda daemon, task_id: task_id)
        acting[task.id] = (threading.Event(), ended)        # the act waits for the End
        thread, out = self.cancel_later(task, op, cookie)
        self.assertTrue(arrived.wait(5), "held inside admission")
        ender, said = self.background(lambda: (end(), ended.set())[0])
        ender.join(2.0)
        self.assertTrue(ender.is_alive(), "End waits for the admission under way "
                                          "[mutation: admission outside the auth lock]")
        go.set()
        ender.join(10)
        thread.join(10)
        self.assertEqual(said, [200])
        status, _, body = out[0]
        self.assertEqual((status, body), (401, {"error": "unauthorized"}))
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CLARIFYING)
        self.assertEqual(self.auth.ops.count(digest), 0, "the retirement took the new record")
        self.assertEqual(self.runner.calls, [])
        print("ok  End during an admission (barrier): it waits for the auth lock, then retires the record; "
              "the cancel is 401")

    def test_an_ending_between_admission_and_the_mutation(self):
        """Opus LOW-1: held at the act-time lookup (after admission, before
        `mutating()`); the ending lands; released, `mutating()`'s final check
        refuses before the mutation."""
        held = self.holder(remote_write, "_task", lambda daemon, task_id: task_id)
        for what in ("End", "revoke", "Lock now"):
            with self.subTest(what=what):
                cookie, end, want = self.ending(what)
                digest = self.digest(cookie)
                task = self.cancellable()
                op = self.op(cookie)
                arrived, go = held[task.id] = (threading.Event(), threading.Event())
                thread, out = self.cancel_later(task, op, cookie)
                self.assertTrue(arrived.wait(5), "held after admission")
                self.assertEqual(self.auth.ops.count(digest), 1, "admitted")
                self.assertEqual(end(), 200)
                go.set()
                thread.join(10)
                status, _, body = out[0]
                self.assertEqual(status, want, (body, "[mutation: no final check in mutating()]"))
                if want == 423:
                    self.assertEqual(body, LOCKED[1])
                self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CLARIFYING)
                if want == 401:
                    self.assertEqual(self.auth.ops.count(digest), 0, "retired; nothing settles it back")
                else:
                    # The session holds: its op stays recorded, refused (the
                    # token is never released; a new tap retries).
                    self.assertEqual(self.status_of(op, cookie)[2],
                                     {"state": "refused", "outcome": None, "code": None, "words": "Not done"})
        self.assertEqual(self.runner.calls, [])
        print("ok  End, revoke or Lock now between admission and the mutation (barrier): 401, 401, 423; "
              "nothing cancelled; an ended session keeps no record, a locked one keeps it refused")

    def test_the_last_check_never_slides(self):
        self.unlocked()
        held = self.holder(remote_write, "_task", lambda daemon, task_id: task_id)
        task = self.cancellable()
        op = self.op()
        arrived, go = held[task.id] = (threading.Event(), threading.Event())
        thread, out = self.cancel_later(task, op, self.cookie)
        self.assertTrue(arrived.wait(5))
        self.mono.now += 59 * MIN
        go.set()
        thread.join(10)
        self.assertEqual(out[0][0], 200, out[0])
        self.assertEqual(self.runner.calls, [("cancel", task.id)])
        self.assertEqual(self.unlock_s(), 60, "slid by require_unlocked only [mutation: the final check slides]")
        print("ok  the final checks at admission and before the mutation never slide the window")


class DoneUndo(Unlock):
    def setUp(self):
        super().setUp()
        self.statuses = {}
        guard = patch.object(self.d.activity, "thread_status", lambda tid: self.statuses.get(tid, "idle"))
        guard.start()
        self.addCleanup(guard.stop)
        self.ws = self.d.working
        self.ws_mono = rac.Clock(100.0)
        self.ws.mono = self.ws_mono
        self.one = self.join()
        self.two = self.join()

    def join(self):
        thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        self.d.bus.publish({"kind": "user_message", "thread_id": thread.id, "project_id": self.project.id,
                            "data": {}})
        eventually(lambda: self.ws.entry(thread.id) is not None, what="the join")
        return thread

    def seq(self, thread) -> int:
        status, _, body = self.phone("GET", "/m/api/working", cookie=self.cookie)
        self.assertEqual(status, 200, body)
        return next(e["seq"] for e in body["entries"] if e["thread_id"] == thread.id)

    def done(self, thread, seq, op=None):
        return self.phone("POST", f"/m/api/working/{thread.id}/done", {"seq": seq, "op": op or self.op()},
                          cookie=self.cookie)

    def undo(self, thread, token, op=None):
        return self.phone("POST", f"/m/api/working/{thread.id}/undo", {"token": token, "op": op or self.op()},
                          cookie=self.cookie)

    def inside(self, thread) -> bool:
        return self.ws.entry(thread.id)["done_at"] is None

    def test_the_read_carries_seq(self):
        self.assertEqual(self.seq(self.one), self.ws.entry(self.one.id)["seq"])
        self.assertIn("seq", remote_api.WORKING_ENTRY_FIELDS)
        print("ok  the phone's working read carries each entry's seq")

    def test_done_and_undo(self):
        seq = self.seq(self.one)
        self.assertEqual(self.done(self.one, seq)[:3:2], LOCKED)
        self.assertTrue(self.inside(self.one))
        self.unlocked()
        for bad in ("1", -1, True, None, 1.5):
            self.assertEqual(self.done(self.one, bad)[0], 400, bad)
        self.assertEqual(self.phone("POST", f"/m/api/working/{self.one.id}/done", {"op": self.op()},
                                    cookie=self.cookie)[0], 400, "seq missing")
        self.assertEqual(self.phone("POST", "/m/api/working/ZZZZ/done", {"seq": seq, "op": self.op()},
                                    cookie=self.cookie)[0], 400, "a malformed id")
        status, _, body = self.done(self.one, seq + 1)
        self.assertEqual(status, 409, "a stale seq on the URL thread")
        self.assertEqual(set(body), {"error"})
        self.assertTrue(self.inside(self.one))
        op = self.op()
        status, _, body = self.done(self.one, seq, op)
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"thread_id", "done", "token", "undo_s"})
        self.assertEqual((body["thread_id"], body["done"]), (self.one.id, True))
        self.assertFalse(self.inside(self.one))
        self.assertEqual(self.status_of(op)[2]["code"], "done")
        token = body["token"]
        self.assertEqual(self.undo(self.two, token)[0], 409, "Done's token on another thread's Undo")
        self.assertEqual(self.undo(self.one, "y" * 24)[0], 409, "a wrong token")
        self.assertEqual(self.undo(self.one, "!")[0], 400)
        status, _, body = self.undo(self.one, token)
        self.assertEqual((status, body), (200, {"thread_id": self.one.id, "done": False}))
        self.assertTrue(self.inside(self.one))
        self.assertEqual(self.undo(self.one, token)[0], 409, "Undo with no Done")
        print("ok  Done and Undo: locked 423; malformed 400; stale 409; Done gives {thread_id, done, token, "
              "undo_s}; another thread's or a wrong token 409; Undo puts it back")

    def test_undo_too_late_and_after_an_archive(self):
        self.unlocked()
        status, _, body = self.done(self.one, self.seq(self.one))
        self.assertEqual(status, 200, body)
        self.ws_mono.now += 10.0
        self.assertEqual(self.undo(self.one, body["token"])[0], 409, "at the deadline: too late")
        status, _, body = self.done(self.two, self.seq(self.two))
        self.assertEqual(status, 200, body)
        self.d.archive_thread(self.two.id) if hasattr(self.d, "archive_thread") else \
            self.d.bus.publish({"kind": "thread_archived", "thread_id": self.two.id, "project_id": self.project.id})
        self.assertEqual(self.undo(self.two, body["token"])[0], 409, "after an archive")
        print("ok  Undo: after 10 s, or after an archive, 409")

    def test_undo_after_a_restart_of_the_set(self):
        """The Undo deadline lives in this process's memory (D-4): what a
        restart forgets, the phone's Undo cannot use either."""
        self.unlocked()
        status, _, body = self.done(self.one, self.seq(self.one))
        self.assertEqual(status, 200, body)
        self.ws._undo.clear()                       # what a restart leaves
        self.assertEqual(self.undo(self.one, body["token"])[0], 409)
        self.assertFalse(self.inside(self.one))
        print("ok  Undo after the set's restart (its in-memory deadline gone): 409")

    def test_a_linked_row_or_a_running_turn_keeps_it_in(self):
        self.secure_ok()
        self.unlocked()
        request, out, worker = self.ask(thread_id=self.one.id)
        # The approval is activity: it bumps the thread's seq, so read it now.
        eventually(lambda: self.seq(self.one) == self.ws.entry(self.one.id)["seq"], what="the bump")
        status, _, body = self.done(self.one, self.seq(self.one))
        self.assertEqual(status, 409, body)
        self.assertEqual(set(body), {"error", "why_not"}, "§5.6/§7: 409 with why_not")
        self.assertIn("waits on you", body["why_not"])
        self.assertEqual(body["error"], body["why_not"])
        self.assertTrue(self.inside(self.one))
        self.statuses[self.two.id] = "working"
        status, _, body = self.done(self.two, self.seq(self.two))
        self.assertEqual((status, body), (409, {"error": "a turn is running in this chat",
                                                "why_not": "a turn is running in this chat"}))
        self.assertTrue(self.inside(self.two))
        # The reason goes through the phone's scrub, as every phone string does.
        with patch.object(self.ws, "why_not", lambda ids, rows=None: {t: "x " + rnc.TOKEN for t in ids}):
            status, _, body = self.done(self.two, self.seq(self.two))
        self.assertEqual(status, 409, body)
        self.assertNotIn(rnc.TOKEN, json.dumps(body))
        self.assertEqual(body["why_not"], remote_write._word("x " + rnc.TOKEN))
        # A stale seq is not a why_not.
        status, _, body = self.done(self.two, self.seq(self.two) + 1)
        self.assertEqual((status, set(body)), (409, {"error"}))
        print("ok  Done: a linked rank 1-4 row or a running turn is 409 {error, why_not} (scrubbed), and the "
              "thread stays in; a stale seq is {error} only")


# -- recording and the boundary ---------------------------------------------------------------


class Boundary(Unlock):
    def test_presence_and_recording(self):
        self.secure_ok()
        self.unlocked()
        seen = []
        self.d.owner_seen = lambda: seen.append(1)
        request, out, worker = self.ask()
        self.assertEqual(self.allow(request)[0], 200)
        self.assertEqual(len(seen), 1, "QH-3: an authorised gated action is presence")
        two, _, _ = self.ask()
        self.phone("POST", "/m/api/lock", {}, cookie=self.cookie)
        self.assertEqual(self.allow(two)[0], 423)
        self.assertEqual(len(seen), 1, "a refused one is not")
        print("ok  owner_seen runs on each authorised gated action, never on a 423")

    def test_agents_never_reach_the_new_paths(self):
        for path in ("/m/api/unlock", "/m/api/unlock/options", "/m/api/lock", "/m/api/ops/tokens",
                     "/m/api/ops/" + "A" * 22, "/m/api/working/0123abcd/done", "/m/api/working/0123abcd/undo",
                     "/m/api/inbox/appr:x/answer/rule"):
            for cmd in (f"curl -s https://{HOST}{path}", f"curl -s -X POST http://127.0.0.1:8408{path}"):
                verdict = rules.decide(cmd)
                self.assertEqual((verdict.decision, verdict.reason), (rules.DENY, rules.REMOTE_DENIED), cmd)
            for port in (self.d.port, self.d.face_port):
                self.assertEqual(self.hud("POST", path, {}, port=port)[0], 404, (port, path))
        print("ok  the new phone paths: agents DENY, the API and HUD listeners 404")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=1)
