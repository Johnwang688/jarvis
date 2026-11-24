"""Google Calendar tools for v3. Availability uses events.list on primary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from jarvis import google_auth, untrusted

_API = "https://www.googleapis.com/calendar/v3/calendars"
_PRIMARY = f"{_API}/primary/events"
_CHICAGO = ZoneInfo("America/Chicago")
_UTC = timezone.utc
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?"
)
_CALENDAR_ID = re.compile(r"(?:[a-zA-Z0-9_.-]+|[a-zA-Z0-9_+.#-]+@[a-zA-Z0-9.-]+)")
_EVENT_ID = re.compile(r"[a-zA-Z0-9_-]{1,1024}")
_ETAG = re.compile(r'"[\x21\x23-\x7e]{1,256}"')
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_MAX_PAGES = 10
_PAGE_SIZE = 100
_MAX_OUTPUT = 24_000
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REQUEST_SECONDS = 30
_READ_SECONDS = 10
_MAX_WINDOW = timedelta(days=366)
_MAX_EVENT = timedelta(days=7)


class _CalendarError(Exception):
    """An error message safe to return to the caller."""


def _text_result(method):
    @wraps(method)
    def run(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except _CalendarError as exc:
            return f"Error: {exc}"
        except OverflowError:
            return "Error: Calendar time is outside the supported date range."
    return run


def _instant(value, *, zone=_CHICAGO, allow_date=False):
    """Parse an ISO time to UTC so comparisons and subtraction use instants."""
    try:
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError
        if not (_DATE_TIME.fullmatch(value) or (allow_date and _DAY.fullmatch(value))):
            raise ValueError
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        local = datetime.fromisoformat(value)
        if local.tzinfo is not None:
            return local.astimezone(_UTC)
        if not isinstance(zone, ZoneInfo):
            zone = _zone(zone)
        early = local.replace(tzinfo=zone, fold=0).astimezone(_UTC)
        late = local.replace(tzinfo=zone, fold=1).astimezone(_UTC)
        if early != late and not _DAY.fullmatch(value):     # a day that starts in a clock change starts at its first instant
            raise _CalendarError("Local time is ambiguous or does not exist. Supply an explicit UTC offset.")
        return early
    except (ValueError, OverflowError):
        raise _CalendarError("Supply a valid ISO 8601 date or time with a valid offset.") from None


def _window(start, end):
    first = _instant(start, allow_date=True)
    last = _instant(end, allow_date=True)
    if not timedelta(0) < last - first <= _MAX_WINDOW:
        raise _CalendarError("Choose an increasing calendar range of at most 366 days.")
    return first, last


def _zone(name):
    if name is None:
        return _CHICAGO
    if isinstance(name, str) and 0 < len(name) <= 128:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, UnicodeError):
            pass
    raise _CalendarError("Google Calendar returned an invalid time zone.")


def _event_times(event, zone):
    first, last = event.get("start"), event.get("end")
    if isinstance(first, dict) and isinstance(last, dict):
        if "dateTime" not in first and "dateTime" not in last:
            dates = first.get("date"), last.get("date")
            if all(isinstance(day, str) and _DAY.fullmatch(day) for day in dates):
                return (
                    _instant(dates[0], zone=zone, allow_date=True),
                    _instant(dates[1], zone=zone, allow_date=True),
                    dates,
                )
        elif "date" not in first and "date" not in last:
            return (
                _instant(first.get("dateTime"), zone=first.get("timeZone", zone)),
                _instant(last.get("dateTime"), zone=last.get("timeZone", zone)),
                None,
            )
    raise _CalendarError("Google Calendar returned invalid event times.")


def _local_iso(instant):
    return instant.astimezone(_CHICAGO).isoformat()


def _display(instant):
    local = instant.astimezone(_CHICAGO)
    precision = "microseconds" if local.microsecond else "seconds" if local.second else "minutes"
    return f"{local.date()} {local.time().isoformat(timespec=precision)} {local.tzname()}"


def _wire_time(instant):
    return {"dateTime": _local_iso(instant), "timeZone": _CHICAGO.key}


def _validate_text(value, name, limit, *, multiline=False):
    if (
        not isinstance(value, str) or len(value) > limit
        or (not multiline and not value.strip())
        or any(untrusted.unseen(c) and not (multiline and c in "\n\t") for c in value)
        or untrusted.strip_invisible(value) != value
    ):
        raise _CalendarError(f"Supply a valid event {name} of at most {limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _CalendarError(f"Supply a valid UTF-8 event {name}.") from None
    return value


def _owned_etag(event, event_id):
    props = event.get("extendedProperties")
    private = props.get("private") if isinstance(props, dict) else None
    if not isinstance(private, dict) or private.get("jarvis") != "1":
        return None
    if not all(isinstance(event.get(key), dict) and event[key].get("self") is True
               for key in ("organizer", "creator")):
        return None
    if (
        event.get("id") != event_id
        or event.get("status", "confirmed") not in ("confirmed", "tentative")
        or event.get("attendees", []) != []
        or event.get("attendeesOmitted", False) is not False
        or event.get("locked", False) is not False
        or event.get("recurrence", []) != []
        or "recurringEventId" in event
        or "originalStartTime" in event
        or event.get("eventType", "default") != "default"
    ):
        return None
    etag = event.get("etag")
    return etag if isinstance(etag, str) and _ETAG.fullmatch(etag) else None


def _blocks_time(event):
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return False
    attendees = event.get("attendees", [])
    if not isinstance(attendees, list) or any(not isinstance(person, dict) for person in attendees):
        raise _CalendarError("Google Calendar returned invalid attendee data.")
    return not any(person.get("self") is True and person.get("responseStatus") == "declined"
                   for person in attendees)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise httpx.TimeoutException("Calendar request exceeded its deadline")
    return seconds


def _read_response(response, deadline):
    if response.status_code == 204:
        return {}
    # Request identity encoding and refuse compressed bodies before decoding.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise _CalendarError("Google Calendar returned an unsupported response encoding.")
    size = response.headers.get("content-length")
    if size is not None and (not re.fullmatch(r"[0-9]{1,16}", size) or int(size) > _MAX_RESPONSE_BYTES):
        raise _CalendarError("Google Calendar response exceeded its size limit or had an invalid length.")
    raw = bytearray()
    for chunk in response.iter_bytes():
        _remaining(deadline)
        if len(chunk) > _MAX_RESPONSE_BYTES - len(raw):
            raise _CalendarError("Google Calendar response exceeded its size limit.")
        raw.extend(chunk)
    _remaining(deadline)
    body = json.loads(raw)
    _remaining(deadline)
    return body


def _event_id(value):
    if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
        raise _CalendarError("Supply a valid Calendar event ID.")
    return value


def _readable(value):
    # JSON can contain escaped lone surrogates, which result strings cannot
    # carry to a UTF-8 client.
    return value.encode("utf-8", errors="replace").decode("utf-8") if isinstance(value, str) else ""


class CalendarTools:
    """Read Calendar and ask for approval before each write to primary."""

    def __init__(self, *, request=None, approve=None, propose=None, now=time.time, transport=None):
        self._request = request
        self._approve = approve
        self._propose = propose
        self._now = now
        self._transport = transport

    def _default_request(self, method, url, *, params, json, _if_match=None):
        deadline = time.monotonic() + _REQUEST_SECONDS
        headers = {"Accept-Encoding": "identity"}
        if _if_match is not None:
            headers["If-Match"] = _if_match
        with httpx.Client(transport=self._transport, follow_redirects=False, trust_env=False) as client:
            for attempt in range(2):
                _remaining(deadline)
                headers["Authorization"] = f"Bearer {google_auth.access_token()}"
                with client.stream(
                    method, url, params=params, json=json, headers=headers, timeout=min(_READ_SECONDS, _remaining(deadline))
                ) as response:
                    status = response.status_code
                    _remaining(deadline)
                    if status != 401 or attempt:
                        return status, _read_response(response, deadline) if 200 <= status < 300 else {}
                google_auth.invalidate()

    def _call(self, method, url, *, params=None, body=None, etag=None):
        write = method != "GET"
        uncertain = " The Calendar write may have taken effect." if write else ""
        try:
            if self._request is None:
                status, event = self._default_request(method, url, params=params, json=body, _if_match=etag)
            else:
                status, event = self._request(method, url, params=params, json=body)
        except google_auth.AuthError:
            raise _CalendarError("Google authorization failed. The owner can run `jarvis auth google`.") from None
        except _CalendarError as exc:
            raise _CalendarError(str(exc) + uncertain) from None
        except Exception:
            raise _CalendarError("Google Calendar request did not complete." + uncertain) from None
        if type(status) is not int or not 100 <= status <= 599:
            raise _CalendarError("Google Calendar returned an invalid status." + uncertain)
        if status in (401, 403):
            raise _CalendarError(f"Google Calendar access was refused (HTTP {status}). The owner can run `jarvis auth google`.")
        if status in (409, 412) and write:
            raise _CalendarError("Calendar event changed. Review the current event before trying again.")
        if not 200 <= status < 300:
            raise _CalendarError(f"Google Calendar returned HTTP {status}." + (uncertain if status >= 500 else ""))
        if status == 204 and method == "DELETE":
            return {}
        if status != 200 and not (status == 201 and method == "POST"):
            raise _CalendarError("Google Calendar returned an unexpected success status." + uncertain)
        if not isinstance(event, dict):
            raise _CalendarError("Google Calendar returned an invalid response." + uncertain)
        return event

    def _pages(self, url, first, last):
        # Events.list ignores fractional seconds. Round the query outwards.
        lower = first.replace(microsecond=0)
        upper = last.replace(microsecond=0) + timedelta(seconds=bool(last.microsecond))
        params = {
            "timeMin": _local_iso(lower), "timeMax": _local_iso(upper),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": str(_PAGE_SIZE),
        }
        events = []
        seen = set()
        for _ in range(_MAX_PAGES):
            page = self._call("GET", url, params=params)
            items = page.get("items", [])
            if not isinstance(items, list) or len(items) > _PAGE_SIZE:
                raise _CalendarError("Google Calendar returned an invalid event list.")
            zone = _zone(page.get("timeZone"))
            events.extend((item, zone) for item in items)
            token = page.get("nextPageToken")
            if token is None or token == "":
                return events, True
            if not isinstance(token, str) or len(token) > 4096 or token in seen:
                raise _CalendarError("Google Calendar returned invalid or repeating pagination.")
            seen.add(token)
            params = {**params, "pageToken": token}
        return events, False

    @_text_result
    def calendar_list(self, start, end, calendar="primary"):
        """List timed and all-day events in an ISO 8601 date or time range."""
        first, last = _window(start, end)
        if (
            not isinstance(calendar, str) or len(calendar) > 254
            or not _CALENDAR_ID.fullmatch(calendar) or calendar in (".", "..")
        ):
            raise _CalendarError("Supply a valid Google Calendar ID.")
        url = f"{_API}/{quote(calendar, safe='')}/events"
        events, complete = self._pages(url, first, last)
        lines = []
        omitted = False
        for event, zone in events:
            if isinstance(event, dict) and event.get("status") == "cancelled":
                continue
            try:
                if not isinstance(event, dict):
                    raise _CalendarError("Invalid event.")
                event_id = _event_id(event.get("id"))
                begin, finish, dates = _event_times(event, zone)
                if finish < begin:
                    raise _CalendarError("Invalid event period.")
            except _CalendarError:
                omitted = True
                continue
            if finish <= first or begin >= last:
                continue
            period = f"{_display(begin)} to {_display(finish)}"
            if dates is not None:
                period = f"{dates[0]} to {dates[1]} (all day, end exclusive, {zone.key})"
            heading = untrusted.one_line(_readable(event.get("summary")), cap=300)
            lines.append(f"{event_id} | {period} | {heading or '(untitled)'}")
            desc = _readable(event.get("description"))
            if desc:
                lines.append(untrusted.strip_invisible(desc)[:1000])
        notes = []
        if not complete:
            notes.append("Calendar page limit reached. Use a shorter range to see all events.")
        if omitted:
            notes.append("Some entries lacked valid event data.")
        if not lines:
            return "\n".join(["No events to display.", *notes])
        return untrusted.fence("\n".join(lines), url, max_chars=_MAX_OUTPUT, notes=notes)

    @_text_result
    def calendar_free(self, start, end, min_minutes=30):
        """Report free intervals on primary in America/Chicago time."""
        first, last = _window(start, end)
        if type(min_minutes) is not int or not 1 <= min_minutes <= _MAX_EVENT // timedelta(minutes=1):
            raise _CalendarError("Minimum free time must be a whole number of minutes from 1 to 10080.")
        events, complete = self._pages(_PRIMARY, first, last)
        if not complete:
            raise _CalendarError("Calendar page limit reached. Use a shorter range to check availability.")
        busy = []
        for event, zone in events:
            if not isinstance(event, dict):
                raise _CalendarError("Google Calendar returned an invalid event.")
            if _blocks_time(event):
                begin, finish, _ = _event_times(event, zone)
                if finish < begin:
                    raise _CalendarError("Google Calendar returned an event ending before it starts.")
                begin, finish = max(first, begin), min(last, finish)
                if begin < finish:
                    busy.append((begin, finish))
        gap = timedelta(minutes=min_minutes)
        cursor = first
        lines = []
        for begin, finish in sorted(busy):
            if begin - cursor >= gap:
                lines.append(f"{_display(cursor)} to {_display(begin)}")
            cursor = max(cursor, finish)
        if last - cursor >= gap:
            lines.append(f"{_display(cursor)} to {_display(last)}")
        if not lines:
            return "No free intervals meet the requested duration."
        output = "Free intervals (America/Chicago)\n" + "\n".join(lines)
        if len(output) > _MAX_OUTPUT:
            raise _CalendarError("Too many free intervals to display. Use a shorter range.")
        return output

    def _future(self, instant):
        try:
            now = datetime.fromtimestamp(self._now(), _UTC)
        except Exception:
            raise _CalendarError("The current time is unavailable.") from None
        if instant < now:
            raise _CalendarError("The event time must be in the future.")

    @staticmethod
    def _duration(first, last):
        if not timedelta(0) < last - first <= _MAX_EVENT:
            raise _CalendarError("The event must end after it starts and last at most seven days.")

    def _new_event(self, title, start, end, description):
        title = _validate_text(title, "title", 300)
        description = _validate_text(description, "description", 8000, multiline=True)
        first, last = _instant(start), _instant(end)
        self._duration(first, last)
        self._future(first)
        return {"summary": title, "start": _wire_time(first), "end": _wire_time(last), "description": description}

    def _authorize(self, action, method, url, params, body, *, before=None, etag=None):
        details = {"method": method, "url": url, "params": params, "body": body, "before": before}
        if etag is not None and self._request is None:
            details["headers"] = {"If-Match": etag}
        try:
            answer = self._approve(action, deepcopy(details)) if self._approve is not None else False
        except Exception:
            answer = False
        if answer is not True:
            raise _CalendarError("Calendar write requires approval.")

    @_text_result
    def calendar_create(self, title, start, end, description="", reminders=()):
        """Create a tagged, timed event on primary with no attendees."""
        body = self._new_event(title, start, end, description)
        if (
            not isinstance(reminders, (tuple, list)) or len(reminders) > 5
            or any(type(minutes) is not int or not 0 <= minutes <= 40320 for minutes in reminders)
        ):
            raise _CalendarError("Supply up to five popup reminders, each 0 to 40320 minutes before the event.")
        if reminders:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": minutes} for minutes in reminders],
            }
        body["extendedProperties"] = {"private": {"jarvis": "1"}}
        params = {"sendUpdates": "none"}
        self._authorize("calendar.create", "POST", _PRIMARY, params, body)
        result = self._call("POST", _PRIMARY, params=params, body=body)
        try:
            event_id = _event_id(result.get("id"))
        except _CalendarError:
            raise _CalendarError("Google Calendar returned no valid event ID. The event may have been created.") from None
        return f"Created event {event_id}."

    def _change(self, action, method, event_id, body, *, start=None, end=None):
        url = f"{_PRIMARY}/{_event_id(event_id)}"
        before = self._call("GET", url)
        etag = _owned_etag(before, event_id)
        if etag is None:
            raise _CalendarError("Only eligible Jarvis-owned events with no attendees can be updated or deleted.")
        if start is not None or end is not None:
            if start is None or end is None:
                first, last, dates = _event_times(before, _CHICAGO)
                if dates is not None:
                    raise _CalendarError("Supply both timed boundaries when changing an all-day event to a timed event.")
            if start is not None:
                first = _instant(start)
                self._future(first)
                body["start"] = _wire_time(first)
            if end is not None:
                last = _instant(end)
                self._future(last)
                body["end"] = _wire_time(last)
            self._duration(first, last)
        params = {"sendUpdates": "none"}
        self._authorize(action, method, url, params, body, before=before, etag=etag)
        current = self._call("GET", url)
        if _owned_etag(current, event_id) != etag:
            raise _CalendarError("Calendar event changed after approval. Review it before trying again.")
        result = self._call(method, url, params=params, body=body, etag=etag)
        if method == "PATCH" and result.get("id") != event_id:
            raise _CalendarError("Google Calendar did not confirm the change. The event may have been updated.")

    @_text_result
    def calendar_update(self, event_id, *, title=None, start=None, end=None, description=None):
        """Patch the supplied fields of a Jarvis-owned event on primary."""
        body = {}
        if title is not None:
            body["summary"] = _validate_text(title, "title", 300)
        if description is not None:
            body["description"] = _validate_text(description, "description", 8000, multiline=True)
        if not body and start is None and end is None:
            raise _CalendarError("Supply at least one field to update.")
        self._change("calendar.update", "PATCH", event_id, body, start=start, end=end)
        return f"Updated event {event_id}."

    @_text_result
    def calendar_delete(self, event_id):
        """Delete a Jarvis-owned event on primary."""
        self._change("calendar.delete", "DELETE", event_id, None)
        return f"Deleted event {event_id}."

    @_text_result
    def calendar_invite_propose(self, title, start, end, attendees, description=""):
        """Send a meeting proposal to the proposal service."""
        if (
            not isinstance(attendees, (tuple, list)) or not 1 <= len(attendees) <= 50
            or any(not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email)
                   for email in attendees)
        ):
            raise _CalendarError("Supply between 1 and 50 valid attendee email addresses.")
        body = self._new_event(title, start, end, description)
        if self._propose is None:
            raise _CalendarError("Calendar proposals are unavailable.")
        try:
            result = self._propose({"kind": "calendar.invite", **body, "attendees": list(attendees)})
        except Exception:
            raise _CalendarError("Calendar proposal could not be submitted.") from None
        if not isinstance(result, str) or not result.strip():
            raise _CalendarError("Calendar proposal returned no confirmation.")
        return result
