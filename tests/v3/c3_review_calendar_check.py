import unittest
from unittest.mock import patch

from c3_acceptance_check import Base, FakeCalendar, cal_event
from jarvis.v3.ingest.calendar import CalendarIngester, SyncTokenExpired


class CalendarBackfillReview(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_expired_full_sync_page_recovers_after_restart(self, _secrets):
        api = FakeCalendar([cal_event("a", "First event"), cal_event("b", "Second event")], page=1)

        def runner():
            return self.runner(CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now))

        self.assertIsNone(runner().tick()["calendar"]["error"])
        _, cursor = self.index.get_ingest_cursor("calendar")
        expired = cursor["page"]
        list_events = api.list_events

        def expire_continuation(calendar, **params):
            if params.get("page_token") == expired:
                raise SyncTokenExpired()
            api.page = 2
            return list_events(calendar, **params)

        restarted = runner()
        with patch.object(api, "list_events", side_effect=expire_continuation):
            result = restarted.tick()["calendar"]
            if result["error"] is not None:
                self.now = result["next"] + 1
                result = restarted.tick()["calendar"]

        self.assertIsNone(result["error"], "The runner kept retrying an expired backfill page")
        self.assertEqual(set(self.metas()), {"a", "b"})
        self.assertEqual(set(self.items("calendar")), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
