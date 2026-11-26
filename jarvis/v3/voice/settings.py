"""The owner's persistent voice settings."""

from contextlib import suppress
from dataclasses import dataclass, replace
import json
import logging
import os
import stat
from uuid import uuid4

from jarvis import config
from .._paths import open_directory


LOG = logging.getLogger(__name__)
FILENAME = "settings.json"
MAX_BYTES = 4096
RANGES = {"speed": (0.8, 2.0), "follow_up_s": (0, 30), "end_silence_ms": (300, 1000)}
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class VoiceSettings:
    speed: float
    follow_up_s: float
    end_silence_ms: int

    def patched(self, body):
        if not isinstance(body, dict) or body.keys() - RANGES.keys():
            raise ValueError(f"Expected a subset of: {', '.join(RANGES)}.")
        for name, value in body.items():
            low, high = RANGES[name]
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f"{name} must be a number from {low} to {high}.")
        if "end_silence_ms" in body:
            value = body["end_silence_ms"]
            if int(value) != value:
                low, high = RANGES["end_silence_ms"]
                raise ValueError(f"end_silence_ms must be an integer from {low} to {high}.")
            body = {**body, "end_silence_ms": int(value)}
        return replace(self, **body)

    def snapshot(self):
        return {name: {"value": getattr(self, name), "min": low, "max": high}
                for name, (low, high) in RANGES.items()}


def defaults():
    low, high = RANGES["end_silence_ms"]
    return VoiceSettings(config.V3_KOKORO_SPEED, min(config.V3_FOLLOW_UP_S, RANGES["follow_up_s"][1]),
                         max(low, min(high, config.V3_END_SILENCE_MS)))


def _directory(data_dir, *, create=False):
    # Match the daemon's data directory policy. Pin the data and voice
    # directories without following links at either final component.
    parent = data_dir.parent.resolve()
    return open_directory(parent / data_dir.name / "voice", create=create)


def load(data_dir):
    fallback = defaults()
    try:
        with _directory(data_dir) as directory:
            fd = os.open(FILENAME, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        saved = json.loads(raw.decode("utf-8"))
        if not isinstance(saved, dict) or saved.keys() - RANGES.keys():
            raise ValueError
    except (OSError, ValueError, RecursionError):
        LOG.info("Voice settings could not be loaded. Using defaults.")
        return fallback
    # One bad value falls back alone; it must not discard the others.
    for name, value in saved.items():
        try:
            fallback = fallback.patched({name: value})
        except ValueError:
            LOG.info("A saved voice setting was invalid. Using its default.")
    return fallback


def save(data_dir, settings):
    fields = {name: getattr(settings, name) for name in RANGES}
    defaults().patched(fields)
    raw = (json.dumps(fields, allow_nan=False) + "\n").encode("utf-8")
    with _directory(data_dir, create=True) as directory:
        if os.fstat(directory).st_uid != os.getuid():
            raise PermissionError("Voice settings directory must belong to the owner.")
        os.fchmod(directory, 0o700)
        temporary = f".settings-{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        fd = os.open(temporary, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
