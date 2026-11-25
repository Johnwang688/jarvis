"""I2 redaction across snapshot boundaries and formatted metadata."""

import unittest
from datetime import date

from i2_acceptance_check import Base, NOW, cal
from jarvis.v3.commitments import Commitment


class DeltaRedaction(Base):
    def test_calendar_url_password_is_withheld_across_the_snapshot_cut(self):
        hidden = "fake-password"
        # The C3 writer's 512-character title limit falls just before the @.
        userinfo = f"https://example-user:{hidden}"
        padding = ("Workshop agenda " * 34)[:511 - len(userinfo)] + " "
        title = padding + userinfo + "@example.invalid/agenda"
        self.put("calendar", [cal("workshop")])
        self.assertEqual(self.tick(("calendar",))["emitted"], 0)
        self.put("calendar", [cal("workshop", title=title)])
        self.assertEqual(self.tick(("calendar",), at=NOW + 300)["emitted"], 1)

        previous = (self.root / "previous/calendar.json").read_text()
        self.assertNotIn(hidden, previous)

    def test_typographic_whitespace_does_not_expose_labelled_secrets(self):
        password = "fake-password"
        token = "fake-token"
        self.index.add("m1", "t1", NOW - 7200, labels=("INBOX", f"Token\u00a0=\u00a0{token}"))
        self.rows = [
            Commitment("c-0000000a", "Example", f"Password\u00a0:\u00a0{password}",
                       date(2027, 1, 17), "owner-owes", "gmail:m1", "open"),
        ]
        self.assertEqual(self.tick(("commitments", "mail"))["emitted"], 2)
        for path, secret in ((self.sig / "commitments.json", password),
                             (self.sig / "mail.json", token)):
            with self.subTest(snapshot=path.name):
                self.assertNotIn(secret, path.read_text())


if __name__ == "__main__":
    unittest.main()
