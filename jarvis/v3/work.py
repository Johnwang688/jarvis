"""Owner-registered projects and a ledger of T3 threads started by Jarvis.

Limits, by design:
- The registry names a folder by its path. A different folder later put at
  the same path is the same project; the path is resolved and checked again
  when work is planned and when it starts, not held open in between.
- The registry and the ledger are trusted as the owner's private files
  (0600 in a 0700 folder); their contents are checked for shape, not origin.
- What T3 answers is untrusted. A thread or run id that is not a plain
  identifier is refused and never stored, shown or put in an error.
"""

from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time as local_time, timedelta
import fcntl
import hashlib
import io
from itertools import chain
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import unicodedata
from uuid import uuid4

from jarvis import config, untrusted

from . import t3_client
from ._paths import open_directory
from .work_status import FINAL as _FINAL_STATUSES, category as work_category


_REGISTRY_LIMIT = 256 * 1024
_LEDGER_LIMIT = 8 * 1024 * 1024
_PROJECT_LIMIT = 32
_TEXT_LIMIT = 16_000
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# T3's ids look like `cli:<uuid>` for a thread and
# `cli:thread:cli%3A<uuid>:message:1` for a run. Anything that is not a short
# identifier of that alphabet (no spaces) is treated as text T3 was made to
# echo, and is not used.
_T3_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:%-]{0,127}")
_SUFFIX = re.compile(r"[a-f0-9]{4}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Default-ignorable marks, fillers and blank glyphs outside the shared control categories.
_INVISIBLE_PATH = re.compile(
    r"[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2065\u2800\u3164"
    r"\ud800-\udfff\ufe00-\ufe0f\uffa0\ufff0-\ufff8\U00013441\U00013442"
    r"\U0001d159\U000e0000-\U000e0fff]"
)
_CREDENTIAL_DIRS = (".config/jarvis", ".ssh", ".gnupg", ".claude", ".codex", ".t3")
_PROJECT_FIELDS = {"path", "provider", "model", "base"}
_PLAN_FIELDS = _PROJECT_FIELDS | {"project", "title", "brief", "branch"}
_EVENT_FIELDS = {"v", "kind", "thread_id", "at"}
_START_FIELDS = _EVENT_FIELDS | {"run_id", "project", "title", "branch", "brief_sha256"}
_GENERAL_PROJECT = "jarvis"
_WORKSPACE_PROJECT = "jarvis-workspace"
GENERAL_WORKSPACE = "General workspace"
_MESSAGE_PROVIDER = "codex"
_MESSAGE_MODEL = "gpt-6.2-sol"
_DELEGATE_PROVIDER = "codex"
_DELEGATE_MODEL = "gpt-6.2-sol"
DELEGATE_MODEL_LABEL = "GPT-6.2 Sol"
DELEGATE_TASK_LIMIT = 4_000
DELEGATE_DAILY_LIMIT = 12
DELEGATE_DAY_ERROR = "Only 12 tasks may be handed off per day."
DELEGATE_PROMPT = (
    "The owner asked Jarvis for this task. Jarvis handed it to you to do in the background. "
    "Do the task. If something you need is missing or the task is unsafe or unclear, stop and say "
    "what you need instead of guessing. Finish with a short, plain summary of what you did and "
    "what, if anything, is left."
)
_DELEGATE_ORIGINS = frozenset({"desk", "voice", "phone"})
_STATUS_FIELDS = _EVENT_FIELDS | {"status", "waiting_on_owner"}
_UNKNOWN_THREAD = "That is not a thread Jarvis started."
_WRONG_THREAD = "T3 returned a different thread than requested."


class WorkError(Exception):
    """An error safe to show to the owner."""


@dataclass(frozen=True)
class Project:
    name: str
    path: str
    provider: str
    model: str
    base: str | None


@dataclass(frozen=True)
class Thread:
    thread_id: str
    project: str
    title: str
    started_at: float
    branch: str | None
    brief_sha256: str
    status: str
    waiting_on_owner: bool
    updated_at: float
    message: bool = False
    notice_key: str | None = None
    settled_once: bool = False
    fallback_status: str | None = None
    fallback: dict | None = field(default=None, repr=False)
    relay_sha256: str | None = None
    delegated: bool = False
    origin: str | None = None
    noticed_categories: frozenset[str] = frozenset()


def _checked(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except t3_client.T3Error as exc:
        raise WorkError(str(exc)) from None


def _name(value):
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise WorkError("Invalid project name.")
    return value


def _allowed_path(path):
    home = Path.home().absolute()
    protected = (Path(config.V3_DIR).absolute(), *(home / part for part in _CREDENTIAL_DIRS))
    if path in (Path("/"), home, home.resolve()) or any(
        path.is_relative_to(root) or path.is_relative_to(root.resolve()) for root in protected
    ):
        raise WorkError("That directory cannot be registered for work.")


def _visible_path(path):
    text = str(path)
    if _INVISIBLE_PATH.search(text) or any(map(untrusted.unseen, text)):
        raise WorkError("Project path contains invisible or control characters.")


def _resolve_path(value):
    try:
        original = Path(value)
        if not original.is_absolute():
            raise WorkError("Project path must be absolute.")
        _visible_path(original)
        path = original.resolve(strict=True)
        _visible_path(path)
        if not path.is_dir() or original.resolve(strict=True) != path:
            raise WorkError("Project path must be a stable, existing directory.")
        _allowed_path(path)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise WorkError("Project path must be a stable, existing directory.") from None
    return _checked(t3_client._project, path)


def _project(name, path, provider, model, base):
    _checked(t3_client._project, path)
    # A registered directory can disappear. Keep its entry removable and
    # check that the canonical path still exists before handing it to T3.
    stored = Path(path)
    if stored.anchor != "/" or ".." in stored.parts or str(stored) != path:
        raise WorkError("Invalid registered project path.")
    _allowed_path(stored)
    return Project(
        _name(name), path, _checked(t3_client._arg, provider), _checked(t3_client._arg, model),
        None if base is None else _checked(t3_client._arg, base),
    )


def _title(value, *, message=False):
    if not isinstance(value, str) or any(
        char in _BIDI_CONTROLS or unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
        for char in value
    ):
        raise WorkError("Invalid thread title.")
    if message and value.strip() and 4 <= len(value) <= 80:
        return value
    title = value.strip()
    if message:
        raise WorkError("Message subject must be 4 to 80 characters.")
    if not 1 <= len(title) <= 80:
        raise WorkError("Thread title must be 1 to 80 characters.")
    return _checked(t3_client._arg, title)


def _message(value):
    if not isinstance(value, str) or len(value) > _TEXT_LIMIT:
        raise WorkError("Work text must be 1 to 16,000 characters.")
    return _checked(t3_client._message, value)


def _shown_whole(value, what):
    # A card shows cleaned text. The agent must get exactly what the card showed.
    if untrusted.strip_invisible(value) != value:
        raise WorkError(f"The {what} has characters that cannot be shown. Write it in plain text.")


def _delegation_task(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= DELEGATE_TASK_LIMIT:
        raise WorkError("Task must be 1 to 4,000 characters after trimming.")
    _message(value)
    _shown_whole(value, "task")
    first_line = " ".join(value.strip().splitlines()[0].split())
    if len(first_line) > 60:
        prefix = first_line[:60]
        if first_line[60] != " ":
            # A first word longer than the limit (a link, a path) is cut where it stands.
            prefix = prefix.rpartition(" ")[0] or prefix
        first_line = prefix.rstrip()
    return _title(first_line)


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-") or "work"


def _valid_suffix(value):
    return isinstance(value, str) and _SUFFIX.fullmatch(value) is not None


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate work file key.")
    return result


def _version_one(data):
    return type(data) is dict and type(data.get("v")) is int and data["v"] == 1


@contextmanager
def _directory(path, *, create=False):
    with open_directory(path.parent, create=create) as directory:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise WorkError("Work state directory must be private.")
        yield directory


@contextmanager
def _file(directory, name, flags):
    fd = os.open(name, flags | _NOFOLLOW, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise WorkError("Work state file must be private and regular.")
        yield fd
    finally:
        os.close(fd)


def _read(fd, limit):
    if os.fstat(fd).st_size > limit:
        raise WorkError("Work state file exceeds its size limit.")
    with os.fdopen(os.dup(fd), "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise WorkError("Work state file exceeds its size limit.")
    return raw


class Registry:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self, directory):
        try:
            with _file(directory, self.path.name, os.O_RDONLY) as fd:
                before = os.fstat(fd).st_ctime_ns
                raw = _read(fd, _REGISTRY_LIMIT)
                if before != os.fstat(fd).st_ctime_ns:
                    raise WorkError("Project registry changed during the read.")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not _version_one(data) or data.keys() != {"v", "projects"}:
                raise ValueError
            rows = data["projects"]
            if type(rows) is not dict or len(rows) > _PROJECT_LIMIT:
                raise ValueError
            if any(type(row) is not dict or row.keys() != _PROJECT_FIELDS for row in rows.values()):
                raise ValueError
            return {name: _project(name, **row) for name, row in rows.items()}
        except (OSError, ValueError, TypeError, RuntimeError, WorkError):
            pass
        raise WorkError("Project registry is malformed.")

    def _replace(self, directory, projects):
        rows = {name: {key: value for key, value in asdict(project).items() if key != "name"}
                for name, project in projects.items()}
        raw = (json.dumps({"v": 1, "projects": rows}, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) > _REGISTRY_LIMIT:
            raise WorkError("Project registry exceeds its size limit.")
        tmp = ".projects-" + uuid4().hex
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=directory)

    @contextmanager
    def _locked(self, lock, *, create=False):
        with _directory(self.path, create=create) as directory:
            # The directory inode stays stable across registry replacements.
            fcntl.flock(directory, lock)
            yield directory

    def list(self) -> list[Project]:
        try:
            with self._locked(fcntl.LOCK_SH) as directory:
                projects = self._read(directory)
        except FileNotFoundError:
            return []
        except OSError:
            raise WorkError("Project registry could not be read safely.") from None
        return [projects[name] for name in sorted(projects)]

    def get(self, name) -> Project:
        for project in self.list():
            if project.name == name:
                return project
        raise WorkError("Unknown work project.")

    def message_project(self, name) -> tuple[Project, bool]:
        projects = {project.name: project for project in self.list()}
        project = projects.get(name) or projects.get(_GENERAL_PROJECT)
        if project is not None:
            return project, False
        return self.builtin_message_project(), True

    def builtin_message_project(self) -> Project:
        path = self._builtin_path(self.path.parent.parent / "messages", "Jarvis message project", create=True)
        return Project(_GENERAL_PROJECT, path, _MESSAGE_PROVIDER, _MESSAGE_MODEL, None)

    def builtin_workspace(self, *, create=False) -> Project:
        path = self._builtin_path(self.path.parent / "workspace", "Jarvis workspace", create=create)
        return Project(_WORKSPACE_PROJECT, path, _DELEGATE_PROVIDER, _DELEGATE_MODEL, None)

    @staticmethod
    def _builtin_path(path, label, *, create):
        # Keep these directories outside the owner-managed project registry.
        path = path.absolute()
        if create:
            with open_directory(path, create=True) as directory:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise WorkError(f"The {label} directory must be private.")
        return str(path)

    def add(self, name, path, *, provider, model, base=None) -> Project:
        project = _project(name, _resolve_path(path), provider, model, base)
        try:
            with self._locked(fcntl.LOCK_EX, create=True) as directory:
                projects = self._read(directory)
                if name in projects:
                    raise WorkError("A work project with that name already exists.")
                if len(projects) >= _PROJECT_LIMIT:
                    raise WorkError("The work registry already has 32 projects.")
                projects[name] = project
                self._replace(directory, projects)
        except OSError:
            raise WorkError("Project registry could not be updated safely.") from None
        return project

    def remove(self, name) -> None:
        _name(name)
        try:
            with self._locked(fcntl.LOCK_EX) as directory:
                projects = self._read(directory)
                if projects.pop(name, None) is None:
                    if name == _GENERAL_PROJECT:
                        raise WorkError("The built-in Jarvis project cannot be removed.")
                    raise WorkError("Unknown work project.")
                self._replace(directory, projects)
        except FileNotFoundError:
            if name == _GENERAL_PROJECT:
                raise WorkError("The built-in Jarvis project cannot be removed.") from None
            raise WorkError("Unknown work project.") from None
        except OSError:
            raise WorkError("Project registry could not be updated safely.") from None


def _fallback_fields(fields, *, replay=False):
    from .feed import FeedError, _fields

    try:
        return _fields(fields, replay=replay)
    except FeedError:
        raise WorkError("Invalid work notice fallback.") from None


def _event(row):
    if not _version_one(row):
        raise WorkError("Invalid work ledger event.")
    kind = row.get("kind")
    at = row.get("at")
    if type(at) not in (int, float) or not -sys.float_info.max <= at <= sys.float_info.max:
        raise WorkError("Invalid work ledger time.")
    _checked(t3_client._arg, row.get("thread_id"))
    if (kind == "start" and _START_FIELDS <= row.keys() <= _START_FIELDS | {
        "message", "notice_key", "fallback", "relay_sha256", "delegated", "origin",
    }):
        _checked(t3_client._arg, row["run_id"])
        _name(row["project"])
        digest = row["brief_sha256"]
        message = row.get("message", False)
        notice = row.get("notice_key")
        relay_digest = row.get("relay_sha256")
        if "delegated" in row or "origin" in row:
            origin = row.get("origin")
            if row.get("delegated") is not True or message or not isinstance(origin, str) or origin not in _DELEGATE_ORIGINS:
                raise WorkError("Invalid delegation in work ledger.")
        if (type(message) is bool
                and (not message or row["branch"] is None
                     or ("notice_key" in row and "relay_sha256" in row and "fallback" not in row))
                and ("notice_key" not in row
                     or (message and isinstance(notice, str) and _SHA256.fullmatch(notice)))
                and ("relay_sha256" not in row or (message and isinstance(relay_digest, str)
                                                   and _SHA256.fullmatch(relay_digest)))
                and _title(row["title"], message=message) == row["title"]
                and isinstance(digest, str) and _SHA256.fullmatch(digest) is not None):
            if row["branch"] is not None:
                _checked(t3_client._arg, row["branch"])
            if "fallback" in row:
                if not message:
                    raise WorkError("Invalid work ledger event.")
                _fallback_fields(row["fallback"], replay=True)
            return row
    if kind == "status" and row.keys() == _STATUS_FIELDS and type(row["waiting_on_owner"]) is bool:
        _checked(t3_client._arg, row["status"])
        return row
    if (kind == "fallback" and row.keys() == _EVENT_FIELDS | {"status"}
            and isinstance(row["status"], str) and row["status"] in _FINAL_STATUSES):
        return row
    raise WorkError("Invalid work ledger event.")


def _fold(lines):
    threads = {}
    for line in lines:
        try:
            row = _event(json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object))
        except (ValueError, RecursionError, WorkError):
            continue
        thread_id, at = row["thread_id"], float(row["at"])
        if row["kind"] == "start":
            threads.setdefault(thread_id, Thread(
                thread_id, row["project"], row["title"], at, row["branch"], row["brief_sha256"],
                "started", False, at,
                row.get("message", False), row.get("notice_key"),
                fallback=row.get("fallback"), relay_sha256=row.get("relay_sha256"),
                delegated=row.get("delegated", False), origin=row.get("origin"),
            ))
        elif thread_id in threads:
            thread = threads[thread_id]
            if row["kind"] == "fallback":
                threads[thread_id] = replace(thread, fallback_status=thread.fallback_status or row["status"])
            else:
                categories = thread.noticed_categories
                if thread.delegated:
                    category = work_category(row["status"], row["waiting_on_owner"])
                    if category != "running":
                        categories = categories | {category}
                threads[thread_id] = replace(
                    thread, status=row["status"], waiting_on_owner=row["waiting_on_owner"], updated_at=at,
                    settled_once=thread.settled_once or row["status"] in _FINAL_STATUSES,
                    noticed_categories=categories,
                )
    return threads


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.recovery_path = self.path.with_name("message-starts.jsonl")
        self._unpersisted = {}

    def _read_file(self, path):
        try:
            with _directory(path) as directory, _file(directory, path.name, os.O_RDONLY) as fd:
                fcntl.flock(fd, fcntl.LOCK_SH)
                return _read(fd, _LEDGER_LIMIT)
        except FileNotFoundError:
            return b""
        except OSError:
            raise WorkError("Work ledger could not be read safely.") from None

    def _starts_to_recover(self):
        return self._read_file(self.recovery_path), self._unpersisted.copy()

    def _read(self):
        recovered, pending = self._starts_to_recover()
        return _fold(chain(io.BytesIO(recovered), pending.values(), io.BytesIO(self._read_file(self.path))))

    def _append(self, row, *, path=None):
        _event(row)
        raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        starting = row["kind"] == "start"
        path = self.path if path is None else path
        recovered, pending = (b"", {}) if starting else self._starts_to_recover()
        flags = os.O_RDWR | os.O_APPEND | (os.O_CREAT if starting or recovered or pending else 0)
        try:
            with _directory(path, create=starting) as directory, _file(directory, path.name, flags) as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                if not starting:
                    events = chain(io.BytesIO(recovered), pending.values(), io.BytesIO(_read(fd, _LEDGER_LIMIT)))
                    if row["thread_id"] not in _fold(events):
                        raise WorkError(_UNKNOWN_THREAD)
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    # A damaged tail must not absorb the next valid event.
                    raw = b"\n" + raw
                if size + len(raw) > _LEDGER_LIMIT:
                    raise WorkError("Work ledger exceeds its size limit.")
                if os.write(fd, raw) != len(raw):
                    os.ftruncate(fd, size)
                    raise WorkError("Work ledger append was incomplete.")
                os.fsync(fd)
                if path == self.recovery_path:
                    os.fsync(directory)
        except FileNotFoundError:
            if not starting:
                raise WorkError(_UNKNOWN_THREAD) from None
            raise WorkError("Work ledger could not be updated safely.") from None
        except OSError:
            raise WorkError("Work ledger could not be updated safely.") from None

    @contextmanager
    def _delivery_lock(self, name):
        with _directory(self.path, create=True) as directory:
            with _file(directory, name, os.O_RDWR | os.O_CREAT) as fd:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield

    def message_delivery(self):
        return self._delivery_lock("messages.lock")

    def notice_delivery(self):
        return self._delivery_lock("notices.lock")

    def delegation(self):
        return self._delivery_lock("delegation.lock")

    def check_delegate_day(self, at):
        day = datetime.fromtimestamp(at).date()
        start = datetime.combine(day, local_time.min).timestamp()
        end = datetime.combine(day + timedelta(days=1), local_time.min).timestamp()
        if sum(t.delegated and start <= t.started_at < end for t in self._read().values()) >= DELEGATE_DAILY_LIMIT:
            raise WorkError(DELEGATE_DAY_ERROR)

    def record_start(self, *, message=False, notice_key=None, **fields) -> None:
        if message:
            fields["message"] = True
        if notice_key is not None:
            fields["notice_key"] = notice_key
        self._append(dict(v=1, kind="start", **fields))

    def recover_start(self, **fields) -> None:
        row = dict(v=1, kind="start", **fields)
        try:
            self._append(row, path=self.recovery_path)
        except (WorkError, OSError):
            self._unpersisted[row["thread_id"]] = json.dumps(row).encode("ascii")

    def record_fallback(self, thread_id, *, status, at) -> None:
        self._append(dict(v=1, kind="fallback", thread_id=thread_id, status=status, at=at))

    def record_status(self, thread_id, *, status, waiting_on_owner, at) -> None:
        self._append(dict(v=1, kind="status", thread_id=thread_id, status=status,
                          waiting_on_owner=waiting_on_owner, at=at))

    def threads(self, project=None) -> list[Thread]:
        threads = (thread for thread in self._read().values() if project is None or thread.project == project)
        return sorted(threads, key=lambda thread: thread.started_at, reverse=True)

    def get(self, thread_id) -> Thread:
        try:
            _checked(t3_client._arg, thread_id)
        except WorkError:
            raise WorkError(_UNKNOWN_THREAD) from None
        thread = self._read().get(thread_id)
        if thread is None:
            raise WorkError(_UNKNOWN_THREAD)
        return thread


class WorkService:
    def __init__(self, *, registry=None, ledger=None, t3=None, clock=time.time, token=secrets.token_hex):
        directory = Path(config.V3_DIR) / "work"
        self._registry = Registry(directory / "projects.json") if registry is None else registry
        self._ledger = Ledger(directory / "threads.jsonl") if ledger is None else ledger
        self._t3 = t3_client.T3() if t3 is None else t3
        self._clock = clock
        self._token = token

    def projects(self) -> list[Project]:
        return self._registry.list()

    def current_project(self, name) -> Project:
        """Return the registered project after checking its canonical directory."""
        project = self._registry.get(name)
        if _resolve_path(project.path) != project.path:
            raise WorkError("Registered project path changed.")
        return project

    def plan_start(self, project, title, brief, *, worktree=True) -> dict:
        return self._plan_start(self.current_project(project), title, brief, worktree)

    def _plan_start(self, registered, title, brief, worktree):
        title = _title(title)
        _message(brief)
        _shown_whole(brief, "brief")
        branch = None
        if registered.base is not None and worktree:
            suffix = self._token(2)
            if not _valid_suffix(suffix):
                raise WorkError("Could not generate a work branch.")
            branch = f"jarvis/{_slug(title)}-{suffix}"
        return dict(project=registered.name, path=registered.path, provider=registered.provider,
                    model=registered.model, title=title, brief=brief, base=registered.base, branch=branch)

    def start(self, plan: dict, *, guard=None) -> Thread:
        """Start the planned thread. `guard`, when given, is called last, just
        before T3 is asked: it raises to stop a start that is no longer wanted."""
        if type(plan) is not dict or plan.keys() != _PLAN_FIELDS:
            raise WorkError("Invalid work start plan.")
        return self._start_plan(plan, self.current_project(plan["project"]), guard=guard)

    def _start_plan(self, plan, project, *, proposal=None, **kwargs):
        if any(plan[key] != getattr(project, key) for key in _PROJECT_FIELDS):
            raise WorkError("Registered project changed since the work was planned.")
        title = _title(plan["title"])
        if title != plan["title"]:
            raise WorkError("Invalid work start plan.")
        brief = plan["brief"]
        branch = plan["branch"]
        options = {}
        if branch is not None:
            prefix = f"jarvis/{_slug(title)}-"
            if (project.base is None or not isinstance(branch, str) or not branch.startswith(prefix)
                    or not _valid_suffix(branch[len(prefix):])):
                raise WorkError("Invalid work branch in start plan.")
            options = {"worktree": project.base, "branch": branch}
        prompt = None
        if proposal is not None:
            from .messages import proposal_prompt

            title = _title(proposal.subject, message=True)
            prompt = proposal_prompt(proposal, brief)
        return self._start(project, title, brief, branch, options, prompt=prompt, **kwargs)

    def _delegate_project(self, name):
        if name is None:
            return self._registry.builtin_workspace()
        names = [project.name for project in self.projects()]
        if name not in names:
            raise WorkError(f"Unknown work project. Registered projects: {', '.join(names) or 'none'}.")
        return replace(self.current_project(name), provider=_DELEGATE_PROVIDER, model=_DELEGATE_MODEL)

    def plan_delegate(self, task, project=None):
        title = _delegation_task(task)
        registered = self._delegate_project(project)
        plan = self._plan_start(registered, title, f"{DELEGATE_PROMPT}\n\n{task}", True)
        self._ledger.check_delegate_day(self._clock())
        return dict(plan, task=task, named=project is not None)

    def start_delegate(self, preview, *, origin, guard=None):
        if (type(preview) is not dict or preview.keys() != _PLAN_FIELDS | {"task", "named"}
                or type(preview["named"]) is not bool or origin not in _DELEGATE_ORIGINS):
            raise WorkError("Invalid delegation plan.")
        task = preview["task"]
        plan = {key: preview[key] for key in _PLAN_FIELDS}
        if plan["title"] != _delegation_task(task) or plan["brief"] != f"{DELEGATE_PROMPT}\n\n{task}":
            raise WorkError("Invalid delegation plan.")
        with self._ledger.delegation():
            at = self._clock()
            self._ledger.check_delegate_day(at)
            name = plan["project"] if preview["named"] else None
            project = self._delegate_project(name)
            if (plan["project"] != project.name or plan["path"] != project.path
                    or (plan["branch"] is None) != (project.base is None)):
                raise WorkError("Invalid delegation plan.")
            target = None
            if name is None:
                if guard is not None:
                    guard()
                workspace = self._registry.builtin_workspace(create=True)
                target = self._confirm_builtin_project(workspace, title="Jarvis workspace",
                                                       label="Jarvis workspace", guard=guard)
            return self._start_plan(plan, project, guard=guard, at=at, target=target,
                                    mark={"delegated": True, "origin": origin})

    def message_delivery(self):
        return self._ledger.message_delivery()

    def _message_target(self, project):
        try:
            if _resolve_path(project.path) != project.path:
                return None
        except WorkError:
            return None
        return _checked(self._t3.project_at, project.path)

    def _confirm_builtin_project(self, project, *, title, label, guard=None):
        target = _checked(self._t3.project_at, project.path)
        if target is None:
            if guard is not None:
                guard()
            _checked(self._t3.project_add, project.path, title=title)
            target = _checked(self._t3.project_at, project.path)
        if target is None:
            raise WorkError(f"T3 could not confirm the exact {label}.")
        return target

    @staticmethod
    def _message_mark(message, notice_key):
        from .messages import relay_sha256

        if notice_key is not None and not (isinstance(notice_key, str) and _SHA256.fullmatch(notice_key)):
            raise WorkError("Invalid work notice key.")
        mark = {"message": True, "relay_sha256": relay_sha256(f"Subject: {message.subject}\n\n{message.body}")}
        if notice_key is not None:
            mark["notice_key"] = notice_key
        return mark

    def start_proposal(self, message, plan, *, at, notice_key) -> Thread:
        if type(plan) is not dict or plan.keys() != _PLAN_FIELDS:
            raise WorkError("Invalid work start plan.")
        if notice_key is None:
            raise WorkError("Invalid work notice key.")
        return self._start_plan(plan, self.current_project(plan["project"]), proposal=message,
                                at=at, mark=self._message_mark(message, notice_key))

    def start_message(self, message, *, at, notice_key=None, fallback=None) -> Thread:
        from .messages import Message, thread_prompt

        mark = self._message_mark(message, notice_key)
        project, builtin = self._registry.message_project(message.project)
        target = None if builtin else self._message_target(project)
        if target is None and not builtin:
            if message.project == project.name:
                message = Message(message.subject, f"About {project.name}:\n\n{message.body}")
                mark = self._message_mark(message, notice_key)
            if project.name != _GENERAL_PROJECT:
                project, builtin = self._registry.message_project(None)
                target = None if builtin else self._message_target(project)
            if target is None and not builtin:
                project, builtin = self._registry.builtin_message_project(), True
        if builtin:
            target = self._confirm_builtin_project(project, title="Jarvis", label="Jarvis message project")
        if target is None:
            raise WorkError("T3 could not confirm the exact Jarvis message project.")

        project = replace(project, provider=_MESSAGE_PROVIDER, model=_MESSAGE_MODEL)
        if fallback is not None:
            mark["fallback"] = _fallback_fields(fallback)
        return self._start(project, _title(message.subject, message=True), thread_prompt(message), None, {},
                           at=at, target=target, mark=mark)

    def _start(self, project, title, brief, branch, options, *, guard=None, at=None, target=None, mark=None,
               prompt=None):
        raw = _message(brief)
        if prompt is not None:
            # The plan's text limit applies to the brief. Hash the full text T3 receives.
            raw = _checked(t3_client._message, prompt)
        brief_sha256 = hashlib.sha256(raw).hexdigest()
        if guard is not None:
            guard()
        run = _checked(self._t3.start, target if target is not None else project.path,
                       brief if prompt is None else prompt, provider=project.provider,
                       model=project.model, title=title, **options)
        at = self._clock() if at is None else at
        thread_id, run_id = run.thread_id, run.run_id
        if not all(isinstance(value, str) and _T3_ID.fullmatch(value) for value in (thread_id, run_id)):
            raise WorkError("T3 started work but returned an invalid thread id.")
        try:
            self._ledger.get(thread_id)
        except WorkError:
            pass
        else:
            raise WorkError("T3 returned the id of a thread Jarvis already recorded.")
        fields = dict(thread_id=thread_id, run_id=run_id, project=project.name, title=title, branch=branch,
                      brief_sha256=brief_sha256, at=at, **(mark or {}))
        try:
            self._ledger.record_start(**fields)
        except (WorkError, OSError):
            if mark is None:
                raise WorkError(f"Could not record the start of T3 thread {thread_id}.") from None
            # T3 already accepted the work. Keep the start for the cap and
            # watcher even when the main ledger could not append the event.
            self._ledger.recover_start(**fields)
        return Thread(thread_id, project.name, title, float(at), branch, brief_sha256, "started", False,
                      float(at), **(mark or {}))

    def threads(self, project=None) -> list[Thread]:
        return self._ledger.threads(project)

    def show(self, thread_id, *, messages=4) -> t3_client.State:
        self._ledger.get(thread_id)
        state = _checked(self._t3.show, thread_id, messages=messages)
        if state.thread_id != thread_id:
            raise WorkError(_WRONG_THREAD)
        return state

    def send(self, thread_id, message, *, guard=None) -> t3_client.Run:
        self._ledger.get(thread_id)
        _message(message)
        if guard is not None:
            guard()
        run = _checked(self._t3.send, thread_id, message)
        if run.thread_id != thread_id:
            raise WorkError(_WRONG_THREAD)
        # A thread that had finished is live again: the watch job follows it.
        self._ledger.record_status(thread_id, status="running", waiting_on_owner=False, at=self._clock())
        return run
