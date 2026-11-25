"""Review probes for the phone's data routes: tiers, sessions, methods and pass-through."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import phone_auth_check as auth_check
from phone_listener_check import HOST, request
import phone_routes_check as base
from phone_routes_check import APPROVAL_ANSWER, DATA_ROUTES, LOCKED, UNAUTHORIZED, BRIEF_FIELDS


from jarvis.v3 import phone
from jarvis.v3.feed import FeedBusy


ORIGIN = "https://" + HOST


class PhoneRoutesReview(unittest.TestCase):
    setUp = base.PhoneRoutesCheck.setUp
    start = base.PhoneRoutesCheck.start
    api = base.PhoneRoutesCheck.api
    open_approval = base.PhoneRoutesCheck.open_approval
    post_brief = base.PhoneRoutesCheck.post_brief
    call = auth_check.PhoneAuthCheck.call
    ok = auth_check.PhoneAuthCheck.ok
    pair = auth_check.PhoneAuthCheck.pair
    sign_in = auth_check.PhoneAuthCheck.sign_in
    unlock = auth_check.PhoneAuthCheck.unlock
    me = auth_check.PhoneAuthCheck.me

    def raw(self, route, *, method="POST", body=None, cookie=None):
        headers = {"Cookie": cookie} if cookie is not None else None
        return request(self.daemon.phone_port, method, "/m/api/" + route, origin=ORIGIN,
                       headers=headers, body=body)

    # 1. A locked phone and every spelling of an unlock-tier body.
    def test_locked_approval_spellings_never_reach_the_broker(self):
        self.start()
        card, _, _, _ = self.open_approval()
        before = self.broker.pending()
        ident = json.dumps(card["id"])
        digest = json.dumps(card["digest"])
        spellings = [
            {**card, "decision": value}
            for value in (" deny", "deny ", "DENY", "deny\n", "deny​", "approve", "Approve",
                          None, 0, 1, True, {"decision": "deny"}, ["deny"], ["approve"])
        ] + [
            {"id": 1, "digest": card["digest"], "decision": "deny"},
            {"id": card["id"], "digest": None, "decision": "deny"},
            {"id": card["id"], "digest": card["digest"], "decision": "deny", "decision2": "approve"},
            b"\xef\xbb\xbf" + json.dumps({**card, "decision": "approve"}).encode(),
            b"\xef\xbb\xbf" + json.dumps({**card, "decision": "deny"}).encode(),
            json.dumps({**card, "decision": "deny"}).encode() + b"x",
            json.dumps({**card, "decision": "approve"}).encode() + b" {}",
            f'{{"id":{ident},"digest":{digest},"decision":"deny","decision":"approve"}}'.encode(),
            f'{{"id":{ident},"digest":{digest},"decision":"approve","decision":"deny"}}'.encode(),
            f'{{"id":{ident},"id":{ident},"digest":{digest},"decision":"deny"}}'.encode(),
            b"\xff\xfe" + json.dumps({**card, "decision": "deny"}).encode("utf-16-le"),
            json.dumps({**card, "decision": "deny"}).encode("utf-16"),
            b"", b"null", b"1", b'"deny"', b"[]", b"[{}]", b"\x80\x81",
            ("[" * 300 + "]" * 300).encode(),
            ('{"a":' * 200 + "1" + "}" * 200).encode(),
            b'{"id":' + ident.encode() + b',"digest":' + digest.encode() + b',"decision":NaN}',
        ]
        with patch.object(self.broker, "answer", wraps=self.broker.answer) as answer:
            for body in spellings:
                with self.subTest(body=body):
                    self.assertEqual(self.api(APPROVAL_ANSWER, body=body), LOCKED)
            answer.assert_not_called()
        self.assertEqual(self.broker.pending(), before)
        self.assertEqual(self.me(self.cookie)["unlock_s"], 0)

    def test_locked_feed_answer_spellings_leave_the_brief_open(self):
        self.start()
        bid = self.post_brief()
        before = self.feed.get(bid)
        spellings = [
            {"id": bid, "action": value}
            for value in ("approve", "edit", "APPROVE", " not_now", "not_now ", "Other", "NOT_NOW",
                          "dismissed", None, 0, ["not_now"], {"action": "not_now"}, "")
        ] + [
            {"id": bid, "action": "edit", "text": "Fixture edit"},
            {"id": bid},
            b"\xef\xbb\xbf" + json.dumps({"id": bid, "action": "approve"}).encode(),
            json.dumps({"id": bid, "action": "approve"}).encode() + b"]",
            f'{{"id":"{bid}","action":"not_now","action":"approve"}}'.encode(),
            f'{{"id":"{bid}","action":"approve","action":"not_now"}}'.encode(),
            b"", b"null", b"[]", b'"not_now"', b"\xfe\xff",
        ]
        with patch.object(self.feed, "answer", wraps=self.feed.answer) as answer:
            for body in spellings:
                with self.subTest(body=body):
                    self.assertEqual(self.api("feed/answer", body=body), LOCKED)
            answer.assert_not_called()
        self.assertEqual(self.feed.get(bid), before)

    def test_unlock_belongs_to_one_session(self):
        self.start()
        other = self.sign_in().split(";", 1)[0]
        card, _, _, _ = self.open_approval()
        bid = self.post_brief()
        self.unlock(self.cookie)
        self.clock.now += 100
        before = self.broker.pending()
        for cookie_body in (("approvals/answer", {**card, "decision": "approve"}),
                            ("feed/answer", {"id": bid, "action": "approve"})):
            route, body = cookie_body
            with self.subTest(route=route):
                status, _, payload = self.call(route, body=body, cookie=other)
                self.assertEqual((status, payload), LOCKED)
        self.assertEqual(self.broker.pending(), before)
        self.assertEqual(self.feed.get(bid)["state"], "open")
        # A session write on the other session neither opens its window nor slides this one.
        status, _, _ = self.call("feed/feedback", body={"id": bid, "tag": "too_long"}, cookie=other)
        self.assertEqual(status, 200)
        self.assertEqual(self.me(other)["unlock_s"], 0)
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3500)

    # 2. No valid session.
    def test_ended_and_revoked_sessions_reach_no_data_route(self):
        self.start()
        self.unlock(self.cookie)
        card, _, _, _ = self.open_approval()
        ended = self.cookie
        self.ok("logout", cookie=ended)
        second = self.sign_in().split(";", 1)[0]
        bodies = {"approvals/answer": {**card, "decision": "approve"}, "chat/send": {"text": "Fixture"},
                  "chat/events": {"after": 0}, "chat/stop": {}, "chat/new": {}}
        with (patch.object(self.broker, "answer", wraps=self.broker.answer) as answer,
              patch.object(self.feed, "snapshot", wraps=self.feed.snapshot) as snapshot,
              patch.object(self.chat, "send", wraps=self.chat.send) as send):
            for method, route in DATA_ROUTES:
                with self.subTest(route=route, cookie="ended"):
                    status, _, payload = self.call(route, method=method, cookie=ended,
                                                   body=bodies.get(route, {}) if method == "POST" else None)
                    self.assertEqual((status, payload), UNAUTHORIZED)
            self.assertTrue(self.auth.revoke(self.passkey))
            for method, route in DATA_ROUTES:
                with self.subTest(route=route, cookie="revoked"):
                    status, _, payload = self.call(route, method=method, cookie=second,
                                                   body=bodies.get(route, {}) if method == "POST" else None)
                    self.assertEqual((status, payload), UNAUTHORIZED)
            answer.assert_not_called()
            snapshot.assert_not_called()
            send.assert_not_called()

    def test_other_methods_on_every_route(self):
        self.start()
        for method, route in DATA_ROUTES:
            for other in ("HEAD", "PUT", "DELETE", "PATCH", "OPTIONS", "GET" if method == "POST" else "POST"):
                with self.subTest(route=route, method=other):
                    status, headers, payload = self.raw(route, method=other)
                    self.assertEqual(status, 401)
                    self.assertEqual(headers["X-Frame-Options"], "DENY")
                    status, headers, payload = self.raw(route, method=other, cookie=self.cookie)
                    self.assertEqual(status, 404)
                    self.assertEqual(headers["Content-Security-Policy"], phone.CSP)
                    if other == "HEAD":
                        self.assertEqual(payload, b"")
                    else:
                        self.assertEqual(json.loads(payload), {"error": "not found"})
        for prefix in ("", "/", "missing", "status/", "feed/answer/x", "../owner/feed", "STATUS"):
            with self.subTest(path=prefix):
                status, _, _ = request(self.daemon.phone_port, "GET", "/m/api" + ("/" + prefix if prefix else ""),
                                       origin=ORIGIN)
                self.assertEqual(status, 401)

    # 5. Pass-through and failures.
    def test_service_exception_is_a_fixed_500_with_headers(self):
        self.start()
        card, _, _, _ = self.open_approval()
        self.unlock(self.cookie)
        secret = "fixture failure at /fixture/private/path"
        cases = (
            ("feed", "GET", None, patch.object(self.feed, "snapshot", side_effect=RuntimeError(secret))),
            (APPROVAL_ANSWER, "POST", {**card, "decision": "approve"},
             patch.object(self.broker, "answer", side_effect=RuntimeError(secret))),
            ("status", "GET", None, patch.object(self.server, "services",
                                                 replace(self.server.services, status=lambda: {"service": "x"}))),
            ("chat/send", "POST", {"text": "Fixture"},
             patch.object(self.chat, "send", side_effect=OSError(secret))),
        )
        for route, method, body, fault in cases:
            with self.subTest(route=route), fault:
                status, headers, payload = self.raw(route, method=method, body=body, cookie=self.cookie)
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(payload), {"error": "internal error"})
                self.assertNotIn(b"fixture", payload)
                self.assertEqual(headers["Content-Security-Policy"], phone.CSP)
                self.assertEqual(headers["Cache-Control"], "no-store")
        # The listener still serves after the failures.
        self.assertEqual(self.api("status", method="GET")[0], 200)

    def test_underlying_refusals_pass_through(self):
        self.start()
        card, _, _, _ = self.open_approval()
        self.assertEqual(self.api(APPROVAL_ANSWER, body={**card, "digest": "0" * 64, "decision": "deny"}),
                         (409, {"error": "Approval conflicts with the pending request."}))
        self.assertEqual(self.api(APPROVAL_ANSWER, body={"id": "fixture-missing", "digest": "x", "decision": "deny"}),
                         (404, {"error": "Approval not found."}))
        bid = self.post_brief()
        self.assertEqual(self.api("feed/answer", body={"id": bid, "action": "not_now"})[0], 200)
        self.assertEqual(self.api("feed/answer", body={"id": bid, "action": "not_now"}),
                         (400, {"error": "Feed operation refused."}))
        self.assertEqual(self.api("feed/answer", body={"id": bid, "action": "not_now", "text": "x"}),
                         (400, {"error": "Feed operation refused."}))
        with patch.object(self.feed, "feedback", side_effect=FeedBusy("fixture")):
            self.assertEqual(self.api("feed/feedback", body={"id": bid, "tag": "too_long"}),
                             (503, {"error": "Feed is busy."}))
        status, _, payload = self.raw("chat/send", body=b'{"text":"' + b"a" * (70 * 1024) + b'"}',
                                      cookie=self.cookie)
        self.assertEqual((status, json.loads(payload)), (413, {"error": "request body too large"}))

    # Ordinary unlocked phone actions on the feed.
    def test_unlocked_feed_approve_and_edit_work_and_slide(self):
        self.start()
        self.unlock(self.cookie)
        approve_id, edit_id = self.post_brief(), self.post_brief()
        self.clock.now += 100
        status, payload = self.api("feed/answer", body={"id": approve_id, "action": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(payload.keys(), {"brief", "executed"})
        self.assertEqual(payload["brief"].keys(), BRIEF_FIELDS)
        self.assertEqual(payload["brief"]["state"], "approved")
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3600)
        self.clock.now += 100
        status, payload = self.api("feed/answer", body={"id": edit_id, "action": "edit", "text": "Fixture edit"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["brief"]["state"], "edited")
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3600)

    # 6. A refused session write is not a write.
    def pair_and_remember(self):
        self.passkey = auth_check.PhoneAuthCheck.pair(self)


# Keep the passkey id for the revoke probe.
PhoneRoutesReview.pair = PhoneRoutesReview.pair_and_remember


if __name__ == "__main__":
    unittest.main()
