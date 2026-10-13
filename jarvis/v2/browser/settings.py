"""The Browser's settings: `browser.json` (BR-1a, BR-8a; plan §4, amendment 1
§D.2 and §F). **The one reader and the one writer of that file**: nothing
else opens it (the engine, the gate, the routes, the terminals and `/status`
all ask the daemon's `BrowserSettings`), and `devurl_check`'s
`test_one_reader_one_writer` fails if another module names its path.

One small JSON object at `config.BROWSER_SETTINGS_PATH`
(`~/.config/jarvis/browser.json`, env `JARVIS_BROWSER_SETTINGS`). It holds the
owner's switches for the HUD's Browser, each a boolean:

- **`lan`** (B-5, BR-1a): the Browser's gate admits private-network addresses.
  **Off** by default.
- **`local_servers`** (BR-1a): the gate admits every loopback port that is not
  Jarvis's, not only the ones the owner opened in a pane. **Off** by default:
  the owner's switch, never a default.
- **`auto_open`** (BR-8a, decisions B-2): "Open dev servers automatically" —
  the daemon reads a HUD terminal's live output for a dev server's
  announcement (`devurl.py`) and tells the window showing that terminal. **On**
  by default. Auto-open loads only in a Direct frame and never opens a port in
  the Remote gate: only the owner's own navigation does (amendment 2).

- **`sound`** (BR-10): WSLg audio for daemon-owned Remote Chrome only. **Off**
  by default; each live engine reports its applied state separately.

A missing, unreadable or malformed file, or a value that is not a boolean,
reads as the default. Later packages add their switches here, and a
write keeps every key it does not change — one a later package added
included.

**Protected state** (`tools/files._protected_state_paths`): no agent write
tool, v2 grant or shell command may change it — the plan's "so no agent can
flip it". **Changed only by the owner**: `PATCH /browser/settings`
(`routes.py`, which owns every `/browser…` path), owner-only on the HUD
listener with a strict loopback Host; `GET` is owner-only too.

**Read where it is cheap.** The gate asks `value("lan")` and
`value("local_servers")` on every connection, and the terminals
`auto_open()` for every chunk of output, so they answer from memory and look
at the file at most once every `RECHECK_S` (a `stat`; read again only when it
changed) — an edit by hand lands within a second, no restart. A file that is
a symlink, not a regular file or larger than `FILE_CAP` is not read (it reads
as the defaults), and it is opened without blocking, so nothing planted there
can hang a read. Written atomically (a temp file beside it, mode 600, then
`rename`).

**Which file.** A `BrowserSettings()` made with no path keeps its values in
memory only: that is what an embedded or test daemon has, so none reads or
writes the owner's file. daemon2's `main()` attaches the real one before the
daemon starts, as it loads the saved Codex catalog; a suite that wants the
file attaches its own temp path.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
import time

LOG = logging.getLogger(__name__)

DEFAULTS: dict[str, bool] = {"lan": False, "local_servers": False, "auto_open": True, "sound": False}
FILE_CAP = 64 * 1024
RECHECK_S = 1.0


def _stamp(path: Path):
    """What says the file changed: its identity, size, time and mode. None
    when it is not there (or cannot be looked at)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode


def _read(path: Path) -> dict | None:
    """The file's JSON object, or None: missing, a symlink, not a regular
    file, too large, not JSON, or not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        LOG.warning("browser settings not read (%s); using the defaults", type(exc).__name__)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_CAP:
            LOG.warning("browser settings not read (not a small regular file); using the defaults")
            return None
        chunks, size = [], 0
        while size <= FILE_CAP:
            chunk = os.read(fd, FILE_CAP + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        LOG.warning("browser settings not read (%s); using the defaults", type(exc).__name__)
        return None
    finally:
        os.close(fd)
    if size > FILE_CAP:
        return None
    try:
        value = json.loads(b"".join(chunks))
    except ValueError:
        LOG.warning("browser settings not read (not JSON); using the defaults")
        return None
    return value if isinstance(value, dict) else None


def _values(raw: dict) -> dict[str, bool]:
    return {key: raw[key] if isinstance(raw.get(key), bool) else default
            for key, default in DEFAULTS.items()}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BrowserSettings:
    """The owner's Browser switches: from `browser.json` once attached,
    else in memory (module note)."""

    def __init__(self, path: Path | str | None = None, *, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._path: Path | None = None
        self._values = dict(DEFAULTS)
        self._stamp = None
        self._checked = float("-inf")
        if path is not None:
            self.attach(path)

    @property
    def path(self) -> Path | None:
        return self._path

    def attach(self, path: Path | str) -> None:
        """Read and write `path` from now on (daemon2's `main()`)."""
        with self._lock:
            self._path = Path(path).expanduser()
            self._values = dict(DEFAULTS)
            self._stamp = None
            self._refresh()

    def _refresh(self) -> None:
        """The caller holds `_lock`."""
        self._checked = self._clock()
        stamp = _stamp(self._path)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        raw = _read(self._path) if stamp is not None else None
        self._values = _values(raw or {})

    def value(self, key: str) -> bool:
        """One switch, as the file says it now. Cheap — memory, and a `stat`
        at most once a `RECHECK_S` — so the gate can ask on every connection
        and the terminals on every chunk of output. KeyError for a name that
        is not a switch."""
        if self._path is not None and self._clock() - self._checked >= RECHECK_S:
            with self._lock:
                if self._clock() - self._checked >= RECHECK_S:
                    self._refresh()
        return self._values[key]

    def auto_open(self) -> bool:
        """"Open dev servers automatically" (BR-8a), for the terminals."""
        return self.value("auto_open")

    def snapshot(self) -> dict[str, bool]:
        with self._lock:
            if self._path is not None:
                self._refresh()
            return dict(self._values)

    def update(self, **changes) -> dict[str, bool]:
        """Set some switches (each a known name, each a boolean) and save.
        Keys of the file this does not know are kept. ValueError for a bad
        change, OSError when the file cannot be written."""
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError("not a Browser setting")   # never echoed (PR #69 review)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
        with self._lock:
            if self._path is None:
                self._values.update(changes)
                return dict(self._values)
            payload = dict(_read(self._path) or {})
            payload.update(changes)
            _write(self._path, payload)
            self._values = _values(payload)
            self._stamp = _stamp(self._path)
            self._checked = self._clock()
            return dict(self._values)
