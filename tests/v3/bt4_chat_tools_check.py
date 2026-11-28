"""Owner-chat click and type through the real approver, desk and fake CDP session."""

import unittest
from unittest.mock import Mock, patch

import bt3_chat_tools_check as bt3
from bt4_desk_check import description
from jarvis import tools
from jarvis.tools import browsing
from jarvis.v3 import chat, fastpath, policy
from jarvis.v3.browser import act, desk
from jarvis.v3.browser.session import BrowserSession, Landing, Location
from jarvis.v3.logins import Login, Store
from jarvis.v3.voice.approving import VoiceApproving


TYPED = "  first line\n" + "Keep this complete. " * 90
CLICK = ("browser_press", {"number": 1})
TYPING = ("browser_fill", {"number": 1, "text": TYPED, "submit": True})


class ActionChat(bt3.BrowserChatFixture):
    def browser(self, *, kind="control", saved=False, follow=bt3.NEXT,
                saved_origin="https://login.example.invalid", items=bt3.ITEMS, **fields):
        self.target = description(kind, **fields)
        self.session = Mock(spec=BrowserSession)
        self.session.location.return_value = Location(bt3.URL, "frame", "loader")
        self.session.is_current.return_value = True
        self.session.read_call.return_value = (dict(title="Fixture", text="Outside text",
                                                   items=items, more=False), bt3.CONTEXT)
        self.session.follow_call.return_value = follow
        self.session.open.return_value = Landing(bt3.NEXT, "loaded")
        self.acted = []

        def run(context_id, script, args):
            self.assertEqual(context_id, bt3.CONTEXT)
            if script is act.DESCRIBE:
                return self.target.copy()
            self.acted.append((script, args))
            return "done"

        self.session.act_call.side_effect = run
        self.store = Mock(spec=Store)
        self.store.list.return_value = (
            [Login("fixture", saved_origin, "user@example.com", "chrome")] if saved else []
        )
        browser = desk.BrowserDesk(bt3.Service(self.session), store_factory=lambda: self.store, feed_post=Mock())
        self.enterContext(patch.object(browser, "available", return_value=True))
        self.enterContext(patch.object(desk, "settle"))
        return browser

    def start(self, *calls, phone=False, auto=True, **fields):
        browser = self.browser(**fields)
        steps = (bt3.fixtures.reply(calls=[("browser_read", {})]), bt3.fixtures.reply("Read it."),
                 bt3.fixtures.reply(calls=calls), bt3.fixtures.reply("Done."))
        svc = self.talk_browser(*steps, browser_desk=browser)
        svc.new_session()
        svc.send("Read the fixture.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(svc.snapshot()["outside_text"])
        policy.set_auto(self.policy_file, auto)
        svc.send("Use the fixture.", phone=phone)
        return svc

    def answer(self, card, action):
        self.assertEqual(self.broker.answer(card["id"], card["digest"], action),
                         "approved" if action == "approve" else "denied")

    def test_controls_and_toggles_ask_regardless_of_saved_login_or_auto_switch(self):
        cases = (
            ("control", False, True, True, "button", "approve"),
            ("control", True, True, False, "other control", "deny"),
            ("toggle", False, True, False, "checkbox or radio", "approve"),
            ("control", False, False, True, "button", "approve"),
        )
        for kind, saved, auto, button_like, label, action in cases:
            with self.subTest(kind=kind, saved=saved, auto=auto):
                policy.set_auto(self.policy_file, True)
                svc = self.start(CLICK, kind=kind, saved=saved, auto=auto, button_like=button_like)
                card = self.next_card()
                self.assertEqual((card["level"], card["why"]), ("L2", "hard:tainted"))
                self.assertEqual(card["card"]["rows"], [
                    {"label": "Site", "value": "https://example.invalid"},
                    {"label": "Button", "value": "button label", "source": "page"},
                    {"label": "Kind", "value": label},
                ])
                self.assertEqual(self.acted, [])
                self.answer(card, action)
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.acted, [(act.CLICK, [1, 1])] if action == "approve" else [])
                self.store.list.assert_not_called()
                self.store.secret_for.assert_not_called()

    def test_submit_asks_with_destination_and_money_fact(self):
        seen = []
        original = policy.Policy.explain_with_auto

        def explain(instance, facts):
            seen.append(facts.copy())
            return original(instance, facts)

        with patch.object(policy.Policy, "explain_with_auto", explain):
            svc = self.start(CLICK, kind="submit", can_submit=True, payment=True, method="post",
                             destination_origin="https://example.invalid:8443")
            card = self.next_card()
            self.assertEqual((card["tool"], card["level"]), ("browser_press", "L2"))
            self.assertEqual(card["card"], {"title": "Submit a browser form", "rows": [
                {"label": "Site", "value": "https://example.invalid"},
                {"label": "Button", "value": "button label", "source": "page"},
                {"label": "Kind", "value": "button"},
                {"label": "Sends to", "value": "https://example.invalid:8443"},
                {"label": "Reason", "value": "This looks like a payment form."},
            ]})
            self.assertEqual(seen[-1], {"kind": "browser.submit", "tainted": True, "money": True})
            self.assertEqual(self.acted, [])
            self.answer(card, "approve")
            self.assertEqual(self.wait_idle(svc), "idle")
            self.assertEqual(self.acted, [(act.CLICK, [1, 1])])

    def test_type_exact_text_and_both_submit_choices(self):
        for submit, origin, then in (
            (False, "https://example.invalid", "the page receives this text; the form is not submitted"),
            (True, "https://example.invalid", "submit the form"),
            (True, "https://example.invalid:8443", "submit the form to https://example.invalid:8443"),
            (True, "http://example.invalid", "submit the form to http://example.invalid"),
        ):
            with self.subTest(submit=submit, origin=origin):
                args = dict(TYPING[1])
                if not submit:
                    del args["submit"]
                svc = self.start(("browser_fill", args), kind="field", textarea=True, can_submit=True,
                                 destination_origin=origin, method="post")
                card = self.next_card()
                self.assertEqual((card["tool"], card["level"]), ("browser_fill", "L2"))
                self.assertEqual(card["card"]["rows"], [
                    {"label": "Site", "value": "https://example.invalid"},
                    {"label": "Field", "value": "button label", "source": "page"},
                    {"label": "Text", "value": TYPED},
                    {"label": "Then", "value": then},
                ])
                self.assertEqual(self.acted, [])
                self.answer(card, "approve" if submit else "deny")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.acted, [(act.TYPE, [1, 1, TYPED, True])] if submit else [])

    def test_card_mutation_refuses_without_acting(self):
        for key, value in (("kind", "control"), ("binding", 2), ("form_has_password", True),
                           ("destination_origin", "https://changed.example.invalid")):
            with self.subTest(key=key):
                svc = self.start(TYPING, kind="field", textarea=True, can_submit=True, method="post",
                                 destination_origin="https://other.example.invalid")
                card = self.next_card()
                self.target[key] = value
                self.answer(card, "approve")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.acted, [])
                self.assertEqual(self.results()[-1], desk._CHANGED)

    def test_fixed_refusals_do_not_ask(self):
        for call, kind, field, result in (
            (CLICK, "submit", "form_has_password", desk._SIGN_IN),
            (TYPING, "field", "form_has_password", desk._SIGN_IN),
            (TYPING, "field", "sensitive", desk._SENSITIVE),
        ):
            with self.subTest(field=field, kind=kind):
                svc = self.start(call, kind=kind, textarea=True, can_submit=True, **{field: True})
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.states(None), [])
                self.assertEqual(self.acted, [])
                self.assertEqual(self.results()[-1], result)

    def test_click_link_reuses_follow_policy_and_stored_address(self):
        svc = self.start(("browser_press", {"number": 2}))
        self.assertEqual(self.wait_idle(svc), "idle")
        self.session.open.assert_called_once_with(bt3.NEXT)
        self.session.act_call.assert_not_called()
        self.assertEqual(self.acted, [])
        self.assertEqual(self.states(None), [])

    def test_link_press_asks_under_follow_rules(self):
        for auto, address in ((True, "https://page.other.invalid/next"), (False, bt3.NEXT)):
            with self.subTest(auto=auto):
                svc = self.start(("browser_press", {"number": 2}), auto=auto, saved=True,
                                 follow=address, saved_origin="https://login.other.invalid")
                card = self.next_card()
                self.assertEqual((card["level"], card["why"]),
                                 ("L2", "hard:tainted") if auto else ("L0", "auto-off"))
                rows = [
                    {"label": "Link number", "value": "2"},
                    {"label": "Address", "value": address, "source": "page"},
                    {"label": "From", "value": "https://example.invalid", "source": "page"},
                ]
                if auto:
                    rows.extend([
                        {"label": "Destination", "value": "page.other.invalid"},
                        {"label": "Reason", "value": "A page on example.invalid links to page.other.invalid, "
                                                     "where Jarvis has a saved login."},
                    ])
                self.assertEqual(card["card"], {"title": "Follow a link on the browser page", "rows": rows})
                self.answer(card, "deny")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.session.open.assert_not_called()

    def test_card_names_the_resolved_submitter(self):
        svc = self.start(CLICK, kind="submit", target_number=3, can_submit=True, method="post",
                         destination_origin="https://example.invalid",
                         items=[*bt3.ITEMS, {"kind": "button", "label": "Native submitter", "labelLines": []}])
        card = self.next_card()
        self.assertEqual(card["card"]["rows"], [
            {"label": "Site", "value": "https://example.invalid"},
            {"label": "Button", "value": "Native submitter", "source": "page"},
            {"label": "Kind", "value": "button"},
        ])
        self.answer(card, "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.acted, [(act.CLICK, [1, 1])])

    def test_fill_and_submit_outside_a_form_is_refused_without_a_card(self):
        svc = self.start(TYPING, kind="field", textarea=True, can_submit=False)
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.states(None), [])
        self.assertEqual(self.acted, [])
        self.assertEqual(self.results()[-1], "That field is not in a form. Fill it, then press the button.")

    def test_browser_actions_use_the_registry_and_keep_the_legacy_tools_intact(self):
        fastpath.register_tools()
        for name in ("browser_click", "browser_type"):
            legacy = tools.REGISTRY[name]
            self.assertIs(legacy.func, getattr(browsing, name))
            self.assertIn("ref", legacy.schema["properties"])
            self.assertIn(name, tools.default_names())
        for name in chat.BROWSER_ACTIONS:
            entry = tools.REGISTRY[name]
            self.assertIs(entry.func, getattr(fastpath, name))
            self.assertTrue(entry.dangerous)
            self.assertEqual(entry.schema["properties"]["number"]["type"], "integer")
            self.assertNotIn("ref", entry.schema["properties"])
            self.assertNotIn(name, tools.default_names())
            self.assertNotIn(name, tools.PARALLEL_SAFE)

    def test_desktop_chat_offers_numbered_actions_without_legacy_browser_tools(self):
        svc = self.start(CLICK)
        self.answer(self.next_card(), "deny")
        self.assertEqual(self.wait_idle(svc), "idle")
        for request in self.model.seen:
            offered = set(request["tools"])
            self.assertTrue(chat.BROWSER_ACTIONS <= offered)
            self.assertTrue({"browser_click", "browser_type"}.isdisjoint(offered))

    def test_phone_and_desk_show_the_same_cards_before_acting(self):
        for action, fields in ((CLICK, {}), (TYPING, {"kind": "field", "textarea": True, "can_submit": True,
                                                     "destination_origin": "https://example.invalid"})):
            shown = []
            for phone in (False, True):
                with self.subTest(tool=action[0], phone=phone):
                    svc = self.start(action, phone=phone, **fields)
                    card = self.next_card()
                    self.assertEqual(card["tool"], action[0])
                    shown.append(card["card"])
                    self.assertEqual(self.acted, [])
                    self.answer(card, "approve" if phone else "deny")
                    self.assertEqual(self.wait_idle(svc), "idle")
                    self.assertEqual(len(self.acted), int(phone))
                    self.assertTrue(chat.BROWSER_ACTIONS <= set(self.model.seen[-1]["tools"]))
            self.assertEqual(shown[0], shown[1])

    def test_unavailable_desk_does_not_offer_actions(self):
        self.assertFalse(chat.BROWSER_ACTIONS & set(chat.available_tools(gated=True)))
        self.desk.available.return_value = False
        self.assertFalse(chat.BROWSER_ACTIONS & set(chat.available_tools(gated=True, browser_desk=self.desk)))

    def test_twelve_calls_include_both_action_tools(self):
        bad_type = ("browser_fill", {"number": "bad", "text": TYPED})
        svc = self.start(*([bad_type, CLICK] * 6), CLICK)
        for _ in range(6):
            self.answer(self.next_card(), "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(len(self.acted), 6)
        self.assertEqual(self.results()[-1], fastpath._BROWSER_TURN_ERROR)

    def test_browser_cards_remain_click_only(self):
        svc = self.start(CLICK, saved=True)
        card = self.next_card()
        voice = VoiceApproving(self.broker, clock=lambda: 0)
        self.assertFalse(voice.ask(card))
        voice.prompt_finished(card["id"], 0)
        self.assertTrue(voice.heard("yes"))
        self.assertTrue(voice.take_needs_click())
        self.assertEqual(self.acted, [])
        self.assertIsNotNone(voice.pending())
        voice.click(card["id"], card["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.acted, [(act.CLICK, [1, 1])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
