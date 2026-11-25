"""Admission for Claude subscription jobs and their shared spend ledger."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import stat
from threading import Lock
import time
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config
from .claude_jobs import JobResult
from .claude_usage import _expired, _percent
from .daemon import _prepare_data_dir

CAPS = {"reviewer": 40, "consolidate": 2, "planner": 24, "style": 2, "brief": 4}
DAILY_CAP = 60
USAGE_LIMIT = 75.0
_CHICAGO = ZoneInfo("America/Chicago")
_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
_LEDGER_ERROR = "The Claude budget ledger is unavailable."


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str
    outcome: Literal["ok", "skipped-budget", "skipped-usage"]
    usage_pct: float | None
    ticket: str | None


@dataclass(frozen=True)
class _Hold:
    job: str
    when: datetime
    admission: Admission
    started: bool


def _open_data_dir(path: Path) -> int:
    # Pin each component before opening the next. The daemon helper can
    # prepare the last component through the pinned parent's procfs handle.
    root, *parts = path.absolute().parts
    directory = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        if not parts:
            raise OSError(_LEDGER_ERROR)
        _prepare_data_dir(Path(f"/proc/self/fd/{directory}") / parts[-1])
        return os.open(parts[-1], _DIR_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)


@contextmanager
def _private_file(directory: int, name: str):
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(_LEDGER_ERROR)
        os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


def _read_spend(fd: int, day) -> Counter:
    counts = Counter()
    # The last line only commits once its newline has been written.
    with open(fd, "rb", closefd=False) as ledger:
        for line in ledger:
            if not line.endswith(b"\n"):
                break
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                outcome = row.get("outcome")
                # An outcome with a start ticket supplies totals for a slot already counted.
                if outcome == "started":
                    if not isinstance(row.get("start_ticket"), str):
                        continue
                elif outcome not in ("ok", "error") or "start_ticket" in row:
                    continue
                job = row.get("job")
                if not isinstance(job, str) or job not in CAPS:
                    continue
                when = datetime.fromisoformat(row["ts"])
                if when.tzinfo is not None and when.astimezone(_CHICAGO).date() == day:
                    counts[job] += 1
            except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
                continue
    return counts


def _append(fd: int, row: dict, *, durable: bool = False) -> None:
    line = (json.dumps(row, allow_nan=False) + "\n").encode("utf-8")
    end = os.lseek(fd, 0, os.SEEK_END)
    cursor = end
    while cursor:
        start = max(0, cursor - 4096)
        chunk = os.pread(fd, cursor - start, start)
        if len(chunk) != cursor - start:
            raise OSError(_LEDGER_ERROR)
        newline = chunk.rfind(b"\n")
        if newline != -1:
            cursor = start + newline + 1
            break
        cursor = start
    if cursor < end:
        os.ftruncate(fd, cursor)
    try:
        if os.write(fd, line) != len(line):
            raise OSError(_LEDGER_ERROR)
        if durable:
            os.fsync(fd)
    except OSError:
        if durable:
            os.ftruncate(fd, cursor)
        raise


def _peak(reading, now: float) -> float | None:
    if not isinstance(reading, list):
        return None
    current = []
    for window in reading:
        if not isinstance(window, dict):
            return None
        percent = _percent(window.get("used_percent"))
        if percent is None:
            return None
        if not _expired(window.get("resets_at"), now):
            current.append(percent)
    return max(current, default=None)


def _row(when, job, model, input_tokens, output_tokens, usage_pct, outcome) -> dict:
    return {
        "ts": when.isoformat(),
        "job": job,
        "model": model,
        "provider": "claude-sub",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_pct_before": usage_pct,
        "outcome": outcome,
    }


class Budget:
    """Share one instance for admission within a daemon.

    The lock file serializes ledger access across processes, and recorded
    jobs are visible to every reader. Ordinary holds are local to this instance.
    A caller ready to start can record its spend at admission, then append the
    outcome and token totals against that ticket when the runner returns.
    """

    def __init__(self, path=None, *, usage, now=time.time, caps=None, daily_cap=None):
        self._path = (config.V3_DIR / "budget.jsonl" if path is None else Path(path)).absolute()
        self._usage = usage
        self._now = now
        self._caps = CAPS | ({} if caps is None else caps)
        self._daily_cap = DAILY_CAP if daily_cap is None else daily_cap
        self._lock = Lock()
        self._held: dict[str, _Hold] = {}

    @contextmanager
    def _ledger(self):
        directory = _open_data_dir(self._path.parent)
        try:
            with _private_file(directory, self._path.name + ".lock") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with _private_file(directory, self._path.name) as ledger:
                    yield ledger
        finally:
            os.close(directory)

    @staticmethod
    def _check_job(job):
        if not isinstance(job, str) or job not in CAPS:
            raise ValueError("unknown Claude budget job")

    def admit(self, job: str, *, start_model: str | None = None) -> Admission:
        self._check_job(job)
        with self._lock:
            try:
                # The ledger is not locked during the usage request, so the caps are judged again after it:
                # another process may have spent, and the day may have turned.
                with self._ledger() as ledger:
                    when = datetime.fromtimestamp(self._now(), _CHICAGO)
                    if (refused := self._over_cap(ledger, job, when)) is not None:
                        return refused
                try:
                    pct = _peak(self._usage(), self._now())
                except Exception:
                    pct = None
                if pct is None:
                    return Admission(False, "Claude usage is unknown.", "skipped-usage", None, None)
                if pct >= USAGE_LIMIT:
                    return Admission(False, f"Claude usage is {pct:g}%.", "skipped-usage", pct, None)
                with self._ledger() as ledger:
                    when = datetime.fromtimestamp(self._now(), _CHICAGO)
                    if (refused := self._over_cap(ledger, job, when)) is not None:
                        return refused
                    ticket = uuid4().hex
                    admission = Admission(True, "", "ok", pct, ticket)
                    if start_model is not None:
                        row = _row(when, job, start_model, 0, 0, pct, "started")
                        _append(ledger, {**row, "start_ticket": ticket}, durable=True)
            except (OSError, RuntimeError):
                if start_model is not None:
                    raise
                return Admission(False, _LEDGER_ERROR, "skipped-budget", None, None)
            self._held[ticket] = _Hold(job, when, admission, started=start_model is not None)
            return admission

    def _over_cap(self, ledger: int, job: str, when: datetime) -> Admission | None:
        counts = _read_spend(ledger, when.date())
        counts.update(hold.job for hold in self._held.values()
                      if not hold.started and hold.when.date() == when.date())
        if counts[job] >= self._caps[job]:
            reason = f"The daily {job} cap was reached."
        elif counts.total() >= self._daily_cap:
            reason = "The daily Claude job cap was reached."
        else:
            return None
        return Admission(False, reason, "skipped-budget", None, None)

    def record(self, admission: Admission, result: JobResult) -> None:
        self._check_job(result.name)
        with self._lock:
            hold = self._held.get(admission.ticket)
            if hold is None or admission is not hold.admission or result.name != hold.job:
                raise ValueError("invalid Claude budget ticket or job")
            row = _row(hold.when, hold.job, result.model, result.input_tokens, result.output_tokens,
                       admission.usage_pct, "ok" if result.ok else "error")
            if hold.started:
                row["start_ticket"] = admission.ticket
            with self._ledger() as ledger:
                _append(ledger, row)
            del self._held[admission.ticket]

    def release(self, admission: Admission) -> None:
        with self._lock:
            hold = self._held.get(admission.ticket)
            if hold is not None and admission is hold.admission:
                del self._held[admission.ticket]

    def skip(self, job: str, admission: Admission) -> None:
        self._check_job(job)
        if admission.ok or admission.outcome not in ("skipped-budget", "skipped-usage"):
            raise ValueError("a skipped job requires a refused admission")
        with self._ledger() as ledger:
            when = datetime.fromtimestamp(self._now(), _CHICAGO)
            _append(ledger, _row(when, job, "haiku", 0, 0, admission.usage_pct, admission.outcome))

    def used(self) -> dict:
        with self._ledger() as ledger:
            day = datetime.fromtimestamp(self._now(), _CHICAGO).date()
            counts = _read_spend(ledger, day)
        return {"day": day.isoformat(), "total": counts.total(), "jobs": dict(counts)}
