"""Periodic jobs for the v3 daemon."""

from collections.abc import Callable, Iterable
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
import fcntl
from functools import partial
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
from uuid import uuid4

from jarvis import config
from ._paths import open_directory


_STOP_GRACE_S = 0.25
_IDLE_SCAN_ENTRIES = 4096
_IDLE_SCAN_DEPTH = 32
_STATE_LIMIT = 1024 * 1024
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_GOOGLE_GRANT_LIMIT = 64 * 1024
_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
_CALENDAR_SCOPES = frozenset(f"https://www.googleapis.com/auth/{scope}" for scope in (
    "calendar", "calendar.readonly", "calendar.events", "calendar.events.readonly",
    "calendar.events.owned", "calendar.events.owned.readonly",
))
_CALENDAR_INGEST_SCOPES = frozenset(("https://www.googleapis.com/auth/calendar.events",
                                    "https://www.googleapis.com/auth/calendar.readonly"))


def _finite(value):
    return type(value) in (int, float) and -math.inf < value < math.inf


@dataclass(frozen=True)
class Job:
    name: str
    run: Callable[[], object]
    every_s: float | None = field(default=None, kw_only=True)
    due: Callable[[float, float | None], bool] | None = field(default=None, kw_only=True)
    needs_idle_s: float = field(default=0, kw_only=True)

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or not callable(self.run):
            raise ValueError("A job needs a name and a callable.")
        if (self.every_s is None) == (self.due is None):
            raise ValueError("A job needs exactly one schedule.")
        if self.every_s is not None and (not _finite(self.every_s) or self.every_s <= 0):
            raise ValueError("A job interval must be positive and finite.")
        if self.due is not None and not callable(self.due):
            raise ValueError("A job's due predicate must be callable.")
        if not _finite(self.needs_idle_s) or self.needs_idle_s < 0:
            raise ValueError("A job's idle requirement must be nonnegative and finite.")


@dataclass(frozen=True)
class _Run:
    last_run: float | None = None
    last_outcome: str | None = None
    failures: int = 0
    retry_from: float | None = None
    retry_run: bool = False

    def status(self):
        return {"last_run": self.last_run, "last_outcome": self.last_outcome, "failures": self.failures}


_NEVER_RUN = _Run()


class _JobNotStarted(Exception):
    """Apply failure backoff without advancing the job's schedule."""


