"""Private brief store, routing and owner action state.

Wiring to real QuietHours/Push follows in B2.
Approval execution and push delivery are at most once: a crash after the logged
claim can lose the callback. A no-op default is only a durable state transition,
applied once when tick processes the deadline. Expiry calls no action.
The push transport must check quiet hours again at send time, after the durable delivery claim.
"""

from collections import Counter, deque
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from jarvis import config, untrusted
from jarvis.v2 import redact

from .index import open_directory


FEED_DIR = config.V3_DIR / "feed"
FEEDBACK_TAGS = frozenset({"too_long", "missing_context"})
_ACTIONS = {"approve": "approved", "edit": "edited", "other": "other", "not_now": "dismissed"}
_STATES = frozenset({"open", "expired", "done", *_ACTIONS.values()})
_TEXT_ANSWERS = {"edit", "other"}
_KINDS = {"decision", "news", "digest"}
_ATTACHMENTS = {"draft", "diff", "thread", None}
_AGENT_OPEN = 5
_AGENT_DAILY = 20
_RECENT_LIMIT = 20
_LOG = "briefs.jsonl"
_INDEX = "index.json"
_INDEX_TEMP = ".index.tmp"
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_POST_FIELDS = ("kind", "rank", "ask")
_OPTIONAL_FIELDS = ("due", "why_now", "recommend", "options", "default", "context",
                    "attachment", "proposal_id", "time_critical")
# Default-ignorable marks and fillers that are outside Unicode category Cf.
_INVISIBLE = re.compile(
    r"[\ud800-\udfff\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164"
    r"\ufe00-\ufe0f\uffa0\ufff0-\ufff8\U000e0000-\U000e0fff]"
)
# The shared display scrub treats percent signs in URL passwords as possible
# placeholders. Withhold passwords containing a percent-encoded byte here.
_ENCODED_URL_PASSWORD = re.compile(
    r"((?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@'\"<>`]*:)"
    r"[^\s/?#@'\"<>`]*%[0-9a-fA-F]{2}[^\s/?#@'\"<>`]*(?=@)"
)


class FeedError(ValueError):
    """A brief operation was refused or could not complete."""


class BriefNotFound(FeedError):
    """The requested brief ID is absent or malformed."""


class FeedBusy(FeedError):
    """The feed lock could not be acquired before the deadline."""


def _text(value, limit, *, empty=False):
    def valid(text):
        return (isinstance(text, str) and len(text) <= limit and (empty or text.strip())
                and not _INVISIBLE.search(text) and not any(map(untrusted.unseen, text)))

    if not valid(value):
        raise FeedError("A brief text field is invalid or too long.")
    clean = _ENCODED_URL_PASSWORD.sub(r"\1***", value)
    clean = redact.for_display(clean)
    if not valid(clean):
        raise FeedError("A brief text field is invalid or too long after redaction.")
    return clean


def _ref(value):
    clean = _text(value, 256)
    if clean != value:
        raise FeedError("A brief identifier contains sensitive material.")
    return clean


def _instant(value):
    try:
        if isinstance(value, datetime) and value.utcoffset() is not None:
            return value.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    raise FeedError("A brief time must have a timezone and a representable UTC instant.")


def _parse_time(value):
    try:
        return _instant(datetime.fromisoformat(value))
    except (TypeError, ValueError, OverflowError):
        raise FeedError("A brief time must be an ISO datetime with a timezone.") from None


def _date_field(value, *, replay):
    if value is None:
        return None
    clean = _text(value, 64)
    _parse_time(clean)
    if not replay:
        # fromisoformat accepts any single character as the separator. Keep
        # older records readable while requiring ISO dates in new posts.
        try:
            date.fromisoformat(clean.partition("T")[0])
        except ValueError:
            raise FeedError("A brief time must be an ISO datetime with a timezone.") from None
    return clean


def _shape(value, required, optional=()):
    if (not isinstance(value, dict) or not value.keys() >= set(required)
            or value.keys() - set(required) - set(optional)):
        raise FeedError("A brief object has missing or unexpected fields.")


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise FeedError("A brief field has an unsupported value.")
    return value


