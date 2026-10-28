"""Quiet hours: the owner's night, one desk setting (phone surface design §7,
§11 question 5; the owner's answer, 2026-10-28).

**The setting.** `{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`, local
time on this machine. On by default, 23:00 to 07:00 (`DEFAULT`). A window may
cross midnight (the default does); `start == end` is refused, never read as
"all day" or "never". Changed only by the owner at the desk (`GET`/`POST
/quiet` on the HUD listener, `projects.owner_only`, in `daemon._route`).

**What reads it.** Web Push (P-6, `push.py`): nothing is pushed while quiet,
rank 1 included, and when quiet hours end one push says what still waits.
The next package (approvals asked at night) reuses this module; nothing here
knows about either, and this module imports no HTTP, push or daemon code.

**The file.** `<data root>/quiet.json` (`config.V2_DATA_DIR`): the data root
opened as a directory, the file read `O_NOFOLLOW | O_NONBLOCK` and refused
unless regular and small, written as a fresh `O_EXCL` 0600 temp file,
fsynced, `os.replace`d, and the folder fsynced. It is protected state
(`jarvis/tools/files._protected_state_paths`): no agent write tool, shell
command or grant changes it, so no agent can switch quiet hours off (or on)
to change what reaches the owner. A file that is missing, a link, not
regular, too large or not exactly the shape above reads as `DEFAULT` (and a
missing one is not written until the owner saves).

**Clocks.** `clock()` returns the local time as an aware `datetime`
(`datetime.now().astimezone()`); tests inject their own. Every test is on
the wall clock's local time of day, read from the `now` given. Across a
daylight-saving change the window is the wall clock's: a night with a
skipped hour is an hour shorter.

Tests: tests/v2/quiet_check.py.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
import errno
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading

LOG = logging.getLogger(__name__)

NAME = "quiet.json"
DEFAULT = {"enabled": True, "start": "23:00", "end": "07:00"}
FIELDS = frozenset(DEFAULT)
_FILE_MAX = 4096
_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_READ = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
          | getattr(os, "O_CLOEXEC", 0))
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_hhmm(value) -> dtime | None:
    """"HH:MM" (00:00 to 23:59, two digits each) as a time, else None."""
    if not isinstance(value, str):
        return None
    match = _HHMM.fullmatch(value)
    if match is None:
        return None
    return dtime(int(match.group(1)), int(match.group(2)))


def valid(value) -> dict | None:
    """The setting if `value` is exactly its shape, else None."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        return None
    if not isinstance(value["enabled"], bool):
        return None
    start, end = parse_hhmm(value["start"]), parse_hhmm(value["end"])
    if start is None or end is None or start == end:
        return None
    return {"enabled": value["enabled"], "start": value["start"], "end": value["end"]}


def within(start: dtime, end: dtime, at: dtime) -> bool:
    """Whether the time of day `at` is in [start, end), across midnight when
    `end` is earlier than `start`."""
    if start < end:
        return start <= at < end
    return at >= start or at < end


def next_end(start: dtime, end: dtime, now: datetime) -> datetime | None:
    """When the window holding `now` ends, or None when `now` is outside it.
    In `now`'s own time zone; seconds and below are zero."""
    at = now.time().replace(tzinfo=None)
    if not within(start, end, at):
        return None
    day = now.date() if at < end else now.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=now.tzinfo)


class QuietHours:
    """The quiet-hours setting under one data root. Read once, then kept in
    memory: only `update` (the owner's route) changes the file."""

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.path = self.root / NAME
        self.clock = clock or local_now
        self._lock = threading.RLock()
        self._cached: dict | None = None

    # -- the file -----------------------------------------------------------------

    def _open_root(self, create: bool) -> int | None:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(self.root, _DIR)
        except FileNotFoundError:
            if create:
                raise
            return None

    def _read(self) -> dict:
        try:
            root_fd = self._open_root(create=False)
        except OSError as exc:
            LOG.warning("Cannot open the data folder for quiet hours (%s); the default applies",
                        type(exc).__name__)
            return dict(DEFAULT)
        if root_fd is None:
            return dict(DEFAULT)
        try:
            try:
                fd = os.open(NAME, _READ, dir_fd=root_fd)
            except FileNotFoundError:
                return dict(DEFAULT)
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.EMLINK):
                    LOG.warning("Cannot read quiet hours (%s); the default applies", type(exc).__name__)
                else:
                    LOG.warning("The quiet hours file is a link; the default applies")
                return dict(DEFAULT)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_MAX:
                    LOG.warning("The quiet hours file is not one of ours; the default applies")
                    return dict(DEFAULT)
                data = os.read(fd, _FILE_MAX + 1)
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)
        try:
            value = valid(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError, RecursionError):
            value = None
        if value is None:
            LOG.warning("The quiet hours file is malformed; the default applies")
            return dict(DEFAULT)
        return value

    def _write(self, value: dict) -> None:
        data = (json.dumps(value, sort_keys=True) + "\n").encode("ascii")
        root_fd = self._open_root(create=True)
        try:
            tmp = f".quiet.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, _WRITE, 0o600, dir_fd=root_fd)
            try:
                try:
                    os.fchmod(fd, 0o600)
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=root_fd)
                except OSError:
                    pass
                raise
            try:
                os.fsync(root_fd)
            except OSError:
                pass
        finally:
            os.close(root_fd)

    # -- the setting ----------------------------------------------------------------

    def settings(self) -> dict:
        """The setting in force: the file's, or `DEFAULT`."""
        with self._lock:
            if self._cached is None:
                self._cached = self._read()
            return dict(self._cached)

    def update(self, changes: dict) -> dict:
        """The owner's change (any of `enabled`, `start`, `end`), checked and
        saved; the new setting. ValueError, with a fixed sentence, for
        anything else; OSError when it cannot be written (nothing changes)."""
        if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
            raise ValueError("quiet hours take enabled, start and end only")
        with self._lock:
            value = dict(self.settings(), **changes)
            checked = valid(value)
            if checked is None:
                raise ValueError("quiet hours need enabled true or false and two different "
                                 "times written HH:MM")
            self._write(checked)
            self._cached = checked
            LOG.info("Quiet hours set by the owner: %s, %s to %s",
                     "on" if checked["enabled"] else "off", checked["start"], checked["end"])
            return dict(checked)

    def reload(self) -> None:
        with self._lock:
            self._cached = None

    # -- the questions ----------------------------------------------------------------

    def _window(self) -> tuple[dtime, dtime] | None:
        value = self.settings()
        if not value["enabled"]:
            return None
        return parse_hhmm(value["start"]), parse_hhmm(value["end"])

    def is_quiet(self, now: datetime | None = None) -> bool:
        """Whether `now` (default: the clock) is inside quiet hours. Never
        when they are off."""
        window = self._window()
        if window is None:
            return False
        now = now or self.clock()
        return within(window[0], window[1], now.time().replace(tzinfo=None))

    def ends_at(self, now: datetime | None = None) -> datetime | None:
        """While quiet, when this quiet period ends (the next `end`, in
        `now`'s time zone); None when not quiet or quiet hours are off."""
        window = self._window()
        if window is None:
            return None
        return next_end(window[0], window[1], now or self.clock())

    def view(self, now: datetime | None = None) -> dict:
        """What the desk shows: the setting, whether it is quiet now, and
        until when."""
        now = now or self.clock()
        ends = self.ends_at(now)
        return {**self.settings(), "quiet_now": ends is not None,
                "ends_at": ends.isoformat(timespec="minutes") if ends is not None else None}
