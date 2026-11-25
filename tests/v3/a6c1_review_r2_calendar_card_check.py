"""Calendar approval cards preserve the description submitted for review."""

import unittest

from a6c1_acceptance_check import Base, CREATE, reply


class CalendarCardReview(Base):
    def test_create_and_update_cards_show_the_entire_description(self):
        self.token.write_text("fake-token")
        description = "Meeting notes for the planning session.\n" * 40 + "Enter through the west lobby."
        calls = (
            ("calendar_create", dict(CREATE, description=description), "Description"),
            ("calendar_update", {"event_id": "e1", "description": description}, "New description"),
        )
        for tool, args, label in calls:
            with self.subTest(tool=tool):
                service = self.talk(reply(calls=[(tool, args)]), reply("Kept unchanged."))
                service.send("Review the appointment details.")
                approval = self.next_card()
                rows = {row["label"]: row["value"] for row in approval["card"]["rows"]}
                self.broker.answer(approval["id"], approval["digest"], "deny")
                self.assertEqual(self.wait_idle(service), "idle")
                self.assertEqual(self.cal.writes(), [])
                self.assertTrue(rows[label] == description, "The card omitted part of the description.")


if __name__ == "__main__":
    unittest.main()
