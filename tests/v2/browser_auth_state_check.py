"""Bounded owner-auth data and timing tests; no Chrome, network or real secrets."""
from pathlib import Path
import secrets
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.auth_state import (
    AuthBudget, AuthRefused, NavigationClock, challenge, credential_fields, origin,
)

# Dummy test data only; these constants do not identify any real account.
FAKE_USER = 'name'
FAKE_SECRET = 'word'
FAKE_USERINFO_URL = f"http://{FAKE_USER}:{FAKE_SECRET}@example.test"


class AuthState(unittest.TestCase):
    def test_origin_matches_scheme_host_and_effective_port_without_path_or_query(self):
        requested = origin("https://EXAMPLE.test:443/private?not-for-display")
        parsed = challenge(requested, {"source":"Server", "scheme":"Basic", "realm":"fixture", "origin":"https://example.test"})
        self.assertEqual(parsed.origin.display, "https://example.test")
        self.assertEqual(parsed.scheme, "basic")
        for changed in ("http://example.test", "https://other.test", "https://example.test:444", "https://example.test/path", "https://example.test/?query"):
            with self.assertRaises(AuthRefused):
                challenge(requested, {"source":"Server", "scheme":"Basic", "realm":"fixture", "origin":changed})

    def test_policy_refusals_and_proxy_unknown_malformed_challenges_never_prompt(self):
        for value in ("about:blank", "file:///tmp/file", FAKE_USERINFO_URL, "http://127.0.0.1:8402", "https://example.test/\x00", None):
            with self.assertRaises(AuthRefused):origin(value)
        with self.assertRaises(AuthRefused):origin("http://localhost:18739", refused=frozenset({18739}))
        requested=origin("https://example.test")
        normal={"source":"Server", "scheme":"Digest", "realm":"fixture", "origin":"https://example.test"}
        for delta in ({"source":"Proxy"},{"scheme":"NTLM"},{"realm":None},{"realm":"x"*4097},{"origin":""}):
            with self.assertRaises(AuthRefused):challenge(requested,{**normal,**delta})
        self.assertEqual(challenge(requested,{**normal,"realm":"untrusted\n"+"x"*300}).realm.count("\n"),0)
        self.assertLessEqual(len(challenge(requested,{**normal,"realm":"x"*300}).realm),200)

    def test_fields_use_utf8_byte_bounds_and_fixed_refusals_without_values(self):
        generated=secrets.token_urlsafe(24)
        self.assertEqual(credential_fields("u"*1024,"s"*4096),("u"*1024,"s"*4096))
        self.assertEqual(credential_fields("",generated),("",generated))
        for name,value in (("u"*1025,generated),("", "s"*4097),("😀"*257,generated),("", "😀"*1025),("",generated+"\n"),("",generated+"\u202e"),(None,generated)):
            with self.assertRaises(AuthRefused) as refusal:credential_fields(name,value)
            self.assertNotIn(generated,str(refusal.exception))

    def test_repeated_request_or_reconnect_cannot_restart_original_sixty_seconds(self):
        auth=AuthBudget();self.assertTrue(auth.announce(100));self.assertTrue(auth.announce(150))
        self.assertTrue(auth.submit(159.999));self.assertEqual(auth.started,100)
        self.assertFalse(auth.announce(160));self.assertFalse(auth.submit(160))
        self.assertEqual(auth.submissions,1)

    def test_three_submissions_shared_across_challenges_and_new_operation_only_resets(self):
        auth=AuthBudget();self.assertFalse(auth.submit(0));self.assertTrue(auth.announce(1))
        for when in (2,10,30):self.assertTrue(auth.submit(when))
        self.assertFalse(auth.announce(31));self.assertFalse(auth.submit(31));self.assertEqual(auth.submissions,3)
        new=AuthBudget();self.assertTrue(new.announce(32));self.assertTrue(new.submit(33));self.assertEqual(new.submissions,1)

    def test_normal_navigation_still_has_exact_twenty_seconds(self):
        clock=NavigationClock(20,100,AuthBudget())
        self.assertEqual(clock.remaining(105),15);self.assertAlmostEqual(clock.remaining(119.999),.001)
        self.assertEqual(clock.remaining(120),0)

    def test_multiple_auth_intervals_do_not_reset_twenty_seconds_of_actual_work(self):
        auth=AuthBudget();clock=NavigationClock(20,0,auth)
        auth.announce(8);clock.set_paused(True,8);self.assertEqual(clock.remaining(28),40)
        clock.set_paused(False,28);self.assertEqual(clock.remaining(34),6)
        auth.announce(34);clock.set_paused(True,34);self.assertEqual(clock.remaining(52),16)
        clock.set_paused(False,52);self.assertAlmostEqual(clock.remaining(57.999),.001)
        self.assertEqual(clock.remaining(58),0);self.assertEqual(auth.started,8)

    def test_original_auth_expiry_wins_even_with_non_auth_budget_left(self):
        auth=AuthBudget();clock=NavigationClock(20,0,auth)
        auth.announce(2);clock.set_paused(True,2);self.assertAlmostEqual(clock.remaining(61.999),.001)
        self.assertEqual(clock.remaining(62),0)
        clock.set_paused(False,62);self.assertEqual(clock.remaining(62),0)

    def test_bad_or_backward_clocks_do_not_create_time(self):
        for now in (float("nan"),float("inf"),99):
            auth=AuthBudget();auth.announce(100)
            self.assertFalse(auth.announce(now));self.assertFalse(auth.submit(now))
            clock=NavigationClock(20,100,auth)
            self.assertEqual(clock.remaining(now),0)


if __name__ == "__main__":unittest.main()
