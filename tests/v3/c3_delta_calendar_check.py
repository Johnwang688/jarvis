import unittest
from unittest.mock import patch

import httpx

from c3_acceptance_check import Base, cal_event
from jarvis.v3.ingest.calendar import CalendarIngester, HttpCalendar


class CalendarDelta(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_failed_replacement_does_not_retry_known_expired_token(self, _secrets):
        params = []

        def respond(request):
            params.append(dict(request.url.params))
            if "syncToken" in params[-1]:
                return httpx.Response(410)
            if len(params) == 1:
                return httpx.Response(200, json={"items": [cal_event("gone", "Old event")],
                                                 "nextSyncToken": "expired-sync"})
            if len(params) == 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"items": [], "nextSyncToken": "fresh-sync"})

        api = HttpCalendar(lambda: "fake-token", transport=httpx.MockTransport(respond))
        self.addCleanup(api.close)
        runner = self.runner(CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now))
        self.assertIsNone(runner.tick()["calendar"]["error"])
        self.now += 301
        failed = runner.tick()["calendar"]
        self.assertEqual(len(params), 3)
        self.assertEqual(failed["deleted"], 0)
        self.assertIn("gone", self.items("calendar"))
        self.now = failed["next"] + 1
        recovered = runner.tick()["calendar"]

        self.assertEqual(recovered["deleted"], 1)
        self.assertEqual(self.metas(), {})
        self.assertEqual(self.items("calendar"), {})
        self.assertEqual([req["syncToken"] for req in params if "syncToken" in req], ["expired-sync"])


if __name__ == "__main__":
    unittest.main()
