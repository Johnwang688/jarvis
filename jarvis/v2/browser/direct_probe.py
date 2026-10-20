"""The owner's Direct capture probe (B-DIRECT-CAPTURE, BR-4/M6).

The installed HUD runs one `getDisplayMedia({preferCurrentTab})` capture
cropped to a HUD-made test pattern, on the owner's press, and posts the facts
here (`POST /browser/direct-capture/probe`, owner-only). No image: the samples
are colours of the HUD's own pattern, never page content.

Every field is checked against a fixed schema, and the verdict is computed
here, not taken from the HUD. A pass is held **in memory only**: it is what
lets the owner turn `direct_capture` on (`routes.py`). The JSON file is a report
for the lead (`config.DIRECT_CAPTURE_PROBE_PATH`, 0600, atomic) and is never
read back, so a planted file unlocks nothing. Without a path (a test or
embedded daemon) nothing is written.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import secrets
import threading
import time

LOG = logging.getLogger(__name__)

DISPLAY_MODES = ("standalone", "browser", "minimal-ui", "fullscreen", "window-controls-overlay", "unknown")
PROMPTS = ("accepted", "cancelled", "error", "not_shown")
ERRORS = ("", "NotAllowedError", "AbortError", "InvalidStateError", "NotFoundError", "NotReadableError",
          "NotSupportedError", "OverconstrainedError", "SecurityError", "TypeError", "TimeoutError", "OtherError")
SURFACES = ("browser", "window", "monitor", "unknown")
CURSORS = ("never", "always", "motion", "unknown")
TIMINGS = ("crop_target", "prompt", "crop", "frame", "total")
SAMPLES = 9
SIZE_SLACK = 2       # px: rounding of a fractional rect × devicePixelRatio
COLOUR_SLACK = 24    # per channel: colour management and video encoding
MAX_EDGE = 16384
REFUSED = "the probe report could not be read"


class ProbeRefused(ValueError):
    """Fixed words only."""


def _keys(value, keys, optional=()):
    if not isinstance(value, dict) or not set(keys) <= set(value) or set(value) - set(keys) - set(optional):
        raise ProbeRefused(REFUSED)
    return value


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProbeRefused(REFUSED)
    return value


def _num(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ProbeRefused(REFUSED)
    return round(float(value), 3)


def _one(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ProbeRefused(REFUSED)
    return value


def _bool(value):
    if type(value) is not bool:
        raise ProbeRefused(REFUSED)
    return value


def _size(value):
    _keys(value, ("width", "height"))
    return {"width": _int(value["width"], 0, MAX_EDGE), "height": _int(value["height"], 0, MAX_EDGE)}


def _colour(value, n):
    if not isinstance(value, list) or len(value) != n:
        raise ProbeRefused(REFUSED)
    return [_int(c, 0, 255) for c in value]


def normalize(raw) -> dict:
    """The facts, re-checked field by field. ProbeRefused for anything else."""
    _keys(raw, ("display_mode", "chrome", "apis", "prompt", "error", "track", "crop", "frame",
                "expected", "rect", "dpr", "samples", "tracks_live_after", "timing_ms"))
    apis = _keys(raw["apis"], ("get_display_media", "crop_target", "image_capture"))
    track = raw["track"]
    if track is not None:
        _keys(track, ("display_surface", "width", "height", "frame_rate", "cursor"))
        track = {"display_surface": _one(track["display_surface"], SURFACES),
                 "width": _int(track["width"], 0, MAX_EDGE), "height": _int(track["height"], 0, MAX_EDGE),
                 "frame_rate": _num(track["frame_rate"], 0, 1000), "cursor": _one(track["cursor"], CURSORS)}
    crop = _keys(raw["crop"], ("applied", "error"))
    rect = _keys(raw["rect"], ("width", "height"))
    samples = raw["samples"]
    if not isinstance(samples, list) or len(samples) > SAMPLES:
        raise ProbeRefused(REFUSED)
    timing = _keys(raw["timing_ms"], (), TIMINGS)
    chrome = raw["chrome"]
    return {
        "display_mode": _one(raw["display_mode"], DISPLAY_MODES),
        "chrome": None if chrome is None else _int(chrome, 1, 9999),
        "apis": {k: _bool(apis[k]) for k in ("get_display_media", "crop_target", "image_capture")},
        "prompt": _one(raw["prompt"], PROMPTS),
        "error": _one(raw["error"], ERRORS),
        "track": track,
        "crop": {"applied": _bool(crop["applied"]), "error": _one(crop["error"], ERRORS)},
        "frame": None if raw["frame"] is None else _size(raw["frame"]),
        "expected": _size(raw["expected"]),
        "rect": {"width": _num(rect["width"], 0, MAX_EDGE), "height": _num(rect["height"], 0, MAX_EDGE)},
        "dpr": _num(raw["dpr"], 0.1, 16),
        "samples": [{"x": _int(_keys(s, ("x", "y", "want", "got"))["x"], 0, MAX_EDGE),
                     "y": _int(s["y"], 0, MAX_EDGE), "want": _colour(s["want"], 3), "got": _colour(s["got"], 4)}
                    for s in samples],
        "tracks_live_after": _int(raw["tracks_live_after"], 0, 16),
        "timing_ms": {k: _num(timing[k], 0, 600_000) for k in TIMINGS if k in timing},
    }


def verdict(facts: dict) -> str:
    """'' when the probe passed, else the first failing reason (a fixed word)."""
    if facts["tracks_live_after"]:
        return "tracks_left"
    if facts["display_mode"] != "standalone":
        return "not_app_mode"
    if not all(facts["apis"].values()):
        return "api_missing"
    if facts["prompt"] == "cancelled":
        return "cancelled"
    if facts["prompt"] != "accepted" or facts["track"] is None:
        return "prompt_error"
    if facts["track"]["display_surface"] != "browser":
        return "not_this_tab"
    if not facts["crop"]["applied"]:
        return "not_cropped"
    frame, expected = facts["frame"], facts["expected"]
    if (frame is None or not expected["width"] or not expected["height"]
            or abs(frame["width"] - expected["width"]) > SIZE_SLACK
            or abs(frame["height"] - expected["height"]) > SIZE_SLACK):
        return "wrong_size"
    samples = facts["samples"]
    if len(samples) != SAMPLES or any(
            s["got"][3] < 250 or any(abs(g - w) > COLOUR_SLACK for g, w in zip(s["got"], s["want"]))
            for s in samples):
        return "wrong_pixels"
    return ""


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DirectProbe:
    def __init__(self, path: Path | str | None = None, *, clock=time.time):
        self._lock = threading.Lock()
        self._path = None if path is None else Path(path).expanduser()
        self._clock = clock
        self._last: dict | None = None

    def attach(self, path: Path | str) -> None:
        with self._lock:
            self._path = Path(path).expanduser()

    def record(self, raw) -> dict:
        facts = normalize(raw)
        reason = verdict(facts)
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
        summary = {"passed": not reason, "reason": reason, "at": at}
        with self._lock:
            self._last = summary
            path = self._path
        saved = False
        if path is not None:
            try:
                _write(path, {"version": 1, **summary, "facts": facts})
                saved = True
            except OSError as exc:
                LOG.warning("direct capture probe report not saved (%s)", type(exc).__name__)
        LOG.info("direct capture probe: %s", "passed" if not reason else "failed (" + reason + ")")
        return {**summary, "saved": saved}

    def status(self) -> dict | None:
        with self._lock:
            return None if self._last is None else dict(self._last)

    def passed(self) -> bool:
        with self._lock:
            return bool(self._last and self._last["passed"])