def _regular_private(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()


def _decode_run(value):
    if not isinstance(value, dict):
        return None
    last = value.get("last_run")
    outcome = value.get("last_outcome")
    failures = value.get("failures")
    if (last is not None and not _finite(last)) or type(failures) is not int:
        return None
    if outcome == "ok" and failures == 0 and last is not None:
        return _Run(last, outcome)
    if outcome == "error" and failures > 0:
        retry_from = value.get("retry_from", last)
        # Older records did not distinguish job failures from predicate failures.
        retry_run = value.get("retry_run", True)
        if _finite(retry_from) and type(retry_run) is bool:
            return _Run(last, outcome, failures, retry_from, retry_run)
    return None


def _read_state(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not _regular_private(info) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > _STATE_LIMIT):
                return {}, True
            data = stream.read(_STATE_LIMIT + 1)
            if len(data) > _STATE_LIMIT:
                return {}, True
            raw = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return {}, True
    if not isinstance(raw, dict):
        return {}, True
    rows = {}
    for job, value in raw.items():
        row = _decode_run(value)
        if row is not None:
            rows[job] = row
    return rows, len(rows) != len(raw)


def _write_state(directory, name, rows):
    mode = os.fstat(directory).st_mode
    if not mode & stat.S_IWUSR:
        raise PermissionError
    if stat.S_IMODE(mode) != 0o700:
        os.fchmod(directory, 0o700)
    with suppress(FileNotFoundError):
        if not _regular_private(os.stat(name, dir_fd=directory, follow_symlinks=False)):
            raise PermissionError
    raw = json.dumps({key: vars(row) for key, row in rows.items()}, allow_nan=False).encode()
    if len(raw) > _STATE_LIMIT:
        raise ValueError("Job state exceeds its size limit.")
    temp = f".jobs-{uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=directory)


class Scheduler:
    def __init__(self, jobs: Iterable[Job], *, state=None, clock=time.time, idle=None, tick_s=30.0):
        job_list = tuple(jobs)
        self._jobs = {job.name: job for job in job_list}
        if len(self._jobs) != len(job_list):
            raise ValueError("Job names must be unique.")
        if not _finite(tick_s) or not 0 < tick_s <= threading.TIMEOUT_MAX:
            raise ValueError("The tick interval is out of range.")
        self._state = Path(config.V3_DIR) / "jobs/state.json" if state is None else Path(state)
        self._clock = clock
        self._idle = idle_seconds if idle is None else idle
        self._tick_s = tick_s
        self._tick_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._rows = {}
        self._disk_rows = {}
        self._stop = threading.Event()
        self._thread = None

    def status(self):
        with self._status_lock:
            return {name: self._rows.get(name, _NEVER_RUN).status() for name in self._jobs}

    def _refresh(self, directory):
        rows, malformed = _read_state(directory, self._state.name)
        if malformed:
            # Salvage intact records without replacing any history we already hold.
            with self._status_lock:
                for name, row in rows.items():
                    self._rows.setdefault(name, row)
            if self._rows:
                self._save(directory)
            return

        # Preserve unsaved outcomes while the corresponding disk record is
        # unchanged. A scheduler that advanced it wins once storage recovers.
        with self._status_lock:
            for name, row in rows.items():
                if row != self._disk_rows.get(name):
                    self._rows[name] = row
        self._disk_rows = rows
        if self._rows != rows:
            self._save(directory)

    def _save(self, directory):
        if directory is None:
            return
        try:
            _write_state(directory, self._state.name, self._rows)
        except (OSError, ValueError):
            pass
        else:
            self._disk_rows = self._rows.copy()

    def _record(self, directory, name, row):
        with self._status_lock:
            self._rows[name] = row
        self._save(directory)

    def _ready(self, job, now, row):
        if row.failures:
            delay = min(3600, 60 * 2 ** min(row.failures - 1, 6))
            if now - row.retry_from < delay:
                return False
            if row.retry_run and job.due is None:
                return True
        if job.due is not None:
            return job.due(now, row.last_run)
        return row.last_run is None or now - row.last_run >= job.every_s

    def _is_idle(self, seconds):
        try:
            value = self._idle()
            return type(value) in (int, float) and value >= seconds
        except BaseException:
            return False

    def _run_due(self, directory, stop):
        outcomes = []
        for name, job in self._jobs.items():
            if stop.is_set():
                break
            now = self._clock()
            row = self._rows.get(name, _NEVER_RUN)
            last_run = min(row.last_run, now) if row.last_run is not None else None
            retry_from = min(row.retry_from, now) if row.retry_from is not None else None
            if (last_run, retry_from) != (row.last_run, row.retry_from):
                row = replace(row, last_run=last_run, retry_from=retry_from)
                self._record(directory, name, row)

            started = time.monotonic()
            retry_run = False
            try:
                if not self._ready(job, now, row):
                    continue
                if stop.is_set():
                    break
                if job.needs_idle_s and not self._is_idle(job.needs_idle_s):
                    outcomes.append({"job": name, "outcome": "busy", "seconds": 0.0})
                    continue
                now = self._clock()
                started = time.monotonic()
                if stop.is_set():
                    break
                retry_run = True
                job.run()
            except _JobNotStarted:
                result = _Run(row.last_run, "error", row.failures + 1, self._clock(), retry_run)
            except BaseException:
                # Job code must never terminate the worker or publish its exception.
                result = _Run(now, "error", row.failures + 1, self._clock(), retry_run)
            else:
                result = _Run(now, "ok")
            self._record(directory, name, result)
            outcomes.append({"job": name, "outcome": result.last_outcome,
                             "seconds": time.monotonic() - started})
        return outcomes

    def tick(self):
        return self._tick(self._stop)

    def _tick(self, stop):
        if stop.is_set() or not self._jobs or not self._tick_lock.acquire(blocking=False):
            return []
        try:
            with ExitStack() as stack:
                try:
                    directory = stack.enter_context(open_directory(self._state.parent, create=True))
                except OSError:
                    return self._run_due(None, stop)
                if os.fstat(directory).st_uid != os.getuid():
                    return []
                try:
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return []
                self._refresh(directory)
                return self._run_due(directory, stop)
        finally:
            self._tick_lock.release()

    def _loop(self, stop):
        while not stop.wait(self._tick_s):
            self._tick(stop)

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            stop = threading.Event()
            worker = threading.Thread(target=self._loop, args=(stop,), name="jarvis-v3-jobs", daemon=True)
            worker.start()
            self._stop = stop
            self._thread = worker

    def stop(self):
        with self._lifecycle_lock:
            self._stop.set()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(_STOP_GRACE_S)


def idle_seconds(roots=None, *, now=None, chats_root=None):
    """Measure activity using file mtimes and treat incomplete scans as busy."""
    if roots is None:
        home = Path.home()
        chats = Path(config.V3_DIR) / "chats" if chats_root is None else chats_root
        roots = (home / ".claude/projects", home / ".codex/sessions", chats)
    now = time.time() if now is None else now
    remaining = _IDLE_SCAN_ENTRIES
    newest = -math.inf

    def scan(directory, depth):
        nonlocal remaining, newest
        with os.scandir(directory) as entries:
            for entry in entries:
                remaining -= 1
                if remaining < 0:
                    return False
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if depth == _IDLE_SCAN_DEPTH:
                            return False
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        child = os.open(entry.name, flags, dir_fd=directory)
                        try:
                            if not scan(child, depth + 1):
                                return False
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        newest = max(newest, info.st_mtime)
                except FileNotFoundError:
                    continue
        return True

    for root in roots:
        try:
            with open_directory(Path(root)) as directory:
                if not scan(directory, 0):
                    return 0.0
        except FileNotFoundError:
            continue
        except OSError:
            return 0.0
    return 1e12 if newest == -math.inf else max(0.0, now - newest)


def _google_scopes(path: Path, *, require_oauth_fields=False) -> set[str]:
    """Read the saved grant scopes without returning OAuth credentials."""
    try:
        with open_directory(path.parent) as directory:
            fd = os.open(path.name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not _regular_private(info) or info.st_mode & 0o077
                        or not info.st_mode & stat.S_IRUSR or info.st_size > _GOOGLE_GRANT_LIMIT):
                    return set()
                data = stream.read(_GOOGLE_GRANT_LIMIT + 1)
                if len(data) > _GOOGLE_GRANT_LIMIT:
                    return set()
                bundle = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return set()
    if not isinstance(bundle, dict):
        return set()
    if require_oauth_fields:
        fields = (bundle.get(key) for key in ("client_id", "client_secret", "refresh_token"))
        if not all(isinstance(value, str) and value for value in fields):
            return set()
    scopes = bundle.get("scopes")
    return set(scopes) if isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes) else set()


