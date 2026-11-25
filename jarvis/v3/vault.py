"""The private, file-backed v3 memory vault."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import date
import fcntl
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import stat
from typing import NamedTuple
from uuid import uuid4

from jarvis import config

from . import content_scan
from .index import open_directory


USER_CAP = 3_000
ENTRY_CAP = 4_000
FACTS_CAP = 400
FOLDERS = {
    "people": "person", "projects": "project", "preferences": "preference",
    "style": "style", "facts": "fact",
}
_ROOT_ENTRIES = {"USER": "person", "commitments": "commitment"}
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_METADATA_LIMITS = {"description": 240, "source": 500}
_REQUIRED = {"name", "kind", "description", "source", "updated", "status"}
_KEYS = _REQUIRED | {"replaced_by"}
_LINE_BREAK_OR_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
_PLAIN_START = re.compile(r"[A-Za-z]")
_YAML_SYNTAX = re.compile(r"\s#|:(?:\s|$)|[\[\]{},]")
_YAML_WORDS = {"y", "yes", "n", "no", "on", "off", "true", "false", "null"}
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_FILE_BYTES = 4 * 64 * 1024
_REF_SCAN_LIMIT = 16_000
_REF_HEADER_LIMIT = 8 * 1024


class VaultError(ValueError):
    """A vault error whose text is safe to return to a tool caller."""


class _DirectoryModeError(VaultError):
    """An owner-owned vault directory needs its permissions tightened."""


class Location(NamedTuple):
    folder: str
    name: str

    @property
    def ref(self) -> str:
        if self.folder and self.name:
            return f"{self.folder}/{self.name}"
        return self.folder or self.name

    @property
    def filename(self) -> str:
        return self.name + ".md"

    @property
    def kind(self) -> str:
        return FOLDERS[self.folder] if self.folder else _ROOT_ENTRIES[self.name]


@dataclass(frozen=True)
class Entry:
    location: Location
    description: str
    source: str
    body: str
    updated: str
    status: str = "active"
    replaced_by: str = ""
    revision: bytes | None = None
    header: tuple[tuple[str, str, str], ...] = ()     # (contract key or "", the line, its value)
    newline: str = "\n"
    closing: str = ""         # the hand-made closing line, kept as it was

    def markdown(self) -> str:
        fields = {
            "name": self.location.name, "kind": self.location.kind,
            "description": self.description, "source": self.source,
            "updated": self.updated, "status": self.status,
        }
        if self.replaced_by:
            fields["replaced_by"] = self.replaced_by
        lines = ["---" + self.newline]
        for key, raw, value in self.header:
            if not key:
                lines.append(raw)
            elif key in fields:
                new = fields.pop(key)
                ending = "\r\n" if raw.endswith("\r\n") else "\n"
                lines.append(raw if new == value else f"{key}: {_written(key, new)}{ending}")
        lines.extend(f"{key}: {_written(key, value)}{self.newline}" for key, value in fields.items())
        return "".join([*lines, self.closing or "---" + self.newline, self.body])

    def scan_markdown(self, text: str, *, purpose: content_scan.Purpose = "memory") -> tuple[str, ...]:
        rules = _scan(text, purpose=purpose)
        for value in (self.description, self.source):
            if purpose == "consolidation-memory" or _scalar(value) not in (value, f'"{value}"'):
                # Scan standalone directives and values altered by JSON escaping.
                rules += _scan(value, purpose=purpose)
        return tuple(dict.fromkeys(rules))


def _scalar(value: str) -> str:
    if (_PLAIN_START.match(value) and value == value.rstrip()
            and not _YAML_SYNTAX.search(value) and value.lower() not in _YAML_WORDS):
        return value
    return json.dumps(value, ensure_ascii=False)


def _written(key: str, value: str) -> str:
    return value if key in ("kind", "updated", "status") else _scalar(value)


def _field_value(value: str) -> str:
    if value.startswith('"'):
        # A hand-made value that is not one JSON string (a comment after it, say) is read as it stands.
        with suppress(ValueError):
            value = json.loads(value)
    if not value.strip() or _LINE_BREAK_OR_CONTROL.search(value):
        raise VaultError("Invalid memory frontmatter.")
    return value


def _slug(value) -> bool:
    return isinstance(value, str) and len(value) <= 80 and _SLUG.fullmatch(value) is not None


def _metadata(key: str, value) -> None:
    if (not isinstance(value, str) or len(value) > _METADATA_LIMITS[key]
            or not value.strip() or _LINE_BREAK_OR_CONTROL.search(value)):
        raise VaultError(f"{key.capitalize()} must be a short, nonempty single line.")


def _body(body, *, allow_empty: bool = False) -> None:
    if not isinstance(body, str) or "\0" in body or (not allow_empty and not body.strip()):
        raise VaultError("Memory body must be text without NUL characters and must contain content.")


def body_cap(location: Location) -> int:
    return USER_CAP if location.name == "USER" else ENTRY_CAP


def _new_entry(folder, name, description, body, source):
    if not isinstance(folder, str) or folder not in FOLDERS:
        raise VaultError("Use a known memory folder.")
    if not _slug(name):
        raise VaultError("Use a short kebab-case memory name.")
    _metadata("description", description)
    _metadata("source", source)
    _body(body)
    return Entry(Location(folder, name), description, source, body, date.today().isoformat())


def _patch_body(entry, old, new):
    if not isinstance(old, str) or not isinstance(new, str):
        raise VaultError("Old and new patch values must be text.")
    pos = entry.body.find(old)
    if old:
        if pos < 0 or entry.body.find(old, pos + 1) >= 0:
            raise VaultError("Patch text must occur exactly once in the memory body.")
    elif entry.location.folder or entry.body:
        raise VaultError("Empty patch text is only valid for an empty profile or commitments.")
    return entry.body[:pos] + new + entry.body[pos + len(old):], pos


@contextmanager
def _writable(write_scope):
    with (write_scope() if write_scope is not None else nullcontext(True)) as allowed:
        if not allowed:
            raise VaultError("The chat turn is stopping.")
        yield


def _parse(location: Location, raw: bytes, info: os.stat_result) -> Entry:
    if len(raw) > _MAX_FILE_BYTES:
        raise VaultError("Memory file is too large to read.")
    stream = StringIO(raw.decode("utf-8"), newline="\n")
    opening = stream.readline()
    if opening not in ("---\n", "---\r\n"):
        raise VaultError("Invalid memory frontmatter.")
    newline = opening[3:]
    fields = {}
    layout = []
    for original in stream:
        line = original.removesuffix("\r\n").removesuffix("\n")
        if line == "---":
            closing = original if original.endswith("\n") else original + newline
            break
        key, _, tail = line.partition(":")
        if key.rstrip() not in _KEYS:
            layout.append(("", original, ""))
            continue
        if key not in _KEYS or key in fields or not tail.startswith(" "):
            raise VaultError("Invalid memory frontmatter.")
        fields[key] = _field_value(tail[1:])
        layout.append((key, original, fields[key]))
    else:
        raise VaultError("Invalid memory frontmatter.")
    if (_REQUIRED - fields.keys() or fields["kind"] != location.kind
            or fields["status"] not in ("active", "archived")):
        raise VaultError("Invalid memory frontmatter.")
    if "replaced_by" in fields and (fields["status"] != "archived" or not _slug(fields["replaced_by"])):
        raise VaultError("Invalid memory replacement reference.")
    try:
        if not _DATE.fullmatch(fields["updated"]):
            raise ValueError
        date.fromisoformat(fields["updated"])
    except ValueError:
        raise VaultError("Invalid memory date.") from None
    for key in _METADATA_LIMITS:
        _metadata(key, fields[key])
    rest = stream.read()
    _body(rest, allow_empty=not location.folder)
    return Entry(location, fields["description"], fields["source"], rest, fields["updated"],
                 fields["status"], fields.get("replaced_by", ""), _revision(raw, info), tuple(layout), newline, closing)


def _is_archived(raw: bytes) -> bool:
    # Inspect status independently of the other fields, which may be damaged.
    opening, sep, rest = raw.partition(b"\n")
    if not sep or opening not in (b"---", b"---\r"):
        return False
    for line in rest.split(b"\n"):
        line = line.removesuffix(b"\r")
        if line == b"---":
            break
        if line.startswith(b"status: "):
            with suppress(UnicodeError, VaultError):
                if _field_value(line[8:].decode("utf-8")) == "archived":
                    return True
    return False


def _archived_header(directory, filename):
    """Check only the status in a bounded frontmatter scan."""
    with _regular_file(directory, filename, private=False) as fd:
        # Read through the frontmatter delimiter without prefetching the body.
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as stream:
            opening = stream.readline(5)
            if opening not in (b"---\n", b"---\r\n"):
                return False
            remaining = _REF_HEADER_LIMIT - len(opening)
            while remaining:
                line = stream.readline(remaining)
                if not line or line.rstrip(b"\r\n") == b"---":
                    break
                remaining -= len(line)
                if line.startswith(b"status: "):
                    with suppress(UnicodeError, VaultError):
                        if _field_value(line[8:].rstrip(b"\r\n").decode("utf-8")) == "archived":
                            return True
    return False


def _revision(raw: bytes, info: os.stat_result) -> bytes:
    # Change time also distinguishes reused inodes and in-place edits that restore the old bytes.
    identity = f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:".encode("ascii")
    return hashlib.sha256(identity + raw).digest()


def _scan(text: str, *, purpose: content_scan.Purpose = "memory") -> tuple[str, ...]:
    verdict = content_scan.scan(text, purpose=purpose)
    if not verdict.ok:
        rules = ", ".join(sorted({hit.rule for hit in verdict.hits if hit.severity == "block"}))
        raise VaultError(f"Memory scanner blocked content ({rules}).")
    return tuple(dict.fromkeys(hit.rule for hit in verdict.hits if hit.severity == "flag"))


def safe_summary(text: str) -> str:
    try:
        _scan(text)
    except VaultError:
        return "[withheld by memory scanner]"
    return text


@contextmanager
def _directory(parent: int, name: str, *, create: bool = False, tighten: bool = False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _private_directory(fd, f"Vault folder {name}", tighten=create or tighten)
        yield fd
    finally:
        os.close(fd)


def _private_directory(fd: int, label: str, *, tighten: bool) -> None:
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        raise VaultError("Vault directories must belong to the vault owner.")
    if stat.S_IMODE(info.st_mode) != 0o700:
        if not tighten:
            raise _DirectoryModeError(f"{label} must have mode 0700.")
        os.fchmod(fd, 0o700)


@contextmanager
def _regular_file(directory: int, name: str, flags: int = os.O_RDONLY, *, private: bool = True):
    fd = os.open(name, flags | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise VaultError("Memory files must be owner-owned regular files with one link.")
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise VaultError("Vault files must have mode 0600.")
        yield fd
    finally:
        os.close(fd)


def _read_bytes(fd: int) -> bytes:
    with os.fdopen(fd, "rb", closefd=False) as stream:
        return stream.read(_MAX_FILE_BYTES + 1)


def _read_file(directory: int, name: str, *, private: bool = True,
               allow_prefix: bool = False) -> tuple[bytes, os.stat_result]:
    # An owner's editor leaves 0644 behind; the 0700 folder keeps it private, and the next write makes it 0600.
    with _regular_file(directory, name, private=private) as fd:
        info = os.fstat(fd)
        raw = _read_bytes(fd)
    if not allow_prefix and len(raw) > _MAX_FILE_BYTES:
        raise VaultError("Memory file is too large to read.")
    return raw, info


def _check_revision(directory: int, filename: str, expected: bytes | None) -> bytes | None:
    try:
        if expected is None:
            os.stat(filename, dir_fd=directory, follow_symlinks=False)
        else:
            with _regular_file(directory, filename, private=False) as fd:
                before = os.fstat(fd)
                raw = _read_bytes(fd)
                if len(raw) <= _MAX_FILE_BYTES and _revision(raw, before) == expected:
                    after = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                    if (before.st_dev == after.st_dev and before.st_ino == after.st_ino
                            and before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
                            and before.st_ctime_ns == after.st_ctime_ns):
                        return raw
    except FileNotFoundError:
        if expected is None:
            return
    except (OSError, VaultError):
        pass
    raise VaultError("Memory entry changed during the write. Read it again.")


def _atomic_replace(directory: int, filename: str, raw: bytes, expected: bytes | None, *,
                    write_scope=None) -> bytes:
    tmp = ".entry-" + uuid4().hex
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            _check_revision(directory, filename, expected)
            with _writable(write_scope):
                os.replace(tmp, filename, src_dir_fd=directory, dst_dir_fd=directory)
                # The rename can change ctime. Use the same file descriptor after the rename.
                return _revision(raw, os.fstat(stream.fileno()))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp, dir_fd=directory)
        raise


def _log_flags(root: int, flags: list[dict[str, str]]) -> None:
    filename = ".flags.jsonl"
    try:
        os.stat(filename, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        new_log = True
    else:
        new_log = False
    payload = "".join(json.dumps(flag) + "\n" for flag in flags).encode()
    mode = os.O_WRONLY | os.O_APPEND
    if new_log:
        mode |= os.O_CREAT | os.O_EXCL
    with _regular_file(root, filename, mode) as log:
        size = os.fstat(log).st_size
        try:
            with os.fdopen(log, "ab", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(log)
            if new_log:
                os.fsync(root)
        except OSError:
            with suppress(OSError):
                if new_log:
                    os.unlink(filename, dir_fd=root)
                    os.fsync(root)
                else:
                    os.ftruncate(log, size)
                    os.fsync(log)
            raise


class Vault:
    def __init__(self, root=None):
        if root is None:
            root = os.environ.get("JARVIS_VAULT") or Path(config.V3_DIR) / "vault"
        self.root = Path(root).expanduser().absolute()

    @contextmanager
    def _locked(self, *, write: bool = False):
        with open_directory(self.root, create=write) as root:
            fcntl.flock(root, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            _private_directory(root, "Vault root", tighten=write)
            yield root

    @contextmanager
    def _folder(self, root: int, folder: str, *, create: bool = False, write: bool = False):
        if folder:
            with _directory(root, folder, create=create, tighten=write) as directory:
                yield directory
        else:
            yield root

    def _load(self, root: int, location: Location, *, write: bool = False) -> Entry | None:
        try:
            with self._folder(root, location.folder, write=write) as directory:
                raw, info = _read_file(directory, location.filename, private=False)
        except FileNotFoundError:
            if location.folder:
                return None
            return self._initial(location)
        return _parse(location, raw, info)

    @staticmethod
    def _initial(location: Location) -> Entry:
        description = "Owner profile" if location.name == "USER" else "Open commitments"
        return Entry(location, description, "owner", "", date.today().isoformat())

    def _resolve(self, root: int, ref: str, *, write: bool = False, pending_archive: bool = False) -> Entry:
        if not isinstance(ref, str):
            raise VaultError("Invalid memory name.")
        if ref in _ROOT_ENTRIES:
            entry = self._load(root, Location("", ref))
            if entry.status == "active" or pending_archive:
                return entry
            raise VaultError("Memory entry was not found.")
        folder, slash, name = ref.partition("/")
        if not slash:
            name = folder
        if not _slug(name) or (slash and folder not in FOLDERS):
            raise VaultError("Use a memory slug or folder/slug.")
        matches = []
        mode_error = None
        for parent in (folder,) if slash else FOLDERS:
            try:
                entry = self._load(root, Location(parent, name), write=write)
            except _DirectoryModeError as exc:
                if slash:
                    raise
                mode_error = mode_error or exc
                continue
            if entry is not None and (entry.status == "active" or pending_archive):
                matches.append(entry)
        if not matches:
            if mode_error is not None:
                raise mode_error
            raise VaultError("Memory entry was not found.")
        if len(matches) > 1:
            refs = safe_summary(", ".join(entry.location.ref for entry in matches))
            raise VaultError(f"Ambiguous memory name. Choose from {refs}.")
        return matches[0]

    def _rows(self, root: int, folder: str, *, refs_only=False):
        budget = iter(range(_REF_SCAN_LIMIT))

        def list_refs(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    if next(budget, None) is None:
                        raise OverflowError("The vault exceeds its inventory scan limit.")
                    yield entry.name

        if not folder:
            for name in _ROOT_ENTRIES:
                location = Location("", name)
                yield from self._row(root, location, refs_only=refs_only)
        for parent in (folder,) if folder else FOLDERS:
            try:
                with self._folder(root, parent) as directory:
                    for filename in sorted(list_refs(directory) if refs_only else os.listdir(directory)):
                        name = filename.removesuffix(".md")
                        if name != filename and _slug(name):
                            yield from self._row(directory, Location(parent, name), refs_only=refs_only)
            except FileNotFoundError:
                continue
            except _DirectoryModeError:
                if folder:
                    raise
                yield Location(parent, ""), None
            except (OSError, VaultError):
                if folder:
                    raise
                if refs_only:
                    yield Location(parent, ""), None

    @staticmethod
    def _row(directory: int, location: Location, *, refs_only=False):
        try:
            if refs_only:
                if _archived_header(directory, location.filename):
                    return
                entry = None
            else:
                raw, info = _read_file(directory, location.filename, private=False, allow_prefix=True)
                if _is_archived(raw):
                    return
                entry = _parse(location, raw, info)
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, VaultError):
            yield location, None
        else:
            yield location, entry

    def entries(self, folder: str = "", *, refs_only=False) -> list[tuple[Location, Entry | None]]:
        """Return active entries and locations of unreadable entries or wrong-mode folders.

        With refs_only, inspect only frontmatter status and leave each Entry unset.
        """
        if folder != "" and (not isinstance(folder, str) or folder not in FOLDERS):
            raise VaultError("Unknown memory folder.")
        try:
            with self._locked() as root:
                return list(self._rows(root, folder, refs_only=refs_only))
        except FileNotFoundError:
            return []

    def read(self, name: str) -> Entry:
        try:
            with self._locked() as root:
                entry = self._resolve(root, name)
        except FileNotFoundError:
            if isinstance(name, str) and name in _ROOT_ENTRIES:
                entry = self._initial(Location("", name))
            else:
                raise VaultError("Memory entry was not found.") from None
        entry.scan_markdown(entry.markdown())
        return entry

    def check_add_target(self, location: Location) -> None:
        """Check for an absent destination without creating vault directories."""
        try:
            with self._locked() as root:
                if self._load(root, location) is not None:
                    raise VaultError("The destination memory entry already exists.")
        except FileNotFoundError:
            pass

    def _check_caps(self, root: int, entry: Entry, *, adding: bool) -> None:
        cap = body_cap(entry.location)
        over_chars = len(entry.body) - cap
        over_entries = 0
        if adding and entry.location.folder == "facts":
            over_entries = sum(1 for _ in self._rows(root, "facts")) + 1 - FACTS_CAP
        if over_chars > 0 or over_entries > 0:
            lines = []
            if over_chars > 0:
                lines.append(f"Free {over_chars} characters to fit the body cap of {cap}.")
            if over_entries > 0:
                lines.append(f"Free {over_entries} active entries from facts to fit the cap of {FACTS_CAP}.")
            lines.append("Current entries:")
            lines.extend(entry_summary(location, row) for location, row in self._rows(root, ""))
            raise VaultError("\n".join(lines))

    @staticmethod
    def _save(root: int, directory: int, entry: Entry, raw: bytes, rules: tuple[str, ...],
              archive_existing: bool = False, write_scope=None):
        location = entry.location
        expected = entry.revision
        flags = [{"name": location.ref, "rule": rule} for rule in rules]
        if flags:
            _check_revision(directory, location.filename, expected)
            with _writable(write_scope):
                _log_flags(root, flags)
        if archive_existing:
            with _directory(directory, ".archive", create=True) as archive:
                Vault._finish_archive(directory, archive, location, expected, write_scope=write_scope)
            expected = None
        revision = _atomic_replace(directory, location.filename, raw, expected, write_scope=write_scope)
        return replace(entry, revision=revision), flags

    def add(self, folder: str, name: str, description: str, body: str, source: str, *,
            purpose: content_scan.Purpose = "memory", require_absent=False, write_scope=None):
        entry = _new_entry(folder, name, description, body, source)
        with self._locked(write=True) as root:
            return self._add(root, entry, purpose=purpose, require_absent=require_absent,
                             write_scope=write_scope)

    def _add(self, root: int, entry: Entry, *, importing: bool = False,
             purpose: content_scan.Purpose = "memory", require_absent=False, write_scope=None):
        location = entry.location
        existing = self._load(root, location, write=True)
        if require_absent and existing is not None:
            raise VaultError("The destination memory entry changed after approval.")
        if existing is not None and (existing.status == "active" or importing):
            raise VaultError(f"Memory entry {safe_summary(location.ref)} already exists.")
        self._check_caps(root, entry, adding=True)
        text = entry.markdown()
        rules = entry.scan_markdown(text, purpose=purpose)
        raw = text.encode("utf-8")
        with self._folder(root, location.folder, create=True) as directory:
            if importing and self._was_archived(directory, location):
                raise VaultError("Memory entry was previously archived.")
            if existing is not None:
                entry = replace(entry, revision=existing.revision)
            return self._save(root, directory, entry, raw, rules, existing is not None, write_scope)

    def patch(self, name: str, old: str, new: str, reads: dict[Location, bytes | None], *,
              purpose: content_scan.Purpose = "memory", review_patch: bool = False,
              expected: tuple[Location, bytes | None] | None = None, write_scope=None):
        with self._locked(write=True) as root:
            entry = self._resolve(root, name, write=True)
            location = entry.location
            if expected is not None and (location, entry.revision) != expected:
                raise VaultError("Memory entry changed after approval. Read it again before patching.")
            if location not in reads:
                raise VaultError("Read the memory entry in this session before patching.")
            if reads[location] != entry.revision:
                raise VaultError("Memory entry changed since the read. Read it again before patching.")
            body, pos = _patch_body(entry, old, new)
            old_end = pos + len(old)
            if purpose == "consolidation-memory" and location == Location("", "commitments"):
                _keep_commitments_block(entry.body, body)
            _body(body, allow_empty=not location.folder)
            before = entry.body
            entry = replace(entry, body=body, updated=date.today().isoformat())
            self._check_caps(root, entry, adding=False)
            text = entry.markdown()
            strict_patch = review_patch or purpose == "consolidation-memory"
            rules = entry.scan_markdown(text, purpose="memory" if strict_patch else purpose)
            if strict_patch and content_scan.review_patch_creates_directive(before, body, pos, old_end, pos + len(new)):
                raise VaultError("Memory scanner blocked content (agent-override).")
            raw = text.encode("utf-8")
            with self._folder(root, location.folder) as directory:
                return self._save(root, directory, entry, raw, rules, write_scope=write_scope)

    def rewrite(self, name: str, transform, *, purpose: content_scan.Purpose):
        """Transform a root entry's current body under the vault write lock."""
        if not isinstance(name, str) or name not in _ROOT_ENTRIES:
            raise VaultError("Only a fixed vault entry can be rewritten.")
        with self._locked(write=True) as root:
            entry = self._resolve(root, name, write=True)
            body = transform(entry.body)
            if body == entry.body:
                return entry, []
            _body(body, allow_empty=True)
            entry = replace(entry, body=body, updated=date.today().isoformat())
            self._check_caps(root, entry, adding=False)
            text = entry.markdown()
            rules = entry.scan_markdown(text, purpose=purpose)
            return self._save(root, root, entry, text.encode("utf-8"), rules)

    @staticmethod
    def _finish_archive(directory: int, archive: int, location: Location, expected: bytes, *,
                        write_scope=None) -> None:
        filename = f"{location.name}.{uuid4().hex}.md"
        _check_revision(directory, location.filename, expected)
        with _writable(write_scope):
            os.rename(location.filename, filename, src_dir_fd=directory, dst_dir_fd=archive)

    @staticmethod
    def _was_archived(directory: int, location: Location) -> bool:
        try:
            with _directory(directory, ".archive", tighten=True) as archive:
                return any(filename == location.filename or filename.startswith(location.name + ".")
                           for filename in os.listdir(archive))
        except FileNotFoundError:
            return False

    def archive(self, name: str, replaced_by: str = "", *,
                expected: tuple[Location, bytes | None] | None = None, write_scope=None):
        if not isinstance(replaced_by, str):
            raise VaultError("Invalid replacement name.")
        with self._locked(write=True) as root:
            entry = self._resolve(root, name, write=True, pending_archive=True)
            location = entry.location
            if expected is not None and (location, entry.revision) != expected:
                raise VaultError("Memory entry changed after approval. Review it again before archiving.")
            if not location.folder:
                raise VaultError("This fixed vault entry cannot be archived.")
            already_archived = entry.status == "archived"
            if not already_archived:
                replacement = self._resolve(root, replaced_by, write=True).location if replaced_by else None
                if replacement == location:
                    raise VaultError("Choose a different active replacement.")
                entry = replace(entry, status="archived", updated=date.today().isoformat(),
                                replaced_by=replacement.name if replacement else "")
            self._check_caps(root, entry, adding=False)
            text = entry.markdown()
            rules = entry.scan_markdown(text)
            raw = text.encode("utf-8")
            with self._folder(root, location.folder) as directory:
                with _directory(directory, ".archive", create=True) as archive:
                    if already_archived and _check_revision(directory, location.filename, entry.revision) == raw:
                        self._finish_archive(directory, archive, location, entry.revision, write_scope=write_scope)
                        return entry, []
                    entry, flags = self._save(root, directory, entry, raw, rules, write_scope=write_scope)
                    # The persisted status already hides the entry. A retry can
                    # finish moving it if the filesystem or an editor intervenes.
                    with suppress(OSError, VaultError):
                        self._finish_archive(directory, archive, location, entry.revision, write_scope=write_scope)
                    return entry, flags

    def import_memory(self, source_dir) -> tuple[list[str], list[str]]:
        """Copy legacy Markdown files with owner provenance. Return added refs and skip reasons."""
        added, skipped = [], []
        with open_directory(Path(source_dir).expanduser()) as source:
            with self._locked(write=True) as root:
                for filename in sorted(os.listdir(source)):
                    if filename.startswith(".") or not filename.endswith(".md"):
                        continue
                    name = re.sub(r"[^a-z0-9]+", "-", filename[:-3].lower()).strip("-")[:80].rstrip("-")
                    try:
                        if not _slug(name):
                            raise VaultError("Invalid legacy filename.")
                        raw, _ = _read_file(source, filename, private=False)
                        body = raw.decode("utf-8")
                        _body(body)
                        description = next(line.strip().lstrip("# ").strip()
                                           for line in body.splitlines() if line.strip().lstrip("# ").strip())
                        description = description[:_METADATA_LIMITS["description"]]
                        _metadata("description", description)
                        entry = Entry(Location("facts", name), description, "owner", body,
                                      date.today().isoformat())
                        self._add(root, entry, importing=True)
                    except VaultError as exc:
                        skipped.append(str(exc).split("\n", 1)[0])
                    except (OSError, UnicodeError, StopIteration):
                        skipped.append("Legacy file could not be read.")
                    else:
                        added.append(entry.location.ref)
        return added, skipped


def _keep_commitments_block(before: str, after: str) -> None:
    """Foreground agents and the owner may still edit the block by hand.

    A malformed block makes every later consolidation tool write refuse until
    it is fixed by hand. The tools never silently repair or drop the block.
    """
    from .commitments import CommitmentError, block_text

    try:
        if block_text(before) == block_text(after):
            return
    except CommitmentError:
        pass
    raise VaultError("Use the commitment tools to edit the commitments table. Repair malformed blocks by hand.")


def entry_summary(location: Location, entry: Entry | None) -> str:
    if not location.name:
        return f"Vault folder {location.folder} must have mode 0700."
    if entry is None:
        return f"{safe_summary(location.ref)} (unreadable entry)"
    label = safe_summary(f"{location.ref} — {entry.description}")
    return f"{label} ({len(entry.body)} chars)"
