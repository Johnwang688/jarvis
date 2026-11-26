"""Clause-first playback preserves the existing speakable text."""

import unittest

from jarvis.v3.voice.sentences import Sentences


def spoken(text, chunk_size):
    sentences = Sentences()
    parts = []
    for start in range(0, len(text), chunk_size):
        end = start + chunk_size
        parts.extend(sentences.feed(text[start:end], final=end >= len(text)))
    return " ".join(parts)


class ClauseTextCheck(unittest.TestCase):
    def assert_text_at_every_chunk_size(self, raw, expected):
        differences = {}
        for size in range(1, 51):
            actual = spoken(raw, size)
            if actual != expected:
                differences.setdefault(actual, []).append(size)
        self.assertFalse(differences, f"Expected {expected!r}. Output by chunk size: {differences!r}")

    def test_a_table_pipe_does_not_shift_an_already_consumed_prefix(self):
        raw = ("The fixture | is ready, take the longer aisle around the display"
               " | Left entrance | Right entrance.")
        expected = ("The fixture, is ready, take the longer aisle around the display,"
                    " Left entrance, Right entrance.")
        self.assert_text_at_every_chunk_size(raw, expected)

    def test_punctuation_only_tail_survives_a_split(self):
        text = "The fixtures have arrived at the front desk this morning as promised, 🎉."
        self.assert_text_at_every_chunk_size(text, text)


if __name__ == "__main__":
    unittest.main()
