"""Private proposal records and durable state transitions."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config

from ._paths import open_directory
from .feed import Feed, _fields, _parse_time
from .policy import LEVELS, Level, Policy


CAPS = {"day": 10, "run": 3, "project": 3}
_CHICAGO = ZoneInfo("America/Chicago")
_DISMISSED_HOLD = timedelta(days=30)
_LIFETIME = timedelta(hours=72)
_LOG = "proposals.jsonl"
_ID = re.compile(r"[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")
_HELD = {"briefed", "approved", "running"}
_TAINT_ROW = {"label": "Based on untrusted text", "ref": "jarvis:tainted"}
_TOOLS = {
    "thread.start": "work_start",
    "thread.send": "work_send",
    "calendar.block": "calendar_create",
    "vault.update": "vault_add",
}
_TRANSITIONS = {
    "new": ("briefed",),
    "briefed": ("approved", "dismissed", "edited", "expired"),
    "approved": ("running",),
    "running": ("done", "failed", "unknown", "approved"),
}


class ProposalError(ValueError):
    """A proposal operation was refused or could not complete."""


class Action(TypedDict):
    tool: str
    args: dict
    preview: dict


@dataclass(frozen=True)
class Draft:
    # The payload fields stay out of repr: a draft in a log line or a traceback
    # must not carry brief text, action text or argument values.
    kind: str
    level_proposed: Level
    basis: list[dict[str, str]] = field(repr=False)
    brief: dict = field(repr=False)
    action: Action | None = field(repr=False)
    dedupe_key: str = field(repr=False)
    project: str | None
    tainted: bool
    revision_of: str | None = None


class Proposal(TypedDict):
    v: int
    id: str
    created: str
    kind: str
    level_proposed: Level
    level: Level
    why: str
    tainted: bool
    basis: list[dict[str, str]]
    action: Action | None
    action_sha256: str
    brief: dict
    brief_id: str | None
    dedupe_key: str
    project: str | None
    run: str | None
    state: str
    revision_of: str | None
    result: object


def action_sha256(proposal: Proposal) -> str:
    """Bind the proposal identity and the exact action shown to the owner."""
    try:
        action = proposal["action"]
        fields = {key: proposal[key] for key in ("v", "id", "kind")}
        fields.update({key: action[key] if action is not None else None
                       for key in ("tool", "args", "preview")})
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise ProposalError("The proposal action cannot be hashed.") from None


def _valid_id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ProposalError("Duplicate proposal log key.")
    return result


def _encode(record):
    try:
        return (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ProposalError("The proposal record cannot be encoded.") from None


def _brief_fields(draft, created):
    try:
        fields = draft.brief.copy()
        fields.pop("proposal_id", None)
        if draft.action is None:
            fields.update(kind="news", default=None)
        else:
            deadline = created + _LIFETIME
            supplied = fields.get("default")
            if supplied is not None and supplied["at"] is not None:
                deadline = min(deadline, _parse_time(supplied["at"]))
            fields.update(kind="decision", default={"action": "none", "at": deadline.isoformat()})
        fields = _fields(fields)
        if draft.tainted:
            context = [row for row in fields["context"] if row != _TAINT_ROW]
            fields["context"] = [_TAINT_ROW.copy(), *context[:31]]
        return fields
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise ProposalError("Invalid proposal brief fields.") from None


def _same_target(action) -> bool:
    """The card is drawn from the preview and the action runs from the args:
    where both name the target, they must name the same one."""
    tool, args, preview = action["tool"], action["args"], action["preview"]
    if tool == "work_send":
        return preview.get("thread_id") == args.get("thread_id")
    if tool == "work_start":
        return (all(preview.get(key) == args.get(key) for key in ("project", "title", "brief"))
                and (args.get("worktree", True) or preview.get("branch") is None))
    return True


def _draft_record(draft, run, created, decision):
    if (type(draft.kind) is not str or draft.level_proposed not in LEVELS
            or type(draft.tainted) is not bool or type(draft.dedupe_key) is not str
            or not draft.dedupe_key or (draft.project is not None and type(draft.project) is not str)
            or (run is not None and (type(run) is not str or not run))
            or (draft.revision_of is not None and type(draft.revision_of) is not str)
            or type(draft.basis) is not list or any(
                type(ref) is not dict or ref.keys() != {"type", "id"}
                or ref["type"] not in ("signal", "thread", "vault", "calendar")
                or type(ref["id"]) is not str for ref in draft.basis
            )):
        raise ProposalError("Invalid proposal draft.")
    action = draft.action
    if action is not None and (
        type(action) is not dict or action.keys() != Action.__required_keys__
        or draft.kind not in _TOOLS
        or action["tool"] != _TOOLS[draft.kind]
        or type(action["args"]) is not dict or type(action["preview"]) is not dict
        or not _same_target(action)
    ):
        raise ProposalError("Invalid proposal action.")
    proposal = Proposal(v=1, id=uuid4().hex, created=created.isoformat(), kind=draft.kind,
                        level_proposed=draft.level_proposed, level=decision["level"],
                        why=decision["why"], tainted=draft.tainted, basis=draft.basis,
                        action=action, action_sha256="", brief=_brief_fields(draft, created),
                        brief_id=None, dedupe_key=draft.dedupe_key, project=draft.project,
                        run=run, state="new", revision_of=draft.revision_of, result=None)
    # Hash the JSON representation that will be read back from disk.
    proposal = json.loads(_encode(proposal), object_pairs_hook=_unique_object)
    proposal["action_sha256"] = action_sha256(proposal)
    return proposal


@dataclass
class _Snapshot:
    rows: dict[str, Proposal] = field(default_factory=dict)
    transitioned_at: dict[str, datetime] = field(default_factory=dict)
    briefed_at: dict[str, datetime] = field(default_factory=dict)
    approved_at: dict[str, datetime] = field(default_factory=dict)
    dismissed_at: dict[str, datetime] = field(default_factory=dict)

    def row(self, pid):
        if not _valid_id(pid) or pid not in self.rows:
            raise ProposalError("Proposal not found.")
        return self.rows[pid]

    def apply(self, record):
        if type(record) is not dict or not _valid_id(record.get("id")):
            raise ProposalError("Invalid proposal log record.")
        pid = record["id"]
        if record.keys() == Proposal.__required_keys__:
            if (type(record["v"]) is not int or record["v"] != 1 or pid in self.rows
                    or record["state"] != "new" or record["brief_id"] is not None
                    or record["result"] is not None or type(record["action_sha256"]) is not str
                    or _HASH.fullmatch(record["action_sha256"]) is None
                    or type(record["dedupe_key"]) is not str):
                raise ProposalError("Invalid new proposal record.")
            self.rows[pid] = record
            return
        # Transition rows have their own schema. They cannot rewrite an action
        # or its digest, and their timestamps drive the day and dismissal caps.
        if record.keys() != {"id", "state", "at", "brief_id", "result"}:
            raise ProposalError("Invalid proposal transition record.")
        row = self.row(pid)
        state = record["state"]
        if state not in _TRANSITIONS.get(row["state"], ()):
            raise ProposalError("Invalid proposal state transition.")
        at = _parse_time(record["at"])
        brief_id = record["brief_id"]
        if state == "briefed":
            if not _valid_id(brief_id):
                raise ProposalError("Invalid proposal brief identifier.")
            row["brief_id"] = brief_id
            self.briefed_at[pid] = at
        elif brief_id is not None:
            raise ProposalError("Invalid proposal transition record.")
        if state == "dismissed":
            key = row["dedupe_key"]
            self.dismissed_at[key] = max(at, self.dismissed_at.get(key, at))
        if state == "approved":
            if row["state"] == "briefed":
                self.approved_at[pid] = at
            elif at != self.approved_at[pid] or record["result"] is not None:
                raise ProposalError("Invalid released proposal claim.")
        self.transitioned_at[pid] = at
        row.update(state=state, result=record["result"])

    def briefs_today(self, now):
        today = now.astimezone(_CHICAGO).date()
        return sum(self.rows[pid]["revision_of"] is None and at.astimezone(_CHICAGO).date() == today
                   for pid, at in self.briefed_at.items())

    def held(self, key, now):
        dismissed = self.dismissed_at.get(key)
        return ((dismissed is not None and now - dismissed < _DISMISSED_HOLD)
                or any(row["dedupe_key"] == key and row["state"] in _HELD
                       for row in self.rows.values()))

    def full(self, project, now):
        if self.briefs_today(now) >= CAPS["day"]:
            return True
        return project is not None and sum(
            row["project"] == project and row["state"] == "briefed" for row in self.rows.values()
        ) >= CAPS["project"]


def _read(fd):
    snapshot = _Snapshot()
    with os.fdopen(fd, "rb", closefd=False) as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break
            if line.endswith(b"!\n"):
                continue  # A prior append sealed an uncommitted tail.
            try:
                snapshot.apply(json.loads(line, object_pairs_hook=_unique_object))
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise ProposalError("The proposal log is malformed.") from None
    return snapshot


def _append(directory, fd, record):
    raw = _encode(record)
    size = os.fstat(fd).st_size
    if size and os.pread(fd, 1, size - 1) != b"\n":
        # Mark the torn line invalid even if its JSON was complete.
        raw = b"!\n" + raw
    if os.write(fd, raw) != len(raw):
        os.ftruncate(fd, size)
        raise ProposalError("The proposal log append was incomplete.")
    os.fsync(fd)
    os.fsync(directory)


class ProposalStore:
    def __init__(self, root=None, *, clock=time.time):
        self.root = Path(Path(config.V3_DIR) / "proposals" if root is None else root).expanduser().absolute()
        self.path = self.root / _LOG
        self._clock = clock

    def _now(self):
        try:
            return datetime.fromtimestamp(self._clock(), UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            raise ProposalError("Invalid proposal clock time.") from None

    @contextmanager
    def _locked(self, *, write=False, create=False):
        with open_directory(self.root, create=create) as directory:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ProposalError("The proposal directory must be private.")
            flags = os.O_RDWR | os.O_APPEND if write else os.O_RDONLY
            if create:
                flags |= os.O_CREAT
            fd = os.open(_LOG, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ProposalError("The proposal log must be a private regular file.")
                fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                yield directory, fd, _read(fd)
            finally:
                os.close(fd)

    def _snapshot(self):
        try:
            with self._locked() as (_, _, snapshot):
                return snapshot
        except FileNotFoundError:
            return _Snapshot()
        except OSError:
            raise ProposalError("The proposal store could not be read safely.") from None

    def add(self, draft: Draft, *, run: str | None = None) -> Proposal | None:
        facts = {"kind": draft.kind, "tainted": draft.tainted}
        if draft.kind == "calendar.block":
            # The only calendar action in this version creates a fresh block
            # with no attendees, like the chat's calendar_create.
            facts.update(attendees=0, owner_event=False)
        decision = Policy().explain(facts)
        if decision["level"] == "L3":
            return None
        try:
            with self._locked(write=True, create=True) as (directory, fd, snapshot):
                now = self._now()
                if draft.revision_of is None:
                    if snapshot.held(draft.dedupe_key, now) or snapshot.full(draft.project, now):
                        return None
                    if run is not None and sum(
                        row["run"] == run and row["revision_of"] is None for row in snapshot.rows.values()
                    ) >= CAPS["run"]:
                        return None
                proposal = _draft_record(draft, run, now, decision)
                _append(directory, fd, proposal)
                return proposal
        except OSError:
            raise ProposalError("The proposal store could not be updated safely.") from None

    def brief(self, pid, feed: Feed) -> str:
        try:
            with self._locked(write=True) as (directory, fd, snapshot):
                row = snapshot.row(pid)
                if row["brief_id"] is not None:
                    return row["brief_id"]
                now = self._now()
                if row["revision_of"] is None and (
                    snapshot.held(row["dedupe_key"], now) or snapshot.full(row["project"], now)
                ):
                    raise ProposalError("The proposal cannot be briefed yet.")
                signature = action_sha256(row)
                if signature != row["action_sha256"]:
                    raise ProposalError("The proposal action hash differs.")
                ref = f"{pid}:{signature}"
                try:
                    brief = feed.post(dict(row["brief"], idempotency_key=f"proposal:{pid}",
                                           proposal_id=ref), actor="jarvis")
                    bid = brief["id"]
                    matches = _valid_id(bid) and brief["proposal_id"] == ref
                except Exception:
                    raise ProposalError("The proposal brief could not be posted.") from None
                if not matches:
                    raise ProposalError("The feed returned a different proposal brief.")
                self._transition(directory, fd, row, "briefed", now, brief_id=bid)
                return bid
        except FileNotFoundError:
            raise ProposalError("Proposal not found.") from None
        except OSError:
            raise ProposalError("The proposal store could not be updated safely.") from None

    @staticmethod
    def _transition(directory, fd, row, state, at, *, brief_id=None, result=None):
        if state not in _TRANSITIONS.get(row["state"], ()):
            raise ProposalError("Invalid proposal state transition.")
        _append(directory, fd, dict(id=row["id"], state=state, at=at.isoformat(),
                                    brief_id=brief_id, result=result))

    def claim(self, pid) -> bool:
        try:
            with self._locked(write=True) as (directory, fd, snapshot):
                row = snapshot.rows.get(pid) if _valid_id(pid) else None
                if row is None or row["state"] != "approved":
                    return False
                self._transition(directory, fd, row, "running", self._now())
                return True
        except FileNotFoundError:
            return False
        except OSError:
            raise ProposalError("The proposal claim could not be recorded safely.") from None

    def release_claim(self, pid) -> None:
        """Restore approval for a claim the executor has yet to act on.

        The caller must still hold the executor's sweep lock and know the
        action never started. Preserve the original approval's age.
        """
        try:
            with self._locked(write=True) as (directory, fd, snapshot):
                row = snapshot.row(pid)
                if row["state"] != "running":
                    raise ProposalError("Only a running proposal claim can be released.")
                self._transition(directory, fd, row, "approved", snapshot.approved_at[pid])
        except FileNotFoundError:
            raise ProposalError("Proposal not found.") from None
        except OSError:
            raise ProposalError("The proposal claim could not be released safely.") from None

    def mark(self, pid, state, *, result=None) -> None:
        if state in ("briefed", "running"):
            raise ProposalError("Use brief or claim for this proposal state.")
        try:
            with self._locked(write=True) as (directory, fd, snapshot):
                row = snapshot.row(pid)
                if row["state"] == "running" and state == "approved":
                    raise ProposalError("Use release_claim for a running proposal.")
                self._transition(directory, fd, row, state, self._now(), result=result)
        except FileNotFoundError:
            raise ProposalError("Proposal not found.") from None
        except OSError:
            raise ProposalError("The proposal state could not be recorded safely.") from None

    def get(self, pid) -> Proposal | None:
        return self._snapshot().rows.get(pid) if _valid_id(pid) else None

    def transition_at(self, pid) -> datetime | None:
        """Return the last recorded transition, if the proposal has one."""
        return self._snapshot().transitioned_at.get(pid) if _valid_id(pid) else None

    def briefs_today(self) -> int:
        return self._snapshot().briefs_today(self._now())

    def proposals(self, state=None) -> list[Proposal]:
        return [row for row in self._snapshot().rows.values() if state is None or row["state"] == state]