def _items(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise FeedError("A brief list is invalid or has too many items.")
    return value


def _fields(fields, *, replay=False):
    _shape(fields, _POST_FIELDS, _OPTIONAL_FIELDS)
    kind = _enum(fields["kind"], _KINDS)
    rank = fields["rank"]
    if type(rank) is not int or not 1 <= rank <= 8:
        raise FeedError("A brief rank must be an integer from 1 to 8.")
    critical = fields.get("time_critical", False)
    if type(critical) is not bool:
        raise FeedError("A brief's time_critical flag must be a boolean.")
    result = {
        "kind": kind, "rank": rank, "time_critical": critical,
        "ask": _text(fields["ask"], 120), "due": _date_field(fields.get("due"), replay=replay),
        "why_now": _text(fields.get("why_now", ""), 200, empty=True),
        "options": [], "context": [], "recommend": None, "default": None,
        "attachment": None, "proposal_id": None,
    }
    for key, limit, extra in (("options", 3, None), ("context", 32, "ref")):
        for item in _items(fields.get(key, []), limit):
            _shape(item, ("label", extra) if extra else ("label",))
            entry = {"label": _text(item["label"], 120)}
            if extra:
                entry[extra] = _text(item[extra], 2048)
            result[key].append(entry)
    recommend = fields.get("recommend")
    if recommend is not None:
        _shape(recommend, ("label", "because"))
        result["recommend"] = {"label": _text(recommend["label"], 120),
                               "because": _text(recommend["because"], 200)}
    default = fields.get("default")
    if default is not None:
        _shape(default, ("action", "at"))
        action = _text(default["action"], 120)
        # Redaction must never turn supplied text into the reserved action.
        if action == "none" and default["action"] != "none":
            raise FeedError("Invalid brief default.")
        result["default"] = {"action": action, "at": _date_field(default["at"], replay=replay)}
    elif kind == "decision":
        raise FeedError("A decision brief requires a default.")
    attachment = fields.get("attachment")
    if attachment is not None:
        _shape(attachment, ("type", "ref"))
        attachment_type = attachment["type"]
        if attachment_type is not None:
            _enum(attachment_type, _ATTACHMENTS)
        result["attachment"] = {"type": attachment_type, "ref": _text(attachment["ref"], 2048)}
    if fields.get("proposal_id") is not None:
        result["proposal_id"] = _ref(fields["proposal_id"])
    return result


def _provenance(fields, actor, session):
    _enum(actor, {"jarvis", "agent"})
    if actor == "agent":
        if fields["rank"] < 3 or fields["time_critical"] or fields["proposal_id"] is not None:
            raise FeedError("Agent briefs must use rank 3–8 and stay in the batch feed.")
        return _ref(session)
    if session is not None:
        raise FeedError("Only agent briefs carry a session.")
    return None


def _answer(action, text):
    _enum(action, _ACTIONS)
    if action in _TEXT_ANSWERS:
        text = _text(text, 2000)
    elif text is not None:
        raise FeedError("Only edit and other accept answer text.")
    return {"action": action, "text": text}


def _deadline(brief):
    default = brief["default"]
    if brief["kind"] == "decision" and default["at"] is not None:
        return _parse_time(default["at"])
    return None


def _route(fields, now, *, system_local):
    if fields["rank"] > 2:
        return "batch"
    if fields["time_critical"]:
        return "push"
    due = fields["due"]
    # astimezone(None) uses the system offset at the due instant. The default
    # clock's tzinfo only contains the offset at the posting instant.
    zone = None if system_local else now.tzinfo
    today = due is not None and _parse_time(due).astimezone(zone).date() == now.date()
    return "push" if today else "batch"


@contextmanager
def _private_file(directory, name, flags, *, readonly=False):
    fd = os.open(name, flags | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise FeedError("Feed files must be private regular files.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            if readonly:
                raise FeedError("Feed files must be private regular files.")
            os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


def _encode(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("ascii")


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise FeedError("A brief record has duplicate keys.")
    return result


def _replace_index(directory, raw):
    # The directory lock also protects the temp name. Unlink any interrupted write.
    with suppress(FileNotFoundError):
        os.unlink(_INDEX_TEMP, dir_fd=directory)
    try:
        with _private_file(directory, _INDEX_TEMP, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
            _write(fd, raw)
            os.fsync(fd)
        os.replace(_INDEX_TEMP, _INDEX, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(_INDEX_TEMP, dir_fd=directory)


def _write(fd, raw):
    remaining = memoryview(raw)
    while remaining:
        remaining = remaining[os.write(fd, remaining):]


def _id(value):
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{32}", value) is None:
        raise BriefNotFound("Brief not found.")
    return value


@dataclass
class _Row:
    brief: dict
    created: datetime
    deadline: datetime | None
    pending: bool


@dataclass
class _Projection:
    rows: dict[str, _Row] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    open_rows: dict[str, _Row] = field(default_factory=dict)
    recent: deque[_Row] = field(default_factory=lambda: deque(maxlen=_RECENT_LIMIT))
    position: int = 0
    stamp: tuple | None = None

    def row(self, bid):
        row = self.rows.get(_id(bid))
        if row is None:
            raise BriefNotFound("Brief not found.")
        return row

    def transition(self, row, state, resolved_by, answer=None):
        self.counts[row.brief["state"]] -= 1
        self.counts[state] += 1
        row.brief.update(state=state, resolved_by=resolved_by, answer=answer)
        row.pending = False
        del self.open_rows[row.brief["id"]]
        self.recent.appendleft(row)

    def apply(self, event):
        _shape(event, ("event", "id"), ("brief", "pending", "answer", "tag", "at"))
        action = event["event"]
        bid = _id(event["id"])
        if action == "post":
            _shape(event, ("event", "id", "brief", "pending"))
            # Validate the entire row before admitting any part of the event.
            raw = event["brief"]
            _shape(raw, (*_POST_FIELDS, "created", "route", "actor", "session"), _OPTIONAL_FIELDS)
            values = raw.copy()
            created = _text(values.pop("created"), 64)
            actor = values.pop("actor")
            session = values.pop("session")
            route = _enum(values.pop("route"), {"push", "batch"})
            values = _fields(values, replay=True)
            session = _provenance(values, actor, session)
            pending = event["pending"]
            # Replay the route chosen in the original clock's zone. The
            # created timestamp preserves its offset, which can differ from
            # the offset at due time on a DST transition day.
            if (bid in self.rows or type(pending) is not bool
                    or (pending and route != "push")
                    or (route == "push" and (values["rank"] > 2
                        or not (values["time_critical"] or values["due"] is not None)))
                    or (route == "batch" and values["rank"] <= 2 and values["time_critical"])):
                raise FeedError("Invalid brief post record.")
            values.update(id=bid, created=created, actor=actor, session=session, route=route,
                          state="open", feedback=[], resolved_by=None, answer=None)
            self.rows[bid] = self.open_rows[bid] = _Row(values, _parse_time(created), _deadline(values), pending)
            self.counts["open"] += 1
            return
        row = self.row(bid)
        if action == "feedback":
            _shape(event, ("event", "id", "tag"))
            tag = _enum(event["tag"], FEEDBACK_TAGS)
            tags = row.brief["feedback"]
            if tag not in tags:
                tags.append(tag)
            return
        if row.brief["state"] != "open":
            raise FeedError("The brief is already closed.")
        if action == "answer":
            _shape(event, ("event", "id", "answer"))
            answer = event["answer"]
            _shape(answer, ("action", "text"))
            answer = _answer(answer["action"], answer["text"])
            self.transition(row, _ACTIONS[answer["action"]], "owner", answer)
        elif action == "expire":
            _shape(event, ("event", "id", "at"))
            if row.deadline is None or _parse_time(_text(event["at"], 64)) <= row.deadline:
                raise FeedError("The brief's default is not due.")
            noop = row.brief["default"]["action"] == "none"
            self.transition(row, "done" if noop else "expired", "default" if noop else "expiry")
        elif action == "notify":
            _shape(event, ("event", "id"))
            if not row.pending:
                raise FeedError("The brief has no pending push.")
            row.pending = False
        else:
            raise FeedError("Unknown brief event.")


class Feed:
    def __init__(self, root=None, *, clock=None, quiet=None, push=None, execute=None,
                 lock_timeout=None):
        """Wait up to lock_timeout seconds per acquisition, or indefinitely by default."""
        self.root = Path(FEED_DIR if root is None else root).expanduser().absolute()
        self._system_local = clock is None
        self._clock = clock if clock is not None else lambda: datetime.now().astimezone()
        self._quiet = quiet
        self._push = push
        self._execute = execute
        self._lock_timeout = lock_timeout
        self._projection = _Projection()

    def _acquire_lock(self, directory):
        if self._lock_timeout is None:
            fcntl.flock(directory, fcntl.LOCK_EX)
            return
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FeedBusy("Feed is busy.") from None
                time.sleep(min(0.05, remaining))

    @contextmanager
    def _locked(self, *, readonly=False):
        # Open a fresh directory description so flock also serializes threads
        # using the same Feed instance. All I/O is relative to this pinned fd.
        with open_directory(self.root, create=not readonly) as directory:
            self._acquire_lock(directory)
            info = os.fstat(directory)
            if info.st_uid != os.getuid():
                raise FeedError("The feed directory must belong to its owner.")
            if stat.S_IMODE(info.st_mode) != 0o700:
                if readonly:
                    raise FeedError("The feed directory must be private.")
                os.fchmod(directory, 0o700)
            flags = os.O_RDONLY if readonly else os.O_RDWR | os.O_APPEND | os.O_CREAT
            with _private_file(directory, _LOG, flags, readonly=readonly) as log:
                state = self._refresh(log)
                # Check the index before changes, including when it is a link.
                if not readonly:
                    self._sync_index(directory, state)
                yield directory, log, state

    def _refresh(self, log):
        info = os.fstat(log)
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        state = self._projection
        if state.stamp == stamp:
            return state
        # The store only appends. An inode replacement or an observed rewrite
        # at the same size requires a full replay.
        if state.stamp is None or stamp[:2] != state.stamp[:2] or stamp[2] <= state.stamp[2]:
            state = _Projection()
        self._projection = _Projection()
        with os.fdopen(log, "rb", closefd=False) as stream:
            stream.seek(state.position)
            for line in stream:
                # A torn record is never committed by a later append.
                if not line.endswith(b"\n"):
                    break
                state.position += len(line)
                try:
                    state.apply(json.loads(line.decode("utf-8"), object_pairs_hook=_unique_keys))
                except (ValueError, RecursionError):
                    continue
        state.stamp = stamp
        self._projection = state
        return state

    @staticmethod
    def _sync_index(directory, state):
        # The small index is a derived summary. It never authorizes actions:
        # a corrupt or stale index cannot reopen an already answered brief.
        expected = _encode({"version": 1, "log_bytes": state.position,
                            "counts": {key: state.counts[key] for key in sorted(_STATES)}})
        try:
            with _private_file(directory, _INDEX, os.O_RDONLY) as fd:
                if os.read(fd, len(expected) + 1) == expected:
                    return
        except FileNotFoundError:
            pass
        # The log already owns the state. A failed cache write must not
        # suppress a callback for an action the log has committed.
        with suppress(OSError):
            _replace_index(directory, expected)

    def _record(self, directory, log, events):
        if not events:
            return
        raw = b"".join(map(_encode, events))
        size = os.fstat(log).st_size
        if size and os.pread(log, 1, size - 1) != b"\n":
            raw = b"!\n" + raw
        _write(log, raw)
        os.fsync(log)
        if events[0]["event"] == "answer":
            # An answer appends to the existing brief log. The file sync
            # commits it, even if syncing the directory or index fails.
            state = self._refresh(log)
            with suppress(OSError, FeedError):
                os.fsync(directory)
                self._sync_index(directory, state)
            return
        os.fsync(directory)
        self._sync_index(directory, self._refresh(log))

    def _notify(self, payload):
        if payload is not None and self._push is not None:
            try:
                self._push(*payload)
            except Exception:
                pass

    def post(self, fields, *, actor="jarvis", session=None):
        values = _fields(fields)
        session = _provenance(values, actor, session)
        payload = None
        with self._locked() as (directory, log, state):
            now = self._clock()
            instant = _instant(now)
            if actor == "agent":
                self._check_agent_caps(state, session, instant)
            route = _route(values, now, system_local=self._system_local)
            bid = uuid4().hex
            values.update(created=now.isoformat(), actor=actor, session=session, route=route)
            self._record(directory, log, [{"event": "post", "id": bid, "brief": values,
                                           "pending": route == "push"}])
            result = deepcopy(self._projection.rows[bid].brief)
            if route == "push" and (self._quiet is None or not self._quiet.is_quiet(self._clock())):
                try:
                    self._record(directory, log, [{"event": "notify", "id": bid}])
                except OSError:
                    # The post is durable. An unconfirmed claim cannot send.
                    return result
                ranks = [row.brief["rank"] for row in self._projection.rows.values()
                         if row.brief["state"] == "open" and row.brief["route"] == "push"]
                payload = len(ranks), min(ranks)
        self._notify(payload)
        return result

    @staticmethod
    def _check_agent_caps(state, session, now):
        recent = opened = 0
        for row in state.rows.values():
            brief = row.brief
            if brief["actor"] == "agent":
                # Include future timestamps after a backward clock adjustment.
                recent += now - row.created < timedelta(days=1)
                opened += brief["session"] == session and brief["state"] == "open"
        if opened >= _AGENT_OPEN or recent >= _AGENT_DAILY:
            raise FeedError("Agent brief capacity has been reached.")

    def get(self, bid):
        with self._locked() as (_, _, state):
            return deepcopy(state.row(bid).brief)

    @staticmethod
    def _sorted(rows):
        return [deepcopy(row.brief) for row in sorted(rows, key=lambda row: (row.brief["rank"], row.created))]

    def briefs(self, state="open"):
        _enum(state, _STATES)
        with self._locked() as (_, _, snapshot):
            return self._sorted(row for row in snapshot.rows.values() if row.brief["state"] == state)

    def snapshot(self):
        """Copy the open queue and latest resolutions from one locked view."""
        try:
            with self._locked(readonly=True) as (_, _, state):
                return {
                    "briefs": self._sorted(state.open_rows.values()),
                    "recent": [deepcopy(row.brief) for row in state.recent],
                }
        except FileNotFoundError:
            return {"briefs": [], "recent": []}

    def batch(self):
        with self._locked() as (_, _, state):
            return self._sorted(row for row in state.rows.values()
                                if row.brief["state"] == "open" and row.brief["route"] == "batch")

    def answer(self, bid, action, *, text=None, return_brief=False):
        answer = _answer(action, text)
        with self._locked() as (directory, log, state):
            brief = state.row(bid).brief
            if brief["state"] != "open":
                raise FeedError("The brief is already closed.")
            proposal = brief["proposal_id"] if action == "approve" else None
            self._record(directory, log, [{"event": "answer", "id": bid, "answer": answer}])
            result = deepcopy(self._projection.rows[bid].brief) if return_brief else None
        if proposal is not None and self._execute is not None:
            try:
                self._execute(proposal)
            except Exception:
                pass
            else:
                return result
            raise FeedError("The approval was recorded but proposal execution failed.")
        return result

    def feedback(self, bid, tag, *, return_brief=False):
        _enum(tag, FEEDBACK_TAGS)
        with self._locked() as (directory, log, state):
            if tag not in state.row(bid).brief["feedback"]:
                self._record(directory, log, [{"event": "feedback", "id": bid, "tag": tag}])
            if return_brief:
                return deepcopy(self._projection.rows[bid].brief)

    def tick(self, now=None):
        """Process expiries and claim one combined push for deferred open briefs."""
        payload = None
        result = {"pushed": 0, "expired": [], "defaulted": []}
        with self._locked() as (directory, log, state):
            now = self._clock() if now is None else now
            instant = _instant(now)
            events = []
            waiting = []
            for bid, row in state.rows.items():
                if row.brief["state"] != "open":
                    continue
                if row.deadline is not None and instant > row.deadline:
                    events.append({"event": "expire", "id": bid, "at": instant.isoformat()})
                    key = "defaulted" if row.brief["default"]["action"] == "none" else "expired"
                    result[key].append(bid)
                elif row.pending:
                    waiting.append(row)
            if waiting and self._push is not None and (self._quiet is None or not self._quiet.is_quiet(now)):
                events.extend({"event": "notify", "id": row.brief["id"]} for row in waiting)
                payload = len(waiting), min(row.brief["rank"] for row in waiting)
                result["pushed"] = 1
            self._record(directory, log, events)
        self._notify(payload)
        return result
