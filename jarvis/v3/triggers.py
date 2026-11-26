"""Turn snapshot conditions into tainted signal records for the planner.

Signals carry titles as untrusted, redacted, bounded text. I2's state carries
digests only for free text. A removal or a replay from the checkpoint alone
has no title to include.

`project.stale` is deferred because there is no projects snapshot or activity
data. `calendar.idle_block_tomorrow` is dormant in the daemon until a calendar
adapter supplies busy intervals with `singleEvents=true`. C3's snapshot
includes recurring masters without expanding their instances and can omit
masters older than its backfill window.
"""

from base64 import urlsafe_b64encode
from collections import Counter, deque
from contextlib import contextmanager, suppress
from datetime import date, datetime, time as wall_time, timedelta, timezone
from email.utils import parseaddr
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config, untrusted
from . import signals
from ._paths import open_directory


SOURCES = ("calendar", "schoolwork", "github", "commitments", "mail")
KINDS = ("mail.needs_reply", "calendar.changed", "calendar.idle_block_tomorrow",
         "school.assignment_new", "school.due_within_72h", "gh.review_requested",
         "gh.ci_failed", "commitment.due_soon")
DEFERRED = ("project.stale",)
STALE_S = {"calendar": 7200, "schoolwork": 21600, "github": 7200}
DEDUPE_TTL_S = 30 * 86400
MAX_PER_KIND = 20
LOG_KEEP = 5000
BLOCKED_ASK = {
    "calendar": "Calendar signals are unavailable.",
    "schoolwork": "Schoolwork signals are unavailable.",
    "github": "GitHub signals are unavailable.",
    "commitments": "Commitment signals are unavailable.",
    "mail": "Mail signals are unavailable.",
}

_CHICAGO = ZoneInfo("America/Chicago")
_UTC = timezone.utc
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_DAY = 86400
_EDGE_KINDS = {"calendar.changed", "school.assignment_new"}
_SOURCE_FOR_PREFIX = {"calendar": "calendar", "school": "schoolwork", "gh": "github",
                      "commitment": "commitments", "mail": "mail"}
