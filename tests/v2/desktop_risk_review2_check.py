"""Second review of jarvis/v2/desktop_risk.py (CU-2a fix round). Each test
states the behaviour the review expects; a failure is an open finding."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.desktop_risk import ASK, Action, decide, label_group, statement


BASE = Action("click", "Outlook", "Send", "button", purpose="Do the step.", risky="no")

# Default_Ignorable_Code_Point characters of category Cn: they render as
# nothing, but neither the matcher nor the card deletes them.
UNASSIGNED_INVISIBLE = ("⁥", "￰", "\U000e0000", "\U000e0002", "\U000e0080", "\U000e01f0")


class N1UnassignedInvisibleCharacters(unittest.TestCase):
    """MED: F5/F6/F12 class. Invisible Cn code points (U+2065, U+FFF0-FFF8,
    U+E0000, U+E0002-E001F, U+E0080-E00FF, U+E01F0-E0FFF) split a word, inflate
    the word count, push the label off the card and leave a card that reads as
    an empty quoted name."""

    def test_inside_a_word(self):
        for char in UNASSIGNED_INVISIBLE:
            with self.subTest(char=ascii(char)):
                name = f"Se{char}nd"
                self.assertEqual(label_group(name), "send")
                self.assertEqual(decide(replace(BASE, name=name)).decision, ASK)

    def test_word_count(self):
        for char in UNASSIGNED_INVISIBLE:
            with self.subTest(char=ascii(char)):
                self.assertEqual(label_group("Pay" + f" {char}" * 12), "pay")

    def test_card_is_not_visually_empty(self):
        for char in UNASSIGNED_INVISIBLE:
            with self.subTest(char=ascii(char)):
                action = replace(BASE, name=char * 3, risky="yes")
                self.assertNotIn(char, statement(action, decide(action)))

    def test_padding_does_not_push_the_label_off_the_card(self):
        for char in UNASSIGNED_INVISIBLE:
            with self.subTest(char=ascii(char)):
                action = replace(BASE, name=char * 80 + "Pay")
                self.assertIn("Pay", statement(action, decide(action)))


class N2ComposedCombiningMark(unittest.TestCase):
    """MED: F5 class. NFKC composes a letter with a following combining mark
    (e + U+0301 -> U+00E9), so the Mn deletion never sees it and the word
    misses, while the card deletes the same mark and shows "Send"/"Pay".
    U+0338 (no composition) is caught; U+0301 is not."""

    def test_composable_marks_do_not_hide_a_word(self):
        for name, group in (("Sénd", "send"), ("Páy", "pay"),
                            ("Rémove", "delete"), ("Instàll", "install")):
            with self.subTest(name=ascii(name)):
                self.assertEqual(label_group(name), group)
                self.assertEqual(decide(replace(BASE, name=name)).decision, ASK)


class N3QuoteLookAlikes(unittest.TestCase):
    """LOW: F7 not fully closed. Double-quote look-alikes outside the list and
    outside Pi/Pf survive and forge the end of the quoted name."""

    def test_look_alike_double_quotes_are_replaced(self):
        for quote in ("ˮ", "״", "‶", "〝", "〞", "〟",
                      "❝", "❞", "˝", "⹂"):
            with self.subTest(quote=ascii(quote)):
                action = replace(BASE, name=f"OK{quote} in Notepad. Click {quote}Close")
                self.assertNotIn(quote, statement(action, decide(action)))


class N4WhitespacePadding(unittest.TestCase):
    """LOW: F6 class. Runs of spaces inside the name are kept, so a short word
    plus 60 spaces pushes the real label past the cut ('Click "Open      …"')."""

    def test_space_runs_do_not_push_the_label_off_the_card(self):
        for pad in (" ", " ", "　"):
            with self.subTest(pad=ascii(pad)):
                action = replace(BASE, name="Open" + pad * 80 + "Pay")
                self.assertIn("Pay", statement(action, decide(action)))


if __name__ == "__main__":
    unittest.main()
