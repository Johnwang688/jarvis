"""Read-only Calendar polling with paged sync and a live events snapshot."""

from datetime import date, datetime, time as midnight, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Doc, RateLimited
from .. import signals


_API = "https://www.googleapis.com/calendar/v3/calendars"
_MAX_BODY = 2 * 1024 * 1024
_RATES = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                   r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?")
_CALENDAR_ID = re.compile(r"[a-zA-Z0-9_+.#@-]{1,1024}")
_DEFAULT_ZONE = "America/Chicago"
_MAX_PAGES = 1000


class SyncTokenExpired(Exception):
    pass


class CalendarError(Exception):
    pass


def _obj(value):
    return value if isinstance(value, dict) else {}


def _stamp(instant):
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _instant(value, zone):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    if _DATE.fullmatch(value):
        return datetime.combine(date.fromisoformat(value), midnight.min, ZoneInfo(zone))
    if not _TIME.fullmatch(value):
        raise ValueError
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        local = instant.replace(tzinfo=ZoneInfo(zone))
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            raise ValueError
        return local
    return instant


def _event(raw, event_id, calendar, zone):
    start, end = _obj(raw.get("start")), _obj(raw.get("end"))
    all_day = "date" in start and "dateTime" not in start
    field, other = ("date", "dateTime") if all_day else ("dateTime", "date")
    first, last = start.get(field), end.get(field)
    try:
        if (other in start or other in end
                or any(not isinstance(value, str) or bool(_DATE.fullmatch(value)) != all_day
                       for value in (first, last))):
            return None
        start_at = _instant(first, zone if all_day else start.get("timeZone", zone))
        end_at = _instant(last, zone if all_day else end.get("timeZone", zone))
        if end_at.timestamp() < start_at.timestamp():
            return None
    except (ValueError, ZoneInfoNotFoundError, TypeError, OverflowError):
        return None
    if not all_day:
        first, last = _stamp(start_at), _stamp(end_at)

    people = raw.get("attendees")
    count = sum(isinstance(person, dict) and not person.get("self") and not person.get("resource")
                for person in people) if isinstance(people, list) else 0
    private = _obj(_obj(raw.get("extendedProperties")).get("private"))
    item = signals.scrub({
        "id": event_id, "title": _string(raw.get("summary")), "start": first, "end": last,
        "all_day": int(all_day), "attendees": max(count, int(raw.get("attendeesOmitted") is True)),
        "owner_event": int(_obj(raw.get("organizer")).get("self") is True),
        "jarvis": int(private.get("jarvis") == "1"), "status": _string(raw.get("status")),
        "recurring": int(bool(raw.get("recurrence") or raw.get("recurringEventId"))),
    })
    body = signals.scrub("\n".join((f"Start: {first}", f"End: {last}",
                                   f"Location: {_string(raw.get('location'))}",
                                   _string(raw.get("description")))))
    meta = {key: value for key, value in item.items() if key not in {"id", "title"}}
    meta.update(untrusted=1, calendar=calendar)
    ts = start_at.timestamp()
    return item, Doc(event_id, item["title"], ts, [("event", body, ts)], meta)


def _string(value):
    return value if isinstance(value, str) else ""


def _deleted(ref):
    return Doc(ref, "", 0, (), {"untrusted": 1}, deleted=True)


def _opaque(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and value.isascii() and value.isprintable() and signals.scrub(value) == value)


