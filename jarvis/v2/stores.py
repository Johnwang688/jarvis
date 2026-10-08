"""Small disk stores; unused conversations stay in memory until first use.

Reads return detached objects. Lists include only saved objects, and missing
ids return None. The lock serializes writers within the daemon process;
atomic replacement protects readers from partially written JSON files.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import types
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar, Union, get_args, get_origin, get_type_hints

from jarvis import config
from . import model

T = TypeVar("T", model.Project, model.Thread, model.Task)
_lock = threading.RLock()


class StoreError(Exception):
    """A store operation failed without substituting incomplete data."""


class ProjectArchived(StoreError):
    """New work was aimed at an archived project (decisions B1).

    Raised by `TaskStore` itself, under the store lock, so every way a task is
    made — `POST /tasks`, Discord intake, a fast-path proposal, a schedule —
    meets the same refusal, and an archive cannot land between the check and
    the write: archiving saves the project under this same lock.
    """


def _validate(value: Any, expected: Any) -> None:
    """Dataclass constructors do not validate annotations on decoded JSON."""
    origin, args = get_origin(expected), get_args(expected)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                _validate(value, option)
                return
            except ValueError:
                pass
    elif origin is list and isinstance(value, list):
        for item in value:
            _validate(item, args[0])
        return
    elif origin is dict and isinstance(value, dict):
        for key, item in value.items():
            _validate(key, args[0])
            _validate(item, args[1])
        return
    elif is_dataclass(expected) and isinstance(value, expected):
        hints = get_type_hints(expected)
        for field in fields(expected):
            _validate(getattr(value, field.name), hints[field.name])
        return
    elif origin is None:
        if expected is float and type(value) in (int, float) and math.isfinite(value):
            return
        if expected is not float and isinstance(value, expected):
            if expected is not int or type(value) is int:
                return
    raise ValueError(f"expected {expected}, got {type(value).__name__}")


def _clone(cls: type[T], data: dict[str, Any], path: Path) -> T:
    try:
        obj = model.from_json(cls, data)
        _validate(obj, cls)
        return obj
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise StoreError(f"{path}: {exc}") from exc


def _write_bytes(path: Path, data: bytes) -> None:
    """Use a sibling temp file so replacement stays on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        raise StoreError(f"{path}: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise StoreError(f"{path}: {exc}") from exc
    result = []
    for number, line in enumerate(lines, 1):
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("expected a JSON object")
            result.append(item)
        except ValueError as exc:
            raise StoreError(f"{path}:{number}: {exc}") from exc
    return result


class _Store(Generic[T]):
    def __init__(self, stores: Stores, kind: str, cls: type[T], filename: str = ""):
        self.stores = stores
        self.root = stores.root / kind
        self.cls = cls
        self.filename = filename
        self._pending: dict[str, dict[str, Any]] = {}

    def path(self, object_id: str) -> Path:
        if not isinstance(object_id, str) or not re.fullmatch(r"[0-9a-f]{8}", object_id):
            raise StoreError(f"Invalid {self.cls.__name__} id: {object_id!r}")
        return (self.root / object_id / self.filename if self.filename
                else self.root / f"{object_id}.json")

    def _create(self, **values: Any) -> T:
        if "id" in values:
            raise StoreError("Ids are minted by the store")
        with _lock:
            while True:
                object_id = secrets.token_hex(4)
                path = self.path(object_id)
                occupied = path.parent if self.filename else path
                if object_id not in self._pending and not occupied.exists():
                    break
            obj = _clone(self.cls, {"id": object_id, **values}, path)
            self._pending[object_id] = model.to_json(obj)
            return obj

    def get(self, object_id: str) -> T | None:
        path = self.path(object_id)
        with _lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = self._pending.get(object_id)
                return None if data is None else _clone(self.cls, data, path)
            except (OSError, ValueError, UnicodeError) as exc:
                raise StoreError(f"{path}: {exc}") from exc
            obj = _clone(self.cls, data, path)
            if obj.id != object_id:
                raise StoreError(f"{path}: id does not match filename")
            return obj

    def _require(self, object_id: str) -> T:
        obj = self.get(object_id)
        if obj is None:
            raise StoreError(f"{self.path(object_id)}: not found")
        return obj

    def list(self, **filters: Any) -> list[T]:
        unknown = filters.keys() - {field.name for field in fields(self.cls)}
        if unknown:
            raise StoreError(f"Unknown {self.cls.__name__} filters: {sorted(unknown)}")
        pattern = f"*/{self.filename}" if self.filename else "*.json"
        with _lock:
            objects = [self._require(p.parent.name if self.filename else p.stem)
                       for p in sorted(self.root.glob(pattern))]
        return [obj for obj in objects
                if all(getattr(obj, key) == value for key, value in filters.items())]

    def ids(self) -> list[str]:
        """Every saved id, without reading a record: a caller that must not
        stop at one unreadable file (the Discord reconcile) reads each with
        `get` and skips the ones that raise. `list` stays strict."""
        pattern = f"*/{self.filename}" if self.filename else "*.json"
        with _lock:
            return [p.parent.name if self.filename else p.stem
                    for p in sorted(self.root.glob(pattern))]

    def _write(self, obj: T) -> None:
        path = self.path(obj.id)
        try:
            _validate(obj, self.cls)
            data = model.to_json(obj)
            _clone(self.cls, data, path)
            encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise StoreError(f"{path}: {exc}") from exc
        _write_bytes(path, encoded)
        self._pending.pop(obj.id, None)

    def save(self, obj: T) -> None:
        with _lock:
            self._write(obj)

    def delete(self, object_id: str) -> None:
        raise StoreError("Projects cannot be deleted")

    def _delete(self, object_id: str) -> None:
        directory = self.path(object_id).parent
        if directory.exists():
            shutil.rmtree(directory)
        self._pending.pop(object_id, None)

    def _append(self, object_id: str, filename: str, entry: dict[str, Any]) -> None:
        path = self.path(object_id).with_name(filename)
        try:
            line = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
            with _lock:
                obj = self._require(object_id)
                if not self.path(object_id).exists():
                    self.save(obj)
                # A migrated log may legitimately lack a final newline.
                with path.open("a+b") as handle:
                    if handle.tell():
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            handle.write(b"\n")
                    handle.write(line.encode("utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise StoreError(f"{path}: {exc}") from exc


class ProjectStore(_Store[model.Project]):
    def create(self, name: str, root: str, **values: Any) -> model.Project:
        with _lock:
            obj = self._create(name=name, root=str(root), **values)
            try:
                self.save(obj)
            except Exception:
                self._pending.pop(obj.id, None)
                raise
            return obj

    def save(self, obj: model.Project) -> None:
        with _lock:
            previous = self.get(obj.id)
            if previous and previous.inbox and not obj.inbox:
                raise StoreError("The inbox cannot become an ordinary project")
            if obj.inbox:
                if obj.name != "Inbox" or obj.root != str(Path.home()):
                    raise StoreError("The inbox must be named Inbox and rooted at the owner's home")
                if any(p.id != obj.id for p in self.list(inbox=True)):
                    raise StoreError("The inbox project already exists")
            super().save(obj)

    def inbox(self) -> model.Project:
        with _lock:
            found = self.list(inbox=True)
            if len(found) > 1:
                raise StoreError(f"{self.root}: multiple inbox projects")
            return found[0] if found else self.create("Inbox", str(Path.home()), inbox=True)


class ThreadStore(_Store[model.Thread]):
    def create(self, project_id: str, role: model.Role, provider: model.ProviderName,
               **values: Any) -> model.Thread:
        return self._create(project_id=project_id, role=role, provider=provider, **values)

    def delete(self, object_id: str) -> None:
        with _lock:
            obj = self._require(object_id)
            tasks = self.stores.tasks
            linked = tasks.list() + [tasks._require(key) for key in tasks._pending]
            if obj.task_id is not None or any(obj.id in task.thread_ids for task in linked):
                raise StoreError(f"Thread {object_id} belongs to a task")
            self._delete(object_id)

    def log(self, thread_id: str, role: str, text: str) -> None:
        self._append(thread_id, "log.jsonl", {"t": time.time(), "role": role, "text": text})

    def read_log(self, thread_id: str) -> list[dict[str, Any]]:
        self._require(thread_id)
        return _read_lines(self.path(thread_id).with_name("log.jsonl"))


class TaskStore(_Store[model.Task]):
    def create(self, project_id: str, brief: str, **values: Any) -> model.Task:
        if "state" in values and values["state"] != model.TaskState.INTAKE:
            raise StoreError("New tasks must start in intake; use transition()")
        with _lock:
            self._refuse_archived(project_id)
            return self._create(project_id=project_id, brief=brief, **values)

    def _refuse_archived(self, project_id: str) -> None:
        """No new task in an archived project. Checked at create and again at
        the first save, both under the store lock: a task minted just before
        its project was archived is still refused when it is written."""
        project = self.stores.projects.get(project_id)
        if project is not None and project.archived:
            raise ProjectArchived(f"project {project.name} is archived; "
                                  "restore it from the Archive first")

    def save(self, obj: model.Task) -> None:
        with _lock:
            previous = self.get(obj.id)
            if previous is None or obj.id in self._pending:
                self._refuse_archived(obj.project_id)
            expected = previous.state if previous else model.TaskState.INTAKE
            if obj.state != expected:
                raise StoreError(f"Task {obj.id}: state changes require transition()")
            if previous is not None and previous.discord_thread_id and not obj.discord_thread_id:
                # Write-once (bug 1). A caller holding a copy read before the
                # Discord poster made the thread — `worktrees.ensure` across
                # `git worktree add`, any runner save — must not wipe its id,
                # or the next snapshot makes a second thread for the task.
                obj.discord_thread_id = previous.discord_thread_id
            super().save(obj)

    def transition(self, task_id: str, new_state: model.TaskState, *, reason: str = "") -> model.Task:
        with _lock:
            task = self._require(task_id)
            try:
                new_state = model.TaskState(new_state)
            except ValueError as exc:
                raise StoreError(f"Task {task_id}: unknown state {new_state!r}") from exc
            old_state = task.state
            if new_state not in model.TRANSITIONS[old_state]:
                raise StoreError(f"Task {task_id}: illegal transition {old_state.value} -> {new_state.value}")
            task.state = new_state
            task.status.phase = new_state
            now = datetime.now(timezone.utc)
            previous = datetime.fromisoformat(task.updated)
            task.updated = max(now, previous + timedelta(microseconds=1)).isoformat()
            self._write(task)
            self.journal(task_id, "transition", old_state=old_state.value,
                         new_state=new_state.value, reason=reason)
            return task

    def delete(self, object_id: str) -> None:
        with _lock:
            if self._require(object_id).state not in model.TERMINAL_STATES:
                raise StoreError(f"Task {object_id} is not in a terminal state")
            self._delete(object_id)

    def journal(self, task_id: str, event: str, **fields: Any) -> None:
        self._append(task_id, "journal.jsonl", {**fields, "at": model.utcnow(), "event": event})

    def read_journal(self, task_id: str) -> list[dict[str, Any]]:
        self._require(task_id)
        return _read_lines(self.path(task_id).with_name("journal.jsonl"))


class Stores:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else config.V2_DATA_DIR
        self.projects = ProjectStore(self, "projects", model.Project)
        self.threads = ThreadStore(self, "threads", model.Thread, "thread.json")
        self.tasks = TaskStore(self, "tasks", model.Task, "task.json")
