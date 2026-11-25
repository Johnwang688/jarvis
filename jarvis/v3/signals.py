"""Private, deterministic snapshots for the daemon's trigger inputs."""

from contextlib import suppress
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from jarvis.untrusted import strip_invisible
from jarvis.v2.redact import for_model
from ._paths import open_directory


_SOURCE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_MAX_READ = 8 * 1024 * 1024
_NO_LINK = os.O_NOFOLLOW | os.O_NONBLOCK
_TITLE_CHARS = 512
_TEXT_CHARS = 4096
_GOOGLE_OAUTH = re.compile(r"(?:ya29\.|1//)[A-Za-z0-9_-]{8,}")
_COOKIE_HEADER = re.compile(
    r"""(?i)(?<![\w-])((?:set-)?cookie["']?[ \t]*:)[ \t]*["']?"""
    r"[!#$%&'*+.^_`|~0-9a-z-]+[ \t]*=[^\r\n\v\f\x85\u2028\u2029]*"
)


def scrub(value, *, secret=None):
    """Scrub strings throughout JSON data, including object keys."""
    if isinstance(value, str):
        text = strip_invisible(value)
        if secret:
            text = text.replace(secret, "[withheld: OAuth token]")
        text = _COOKIE_HEADER.sub(r"\1 [withheld: cookie]", text)
        return for_model(_GOOGLE_OAUTH.sub("[withheld: OAuth token]", text))
    if isinstance(value, dict):
        return {scrub(key, secret=secret): scrub(item, secret=secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, secret=secret) for item in value]
    return value


def _bound(value, limit=_TEXT_CHARS):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {key: _bound(item, _TITLE_CHARS if key == "title" else _TEXT_CHARS)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_bound(item, limit) for item in value]
    return value


def _location(source, directory):
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValueError("Invalid signal source.")
    if directory is None:
        from jarvis import config

        directory = config.V3_SIGNALS_DIR
    return Path(directory), f"{source}.json"


def _ids(items):
    ids = set()
    for item in items:
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item_id, str) or len(item_id) > _TEXT_CHARS or item_id in ids:
            raise ValueError("Invalid or duplicate signal item ID.")
        ids.add(item_id)


def write(source, items, *, directory=None, **extra):
    directory, name = _location(source, directory)
    extra = _bound(scrub(extra))
    if {"version", "source", "items"}.intersection(extra):
        raise ValueError("Reserved signal field.")
    items = list(items)
    _ids(items)
    items = scrub(items)
    _ids(items)
    items = _bound(items)
    items.sort(key=lambda item: item["id"])
    payload = {"version": 1, "source": source, **extra, "items": items}
    data = (json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(data) > _MAX_READ:
        raise ValueError("Signal snapshot exceeds its size limit.")
    with open_directory(directory, create=True) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not info.st_mode & stat.S_IWUSR:
            raise PermissionError("Signal directory is not writable by its owner.")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        temp = f".signals-{uuid4().hex}.tmp"
        output = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_LINK, 0o600, dir_fd=fd)
        try:
            with os.fdopen(output, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=fd)
    return directory / name


def read(source, *, directory=None):
    directory, name = _location(source, directory)
    try:
        with open_directory(directory) as fd:
            with os.fdopen(os.open(name, os.O_RDONLY | _NO_LINK, dir_fd=fd), "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.getuid() or info.st_nlink != 1
                        or info.st_size > _MAX_READ):
                    return None
                data = stream.read(_MAX_READ + 1)
        if len(data) > _MAX_READ:
            return None
        payload = json.loads(data, parse_constant=_invalid_constant)
        if (isinstance(payload, dict) and type(payload.get("version")) is int
                and payload["version"] == 1 and payload.get("source") == source
                and isinstance(payload.get("items"), list)):
            _ids(payload["items"])
            payload = scrub(payload)
            _ids(payload["items"])
            return _bound(payload)
    except (OSError, ValueError, RecursionError):
        return None
    return None


def _invalid_constant(_):
    raise ValueError("Non-finite signal value.")
