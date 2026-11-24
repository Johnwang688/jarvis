"""Owner CLI for the user-scope Claude and Codex brain registrations."""

from contextlib import ExitStack, suppress
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import tomllib
from uuid import uuid4

from jarvis import config

from .index import open_directory


_SERVER = "jarvis-brain"
_BEGIN = "# BEGIN jarvis-brain MCP"
_END = "# END jarvis-brain MCP"
_MARKERS = re.compile(rf"^(?:{re.escape(_BEGIN)}|{re.escape(_END)})(?:\r?\n|\Z)", re.M)
_BACKUP = re.compile(r"\d{20}-[0-9a-f]{32}-(missing|[0-9a-f]{64})-([0-9a-f]{64})")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class _Refused(ValueError):
    pass


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    mode: int
    identity: tuple


@dataclass(frozen=True)
class _Target:
    path: Path
    directory: int
    before: _Snapshot | None
    table: str
    text: str
    parsed: dict


@dataclass(frozen=True)
class _Edit:
    target: _Target
    after: bytes | None


def _registration():
    repo = config.REPO_ROOT
    python = repo / ".venv/bin/python"
    # Keep the venv's symlink in the command so Python can find its environment.
    if not python.is_file():
        python = Path(sys.executable).absolute()
    return {"command": str(python), "args": ["-m", "jarvis", "brain", "mcp"],
            "env": {"PYTHONPATH": str(repo)}}


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(directory, name):
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise _Refused("Expected an owner-controlled regular file with one link.")
        data = stream.read()
        identity = _identity(info)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if identity != _identity(os.fstat(fd)) or identity != _identity(current):
            raise _Refused("File changed while it was being read.")
        return _Snapshot(data, stat.S_IMODE(info.st_mode), identity)


def _verify(target):
    # Confirm that the pinned directory still represents the requested path.
    with open_directory(target.path.parent) as current:
        old, new = os.fstat(target.directory), os.fstat(current)
        if (old.st_dev, old.st_ino) != (new.st_dev, new.st_ino):
            raise _Refused("Config directory changed during the operation.")
        if _read(current, target.path.name) != target.before:
            raise _Refused("Config changed during the operation.")


def _new_file(directory, name, data, mode):
    fd = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(fd, mode)
            os.fsync(fd)
    except BaseException:
        _remove_temp(directory, name)
        raise


def _remove_temp(directory, name):
    with suppress(FileNotFoundError):
        os.unlink(name, dir_fd=directory)


def _digest(data):
    return sha256(data).hexdigest()


def _backup_prefix(target):
    return target.path.name + ".bak-"


def _backup(target, after, mode):
    before = target.before
    prior_hash = "missing" if before is None else _digest(before.data)
    # The filename is the receipt. It binds the backup to the exact bytes
    # on both sides of the install, independent of Jarvis's data directory.
    stamp = f"{time.time_ns():020}-{uuid4().hex}-{prior_hash}-{_digest(after)}"
    _new_file(target.directory, _backup_prefix(target) + stamp,
              b"" if before is None else before.data, mode)
    os.fsync(target.directory)


def _backups(target):
    prefix = _backup_prefix(target)
    for name in sorted(os.listdir(target.directory), reverse=True):
        if name.startswith(prefix) and (match := _BACKUP.fullmatch(name[len(prefix):])):
            yield name, *match.groups()


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _Refused("Config contains duplicate JSON keys.")
    return obj


def _bad_constant(_value):
    raise _Refused("Config contains an invalid JSON constant.")


def _parse(text, table):
    if table == "mcpServers":
        data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    else:
        data = tomllib.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get(table, {}), dict):
        raise _Refused("Config and its MCP server settings must be objects.")
    return data


_JSON_SPACE = re.compile(r"[ \t\r\n]*")
_JSON_DECODER = json.JSONDecoder()


def _skip_space(text, offset):
    return _JSON_SPACE.match(text, offset).end()


def _json_members(text, start):
    """Locate members in an object that has already been validated."""
    members = {}
    pos = _skip_space(text, start + 1)
    while text[pos] != "}":
        key, end = _JSON_DECODER.raw_decode(text, pos)
        value = _skip_space(text, _skip_space(text, end) + 1)
        _, end = _JSON_DECODER.raw_decode(text, value)
        members[key] = (pos, value, end)
        pos = _skip_space(text, end)
        if text[pos] == ",":
            pos = _skip_space(text, pos + 1)
    return members


