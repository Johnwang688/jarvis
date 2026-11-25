"""Review patches should accept an ordinary fact with a parenthetical list."""

import unittest

import l2_review_memory_writes_check as memory_writes


class ReviewerPatchProse(unittest.TestCase):
    def test_parenthetical_role_list_does_not_block_unrelated_patch(self):
        fixture = memory_writes.ReviewerMemoryWrites()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        before = "The review panel (assistant, reviewer, and owner) meets on Thursdays."
        result, entry = fixture.patch_fact(before, "Thursdays", "Fridays")
        self.assertFalse(result["isError"], result)
        self.assertEqual(entry.body.strip(), before.replace("Thursdays", "Fridays"))


if __name__ == "__main__":
    unittest.main()
