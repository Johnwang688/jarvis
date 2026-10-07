"""Durable schedules and a minute-tick Chicago cron scheduler (stdlib only)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
import secrets
import threading
import time
from zoneinfo import ZoneInfo

from .control import ControlError
from .model import TERMINAL_STATES
from .stores import _write_bytes

ZONE = ZoneInfo("America/Chicago")
LOG = logging.getLogger(__name__)


def _field(text, low, high):
    values = set()
    for item in text.split(","):
        step = 1
        if "/" in item:
            item, step_text = item.split("/")
            if not step_text.isdecimal() or int(step_text) < 1:
                raise ValueError("invalid cron step")
            step = int(step_text)
        if item == "*":
            start, end = low, high
        elif re.fullmatch(r"\d+-\d+", item):
            start, end = map(int, item.split("-"))
        elif item.isdecimal() and step == 1:
            start = end = int(item)
        else:
            raise ValueError("invalid cron field")
        if not low <= start <= end <= high:
            raise ValueError("cron field out of range")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expression):
        if not isinstance(expression, str) or len(expression.split()) != 5:
            raise ValueError("cron must have five fields")
        self.parts = expression.split()
        self.values = [_field(p, lo, hi) for p, (lo, hi) in zip(
            self.parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)))]
        self.values[4] = {n % 7 for n in self.values[4]}

    def matches(self, at):
        local = at.astimezone(ZONE)
        minute, hour, dom, month, dow = self.values
        d, w = local.day in dom, (local.weekday() + 1) % 7 in dow
        # Traditional cron: restricted day-of-month and weekday are ORed.
        day = (d and w) if self.parts[2].startswith("*") or self.parts[4].startswith("*") else (d or w)
        return local.minute in minute and local.hour in hour and local.month in month and day

    def next(self, after):
        """UTC epoch, strictly after `after`. Missing DST times skip; folds fire once."""
        first = datetime.fromtimestamp(after, ZONE).date()
        for offset in range(366 * 8):
            day = first + timedelta(days=offset)
            if day.month not in self.values[3]:
                continue
            for hour in sorted(self.values[1]):
                for minute in sorted(self.values[0]):
                    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)
                    stamp = local.timestamp()
                    # A spring-gap wall time round-trips to a different hour.
                    if (stamp > after and datetime.fromtimestamp(stamp, ZONE) == local
                            and self.matches(local)):
                        return stamp
        raise ValueError("cron has no occurrence in the next eight years")


ACCEPTED_WHEN = ("a five-field cron; every day at 9; weekdays at 8:30; "
                 "weekends at 10am; mondays at 10; every 15 minutes; every 2 hours")
_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
_PERIODS = {"every day": "*", "daily": "*", "weekdays": "1-5", "weekends": "0,6"}
_PERIODS.update({form: str(i) for i, day in enumerate(_DAYS)
                 for form in (day, day + "s", "every " + day)})
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
_WHEN_RULES = (
    (re.compile(r"every (?:(\d+) )?(second|minute|hour|day|week)s?"), "interval"),
    (re.compile(r"(.+) at (\d{1,2})(?::([0-9]{2}))?\s*(am|pm)?"), "clock"),
)


def parse_when(text: str) -> dict | None:
    """Return exactly one of {cron: str}/{every_s: int}, or refuse to guess."""
    if not isinstance(text, str) or len(text) > 512:
        return None
    text = " ".join(text.lower().split())
    try:
        cron = Cron(text)
        cron.next(time.time())
        return {"cron": text}
    except ValueError:
        pass
    for pattern, kind in _WHEN_RULES:
        match = pattern.fullmatch(text)
        if not match:
            continue
        if kind == "interval":
            count, unit = match.groups()
            seconds = int(count or 1) * _UNITS[unit]
            return {"every_s": seconds} if seconds > 0 else None
        period, hour, minute, meridiem = match.groups()
        hour, minute = int(hour), int(minute or 0)
        if period not in _PERIODS or minute > 59:
            return None
        if meridiem:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        elif hour > 23:
            return None
        return {"cron": f"{minute} {hour} * * {_PERIODS[period]}"}
    return None


def _timing(cron=None, every_s=None):
    if (cron is None) == (every_s is None):
        raise ValueError("provide exactly one of cron or every_s")
    if cron is not None:
        return Cron(cron)
    if type(every_s) is not int or every_s <= 0:
        raise ValueError("every_s must be a positive integer")
    return None


def describe(cron=None, every_s=None) -> str:
    """Read schedule timing in English, including arbitrary supported cron fields."""
    parsed = _timing(cron, every_s)
    if parsed is None:
        for unit, seconds in reversed(tuple(_UNITS.items())):
            if every_s % seconds == 0:
                count = every_s // seconds
                return f"every {count} {unit}{'s' if count != 1 else ''}"
    minute, hour, dom, month, dow = parsed.parts
    if dom == month == "*" and minute.isdecimal() and hour.isdecimal():
        days = parsed.values[4]
        period = ("every day" if days == set(range(7)) else
                  "weekdays" if days == set(range(1, 6)) else
                  "weekends" if days == {0, 6} else
                  ", ".join(_DAYS[d] + "s" for d in sorted(days)))
        return f"{period} at {int(hour):02d}:{int(minute):02d}"

    def reading(part, values, noun):
        if part == "*":
            return f"every {noun}"
        return f"{noun} " + ", ".join(str(n) for n in sorted(values))

    day_join = " or " if not dom.startswith("*") and not dow.startswith("*") else "; "
    weekdays = "every day of the week" if dow == "*" else "on " + ", ".join(_DAYS[d] for d in sorted(parsed.values[4]))
    return (f"{reading(minute, parsed.values[0], 'minute')}; "
            f"{reading(hour, parsed.values[1], 'hour')}; "
            f"{reading(month, parsed.values[3], 'month')}; "
            f"({reading(dom, parsed.values[2], 'day of month')}{day_join}{weekdays})")


def preview(*, cron=None, every_s=None, now=None):
    parsed = _timing(cron, every_s)
    stamp = time.time() if now is None else now
    upcoming = []
    for _ in range(3):
        stamp = parsed.next(stamp) if parsed else stamp + every_s
        upcoming.append(datetime.fromtimestamp(stamp, ZONE).isoformat())
    return {"next": upcoming, "describe": describe(cron, every_s)}


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _stamp(value):
    return datetime.fromisoformat(value).timestamp()


class Schedules:
    def __init__(self, daemon, *, clock=time.time):
        self.daemon = daemon
        self.root = daemon.stores.root / "schedules"
        self.clock = clock
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.worker = None

    def _path(self, schedule_id):
        from .daemon import APIError
        if not isinstance(schedule_id, str) or not re.fullmatch(r"[0-9a-f]{8}", schedule_id):
            raise APIError(400, "invalid schedule id")
        return self.root / f"{schedule_id}.json"

    def get(self, schedule_id):
        from .daemon import APIError
        try:
            return json.loads(self._path(schedule_id).read_text())
        except FileNotFoundError:
            raise APIError(404, "schedule not found") from None

    def list(self):
        with self.lock:
            return [self.get(p.stem) for p in sorted(self.root.glob("*.json"))]

    def _save(self, record):
        _write_bytes(self._path(record["id"]), json.dumps(record, allow_nan=False).encode())

    def _next(self, record, now):
        if not record["enabled"]:
            return None
        return _iso(Cron(record["cron"]).next(now) if record["cron"] else now + record["every_s"])

    def save(self, body, schedule_id=None):
        from .daemon import _object, _text
        _object(body, ("project_id", "brief", "cron", "every_s", "enabled"),
                () if schedule_id else ("project_id", "brief"))
        with self.lock:
            now = self.clock()
            record = self.get(schedule_id) if schedule_id else dict(
                id=secrets.token_hex(4), cron=None, every_s=None, enabled=True,
                last_run_at=None, last_task_id=None, next_run_at=None, created=_iso(now))
            record.update(body)
            from .projects import refuse_archived_project
            refuse_archived_project(self.daemon.require(self.daemon.stores.projects, record["project_id"]))
            _text(record["brief"], "brief")
            if not isinstance(record["enabled"], bool):
                raise ValueError("enabled must be boolean")
            if (record["cron"] is None) == (record["every_s"] is None):
                raise ValueError("provide exactly one of cron or every_s")
            if record["cron"] is not None:
                Cron(record["cron"]).next(now)  # Reject impossible expressions even when disabled.
            elif type(record["every_s"]) is not int or record["every_s"] <= 0:
                raise ValueError("every_s must be a positive integer")
            if not schedule_id or any(k in body for k in ("cron", "every_s", "enabled")):
                record["next_run_at"] = self._next(record, now)
            self._save(record)
            self._changed("schedule_updated" if schedule_id else "schedule_created", record)
            return record

    def delete(self, schedule_id):
        with self.lock:
            record = self.get(schedule_id)
            self._path(schedule_id).unlink()
            self._changed("schedule_deleted", record)

    def _changed(self, kind, record):
        self.daemon.bus.publish({"kind": kind, "project_id": record["project_id"],
                                 "data": {"schedule_id": record["id"]}})

    def fire(self, schedule_id, *, manual=False):
        from .daemon import APIError
        with self.lock:
            record = self.get(schedule_id)
            now = self.clock()
            if not record["enabled"]:
                if manual:
                    raise APIError(409, "schedule is disabled")
                return None
            if not manual and (not record["next_run_at"] or _stamp(record["next_run_at"]) > now):
                return None
            stores = self.daemon.stores
            from .projects import is_archived
            if is_archived(stores, record["project_id"]):
                # Paused with its project (decisions B1); belt and braces for a
                # record whose `enabled` was changed by hand.
                if manual:
                    raise APIError(409, "its project is archived")
                return None
            previous = stores.tasks.get(record["last_task_id"]) if record["last_task_id"] else None
            if previous and previous.state not in TERMINAL_STATES:
                stores.tasks.journal(previous.id, "schedule_skipped", schedule_id=schedule_id,
                                     reason="previous task is still active")
                record["next_run_at"] = self._next(record, now)
                self._save(record)
                return record
            if self.daemon.runner is None:
                raise APIError(409, "no task runner is attached to this daemon")
            self.daemon._active()
            self.daemon.require(stores.projects, record["project_id"])
            task = stores.tasks.create(record["project_id"], record["brief"])
            stores.tasks.save(task)
            stores.tasks.journal(task.id, "scheduled_by", schedule_id=schedule_id)
            record.update(last_run_at=_iso(now), last_task_id=task.id,
                          next_run_at=self._next(record, now))
            self._save(record)  # Persist ownership before admitting a concurrent task.
            self.daemon._lifecycle("task_created", task)
            try:
                self.daemon.runner.start(task.id)
            except ControlError as exc:
                stores.tasks.journal(task.id, "schedule_start_failed", reason=str(exc))
                raise APIError(409, str(exc)) from exc
            self.daemon.bus.publish({"kind": "schedule_fired", "project_id": task.project_id,
                                     "task_id": task.id,
                                     "data": {"schedule_id": schedule_id, "task_id": task.id}})
            return record

    def tick(self):
        for record in self.list():
            if self.stopped.is_set():
                break
            try:
                self.fire(record["id"])
            except Exception:
                LOG.exception("Schedule %s could not fire", record["id"])

    def start(self):
        def run():
            while not self.stopped.wait(60):
                try:
                    self.tick()
                except Exception:
                    LOG.exception("Schedule tick failed")
        self.worker = threading.Thread(target=run, name="jarvis-schedules", daemon=True)
        self.worker.start()

    def stop(self):
        self.stopped.set()
        if self.worker:
            self.worker.join(2)