def _json_append(text, start, members, key, value):
    field = f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
    if not members:
        pos = start + 1
        return text[:pos] + field + text[pos:]
    key_start, _, end = next(reversed(members.values()))
    line_start = text.rfind("\n", 0, key_start) + 1
    indent = text[line_start:key_start]
    space = _newline(text) + indent if not indent.strip() else " "
    return text[:end] + "," + space + field + text[end:]


def _claude_edit(target, entry):
    text = target.text
    root_start = _skip_space(text, 0)
    root = _json_members(text, root_start)
    if target.table not in root:
        return _json_append(text, root_start, root, target.table, {_SERVER: entry})
    _, start, _ = root[target.table]
    members = _json_members(text, start)
    if _SERVER not in members:
        return _json_append(text, start, members, _SERVER, entry)
    key_start, value_start, end = members[_SERVER]
    if entry is not None:
        return text[:value_start] + json.dumps(entry, ensure_ascii=False) + text[end:]
    keys = list(members)
    i = keys.index(_SERVER)
    if i:
        key_start = members[keys[i - 1]][2]
    elif len(keys) > 1:
        end = members[keys[1]][0]
    return text[:key_start] + text[end:]


def _newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _toml_string(text):
    # JSON's basic string escapes are also TOML escapes. Keep Unicode
    # scalars intact: TOML rejects JSON's escaped UTF-16 surrogate pairs.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", r"\u007f")


def _codex_block(entry, newline):
    args = ", ".join(_toml_string(arg) for arg in entry["args"])
    return newline.join((
        _BEGIN, f"[mcp_servers.{_SERVER}]",
        f"command = {_toml_string(entry['command'])}", f"args = [{args}]",
        f"[mcp_servers.{_SERVER}.env]", f"PYTHONPATH = {_toml_string(entry['env']['PYTHONPATH'])}",
        _END, "",
    ))


def _codex_span(text):
    marks = list(_MARKERS.finditer(text))
    if not marks:
        return None
    if (len(marks) != 2 or not marks[0].group().startswith(_BEGIN)
            or not marks[1].group().startswith(_END)):
        raise _Refused("Jarvis's Codex block markers differ from the installed markers.")
    return marks[0].start(), marks[1].end()


def _codex_edit(target, entry):
    text = target.text
    span = _codex_span(text)
    has_entry = _SERVER in target.parsed.get(target.table, {})
    if bool(span) != has_entry:
        raise _Refused("Codex has a Jarvis entry or block without its matching markers.")
    newline = _newline(text)
    if span is None:
        separator = newline if text and not text.endswith("\n") else ""
        return text + separator + _codex_block(entry, newline)
    start, end = span
    # Prove the marked bytes are the registration, even if a marker occurs
    # inside a multiline TOML value or someone edited the block by hand.
    without = text[:start] + text[end:]
    _check_edit(target, without, None)
    block = "" if entry is None else _codex_block(entry, newline)
    return text[:start] + block + text[end:]


def _without_entry(data, table):
    outer = dict(data)
    servers = dict(outer.pop(table, {}))
    servers.pop(_SERVER, None)
    return outer, servers


def _check_edit(target, edited, entry):
    parsed = _parse(edited, target.table)
    servers = parsed.get(target.table, {})
    expected = _without_entry(target.parsed, target.table)
    actual = _without_entry(parsed, target.table)
    # Preserve unrelated TOML NaN settings, which do not compare equal.
    if (actual != expected and repr(actual) != repr(expected)
            or (entry is None and _SERVER in servers)
            or (entry is not None and servers.get(_SERVER) != entry)):
        raise _Refused("The edit would change settings outside the Jarvis registration.")


def _restore(target):
    digest = _digest(target.before.data)
    for name, prior_hash, installed_hash in _backups(target):
        if digest != installed_hash:
            continue
        backup = _read(target.directory, name)
        if backup is None:
            continue
        if prior_hash == "missing" and not backup.data:
            return True, None
        if _digest(backup.data) != prior_hash:
            raise _Refused("Backup bytes differ from the recorded original.")
        before = _parse(backup.data.decode("utf-8"), target.table)
        # A repair backup can contain a drifted Jarvis entry. Uninstall must
        # remove the registration rather than reinstate that entry.
        if _SERVER not in before.get(target.table, {}):
            return True, backup.data
    return False, None