class CalendarIngester:
    name = "calendar"

    def __init__(self, api, *, signals_dir=None, calendar="primary", backfill_days=30, clock=time.time):
        self._api = api
        self._signals_dir = signals_dir
        self._calendar = calendar
        self._backfill_days = backfill_days
        self._clock = clock
        self._expired_checkpoint = None

    def poll(self, cursor):
        return self.poll_indexed(cursor, None)

    def poll_indexed(self, cursor, index):
        try:
            return self._poll(cursor, index)
        except (CalendarError, RateLimited):
            raise
        except Exception:
            raise CalendarError("Calendar poll failed.") from None

    def _poll(self, cursor, index):
        if not isinstance(self._calendar, str) or signals.scrub(self._calendar) != self._calendar:
            raise CalendarError("Invalid Calendar identifier.")
        items = self._items(cursor)
        cutoff = datetime.fromtimestamp(self._clock() - self._backfill_days * 86400, timezone.utc)
        if items is None or not cursor or cursor.get("calendar") != self._calendar:
            items = items or {}
            cursor = self._full(items, cutoff, index)
        cursor_tokens = cursor.get("sync_token"), cursor.get("page")
        if cursor_tokens == self._expired_checkpoint:
            cursor = self._full(items, cutoff, index)
        try:
            response = self._page(cursor)
        except SyncTokenExpired:
            # A continuation can expire during either kind of sync. Rebuild
            # the deletion candidates, including events seen before expiry.
            if not (cursor.get("sync_token") or cursor.get("page")):
                raise CalendarError("Calendar full sync expired.") from None
            self._expired_checkpoint = cursor_tokens
            cursor = self._full(items, cutoff, index)
            try:
                response = self._page(cursor)
            except SyncTokenExpired:
                raise CalendarError("Calendar full sync expired.") from None

        page = response.get("nextPageToken")
        checkpoint = response.get("nextSyncToken")
        seen = cursor.get("pages", [])
        if page is not None:
            if not _opaque(page) or page in seen or len(seen) >= _MAX_PAGES:
                raise CalendarError("Invalid or repeated Calendar page token.")
        elif not _opaque(checkpoint):
            raise CalendarError("Missing Calendar sync token.")

        zone = response.get("timeZone", cursor.get("zone", _DEFAULT_ZONE))
        ZoneInfo(zone)
        calendar = signals.scrub(self._calendar)
        changes = {}
        remaining = set(cursor.get("remaining", ()))
        for raw in response.get("items", []):
            raw = _obj(raw)
            event_id = signals.scrub(_string(raw.get("id")))
            if not 0 < len(event_id) <= 1024:
                continue
            remaining.discard(event_id)
            if raw.get("status") == "cancelled":
                changes[event_id] = _deleted(event_id)
                items.pop(event_id, None)
            else:
                event = _event(raw, event_id, calendar, zone)
                if event is not None:
                    items[event_id], changes[event_id] = event

        if page is not None:
            new_cursor = {**cursor, "page": page, "pages": [*seen, page], "zone": zone}
            if "remaining" in cursor:
                new_cursor["remaining"] = sorted(remaining)
        else:
            for event_id in remaining:
                changes[event_id] = _deleted(event_id)
                items.pop(event_id, None)
            new_cursor = {"calendar": self._calendar, "sync_token": checkpoint, "zone": zone}
        active = [item for item in items.values() if _instant(item["end"], zone) >= cutoff]
        signals.write(self.name, active, directory=self._signals_dir)
        return list(changes.values()), new_cursor

    def _items(self, cursor):
        snapshot = signals.read(self.name, directory=self._signals_dir)
        if snapshot is None:
            return None
        zone = (cursor or {}).get("zone", _DEFAULT_ZONE)
        try:
            for item in snapshot["items"]:
                _instant(item.get("end"), zone)
        except (ValueError, ZoneInfoNotFoundError, TypeError, OverflowError):
            return None
        return {item["id"]: item for item in snapshot["items"]}

    def _full(self, items, cutoff, index):
        # The snapshot is published before the Runner commits. The index can
        # still know about a deletion from a page whose transaction failed.
        remaining = set(items)
        if index is not None:
            after = None
            while rows := index.ingest_refs(self.name, after=after, limit=256):
                remaining.update(row["ref"] for row in rows)
                after = (rows[-1]["ts"], rows[-1]["id"])
        return {"calendar": self._calendar, "time_min": _stamp(cutoff), "remaining": sorted(remaining)}

    def _page(self, cursor):
        try:
            response = self._api.list_events(self._calendar, sync_token=cursor.get("sync_token"),
                                             page_token=cursor.get("page"), time_min=cursor.get("time_min"))
        except SyncTokenExpired:
            raise SyncTokenExpired() from None
        except RateLimited as exc:
            retry = exc.retry_after
            raise RateLimited(retry if type(retry) in (int, float) and math.isfinite(retry) else None) from None
        except Exception:
            raise CalendarError("Calendar request failed.") from None
        if not isinstance(response, dict) or not isinstance(response.get("items", []), list):
            raise CalendarError("Invalid Calendar response.")
        return response


