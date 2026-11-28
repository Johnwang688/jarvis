"""Pin the BT10 decision table and exercise phone chat through a fake browser."""

from contextlib import nullcontext
from dataclasses import replace
from itertools import product
from threading import Event
import unittest
from unittest.mock import Mock, patch

from bt3_chat_tools_check import BrowserChatFixture, LINK, OWNER_URL, PAGE, URL, fixtures
from bt3_desk_check import Service
from bt4_chat_tools_check import CLICK, TYPING
from bt4_desk_check import description
from jarvis import tools
from jarvis.v3 import chat, fastpath, pc, policy
from jarvis.v3.browser import desk, signin
from jarvis.v3.browser.session import BrowserSession, Location
from jarvis.v3.logins import Login, Store
from jarvis.v3.voice.approving import VoiceApproving


OPEN = ("browser_open", {"url": URL})
READ = ("browser_read", {})
FOLLOW = ("browser_follow", {"number": 1})
SIGN_IN = ("browser_sign_in", {"name": "example"})
LOGIN = Login("example", "https://example.invalid", "user@example.com", "edge")
TYPED = "fixture text for the saved-login form"

# Desk expectations and blanket phone refusal from origin/main bb0d279ab13ff619c082b5dab76e87032ed1ba9d.
# Columns are clean/on, clean/off, outside text/on, outside text/off.
RUN, OFF, ASK, TAINT, PHONE = "runs", "auto-off", "policy ask", "hard:tainted", "phone"
REFUSED = "refused"
DESK = (
    (READ, "browser.read", (RUN, OFF, RUN, OFF)),
    (FOLLOW, "browser.follow", (RUN, OFF, RUN, OFF)),
    (CLICK, "browser.follow", (RUN, OFF, RUN, OFF)),
    (OPEN, "browser.open", (RUN, OFF, TAINT, TAINT)),
    (SIGN_IN, "browser.sign_in", (RUN, OFF, RUN, OFF)),
    (CLICK, "browser.click", (ASK, ASK, TAINT, TAINT)),
    (CLICK, "browser.submit", (ASK, ASK, TAINT, TAINT)),
    (TYPING, "browser.type", (ASK, ASK, TAINT, TAINT)),
)