def _target(stack, home, path):
    claude = path.name == ".claude.json"
    table = "mcpServers" if claude else "mcp_servers"
    try:
        directory = stack.enter_context(open_directory(path.parent))
    except FileNotFoundError:
        return None
    before = _read(directory, path.name)
    if claude and before is None:
        try:
            with open_directory(home / ".claude"):
                pass
        except FileNotFoundError:
            return None
    text = before.data.decode("utf-8") if before is not None else ("{}\n" if claude else "")
    return _Target(path, directory, before, table, text, _parse(text, table))


def _plan(action, target, registration, out):
    servers = target.parsed.get(target.table, {})
    present = _SERVER in servers
    matches = servers.get(_SERVER) == registration
    codex = target.table == "mcp_servers"
    if codex and (present or _codex_span(target.text)):
        # Even a matching entry needs a removable, owned block.
        _codex_edit(target, None)
    if action == "status":
        detail = ("installed and unchanged" if matches else
                  "registration differs from install" if present else "not installed")
        out(f"{target.path}: {detail}.")
        return matches
    if (action == "install" and matches) or (action == "uninstall" and not present):
        out(f"{target.path}: {'already installed' if matches else 'already absent'}.")
        return None
    if not codex and present and not matches and not any(_backups(target)):
        raise _Refused("Existing Claude registration differs and has no Jarvis backup.")
    if action == "uninstall":
        exact, before = _restore(target)
        if exact:
            return _Edit(target, before)
    entry = registration if action == "install" else None
    text = _codex_edit(target, entry) if codex else _claude_edit(target, entry)
    _check_edit(target, text, entry)
    return _Edit(target, text.encode("utf-8"))


def _apply(action, edits, snapshots, stack, out):
    staged = []
    for edit in edits:
        target = edit.target
        mode = target.before.mode if target.before is not None else 0o600
        temp = None
        if edit.after is not None:
            temp = f".{target.path.name}.jarvis-{uuid4().hex}"
            _new_file(target.directory, temp, edit.after, mode)
            stack.callback(_remove_temp, target.directory, temp)
        staged.append((edit, temp, mode))
    for snapshot in snapshots:
        _verify(snapshot)
    for edit, temp, mode in staged:
        target = edit.target
        if action == "install":
            _backup(target, edit.after, mode)
        # Check again after staging and fsync, directly before publication.
        _verify(target)
        if temp is None:
            os.unlink(target.path.name, dir_fd=target.directory)
        else:
            os.replace(temp, target.path.name, src_dir_fd=target.directory, dst_dir_fd=target.directory)
        out(f"{target.path}: {'installed' if action == 'install' else 'uninstalled'} jarvis-brain.")
        os.fsync(target.directory)


def _run(action, home, out):
    home = Path.home() if home is None else Path(home)
    path = home
    try:
        with ExitStack() as stack:
            home_fd = stack.enter_context(open_directory(home))
            # Serialize Jarvis installers. The clients do not take this lock.
            fcntl.flock(home_fd, fcntl.LOCK_SH if action == "status" else fcntl.LOCK_EX)
            registration = _registration()
            plans, snapshots = [], []
            for path in (home / ".claude.json", home / ".codex/config.toml"):
                target = _target(stack, home, path)
                if target is None:
                    out(f"{path}: consumer absent, skipped.")
                    continue
                snapshots.append(target)
                result = _plan(action, target, registration, out)
                if result is not None:
                    plans.append(result)
            if action == "status":
                return int(not plans or not all(plans))
            _apply(action, plans, snapshots, stack, out)
            return 0
    except _Refused as exc:
        out(f"jarvis brain {action}: {exc}")
    except (OSError, ValueError, RecursionError):
        out(f"jarvis brain {action}: could not safely use the consumer configs.")
    return 1


def install(*, home=None, out=print):
    return _run("install", home, out)


def status(*, home=None, out=print):
    return _run("status", home, out)


def uninstall(*, home=None, out=print):
    return _run("uninstall", home, out)
