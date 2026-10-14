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
import secrets
import stat
from pathlib import Path

from . import config

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


def load() -> dict:
    """`{backend, cloud_fallback, error}`; `error` is "" unless the file was untrustworthy."""
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
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
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
    return load()
