"""Post a daily learning digest from store metadata and logs.

Skill lines carry skill-ledger IDs. A later hud3 panel will use them for
rollback, pin and lock buttons that call the owner routes. The digest has
no answer options. Viewing a diff would require a separate blob-reading route.

Reads are capped at 16 MiB for the ledger and 1 MiB of rows per JSONL sidecar
per run, plus short cursor samples. A replaced or truncated sidecar starts
at byte zero. Checkpoints keep display-safe memory refs or their digests.
Vault inventory caps the scan at 16,000 directory entries and 8 KiB of
frontmatter per entry. A memory archived and re-added at the same ref between
snapshots produces neither an add nor an archive. Flags are counted separately.

Like B4, the digest checks quiet hours immediately before calling the feed.
The feed acquires its own lock afterward, so its write can cross 23:00.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config, untrusted
from jarvis.v2 import redact
from jarvis.v2.quiet import parse_hhmm

from ._paths import open_directory
from .feed import FeedError, _ref, _text, _unique_keys

DIGEST_AT = "20:30"
MAX_LINES = 12
RANK = 3

_CHICAGO = ZoneInfo("America/Chicago")
_WAKE, _NIGHT = wall_time(7), wall_time(23)
_STATE = "state.json"
_LOG = "log.jsonl"
_LEDGER_LIMIT = 16 * 1024 * 1024
_LOG_LIMIT = 1024 * 1024
_CURSOR_SAMPLE = 256
_STATE_LIMIT = 2 * 1024 * 1024
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_OFFSETS = {"curator", "flags", "reviewer"}
_SOURCE_FIELDS = {"curator.jsonl": ("skill", "change", "idle_days"), ".flags.jsonl": ("name", "rule"),
                  "log.jsonl": ("outcome", "input_tokens", "output_tokens")}
_ID = re.compile(r"[0-9a-f]{32}")
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_MEMORY_FOLDER = r"(?:people|projects|preferences|style|facts)/"
_MEMORY_REF = re.compile(rf"(?:USER|commitments|{_MEMORY_FOLDER}{_SLUG})")
_MEMORY_DIGEST = re.compile(rf"(?:{_MEMORY_FOLDER})?#[0-9a-f]{{64}}")
_SHA = re.compile(r"[0-9a-f]{64}")
_OP = {"create": "created", "patch": "patched", "write_file": "wrote a file", "remove_file": "removed a file",
       "archive": "archived", "restore": "restored", "rollback": "rolled back"}


@dataclass(frozen=True)
class Result:
    outcome: str
    brief_id: str | None = None
    items: int = 0
    lines: int = 0


def due(now, *, last_run, at=DIGEST_AT) -> bool:
    start = parse_hhmm(at)
    if start is None or not _WAKE <= start < _NIGHT:
        raise ValueError("The digest schedule requires 07:00 <= time < 23:00.")
    local = datetime.fromtimestamp(now, _CHICAGO)
    return start <= local.time() < _NIGHT and (
        last_run is None or datetime.fromtimestamp(last_run, _CHICAGO).date() < local.date()
    )


def _state_path(state_dir):
    return Path(config.V3_DIR / "learning-digest" if state_dir is None else state_dir)


def _check_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Learning digest state needs a private owner directory.")


@contextmanager
def _file(directory, name, flags=os.O_RDONLY, *, private=False):
    fd = os.open(name, flags | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or private and stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("Learning digest files must be owner-owned regular files.")
        yield fd
    finally:
        os.close(fd)


def _id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _memory_ref(value):
    return isinstance(value, str) and len(value) <= 144 and _MEMORY_REF.fullmatch(value) is not None


def _saved_memory_ref(value):
    return _memory_ref(value) or isinstance(value, str) and _MEMORY_DIGEST.fullmatch(value) is not None


def _log_cursor(value):
    # Integer offsets from old checkpoints carry no file identity.
    return value is None or _count(value) or (
        isinstance(value, list) and len(value) == 4
        and all(type(n) is int and 0 <= n < 2**64 for n in value[:3])
        and isinstance(value[3], str) and _SHA.fullmatch(value[3]) is not None
    )


def _day(value):
    return isinstance(value, str) and date.fromisoformat(value).isoformat() == value


def _last_brief(value, day):
    return value is None or (
        isinstance(value, dict) and value.keys() == {"day", "id", "items"}
        and _day(value["day"]) and value["day"] <= day and _id(value["id"]) and _count(value["items"])
    )


def _read_state(directory):
    try:
        with _file(directory, _STATE, private=True) as fd:
            data = os.read(fd, _STATE_LIMIT + 1)
    except FileNotFoundError:
        return None
    if len(data) <= _STATE_LIMIT:
        row = json.loads(data, object_pairs_hook=_unique_keys)
        if (isinstance(row, dict)
                and row.keys() == {"day", "ledger", "offsets", "memory", "last_brief", "items", "lines"}
                and _day(row["day"]) and _last_brief(row["last_brief"], row["day"])
                and (row["ledger"] is None or _id(row["ledger"]))
                and isinstance(row["offsets"], dict) and row["offsets"].keys() == _OFFSETS
                and all(map(_log_cursor, row["offsets"].values()))
                and isinstance(row["memory"], list) and all(map(_saved_memory_ref, row["memory"]))
                and _count(row["items"]) and _count(row["lines"]) and row["lines"] <= MAX_LINES):
            return row
    raise ValueError("Invalid learning digest state.")


def _write_state(directory, row):
    temp = ".learning-digest-" + uuid4().hex
    with _file(directory, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
        try:
            with os.fdopen(fd, "w", encoding="ascii", closefd=False) as stream:
                os.fchmod(fd, 0o600)
                json.dump(row, stream, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(fd)
            os.replace(temp, _STATE, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=directory)


def _append_log(directory, now, day, result):
    row = {"ts": now, "day": day, "outcome": result.outcome, "items": result.items, "lines": result.lines}
    with _file(directory, _LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, private=True) as fd:
        # Contenders also record their outcome while the directory is locked.
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, "w", encoding="ascii", closefd=False) as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(fd)
        os.fsync(directory)


def _log_sample(fd, offset):
    # Also detect common in-place rewrites and reuse of a removed file's inode.
    size = min(offset, _CURSOR_SAMPLE)
    return sha256(os.pread(fd, size, offset - size)).hexdigest()


def _log_rows(directory, name, cursor, baseline):
    try:
        with _file(directory, name) as fd:
            info = os.fstat(fd)
            size = info.st_size
            identity = [info.st_dev, info.st_ino]
            start = 0
            if baseline:
                start = max(0, size - _LOG_LIMIT)
            elif (isinstance(cursor, list) and cursor[:2] == identity and cursor[2] <= size
                  and cursor[3] == _log_sample(fd, cursor[2])):
                start = cursor[2]
            data = os.pread(fd, min(size - start, _LOG_LIMIT), start)
            end = data.rfind(b"\n") + 1
            offset = start + end if end or not baseline else 0
            position = [*identity, offset, _log_sample(fd, offset)]
            if not baseline and start and cursor[3] != _log_sample(fd, start):
                # The file was rewritten during the read. Retry from the saved cursor.
                return [], cursor
    except (OSError, ValueError):
        return [], cursor

    if baseline:
        return [], position
    rows = []
    for line in data[:end].split(b"\n")[:-1]:
        try:
            row = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            rows.append({key: row.get(key) for key in _SOURCE_FIELDS[name]})
    return rows, position


def _read_log(path, cursor, baseline):
    try:
        with open_directory(path.parent) as directory:
            if os.fstat(directory).st_uid == os.getuid():
                return _log_rows(directory, path.name, cursor, baseline)
    except OSError:
        pass
    return [], cursor


def _line(prefix, name, suffix, ref, *, category):
    try:
        clean = redact.for_display(name)
        clean = untrusted.one_line(clean, cap=max(2, len(clean)))
        clean = _text(clean, len(clean), empty=True)
        if suffix:
            suffix = " " + untrusted.one_line(_text(suffix, len(suffix)), cap=64)
        name_cap = 120 - len(prefix) - 2 - len(suffix)
        name = untrusted.one_line(clean, cap=name_cap)
        label = _text(f"{prefix}: {name}{suffix}", 120) if name else None
    except (FeedError, UnicodeError):
        label = None
    try:
        safe_ref = _ref(ref)
    except FeedError:
        safe_ref = "jarvis:learning"
    if safe_ref != ref:
        label = None
    return {"label": label or f"{category}: (name withheld)", "ref": safe_ref}


def _skills(store, saved, baseline):
    groups = ([], [], [])
    ledger, curator = saved["ledger"], saved["offsets"]["curator"]
    try:
        with store._operation(initialize=False, nonblocking_read=True) as op:
            curator_rows, curator = _log_rows(op.ledger_fd, "curator.jsonl", curator, baseline)
            try:
                rows = op.records(visible_only=True, limit=_LEDGER_LIMIT)
            except (OSError, ValueError):
                return [], ledger, curator_rows, curator
            start = next((i + 1 for i, row in enumerate(rows) if row["id"] == ledger), None) if ledger else 0
            if start is not None and not baseline:
                for row in rows[start:]:
                    if row["actor"] == "curator":
                        continue
                    try:
                        status = op.status(row)
                    except (OSError, ValueError):
                        # Retry from the last committed digest if receipts are inaccessible.
                        return [], ledger, curator_rows, curator
                    if status == "unapplied":
                        continue
                    op_label = _OP[row["op"]]
                    if status == "blocked":
                        group, prefix = 1, "Quarantined"
                        rules = sorted({hit["rule"] for hit in row["scan"]["hits"]
                                        if _rule(hit["rule"])})
                        suffix = f" {op_label} ({', '.join(rules) or 'scan'})"
                    else:
                        group = 0 if row["tainted"] else 2
                        prefix = "Tainted" if row["tainted"] else "Owner" if row["actor"] == "owner" else "Skill"
                        suffix = " " + op_label
                    groups[group].append(_line(prefix, row["skill"], suffix,
                                               "skill-ledger:" + row["id"], category="Skill"))
            ledger = rows[-1]["id"] if rows else None
    except (OSError, ValueError):
        return [], ledger, [], curator
    return [line for group in groups for line in group], ledger, curator_rows, curator


def _rule(value):
    return isinstance(value, str) and len(value) <= 64 and re.fullmatch(_SLUG, value) is not None


def _memory_key(ref):
    if _MEMORY_DIGEST.fullmatch(ref):
        return ref
    folder = ref[:ref.rfind("/") + 1]
    return f"{folder}#{sha256(ref.encode('ascii')).hexdigest()}"


def _memory_checkpoint_ref(ref, key):
    try:
        _ref(ref)
        _ref("memory:" + ref)
    except FeedError:
        return key
    return ref


def _memory(vault, previous):
    try:
        refs = {}
        for location, _ in vault.entries(refs_only=True):
            if _memory_ref(location.ref):
                key = _memory_key(location.ref)
                refs[key] = key if previous.get(key) == key else _memory_checkpoint_ref(location.ref, key)
            elif not location.name:
                prefix = location.folder + "/"
                refs.update((key, ref) for key, ref in previous.items() if key.startswith(prefix))
        return refs
    except (OSError, ValueError, OverflowError):
        return previous


def _memory_lines(previous, current, flags):
    lines = []
    for change, source, other in (("added", current, previous), ("archived", previous, current)):
        for ref in sorted(source[key] for key in source.keys() - other.keys()):
            if _MEMORY_DIGEST.fullmatch(ref):
                lines.append({"label": "Memory: (name withheld)", "ref": "jarvis:learning"})
            else:
                lines.append(_line("Memory " + change, ref, "", "memory:" + ref, category="Memory"))
    for row in flags:
        name, rule = row.get("name"), row.get("rule")
        if _memory_ref(name) and _rule(rule):
            lines.append(_line("Memory flagged", name, f" ({rule})", "memory:" + name, category="Memory"))
    return lines


def _curator_lines(rows):
    for row in rows:
        name, change, days = row.get("skill"), row.get("change"), row.get("idle_days")
        if isinstance(name, str) and change in ("stale", "archived") and _count(days):
            ref = "skill:" + name if _rule(name) else "jarvis:learning"
            if ref == "jarvis:learning":
                yield {"label": "Curator: (name withheld)", "ref": ref}
            else:
                yield _line("Curator", name, f" {change} ({days} idle days)", ref, category="Curator")


def _spend(rows):
    runs = tokens_in = tokens_out = skipped = 0
    for row in rows:
        outcome = row.get("outcome")
        if outcome in ("ok", "error"):
            runs += 1
            incoming, outgoing = row.get("input_tokens"), row.get("output_tokens")
            tokens_in += incoming if _count(incoming) else 0
            tokens_out += outgoing if _count(outgoing) else 0
        elif outcome in ("skipped-budget", "skipped-usage"):
            skipped += 1
    if runs or skipped:
        return [{"label": f"Reviewer: {runs} runs, {tokens_in} in / {tokens_out} out tokens, {skipped} skipped",
                 "ref": "jarvis:reviewer"}]
    return []


def _collect(saved, previous, store, vault, reviewer_dir, baseline):
    skills, ledger, curator, curator_offset = _skills(store, saved, baseline)
    memory = _memory(vault, previous)
    flags, flags_offset = _read_log(vault.root / ".flags.jsonl", saved["offsets"]["flags"], baseline)
    reviewer, reviewer_offset = _read_log(reviewer_dir / _LOG, saved["offsets"]["reviewer"], baseline)
    positions = {"ledger": ledger, "memory": sorted(memory.values()),
                 "offsets": {"curator": curator_offset, "flags": flags_offset, "reviewer": reviewer_offset}}
    if baseline:
        return positions, [], []
    items = skills + _memory_lines(previous, memory, flags) + list(_curator_lines(curator))
    return positions, items, _spend(reviewer)


def _item_count(items):
    return f"{items} {'item' if items == 1 else 'items'}"


def _claim_and_post(directory, day, feed, store, vault, reviewer_dir, in_window):
    try:
        saved = _read_state(directory)
    except (ValueError, RecursionError):
        os.rename(_STATE, f"state.corrupt-{uuid4().hex}.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        saved = None
    baseline = saved is None
    if baseline:
        saved = {"ledger": None, "memory": [], "offsets": dict.fromkeys(_OFFSETS), "last_brief": None}
    previous = {}
    for ref in saved["memory"]:
        key = _memory_key(ref)
        previous[key] = key if key == ref or previous.get(key) == key else _memory_checkpoint_ref(ref, key)
    safe_memory = sorted(previous.values())
    # Rewrite valid older checkpoints even when the day was already claimed.
    if safe_memory != saved["memory"]:
        saved["memory"] = safe_memory
        _write_state(directory, saved)
    if not baseline and day <= saved["day"]:
        return Result("already")

    positions, items, spend = _collect(saved, previous, store, vault, reviewer_dir, baseline)
    if not in_window():
        return Result("quiet")
    count = len(items)
    if count + len(spend) > MAX_LINES:
        keep = MAX_LINES - 1 - len(spend)
        items = items[:keep] + [{"label": f"+{count - keep} more", "ref": "jarvis:learning"}]
    lines = items + spend if count else []
    if not baseline and not count:
        positions = {key: saved[key] for key in positions}
    claim = dict(positions, day=day, items=count, lines=len(lines), last_brief=saved["last_brief"])
    _write_state(directory, claim)
    if baseline or not count:
        return Result("baseline" if baseline else "empty")
    fields = {"kind": "digest", "rank": RANK, "ask": f"Learning digest · {_item_count(count)}",
              "why_now": f"{_item_count(count)} since the previous digest.", "context": lines}
    if not in_window():
        return Result("quiet")
    try:
        brief = feed.post(fields, actor="jarvis")
    except Exception:
        return Result("error", items=count, lines=len(lines))
    brief_id = brief["id"]
    if _id(brief_id):
        _write_state(directory, dict(claim, last_brief={"day": day, "id": brief_id, "items": count}))
    return Result("posted", brief_id, count, len(lines))


def run(*, feed, now=None, state_dir=None, store=None, vault=None, reviewer_dir=None) -> Result:
    """Persist the day and source positions before posting to the owner feed."""
    started_at = time.time() if now is None else now
    local = datetime.fromtimestamp(started_at, _CHICAGO)
    day = local.date().isoformat()
    if not _WAKE <= local.time() < _NIGHT:
        return Result("quiet")

    def in_window():
        current = datetime.fromtimestamp(time.time(), _CHICAGO) if now is None else local
        return current.date() == local.date() and _WAKE <= current.time() < _NIGHT

    from .skill_store import SkillStore
    from .vault import Vault

    store = SkillStore() if store is None else store
    vault = Vault() if vault is None else vault
    review = Path(config.V3_DIR / "reviewer" if reviewer_dir is None else reviewer_dir)
    try:
        with open_directory(_state_path(state_dir), create=True) as directory:
            _check_directory(directory)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = Result("already")
            else:
                try:
                    result = _claim_and_post(directory, day, feed, store, vault, review, in_window)
                except (OSError, ValueError, RecursionError):
                    result = Result("error")
            with suppress(OSError, ValueError):
                _append_log(directory, started_at, day, result)
            return result
    except (OSError, ValueError):
        return Result("error")


def summary_line(*, now=None, state_dir=None):
    day = datetime.fromtimestamp(time.time() if now is None else now, _CHICAGO).date().isoformat()
    try:
        with open_directory(_state_path(state_dir)) as directory:
            _check_directory(directory)
            saved = _read_state(directory)
    except (FileNotFoundError, ValueError, RecursionError):
        return None
    last = None if saved is None else saved["last_brief"]
    if last is not None and last["day"] == day:
        return {"label": f"Learning digest: {_item_count(last['items'])}", "ref": f"brief:{last['id']}"}
    return None
