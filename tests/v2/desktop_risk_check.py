"""Exercise the pure desktop approval policy and card text."""

from collections import Counter
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.desktop_risk import (
    ASK, GROUPS, KEYS, REFUSE, RUN, Action, Verdict, canonical_keys, decide, enter_asks, label_group,
    statement, voice_may_approve,
)


# Every key once, in its judged spelling: a chord cannot repeat a key.
LONG_CHORD = tuple(sorted({canonical_keys((key,))[0] for key in KEYS} - {"ctrl", "escape"}))

BASE = Action("click", "Outlook", "Send", "button", purpose="Send the reply.", risky="no")


class Labels(unittest.TestCase):
    def test_normalisation_and_punctuation(self):
        for name in ("Send", "Send message", "&Send", "ＳｅＮＤ", "sEnD",
                     "  Send…", "Send (Ctrl+Enter)", "S&end"):
            with self.subTest(name=name):
                self.assertEqual(label_group(name), "send")
        self.assertEqual(label_group("ＡＤＤ—ＦＵＮＤＳ"), "pay")
        self.assertEqual(label_group("Save / changes"), "confirm")

    def test_hidden_characters_and_original_word_count(self):
        for hidden in "\u200b\ud800\u0338\u20dd\x85\u034f\u115f\u1160\u3164\uffa0\u2800\U000e0100":
            with self.subTest(hidden=ascii(hidden)):
                self.assertEqual(label_group(f"Se{hidden}nd"), "send")
        self.assertEqual(label_group("SUBMİT"), "send")
        self.assertEqual(
            label_group("Pay $1,234.56 to john.doe@example.com and jane.roe@example.com"), "pay"
        )

    def test_safe_words_and_near_misses(self):
        for name in (
            "Sender", "Sent", "descend", "payload", "Resent", "postage", "unpaid",
            "buyers", "deployment", "formatter", "preview", "unconfirmed", "bookcase",
            "backup", "bookmark", "checked out", "clear selected history", "sign then out",
            "ok", "yes", "next", "close", "cancel", "back", "save", "open", "search",
            "copy", "paste", "continue", "123-４５!", "", "…",
            "order", "tip", "email", "apply", "book", "forward", "format",
        ):
            with self.subTest(name=name):
                self.assertEqual(label_group(name), "")

    def test_all_listed_terms(self):
        expected = {
            "pay": ("pay|buy|purchase|checkout|check out|subscribe|donate|place order|order now|"
                    "complete order|submit order|buy now|pay now|complete purchase|add funds|"
                    "send tip|add tip|bid|transfer|withdraw|payment|make a payment|renew|top up|"
                    "start free trial|rent"),
            "send": ("send|submit|post|publish|reply|reply all|share|tweet|comment|send message|"
                     "send email|invite|apply now|submit application|sign up|register|book now|"
                     "reserve|rsvp|upload|resend|merge|call"),
            "delete": ("delete|remove|erase|discard|trash|uninstall|empty recycle bin|clear all|"
                       "clear history|clear data|clear cache|format drive|format disk|reset|wipe|"
                       "unsubscribe|cancel subscription|cancel my subscription|cancel membership|"
                       "cancel order|cancel plan|don't save|overwrite|replace the file|"
                       "replace the files|deactivate|close account"),
            "install": ("install|update|upgrade|run anyway|allow access|grant|authorize|allow|"
                        "enable|turn off|disable|restart|shut down|shutdown|sign out|signout|"
                        "log out|logout|log off|add to chrome|add extension|trust"),
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
        for role in ("", "button", "link", "menuitem", "other"):
            with self.subTest(role=role):
                self.assertEqual(label_group("Send", role), "send")
        for role in ("editable", "edit", "textbox", "richedit", "combobox", "document",
                     "search", "searchbox", "Editable Text", "search_box"):
            with self.subTest(role=role):
                self.assertEqual(label_group("Search or send a command", role), "")
        self.assertEqual(label_group("Send " + "word " * 11), "send")
        self.assertEqual(label_group("Send " + "word " * 12), "")
        self.assertEqual(label_group(None), "")
        self.assertEqual(label_group("Send", None), "")


class Enter(unittest.TestCase):
    def test_search_role_and_label_exceptions(self):
        for role in ("search", " SearchBOX "):
            self.assertFalse(enter_asks("Send", role))
        for name in ("ＳＥＡＲＣＨ messages", "Find", "Address bar", "Address", "Enter a URL",
                     "Filter replies", "Look…up", "Search and pay", "Look up a contact",
                     "Address and search bar", "URL", "Enter URL"):
            with self.subTest(name=name):
                self.assertFalse(enter_asks(name, "editable"))
                # An unknown role may be a button: a risky label there asks.
                self.assertEqual(enter_asks(name, "unrecognized"), name == "Search and pay")
                # A focused button is pressed by Enter whatever else its label says.
                self.assertTrue(enter_asks(name + " and send", "button"))

    def test_text_entry_defaults_to_asking(self):
        for role in ("editable", "combobox", "document"):
            for name in ("Chat message", "", "123", "research", "finder", "lookup",
                         "addressable", "urlencoded", "filtering", "word " * 13):
                with self.subTest(role=role, name=name):
                    self.assertTrue(enter_asks(name, role))
        self.assertTrue(enter_asks("word " * 13 + "Search", "editable"))
        self.assertTrue(enter_asks(None, "search"))
        self.assertTrue(enter_asks("Search", None))

    def test_other_roles_follow_labels(self):
        for role in (
            "button", "splitbutton", "link", "hyperlink", "menuitem", "menu", "listitem",
            "list", "tab", "tabitem", "checkbox", "radio", "radiobutton", "treeitem",
            "toolbar", "image", "cell", "row", "option", "switch",
        ):
            with self.subTest(role=role):
                self.assertTrue(enter_asks("Send", f" {role.upper()} "))
                self.assertFalse(enter_asks("Next", role))
        self.assertTrue(enter_asks("Pay", ""))
        self.assertTrue(enter_asks("", ""))
        for name in ("Message #search-team", "Reply to Find", "URL field", "Look upload"):
            with self.subTest(name=name):
                self.assertTrue(enter_asks(name, "textbox"))
                self.assertTrue(enter_asks(name, "unrecognized"))


class Decisions(unittest.TestCase):
    def test_invalid_fields(self):
        self.assertEqual(decide(object()), Verdict(REFUSE, reasons=("invalid:action",)))
        for field, values in (
            ("kind", ("drag", "Click", None, [])),
            ("purpose", (None, "", " ", "! …", "p" * 301, "x\x00y", "x\x1fy", "x\x7fy",
                         "x\ny", "x\x85y", "x\u202ey", "x\ud800y", "x\u2028y", "x\u2029y")),
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

    def test_exact_action_and_field_types(self):
        class OtherAction(Action):
            pass

        class Text(str):
            pass

        class Chord(tuple):
            pass

        invalid_card = "An invalid desktop action"
        for action in (OtherAction("click", "Outlook", purpose="Click.", risky="no"),
                       object.__new__(Action)):
            self.assertEqual(decide(action).decision, REFUSE)
            self.assertEqual(statement(action, None), invalid_card)
        for field in ("kind", "app", "name", "role", "purpose", "risky"):
            with self.subTest(field=field):
                action = replace(BASE, **{field: Text(getattr(BASE, field))})
                self.assertEqual(decide(action), Verdict(REFUSE, reasons=(f"invalid:{field}",)))
                self.assertEqual(statement(action, None), invalid_card)
        for keys in (Chord(("enter",)), (Text("enter"),)):
            action = replace(BASE, kind="key", keys=keys)
            self.assertEqual(decide(action), Verdict(REFUSE, reasons=("invalid:keys",)))
            self.assertEqual(statement(action, None), invalid_card)

    def test_record_fields_read_once(self):
        for record, fields, use in (
            (BASE, Action.__dataclass_fields__, decide),
            (BASE, Action.__dataclass_fields__, lambda action: statement(action, None)),
            (Verdict(ASK, "send"), Verdict.__dataclass_fields__, voice_may_approve),
            (Verdict(ASK, "send"), Verdict.__dataclass_fields__, lambda verdict: statement(BASE, verdict)),
        ):
            reads = Counter()

            def read(instance, field):
                reads[field] += 1
                return object.__getattribute__(instance, field)

            with patch.object(type(record), "__getattribute__", read):
                use(record)
            self.assertEqual(reads, Counter(fields.keys()))

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
        self.assertEqual(decide(key), Verdict(ASK, "send", ("shortcut", "enter", "key:delete", "declared:unsure")))
        self.assertEqual(decide(replace(key, name="Pay")),
                         Verdict(ASK, "pay", ("shortcut", "enter", "key:delete", "declared:unsure")))
        self.assertEqual(decide(replace(key, name="Confirm", risky="no")),
                         Verdict(ASK, "delete", ("shortcut", "enter", "key:delete")))
        self.assertEqual(decide(replace(key, name="Search", role="search", risky="no")),
                         Verdict(ASK, reasons=("shortcut", "enter")))
        # The focused control is "Send": a key press that asks carries its group.
        self.assertEqual(decide(replace(key, keys=("Delete",), risky="no")),
                         Verdict(ASK, "send", ("key:delete",)))
        self.assertEqual(decide(replace(key, keys=("Delete",), name="Inbox", risky="no")),
                         Verdict(ASK, "delete", ("key:delete",)))
        self.assertEqual(decide(replace(key, keys=(" RETURN ",), risky="no")),
                         Verdict(ASK, "send", ("enter",)))
        self.assertEqual(decide(replace(key, name="Search", keys=("enter",), risky="no")), Verdict(RUN))
        self.assertEqual(decide(replace(key, keys=("ReturnValue", "Backspace", "Ctrl+Enter"),
                                        risky="no")), Verdict(REFUSE, reasons=("invalid:keys",)))

    def test_closed_key_vocabulary(self):
        self.assertIs(type(KEYS), frozenset)
        for keys in ((), ("",), ("\n",), ("{Enter}",), ("NumpadEnter",), ("VK_RETURN",),
                     ("ctrl+enter",), ("ｅｎｔｅｒ",), ("\x7f",), ("forwarddelete",)):
            with self.subTest(keys=keys):
                self.assertEqual(decide(replace(BASE, kind="key", keys=keys)),
                                 Verdict(REFUSE, reasons=("invalid:keys",)))

    def test_space_and_delete_follow_focused_role(self):
        for key in ("space", " SpaceBAR "):
            action = replace(BASE, kind="key", keys=(key,), name="Amount to pay")
            self.assertEqual(decide(action), Verdict(ASK, "pay", ("press",)))
            self.assertEqual(decide(replace(action, name="Next")), Verdict(RUN))
            for role in ("editable", "searchbox"):
                self.assertEqual(decide(replace(action, role=role)), Verdict(RUN))
            # A role this module cannot name may be a button built from a plain
            # element: Space presses it, so its label decides.
            for role in ("", "unrecognized", "text", "push button"):
                self.assertEqual(decide(replace(action, role=role)), Verdict(ASK, "pay", ("press",)))
                self.assertEqual(decide(replace(action, role=role, name="Next")), Verdict(RUN))
        for key in ("Del", "Delete"):
            action = replace(BASE, kind="key", keys=(key,), name="Selection")
            for role in ("button", "", "unrecognized"):
                self.assertEqual(decide(replace(action, role=role)), Verdict(ASK, "delete", ("key:delete",)))
            for role in ("edit", "richedit", "search", "searchbox"):
                self.assertEqual(decide(replace(action, role=role)), Verdict(RUN))
        action = replace(BASE, kind="key", keys=("space", "delete"), name="Pay")
        self.assertEqual(decide(action), Verdict(ASK, "pay", ("press", "key:delete")))

    def test_enter_and_submit_keep_the_focused_label_group(self):
        typing = replace(BASE, kind="type", role="edit", name="Amount to pay", submit=True)
        key = replace(typing, kind="key", keys=("return",), submit=False)
        for action in (typing, key):
            self.assertEqual(decide(action), Verdict(ASK, "pay", ("enter",)))
            self.assertFalse(voice_may_approve(decide(action)))

    def test_shortcut_allowlist(self):
        action = replace(BASE, kind="key", role="textbox", name="Search")
        for modifier, allowed in (
            ("control", (*"acvxzyfltnrsp0", *"wbiuhke", "home", "end", "left", "right", "up", "down",
                         "tab", "pageup", "pagedown", "plus", "minus", "backspace")),
            ("alt", ("left", "right", "up", "tab", "home")),
        ):
            # The list is pinned whole: a key added to it must be added here.
            for key in sorted(KEYS - set(allowed) - {"shift", "enter", "return"}):
                if canonical_keys((modifier, key)) is None or canonical_keys((key,))[0] in allowed:
                    continue
                with self.subTest(modifier=modifier, asks=key):
                    self.assertIn("shortcut", decide(replace(action, keys=(modifier, key))).reasons)
            for key in allowed:
                with self.subTest(modifier=modifier, key=key):
                    self.assertEqual(decide(replace(action, keys=(modifier, key))), Verdict(RUN))
                    shifted = decide(replace(action, keys=(key, "shift", modifier)))
                    beside = key in ("home", "end", "left", "right", "up", "down", "tab",
                                     "pageup", "pagedown", "z", "t", "v")
                    self.assertEqual(shifted, Verdict(RUN) if beside else Verdict(ASK, reasons=("shortcut",)))
        for keys in (("ctrl", "pgup"), ("alt", "arrowleft"), ("CONTROL", "PgDn")):
            with self.subTest(alias=keys):
                self.assertEqual(decide(replace(action, keys=keys)), Verdict(RUN))
        for keys in (("ctrl", "a", "a"), ("ctrl", "shift", "shift", "t"), ("ctrl", "control", "c"),
                     ("del", "delete"), ("ſpace",), ("\u212a",), ("ctrl", "ſ")):
            with self.subTest(refused=keys):
                self.assertEqual(decide(replace(action, keys=keys)), Verdict(REFUSE, reasons=("invalid:keys",)))
                self.assertIsNone(canonical_keys(keys))
        self.assertEqual(canonical_keys((" Control ", "RETURN")), ("ctrl", "enter"))
        self.assertEqual(canonical_keys(("cmd", "Del")), ("win", "delete"))
        for bad in ((), ["ctrl"], ("ctrl", 1), None, "ctrl"):
            self.assertIsNone(canonical_keys(bad))
        for keys in (("ctrl", "g"), ("ctrl", "a", "c"),
                     ("ctrl", "alt", "left"), ("ctrl", "alt"), ("ctrl", "shift"),
                     ("alt", "end"), ("alt", "f4"), ("altgr", "left")):
            with self.subTest(keys=keys):
                self.assertEqual(decide(replace(action, keys=keys)), Verdict(ASK, reasons=("shortcut",)))
        for modifier in ("ctrl", "control", "alt", "altgr", "win", "windows",
                         "meta", "super", "cmd", "command"):
            with self.subTest(modifier=modifier):
                self.assertEqual(decide(replace(action, keys=(modifier,))), Verdict(RUN))
                self.assertEqual(decide(replace(action, keys=(modifier, "return"))),
                                 Verdict(ASK, reasons=("shortcut", "enter")))
                if modifier in ("win", "windows", "meta", "super", "cmd", "command"):
                    self.assertEqual(decide(replace(action, keys=(modifier, "a"))),
                                     Verdict(ASK, reasons=("shortcut",)))

    def test_type_and_scroll(self):
        typing = replace(BASE, kind="type", name="Chat message", role="editable", submit=True)
        self.assertEqual(decide(typing), Verdict(ASK, reasons=("enter",)))
        self.assertEqual(decide(replace(typing, name="Search")), Verdict(RUN))
        self.assertEqual(decide(replace(typing, name="")), Verdict(ASK, reasons=("enter",)))
        self.assertEqual(decide(replace(typing, name="Pay", role="button", risky="yes")),
                         Verdict(ASK, "pay", ("enter", "declared:yes")))
        self.assertEqual(decide(replace(typing, role="")), Verdict(ASK, reasons=("enter",)))
        self.assertEqual(decide(replace(typing, submit=False, keys=("delete", "enter"))),
                         Verdict(REFUSE, reasons=("invalid:keys",)))
        scroll = replace(BASE, kind="scroll")
        self.assertEqual(decide(scroll), Verdict(RUN))
        self.assertEqual(decide(replace(scroll, risky="yes")), Verdict(ASK, reasons=("declared:yes",)))
        self.assertEqual(decide(replace(scroll, keys=("enter", "delete"), submit=True)),
                         Verdict(REFUSE, reasons=("invalid:keys", "invalid:submit")))



class Statements(unittest.TestCase):
    def test_shapes_and_purpose(self):
        self.assertEqual(statement(BASE, decide(BASE)), 'Click "Send" in Outlook (send)')
        chord = replace(BASE, kind="key", keys=(" CTRL ", "enter"), name="Message body")
        self.assertEqual(statement(chord, decide(chord)), 'Press Ctrl+Enter in "Message body" in Outlook')
        typing = replace(BASE, kind="type", name="Reply", app="Slack", submit=True)
        self.assertEqual(statement(typing, decide(typing)), 'Type and press Enter in "Reply" in Slack (send)')
        self.assertEqual(statement(replace(typing, submit=False), None), 'Type in "Reply" in Slack')
        scroll = replace(BASE, kind="scroll", app="Chrome")
        self.assertEqual(statement(scroll, decide(scroll)), "Scroll in Chrome")

    def test_sanitisation_and_fallbacks(self):
        action = replace(BASE, name='  Se"\n\t\x7f\x85\u202e\u200b\u2028\u2029nd  ',
                         app='  O"u\u202et\nlook <x>/@!😈. -_+()  ')
        self.assertEqual(statement(action, decide(action)), 'Click "Se\' nd" in Outlook x. -_+()')
        action = replace(BASE, app=' "\u202e\n\u200b! ', name="\t\u202e\u2029 ")
        self.assertEqual(statement(action, decide(action)), "Click an unlabeled control in an unknown app")
        self.assertEqual(statement(replace(BASE, app="Éditeur ２"), None), 'Click "Send" in Éditeur ２')
        self.assertEqual(statement(None, None), "An invalid desktop action")

    def test_card_sanitisation_preserves_visible_unicode_and_group(self):
        hidden = "\x85\u200b\ud800\u2028\u2029\u0307\u20dd\u034f\u115f\u1160\u3164\uffa0\u2800\ufe0f"
        quotes = '"＂ʺ“”„‟«»″〃‘’'
        action = replace(BASE, name=f" Ｐay{hidden}{quotes} now ", app=f"AC{hidden}ME")
        card = statement(action, Verdict(ASK, "pay"))
        # The line break in `hidden` is a gap; a run of quote marks is one mark.
        self.assertEqual(card, 'Click "Ｐay \' now" in ACME (pay)')
        card.encode("utf-8")
        self.assertEqual(statement(replace(BASE, app="É ２"), None),
                         'Click "Send" in an unknown app')
        self.assertEqual(statement(replace(BASE, app="A" * 59 + " B"), Verdict(ASK, "send")),
                         f'Click "Send" in {"A" * 59} (send)')
        name = "z" * 65 + " Pay"
        action = replace(BASE, name=name)
        self.assertEqual(statement(action, decide(action)),
                         f'Click "{"z" * 59}…" in Outlook (pay)')

    def test_length_limits(self):
        action = replace(BASE, name="x" * 500, app="💥" * 200 + "A" * 500)
        card = statement(action, decide(action))
        self.assertEqual(card, f'Click "{"x" * 59}…" in {"A" * 60}')
        exact = replace(BASE, name="x" * 60)
        self.assertEqual(statement(exact, None), f'Click "{"x" * 60}" in Outlook')
        chord = replace(action, kind="key", keys=("ctrl", "escape", *LONG_CHORD))
        card = statement(chord, None)
        self.assertLessEqual(len(card), 200)
        self.assertEqual(len(card.splitlines()), 1)
        self.assertTrue(card.startswith("Press Ctrl+Escape+"))
        self.assertTrue(card.endswith(f'+… in "{"x" * 59}…" in {"A" * 60}'))

    def test_card_bound_across_long_name_app_and_chord_grid(self):
        for name_size in (0, 42, 58, 59, 60, 61, 90):
            for app_size in (0, 45, 57, 58, 59, 60, 94):
                for keys in (LONG_CHORD, LONG_CHORD[:40], LONG_CHORD[::-1], ("ctrl", "pageup")):
                    for group in ("", "pay", "confirm"):
                        action = replace(BASE, kind="key", keys=keys,
                                         name="界" * name_size, app="A" * app_size)
                        card = statement(action, Verdict(ASK, group))
                        with self.subTest(name_size=name_size, app_size=app_size, keys=keys[:2], group=group):
                            self.assertLessEqual(len(card), 200)
                            self.assertTrue(card.startswith("Press "))
                            if group:
                                self.assertTrue(card.endswith(f" ({group})"))
                            card.encode("utf-8")

    def test_keys_use_only_fixed_display_text(self):
        action = replace(BASE, kind="key", keys=("CTRL", 'Fake"+\n\u202eEnter', "f12"))
        self.assertEqual(statement(action, decide(action)), "An invalid desktop action")
        empty = replace(action, keys=())
        self.assertEqual(statement(empty, decide(empty)), "An invalid desktop action")

    def test_display_names_for_known_keys(self):
        expected = {
            "esc": "Escape", "pageup": "Pageup", "f12": "F12", "backspace": "Backspace",
            "ctrl": "Ctrl", "enter": "Enter", "del": "Delete", "spacebar": "Space",
            "printscreen": "Printscreen", "minus": "Minus", "plus": "Plus",
            "control": "Ctrl", "return": "Enter", "pgup": "Pageup", "arrowleft": "Left",
            "cmd": "Win", "windows": "Win",
        }
        self.assertLessEqual(expected.keys(), KEYS)
        for key in sorted(KEYS):
            with self.subTest(key=key):
                action = replace(BASE, kind="key", keys=(key,))
                display = expected.get(key, canonical_keys((key,))[0].capitalize())
                self.assertEqual(statement(action, None), f'Press {display} in "Send" in Outlook')

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

    def test_exact_verdict_and_field_types(self):
        class OtherVerdict(Verdict):
            pass

        class Text(str):
            pass

        class Reasons(tuple):
            pass

        for verdict in (
            OtherVerdict(ASK, "send"), object.__new__(Verdict), Verdict(Text(ASK), "send"),
            Verdict(ASK, Text("send")), Verdict(ASK, "money"), Verdict(ASK, "Pay"),
            Verdict(ASK, "send", Reasons()), Verdict(ASK, "send", (Text("reason"),)),
            Verdict(ASK, "send", (None,)), Verdict(RUN, 'pay\ud800'),
        ):
            self.assertFalse(voice_may_approve(verdict))
            self.assertEqual(statement(BASE, verdict), 'Click "Send" in Outlook')


class LeadAdditions(unittest.TestCase):
    def test_role_spelling_does_not_change_the_answer(self):
        self.assertEqual(label_group("Send", " Editable "), "")
        self.assertEqual(label_group("Send", "SEARCH"), "")
        self.assertFalse(enter_asks("anything", " Search"))
        self.assertTrue(enter_asks("Message", "EDITABLE"))

    def test_address_only_as_the_whole_name(self):
        self.assertFalse(enter_asks("Address", "editable"))
        self.assertFalse(enter_asks("Address and search bar", "editable"))
        self.assertTrue(enter_asks("Email address", "editable"))
        self.assertTrue(enter_asks("Send to address", "editable"))
        self.assertTrue(enter_asks("Shipping address", "combobox"))

    def test_added_words(self):
        self.assertEqual(label_group("Resend"), "send")
        self.assertEqual(label_group("Cancel my subscription"), "delete")
        self.assertEqual(label_group("Cancel order"), "delete")
        self.assertEqual(label_group("Cancel membership"), "delete")
        self.assertEqual(label_group("Cancel"), "")


class SecondReview(unittest.TestCase):
    def test_shift_enter_is_a_new_line_only_in_a_text_field(self):
        box = replace(BASE, kind="key", role="document", name="Message", keys=("shift", "enter"))
        # A single-line field submits on Shift+Enter as on Enter.
        self.assertEqual(decide(replace(box, role="edit")).decision, ASK)
        self.assertEqual(decide(replace(box, role="rich edit")), Verdict(RUN))
        self.assertEqual(decide(box), Verdict(RUN))
        self.assertEqual(decide(replace(box, keys=("enter",))).decision, ASK)
        self.assertEqual(decide(replace(box, keys=("ctrl", "shift", "enter"))).decision, ASK)
        for role in ("", "button", "unrecognized"):
            self.assertEqual(decide(replace(box, role=role, name="Send")).decision, ASK)

    def test_stated_payment_blocks_voice_when_the_step_asks(self):
        action = replace(BASE, name="", role="", purpose="Pay the $500 invoice", risky="yes")
        verdict = decide(action)
        self.assertEqual(verdict, Verdict(ASK, "pay", ("declared:yes",)))
        self.assertFalse(voice_may_approve(verdict))
        # The purpose alone never makes a step ask.
        quiet = replace(action, name="Next", risky="no")
        self.assertEqual(decide(quiet), Verdict(RUN))
        other = replace(action, purpose="Open the payload viewer", risky="unsure")
        self.assertEqual(decide(other), Verdict(ASK, "", ("declared:unsure",)))

    def test_ampersand_is_read_both_ways(self):
        self.assertEqual(label_group("Send&Close"), "send")
        self.assertEqual(label_group("Review&Pay"), "pay")
        self.assertEqual(label_group("&Send"), "send")
        self.assertEqual(label_group("Se&nd"), "send")
        self.assertEqual(label_group("E&xit"), "")

    def test_role_names_with_spaces(self):
        action = replace(BASE, kind="key", keys=("space",), name="Pay now")
        for role in ("push button", "Push_Button", "menu item", "list-item", "page tab"):
            self.assertEqual(decide(replace(action, role=role)), Verdict(ASK, "pay", ("press",)))
        for role in ("editable text", "Editable_Text", "search box"):
            self.assertEqual(decide(replace(action, role=role)), Verdict(RUN))

    def test_added_words(self):
        for name in ("Place your order", "Confirm order", "Send money", "Redeem", "Claim"):
            self.assertEqual(label_group(name), "pay")

    def test_access_keys_on_a_focused_button(self):
        button = replace(BASE, kind="key", role="button", name="No", keys=("y",))
        self.assertEqual(decide(button), Verdict(ASK, "", ("accesskey",)))
        self.assertEqual(decide(replace(button, keys=("shift", "n"), name="Save")).reasons, ("accesskey",))
        self.assertEqual(decide(replace(button, role="push button", keys=("1",))).decision, ASK)
        self.assertEqual(decide(replace(button, name="Pay now")), Verdict(ASK, "pay", ("accesskey",)))
        for role in ("edit", "document", "listitem", "treeitem", "", "cell"):
            with self.subTest(role=role):
                self.assertEqual(decide(replace(button, role=role)), Verdict(RUN))
        for keys in (("tab",), ("down",), ("escape",), ("f5",)):
            self.assertEqual(decide(replace(button, keys=keys)), Verdict(RUN))
        self.assertEqual(decide(replace(button, keys=("alt", "d"))), Verdict(ASK, "", ("shortcut",)))

    def test_plain_role_spelt_with_a_space_follows_its_label(self):
        key = replace(BASE, kind="key", keys=("enter",), name="Next")
        for role in ("push button", "pushbutton", "page tab", "outline item", "menu button"):
            with self.subTest(role=role):
                self.assertEqual(decide(replace(key, role=role)), Verdict(RUN))
        self.assertEqual(decide(replace(key, role="mystery")).decision, ASK)

    def test_line_breaks_are_read_as_a_gap_and_as_nothing(self):
        for name in ("Pay\nnow", "Buy\r\nnow", "Delete\naccount", "Send\tmessage", "Se\x85nd", "Se\u200and"):
            with self.subTest(name=name):
                self.assertTrue(label_group(name))
        self.assertEqual(statement(replace(BASE, name="Pay\nnow"), None), 'Click "Pay now" in Outlook')
        self.assertEqual(label_group("word\n" * 12 + "pay"), "")

    def test_a_payment_control_in_focus_blocks_voice_for_any_asking_key(self):
        focused = replace(BASE, kind="key", role="button", name="Pay now")
        for keys in (("ctrl", "q"), ("alt", "p"), ("delete",)):
            with self.subTest(keys=keys):
                verdict = decide(replace(focused, keys=keys))
                self.assertEqual((verdict.decision, verdict.group), (ASK, "pay"))
                self.assertFalse(voice_may_approve(verdict))

    def test_only_three_first_words_mark_a_lookup_box(self):
        for word in ("search", "find", "filter"):
            self.assertFalse(enter_asks(word + " messages", "edit"))
        for word in ("go", "open", "type", "enter", "query", "lookup", "browse", "jump", "ask", "message"):
            with self.subTest(word=word):
                self.assertTrue(enter_asks(word + " here", "edit"))

    def test_private_use_and_unassigned_characters(self):
        self.assertEqual(label_group("Se\ue000nd"), "send")
        self.assertEqual(label_group("Se\U000e0001nd"), "send")
        card = statement(replace(BASE, name="\ue000" * 80 + "Pay"), None)
        self.assertEqual(card, 'Click "Pay" in Outlook')


if __name__ == "__main__":
    unittest.main()
