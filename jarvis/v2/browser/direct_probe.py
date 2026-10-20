"""The owner's Direct capture probe (B-DIRECT-CAPTURE, BR-4/M6).

The installed HUD runs one `getDisplayMedia({preferCurrentTab})` capture
cropped to a HUD-made test pattern, on the owner's press, and posts the facts
here (`POST /browser/direct-capture/probe`, owner-only). No image: the samples
are colours of the HUD's own pattern, never page content.

Every field is checked against a fixed schema, and the verdict is computed
here, not taken from the HUD: the pattern's colours and the nine sample
positions are this module's own, the size rule is applied to the measured
rect, window and devicePixelRatio, and the rect must have the pattern's
shape. What the HUD reports it measured (rect, window, DPR, frame size,
sample colours) is trusted as measurement; a forged HUD is outside this
check, which is why the daemon also refuses the switch without a pass. A pass is held **in memory only**: it is what
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
# The HUD's test pattern (hud/src/lib/directProbe.ts), kept here so the
# verdict never takes the expected colours from the report.
PATTERN = {"tl": (220, 40, 40), "tr": (40, 180, 60), "bl": (40, 80, 220), "br": (240, 200, 40),
           "centre": (250, 250, 250)}
SAMPLE_POINTS = ((0.15, 0.15, "tl"), (0.3, 0.33, "tl"), (0.85, 0.15, "tr"), (0.7, 0.33, "tr"),
                 (0.15, 0.85, "bl"), (0.3, 0.67, "bl"), (0.85, 0.85, "br"), (0.7, 0.67, "br"),
                 (0.5, 0.5, "centre"))
SAMPLES = len(SAMPLE_POINTS)
PROBE_ASPECT = 1.4   # 7:5, not a window's usual shape
ASPECT_SLACK = 0.01
PROBE_MIN_WIDTH = 160  # CSS px after any HUD zoom
# The size rule (hud/src/lib/directCapture.ts `scaleOf`): one uniform scale
# between CSS pixels and device pixels, give or take rounding.
MIN_SCALE = 0.5
SHAPE_SLACK = 1.5
EDGE_SLACK = 2
COLOUR_MARGIN = 20   # a sample must be this much nearer its own colour than any other
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
                "viewport", "rect", "dpr", "samples", "tracks_live_after", "timing_ms"))
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
        "viewport": _size(raw["viewport"]),
        "rect": {"width": _num(rect["width"], 0, MAX_EDGE), "height": _num(rect["height"], 0, MAX_EDGE)},
        "dpr": _num(raw["dpr"], 0.1, 16),
        "samples": [{"x": _int(_keys(s, ("x", "y", "got"))["x"], 0, MAX_EDGE),
                     "y": _int(s["y"], 0, MAX_EDGE), "got": _colour(s["got"], 4)}
                    for s in samples],
        "tracks_live_after": _int(raw["tracks_live_after"], 0, 16),
        "timing_ms": {k: _num(timing[k], 0, 600_000) for k in TIMINGS if k in timing},
    }


def scale_of(frame: dict, box: dict, dpr: float):
    """The uniform scale at which `frame` is `box`, or None (`scaleOf` in the HUD)."""
    if min(box["width"], box["height"], frame["width"], frame["height"]) < 1:
        return None
    wide = box["width"] >= box["height"]
    long, short = (box["width"], box["height"]) if wide else (box["height"], box["width"])
    f_long, f_short = (frame["width"], frame["height"]) if wide else (frame["height"], frame["width"])
    s = f_long / long
    if s < MIN_SCALE - EDGE_SLACK / long or s > max(MIN_SCALE, dpr) + EDGE_SLACK / long:
        return None
    return s if abs(f_short - short * s) <= SHAPE_SLACK else None


def size_verdict(frame: dict, rect: dict, dpr: float, viewport: dict):
    """('' | 'wrong_size' | 'not_cropped' | 'ambiguous', scale)."""
    pane, whole = scale_of(frame, rect, dpr), scale_of(frame, viewport, dpr)
    if pane is not None and whole is not None:
        return "ambiguous", pane
    if whole is not None:
        return "not_cropped", whole
    if pane is None:
        return "wrong_size", None
    return "", pane


def sample_positions(frame: dict):
    return [(min(frame["width"] - 1, math.floor(frame["width"] * fx)),
             min(frame["height"] - 1, math.floor(frame["height"] * fy)), name) for fx, fy, name in SAMPLE_POINTS]


def nearest_is(got, name: str) -> bool:
    """`got` is clearly the pattern colour `name`: nearer it than any other, by a margin."""
    dist = {k: math.dist(got[:3], c) for k, c in PATTERN.items()}
    return got[3] >= 250 and all(dist[name] + COLOUR_MARGIN < d for k, d in dist.items() if k != name)


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
    rect, frame = facts["rect"], facts["frame"]
    if (rect["width"] < PROBE_MIN_WIDTH or not rect["height"]
            or abs(rect["width"] / rect["height"] - PROBE_ASPECT) > PROBE_ASPECT * ASPECT_SLACK):
        return "wrong_rect"
    if frame is None:
        return "wrong_size"
    size, _ = size_verdict(frame, rect, facts["dpr"], facts["viewport"])
    if size:
        return size
    samples = facts["samples"]
    if len(samples) != SAMPLES or any(
            (s["x"], s["y"]) != (x, y) or not nearest_is(s["got"], name)
            for s, (x, y, name) in zip(samples, sample_positions(frame))):
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
        scale = None
        if facts["frame"] is not None and facts["rect"]["height"]:
            scale = size_verdict(facts["frame"], facts["rect"], facts["dpr"], facts["viewport"])[1]
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
        summary = {"passed": not reason, "reason": reason, "at": at}
        with self._lock:
            self._last = summary
            path = self._path
        saved = False
        if path is not None:
            try:
                _write(path, {"version": 2, **summary,
                              "scale": None if scale is None else round(scale, 4), "facts": facts})
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
