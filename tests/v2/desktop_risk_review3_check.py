"""Third review of jarvis/v2/desktop_risk.py (CU-2a fix round 2). Each test
states the behaviour the review expects; a failure is an open finding."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.desktop_risk import (
    ASK, Action, decide, label_group, statement, voice_may_approve,
)


BASE = Action("click", "Outlook", "Send", "button", purpose="Do the step.", risky="no")
KEY = replace(BASE, kind="key", name="", role="")


class G1LineBreaksMergeWords(unittest.TestCase):
    """HIGH: Cc characters are deleted, not read as a gap, so a line break or
    tab between two words merges them ("Pay\\nnow" -> "paynow") and the label
    misses. The card shows "Paynow". Chrome's accessible name for
    <button>Pay<br>now</button> and Win32 multi-line button text carry such
    breaks. Note: tests/v2/desktop_risk_check.py asserts "Se\\x85nd" is "send";
    reading these characters both deleted and as a space (as "&" is) keeps
    both."""

    NAMES = (("Pay\nnow", "pay"), ("Buy\r\nnow", "pay"), ("Delete\naccount", "delete"),
             ("Send\tmessage", "send"), ("Clear\x0call", "delete"), ("Pay\x85now", "pay"),
             ("Pay\x0bnow", "pay"))

    def test_label_group(self):
        for name, group in self.NAMES:
            with self.subTest(name=ascii(name)):
                self.assertEqual(label_group(name), group)

    def test_click_asks(self):
        for name, group in self.NAMES:
            with self.subTest(name=ascii(name)):
                verdict = decide(replace(BASE, name=name))
                self.assertEqual((verdict.decision, verdict.group), (ASK, group))

    def test_card_keeps_the_gap(self):
        action = replace(BASE, name="Pay\nnow", risky="yes")
        self.assertIn('"Pay now"', statement(action, decide(action)))


class G2LookupWordHidesTheLabelOnEnter(unittest.TestCase):
    """MED: for a role this module cannot name (UIA "custom", "group", "pane",
    "data item", static "text", ARIA "gridcell"), Enter is judged only by the
    lookup exemption, never by the label group. "Find and delete duplicates"
    gives RUN on Enter and ASK on Space for the same focused control."""

    def test_enter_on_unknown_role_follows_the_label(self):
        for role in ("custom", "text", "group", "data item", "gridcell", ""):
            for name, group in (("Find and delete duplicates", "delete"),
                                ("Search and pay", "pay"), ("Filter & remove all", "delete")):
                with self.subTest(role=role, name=name):
                    verdict = decide(replace(KEY, name=name, role=role, keys=("enter",)))
                    self.assertEqual((verdict.decision, verdict.group), (ASK, group))


class G3TeamsCallShortcuts(unittest.TestCase):
    """MED: "shift allowed beside" lets Ctrl+Shift+<allowlisted letter> run.
    Microsoft's published Teams shortcuts include Ctrl+Shift+E (start screen
    share), Ctrl+Shift+A / Ctrl+Shift+S (accept video / audio call), Ctrl+Shift+C
    (start audio call) and Ctrl+Shift+U (start video call): a call and a screen
    share are "send"-class. Verify against the owner's Teams build."""

    def test_ctrl_shift_letters_ask(self):
        for letter in "eascu":
            with self.subTest(letter=letter):
                verdict = decide(replace(KEY, app="Microsoft Teams", keys=("ctrl", "shift", letter)))
                self.assertEqual(verdict.decision, ASK)


class G4DialogMnemonicLetters(unittest.TestCase):
    """MED: a bare letter runs on any role. In a standard Windows message box
    or task dialog, a letter pressed while a button has focus presses the
    button with that access key: "Y" confirms Explorer's "permanently delete"
    Yes/No box, "N" picks classic Notepad's "Don't Save". Alt+D (allowlisted)
    does the same for a "&Delete"/"&Discard" button in a dialog."""

    def test_letter_on_a_focused_button_asks(self):
        for name, letter in (("No", "y"), ("Save", "n")):
            with self.subTest(name=name, letter=letter):
                verdict = decide(replace(KEY, app="File Explorer", name=name, role="button", keys=(letter,)))
                self.assertEqual(verdict.decision, ASK)


class G5PayLabelVoice(unittest.TestCase):
    """LOW: a step that asks for a reason other than its label never reads the
    focused label for "pay", so voice may approve it: a non-allowlisted
    shortcut or Delete with focus on "Pay now"/"Buy now"."""

    def test_pay_focused_steps_are_not_voice_approvable(self):
        for name, keys in (("Pay now", ("ctrl", "q")), ("Buy now", ("delete",)),
                           ("Pay now", ("alt", "p"))):
            with self.subTest(name=name, keys=keys):
                verdict = decide(replace(KEY, name=name, role="button", keys=keys))
                self.assertEqual(verdict.decision, ASK)
                self.assertFalse(voice_may_approve(verdict))


class G6QuoteLookAlikesOnTheCard(unittest.TestCase):
    """LOW: F7/N3 class. Characters that draw like a double quote survive, and
    the replacement itself turns '""' into "''", which reads as '"'."""

    def test_no_double_quote_look_alike(self):
        for quote in ('""', "''", "˵", "΅", "῭", "΅", "῁", "¨", "⹉", "``", "´´", "ˊˊ", "ˋˋ"):
            with self.subTest(quote=ascii(quote)):
                action = replace(BASE, name=f"OK{quote} in Notepad. Click {quote}Close", risky="yes")
                card = statement(action, decide(action))
                body = card[card.index('"') + 1:card.rindex('"')]
                self.assertNotIn(quote, body)
                for pair in ("''", "``", "´´", "ˊˊ", "ˋˋ"):
                    self.assertNotIn(pair, body)


class G7NearInvisibleSplitters(unittest.TestCase):
    """LOW: blank characters outside the deleted set split or merge a word that
    a reader sees whole: hair/six-per-em/thin/narrow no-break spaces (Zs, read
    as a word break) and the Egyptian hieroglyph blanks U+13441/U+13442 (Lo,
    drawn as empty space like U+3164)."""

    def test_word_stays_whole(self):
        for char in (" ", " ", " ", " ", "\U00013441", "\U00013442"):
            with self.subTest(char=ascii(char)):
                self.assertEqual(label_group(f"Se{char}nd"), "send")


if __name__ == "__main__":
    unittest.main()
