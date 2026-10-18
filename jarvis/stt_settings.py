"""The owner's speech-to-text switches (voice rework V13, answered Q6).

One small JSON object at `config.STT_SETTINGS_PATH` (`~/.config/jarvis/stt.json`,
env `JARVIS_STT_SETTINGS`):

    {"backend": "cloud" | "local" | "auto", "cloud_fallback": true | false}

- **`backend`**: `cloud` is today's OpenRouter chain; `local` decodes in the
  daemon (jarvis/stt_local.py) and only falls to the cloud when the fallback
  switch allows; `auto` is `local` that quietly uses the cloud when the local
  engine is not installed. The default is `config.STT_BACKEND_DEFAULT`.
- **`cloud_fallback`**: whether audio may be uploaded at all. Off means nothing
  is ever sent to a cloud model, by any caller, and a local failure is an
  error the HUD shows.

A missing file, or a missing key, reads as the default. A file that EXISTS but
cannot be trusted (not JSON, not an object, a symlink, too large, or a key of
the wrong type) fails CLOSED: the unreadable key reads as `local` / no upload,
never as "upload". The one writer is the owner-only `/stt/settings` route; the
file is protected state (tools/files.py), so no agent write can switch the
owner's audio onto the network. Read on every clip (a `stat` and a few bytes).
"""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

from . import config
from .private_write import write_private

BACKENDS = ("cloud", "local", "auto")
FILE_CAP = 4096


def _path() -> Path:
    return Path(config.STT_SETTINGS_PATH)


def defaults() -> dict:
    return {"backend": config.STT_BACKEND_DEFAULT, "cloud_fallback": bool(config.STT_CLOUD_FALLBACK_DEFAULT)}


def _read(path: Path):
    """(object, None), (None, None) for a missing file, or (None, reason)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_CAP:
            return None, "not a small regular file"
        data = os.read(fd, FILE_CAP + 1)
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    finally:
        os.close(fd)
    if len(data) > FILE_CAP:
        return None, "not a small regular file"
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return None, "not JSON"
    return (value, None) if isinstance(value, dict) else (None, "not an object")


_lock = threading.Lock()
_last: tuple | None = None
_watchers: dict[object, object] = {}


def watch(key: object, callback) -> None:
    """Call `callback(settings)` whenever a read finds a different resolved value than the last
    read did (a hand edit, a corrupt file noticed). One callback per `key`, replaced on re-register.
    A change this module's own `update` made is not reported: its caller publishes it."""
    with _lock:
        _watchers[key] = callback


def _note(result: dict, quiet: bool) -> None:
    global _last
    token = (result["backend"], result["cloud_fallback"], result["error"])
    with _lock:
        changed = _last is not None and token != _last
        _last = token
        callbacks = list(_watchers.values()) if changed and not quiet else []
    for callback in callbacks:
        try:
            callback(result)
        except Exception:  # noqa: BLE001 - an observer cannot break a read
            pass


def load(_quiet: bool = False) -> dict:
    """`{backend, cloud_fallback, error}`; `error` is "" unless the file was untrustworthy."""
    result = _load()
    _note(result, _quiet)
    return result


def _load() -> dict:
    out = defaults()
    raw, problem = _read(_path())
    if problem:
        return {"backend": "local", "cloud_fallback": False, "error": f"stt.json {problem}: using local only, no upload"}
    errors = []
    if raw is not None:
        if "backend" in raw:
            if raw["backend"] in BACKENDS:
                out["backend"] = raw["backend"]
            else:
                out["backend"] = "local"
                errors.append("stt.json has an invalid backend: using local")
        if "cloud_fallback" in raw:
            if isinstance(raw["cloud_fallback"], bool):
                out["cloud_fallback"] = raw["cloud_fallback"]
            else:
                out["cloud_fallback"] = False
                errors.append("stt.json has an invalid cloud_fallback: failing closed, no upload")
    out["error"] = "; ".join(errors)
    return out


def update(**changes) -> dict:
    """Merge `backend` / `cloud_fallback` into the file (atomic, mode 600) and return `load()`.

    Raises ValueError for an unknown key or a bad value, before anything is written.
    """
    for key, value in changes.items():
        if key == "backend":
            if value not in BACKENDS:
                raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
        elif key == "cloud_fallback":
            if not isinstance(value, bool):
                raise ValueError("cloud_fallback must be a boolean")
        else:
            raise ValueError(f"unknown setting {key!r}")
    path = _path()
    raw, _ = _read(path)
    effective = load()
    if effective["error"]:
        # A repair through one switch must not turn the other one toward "upload":
        # keep the fail-closed values the reader has been using.
        current = {"backend": effective["backend"], "cloud_fallback": effective["cloud_fallback"]}
    else:
        current = dict(raw or {})
    current.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(path, (json.dumps(current, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return load(_quiet=True)
