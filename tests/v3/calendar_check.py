"""Offline Calendar checks. Every HTTP response is synthetic."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import httpx

from jarvis import google_auth, untrusted
from jarvis.v3.tools import calendar

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def event(start="2026-11-25T10:00:00-06:00", end="2026-11-25T11:00:00-06:00", **fields):
    def boundary(value):
        if isinstance(value, str):
            return {"dateTime" if "T" in value else "date": value}
        return value

    return {
        "id": "e1",
        "summary": "Example block",
        "start": boundary(start),
        "end": boundary(end),
        "status": "confirmed",
        "organizer": {"self": True},
        "creator": {"self": True},
        "extendedProperties": {"private": {"jarvis": "1"}},
        "etag": '"v1"',
        **fields,
    }


def listing(*events, **fields):
    return calendar.CalendarTools(request=lambda *args, **kwargs: (200, {"items": list(events), **fields}))


class CalendarTest(unittest.TestCase):
    def good(self, reply):
        self.assertFalse(reply.startswith("Error:"), reply)
        return reply

    def bad(self, reply, word=""):
        self.assertTrue(reply.startswith("Error:"), reply)
        self.assertIn(word, reply.lower())


class Availability(CalendarTest):
    def test_spring_day_has_23_hours(self):
        calls = []

        def request(method, url, *, params, json):
            calls.append(params)
            return 200, {"items": []}

        tool = calendar.CalendarTools(request=request)
        reply = self.good(tool.calendar_free("2026-03-08", "2026-03-09", 23 * 60))
        self.assertIn("2026-03-08 00:00 CST to 2026-03-09 00:00 CDT", reply)
        self.assertEqual(calls[0]["timeMin"], "2026-03-08T00:00:00-06:00")
        self.assertEqual(calls[0]["timeMax"], "2026-03-09T00:00:00-05:00")
        self.assertIn("No free", tool.calendar_free("2026-03-08", "2026-03-09", 23 * 60 + 1))
        full = listing(event("2026-03-08", "2026-03-09"))
        self.assertIn("No free", full.calendar_free("2026-03-08", "2026-03-09"))

    def test_fall_day_has_25_hours_and_orders_repeated_hour(self):
        self.assertIn(
            "2026-11-01 00:00 CDT to 2026-11-02 00:00 CST",
            self.good(listing().calendar_free("2026-11-01", "2026-11-02", 25 * 60)),
        )
        tool = listing(event("2026-11-01T01:30:00-05:00", "2026-11-01T07:15:00Z"))
        reply = self.good(tool.calendar_free("2026-11-01T01:00:00-05:00", "2026-11-01T01:45:00-06:00"))
        self.assertIn("2026-11-01 01:00 CDT to 2026-11-01 01:30 CDT", reply)
        self.assertIn("2026-11-01 01:15 CST to 2026-11-01 01:45 CST", reply)

    def test_ambiguous_and_missing_wall_times_need_an_offset(self):
        calls = []
        tool = calendar.CalendarTools(request=lambda *args, **kwargs: calls.append(args))
        for start, end in (
            ("2026-03-08T02:15", "2026-03-08T04:00"),
            ("2026-11-01T01:15", "2026-11-01T02:15"),
        ):
            with self.subTest(start=start):
                self.bad(tool.calendar_list(start, end), "offset")
        self.assertEqual(calls, [])

    def test_multiday_all_day_in_calendar_zone(self):
        tool = listing(event("2026-11-26", "2026-11-28"), timeZone="America/Los_Angeles")
        reply = self.good(tool.calendar_free("2026-11-26", "2026-11-29"))
        self.assertIn("2026-11-26 00:00 CST to 2026-11-26 02:00 CST", reply)
        self.assertIn("2026-11-28 02:00 CST to 2026-11-29 00:00 CST", reply)

    def test_busy_intervals_clip_and_merge_without_splitting_at_zero_length(self):
        tool = listing(
            event("2026-11-26T00:15:00-06:00", "2026-11-26T01:30:00-06:00",
                  id="overlap"),
            event("2026-11-24T23:00:00-06:00", "2026-11-26T00:30:00-06:00"),
            event("2026-11-26T01:30:00-06:00", "2026-11-26T03:00:00-06:00",
                  id="touches"),
            event("2026-11-26T08:15:00-06:00", "2026-11-26T08:15:00-06:00",
                  id="empty"),
            event("2026-11-26T10:00:00-06:00", "2026-11-28T00:00:00-06:00",
                  id="extends"),
        )
        reply = self.good(tool.calendar_free("2026-11-25", "2026-11-27"))
        self.assertEqual(reply.splitlines()[1:], ["2026-11-26 03:00 CST to 2026-11-26 10:00 CST"])

    def test_fractional_query_rounds_out_and_retains_exact_gap(self):
        queries = []

        def request(method, url, *, params, json):
            queries.append(params)
            return 200, {"items": [event("2026-11-25T10:00:00.1-06:00", "2026-11-25T10:00:00.2-06:00")]}

        tool = calendar.CalendarTools(request=request)
        start, end = "2026-11-25T10:00:00.3-06:00", "2026-11-25T11:00:00.4-06:00"
        self.assertIn("10:00:00.300000 CST", self.good(tool.calendar_free(start, end)))
        self.assertIn("No events to display", self.good(tool.calendar_list(start, end)))
        self.assertEqual(queries[0]["timeMin"], "2026-11-25T10:00:00-06:00")
        self.assertEqual(queries[0]["timeMax"], "2026-11-25T11:00:01-06:00")

    def test_invalid_page_and_event_data_never_yield_free_intervals(self):
        bad_pages = (
            None, [], {"items": None}, {"items": {}}, {"items": [], "nextPageToken": []},
            {"items": [], "timeZone": "../UTC"}, {"items": [{}] * 101},
        )
        for page in bad_pages:
            with self.subTest(page=type(page), keys=list(page)[:3] if isinstance(page, dict) else []):
                tool = calendar.CalendarTools(request=lambda *args, **kwargs: (200, page))
                self.bad(tool.calendar_free("2026-11-25", "2026-11-26"), "calendar")
        for malformed in (
            None, {}, event(start=None), event(attendees=[None]), event(attendees="no"),
            event(end={"date": "2026-11-26"}), event(end={"dateTime": "2026-11-25T02:00:00-06:00"}),
            event(start={"dateTime": "2026-11-25T10:00", "timeZone": "bad-zone"}),
        ):
            with self.subTest(malformed=malformed):
                self.bad(listing(malformed).calendar_free("2026-11-25", "2026-11-26"))
        for start, end in (
            ("0001-01-01T01:00Z", "0001-01-01T02:00Z"),
            ("9999-12-31T23:58Z", "9999-12-31T23:59:59.1Z"),
        ):
            self.bad(listing().calendar_free(start, end), "date range")

    def test_pagination_repeat_and_limit(self):
        tokens = []

        def repeating(method, url, *, params, json):
            tokens.append(params.get("pageToken"))
            return 200, {"nextPageToken": "same"}

        tool = calendar.CalendarTools(request=repeating)
        self.bad(tool.calendar_free("2026-11-25", "2026-11-26"), "repeating")
        self.assertEqual(tokens, [None, "same"])
        tokens.clear()

        def endless(method, url, *, params, json):
            tokens.append(params.get("pageToken"))
            return 200, {"items": [event()], "nextPageToken": str(len(tokens))}

        tool = calendar.CalendarTools(request=endless)
        self.assertIn("page limit reached", self.good(tool.calendar_list("2026-11-25", "2026-11-26")).lower())
        self.assertEqual(len(tokens), 10)
        tokens.clear()
        self.bad(tool.calendar_free("2026-11-25", "2026-11-26"), "page limit")
        self.assertEqual(len(tokens), 10)

    def test_list_fences_valid_text_and_reports_invalid_rows(self):
        valid = event(summary="Visible\ud800\u202e", description="Example text\u200b")
        reply = self.good(listing(None, valid, event(end={"date": "2026-11-26"})).calendar_list(
            "2026-11-25", "2026-11-26",
        ))
        self.assertTrue(untrusted.has_fence(reply))
        self.assertIn("Visible?", reply)
        self.assertIn("Example text", reply)
        self.assertNotIn("\u202e", reply)
        self.assertNotIn("\u200b", reply)
        self.assertIn("Some entries lacked valid event data.", reply)
        reply.encode("utf-8")

    def test_list_encodes_calendar_address_as_one_path_segment(self):
        calls = []

        def request(method, url, *, params, json):
            calls.append(url)
            return 200, {"items": []}

        tool = calendar.CalendarTools(request=request)
        self.good(tool.calendar_list("2026-11-25", "2026-11-26", "example#holiday@example.com"))
        self.assertEqual(calls, [BASE.replace("/primary/", "/example%23holiday%40example.com/")])
        self.bad(tool.calendar_list("2026-11-25", "2026-11-26", ".."))
        self.bad(tool.calendar_list("2026-11-25", "2026-11-26", "primary#fragment"))
        self.assertEqual(len(calls), 1)


class Writes(CalendarTest):
    def test_approval_snapshot_matches_write_and_callback_cannot_mutate_it(self):
        before = event()
        calls, approvals, clocks = [], [], []

        def request(method, url, *, params, json):
            calls.append((method, url, params, json))
            return 200, before

        def now():
            clocks.append(1)
            if approvals:
                raise AssertionError("Clock read after approval")
            return NOW

        def approve(action, details):
            approvals.append((action, deepcopy(details)))
            details["before"]["extendedProperties"]["private"].clear()
            details["params"]["sendUpdates"] = "all"
            details["url"] = "https://example.invalid/"
            details["body"]["start"]["dateTime"] = "altered"
            details["body"]["attendees"] = [{"email": "user@example.com"}]
            return True

        tool = calendar.CalendarTools(request=request, approve=approve, now=now)
        self.good(tool.calendar_update("e1", start="2026-11-25T09:15"))
        self.assertEqual([c[0] for c in calls], ["GET", "GET", "PATCH"])
        action, detail = approvals[0]
        self.assertEqual(action, "calendar.update")
        self.assertEqual(detail["before"], event())
        self.assertEqual(calls[-1], (detail["method"], detail["url"], detail["params"], detail["body"]))
        self.assertEqual(calls[-1][-1]["start"]["dateTime"], "2026-11-25T09:15:00-06:00")
        self.assertEqual(clocks, [1])

    def test_etag_and_scope_of_ownership_are_rechecked_independently(self):
        for changed in (
            {"etag": '"v2"'}, {"extendedProperties": {"private": {"jarvis": "0"}}},
            {"attendees": [{"email": "user@example.com"}]}, {"creator": {"self": False}},
            {"attendeesOmitted": True}, {"id": "e2"},
        ):
            for operation in ("calendar_update", "calendar_delete"):
                with self.subTest(changed=changed, operation=operation):
                    live = event()
                    calls = []

                    def request(method, url, *, params, json):
                        calls.append(method)
                        return 200, live

                    def approve(action, details):
                        live.update(changed)
                        return True

                    tool = calendar.CalendarTools(request=request, approve=approve)
                    args = {"title": "Example update"} if operation == "calendar_update" else {}
                    self.bad(getattr(tool, operation)("e1", **args), "changed")
                    self.assertEqual(calls, ["GET", "GET"])

    def test_unsafe_etags_are_refused_before_asking(self):
        for tag in (None, "", "*", 'W/"v1"', '"v1"\r\nIf-Match: *', '"v1", "v2"'):
            asked = []
            tool = calendar.CalendarTools(
                request=lambda *a, **k: (200, event(etag=tag)),
                approve=lambda *a: asked.append(a) or True,
            )
            self.bad(tool.calendar_delete("e1"), "eligible")
            self.assertEqual(asked, [])

    def test_default_update_and_delete_send_the_approved_precondition(self):
        for method in ("PATCH", "DELETE"):
            for status in (200 if method == "PATCH" else 204, 412):
                with self.subTest(method=method, status=status):
                    calls, approved = [], []

                    def request(req):
                        calls.append(req)
                        if req.method == "GET":
                            return httpx.Response(200, json=event())
                        return httpx.Response(status, json=event()) if status != 204 else httpx.Response(status)

                    with patch.object(google_auth, "access_token", return_value="fake-token"):
                        tool = calendar.CalendarTools(
                            transport=httpx.MockTransport(request),
                            approve=lambda action, details: approved.append(details) or True,
                        )
                        reply = tool.calendar_update("e1", title="Example update") if method == "PATCH" else tool.calendar_delete("e1")
                    self.bad(reply, "changed") if status == 412 else self.good(reply)
                    self.assertEqual([req.method for req in calls], ["GET", "GET", method])
                    self.assertEqual([req.headers.get("If-Match") for req in calls], [None, None, '"v1"'])
                    self.assertEqual(approved[0]["before"], event())
                    self.assertEqual(approved[0]["headers"], {"If-Match": '"v1"'})
                    self.assertEqual(approved[0]["url"], BASE + "/e1")
                    self.assertEqual(dict(calls[-1].url.params), approved[0]["params"])
                    self.assertEqual(json.loads(calls[-1].content or "null"), approved[0]["body"])


class CountingStream(httpx.SyncByteStream):
    def __init__(self, count, on_chunk=None):
        self.count = count
        self.on_chunk = on_chunk
        self.pulled = 0
        self.closed = False

    def __iter__(self):
        for _ in range(self.count):
            self.pulled += 1
            if self.on_chunk is not None:
                self.on_chunk()
            yield b" " * 65536

    def close(self):
        self.closed = True


class Transport(CalendarTest):
    def read(self, handler):
        with patch.object(google_auth, "access_token", return_value="fake-token"):
            tool = calendar.CalendarTools(transport=httpx.MockTransport(handler))
            return tool.calendar_list("2026-11-25", "2026-11-26")

    def test_default_caps_streaming_declared_and_buffered_sizes(self):
        limit = 2 * 1024 * 1024
        for headers, pulls, error in (
            ({}, 33, "size limit"),
            ({"content-length": str(limit + 1)}, 0, "size limit"),
            ({"content-length": "invalid"}, 0, "invalid length"),
            ({"content-encoding": "gzip"}, 0, "unsupported response encoding"),
        ):
            with self.subTest(headers=headers):
                stream = CountingStream(100)

                def respond(request):
                    self.assertEqual(request.headers["Accept-Encoding"], "identity")
                    return httpx.Response(200, headers=headers, stream=stream)

                self.bad(self.read(respond), error)
                self.assertEqual(stream.pulled, pulls)
                self.assertTrue(stream.closed)

        def buffered(length):
            start = b'{"items":[]}'
            return start + b" " * (length - len(start))

        self.good(self.read(lambda req: httpx.Response(200, content=buffered(limit))))
        self.bad(self.read(lambda req: httpx.Response(200, content=buffered(limit + 1))), "size limit")

    def test_elapsed_deadline_stops_a_slow_response(self):
        elapsed = [0]

        def advance():
            elapsed[0] += 11

        stream = CountingStream(20, on_chunk=advance)
        with patch.object(calendar.time, "monotonic", side_effect=lambda: elapsed[0]):
            self.bad(self.read(lambda req: httpx.Response(200, stream=stream)), "did not complete")
        self.assertEqual(stream.pulled, 3)
        self.assertTrue(stream.closed)

    def test_expired_auth_budget_never_sends_an_http_request(self):
        elapsed, calls = [0], []

        def access_token():
            elapsed[0] += 31
            return "fake-token"

        tool = calendar.CalendarTools(transport=httpx.MockTransport(lambda req: calls.append(req)))
        with patch.object(calendar.time, "monotonic", side_effect=lambda: elapsed[0]):
            with patch.object(google_auth, "access_token", side_effect=access_token):
                self.bad(tool.calendar_list("2026-11-25", "2026-11-26"), "did not complete")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
