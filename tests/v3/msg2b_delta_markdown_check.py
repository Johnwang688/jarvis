"""Check that normal Markdown briefs can be delivered as proposals."""

import unittest

import msg2b_check as fixtures
from planner_check import basis, proposal


class MarkdownBriefs(unittest.TestCase):
    def delivery(self, brief):
        fx = fixtures.PlannerMessages()
        self.addCleanup(fx.doCleanups)
        fx.setUp()
        item = proposal(basis(fx.signal()), "thread.start")
        item["action"]["brief"] = brief
        self.assertEqual(fx.run_items(item).posted, 1)
        return len(fx.t3.starts), len(fx.store.proposals("briefed"))

    def test_markdown_briefs_are_delivered(self):
        for shape, brief in (
            ("prose", "Compare the two fixture outputs."),
            ("bullets", "- Compare the outputs.\n- Summarize the changes."),
            ("numbered", "1. Compare the outputs.\n2. Summarize the changes."),
            ("code fence", "Compare the literal values.\n\n```text\nalpha\n```"),
            ("heading", "## Fixture outputs\n\nCompare the results."),
            ("table with leading pipe", "| Input | Output |\n| --- | --- |\n| Alpha | Beta |"),
            ("table without leading pipe", "Input | Output\n--- | ---\nAlpha | Beta"),
            ("bulleted CLI flags", "Review these documented flags.\n\n- --help\n- --verbose"),
            ("equals line", "Fixture outputs\n===\nCompare the results."),
            ("asterisk rule", "Compare the results.\n\n***\n\nSummarize the changes."),
            ("underscore rule", "Compare the results.\n\n___\n\nSummarize the changes."),
        ):
            with self.subTest(shape=shape):
                self.assertEqual(self.delivery(brief), (1, 0))


if __name__ == "__main__":
    unittest.main()
