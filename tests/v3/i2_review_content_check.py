"""I2 must withhold labelled credentials from its snapshots and signal log."""

import json
import unittest
from datetime import date

from i2_acceptance_check import Base, NOW
from jarvis.v3.commitments import Commitment


class CredentialTitles(Base):
    def test_labelled_password_is_withheld_at_each_output(self):
        password = "fake-password"
        self.index.add("m1", "t1", NOW - 7200, title=f"Re: lab setup\nPassword: {password}")
        self.rows = [
            Commitment("c-0000000a", "Example", f"Rotate test password: {password}",
                       date(2027, 1, 17), "owner-owes", "gmail:m1", "open"),
        ]
        self.assertEqual(self.tick(("mail", "commitments"))["emitted"], 2)
        self.assertEqual(self.posts.attempts, [])
        for row in self.log():
            with self.subTest(kind=row["kind"]):
                self.assertIs(row["tainted"], True)
                self.assertNotIn(password, row["summary_fields"]["untrusted_title"])
        for source, title_field in (("mail", "title"), ("commitments", "what")):
            with self.subTest(snapshot=source):
                snapshot = json.loads((self.sig / f"{source}.json").read_text())
                self.assertNotIn(password, snapshot["items"][0][title_field])
        self.assertNotIn(password, (self.root / "signals.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
