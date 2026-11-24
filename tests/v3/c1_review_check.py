"""Offline regressions for Calendar ownership, responses, availability and OAuth."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from jarvis import google_auth
from jarvis.v3.tools import calendar


def owned_event():
    return {
        "id": "event1",
        "etag": '"before"',
        "summary": "Study",
        "creator": {"self": True},
        "organizer": {"self": True},
        "extendedProperties": {"private": {"jarvis": "1"}},
    }


class CalendarReview(unittest.TestCase):

    def test_patch_200_without_matching_id_reports_uncertain_result(self):
        for body in ({}, {"id": "other-event"}):
            with self.subTest(body=body):
                writes = []

                def respond(request):
                    if request.method == "GET":
                        return httpx.Response(200, json=owned_event())
                    writes.append(request.method)
                    return httpx.Response(200, json=body)

                tool = calendar.CalendarTools(
                    transport=httpx.MockTransport(respond), approve=lambda *args: True,
                )
                with patch.object(google_auth, "access_token", return_value="fake-token"):
                    result = tool.calendar_update("event1", title="New title")
                self.assertEqual(writes, ["PATCH"])
                self.assertTrue(result.startswith("Error:"), result)
                self.assertRegex(result.lower(), r"may have|might have|uncertain|could not confirm")

    def test_all_day_event_spans_santiago_midnight_dst_gap(self):
        page = {"timeZone": "America/Santiago", "items": [{
            "id": "event1",
            "start": {"date": "2026-09-06"},
            "end": {"date": "2026-09-07"},
        }]}
        tool = calendar.CalendarTools(request=lambda *args, **kwargs: (200, page))
        result = tool.calendar_free("2026-09-06T03:00Z", "2026-09-07T04:00Z")
        # September 6 starts at 01:00 in Santiago (04:00 UTC). The end is
        # the next midnight in Santiago (03:00 UTC), exclusive.
        self.assertEqual(result.splitlines(), [
            "Free intervals (America/Chicago)",
            "2026-09-05 22:00 CDT to 2026-09-05 23:00 CDT",
            "2026-09-06 22:00 CDT to 2026-09-06 23:00 CDT",
        ])

    def test_rotated_refresh_token_is_persisted_before_calendar_uses_access_token(self):
        with TemporaryDirectory() as directory:
            token_path = Path(directory) / "google-token.json"
            token_path.touch(mode=0o600)
            token_path.write_text(json.dumps({
                "client_id": "fake-client-id",
                "client_secret": "fake-client-secret",
                "refresh_token": "fake-old-refresh-token",
            }), encoding="utf-8")
            seen = []

            def respond(request):
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                seen.append((request.headers["Authorization"], saved["refresh_token"]))
                return httpx.Response(200, json={"items": []})

            renewed = httpx.Response(200, json={
                "access_token": "fake-new-access-token", "expires_in": 3600,
                "refresh_token": "fake-rotated-refresh-token",
            })
            tool = calendar.CalendarTools(transport=httpx.MockTransport(respond))
            with (patch.object(google_auth.config, "GOOGLE_TOKEN_PATH", token_path),
                  patch.object(google_auth, "_cached_token", None),
                  patch.object(google_auth, "_expires_at", 0),
                  patch.object(httpx, "post", return_value=renewed)):
                result = tool.calendar_list("2026-11-25", "2026-11-26")
            self.assertEqual(result, "No events to display.")
            self.assertEqual(seen, [("Bearer fake-new-access-token", "fake-rotated-refresh-token")])



if __name__ == "__main__":
    unittest.main()