_EXCLUDED_LABELS = {"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
                    "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
_AUTOMATED = re.compile(
    r"^(?:noreply|no-reply|no_reply|no\.reply|donotreply|do-not-reply|do_not_reply|"
    r"mailer-daemon|postmaster|bounce|bounces)", re.IGNORECASE,
)
_C3_FIELDS = {
    "calendar": {"title": (str,), "start": (str,), "end": (str,), "status": (str,),
                 "all_day": (int,), "attendees": (int,), "owner_event": (int,),
                 "jarvis": (int,), "recurring": (int,)},
    "schoolwork": {"title": (str,), "course": (str, type(None)), "due": (str, type(None)),
                   "state": (str, type(None)), "done": (int,), "locked": (int,)},
    "github": {"repo": (str,), "number": (int,), "authored": (int,), "review_requested": (int,),
               "title": (str, type(None)), "checks": (str, type(None)),
               "review_decision": (str, type(None)), "draft": (int, type(None))},
}
_TEXT_FIELDS = {
    "calendar": ("title", "status"),
    "schoolwork": ("title", "course", "state"),
    "github": ("title", "checks", "review_decision"),
    "commitments": ("who", "what"),
    "mail": ("title", "labels", "from"),
}
_SIGNAL_TEXT_FIELDS = ("untrusted_title", "untrusted_from")
_KEY_SUFFIXES = {
    "calendar.changed": re.compile(r":[0-9a-f]{16}$"),
    "school.due_within_72h": re.compile(
        r":[0-9]{4}-[0-9]{2}-[0-9]{2}.[0-9]{2}:[0-9]{2}"
        r"(?::[0-9]{2}(?:[.,][0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
    ),
    "commitment.due_soon": re.compile(r":[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
}
_LOG = "signals.jsonl"
_STATE = "state.json"
_PENDING = "pending.json"
_STATE_LIMIT = 1024 * 1024
_STATE_ENVELOPE_RESERVE = 512  # Room for every blocked episode and the sequence cursor.
# Keys can carry a complete snapshot ID, even though summaries show a shorter label.
_ID_LIMIT = 4096
_LOG_LINE_LIMIT = 128 * 1024
_LOG_CHUNK = 64 * 1024
# The journal stores one bounded snapshot per source plus state and a single record batch.
_PENDING_LIMIT = len(SOURCES) * signals._MAX_READ + _STATE_LIMIT + len(KINDS) * MAX_PER_KIND * _LOG_LINE_LIMIT
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_HEX_ID = re.compile(r"[0-9a-f]{32}")
_RECORD_FIELDS = {"seq", "id", "kind", "source", "source_ref", "summary_fields",
                  "dedupe_key", "tainted", "created"}
_METADATA_SECRET = re.compile(
    r"""(?ix)(?<![a-z0-9])(?:pass[_ -]?(?:word|wd|phrase)|token|secret|cookie|"""
    r"""(?:access|auth|refresh|session|aws[_ -]session)[_ -]?token|"""
    r"""(?:api|private|secret|secret[_ -]access)[_ -]?key|"""
    r"""(?:client|aws[_ -]secret[_ -]access)[_ -]?secret|aws[_ -]secret[_ -]access[_ -]key)"""
    r"""["']?\s*[:=]\s*\S"""
)
_SCHEME = re.compile(r"(?<![\w+.-])[a-zA-Z][a-zA-Z0-9+.-]*://")
# A shared redaction marker can contain spaces inside an authority.
_AUTHORITY_CHAR = r"""(?:\[(?:withheld: [\w -]+|redacted)\]|[^\s/?#@\[\]<>"'])"""
_URL_USERINFO = re.compile(
    rf"""(?:{_AUTHORITY_CHAR}*@|[^\s/?#@:\[\]<>"']*:{_AUTHORITY_CHAR}*(?=$|[\s<>"']))"""
)
_HELD_ID = re.compile(r"[A-Za-z0-9_-]{22}")


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, _UTC).strftime(_STAMP)


def _instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Expected a time with a UTC offset.")
    return parsed.timestamp()


def _event_time(value, all_day):
    if all_day == 1:
        return datetime.combine(date.fromisoformat(value), wall_time(), _CHICAGO).timestamp()
    return _instant(value)


def _mask_userinfo(text):
    parts, cursor = [], 0
    for scheme in _SCHEME.finditer(text):
        if scheme.start() < cursor:
            continue
        info = _URL_USERINFO.match(text, scheme.end())
        if info is not None:
            parts.extend((text[cursor:scheme.end()], "[withheld: URL userinfo]"))
            if info[0].endswith("@"):
                parts.append("@")
            cursor = info.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _scrub_text(value, cap=_ID_LIMIT):
    from jarvis.v2.redact import LABELLED_MARKER, for_display

    # Neither the shared redactor nor the authority check sees a truncated value.
    visible = untrusted.strip_invisible(value)
    normalized = " ".join(visible.split())
    clean = for_display(normalized)
    if value != normalized:
        # The shared redactor also recognizes token boundaries in the original text.
        original_safe = for_display(value)
        if value != original_safe:
            clean = for_display(" ".join(untrusted.strip_invisible(original_safe).split()))
    clean = _mask_userinfo(signals.scrub(clean))
    bounded = untrusted.one_line(clean, cap=cap)
    folded = unicodedata.normalize("NFKC", clean)
    if _METADATA_SECRET.search(folded) or folded != clean and _mask_userinfo(folded) != folded:
        return LABELLED_MARKER
    return bounded


def _scrub_fields(fields):
    return {key: ([_scrub_text(text) for text in value] if isinstance(value, list)
                  else _scrub_text(value, signals._TITLE_CHARS if key == "title" else _ID_LIMIT)
                  if isinstance(value, str) else value)
            for key, value in fields.items()}


def _line(value):
    return _scrub_text(value or "", cap=120)


def _digest_text(value):
    if isinstance(value, list):
        return [_digest_text(text) for text in value]
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
    return value


def _digest_item(source, item):
    return {key: _digest_text(value) if key in _TEXT_FIELDS[source] else value
            for key, value in item.items()}


def _digest_snapshot(source, snapshot):
    baseline = {"version": 2, "source": source,
                "items": [_digest_item(source, item) for item in snapshot["items"]]}
    if source == "schoolwork":
        baseline["needs_sign_in"] = snapshot["needs_sign_in"]
    return baseline


def _checkpoint_record(row, *, recovering=False):
    fields = row["summary_fields"]
    # The digest is useful for the checkpoint's recorded observation only.
    # On replay, missing log rows retain their structured fields and cursor.
    return dict(row, summary_fields={
        key: ("" if recovering else _digest_text(value)) if key in _SIGNAL_TEXT_FIELDS else value
        for key, value in fields.items()
    })


def _candidate(kind, source, item_id, fields, *, suffix=""):
    item_id = _scrub_text(item_id)[:_ID_LIMIT]
    return {"kind": kind, "source_ref": f"{source}:{item_id}", "summary_fields": fields,
            "dedupe_key": f"{kind}:{item_id}{suffix}", "tainted": True}


def _calendar_changes(previous, current, now, *, previous_digested):
    if previous is None:
        return
    before = {item["id"]: item for item in previous}
    after = {item["id"]: item for item in current}
    for event in sorted(before.keys() | after.keys()):
        old, new = before.get(event), after.get(event)
        item = new if new is not None else old
        old_digest = _digest_item("calendar", old) if old is not None and not previous_digested else old
        new_digest = _digest_item("calendar", new) if new is not None else None
        if old_digest == new_digest or item["jarvis"] == 1 or _event_time(item["end"], item["all_day"]) <= now:
            continue
        if old is None:
            change = "added"
        elif new is None:
            change = "removed"
        elif any(old[field] != new[field] for field in ("start", "end", "all_day")):
            change = "moved"
        else:
            change = "updated"
        fields = {"event": _line(event), "change": change, "start": _line(item["start"]),
                  "end": _line(item["end"]), "all_day": int(item["all_day"] == 1),
                  "attendees": item["attendees"], "owner_event": int(item["owner_event"] == 1),
                  "recurring": int(item["recurring"] == 1),
                  "untrusted_title": "" if new is None and previous_digested else _line(item["title"])}
        if change == "moved":
            fields.update(previous_start=_line(old["start"]), previous_end=_line(old["end"]))
        digest = hashlib.sha256(_json([old_digest, new_digest])).hexdigest()[:16]
        yield _candidate("calendar.changed", "calendar", event, fields, suffix=f":{digest}")


def _idle_tomorrow(now, busy):
    tomorrow = datetime.fromtimestamp(now, _CHICAGO).date() + timedelta(days=1)
    start = datetime.combine(tomorrow, wall_time(9), _CHICAGO)
    end = datetime.combine(tomorrow, wall_time(21), _CHICAGO)
    first, last = start.timestamp(), end.timestamp()
    try:
        spans = []
        for left, right in busy(start, end):
            if left.utcoffset() is None or right.utcoffset() is None:
                raise ValueError("Expected aware busy intervals.")
            lo, hi = max(first, left.timestamp()), min(last, right.timestamp())
            if lo < hi:
                spans.append((lo, hi))
    except Exception:
        return
    cursor, gaps = first, []
    for lo, hi in sorted(spans) + [(last, last)]:
        if lo - cursor >= 120 * 60:
            gaps.append((cursor, lo))
        cursor = max(cursor, hi)
    if gaps:
        def hhmm(epoch):
            return datetime.fromtimestamp(epoch, _CHICAGO).strftime("%H:%M")

        day = tomorrow.isoformat()
        fields = {"date": day, "blocks": [{"start": hhmm(lo), "end": hhmm(hi)} for lo, hi in gaps[:5]],
                  "longest_min": int(max(hi - lo for lo, hi in gaps) // 60)}
        return _candidate("calendar.idle_block_tomorrow", "calendar", day, fields)


def _school(previous, current, now):
    before = None if previous is None else {item["id"] for item in previous}
    for item in current:
        if item["done"] != 0:
            continue
        aid, due = item["id"], item["due"]
        fields = {"assignment": _line(aid), "due": _line(due) if due is not None else None,
                  "locked": int(item["locked"] == 1), "untrusted_title": _line(item["title"])}
        if before is not None and aid not in before:
            yield _candidate("school.assignment_new", "schoolwork", aid, fields)
        if due is not None and now < _instant(due) <= now + 72 * 3600:
            yield _candidate("school.due_within_72h", "schoolwork", aid, fields, suffix=f":{fields['due']}")


def _github(current):
    for item in current:
        ref = item["id"]
        fields = {"repo": _line(item["repo"]), "number": item["number"],
                  "untrusted_title": _line(item["title"])}
        if item["review_requested"] == 1 and item["draft"] != 1:
            yield _candidate("gh.review_requested", "github", ref, fields)
        if item["authored"] == 1 and item["checks"] in ("failure", "error"):
            yield _candidate("gh.ci_failed", "github", ref, fields)


def _commitment_due(current, now):
    # Same local-date window as commitments.due_within, applied to the snapshot.
    today = datetime.fromtimestamp(now, _CHICAGO).date()
    last_day = today + timedelta(days=2)
    for item in current:
        due = item["due"]
        if item["state"] != "open" or due is None:
            continue
        day = date.fromisoformat(due)
        if day <= last_day:
            cid = item["id"]
            fields = {"commitment": _line(cid), "due": day.isoformat(), "direction": _line(item["direction"]),
                      "overdue": day < today, "untrusted_title": _line(item["what"])}
            yield _candidate("commitment.due_soon", "commitments", cid, fields, suffix=f":{due}")


def _mail(current, now):
    newest = {}
    for item in current:
        last = newest.get(item["thread"])
        if last is None or (item["ts"], item["id"]) > (last["ts"], last["id"]):
            newest[item["thread"]] = item
    for item in newest.values():
        labels = item["labels"]
        if (item["from_owner"] == 0 and "INBOX" in labels
                and not _EXCLUDED_LABELS.intersection(labels) and now - item["ts"] >= 3600):
            mid = item["id"]
            sender = item.get("from", "")
            local, sep, domain = parseaddr(sender)[1].rpartition("@")
            fields = {"message": _line(mid), "thread": _line(item["thread"]),
                      "received": _stamp(item["ts"]), "untrusted_title": _line(item["title"]),
                      "untrusted_from": _line(sender),
                      "automated": bool(sep and domain and _AUTOMATED.match(local))}
            yield _candidate("mail.needs_reply", "gmail", mid, fields)


def evaluate(source, previous, current, now, *, busy=None):
    """Evaluate a preflight-checked snapshot and return signal candidates."""
    before = None if previous is None else previous["items"]
    items = current["items"]
    if source == "calendar":
        candidates = list(_calendar_changes(
            before, items, now, previous_digested=previous is not None and previous.get("version") == 2
        ))
        idle = _idle_tomorrow(now, busy) if busy is not None else None
        if idle is not None:
            candidates.append(idle)
        return candidates
    if source == "schoolwork":
        return list(_school(before, items, now))
    if source == "github":
        return list(_github(items))
    if source == "commitments":
        return list(_commitment_due(items, now))
    if source == "mail":
        return list(_mail(items, now))
    raise ValueError("Unknown trigger source.")


def _validate_c3(source, snapshot):
    if source == "schoolwork" and type(snapshot.get("needs_sign_in")) is not bool:
        raise ValueError("Invalid schoolwork sign-in flag.")
    fields = _C3_FIELDS[source]
    for item in snapshot["items"]:
        if any(key not in item or type(item[key]) not in types for key, types in fields.items()):
            raise ValueError("Invalid snapshot item.")
        if source == "calendar":
            for key in ("start", "end"):
                _event_time(item[key], item["all_day"])
        elif source == "schoolwork" and item["due"] is not None:
            _instant(item["due"])


def _c3_snapshot(source, directory, *, previous=False):
    # Keep full text in memory so digests and redaction precede truncation.
    with open_directory(directory) as fd:
        snapshot, info = _read_private_json(fd, f"{source}.json", signals._MAX_READ)
    if (type(snapshot) is not dict or type(snapshot.get("version")) is not int
            or snapshot["version"] not in ((1, 2) if previous else (1,)) or snapshot.get("source") != source
            or type(snapshot.get("items")) is not list):
        raise ValueError("Invalid snapshot envelope.")
    signals._ids(snapshot["items"])
    _validate_c3(source, snapshot)
    fields = ("id", *_C3_FIELDS[source])
    items = [{key: item[key] for key in fields} for item in snapshot["items"]]
    if not previous:
        for item in items:
            item["id"] = _scrub_text(item["id"])
            if source == "github":
                item["repo"] = _scrub_text(item["repo"])
    signals._ids(items)
    items.sort(key=lambda item: item["id"])
    clean = {"version": snapshot["version"], "source": source, "items": items}
    if source == "schoolwork":
        clean["needs_sign_in"] = snapshot["needs_sign_in"]
    return clean, info.st_mtime


def _read_c3(source, directory, now):
    try:
        snapshot, modified = _c3_snapshot(source, directory)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, OverflowError, RecursionError):
        return None, "unreadable"
    if now - modified > STALE_S[source]:
        return None, "stale"
    if source == "schoolwork" and snapshot.get("needs_sign_in") is True:
        return None, "needs_sign_in"
    return snapshot, None


def _default_commitments():
    from .commitments import load
    from .vault import VaultError

    try:
        return load()
    except VaultError as exc:
        if str(exc) != "Memory entry was not found.":
            raise
        return []


def _commitment_items(load):
    for row in load():
        if (row.direction not in ("owner-owes", "owed-to-owner")
                or row.state not in ("open", "done", "dropped")
                or row.due is not None and type(row.due) is not date
                or any(type(value) is not str for value in (row.id, row.who, row.what))):
            raise ValueError("Invalid commitment row.")
        yield {"id": row.id, "who": row.who, "what": row.what,
               "due": row.due.isoformat() if row.due is not None else None,
               "direction": row.direction, "state": row.state}


def _mail_items(index, now):
    for row in index.documents_between(since=now - 7 * _DAY, until=now):
        if row["source"] != "gmail":
            continue
        meta = json.loads(row["meta_json"])
        mid, title, ts = row["ref"], row["title"], row["ts"]
        thread, owner, labels = meta["thread"], meta["from_owner"], meta["labels"]
        sender = meta.get("from", "")
        if (type(mid) is not str or type(title) is not str or type(thread) is not str
                or type(sender) is not str
                or type(owner) is not int or owner not in (0, 1)
                or type(ts) not in (int, float) or not math.isfinite(ts)
                or not isinstance(labels, list) or any(type(label) is not str for label in labels)):
            raise ValueError("Invalid mail metadata.")
        _stamp(ts)
        yield {"id": mid, "thread": thread or mid, "from_owner": owner, "ts": ts,
               "labels": labels, "title": title, "from": sender}


def _json(value, *, ensure_ascii=True):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      ensure_ascii=ensure_ascii).encode("utf-8")


def _atomic(directory, name, data):
    temp = f".triggers-{uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=directory)


def _private(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600)


def _ensure_private_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or not info.st_mode & stat.S_IWUSR:
        raise PermissionError("The triggers directory must be writable by its owner.")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(fd, 0o700)


def _read_private_json(directory, name, limit):
    with os.fdopen(os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not _private(info) or info.st_size > limit:
            raise ValueError("Invalid trigger input file.")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Trigger input file exceeds its size limit.")
    return json.loads(raw, parse_constant=signals._invalid_constant), info


@contextmanager
def _open_log(directory, *, write=False):
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY
    with os.fdopen(os.open(_LOG, flags | _FILE_FLAGS, 0o600, dir_fd=directory),
                   "ab" if write else "rb") as stream:
        if not _private(os.fstat(stream.fileno())):
            raise PermissionError("The signal log must be an owner-only regular file.")
        yield stream


def _tail_lines(fd):
    """Read newest lines first with a bound on both bytes scanned and line size."""
    offset = os.fstat(fd).st_size
    floor = max(0, offset - LOG_KEEP * _LOG_LINE_LIMIT)
    pending = b""
    while offset > floor:
        size = min(_LOG_CHUNK, offset - floor)
        offset -= size
        block = os.pread(fd, size, offset)
        right = len(block)
        while (left := block.rfind(b"\n", 0, right)) >= 0:
            yield offset + left + 1, block[left + 1:right] + pending
            pending = b""
            right = left
        pending = (block[:right] + pending)[:_LOG_LINE_LIMIT + 1]
    if floor == 0 and pending:
        yield 0, pending


def _kind(key):
    return key.partition(":")[0]


def _key_source(key):
    return _SOURCE_FOR_PREFIX.get(_kind(key).partition(".")[0])


def _valid_key(key):
    return type(key) is str and ":" in key and _kind(key) in KINDS


def _clean_key(key):
    kind, _, identity = key.partition(":")
    pattern = _KEY_SUFFIXES.get(kind)
    suffix = pattern.search(identity) if pattern is not None else None
    item = identity[:suffix.start()] if suffix is not None else identity
    rest = _scrub_text(suffix[0])[:120] if suffix is not None else ""
    return f"{kind}:{_scrub_text(item)[:_ID_LIMIT]}{rest}"


def _held_key(key):
    kind, _, identity = key.partition(":")
    digest = hashlib.sha256(identity.encode("utf-8")).digest()[:16]
    return kind, urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _hold(held, key, created):
    kind, identity = _held_key(key)
    held.setdefault(kind, {})[identity] = int(_instant(created))


def _clean_record(row):
    # Older log rows may be copied during compaction or returned to a reader.
    def clean(value):
        if isinstance(value, dict):
            return {_line(key): clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return _line(value) if isinstance(value, str) else value

    prefix = "gmail" if row["source"] == "mail" else row["source"]
    item = row["source_ref"].partition(":")[2]
    return dict(row, source_ref=f"{prefix}:{_scrub_text(item)[:_ID_LIMIT]}",
                dedupe_key=_clean_key(row["dedupe_key"]), summary_fields=clean(row["summary_fields"]))


def _read_tail(fd):
    rows = []
    for offset, raw in _tail_lines(fd):
        if not raw or len(raw) > _LOG_LINE_LIMIT:
            continue
        try:
            row = json.loads(raw, parse_constant=signals._invalid_constant)
            if (type(row) is not dict or set(row) != _RECORD_FIELDS
                    or type(row["seq"]) is not int or row["seq"] < 1
                    or rows and row["seq"] >= rows[-1]["seq"]
                    or type(row["id"]) is not str or not _HEX_ID.fullmatch(row["id"])
                    or not _valid_key(row["dedupe_key"]) or row["kind"] != _kind(row["dedupe_key"])
                    or row["source"] != _key_source(row["dedupe_key"])
                    or type(row["source_ref"]) is not str or type(row["summary_fields"]) is not dict
                    or type(row["summary_fields"].get("untrusted_from", "")) is not str
                    or type(row["summary_fields"].get("automated", False)) is not bool
                    or row["tainted"] is not True):
                continue
            _instant(row["created"])
        except (TypeError, ValueError, OverflowError, RecursionError):
            continue
        rows.append(row)
        if len(rows) == LOG_KEEP:
            return rows[::-1], offset > 0
    return rows[::-1], False


def _decode_state(state):
    seq, held, episodes = state["next_seq"], state["held"], state["episodes"]
    if type(seq) is not int or seq < 1 or type(held) is not dict or type(episodes) is not dict:
        raise ValueError
    for source, entry in episodes.items():
        if (source not in SOURCES or type(entry) is not dict or set(entry) != {"id", "posted"}
                or not _HEX_ID.fullmatch(entry["id"]) or type(entry["posted"]) is not bool):
            raise ValueError
    if state.get("version") == 2:
        for kind, bucket in held.items():
            if (kind not in KINDS or type(bucket) is not dict or not bucket
                    or any(not _HELD_ID.fullmatch(key) or type(epoch) is not int
                           for key, epoch in bucket.items())):
                raise ValueError
    else:
        legacy, held = held, {}
        for key, created in legacy.items():
            if not _valid_key(key):
                raise ValueError
            _hold(held, _clean_key(key), created)
    return {"version": 2, "next_seq": seq, "held": held, "episodes": episodes}


def _read_state(directory):
    try:
        state, _ = _read_private_json(directory, _STATE, _STATE_LIMIT)
        return _decode_state(state), state.get("version") != 2
    except (OSError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return {"version": 2, "next_seq": 1, "held": {}, "episodes": {}}, True


def _entry_size(key, value):
    """Bytes contributed by a JSON object entry, before its comma."""
    return len(_json({key: value})) - 2


def _discard(held, kind, identity):
    bucket = held[kind]
    size = _entry_size(identity, bucket.pop(identity)) + bool(bucket)
    if not bucket:
        del held[kind]
        size += _entry_size(kind, {}) + bool(held)
    return size


def _new_key_size(held, kind, identity, epoch):
    size = _entry_size(identity, epoch)
    if kind in held:
        return size + 1
    return size + _entry_size(kind, {}) + bool(held)


def _read_pending(directory):
    try:
        batch, _ = _read_private_json(directory, _PENDING, _PENDING_LIMIT)
        if (type(batch) is not dict or set(batch) != {"snapshots", "state", "records"}
                or type(batch["snapshots"]) is not dict or set(batch["snapshots"]) - set(SOURCES)
                or type(batch["state"]) is not dict or type(batch["records"]) is not list):
            raise ValueError
        legacy = batch["state"].get("version") != 2
        if legacy:
            batch["state"] = _decode_state(batch["state"])
            batch["snapshots"] = {source: _digest_snapshot(source, snapshot)
                                  for source, snapshot in batch["snapshots"].items()}
            batch["records"] = [_clean_record(row) for row in batch["records"]]
        batch["records"] = [_checkpoint_record(row, recovering=True) for row in batch["records"]]
    except FileNotFoundError:
        return None, False
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise OSError("The pending trigger checkpoint is unreadable.") from exc
    return batch, legacy


def _append(stream, records):
    if records:
        fd = stream.fileno()
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            stream.write(b"\n")
        for row in records:
            stream.write(_json(row) + b"\n")
    stream.flush()
    os.fsync(stream.fileno())


def read_signals(after=0, *, root=None, limit=500):
    """Return committed signal log records after the caller's sequence cursor."""
    directory = Path(config.V3_TRIGGERS_DIR if root is None else root)
    try:
        with open_directory(directory) as fd:
            fcntl.flock(fd, fcntl.LOCK_SH)
            with _open_log(fd) as log:
                rows, _ = _read_tail(log.fileno())
    except FileNotFoundError:
        return []
    selected = [row for row in rows if row["seq"] > after][:max(0, limit)]
    return [_clean_record(row) for row in selected]


class Triggers:
    def __init__(self, *, root=None, signals_dir=None, sources=SOURCES, post, index=None,
                 commitments=None, busy=None, clock=time.time):
        self._root = Path(config.V3_TRIGGERS_DIR if root is None else root)
        self._signals = Path(config.V3_SIGNALS_DIR if signals_dir is None else signals_dir)
        self._sources = tuple(dict.fromkeys(sources))
        if any(source not in SOURCES for source in self._sources):
            raise ValueError("Unknown trigger source.")
        self._post = post
        self._index = index
        self._commitments = _default_commitments if commitments is None else commitments
        self._busy = busy
        self._clock = clock

    def _current(self, source, now):
        if source in _C3_FIELDS:
            current, reason = _read_c3(source, self._signals, now)
            return current, None, reason
        try:
            items = list(_commitment_items(self._commitments) if source == "commitments"
                         else _mail_items(self._index, now))
            prepared = [_scrub_fields(item) for item in items]
            signals.write(source, prepared, directory=self._signals)
            current = signals.read(source, directory=self._signals)
            digests = {safe["id"]: {key: _digest_text(raw[key]) for key in _TEXT_FIELDS[source]}
                       for raw, safe in zip(items, prepared)}
            baseline = dict(current, version=2, items=[
                dict(item, **digests[item["id"]]) for item in current["items"]
            ])
        except Exception:
            return None, None, "unreadable"
        return current, baseline, None

    def _evaluate(self, now):
        previous_dir = self._root / "previous"
        snapshots, previous_copies, blocked, candidates = {}, {}, {}, {}
        for source in self._sources:
            current, baseline, reason = self._current(source, now)
            if reason is not None:
                blocked[source] = reason
                continue
            previous = None
            if source in _C3_FIELDS:
                with suppress(OSError, ValueError, OverflowError, RecursionError):
                    previous, _ = _c3_snapshot(source, previous_dir, previous=True)
                if previous is not None and previous["version"] == 1:
                    previous = _digest_snapshot(source, previous)
                baseline = _digest_snapshot(source, current)
            if len(_json(baseline, ensure_ascii=False)) + 1 > signals._MAX_READ:
                blocked[source] = "unreadable"
                continue
            candidates[source] = evaluate(source, previous, current, now, busy=self._busy)
            snapshots[source] = baseline
            if previous is not None:
                previous_copies[source] = previous
        return snapshots, previous_copies, blocked, candidates

    def _legacy_baselines(self, evaluated):
        """Convert legacy copies even when their source cannot advance."""
        try:
            with open_directory(self._root / "previous") as directory:
                for source in SOURCES:
                    if source in evaluated:
                        continue
                    with suppress(OSError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
                        snapshot, _ = _read_private_json(directory, f"{source}.json", signals._MAX_READ)
                        if snapshot["version"] == 1:
                            yield source, _digest_snapshot(source, snapshot)
        except OSError:
            return

    def _records(self, state, candidates, now):
        active = {_held_key(row["dedupe_key"]) for rows in candidates.values() for row in rows}
        held, histories = state["held"], []
        for kind, bucket in list(held.items()):
            if _key_source(kind) not in candidates:
                continue
            for identity, epoch in list(bucket.items()):
                present = (kind, identity) in active
                if now - epoch >= DEDUPE_TTL_S or not present and kind not in _EDGE_KINDS:
                    _discard(held, kind, identity)
                elif not present:
                    histories.append((epoch, kind, identity))
        evictable = deque(sorted(histories))
        size = len(_json(state)) + 1
        envelope = len(_json(dict(state, held={}, next_seq=state["next_seq"] + len(KINDS) * MAX_PER_KIND))) + 1
        target = _STATE_LIMIT - max(0, _STATE_ENVELOPE_RESERVE - envelope)
        created = _stamp(now)
        epoch = int(_instant(created))
        counts, records = Counter(), []
        held_back, deferred_edges = 0, {}
        for source, rows in candidates.items():
            for row in rows:
                kind, identity = _held_key(row["dedupe_key"])
                if identity in held.get(kind, {}):
                    continue
                within_quota = counts[kind] < MAX_PER_KIND
                added = _new_key_size(held, kind, identity, epoch)
                while within_quota and evictable and size + added > target:
                    _, old_kind, old_id = evictable.popleft()
                    size -= _discard(held, old_kind, old_id)
                    added = _new_key_size(held, kind, identity, epoch)
                if size + added > target and not evictable:
                    held_back += 1
                elif within_quota:
                    size += added
                    held.setdefault(kind, {})[identity] = epoch
                    records.append(dict(row, seq=state["next_seq"], id=uuid4().hex,
                                        source=source, created=created))
                    counts[kind] += 1
                    state["next_seq"] += 1
                    continue
                if kind in _EDGE_KINDS:
                    deferred_edges.setdefault(source, set()).add(row["source_ref"].partition(":")[2])
        return records, held_back, deferred_edges

    def _episodes(self, episodes, snapshots, blocked):
        pending = []
        for source in snapshots:
            episodes.pop(source, None)
        for source, reason in blocked.items():
            if source not in episodes:
                episodes[source] = {"id": uuid4().hex, "posted": False}
            if reason != "needs_sign_in" and not episodes[source]["posted"]:
                pending.append(source)
        return pending

    def _post_pending(self, directory, state, pending):
        episodes = state["episodes"]
        posted = False
        for source in pending:
            entry = episodes[source]
            try:
                self._post({"kind": "news", "rank": 4, "ask": BLOCKED_ASK[source],
                            "idempotency_key": f"triggers-blocked:{source}:{entry['id']}"})
            except Exception:
                continue
            entry["posted"] = posted = True
        if posted:
            _atomic(directory, _STATE, _json(state) + b"\n")

    def _finish(self, directory, log, batch, tail, trimmed):
        last_seq = tail[-1]["seq"] if tail else 0
        records = [row for row in batch["records"] if row["seq"] > last_seq]
        _append(log, records)
        os.fsync(directory)
        if trimmed or len(tail) + len(records) > LOG_KEEP:
            kept = (tail + records)[-LOG_KEEP:]
            _atomic(directory, _LOG, b"".join(_json(_clean_record(row)) + b"\n" for row in kept))
        _atomic(directory, _STATE, _json(batch["state"]) + b"\n")
        if batch["snapshots"]:
            with open_directory(self._root / "previous", create=True) as previous:
                _ensure_private_directory(previous)
                for source, snapshot in batch["snapshots"].items():
                    _atomic(previous, f"{source}.json", _json(snapshot, ensure_ascii=False) + b"\n")
        os.unlink(_PENDING, dir_fd=directory)
        os.fsync(directory)
        return len(records)

    def tick(self):
        with open_directory(self._root, create=True) as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            _ensure_private_directory(directory)
            recovered = 0
            batch, legacy = _read_pending(directory)
            if batch is not None:
                if legacy:
                    batch["snapshots"].update(self._legacy_baselines(batch["snapshots"]))
                # Finish the exact observation paired with the log before reading
                # newer source files. The log's sequence skips any durable prefix.
                with _open_log(directory, write=True) as log:
                    tail, trimmed = _read_tail(log.fileno())
                    recovered = self._finish(directory, log, batch, tail, trimmed or (legacy and bool(tail)))
            with _open_log(directory, write=True) as log:
                result = self._tick(directory, log)
            result["emitted"] += recovered
            return result

    def _tick(self, directory, log):
        now = self._clock()
        state, legacy = _read_state(directory)
        tail, trimmed = _read_tail(log.fileno())
        for row in tail:
            if row["seq"] >= state["next_seq"]:
                _hold(state["held"], _clean_key(row["dedupe_key"]), row["created"])
                state["next_seq"] = row["seq"] + 1
        snapshots, previous_copies, blocked, candidates = self._evaluate(now)
        pending = self._episodes(state["episodes"], snapshots, blocked)
        records, held_back, deferred_edges = self._records(state, candidates, now)
        for source, ids in deferred_edges.items():
            # Keep each deferred edge observable against the same prior item.
            baseline = snapshots[source]
            previous = previous_copies[source]
            items = [item for item in baseline["items"] if item["id"] not in ids]
            items.extend(item for item in previous["items"] if item["id"] in ids)
            merged = dict(baseline, items=sorted(items, key=lambda item: item["id"]))
            snapshots[source] = (merged if len(_json(merged, ensure_ascii=False)) + 1 <= signals._MAX_READ
                                 else previous)
        if legacy:
            snapshots.update(self._legacy_baselines(snapshots))

        # Stage the observation with its sequence range. Only _finish publishes
        # the log, dedupe state and previous snapshots, in that order.
        batch = {"records": records, "state": state, "snapshots": snapshots}
        checkpoint = dict(batch, records=[_checkpoint_record(row) for row in records])
        data = _json(checkpoint, ensure_ascii=False) + b"\n"
        if len(data) > _PENDING_LIMIT:
            raise OSError("The pending trigger checkpoint exceeds its size limit.")
        _atomic(directory, _PENDING, data)
        self._finish(directory, log, batch, tail, trimmed or (legacy and bool(tail)))
        self._post_pending(directory, state, pending)
        result = {"emitted": len(records), "blocked_config": blocked}
        if held_back:
            result["held_back"] = held_back
        return result
