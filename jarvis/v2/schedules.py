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
            self.daemon.require(self.daemon.stores.projects, record["project_id"])
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
            return record

    def delete(self, schedule_id):
        with self.lock:
            self.get(schedule_id)
            self._path(schedule_id).unlink()

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
