"""Exercise the pure desktop approval policy and card text."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.desktop_risk import (
    ASK, GROUPS, REFUSE, RUN, Action, Verdict, decide, enter_asks, label_group,
    statement, voice_may_approve,
)


BASE = Action("click", "Outlook", "Send", "button", purpose="Send the reply.", risky="no")


class Labels(unittest.TestCase):
    def test_normalisation_and_punctuation(self):
        for name in ("Send", "Send message", "&Send", "ＳｅＮＤ", "sEnD",
                     "  Send…", "Send (Ctrl+Enter)", "S&end"):
            with self.subTest(name=name):
                self.assertEqual(label_group(name), "send")
        self.assertEqual(label_group("ＡＤＤ—ＦＵＮＤＳ"), "pay")
        self.assertEqual(label_group("Save / changes"), "confirm")

    def test_safe_words_and_near_misses(self):
        for name in (
            "Sender", "Sent", "descend", "payload", "Resend", "postage", "unpaid",
            "buyers", "deployment", "formatter", "preview", "unconfirmed", "bookcase",
            "backup", "bookmark", "checked out", "clear selected history", "sign then out",
            "ok", "yes", "next", "close", "cancel", "back", "save", "open", "search",
            "copy", "paste", "continue", "123-４５!", "", "…",
        ):
            with self.subTest(name=name):
                self.assertEqual(label_group(name), "")

    def test_all_listed_terms(self):
        expected = {
            "pay": ("pay|buy|purchase|order|checkout|check out|subscribe|donate|place order|"
                    "buy now|pay now|complete purchase|add funds|tip|bid|transfer|withdraw"),
            "send": ("send|submit|post|publish|reply|forward|share|tweet|comment|send message|"
                     "reply all|email|invite|apply|sign up|register|book|reserve|rsvp|upload"),
            "delete": ("delete|remove|erase|discard|trash|uninstall|empty recycle bin|clear all|"
                       "clear history|format|reset|wipe|unsubscribe|cancel subscription"),
            "install": ("install|update|upgrade|run anyway|allow access|grant|authorize|allow|"
                        "enable|turn off|disable|restart|shut down|sign out|log out"),
            "confirm": "confirm|agree|accept|i agree|save changes|continue anyway|proceed|finish|approve",
        }
        for group, terms in expected.items():
            for term in terms.split("|"):
                with self.subTest(group=group, term=term):
                    self.assertEqual(label_group(term), group)

    def test_strongest_group(self):
        self.assertEqual(GROUPS, ("pay", "send", "delete", "install", "confirm"))
        for name, group in (
            ("Confirm and pay", "pay"),
            ("Confirm install delete send", "send"),
            ("Confirm install delete", "delete"),
            ("Confirm install", "install"),
            ("Confirm", "confirm"),
        ):
            with self.subTest(name=name):
                self.assertEqual(label_group(name), group)

    def test_roles_length_and_unreadable_inputs(self):
        for role in ("", "button", "link", "menuitem", "document", "other", "Search"):
            with self.subTest(role=role):
                self.assertEqual(label_group("Send", role), "send")
        for role in ("editable", "combobox", "search"):
            with self.subTest(role=role):
                self.assertEqual(label_group("Search or send a command", role), "")
        self.assertEqual(label_group("Send " + "word " * 11), "send")
        self.assertEqual(label_group("Send " + "word " * 12), "")
        self.assertEqual(label_group(None), "")
        self.assertEqual(label_group("Send", None), "")


class Enter(unittest.TestCase):
    def test_search_role_and_label_exceptions(self):
        self.assertFalse(enter_asks("Send", "search"))
        for name in ("ＳＥＡＲＣＨ messages", "Find", "Address bar", "Enter a URL",
                     "Filter replies", "Look…up", "Search and pay"):
            with self.subTest(name=name):
                self.assertFalse(enter_asks(name, "editable"))
                self.assertFalse(enter_asks(name + " and send", "button"))

    def test_text_entry_defaults_to_asking(self):
        for role in ("editable", "combobox", "document"):
            for name in ("Chat message", "", "123", "research", "finder", "lookup",
                         "addressable", "urlencoded", "filtering", "word " * 13):
                with self.subTest(role=role, name=name):
                    self.assertTrue(enter_asks(name, role))
        self.assertFalse(enter_asks("word " * 13 + "Search", "editable"))
        self.assertTrue(enter_asks(None, "search"))
        self.assertTrue(enter_asks("Search", None))

    def test_other_roles_follow_labels(self):
        self.assertTrue(enter_asks("Send", "button"))
        self.assertTrue(enter_asks("Pay", ""))
        self.assertFalse(enter_asks("Next", "button"))
        self.assertFalse(enter_asks("Message", "listitem"))
        self.assertFalse(enter_asks("", ""))


class Decisions(unittest.TestCase):
    def test_invalid_fields(self):
        self.assertEqual(decide(object()), Verdict(REFUSE, reasons=("invalid:action",)))
        for field, values in (
            ("kind", ("drag", "Click", None, [])),
            ("purpose", (None, "", " ", "p" * 301, "x\x00y", "x\x1fy", "x\x7fy", "x\ny")),
            ("risky", (None, "", "Yes", " no ", "maybe", True)),
            ("keys", ([], "enter", ("enter", 1), None)),
            ("submit", (1, 0, None, "false")),
            ("app", (None, 5)),
            ("name", (None, 5)),
            ("role", (None, 5)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    action = replace(BASE, **{field: value})
                    self.assertEqual(decide(action), Verdict(REFUSE, reasons=(f"invalid:{field}",)))
                    self.assertEqual(statement(action, Verdict(ASK)), "An invalid desktop action")

    def test_refuse_precedes_all_ask_reasons(self):
        action = replace(BASE, kind="key", keys=("enter", "del"), risky="yes", purpose=" ")
        self.assertEqual(decide(action), Verdict(REFUSE, reasons=("invalid:purpose",)))
        action = replace(BASE, app=None, risky="YES")
        self.assertEqual(decide(action), Verdict(REFUSE, reasons=("invalid:risky", "invalid:app")))

    def test_valid_boundaries_and_frozen_records(self):
        quiet = replace(BASE, name="Open", purpose="p" * 300)
        self.assertEqual(decide(quiet), Verdict(RUN))
        self.assertEqual(decide(replace(quiet, purpose="First sentence. Second sentence.")), Verdict(RUN))
        with self.assertRaises(FrozenInstanceError):
            quiet.name = "Send"
        verdict = decide(quiet)
        with self.assertRaises(FrozenInstanceError):
            verdict.decision = ASK

    def test_click_and_declarations(self):
        self.assertEqual(decide(BASE), Verdict(ASK, "send", ("label:send",)))
        self.assertEqual(decide(replace(BASE, risky="yes")),
                         Verdict(ASK, "send", ("label:send", "declared:yes")))
        self.assertEqual(decide(replace(BASE, role="editable")), Verdict(RUN))
        self.assertEqual(decide(replace(BASE, name="Open", risky="unsure")),
                         Verdict(ASK, reasons=("declared:unsure",)))

    def test_key_reasons_and_precedence(self):
        key = replace(BASE, kind="key", keys=(" Ctrl ", " EnTeR ", " DeL "), risky="unsure")
        self.assertEqual(decide(key), Verdict(ASK, "send", ("enter", "key:delete", "declared:unsure")))
        self.assertEqual(decide(replace(key, name="Pay")),
                         Verdict(ASK, "pay", ("enter", "key:delete", "declared:unsure")))
        self.assertEqual(decide(replace(key, name="Confirm", risky="no")),
                         Verdict(ASK, "delete", ("enter", "key:delete")))
        self.assertEqual(decide(replace(key, name="Search", role="search", risky="no")),
                         Verdict(ASK, "delete", ("key:delete",)))
        self.assertEqual(decide(replace(key, keys=("Delete",), risky="no")),
                         Verdict(ASK, "delete", ("key:delete",)))
        self.assertEqual(decide(replace(key, keys=(" RETURN ",), risky="no")),
                         Verdict(ASK, "send", ("enter",)))
        self.assertEqual(decide(replace(key, name="Search", keys=("enter",), risky="no")), Verdict(RUN))
        self.assertEqual(decide(replace(key, keys=("ReturnValue", "Backspace", "Ctrl+Enter"),
                                        risky="no")), Verdict(RUN))

    def test_type_and_scroll(self):
        typing = replace(BASE, kind="type", name="Chat message", role="editable", submit=True)
        self.assertEqual(decide(typing), Verdict(ASK, reasons=("enter",)))
        self.assertEqual(decide(replace(typing, name="Search")), Verdict(RUN))
        self.assertEqual(decide(replace(typing, name="")), Verdict(ASK, reasons=("enter",)))
        self.assertEqual(decide(replace(typing, name="Pay", role="button", risky="yes")),
                         Verdict(ASK, "pay", ("enter", "declared:yes")))
        self.assertEqual(decide(replace(typing, submit=False, keys=("delete", "enter"))), Verdict(RUN))
        scroll = replace(BASE, kind="scroll", keys=("enter", "delete"), submit=True)
        self.assertEqual(decide(scroll), Verdict(RUN))
        self.assertEqual(decide(replace(scroll, risky="yes")), Verdict(ASK, reasons=("declared:yes",)))


class Statements(unittest.TestCase):
    def test_shapes_and_purpose(self):
        self.assertEqual(statement(BASE, decide(BASE)), 'Click "Send" in Outlook')
        chord = replace(BASE, kind="key", keys=(" CTRL ", "enter"), name="Message body")
        self.assertEqual(statement(chord, decide(chord)), 'Press Ctrl+Enter in "Message body" in Outlook')
        typing = replace(BASE, kind="type", name="Reply", app="Slack", submit=True)
        self.assertEqual(statement(typing, decide(typing)), 'Type and press Enter in "Reply" in Slack')
        self.assertEqual(statement(replace(typing, submit=False), None), 'Type in "Reply" in Slack')
        scroll = replace(BASE, kind="scroll", app="Chrome")
        self.assertEqual(statement(scroll, decide(scroll)), "Scroll in Chrome")

    def test_sanitisation_and_fallbacks(self):
        action = replace(BASE, name='  Se"\n\t\x7f\x85\u202e\u200b\u2028\u2029nd  ',
                         app='  O"u\u202et\nlook <x>/@!😈. -_+()  ')
        self.assertEqual(statement(action, decide(action)), 'Click "Se\'nd" in Outlook x. -_+()')
        action = replace(BASE, app=' "\u202e\n\u200b! ', name="\t\u202e\u2029 ")
        self.assertEqual(statement(action, decide(action)), "Click an unlabeled control in an unknown app")
        self.assertEqual(statement(replace(BASE, app="Éditeur ２"), None), 'Click "Send" in Éditeur ２')
        self.assertEqual(statement(None, None), "An invalid desktop action")

    def test_length_limits(self):
        action = replace(BASE, name="x" * 500, app="💥" * 200 + "A" * 500)
        card = statement(action, decide(action))
        self.assertEqual(card, f'Click "{"x" * 59}…" in {"A" * 60}')
        exact = replace(BASE, name="x" * 60)
        self.assertEqual(statement(exact, None), f'Click "{"x" * 60}" in Outlook')
        chord = replace(action, kind="key", keys=("ctrl", "escape") * 100)
        card = statement(chord, decide(chord))
        self.assertLessEqual(len(card), 200)
        self.assertEqual(len(card.splitlines()), 1)
        self.assertTrue(card.startswith("Press Ctrl+Escape+"))
        self.assertTrue(card.endswith(f'+… in "{"x" * 59}…" in {"A" * 60}'))

    def test_keys_use_only_fixed_display_text(self):
        action = replace(BASE, kind="key", keys=("CTRL", 'Fake"+\n\u202eEnter', "f12"))
        self.assertEqual(statement(action, decide(action)), 'Press Ctrl+a key+F12 in "Send" in Outlook')
        empty = replace(action, keys=())
        self.assertEqual(statement(empty, decide(empty)), 'Press a key in "Send" in Outlook')

    def test_malformed_instance_returns_the_invalid_card(self):
        class UnreadableApp(Action):
            def __getattribute__(self, field):
                if field == "app":
                    raise ValueError("unreadable")
                return super().__getattribute__(field)

        self.assertEqual(statement(UnreadableApp("click", "Outlook", purpose="Click.", risky="no"),
                                   None), "An invalid desktop action")


class Voice(unittest.TestCase):
    def test_only_non_payment_asks_can_use_voice(self):
        self.assertFalse(voice_may_approve(None))
        self.assertFalse(voice_may_approve({"decision": ASK, "group": "send"}))
        for decision in (RUN, REFUSE, "other"):
            self.assertFalse(voice_may_approve(Verdict(decision)))
        self.assertFalse(voice_may_approve(Verdict(ASK, "pay")))
        payment_chord = replace(BASE, kind="key", name="Pay", keys=("enter", "delete"))
        self.assertFalse(voice_may_approve(decide(payment_chord)))
        for group in ("", "send", "delete", "install", "confirm"):
            self.assertTrue(voice_may_approve(Verdict(ASK, group)))


if __name__ == "__main__":
    unittest.main()
