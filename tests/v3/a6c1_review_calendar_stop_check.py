"""Stop must prevent a Calendar write that has not reached the provider."""

import threading
import unittest

from a6c1_acceptance_check import Base, Cal, reply


class CalendarStopReview(Base):
    def test_stop_during_the_final_event_read_prevents_the_calendar_write(self):
        final_read = threading.Event()
        resume_read = threading.Event()

        class PausedCalendar(Cal):
            def __call__(self, method, url, *, params=None, json=None):
                result = super().__call__(method, url, params=params, json=json)
                if method == "GET" and len(self.calls) == 3:
                    final_read.set()
                    if not resume_read.wait(5):
                        raise TimeoutError("The test did not release the final read.")
                return result

        self.cal = PausedCalendar()
        self.token.write_text("fake-token")
        service = self.talk(reply(calls=[("calendar_delete", {"event_id": "e1"})]), reply("Done."))
        service.send("Delete the appointment.")
        card = self.next_card()
        self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
        try:
            self.assertTrue(final_read.wait(5), "Calendar did not reach its last eligibility check.")
            self.assertEqual(service.stop()["state"], "stopping")
        finally:
            resume_read.set()

        self.assertEqual(self.wait_idle(service), "idle")
        self.assertIn("stopped", self.kinds(service))
        self.assertEqual(self.cal.writes(), [], "Calendar received a write after Stop.")


if __name__ == "__main__":
    unittest.main()
