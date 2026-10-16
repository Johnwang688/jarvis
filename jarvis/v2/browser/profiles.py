"""Private profile metadata; labels never name paths (BR-1c).

A row may carry `"password_store": "keyring"`: that profile's Chrome has
been launched with the owner's keyring, and is never launched with the basic
store again (`password_store.py`). Once set it is never cleared; deleting
the profile removes it with the row.

One store per host serializes atomic metadata updates. Chrome lifetimes and
filesystem removal belong to the host, which excludes new opens while deleting.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import unicodedata
from datetime import datetime, timezone

from jarvis import config, untrusted

PROFILE_ID = re.compile(r"p-[0-9a-f]{8}")
MAX_PROFILES = 8
MAX_BYTES = 64 * 1024
_KEYS = frozenset({"id", "label", "created"})
STICKY_KEY, STICKY_VALUE = "password_store", "keyring"


def _refuse(status: int, sentence: str):
    from .host import BrowserRefused
    raise BrowserRefused(status, sentence)


def label(value) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 32
            or value != value.strip() or unicodedata.normalize("NFC", value) != value
            or any(not c.isprintable() or untrusted.unseen(c) for c in value)):
        _refuse(400, "a profile label must be 1 to 32 visible characters on one line")
    if value.casefold() == "private":
        _refuse(400, "Private is reserved for off-the-record browsing")
    return value


def profile_id(value) -> str:
    if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
        _refuse(400, "a profile id is p- followed by eight lowercase hex characters")
    return value


class ProfileStore:
    def __init__(self):
        self._lock = threading.RLock()

    @staticmethod
    def _root(*, create: bool) -> Path | None:
        from .host import secure_dir
        root = config.BROWSER_DIR
        if not create and not os.path.lexists(root):
            return None
        secure_dir(root, create=create)
        for name in ("profiles", "downloads"):
            path = root / name
            if create or os.path.lexists(path):
                secure_dir(path, create=create)
        return root

    def _read(self, *, create: bool = False) -> list[dict]:
        root = self._root(create=create)
        if root is None:
            return []
        path = root / "profiles.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if not create:
                return []
            rows = [{"id": "p-" + secrets.token_hex(4), "label": "Main",
                     "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
            self._write(rows)
            return rows
        except OSError:
            _refuse(409, "profiles.json is unreadable")
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    _refuse(409, "profiles.json must be a private regular file owned by you")
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError()
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > MAX_PROFILES:
                raise ValueError()
            ids, labels = set(), set()
            for row in rows:
                if not isinstance(row, dict) or set(row) - {STICKY_KEY} != _KEYS:
                    raise ValueError()
                if STICKY_KEY in row and row[STICKY_KEY] != STICKY_VALUE:
                    raise ValueError()
                if not isinstance(row["id"], str) or not PROFILE_ID.fullmatch(row["id"]):
                    raise ValueError()
                try:
                    label(row["label"])
                except Exception:
                    raise ValueError() from None
                if (not isinstance(row["created"], str) or len(row["created"]) > 64
                        or datetime.fromisoformat(row["created"]).tzinfo is None):
                    raise ValueError()
                if row["id"] in ids or row["label"].casefold() in labels:
                    raise ValueError()
                ids.add(row["id"]); labels.add(row["label"].casefold())
        except (OSError, ValueError, TypeError, RecursionError):
            _refuse(409, "profiles.json is unreadable")
        return rows

    def _write(self, rows):
        from .host import _write_private
        root = self._root(create=True)
        try:
            _write_private(root / "profiles.json", (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode())
        except OSError:
            _refuse(409, "the profile list could not be saved")

    def list(self, *, create: bool = False) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._read(create=create)]

    def require(self, value) -> dict:
        identity = profile_id(value)
        with self._lock:
            row = next((r for r in self._read() if r["id"] == identity), None)
            if row is None:
                _refuse(404, "no such profile")
            return dict(row)

    def create(self, value) -> dict:
        name = label(value)
        with self._lock:
            rows = self._read(create=True)
            if len(rows) >= MAX_PROFILES:
                _refuse(409, "the Browser already has eight profiles; delete one first")
            if any(r["label"].casefold() == name.casefold() for r in rows):
                _refuse(409, "a profile with that label already exists")
            taken = {r["id"] for r in rows}
            while (identity := "p-" + secrets.token_hex(4)) in taken:
                pass
            row = {"id": identity, "label": name,
                   "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._write([*rows, row])
            return dict(row)

    def rename(self, value, name) -> dict:
        identity, name = profile_id(value), label(name)
        with self._lock:
            rows = self._read()
            row = next((r for r in rows if r["id"] == identity), None)
            if row is None:
                _refuse(404, "no such profile")
            if any(r["id"] != identity and r["label"].casefold() == name.casefold() for r in rows):
                _refuse(409, "a profile with that label already exists")
            row["label"] = name
            self._write(rows)
            return dict(row)

    def mark_keyring(self, value) -> dict:
        """Record that this profile's Chrome uses the keyring, for good.
        `BrowserRefused` when it cannot be saved: the caller then does not
        launch."""
        identity = profile_id(value)
        with self._lock:
            rows = self._read()
            row = next((r for r in rows if r["id"] == identity), None)
            if row is None:
                _refuse(404, "no such profile")
            if row.get(STICKY_KEY) != STICKY_VALUE:
                row[STICKY_KEY] = STICKY_VALUE
                self._write(rows)
            return dict(row)

    def delete(self, value) -> None:
        identity = profile_id(value)
        with self._lock:
            rows = self._read()
            if not any(r["id"] == identity for r in rows):
                _refuse(404, "no such profile")
            self._write([r for r in rows if r["id"] != identity])
