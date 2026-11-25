"""Keep revision identities distinct when a vault entry is recreated."""

from datetime import date
import unittest
from unittest.mock import patch

from a6c1_acceptance_check import ADD, Base
from jarvis.v3 import vault


class VaultRevisionReview(Base):
    def test_same_day_recreated_entry_has_a_distinct_revision(self):
        store = self.vault()
        with patch.object(vault, "date", wraps=date) as calendar:
            calendar.today.return_value = date(2030, 1, 7)
            original, _ = store.add(**ADD)
            ref = original.location.ref
            self.assertEqual(store.read(ref).revision, original.revision)
            store.archive(ref)
            recreated, _ = store.add(**ADD)
            self.assertEqual(store.read(ref).revision, recreated.revision)

        self.assertEqual(recreated.location, original.location)
        self.assertEqual(recreated.body, original.body)
        self.assertEqual(recreated.updated, original.updated)
        self.assertIsNotNone(recreated.revision)
        self.assertNotEqual(recreated.revision, original.revision,
                            "A recreated entry must have its own revision identity.")


if __name__ == "__main__":
    unittest.main()
