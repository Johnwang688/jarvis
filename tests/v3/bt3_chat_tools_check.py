"""Exercise the owner chat's BT3 tools with a fake desk and model."""

import os
from threading import Event
import unittest
from unittest.mock import Mock, call, patch

import a6c1_acceptance_check as fixtures
from bt3_desk_check import CONTEXT, ITEMS, Service

from jarvis import tools, untrusted
from jarvis.v3 import chat, fastpath, policy
from jarvis.v3.browser.desk import BrowserDesk, ResolvedLink
from jarvis.v3.browser.session import BrowserSession, Landing, Location
from jarvis.v3.daemon import Daemon
from jarvis.v3.logins import Login, LoginError, Store
from jarvis.v3.voice.approving import VoiceApproving


URL = "https://example.invalid/path"
OWNER_URL = "https://example.invalid/page"
NEXT = "https://other.example.invalid/next"
PAGE = "Site: https://example.invalid. Text was not cut.\n" + untrusted.fence(
    "Page title\n[1] link: Next\n\nOutside text", "https://example.invalid"
)
TAINT_NOTE = "This chat has read outside text. Jarvis wants to open an address you did not supply in this turn."
LINK = ResolvedLink(NEXT, "https://example.invalid", "https://other.example.invalid")


class OwnerAddressMatcherCheck(unittest.TestCase):
    def match(self, address, text):
        return fastpath._matches_owner_address(address, [text])

    def test_a_wrapped_address_is_exactly_what_was_typed_inside(self):
        typed = "open <https://example.invalid/page?q=value!> please"
        self.assertTrue(self.match("https://example.invalid/page?q=value!", typed))
        self.assertFalse(self.match("https://example.invalid/page?q=value", typed))
        self.assertTrue(self.match("https://example.invalid/a.", 'see "https://example.invalid/a.".'))
        self.assertFalse(self.match("https://example.invalid/a", 'see "https://example.invalid/a.".'))

    def test_an_unwrapped_word_means_itself_or_itself_without_sentence_punctuation(self):
        typed = "is it https://example.invalid/page?q=value! or (https://example.invalid/other)."
        for address in ("https://example.invalid/page?q=value!", "https://example.invalid/page?q=value",
                        "https://example.invalid/other"):
            self.assertTrue(self.match(address, typed), address)
        for address in ("https://example.invalid/page", "https://example.invalid/page?q=value!!",
                        "https://example.invalid/other)x", "https://example.invalid.evil.invalid/other"):
            self.assertFalse(self.match(address, typed), address)


class BrowserChatFixture(fixtures.Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp)}))
        self.enterContext(patch("jarvis.tools.secrets.secret_values", return_value=[]))
        self.enterContext(patch("jarvis.tools.secrets._process_env_values", return_value=[]))
        policy.set_auto(self.policy_file, True)
        self.desk = Mock(spec=BrowserDesk)
        self.desk.available.return_value = True
        self.desk.open.return_value = self.desk.read.return_value = self.desk.follow.return_value = PAGE
        self.desk.sign_in.return_value = "The site accepted the form."
        self.desk.describe_login.return_value = {
            "name": "example", "origin": "https://example.invalid",
            "username": "user@example.com", "browser": "chrome",
        }
        self.desk.resolve_follow.return_value = LINK
        self.desk.crosses_into_saved_site.return_value = False

    def talk_browser(self, *steps, **options):
        self.model = fixtures.Model(*steps)
        return self.service(self.model, **({"approvals": self.broker, "browser_desk": self.desk} | options))

    def tainted_chat(self, *steps, earlier_input="Read the fixture.", earlier_reply="Read it."):
        svc = self.talk_browser(fixtures.reply(calls=[("browser_read", {})]),
                                fixtures.reply(earlier_reply), *steps)
        svc.new_session()
        svc.send(earlier_input)
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(svc.snapshot()["outside_text"])
        self.desk.reset_mock()
        return svc


