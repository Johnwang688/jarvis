"""Review findings for jarvis/v2/desktop_risk.py (CU-2a). Each test states the
behaviour the review expects; a failure is an open finding."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.desktop_risk import (
    ASK, REFUSE, RUN, Action, Verdict, decide, enter_asks, label_group, statement,
    voice_may_approve,
)


BASE = Action("click", "Outlook", "Send", "button", purpose="Do the step.", risky="no")


class F1UnknownKeyNames(unittest.TestCase):
    """HIGH: a key name outside the known list runs unasked. Several are
    spellings of Enter or Delete that common executors accept (pyautogui takes
    "\\n" and "\\r"; pywinauto writes "{ENTER}", "{DEL}", "VK_RETURN")."""

    def test_unknown_keys_never_run(self):
        for keys in (("\n",), ("\r",), ("{Enter}",), ("NumpadEnter",), ("VK_RETURN",),
                     ("kp_enter",), ("ctrl+enter",), ("ｅｎｔｅｒ",),
                     ("{DEL}",), ("forwarddelete",), ("\x7f",)):
            with self.subTest(keys=keys):
                action = replace(BASE, kind="key", keys=keys, name="Pay", role="button")
                self.assertNotEqual(decide(action).decision, RUN)


class F2SpaceOnFocusedButton(unittest.TestCase):
    """MED: Space presses a focused button exactly as Enter does, but only
    Enter consults the label."""

    def test_space_on_pay_button_asks(self):
        action = replace(BASE, kind="key", keys=("space",), name="Pay now", role="button")
        verdict = decide(action)
        self.assertEqual(verdict.decision, ASK)
        self.assertFalse(voice_may_approve(verdict))


class F3VoiceOnPaymentEnter(unittest.TestCase):
    """MED: Enter in a text field drops the label's group (label_group returns
    "" for editable/combobox), so a payment submitted by Enter is voice-approvable."""

    def test_enter_in_pay_field_is_not_voice_approvable(self):
        for action in (
            replace(BASE, kind="type", name="Amount to pay", role="editable", submit=True),
            replace(BASE, kind="key", keys=("enter",), name="Pay now", role="editable"),
            replace(BASE, kind="key", keys=("enter",), name="Pay with", role="combobox"),
        ):
            with self.subTest(name=action.name, role=action.role):
                verdict = decide(action)
                self.assertEqual(verdict.decision, ASK)
                self.assertFalse(voice_may_approve(verdict))


class F4SearchWordInsideMessageBox(unittest.TestCase):
    """MED: the search exemption matches the word anywhere in the name, so a
    chat composer named after a channel ("Message #search-team", Slack's own
    label format) sends on Enter without asking."""

    def test_channel_name_does_not_exempt_composer(self):
        for name in ("Message #search-team", "Message #find-a-mentor",
                     "Message #url-shortener", "Reply to #filter-dev"):
            with self.subTest(name=name):
                self.assertTrue(enter_asks(name, "editable"))
                action = replace(BASE, kind="type", name=name, role="editable", submit=True)
                self.assertEqual(decide(action).decision, ASK)


class F5InvisibleCharactersSplitWords(unittest.TestCase):
    """MED: format characters and default-ignorable marks inside a word become
    spaces, so the label misses, while statement() strips the same Cf characters
    and would show the owner "Send". Turkish dotted I casefolds to i + U+0307,
    which also splits the word."""

    def test_invisible_characters_do_not_hide_a_word(self):
        for name in ("Se​nd", "Se‍nd", "Se­nd", "Se⁠nd", "Se﻿nd",
                     "Se͏nd", "Se️nd"):
            with self.subTest(name=ascii(name)):
                self.assertEqual(label_group(name), "send")
                self.assertEqual(decide(replace(BASE, name=name)).decision, ASK)

    def test_turkish_dotted_capital_i(self):
        self.assertEqual(label_group("SUBMİT"), "send")
        self.assertEqual(label_group("İNSTALL"), "install")


class F6CardHidesTheLabel(unittest.TestCase):
    """MED: invisible marks (variation selectors, CGJ) and blank-looking symbols
    survive sanitising; as padding they push the real label past the 60-char cut,
    so a Pay ask reads as a click on an empty-looking control."""

    def test_padding_does_not_push_the_label_off_the_card(self):
        for pad in ("︀", "͏", "⠀"):
            with self.subTest(pad=ascii(pad)):
                action = replace(BASE, name=pad * 80 + "Pay")
                self.assertEqual(decide(action).group, "pay")
                self.assertIn("Pay", statement(action, decide(action)))


class F7LookAlikeQuotes(unittest.TestCase):
    """LOW: only ASCII '"' is replaced; look-alike double quotes survive and
    forge the end of the quoted name."""

    def test_only_the_two_framing_quotes_look_like_quotes(self):
        for q_open, q_close in (("＂", "＂"), ("“", "”"), ("ʺ", "ʺ")):
            with self.subTest(quote=ascii(q_open)):
                action = replace(BASE, name=f"OK{q_close} in Notepad. Click {q_open}Close")
                card = statement(action, decide(action))
                self.assertFalse(any(char in card for char in "＂“”ʺ"), card)


class F8LoneSurrogate(unittest.TestCase):
    """LOW: category Cs is not removed, so the card cannot be UTF-8 encoded."""

    def test_card_is_encodable(self):
        action = replace(BASE, name="Se\ud800nd")
        statement(action, decide(action)).encode("utf-8")


class F9AppNameWithoutContent(unittest.TestCase):
    """LOW: an app name of dots/spaces or an invisible letter (Hangul filler)
    renders as "in ..." / an apparently blank name instead of the fallback."""

    def test_fallback_for_empty_looking_app(self):
        for app in ("...", " . . . ", " - ", "ㅤㅤㅤ"):
            with self.subTest(app=ascii(app)):
                self.assertEqual(statement(replace(BASE, app=app), None),
                                 'Click "Send" in an unknown app')


class F10OddInstances(unittest.TestCase):
    """MED: decide() and voice_may_approve() raise on an instance whose field
    read raises; subclasses that answer differently on a second read fail open."""

    def test_decide_refuses_unreadable_instance(self):
        class UnreadableApp(Action):
            def __getattribute__(self, field):
                if field == "app":
                    raise ValueError("unreadable")
                return super().__getattribute__(field)

        verdict = decide(UnreadableApp("click", "Outlook", purpose="Click.", risky="no"))
        self.assertEqual(verdict.decision, REFUSE)

    def test_voice_does_not_raise(self):
        class Unreadable(Verdict):
            @property
            def decision(self):
                raise ValueError("unreadable")

            @decision.setter
            def decision(self, value):
                pass

        self.assertFalse(voice_may_approve(Unreadable(ASK, "pay")))

    def test_values_read_twice_do_not_fail_open(self):
        class Flip(Action):
            reads = [0]

            @property
            def risky(self):
                self.reads[0] += 1
                return "no" if self.reads[0] > 2 else "yes"

            @risky.setter
            def risky(self, value):
                pass

        self.assertNotEqual(decide(Flip("click", "Outlook", "Open", "button", purpose="p")).decision, RUN)

        class NeverDiffers(str):
            def __ne__(self, other):
                return False

            __hash__ = str.__hash__

        self.assertNotEqual(decide(replace(BASE, name="Open", risky=NeverDiffers("yes"))).decision, RUN)

        class IterOnce(tuple):
            calls = [0]

            def __iter__(self):
                self.calls[0] += 1
                return iter(()) if self.calls[0] > 1 else tuple.__iter__(self)

        action = replace(BASE, kind="key", keys=IterOnce(("enter",)), name="Pay")
        self.assertNotEqual(decide(action).decision, RUN)


class F11RoleVocabulary(unittest.TestCase):
    """MED (contract): UIA's text box is "Edit" and ARIA's is "textbox"; both
    count as "any other role", so Enter in a chat composer runs unasked, and a
    click into a comment box asks every time."""

    def test_common_text_box_roles(self):
        for role in ("Edit", "textbox", "text"):
            with self.subTest(role=role):
                self.assertTrue(enter_asks("Type a message", role))
                self.assertEqual(label_group("Add a comment", role), "")


class F12WordCountInflation(unittest.TestCase):
    """LOW: words are counted after punctuation becomes spaces, so an address or
    a price adds several "words" and pushes a short label over 12."""

    def test_addresses_and_prices_count_as_one_word(self):
        self.assertEqual(label_group("Pay $1,234.56 to john.doe@example.com and jane.roe@example.com"), "pay")
        self.assertEqual(label_group("Send to a@b.com, c@d.com, e@f.com, g@h.com"), "send")


class F13PurposeControlCharacters(unittest.TestCase):
    """LOW: only C0 and DEL are refused; C1 controls, line/paragraph
    separators, bidi overrides and an invisible-only purpose pass."""

    def test_purpose_refuses_other_control_and_invisible_text(self):
        for purpose in ("x\x85y", "x y", "x‮y", "​"):
            with self.subTest(purpose=ascii(purpose)):
                self.assertEqual(decide(replace(BASE, name="Open", purpose=purpose)).decision, REFUSE)


class F14HandBuiltVerdict(unittest.TestCase):
    """LOW: voice_may_approve trusts any group spelling."""

    def test_pay_spellings_and_unknown_groups(self):
        for group in ("Pay", " pay", "PAY", "money"):
            with self.subTest(group=group):
                self.assertFalse(voice_may_approve(Verdict(ASK, group)))


if __name__ == "__main__":
    unittest.main()
