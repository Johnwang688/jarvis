import unittest
from unittest.mock import patch

import httpx

from c3_acceptance_check import Base, FakeCalendar, cal_event
from jarvis.v3.ingest.calendar import CalendarIngester, HttpCalendar


class CalendarSignalReview(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_calendar_title_is_bounded_in_trigger_snapshot(self, _secrets):
        title = "Example meeting " + "display text " * 512
        api = FakeCalendar([cal_event("meeting", title)])
        ingester = CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now)
        docs, _ = ingester.poll(None)

        self.assertEqual([doc.ref for doc in docs], ["meeting"])
        summary = self.items("calendar")["meeting"]["title"]
        self.assertTrue(summary.startswith("Example meeting"))
        # Generous per-title budget for a trigger summary.
        self.assertLessEqual(len(summary.encode("utf-8")), 4096)

    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_transient_google_access_token_is_withheld_from_docs_and_signals(self, _secrets):
        token = "ya29.fake-OAuth-access-token-example-001"

        def fake_response(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["Authorization"], f"Bearer {token}")
            event = cal_event("meeting", f"Example meeting {token}", description=token)
            return httpx.Response(200, json={"items": [event], "nextSyncToken": "fake-sync"})

        calendar = HttpCalendar(lambda: token, transport=httpx.MockTransport(fake_response))
        self.addCleanup(calendar.close)
        docs, cursor = CalendarIngester(calendar, signals_dir=self.sig, clock=lambda: self.now).poll(None)
        self.assertEqual([doc.ref for doc in docs], ["meeting"])
        for surface, content in (("document", repr(docs)), ("cursor", repr(cursor)),
                                 ("snapshot", (self.sig / "calendar.json").read_text(encoding="utf-8"))):
            with self.subTest(surface=surface):
                self.assertNotIn(token, content)


if __name__ == "__main__":
    unittest.main()