class BrowserChatCheck(BrowserChatFixture):
    def deny_open(self, why="hard:tainted"):
        card = self.next_card()
        level = "L0" if why == "auto-off" else "L2"
        self.assertEqual((card["tool"], card["level"], card["why"]), ("browser_open", level, why))
        self.desk.open.assert_not_called()
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "deny"), "denied")

    def test_tainted_chat_opens_the_owners_address(self):
        max_address = OWNER_URL + "x" * (2048 - len(OWNER_URL))
        for token, address in (
            (OWNER_URL, OWNER_URL), (f"<{OWNER_URL}>", OWNER_URL),
            (f'"{OWNER_URL}"', OWNER_URL), (f"'{OWNER_URL}'", OWNER_URL),
            (f"{OWNER_URL}.", OWNER_URL), (f"{OWNER_URL},", OWNER_URL),
            ("example.invalid/page", OWNER_URL), ("example.invalid/page/", OWNER_URL),
            (OWNER_URL, OWNER_URL + "/"), (OWNER_URL + "/", f" {OWNER_URL}\t"),
            (OWNER_URL + "?key=value#part", OWNER_URL + "?key=value#part"),
            (max_address, max_address),
        ):
            with self.subTest(token=token[:100], address=address[:100]):
                svc = self.tainted_chat(fixtures.reply(calls=[("browser_open", {"url": address})]),
                                        fixtures.reply("Done."))
                svc.send(f"Open {token} please.")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.desk.open.assert_called_once_with(url=address)
                self.assertEqual(self.states(None), [])

    def test_tainted_chat_asks_for_altered_addresses(self):
        long_address = OWNER_URL + "x" * (2049 - len(OWNER_URL))
        for token, address in (
            (OWNER_URL, OWNER_URL + "?key=value"), (OWNER_URL, OWNER_URL + "#part"),
            (OWNER_URL + "?key=value", OWNER_URL), (OWNER_URL + "#part", OWNER_URL),
            (OWNER_URL + "?key=value", OWNER_URL + "?key=value/"),
            (OWNER_URL + "#part", OWNER_URL + "#part/"),
            (OWNER_URL, OWNER_URL + "/extra"), (OWNER_URL, OWNER_URL + "//"),
            (OWNER_URL, "https://other.example.invalid/page"),
            (OWNER_URL, "http://example.invalid/page"), (OWNER_URL, "https://example.invalid/Page"),
            (OWNER_URL, "https://example.invalid/p%61ge"),
            ("user@example.invalid/page", "https://user@example.invalid/page"),
            ("https://user@example.invalid/page", "https://user@example.invalid/page"),
            (long_address, long_address),
        ):
            with self.subTest(token=token[:100], address=address[:100]):
                svc = self.tainted_chat(fixtures.reply(calls=[("browser_open", {"url": address})]),
                                        fixtures.reply("Done."))
                svc.send(f"Open {token} please.")
                self.deny_open()
                self.assertEqual(self.wait_idle(svc), "idle")

    def test_tainted_chat_does_not_use_prior_inputs_assistant_text_or_current_page(self):
        for source in ("previous owner input", "assistant reply", "current page"):
            with self.subTest(source=source):
                steps = [fixtures.reply(calls=[("browser_open", {"url": OWNER_URL})]), fixtures.reply("Done.")]
                if source == "current page":
                    self.desk.read.side_effect = [PAGE, PAGE + "\n" + OWNER_URL]
                    steps.insert(0, fixtures.reply(calls=[("browser_read", {})]))
                svc = self.tainted_chat(*steps,
                                        earlier_input=OWNER_URL if source == "previous owner input" else "Read it.",
                                        earlier_reply=OWNER_URL if source == "assistant reply" else "Done reading.")
                svc.send("Open the fixture.")
                self.deny_open()
                self.assertEqual(self.wait_idle(svc), "idle")

    def test_typed_address_still_respects_the_switch_and_stricter_rules(self):
        for why in ("auto-off", "file:0", "blocked"):
            with self.subTest(why=why):
                policy.set_auto(self.policy_file, True)
                svc = self.tainted_chat(fixtures.reply(calls=[("browser_open", {"url": OWNER_URL})]),
                                        fixtures.reply("Done."))
                policy.set_auto(self.policy_file, why != "auto-off")
                if why != "auto-off":
                    policy.set_rule(self.policy_file, "browser.open", "L3" if why == "blocked" else "L2")
                svc.send(OWNER_URL)
                if why != "blocked":
                    self.deny_open(why)
                self.assertEqual(self.wait_idle(svc), "idle")
                self.desk.open.assert_not_called()
                if why == "blocked":
                    self.assertIn("Policy blocks this action.", self.results()[-1])

    def test_steering_counts_only_after_admission_and_queued_input_counts_in_its_turn(self):
        open_address = fixtures.reply(calls=[("browser_open", {"url": OWNER_URL})])
        for steer in (True, False):
            with self.subTest(steer=steer):
                entered, release = Event(), Event()

                def first_step(_messages):
                    entered.set()
                    assert release.wait(10), "test did not release the provider"
                    return open_address

                finish = fixtures.reply("Done.")
                later_steps = (open_address, finish) if steer else (finish, open_address, finish)
                svc = self.tainted_chat(first_step, *later_steps)
                svc.send("Open the fixture.")
                try:
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(svc.send(OWNER_URL, steer=steer)["steered"], steer)
                finally:
                    release.set()
                self.deny_open()
                self.assertEqual(self.wait_idle(svc), "idle")
                self.desk.open.assert_called_once_with(url=OWNER_URL)

    def test_fact_is_code_computed_for_browser_open_only(self):
        explain_with_auto = policy.Policy.explain_with_auto
        seen = []

        def explain(instance, facts):
            seen.append(facts)
            return explain_with_auto(instance, facts)

        svc = self.tainted_chat(fixtures.reply(calls=[
            ("browser_read", {}), ("browser_follow", {"number": 1}), ("browser_sign_in", {"name": "example"}),
            ("browser_open", {"url": NEXT, "owner_address": True}), ("browser_open", {"url": OWNER_URL}),
            ("vault_add", fixtures.ADD),
        ]), fixtures.reply("Done."))
        with patch.object(policy.Policy, "explain_with_auto", explain):
            svc.send(OWNER_URL)
            card = self.next_card()
            self.assertEqual(card["tool"], "vault_add")
            self.assertEqual(self.broker.answer(card["id"], card["digest"], "deny"), "denied")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.desk.open.assert_called_once_with(url=OWNER_URL)
        self.assertIn("Error: arguments contain an unknown field.", self.results())
        self.assertEqual([f["kind"] for f in seen if "owner_address" in f], ["browser.open"])
        self.assertIs(next(f["owner_address"] for f in seen if f["kind"] == "browser.open"), True)

    def test_phone_input_can_open_its_own_address_after_outside_text(self):
        svc = self.tainted_chat(fixtures.reply(calls=[("browser_open", {"url": OWNER_URL})]),
                                fixtures.reply("Done."))
        svc.send(OWNER_URL, steer=False, phone=True)
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIn("browser_open", self.model.seen[-1]["tools"])
        self.desk.open.assert_called_once_with(url=OWNER_URL)
        self.assertEqual(self.states(None), [])

    def test_offer_on_either_surface_with_an_available_desk_and_broker(self):
        for browser, ready, broker, phone in (
            (None, True, self.broker, False), (self.desk, False, self.broker, False),
            (self.desk, True, None, False), (self.desk, True, self.broker, True),
            (self.desk, True, self.broker, False),
        ):
            with self.subTest(ready=ready, broker=broker is not None, phone=phone):
                self.desk.available.return_value = ready
                svc = self.talk_browser(fixtures.reply("Done."), approvals=broker, browser_desk=browser)
                visible = chat.BROWSER_GATED if ready and browser is not None and broker is not None else set()
                self.assertEqual(chat.BROWSER_GATED & set(svc.snapshot()["tools"]), visible)
                svc.send("Fixture.", phone=phone)
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(chat.BROWSER_GATED & set(self.model.seen[0]["tools"]), visible)
        self.assertFalse((chat.BROWSER_GATED - chat.BROWSER_ACTIONS) & set(tools.default_names()))
        self.assertFalse(chat.BROWSER_GATED & tools.PARALLEL_SAFE)

    def test_open_taints_before_the_next_approval_in_the_same_batch(self):
        dirty_url = NEXT + "\u200b"
        svc = self.talk_browser(fixtures.reply(calls=[
            ("browser_open", {"url": URL}), ("browser_read", {}), ("browser_follow", {"number": 1}),
            ("browser_sign_in", {"name": "example"}), ("browser_open", {"url": dirty_url}),
        ]), fixtures.reply("Done."))
        svc.send("Browse the fixture.")
        card = self.next_card()
        self.assertEqual((card["tool"], card["level"], card["why"]), ("browser_open", "L2", "hard:tainted"))
        self.assertEqual(card["card"]["rows"], [{"label": "URL", "value": NEXT},
                                               {"label": "Reason", "value": TAINT_NOTE}])
        self.assertEqual(self.desk.open.call_args_list, [call(url=URL)])
        self.desk.read.assert_called_once_with()
        self.desk.follow.assert_called_once_with(number=1, expected=LINK)
        self.desk.sign_in.assert_called_once_with(name="example", expected=self.desk.describe_login.return_value)
        self.assertTrue(svc.snapshot()["outside_text"])
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "deny"), "denied")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.desk.open.assert_called_once_with(url=URL)
        self.assertEqual(self.results()[:4], [PAGE, PAGE, PAGE, "The site accepted the form."])
        self.assertIn("declined", self.results()[-1])
        self.assertEqual(self.states(None), ["pending", "denied"])
        tool_events = [event for event in svc.events(0)["events"] if event["kind"] == "tool_end"]
        self.assertTrue(tool_events)
        self.assertTrue(all("Outside text" not in event["summary"] for event in tool_events))

    def test_browser_reads_and_follows_taint_and_sign_in_leaves_the_turn_clean(self):
        for name, args, tainted in (("browser_sign_in", {"name": "example"}, False),
                                    ("browser_read", {}, True), ("browser_follow", {"number": 1}, True)):
            with self.subTest(name=name):
                svc = self.talk_browser(fixtures.reply(calls=[(name, args)]), fixtures.reply("Done."))
                svc.new_session()
                svc.send("Fixture.")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(svc.snapshot()["outside_text"], tainted)
                self.assertEqual(self.broker.pending(), [])

    def test_follow_read_and_sign_in_stay_automatic_after_other_outside_text(self):
        svc = self.talk_browser(fixtures.reply(calls=[
            ("vault_list", {}), ("browser_read", {}), ("browser_follow", {"number": 1}),
            ("browser_sign_in", {"name": "example"}), ("browser_open", {"url": NEXT}),
        ]), fixtures.reply("Done."))
        svc.send("Browse after reading.")
        card = self.next_card()
        self.assertEqual(card["tool"], "browser_open")
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.desk.open.assert_called_once_with(url=NEXT)
        self.assertEqual(self.states(None), ["pending", "approved"])

    def test_higher_browser_policy_still_asks_and_all_browser_cards_are_click_only(self):
        voice = VoiceApproving(self.broker, clock=lambda: 0)
        for name in chat.BROWSER_GATED:
            self.assertFalse(voice.ask({"tool": name, "id": "fixture", "digest": "fixture"}))
        policy.set_rule(self.policy_file, "browser.read", "L2")
        svc = self.talk_browser(fixtures.reply(calls=[("browser_read", {})]), fixtures.reply("Done."))
        svc.send("Read the fixture.")
        card = self.next_card()
        self.assertEqual((card["tool"], card["level"]), ("browser_read", "L2"))
        self.assertFalse(voice.ask(card))
        voice.prompt_finished(card["id"], 0)
        self.assertTrue(voice.heard("yes"))
        self.assertTrue(voice.take_needs_click())
        self.assertIsNotNone(voice.pending())
        self.desk.read.assert_not_called()
        voice.click(card["id"], card["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.desk.read.assert_called_once_with()

    def test_twelve_attempts_include_invalid_calls_and_limit_resets_next_turn(self):
        attempts = [("browser_follow", {"number": "1"})] + [("browser_read", {})] * 12
        svc = self.talk_browser(fixtures.reply(calls=attempts), fixtures.reply("Done."),
                                fixtures.reply(calls=[("browser_read", {})]), fixtures.reply("Done."))
        svc.send("Browse the fixture.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.desk.read.call_count, 11)
        self.assertEqual(self.results()[-1], fastpath._BROWSER_TURN_ERROR)
        self.assertEqual(self.states(None), [])
        svc.send("Read again.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.desk.read.call_count, 12)

    def test_auto_off_asks_for_each_browser_tool_including_tainted_actions(self):
        policy.set_auto(self.policy_file, False)
        calls = [("browser_open", {"url": URL}), ("browser_read", {}),
                 ("browser_follow", {"number": 1}), ("browser_sign_in", {"name": "example"})]
        svc = self.talk_browser(fixtures.reply(calls=calls), fixtures.reply("Done."))
        svc.send("Browse the fixture.")
        for name, _ in calls:
            card = self.next_card()
            self.assertEqual((card["tool"], card["why"]), (name, "auto-off"))
            method = getattr(self.desk, name.removeprefix("browser_"))
            method.assert_not_called()
            self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(svc.snapshot()["outside_text"])
        self.assertEqual(self.states(None), ["pending", "approved"] * len(calls))

    def test_follow_schema_accepts_one_integer_and_dispatch_refuses_extra_fields(self):
        svc = self.talk_browser(fixtures.reply(calls=[
            ("browser_follow", {"number": 1, "address": NEXT}),
            ("browser_follow", {"number": True}),
        ]), fixtures.reply("Done."))
        svc.send("Browse the fixture.")
        self.assertEqual(self.wait_idle(svc), "idle")
        schema = tools.REGISTRY["browser_follow"].schema
        self.assertEqual(set(schema["properties"]), {"number"})
        self.assertEqual(schema["properties"]["number"]["type"], "integer")
        self.assertEqual(schema["required"], ["number"])
        self.assertEqual(self.results(), ["Error: arguments contain an unknown field.",
                                          "Error: invalid value for argument number."])
        self.desk.resolve_follow.assert_not_called()
        self.desk.follow.assert_not_called()

    def follow_real_desk(self, address, *, store_failure=False, owner_input="Follow the fixture link."):
        session = Mock(spec=BrowserSession)
        session.location.return_value = Location(URL, "frame", "loader")
        session.is_current.return_value = True
        session.read_call.return_value = (dict(title="Fixture", text="Outside text", items=ITEMS, more=False),
                                          CONTEXT)
        session.follow_call.return_value = address
        session.open.return_value = Landing(address, "loaded")
        store = Mock(spec=Store)
        store.list.return_value = [
            Login("example", "https://login.example.invalid", "user@example.com", "chrome"),
            Login("other", "https://login.other.invalid", "user@example.com", "chrome"),
        ]
        if store_failure:
            store.list.side_effect = LoginError("Fixture store unavailable.")
        desk = BrowserDesk(Service(session), store_factory=lambda: store, feed_post=Mock())
        self.enterContext(patch.object(desk, "available", return_value=True))
        svc = self.talk_browser(fixtures.reply(calls=[("browser_read", {}), ("browser_follow", {"number": 2})]),
                                fixtures.reply("Done."), browser_desk=desk)
        svc.new_session()
        svc.send(owner_input)
        return svc, session, store

    def test_follow_is_automatic_for_same_origin_same_site_and_unsaved_sites(self):
        for address in (URL, NEXT, "https://unsaved.invalid/path"):
            with self.subTest(address=address):
                svc, session, store = self.follow_real_desk(address)
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertTrue(svc.snapshot()["outside_text"])
                self.assertEqual(self.states(None), [])
                self.assertEqual(session.follow_call.call_args_list, [call(CONTEXT, 2)] * 2)
                session.open.assert_called_once_with(address)
                self.assertEqual(store.list.call_count, int(address.startswith("https://unsaved.invalid")))
                store.secret_for.assert_not_called()

    def test_cross_site_saved_login_uses_open_policy_and_binds_the_full_address(self):
        address = "https://page.other.invalid/approved"
        for landing in (address, "https://page.other.invalid/changed"):
            with self.subTest(landing=landing):
                svc, session, store = self.follow_real_desk(address, owner_input=f"Follow the link to {address}")
                card = self.next_card()
                self.assertEqual((card["tool"], card["level"], card["why"]),
                                 ("browser_follow", "L2", "hard:tainted"))
                self.assertEqual(card["card"]["rows"], [
                    {"label": "Link number", "value": "2"},
                    {"label": "Address", "value": address, "source": "page"},
                    {"label": "From", "value": "https://example.invalid", "source": "page"},
                    {"label": "Destination", "value": "page.other.invalid"},
                    {"label": "Reason", "value": "A page on example.invalid links to page.other.invalid, "
                                                 "where Jarvis has a saved login."},
                ])
                session.follow_call.assert_called_once_with(CONTEXT, 2)
                session.open.assert_not_called()
                session.follow_call.return_value = landing
                self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(session.follow_call.call_args_list, [call(CONTEXT, 2)] * 2)
                if address == landing:
                    session.open.assert_called_once_with(address)
                else:
                    session.open.assert_not_called()
                    self.assertEqual(self.results()[-1], "That item is not a link on the current page.")
                store.secret_for.assert_not_called()

    def test_cross_site_saved_login_never_loosens_the_owners_rule_for_following(self):
        # The saved-site branch judges the call as an opened address. An owner
        # rule that blocks following must still block it.
        policy.set_rule(self.policy_file, "browser.follow", "L3")
        svc, session, store = self.follow_real_desk("https://page.other.invalid/approved")
        self.assertEqual(self.wait_idle(svc), "idle")
        session.open.assert_not_called()
        self.assertIn("Policy blocks this action.", self.results()[-1])

    def test_cross_site_follow_asks_when_saved_login_store_cannot_be_read(self):
        svc, session, store = self.follow_real_desk("https://unsaved.invalid/path", store_failure=True)
        card = self.next_card()
        self.assertEqual((card["tool"], card["level"]), ("browser_follow", "L2"))
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "deny"), "denied")
        self.assertEqual(self.wait_idle(svc), "idle")
        session.open.assert_not_called()
        store.list.assert_called_once_with()
        store.secret_for.assert_not_called()

    def test_follow_resolution_refusal_returns_fixed_sentence_without_asking(self):
        svc, session, _ = self.follow_real_desk(None)
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results()[-1], "That item is not a link on the current page.")
        self.assertEqual(self.states(None), [])
        session.open.assert_not_called()

    def test_daemon_constructs_a_lazy_desk_and_adapts_the_feed_callback(self):
        service = Mock()
        feed = Mock()
        with patch.object(Daemon, "_initiative", return_value=(feed, Mock())):
            daemon = Daemon(data_dir=self.v3, browser=service, jobs=(), phone_host=None)
            self.addCleanup(daemon.stop)
            browser = daemon._chat._browser_desk
            self.assertIs(browser._service, service)
            self.assertEqual(service.mock_calls, [])
            daemon._post_browser_feed(actor="jarvis", kind="news", rank=4,
                                      ask="Jarvis used the saved login for example.invalid.", idempotency_key="fixture")
        feed.post.assert_called_once_with(
            {"kind": "news", "rank": 4, "ask": "Jarvis used the saved login for example.invalid.",
             "idempotency_key": "fixture"}, actor="jarvis",
        )


if __name__ == "__main__":
    unittest.main()
