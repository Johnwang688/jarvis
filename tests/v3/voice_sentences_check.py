"""A long opening sentence goes to speech clause first, released when the whole sentence would be."""

import unittest

from chat_check import reply
from jarvis.v3.voice.sentences import CLAUSE_AFTER, CLAUSE_MIN, MIN_SENTENCE, Sentences, split_opening


class SplitOpening(unittest.TestCase):
    def test_a_long_sentence_is_split_at_its_first_usable_clause(self):
        cases = {
            "It is ten to three in the morning on Thursday, the twenty-sixth of November, Central time.":
                ["It is ten to three in the morning on Thursday,",
                 "the twenty-sixth of November, Central time."],
            "First, test the fixture on the actual field, or a close copy of it, as early as you can.":
                ["First, test the fixture on the actual field,", "or a close copy of it, as early as you can."],
            "The fixture can open several programs for you: a browser, an editor and a terminal window.":
                ["The fixture can open several programs for you:", "a browser, an editor and a terminal window."],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(split_opening(text), expected)
                self.assertEqual(" ".join(split_opening(text)), text)

    def test_short_sentences_and_unusable_commas_stay_whole(self):
        for text in (
            "The fixture is open.",
            "The research is mixed, and it depends on who you ask.",          # shorter than the threshold
            "Got it, the fixture meeting has been moved to Friday afternoon at four.",  # comma too early
            "The total came to 1,200 for the 3,000 fixtures that were ordered in March.",  # commas in numbers
            "It starts at 12:46 and the fixture should be finished before the lunch break begins.",  # a time
            "A very long opening sentence that has no clause boundary anywhere inside it at all.",
            "The fixture order for the whole of last week has now been delivered, I think.",  # rest too short
        ):
            with self.subTest(text=text):
                self.assertEqual(split_opening(text), [text])

    def test_each_piece_can_stand_alone(self):
        text = ("Yes, it is, and the fixture you asked about was delivered to the front desk this morning, "
                "so you can collect it whenever you like.")
        first, rest = split_opening(text)
        self.assertGreaterEqual(len(first), CLAUSE_MIN)
        self.assertGreaterEqual(len(first.split()), 3)
        self.assertGreaterEqual(len(rest), MIN_SENTENCE)
        self.assertGreater(CLAUSE_AFTER, CLAUSE_MIN)

    def test_the_parser_itself_is_unchanged(self):
        text = "It is ten to three in the morning on Thursday, the twenty-sixth of November, Central time."
        self.assertEqual(list(Sentences().feed(text, final=True)), [text])


class OpeningInATurn(unittest.TestCase):
    """Through the real voice turn code: only the opening sentence of a reply is split."""

    def setUp(self):
        import voice_turns_check as fixture

        self.fixture = fixture.VoiceTurnsCheck("test_real_broker_approval_is_announced_before_the_write")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_only_the_opening_sentence_is_split_and_the_text_is_whole(self):
        opening = "It is ten to three in the morning on Thursday, the twenty-sixth of November, Central time."
        later = "The fixture can open several programs for you: a browser, an editor and a terminal window."
        self.fixture.start(reply(f"{opening} {later}"))
        self.fixture.voice.heard("Tell me the fixture time.")
        self.fixture.wait_chat("reply", 1)
        self.fixture.play_all()
        self.assertEqual(self.fixture.texts(), [*split_opening(opening), later])
        self.assertEqual(" ".join(self.fixture.texts()), f"{opening} {later}")


if __name__ == "__main__":
    unittest.main()