class PhoneBrowserCheck(BrowserChatFixture):
    def decision(self, call, kind, *, phone, outside, auto, owner_texts=(), saved=False):
        policy.set_auto(self.policy_file, auto)
        tool, args = call
        self.desk.is_read_native_link.return_value = tool == "browser_press" and kind == "browser.follow"
        item_kind = {"browser.submit": "submit", "browser.type": "field"}.get(kind, "control")
        self.desk.describe.return_value = {
            **description(item_kind), "origin": "https://example.invalid", "label": "Fixture label",
        }
        self.desk.action_refusal.return_value = None
        self.desk.crosses_into_saved_site.return_value = saved
        turn = fastpath._TurnTools(
            "fixture-session", "fixture-turn", self.tmp, self.tmp, lambda: 0,
            lambda: nullcontext(True), lambda: False, lambda: None, lambda: outside, lambda: None,
            browser_desk=self.desk, phone_input=lambda: phone, owner_texts=lambda: owner_texts,
        )
        broker = Mock()
        broker.ask.return_value = False
        approver = fastpath.ChatApprover(broker, turn)
        registration = Mock()
        registration.name = tool
        result = approver(registration, args)
        broker.auto.assert_not_called()
        if result is True:
            self.assertIsNotNone(turn.grant)
            broker.ask.assert_not_called()
            return RUN
        self.assertIsNone(turn.grant)
        if isinstance(result, tools.Refusal):
            self.assertEqual(result.reason, "Policy blocks this action.")
            return REFUSED
        self.assertIs(result, False)
        broker.ask.assert_called_once()
        asked = broker.ask.call_args.kwargs
        verdict = asked["verdict"]
        if kind in {"browser.click", "browser.submit", "browser.type"}:
            self.assertEqual(verdict, {"level": "L2", "why": f"default:{kind}"})
        return asked["display_why"] or (TAINT if verdict["why"] == TAINT else ASK)

    def test_before_and_after_decision_table(self):
        for call, kind, before_desk in DESK:
            for phone, (index, (outside, auto)) in product(
                (False, True), enumerate(product((False, True), (True, False))),
            ):
                with self.subTest(kind=kind, phone=phone, outside=outside, auto=auto):
                    before = REFUSED if phone else before_desk[index]
                    after = PHONE if phone and call == SIGN_IN else before_desk[index]
                    actual = self.decision(call, kind, phone=phone, outside=outside, auto=auto)
                    self.assertEqual(actual, after, f"before BT10: {before}")

    def test_typed_address_saved_site_and_explicit_policy_on_both_surfaces(self):
        for phone in (False, True):
            with self.subTest(phone=phone):
                self.assertEqual(self.decision(
                    OPEN, "browser.open", phone=phone, outside=True, auto=True, owner_texts=(URL,),
                ), RUN)
                self.assertEqual(self.decision(
                    FOLLOW, "browser.follow", phone=phone, outside=True, auto=True, saved=True,
                ), TAINT)
        for tool, kind, level, expected in (
            (OPEN, "browser.open", "L2", ASK),
            (SIGN_IN, "browser.sign_in", "L2", ASK),
            (SIGN_IN, "browser.sign_in", "L3", REFUSED),
        ):
            policy.set_rule(self.policy_file, kind, level)
            for phone in (False, True):
                with self.subTest(kind=kind, level=level, phone=phone):
                    self.assertEqual(self.decision(
                        tool, kind, phone=phone, outside=False, auto=True,
                    ), expected)

    def test_phone_opens_reads_and_follows_without_a_card_and_pc_tools_are_refused(self):
        pc_calls = [("link_open", {"url": OWNER_URL}), ("app_open", {"name": "Chrome"}),
                    ("folder_open", {"project": "Fixture"})]
        svc = self.talk_browser(fixtures.reply(calls=[OPEN, READ, FOLLOW, *pc_calls]), fixtures.reply("Done."))
        with patch.object(fastpath, "_pc_preview") as pc_preview:
            svc.send("Browse the fixture.", steer=False, phone=True)
            self.assertEqual(self.wait_idle(svc), "idle")
        pc_preview.assert_not_called()
        self.assertTrue(chat.BROWSER_GATED <= set(self.model.seen[0]["tools"]))
        self.desk.open.assert_called_once_with(url=URL)
        self.desk.read.assert_called_once_with()
        self.desk.follow.assert_called_once_with(number=1, expected=LINK)
        self.assertEqual(self.results(), [PAGE] * 3 + [f"Refused: {pc.PHONE_REFUSAL} (the user was not asked)."] * 3)
        self.assertEqual(self.states(None), [])

    def test_link_cards_show_the_entire_bound_address(self):
        address = LINK.address + "?q=" + "x" * 1600 + "#fixture"
        link = LINK._replace(address=address)
        self.desk.resolve_follow.return_value = link
        for phone, (tool, args) in product((False, True), (FOLLOW, CLICK)):
            with self.subTest(phone=phone, tool=tool):
                self.desk.is_read_native_link.return_value = tool == "browser_press"
                self.desk.follow.reset_mock()
                policy.set_rule(self.policy_file, "browser.follow", "L2")
                svc = self.talk_browser(fixtures.reply(calls=[(tool, args)]), fixtures.reply("Done."))
                svc.new_session()
                svc.send("Follow the fixture link.", phone=phone)
                card = self.next_card()
                self.assertEqual(card["card"]["rows"], [
                    {"label": "Link number", "value": str(args["number"])},
                    {"label": "Address", "value": link.address, "source": "page"},
                    {"label": "From", "value": link.source_origin, "source": "page"},
                ])
                self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.desk.follow.assert_called_once_with(**args, expected=link)
                svc.close()

    def test_unshowable_link_address_refuses_before_asking(self):
        policy.set_auto(self.policy_file, False)
        for suffix in ("\t", "\n", "\x7f", "\u202e", "x" * fastpath.BROWSER_URL_LIMIT):
            with self.subTest(suffix=suffix[:10]):
                self.desk.resolve_follow.return_value = LINK._replace(address=LINK.address + suffix)
                self.desk.is_read_native_link.return_value = True
                svc = self.talk_browser(fixtures.reply(calls=[FOLLOW, CLICK]), fixtures.reply("Done."))
                svc.new_session()
                svc.send("Follow the fixture link.")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.results(), [
                    "Refused: The browser destination cannot be shown safely. (the user was not asked).",
                ] * 2)
                self.assertEqual(self.states(None), [])
                self.desk.follow.assert_not_called()
                self.desk.click.assert_not_called()
                svc.close()

    def test_missing_saved_login_refuses_before_a_card_on_both_surfaces(self):
        # Intentionally changes old desk behavior with auto off: a missing login
        # cannot be used, so there is no action for the owner to approve.
        policy.set_auto(self.policy_file, False)
        store = Mock(spec=Store)
        store.list.return_value = []
        service = Service(Mock(spec=BrowserSession))
        browser = desk.BrowserDesk(service, store_factory=lambda: store, feed_post=Mock())
        with patch.object(browser, "available", return_value=True):
            for phone in (False, True):
                with self.subTest(phone=phone):
                    svc = self.talk_browser(fixtures.reply(calls=[SIGN_IN]), fixtures.reply("Done."),
                                            browser_desk=browser)
                    svc.new_session()
                    svc.send("Use the saved fixture login.", phone=phone)
                    self.assertEqual(self.wait_idle(svc), "idle")
                    self.assertEqual(self.results()[-1], "Error: The saved login could not be used.")
                    self.assertEqual(self.states(None), [])
                    self.assertEqual(service.leases, [])
                    svc.close()
        store.secret_for.assert_not_called()

    def test_automatic_desk_sign_in_requires_a_new_request_after_phone_steering(self):
        granted, release = Event(), Event()
        browser_call = fastpath._browser_call

        def pause_after_grant(tool, **args):
            grant = fastpath._TURN.get().grant
            if not grant.owner_answered:
                granted.set()
                self.assertTrue(release.wait(5))
            return browser_call(tool, **args)

        sign_in_step = fixtures.reply(calls=[SIGN_IN])
        svc = self.talk_browser(sign_in_step, sign_in_step, fixtures.reply("Done."))
        with patch.object(fastpath, "_browser_call", side_effect=pause_after_grant):
            svc.send("Use the saved fixture login.")
            try:
                self.assertTrue(granted.wait(5))
                self.assertTrue(svc.send("Continue from the phone.", phone=True)["steered"])
            finally:
                release.set()
            card = self.next_card()
            self.assertEqual((card["tool"], card["why"]), ("browser_sign_in", "phone"))
            self.desk.sign_in.assert_not_called()
            self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results(), [
            "Refused: This is now a phone turn. Request the browser sign-in again.",
            self.desk.sign_in.return_value,
        ])
        self.desk.sign_in.assert_called_once_with(name="example", expected=self.desk.describe_login.return_value)

    def test_carried_over_phone_steer_keeps_the_followup_turn_on_phone(self):
        entered, release = Event(), Event()

        def finish_desk_turn(_messages):
            entered.set()
            self.assertTrue(release.wait(5))
            return fixtures.reply("Finished the desk turn.")

        svc = self.talk_browser(finish_desk_turn, fixtures.reply(calls=[SIGN_IN]), fixtures.reply("Done."))
        svc.send("Start at the desk.")
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(svc.send("Use the saved fixture login.", phone=True)["steered"])
        finally:
            release.set()
        card = self.next_card()
        self.assertEqual((card["tool"], card["why"]), ("browser_sign_in", "phone"))
        self.desk.sign_in.assert_not_called()
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "deny"), "denied")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.desk.sign_in.assert_not_called()

    def test_phone_sign_in_waits_for_approval_and_keeps_the_feed_notice(self):
        for decision in ("deny", "replace", "approve"):
            with self.subTest(decision=decision):
                session, store, feed = Mock(spec=BrowserSession), Mock(spec=Store), Mock()
                service = Service(session)
                location = Location(LOGIN.origin + "/sign-in", "frame", "document")
                session.location.return_value = location
                session.document_call.side_effect = [{"result": "both"}, {"result": "submitted"}, {"result": "none"}]
                store.list.return_value = [LOGIN]
                store.secret_for.return_value = LOGIN, TYPED
                browser = desk.BrowserDesk(service, store_factory=lambda: store, feed_post=feed, clock=lambda: 0)
                with patch.object(browser, "available", return_value=True), patch.object(
                    signin, "_settle", return_value=location,
                ):
                    svc = self.talk_browser(fixtures.reply(calls=[SIGN_IN]), fixtures.reply("Done."),
                                            browser_desk=browser)
                    svc.new_session()
                    svc.send("Sign in to the fixture.", steer=False, phone=True)
                    card = self.next_card()
                    self.assertEqual((card["tool"], card["level"], card["why"]), ("browser_sign_in", "L1", "phone"))
                    self.assertEqual(card["card"], {"title": "Sign in to a saved site", "rows": [
                        {"label": "Saved login", "value": LOGIN.name},
                        {"label": "Site", "value": LOGIN.origin},
                        {"label": "Browser", "value": LOGIN.browser},
                    ]})
                    self.assertFalse(VoiceApproving(self.broker, clock=lambda: 0).ask(card))
                    self.assertEqual(service.leases, [])
                    store.secret_for.assert_not_called()
                    if decision == "replace":
                        store.list.return_value = [replace(LOGIN, origin="https://other.example.invalid")]
                    answer = "deny" if decision == "deny" else "approve"
                    self.assertEqual(self.broker.answer(card["id"], card["digest"], answer),
                                     "denied" if answer == "deny" else "approved")
                    self.assertEqual(self.wait_idle(svc), "idle")
                if decision == "approve":
                    self.assertEqual(service.leases, ["edge"])
                    store.secret_for.assert_called_once_with(LOGIN.name, location.url)
                    feed.assert_called_once()
                    self.assertEqual(feed.call_args.kwargs["ask"], "Jarvis used the saved login for example.invalid.")
                else:
                    self.assertEqual(service.leases, [])
                    store.secret_for.assert_not_called()
                    feed.assert_not_called()
                self.assertNotIn(TYPED, self.log.read_text())
                self.assertTrue(all(TYPED not in result for result in self.results()))


if __name__ == "__main__":
    unittest.main()
