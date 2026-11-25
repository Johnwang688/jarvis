"""Private learned skills, with a write-ahead ledger and reversible renames."""

from contextlib import ExitStack, contextmanager, suppress
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
from uuid import uuid4

from jarvis import config
from jarvis.tools.secrets import is_credential_name
from jarvis.tools.skills import metadata
from jarvis.v2 import redact
from . import content_scan
from .index import open_directory

MAX_BYTES = 24 * 1024
MAX_FILES = 60
_MAIN = "SKILL.md"
_AUX = ("references", "templates")
_OWNER_READ_LIMIT = 64 * 1024
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_NOFOLLOW | os.O_NONBLOCK
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_LOG = re.compile(r"^bugfix|^(?:fix|session)(?:-|$)|(?:^|-)(?:(?:pr|issue)-?[0-9]+|(?:19|20)[0-9]{2}(?:[0-9]{4})?)(?:-|$)")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])\Z", re.I)
_PLAIN_DESCRIPTION = re.compile(r"[A-Za-z][A-Za-z0-9 ,.()'!?/-]*\Z")
_YAML_WORDS = {"y", "yes", "n", "no", "on", "off", "true", "false", "null"}
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_ACTORS = ("reviewer", "foreground", "owner", "curator")
_OPS = ("create", "patch", "write_file", "remove_file", "archive", "restore", "rollback")
_KEYS = {"id", "ts", "op", "skill", "path", "before", "after", "actor", "session", "tainted", "scan"}
_FIELDS = {"name", "description", "created_by", "pinned", "locked"}
_USAGE = ".usage.json"
_STATE_LIMIT = 1024 * 1024
_TORN_END = b"\0\n"


class StoreError(ValueError):
    """An error whose message is safe to return on the tool surface."""


class StoreBusy(StoreError):
    """The operation could not acquire the store lock by its deadline."""


def _leaf(value):
    if (not isinstance(value, str) or not value or value.startswith(".")
            or value != value.strip() or value.endswith(".")
            or any(c in '/\\:<>"|?*' or unicodedata.category(c).startswith("C") for c in value)
            or len(value.encode()) > 255 or _DEVICE.fullmatch(value.split(".")[0])):
        raise StoreError("Invalid skill name or file path.")
    return value


def _name(value):
    _leaf(value)
    if len(value) > 64 or not _SLUG.fullmatch(value) or _LOG.search(value):
        raise StoreError("Use a short lowercase kebab-case skill name that describes the workflow.")
    return value


def _path(value, *, main=True):
    if main and value == _MAIN:
        return (_MAIN,)
    if isinstance(value, str):
        folder, _, leaf = value.partition("/")
        if folder in _AUX and not is_credential_name(leaf):
            return folder, _leaf(leaf)
    raise StoreError("Use SKILL.md or a file directly inside references or templates.")


def _text(value, *, limit=MAX_BYTES, required=False):
    if (not isinstance(value, str) or "\0" in value or (required and not value.strip())
            or len(value.encode()) > limit):
        raise StoreError("Missing or invalid text, or the file exceeds its size limit.")
    return value


def _description(value):
    _text(value, limit=1024, required=True)
    if any(c in value for c in '\r\n\t\v\f\x85\u2028\u2029'):
        raise StoreError("The skill description must fit on one line.")
    return value.strip()


def _plain_description(value):
    return _PLAIN_DESCRIPTION.fullmatch(value) and value.lower() not in _YAML_WORDS


def _scalar(value):
    return value if _plain_description(value) else _json(value)


def _header(text, name):
    opening, sep, body = text.partition("\n---\n")
    lines = opening.split("\n")
    if not sep or lines[0] != "---" or len(lines) != len(_FIELDS) + 1:
        raise StoreError("Invalid learned skill frontmatter.")
    raw = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    if (set(raw) != _FIELDS or raw["name"] != _scalar(name)
            or raw["created_by"] != "agent" or any(raw[k] not in ("true", "false")
                                                   for k in ("pinned", "locked"))):
        raise StoreError("Invalid learned skill frontmatter.")
    written = raw["description"]
    if _plain_description(written):
        raw["description"] = _description(written)
    else:
        try:
            desc = _description(json.loads(written))
        except ValueError:
            raise StoreError("Invalid description field in learned skill frontmatter.") from None
        if written != _json(desc):
            raise StoreError("Invalid description field in learned skill frontmatter.")
        raw["description"] = desc
    _text(body, required=True)
    return lines[1:], raw


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _epoch(value):
    return (type(value) is int and -(1 << 63) <= value < (1 << 63)
            or type(value) is float and math.isfinite(value))


def _usage(data):
    rows = json.loads(data)
    if not isinstance(rows, dict):
        raise ValueError("Invalid skill usage.")
    for name, row in rows.items():
        _name(name)
        if (not isinstance(row, dict) or row.keys() != {"count", "last_used"}
                or type(row["count"]) is not int or row["count"] < 1
                or not _epoch(row["last_used"])):
            raise ValueError("Invalid skill usage.")
    return rows


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _scan_metadata(*values):
    if any(not content_scan.scan(value, purpose="skill").ok for value in values):
        raise StoreError("Skill metadata failed its content scan.")