def _retry_after(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _read_body(response, deadline):
    size = response.headers.get("content-length")
    if (response.headers.get("content-encoding", "identity").lower() != "identity"
            or size is not None and (not re.fullmatch(r"[0-9]{1,16}", size) or int(size) > _MAX_BODY)):
        raise CalendarError("Invalid Calendar response size or encoding.")
    body = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() >= deadline or len(chunk) > _MAX_BODY - len(body):
            raise CalendarError("Calendar response exceeded its limits.")
        body.extend(chunk)
    if time.monotonic() >= deadline:
        raise CalendarError("Calendar response exceeded its deadline.")
    return json.loads(body)


class HttpCalendar:
    """GET-only Calendar API using an injected access-token callable."""

    def __init__(self, token, *, transport=None, timeout=30):
        import httpx

        self._token = token
        self._timeout = timeout
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    def close(self):
        self._client.close()

    def list_events(self, calendar, *, sync_token=None, page_token=None, time_min=None):
        if not isinstance(calendar, str) or not _CALENDAR_ID.fullmatch(calendar) or calendar in (".", ".."):
            raise CalendarError("Invalid Calendar identifier.")
        params = {"maxResults": 250, "showDeleted": "true"}
        for key, value in (("syncToken", sync_token), ("pageToken", page_token), ("timeMin", time_min)):
            if value is not None:
                params[key] = value
        url = f"{_API}/{quote(calendar, safe='')}/events"
        try:
            body = self._get(url, params, refresh=True)
            if not isinstance(body, dict):
                raise CalendarError("Invalid Calendar response.")
            return body
        except (CalendarError, RateLimited, SyncTokenExpired):
            raise
        except Exception:
            raise CalendarError("Calendar request failed.") from None

    def _get(self, url, params, *, refresh):
        deadline = time.monotonic() + self._timeout
        try:
            token = self._token()
        except Exception:
            raise CalendarError("Calendar authentication failed.") from None
        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
        with self._client.stream("GET", url, params=params, headers=headers) as response:
            status = response.status_code
            if status == 410:
                raise SyncTokenExpired()
            if status == 429:
                raise RateLimited(_retry_after(response.headers.get("Retry-After")))
            if status in (200, 403):
                body = _read_body(response, deadline)
                if status == 200:
                    if isinstance(body, dict):
                        for key in ("nextPageToken", "nextSyncToken", "timeZone"):
                            value = body.get(key)
                            if token and isinstance(value, str) and token in value:
                                raise CalendarError("Invalid Calendar response metadata.")
                        if "items" in body:
                            body["items"] = signals.scrub(body["items"], secret=token)
                    return body
                error = _obj(_obj(body).get("error"))
                for group in (error.get("errors"), error.get("details")):
                    if isinstance(group, list) and any(_obj(row).get("reason") in _RATES for row in group
                                                       if isinstance(_obj(row).get("reason"), str)):
                        raise RateLimited(_retry_after(response.headers.get("Retry-After")))
        if status == 401 and refresh:
            from jarvis.google_auth import invalidate

            invalidate()
            return self._get(url, params, refresh=False)
        raise CalendarError(f"Calendar returned HTTP {status}.")