def _has_gmail_grant(path: Path) -> bool:
    return _GMAIL_READONLY in _google_scopes(path, require_oauth_fields=True)


def _find_cli(name):
    path = Path.home() / ".local" / "bin" / name
    if path.is_file() and os.access(path, os.X_OK):
        return path
    return None


def default_jobs(*, feed_root=None, index_path=None, google_token=None, chats_root=None,
                 signals_dir=None, find_cli=None):
    """Build the daemon jobs without opening their stores or contacting providers.

    The Gmail grant check and OAuth refresh read the file separately.
    A test grant at a different path can exercise wiring with a fake client.
    The transcript ingest's `budget_s` bounds the scan. The index opens first.
    The reviewer consumes the queue after transcript ingestion.
    """
    feed = None
    index = None
    gmail_runner = None
    reviewer_runner = None
    calendar_runner = None
    budget = None
    lookup = _find_cli if find_cli is None else find_cli

    def get_feed():
        nonlocal feed
        if feed is None:
            from .feed import Feed

            feed = Feed() if feed_root is None else Feed(feed_root)
        return feed

    def feed_tick():
        get_feed().tick()

    idle_required = 2 * 3600

    def curator_due(now, last_run):
        from .skill_curator import due

        # The scheduler separately checks that the owner meets this threshold.
        return due(now, last_run=last_run, idle_s=idle_required)

    def curate():
        from .skill_curator import Curator
        from .skill_store import SkillStore

        Curator(SkillStore()).run()

    def get_index():
        nonlocal index
        if index is None:
            from .index import Index

            index = Index(config.V3_INDEX_DB if index_path is None else index_path)
        return index

    def ingest_transcripts():
        from .ingest.transcripts import ingest

        if chats_root is None:
            ingest(get_index(), budget_s=20)
        else:
            ingest(get_index(), chats=chats_root, budget_s=20)

    def review_sessions():
        nonlocal reviewer_runner
        if reviewer_runner is None:
            from .budget import Budget
            from .claude_usage import read_windows
            from .review_queue import Consumer
            from .reviewer import Reviewer

            reviewer_runner = Reviewer(consumer=Consumer(), budget=Budget(usage=read_windows), index=get_index())
        if any(row["outcome"] == "error" for row in reviewer_runner.tick()):
            raise RuntimeError("Session review failed.")

    def ingest_gmail():
        nonlocal gmail_runner
        configured = Path(config.GOOGLE_TOKEN_PATH)
        path = configured if google_token is None else Path(google_token)
        if not _has_gmail_grant(path):
            return
        if gmail_runner is None:
            from jarvis.google_auth import access_token
            from .ingest import Runner
            from .ingest.gmail import GmailIngester, HttpGmail

            def test_grant_token():
                # A test grant at a different path cannot authorize real OAuth.
                raise RuntimeError("The Gmail grant is outside the OAuth token path.")

            token = access_token if path == configured else test_grant_token
            gmail_runner = Runner(get_index(), [GmailIngester(HttpGmail(token))])
        outcome = gmail_runner.tick().get("gmail")
        if outcome is not None and outcome["error"] is not None:
            raise RuntimeError("Gmail ingestion failed.")

    def tick_ingester(runner, name):
        outcome = runner.tick().get(name)
        if outcome is not None and outcome["error"] is not None:
            raise RuntimeError(f"{name} ingestion failed.")

    def ingest_calendar():
        nonlocal calendar_runner
        configured = Path(config.GOOGLE_TOKEN_PATH)
        path = configured if google_token is None else Path(google_token)
        if not (_google_scopes(path, require_oauth_fields=True) & _CALENDAR_INGEST_SCOPES):
            return
        if calendar_runner is None:
            from jarvis.google_auth import access_token
            from .ingest import Runner
            from .ingest.calendar import CalendarIngester, HttpCalendar

            def test_grant_token():
                # A test grant at a different path cannot authorize real OAuth.
                raise RuntimeError("The Calendar grant is outside the OAuth token path.")

            shared = get_index()
            token = access_token if path == configured else test_grant_token
            client = HttpCalendar(token)
            calendar_runner = Runner(shared, [CalendarIngester(client, signals_dir=signals_dir)])
        tick_ingester(calendar_runner, "calendar")

    def cli_job(name, executable, interval):
        runner = path = None

        def run_cli(argv):
            from .ingest import proc

            return proc.run_bounded([os.fspath(path), *argv[1:]], timeout_s=60, max_bytes=8 * 1024 * 1024)

        def poll():
            nonlocal runner, path
            path = lookup(executable)
            if path is None:
                return
            if runner is None:
                from .ingest import Runner

                if name == "schoolwork":
                    from .ingest.schoolwork import SchoolworkIngester

                    ingester = SchoolworkIngester(run_cli, post=get_feed().post, signals_dir=signals_dir)
                else:
                    from .ingest.github import GitHubIngester

                    ingester = GitHubIngester(run_cli, signals_dir=signals_dir, require_pagination=True)
                runner = Runner(get_index(), [ingester])
            tick_ingester(runner, name)

        return Job(name, poll, every_s=interval)

    def consolidate_due(now, last_run):
        from .consolidate import due

        return due(now, last_run=last_run)

    def get_budget():
        nonlocal budget
        if budget is None:
            from .budget import Budget
            from .claude_usage import read_windows

            budget = Budget(usage=read_windows)
        return budget

    def consolidate():
        from .consolidate import run

        result = run(index=get_index(), budget=get_budget())
        if result is not None and result.outcome == "error":
            raise RuntimeError("Consolidation failed.")

    def brief_due(which, now, last_run):
        from .daily_briefs import due, schedule_from_env

        morning, evening = schedule_from_env(os.environ)
        return due(now, last_run=last_run, which=which, morning=morning, evening=evening)

    def learning_due(now, last_run):
        from .daily_briefs import schedule_from_env
        from .learning_digest import DIGEST_AT, due

        _, evening = schedule_from_env(os.environ)
        return due(now, last_run=last_run, at=min(DIGEST_AT, evening))

    def post_learning():
        from .learning_digest import run

        if run(feed=get_feed()).outcome == "error":
            raise RuntimeError("Learning digest failed.")

    def post_brief(which):
        from .daily_briefs import read_calendar, read_mail, run

        def mail(since, until):
            return read_mail(get_index(), since, until)

        try:
            path = Path(config.GOOGLE_TOKEN_PATH if google_token is None else google_token)
            scopes = _google_scopes(path)
            calendar = partial(read_calendar, grant_path=path) if scopes & _CALENDAR_SCOPES else None
            feed, brief_budget = get_feed(), get_budget()
        except Exception as exc:
            raise _JobNotStarted from exc

        learning = None
        if which == "evening":
            from .learning_digest import summary_line

            learning = summary_line
        result = run(which, feed=feed, budget=brief_budget, calendar=calendar, mail=mail, learning=learning)
        if result.outcome == "error":
            if result.retryable:
                raise _JobNotStarted
            raise RuntimeError("Daily brief failed.")

    return [Job("feed", feed_tick, every_s=60),
            Job("skill-curator", curate, due=curator_due, needs_idle_s=idle_required),
            Job("transcripts", ingest_transcripts, every_s=300),
            Job("reviewer", review_sessions, every_s=300),
            Job("gmail", ingest_gmail, every_s=300),
            Job("calendar", ingest_calendar, every_s=300),
            cli_job("schoolwork", "sw", 900),
            cli_job("github", "gh", 600),
            Job("consolidate", consolidate, due=consolidate_due),
            Job("morning-brief", partial(post_brief, "morning"), due=partial(brief_due, "morning")),
            Job("learning-digest", post_learning, due=learning_due),
            Job("evening-wrap", partial(post_brief, "evening"), due=partial(brief_due, "evening"))]