def _visible_record(row):
    # Older stores could record blocked metadata. Keep those receipts
    # available internally without displaying the blocked strings.
    try:
        _scan_metadata(row["skill"], row["path"], row["session"])
    except StoreError:
        return False
    return True


def _scan_file(text, name, path):
    verdict = content_scan.scan(text, purpose="skill")
    if path != _MAIN:
        return verdict
    _, fields = _header(text, name)
    description = content_scan.scan(fields["description"], purpose="skill")
    if verdict.ok and not description.ok:
        # A quoted scalar can hide characters from the literal file scan.
        # Attribute those hits to the scalar as written in the file.
        label = "\ndescription: "
        start = text.index(label) + len(label)
        span = (start, text.index("\n", start))
        hits = tuple(content_scan.Hit(hit.rule, span, hit.severity) for hit in description.hits)
        verdict = content_scan.Verdict((*verdict.hits, *hits))
    return verdict


def _stat(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity(st):
    return (st.st_dev, st.st_ino) if st is not None else None


def _stamp(st):
    return (*_identity(st), st.st_ctime_ns, st.st_mtime_ns, st.st_size) if st is not None else None


def _verify(fd, name, expected):
    if _stamp(_stat(fd, name)) != _stamp(expected):
        raise StoreError("The skill path changed during the operation.")


def _verify_directory(path, fd):
    with open_directory(path) as current:
        if _identity(os.fstat(current)) != _identity(os.fstat(fd)):
            raise StoreError("A skill storage directory changed during the operation.")


def _private(info):
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StoreError("Learned skill storage must be owner-only.")


def _private_root(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        raise StoreError("Learned skill storage must belong to the owner.")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(fd, 0o700)


def _directory(stack, parent, name, *, create=False, private=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    fd = os.open(name, _DIR, dir_fd=parent)
    stack.callback(os.close, fd)
    if private:
        _private(os.fstat(fd))
    return fd


def _open_file(parent, name, flags=os.O_RDONLY, *, private=True):
    fd = os.open(name, flags | _FILE, 0o600, dir_fd=parent)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise StoreError("Skill storage requires regular files with one link.")
        if private:
            _private(st)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(parent, name, *, private=True, limit=MAX_BYTES + 2048, prefix=False):
    with os.fdopen(_open_file(parent, name, private=private), "rb") as file:
        initial = os.fstat(file.fileno())
        data = file.read(limit if prefix else limit + 1)
        if not prefix and len(data) > limit:
            raise StoreError("The skill file exceeds its size limit.")
        if _stamp(initial) != _stamp(os.fstat(file.fileno())):
            raise StoreError("The skill file changed while being read.")
        _verify(parent, name, initial)
        return data


def _write(fd, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("Short skill store write")
        remaining = remaining[count:]
    os.fsync(fd)


def _atomic(parent, name, data):
    temp = ".tmp-" + uuid4().hex
    fd = _open_file(parent, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.fchmod(fd, 0o600)
        _write(fd, data)
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=parent)


def _rename_new(source, entry, dest, target):
    """Linux renameat2 with RENAME_NOREPLACE, including for directories."""
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    if renameat2(source, os.fsencode(entry), dest, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _valid_record(row):
    if (not isinstance(row, dict) or row.keys() != _KEYS
            or not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
            or row["op"] not in _OPS or row["actor"] not in _ACTORS
            or (row["actor"] == "curator" and row["op"] != "archive")
            or not isinstance(row["ts"], str) or not isinstance(row["session"], str)
            or type(row["tainted"]) is not bool):
        return False
    for value in (row["before"], row["after"]):
        if value is not None and (not isinstance(value, str) or not _SHA.fullmatch(value)):
            return False
    try:
        _name(row["skill"])
        _path(row["path"])
        timestamp = datetime.fromisoformat(row["ts"])
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        return False
    before, after = row["before"], row["after"]
    main = row["path"] == _MAIN
    valid_change = {
        "create": main and before is None and after is not None,
        "patch": before is not None and after is not None,
        "write_file": not main and after is not None,
        "remove_file": not main and before is not None and after is None,
        "archive": main and before is not None and after is None,
        "restore": main and before is None and after is not None,
        "rollback": row["actor"] == "owner" and (before is not None or after is not None),
    }
    if not valid_change[row["op"]]:
        return False
    verdict = row["scan"]
    if not isinstance(verdict, dict) or verdict.keys() != {"ok", "hits"}:
        return False
    if type(verdict["ok"]) is not bool or not isinstance(verdict["hits"], list):
        return False
    for hit in verdict["hits"]:
        if (not isinstance(hit, dict) or hit.keys() != {"rule", "span", "severity"}
                or not isinstance(hit["rule"], str) or hit["severity"] not in ("block", "flag")
                or not isinstance(hit["span"], list) or len(hit["span"]) != 2
                or any(type(n) is not int for n in hit["span"])
                or not 0 <= hit["span"][0] <= hit["span"][1]):
            return False
    return verdict["ok"] == all(hit["severity"] != "block" for hit in verdict["hits"])


class SkillStore:
    def __init__(self, root=None, *, ledger=None, links=None, owner_roots=None, lock_deadline=None):
        self._root = root
        self._ledger = ledger
        self._links = None if links is None else tuple(links)
        self._owners = None if owner_roots is None else tuple(owner_roots)
        self._lock_deadline = lock_deadline

    def _root_path(self):
        root = self._root
        if root is None:
            override = os.environ.get("JARVIS_LEARNED_SKILLS")
            root = Path(override).expanduser() if override else config.V3_DIR / "skills"
        return Path(os.path.abspath(root))

    def _acquire_lock(self, fd, mode, *, nonblocking):
        if nonblocking:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        elif self._lock_deadline is None:
            fcntl.flock(fd, mode)
        else:
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    remaining = self._lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreBusy("Skill store is busy.") from None
                    time.sleep(min(0.05, remaining))

    @contextmanager
    def _operation(self, *, initialize=True, readonly=False, nonblocking_read=False):
        initialize = initialize and not readonly
        # A deadline-bound owner request only acts on existing storage.
        create = initialize and self._lock_deadline is None
        consumers = (Path.home() / ".claude/skills", Path.home() / ".codex/skills")
        root = self._root_path()
        ledger = Path(os.path.abspath(self._ledger if self._ledger is not None else config.V3_DIR / "skill-ledger"))
        links = tuple(Path(os.path.abspath(p)) for p in (consumers if self._links is None else self._links))
        owners = (config.REPO_ROOT / "skills", *consumers) if self._owners is None else self._owners
        owners = dict.fromkeys([*(Path(os.path.abspath(p)) for p in owners), *links])
        with ExitStack() as stack:
            try:
                root_fd = stack.enter_context(open_directory(root, create=create))
                if create:
                    _private_root(root_fd)
                else:
                    _private(os.fstat(root_fd))
                lock_flags = (os.O_CREAT | os.O_RDWR) if create else os.O_RDONLY
                lock_fd = _open_file(root_fd, ".store.lock", lock_flags)
            except FileNotFoundError:
                if not readonly:
                    raise
                # The owner may have skills before a learned store exists.
                yield _Operation(root, None, ledger, None, None, links, owners)
                return
            stack.callback(os.close, lock_fd)
            lock_mode = fcntl.LOCK_SH if readonly else fcntl.LOCK_EX
            self._acquire_lock(lock_fd, lock_mode, nonblocking=nonblocking_read and not initialize)
            _verify(root_fd, ".store.lock", os.fstat(lock_fd))
            if readonly:
                yield _Operation(root, root_fd, ledger, None, lock_fd, links, owners)
                return
            ledger_fd = stack.enter_context(open_directory(ledger, create=create))
            if create:
                _private_root(ledger_fd)
            else:
                _private(os.fstat(ledger_fd))
            op = _Operation(root, root_fd, ledger, ledger_fd, lock_fd, links, owners)
            if initialize:
                op.recover_links()
            yield op

    def records(self, *, with_status=False):
        with self._operation() as op:
            rows = op.records(visible_only=True)
            if with_status:
                return [dict(row, status=op.status(row)) for row in rows]
            return rows

    def names(self, *, readonly=False):
        with self._operation(readonly=readonly) as op:
            names = []
            if op.root_fd is None:
                return names
            for name in sorted(os.listdir(op.root_fd)):
                try:
                    _name(name)
                    with ExitStack() as stack:
                        skill = op.learned(stack, name)
                        if skill is not None and op.owner(name) is None:
                            _header(_read(skill, _MAIN).decode(), name)
                            names.append(name)
                except (OSError, ValueError):
                    continue
            return names

    def skill_dir(self, name):
        """Return the conventional path. Store I/O always uses pinned descriptors."""
        _name(name)
        return self._root_path() / name

    def _view(self, name, path, views, *, readonly=False):
        _leaf(name)
        parts = _path(path)
        with self._operation(readonly=readonly) as op, ExitStack() as stack:
            owner = op.owner(name, keep=stack)
            if owner is not None:
                parent, entry = owner
                if is_credential_name(entry):
                    raise StoreError("No such skill file.")
                skill = _directory(stack, parent, entry, private=False)
                folder = op.folder(stack, skill, parts, private=False)
                if folder is None:
                    raise StoreError("No such skill file.")
                return redact.for_model_strict(_read(folder, parts[-1], private=False,
                                                     limit=_OWNER_READ_LIMIT).decode())
            _name(name)
            skill = op.learned(stack, name, required=True)
            revision = op.revision(skill)
            folder = op.folder(stack, skill, parts)
            if folder is None:
                raise StoreError("No such skill file.")
            text = redact.for_model_strict(_read(folder, parts[-1]).decode())
            op.verify_skill(name, skill, revision)
            if readonly:
                return text
            previous, seen = views.get(name, (None, set()))
            views[name] = (revision, (seen if previous == revision else set()) | {path})
            if path == _MAIN:
                with suppress(OSError, ValueError, RecursionError):
                    op.record_use(name)
            return text

    def _manage(self, action, name, views, actor, session, tainted, fields):
        _name(name)
        if actor not in ("reviewer", "foreground") or not isinstance(session, str) or type(tainted) is not bool:
            raise StoreError("Invalid skill session.")
        with self._operation() as op:
            return op.manage(action, name, views, actor, session, tainted, fields)

    def pin(self, name, on):
        return self._set_flag(name, "pinned", on)

    def lock(self, name, on):
        return self._set_flag(name, "locked", on)

    def _set_flag(self, name, flag, on):
        _name(name)
        if type(on) is not bool:
            raise StoreError("Expected a boolean flag.")
        with self._operation() as op, ExitStack() as stack:
            op.require_unowned(name)
            skill = op.learned(stack, name, required=True)
            revision = op.revision(skill)
            before = _read(skill, _MAIN)
            text = before.decode()
            _, fields = _header(text, name)
            value = str(on).lower()
            if fields[flag] == value:
                return "Skill flag is already in the requested state."
            after = text.replace(f"\n{flag}: {fields[flag]}\n", f"\n{flag}: {value}\n", 1).encode()
            return op.change("patch", name, _MAIN, before, after, skill, revision)

    def rollback(self, id, *, actor="owner"):
        if not isinstance(id, str) or not _ID.fullmatch(id) or actor != "owner":
            raise StoreError("Unknown skill ledger record.")
        with self._operation() as op:
            return op.rollback(id)


class _Operation:
    def __init__(self, root, root_fd, ledger, ledger_fd, lock_fd, links, owners):
        self.root = root
        self.root_fd = root_fd
        self.ledger = ledger
        self.ledger_fd = ledger_fd
        self.lock_fd = lock_fd
        self.links = links
        self.owners = owners

    def _ours(self, fd, name, *, entry=None):
        try:
            return os.readlink(entry or name, dir_fd=fd) == str(self.root / name)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOENT):
                return False
            raise

    def owner(self, name, *, keep=None):
        folded = name.casefold()
        for path in self.owners:
            with ExitStack() as stack:
                try:
                    fd = stack.enter_context(open_directory(path))
                except FileNotFoundError:
                    continue
                entries = os.listdir(fd)
                if path in self.links and name in entries and self._ours(fd, name):
                    entries.remove(name)
                entry = next((entry for entry in entries if entry.casefold() == folded), None)
                if entry is None:
                    entry = next((entry for entry in entries if self._declares(fd, entry, folded)), None)
                if entry is not None:
                    if keep is not None:
                        keep.callback(stack.pop_all().close)
                    return fd, entry
        return None

    @staticmethod
    def _declares(root, entry, folded):
        if is_credential_name(entry):
            return False
        try:
            with ExitStack() as stack:
                _leaf(entry)
                skill = _directory(stack, root, entry, private=False)
                text = _read(skill, _MAIN, private=False, limit=_OWNER_READ_LIMIT, prefix=True)
                _verify(root, entry, os.fstat(skill))
        except (OSError, ValueError):
            return False
        # A large body can end mid-character in the bounded read.
        declared = metadata(text.decode(errors="surrogateescape")).get("name", "")
        # A YAML comment after the name is not part of it.
        return re.split(r"\s+#", declared, maxsplit=1)[0].strip().strip("\"'").casefold() == folded

    def require_unowned(self, name):
        if self.owner(name) is not None:
            raise StoreError("This name belongs to an owner skill.")

    def learned(self, stack, name, *, required=False):
        try:
            if self.root_fd is None:
                raise FileNotFoundError
            return _directory(stack, self.root_fd, name)
        except FileNotFoundError:
            if required:
                raise StoreError("No live learned skill with that name.") from None
            return None

    @staticmethod
    def folder(stack, skill, parts, *, private=True):
        if len(parts) == 1:
            return skill
        try:
            return _directory(stack, skill, parts[0], private=private)
        except FileNotFoundError:
            return None

    @staticmethod
    def _file_stamp(fd, name):
        info = _stat(fd, name)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StoreError("Skill storage requires regular files with one link.")
        _private(info)
        return _stamp(info)

    def revision(self, skill):
        if set(os.listdir(skill)) - {_MAIN, *_AUX}:
            raise StoreError("The learned skill contains an unsupported entry.")
        stamps = {_MAIN: self._file_stamp(skill, _MAIN)}
        for folder in _AUX:
            with ExitStack() as stack:
                fd = self.folder(stack, skill, (folder, ""))
                if fd is None:
                    continue
                stamps[folder] = _stamp(os.fstat(fd))
                for entry in os.listdir(fd):
                    _leaf(entry)
                    stamps[f"{folder}/{entry}"] = self._file_stamp(fd, entry)
        return _stamp(os.fstat(skill)), stamps

    def verify_skill(self, name, skill, revision):
        _verify(self.root_fd, name, os.fstat(skill))
        if self.revision(skill) != revision:
            raise StoreError("The skill changed. View it again before editing.")

    def read_sidecar(self, name, *, ledger=False, limit=_STATE_LIMIT):
        fd = self.ledger_fd if ledger else self.root_fd
        info = _stat(fd, name)
        if info is None:
            return None, None
        if info.st_uid != os.getuid():
            raise StoreError("Skill state must belong to the owner.")
        # Older or manually created usage data may have the owner's default
        # mode. It is inside a private directory and the next write sets 0600.
        data = _read(fd, name, private=name != _USAGE, limit=limit)
        _verify(fd, name, info)
        return data, info

    def write_sidecar(self, name, data, expected, *, ledger=False, limit=_STATE_LIMIT):
        if len(data) > limit:
            raise StoreError("The skill state exceeds its size limit.")
        fd = self.ledger_fd if ledger else self.root_fd
        self._verify_storage()
        _verify(fd, name, expected)
        _atomic(fd, name, data)
        return _stat(fd, name)

    def record_use(self, name):
        data, expected = self.read_sidecar(_USAGE)
        try:
            rows = {} if data is None else _usage(data)
        except (ValueError, RecursionError):
            rows = {}
        previous = rows.get(name)
        now = time.time()
        rows[name] = ({"count": previous["count"] + 1, "last_used": max(now, previous["last_used"])}
                      if previous else {"count": 1, "last_used": now})
        self.write_sidecar(_USAGE, _json(rows).encode(), expected)

    def records(self, *, strict=False, visible_only=False, limit=None):
        try:
            fd = _open_file(self.ledger_fd, "ledger.jsonl")
        except FileNotFoundError:
            return []
        rows = {}
        with os.fdopen(fd, "rb") as file:
            initial = os.fstat(fd)
            torn_tail = False
            stream = file
            if limit is not None:
                if initial.st_size > limit:
                    raise StoreError("The skill ledger exceeds its read limit.")
                data = file.read(limit + 1)
                if len(data) > limit:
                    raise StoreError("The skill ledger exceeds its read limit.")
                stream = BytesIO(data)
            lines = iter(lambda: stream.readline(_STATE_LIMIT), b"") if strict else stream
            for line in lines:
                if not line.endswith(b"\n"):
                    if strict:
                        raise StoreError("Incomplete skill ledger record.")
                    break
                if strict and line.endswith(_TORN_END):
                    # The writer seals interrupted lines without modifying
                    # their bytes. A later valid row confirms append recovered.
                    torn_tail = True
                    continue
                try:
                    row = json.loads(line.decode())
                    if _valid_record(row):
                        rid = row["id"]
                        if strict and rid in rows:
                            raise StoreError("Duplicate skill ledger record.")
                        rows[rid] = None if rid in rows else row
                        torn_tail = False
                        continue
                except (ValueError, RecursionError):
                    if strict:
                        raise StoreError("Invalid skill ledger record.") from None
                if strict:
                    raise StoreError("Invalid skill ledger record.")
            if strict:
                if torn_tail:
                    raise StoreError("Incomplete skill ledger recovery.")
                if _stamp(initial) != _stamp(os.fstat(fd)):
                    raise StoreError("The skill ledger changed while being read.")
                _verify(self.ledger_fd, "ledger.jsonl", initial)
        return [row for row in rows.values()
                if row is not None and (not visible_only or _visible_record(row))]

    def _append(self, row):
        self._verify_storage()
        with os.fdopen(_open_file(self.ledger_fd, "ledger.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT), "rb") as file:
            size = file.seek(0, os.SEEK_END)
            prefix = b""
            if size:
                file.seek(-1, os.SEEK_END)
                if file.read(1) != b"\n":
                    # Preserve torn bytes. A missing newline alone must never
                    # let a subsequent append turn a torn record into a valid one.
                    prefix = _TORN_END
            _write(file.fileno(), prefix + _json(row).encode() + b"\n")
        os.fsync(self.ledger_fd)

    @staticmethod
    def _blob(blobs, sha):
        try:
            data = _read(blobs, sha)
        except FileNotFoundError:
            raise StoreError("Missing skill ledger blob.") from None
        if _digest(data) != sha:
            raise StoreError("Skill ledger blob failed its hash check.")
        return data

    def _save_blobs(self, stack, before, after):
        blobs = _directory(stack, self.ledger_fd, "blobs", create=True)
        for data in (before, after):
            if data is None:
                continue
            sha = _digest(data)
            if _stat(blobs, sha) is None:
                _atomic(blobs, sha, data)
            else:
                self._blob(blobs, sha)

    def _verify_storage(self):
        _verify_directory(self.root, self.root_fd)
        _verify_directory(self.ledger, self.ledger_fd)
        _verify(self.root_fd, ".store.lock", os.fstat(self.lock_fd))

    def _unlink_owned(self, fd, name, existing):
        _verify(fd, name, existing)
        # Claim the entry before unlinking. If a concurrent owner rename won
        # the race, put the claimed entry back without overwriting its slot.
        claimed = ".learned-link-" + uuid4().hex
        _rename_new(fd, name, fd, claimed)
        if self._ours(fd, name, entry=claimed):
            os.unlink(claimed, dir_fd=fd)
        else:
            _rename_new(fd, claimed, fd, name)
            return False
        return True

    def _sync_links(self, name, *, live, make=True):
        """`make`: create a consumer folder that is not there (a write does; a read does not)."""
        if live and not self._linkable(name):
            return " Consumer links need attention."
        errors = False
        for path in self.links:
            try:
                with open_directory(path, create=live and make) as fd:
                    _verify_directory(self.root, self.root_fd)
                    _verify_directory(path, fd)
                    existing = _stat(fd, name)
                    if live and existing is None:
                        os.symlink(str(self.root / name), name, dir_fd=fd)
                        os.fsync(fd)
                    elif existing is not None and self._ours(fd, name):
                        if not live:
                            errors |= not self._unlink_owned(fd, name, existing)
                            os.fsync(fd)
                    elif live:
                        errors = True
            except FileNotFoundError:
                errors |= live
            except (OSError, StoreError):
                errors = True
        return " Consumer links need attention." if errors else ""

    def _linkable(self, name):
        try:
            _name(name)
            with ExitStack() as stack:
                skill = self.learned(stack, name, required=True)
                _header(_read(skill, _MAIN).decode(), name)
                _verify(self.root_fd, name, os.fstat(skill))
        except (OSError, ValueError):
            return False
        return True

    def recover_links(self):
        # Both sides can be reconstructed after a crash. An empty or staged
        # directory doesn't qualify as a live skill, and only exact links
        # count as ours when removing entries from a consumer.
        live = {name for name in os.listdir(self.root_fd) if self._linkable(name)}
        missing, stale, held = set(), set(), set()
        for path in self.links:
            try:
                with open_directory(path) as fd:
                    entries = set(os.listdir(fd))
                    missing.update(live - entries)
                    held.update(live & entries)
                    stale.update(name for name in entries - live
                                 if _SLUG.fullmatch(name) and self._ours(fd, name))
            except FileNotFoundError:
                missing.update(live)
            except OSError:
                continue        # an unreadable consumer is left alone; a read never fails for it
        for name in sorted(stale):
            self._sync_links(name, live=False)
        for name in sorted(missing):
            try:
                owned = self.owner(name) is not None
            except OSError:
                owned = True    # cannot tell whose name it is: link nothing
            if not owned:
                # No consumer has it: the create was interrupted. Otherwise a missing folder was removed on purpose.
                self._sync_links(name, live=True, make=name not in held)

    def _quarantine(self, stack, row, parts, data):
        pending = _directory(stack, self.root_fd, ".pending", create=True)
        skill = _directory(stack, pending, row["skill"], create=True)
        verdicts = _directory(stack, skill, ".verdicts", create=True)
        _atomic(verdicts, row["id"] + ".json", _json(row).encode())
        folder = skill if len(parts) == 1 else _directory(stack, skill, parts[0], create=True)
        _atomic(folder, parts[-1], data)

    def _stage(self, stack, txn, parts, skill, folder, after, snapshot):
        if skill is None or folder is None:
            staged = _directory(stack, txn, "change", create=True)
            if skill is None:
                for path, data in snapshot.items():
                    if path in _AUX:
                        with ExitStack() as copy:
                            _directory(copy, staged, path, create=True)
                        continue
                    target = _path(path)
                    with ExitStack() as copy:
                        dest = staged if len(target) == 1 else _directory(copy, staged, target[0], create=True)
                        _atomic(dest, target[-1], data)
            else:
                _atomic(staged, parts[-1], after)
        else:
            _atomic(txn, "change", after)

    def _publish(self, name, skill, revision, source, entry, expected, dest, target, after):
        # Recheck after the blob, staging and ledger I/O, with the same pinned
        # descriptors. A new destination also gets a kernel no-replace rename.
        self._verify_storage()
        self.require_unowned(name)
        if skill is None:
            _verify(self.root_fd, name, None)
        else:
            self.verify_skill(name, skill, revision)
        if after is None:
            _verify(source, entry, expected)
        if after is None or expected is None:
            _rename_new(source, entry, dest, target)
        else:
            _verify(dest, target, expected)
            os.replace(entry, target, src_dir_fd=source, dst_dir_fd=dest)
        os.fsync(dest)
        if source != dest:
            os.fsync(source)

    def change(self, action, name, path, before, after, skill, revision, *,
               actor="owner", session="", tainted=False, snapshot=None, restored_from=None,
               timestamp=None, record_id=None):
        if actor not in _ACTORS or (actor == "curator" and action != "archive"):
            raise StoreError("Invalid skill ledger actor.")
        parts = _path(path)
        _scan_metadata(name, path, session)
        verdict = (_scan_file(after.decode(), name, path) if after is not None
                   else content_scan.Verdict(())).to_json()
        row = {"id": record_id or uuid4().hex,
               "ts": timestamp or datetime.now(timezone.utc).isoformat(), "op": action,
               "skill": name, "path": path, "before": _digest(before) if before is not None else None,
               "after": _digest(after) if after is not None else None, "actor": actor,
               "session": session, "tainted": tainted, "scan": verdict}
        rid = row["id"]
        with ExitStack() as stack:
            self._save_blobs(stack, before, after)
            if not verdict["ok"]:
                self._append(row)
                self._quarantine(stack, row, parts, after)
                rules = ", ".join(sorted({hit["rule"] for hit in verdict["hits"]}))
                raise StoreError(f"Skill change quarantined in .pending ({rules}).")

            txns = _directory(stack, self.root_fd, ".transactions", create=True)
            os.mkdir(rid, 0o700, dir_fd=txns)
            os.fsync(txns)
            txn = _directory(stack, txns, rid)
            folder = self.folder(stack, skill, parts) if skill is not None else None
            if after is None:
                dest = _directory(stack, self.root_fd, ".archive", create=True)
                source, entry = (self.root_fd, name) if path == _MAIN else (folder, parts[-1])
                expected = _stat(source, entry)
                target = rid
                if path == _MAIN:
                    files = self._snapshot(skill, revision, before)
                    _atomic(txn, "tree.json", _json(self._manifest(files)).encode())
            else:
                if skill is None and snapshot is None:
                    snapshot = {_MAIN: after}
                self._stage(stack, txn, parts, skill, folder, after, snapshot)
                source, entry = txn, "change"
                if skill is None:
                    dest, target = self.root_fd, name
                elif folder is None:
                    dest, target = skill, parts[0]
                else:
                    dest, target = folder, parts[-1]
                expected = _stat(dest, target)
            # The intent binds the receipt to the exact JSON record. An install
            # consumes its staged inode; a removal creates its archive entry.
            # These witnesses change in the live rename itself, so a crash just
            # before or after it is distinguishable without a second commit write.
            intent = _digest(_json(row).encode())
            if restored_from is not None:
                intent += "\n" + restored_from
            _atomic(txn, "intent", intent.encode())
            self._append(row)
            self._publish(name, skill, revision, source, entry, expected, dest, target, after)
            warning = self._sync_links(name, live=path != _MAIN or after is not None)
        flagged = " Flagged for review." if tainted or verdict["hits"] else ""
        return f"Skill change saved.{flagged}{warning}"

    def manage(self, action, name, views, actor, session, tainted, fields):
        allowed = {
            "create": ({"description", "body"}, set()), "patch": ({"old", "new"}, {"path"}),
            "write_file": ({"path", "content"}, set()), "remove_file": ({"path"}, set()),
            "archive": (set(), set()),
        }
        if not isinstance(action, str) or action not in allowed:
            raise StoreError("Unknown skill action.")
        required, optional = allowed[action]
        if not required <= fields.keys() <= required | optional:
            raise StoreError("Missing or unexpected arguments for the skill action.")
        path = fields.get("path", _MAIN)
        parts = _path(path, main=action not in ("write_file", "remove_file"))
        self.require_unowned(name)
        with ExitStack() as stack:
            skill = self.learned(stack, name, required=action != "create")
            if action == "create":
                if skill is not None:
                    raise StoreError("A learned skill already has that name.")
                description = _description(fields["description"])
                body = _text(fields["body"], required=True)
                after = (f"---\nname: {_scalar(name)}\ndescription: {_scalar(description)}\ncreated_by: agent\n"
                         f"pinned: false\nlocked: false\n---\n{body}")
                return self.change(action, name, path, None, after.encode(), skill, None,
                                   actor=actor, session=session, tainted=tainted)
            return self._edit(stack, skill, action, name, path, parts, views, actor, session, tainted, fields)

    def _edit(self, stack, skill, action, name, path, parts, views, actor, session, tainted, fields):
        revision = self.revision(skill)
        main_text = _read(skill, _MAIN).decode()
        header, flags = _header(main_text, name)
        if flags["locked"] == "true":
            raise StoreError("The learned skill is locked.")
        if action == "archive" and flags["pinned"] == "true":
            raise StoreError("The learned skill is pinned.")
        previous, seen = views.get(name, (None, set()))
        if action in ("patch", "write_file", "remove_file"):
            if previous is None or (action == "patch" and path not in seen):
                raise StoreError("First view the skill file in this session.")
            if revision != previous:
                raise StoreError("The skill changed. View it again before editing.")
        folder = self.folder(stack, skill, parts)
        before = _read(folder, parts[-1]) if path in revision[1] else None
        after = None
        if action == "patch":
            old = _text(fields["old"], limit=MAX_BYTES + 2048)
            new = _text(fields["new"], limit=MAX_BYTES + 2048)
            text = before.decode()
            pos = text.find(old) if old else -1
            if pos < 0 or text.find(old, pos + 1) >= 0:
                raise StoreError("The patch must match exactly once.")
            text = text[:pos] + new + text[pos + len(old):]
            if path == _MAIN:
                new_header, _ = _header(text, name)
                if ([line for line in header if not line.startswith("description: ")]
                        != [line for line in new_header if not line.startswith("description: ")]):
                    raise StoreError("A patch may change only the description field in frontmatter.")
            else:
                _text(text)
            after = text.encode()
        elif action == "write_file":
            if before is None and sum("/" in entry for entry in revision[1]) >= MAX_FILES:
                raise StoreError("The skill has reached its reference and template file limit.")
            after = _text(fields["content"]).encode()
        elif before is None:
            raise StoreError("No such skill file.")
        if before == after:
            return "Skill file is unchanged."
        result = self.change(action, name, path, before, after, skill, revision,
                             actor=actor, session=session, tainted=tainted)
        if action == "archive":
            views.pop(name, None)
        else:
            views[name] = (self.revision(skill), seen if after is not None else seen - {path})
        return result

    def _receipt(self, stack, row):
        """Return the archive source ID, an empty string for other applied changes, or None."""
        try:
            txns = _directory(stack, self.root_fd, ".transactions")
            txn = _directory(stack, txns, row["id"])
            intent, sep, restored_from = _read(txn, "intent").partition(b"\n")
            if intent != _digest(_json(row).encode()).encode():
                return None
            if sep and (row["op"] != "rollback" or row["path"] != _MAIN
                        or row["before"] is not None or not _ID.fullmatch(restored_from.decode())):
                return None
            if row["after"] is None:
                archive = _directory(stack, self.root_fd, ".archive")
                applied = _stat(archive, row["id"]) is not None
            else:
                applied = _stat(txn, "change") is None
            return restored_from.decode() if applied else None
        except (FileNotFoundError, UnicodeError):
            return None

    def status(self, row):
        if not row["scan"]["ok"]:
            return "blocked"
        with ExitStack() as stack:
            return "applied" if self._receipt(stack, row) is not None else "unapplied"

    def _same_life(self, rows, row):
        # Applied creates start lives. A restore carries the archive's life
        # forward, and an unapplied intent never changes the active life.
        life = recorded = None
        archives = {}
        for change in rows:
            if change["skill"] != row["skill"] or not change["scan"]["ok"]:
                continue
            rid = change["id"]
            if change["path"] == _MAIN and None in (change["before"], change["after"]):
                with ExitStack() as stack:
                    restored_from = self._receipt(stack, change)
                if restored_from is None:
                    continue
                if change["after"] is None:
                    archives[rid] = (life, change["before"])
                elif restored_from:
                    source = archives.get(restored_from)
                    life = source[0] if source and source[1] == change["after"] else rid
                elif change["op"] == "rollback":
                    # Old restore receipts lack the source ID. Inherit a life
                    # only if the restored bytes identify it unambiguously.
                    matches = {origin for origin, sha in archives.values() if sha == change["after"]}
                    life = matches.pop() if len(matches) == 1 else rid
                else:
                    life = rid
            if rid == row["id"]:
                recorded = life
        return recorded is not None and recorded == life

    def _snapshot(self, skill, revision, main):
        files = {_MAIN: main}
        for path in revision[1]:
            if path in _AUX:
                files[path] = None
                continue
            if path == _MAIN:
                continue
            _scan_metadata(path)
            parts = _path(path)
            with ExitStack() as child:
                folder = self.folder(child, skill, parts)
                if folder is None:
                    raise StoreError("A skill folder changed while being read.")
                files[path] = _read(folder, parts[-1], limit=MAX_BYTES)
        if sum("/" in path for path in files) > MAX_FILES:
            raise StoreError("The archived skill exceeds its file limit.")
        return files

    @staticmethod
    def _manifest(files):
        return {path: _digest(data) if data is not None else None for path, data in files.items()}

    def _archive_snapshot(self, stack, row, main):
        archive = _directory(stack, self.root_fd, ".archive")
        snapshot = _directory(stack, archive, row["id"])
        txns = _directory(stack, self.root_fd, ".transactions")
        txn = _directory(stack, txns, row["id"])
        expected = json.loads(_read(txn, "tree.json"))
        revision = self.revision(snapshot)
        files = self._snapshot(snapshot, revision, _read(snapshot, _MAIN))
        _verify(archive, row["id"], os.fstat(snapshot))
        if self.revision(snapshot) != revision or self._manifest(files) != expected or files[_MAIN] != main:
            raise StoreError("Archived skill failed its hash check.")
        for path, data in files.items():
            if path != _MAIN and data is not None and not content_scan.scan(data.decode(), purpose="skill").ok:
                raise StoreError("An archived skill file failed its scan.")
        return files

    def rollback(self, rid):
        rows = self.records()
        row = next((row for row in rows if row["id"] == rid), None)
        with ExitStack() as stack:
            if row is None or self.status(row) != "applied":
                raise StoreError("No applied live skill change for that ledger ID.")
            if not self._same_life(rows, row):
                raise StoreError("The ledger change belongs to a different life of this skill.")
            name, path = row["skill"], row["path"]
            self.require_unowned(name)
            parts = _path(path)
            skill = self.learned(stack, name)
            revision = self.revision(skill) if skill is not None else None
            folder = self.folder(stack, skill, parts) if skill is not None else None
            current = _read(folder, parts[-1]) if folder is not None and _stat(folder, parts[-1]) is not None else None
            if (current is None and path != _MAIN and skill is None
                    or (_digest(current) if current is not None else None) != row["after"]):
                raise StoreError("The live skill differs from the ledger change.")
            previous = None
            snapshot = None
            if row["before"] is not None:
                blobs = _directory(stack, self.ledger_fd, "blobs")
                previous = self._blob(blobs, row["before"])
                if path == _MAIN:
                    _header(previous.decode(), name)
                    if skill is None:
                        snapshot = self._archive_snapshot(stack, row, previous)
                elif current is None and sum("/" in entry for entry in revision[1]) >= MAX_FILES:
                    raise StoreError("The skill has reached its reference and template file limit.")
            return self.change("rollback", name, path, current, previous, skill, revision,
                               snapshot=snapshot, restored_from=rid if snapshot is not None else None)
