"""The `/speaker` routes and `/stt?verify=1` of `docs/hud-api.md` (V12), for the HUD mock.

Free and hermetic: nothing is embedded and no audio is judged. The mock keeps a
tiny model of what the real daemon does, enough for the HUD's behaviour to be
checked end to end:

  - `mock.speaker` is the daemon's store in miniature: `readings`, `enrolled`,
    `guest` (persistent until changed), `threshold`, and the test's levers
    `reject_next` (how many `verify=1` clips the "voiceprint" refuses) and
    `installed` (the speaker model);
  - `GET /speaker` -> the status the real route answers;
  - `POST /speaker/reading` (audio/wav) -> `{accepted, problems, readings}`; a reading
    of fewer than `MIN_BYTES` is "too short" and not kept;
  - `POST /speaker {propose|accept|threshold|guest}`, `DELETE /speaker`;
  - a change to Guest or enrollment publishes `speaker_guest` on the bus;
  - `POST /stt?verify=1` is in `hud_v2_mock.py`: it counts `mock.cloud_uploads` only for a clip
    that passed verification, which is how a test proves a refusal uploads nothing.

Owner-only on the real daemon: a request with an `Origin` that is not the mock's own is 403 here too.
"""
from __future__ import annotations

MIN_BYTES = 6 * 16000 * 2          # six seconds of 16-bit mono at 16 kHz
PROMPTS = ["The quick brown fox jumps over the lazy dog.", "Please read this second passage.", "For the last reading, say anything."]


def fresh() -> dict:
    return {"readings": 0, "enrolled": False, "guest": False, "threshold": None, "reject_next": 0, "installed": True,
            "last": None, "analysed": False}


def status(sp: dict) -> dict:
    kind = ("enrolled" if sp["enrolled"] else "enrolling" if sp["readings"] else "not_enrolled")
    if sp["guest"]:
        mode = "guest"
    elif not sp["enrolled"]:
        mode = "off"
    else:
        mode = "on" if sp["installed"] else "blocked"
    return {
        "state": kind, "enrolled": sp["enrolled"], "readings": sp["readings"], "readings_min": 3, "readings_max": 6,
        "prompts": PROMPTS,
        "quality": {"min_s": 6.0, "max_s": 30.0, "min_level_db": -40.0, "max_level_db": -6.0, "max_clip": 0.005, "min_speech": 0.45},
        "guest": sp["guest"], "mode": mode, "threshold": sp["threshold"], "threshold_default": 0.6,
        "threshold_min": 0.3, "threshold_max": 0.9, "short_clip_s": 1.2, "short_relax": 0.08,
        "voiceprint_readings": 3 if sp["enrolled"] else None, "voiceprint_model": "nemo_en_titanet_small" if sp["enrolled"] else None,
        "created": None, "installed": sp["installed"], "reason": "" if sp["installed"] else "the speaker model is not downloaded",
        "loaded": False, "threads": 1, "idle_unload_min": 5.0, "model": "nemo_en_titanet_small",
        "fetch": "python -m jarvis.speaker fetch", "error": "", "last": sp["last"],
    }


def _publish(mock) -> None:
    s = status(mock.speaker)
    mock.emit("speaker_guest", {"guest": s["guest"], "enrolled": s["enrolled"], "mode": s["mode"]})


def _owner(h, mock) -> bool:
    origin = h.headers.get("Origin")
    if origin is not None and origin != f"http://127.0.0.1:{mock.port}":
        h._err(403, "only the owner can do this, from the HUD")
        return False
    return True


def reading(h, mock, raw: bytes) -> None:
    """`POST /speaker/reading`: the bytes were read by the caller."""
    if not _owner(h, mock):
        return
    sp = mock.speaker
    if len(raw) < MIN_BYTES:
        return h._json({"accepted": False, "problems": ["too short: read the whole passage"],
                        "metrics": {"seconds": round(len(raw) / 32000, 2)}, "readings": sp["readings"]})
    if sp["readings"] >= 6:
        return h._err(409, "enough readings (6): accept the voiceprint, or delete and start again")
    sp["readings"] += 1
    h._json({"accepted": True, "problems": [], "metrics": {"seconds": round(len(raw) / 32000, 2)}, "readings": sp["readings"]})


def handle(h, mock, method: str, path: str, body) -> bool:
    if path != "/speaker":
        return False
    if not _owner(h, mock):
        return True
    sp = mock.speaker
    if method == "GET":
        h._json(status(sp))
    elif method == "DELETE":
        mock.speaker.update(fresh())
        h._json(status(mock.speaker))
        _publish(mock)
    elif method == "POST":
        if not isinstance(body, dict):
            return h._err(400, "body must be a JSON object") or True
        keys = set(body)
        if keys == {"propose"} and body["propose"] is True:
            if sp["readings"] < 3:
                h._err(409, "record 3 readings first")
            else:
                sp["analysed"] = True
                h._json({"threshold": 0.47, "bins": {"lo": -0.2, "width": 0.05, "n": 24},
                         "genuine": {"n": 9, "min": 0.71, "p10": 0.73, "median": 0.8, "hist": [0] * 18 + [1, 2, 3, 2, 1, 0]},
                         "impostors": {"n": 12, "max": 0.31, "median": 0.1, "hist": [0, 0, 3, 4, 3, 1, 1] + [0] * 17,
                                       "closest": [{"voice": "af_heart", "score": 0.31}]},
                         "false_reject": 0.0, "false_accept": 0.0, "overlap": False, "warning": "", "readings": sp["readings"]})
        elif keys <= {"accept", "threshold"} and body.get("accept") is True:
            t = body.get("threshold", 0.47)
            if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0.3 <= t <= 0.9:
                h._err(400, "threshold must be a number from 0.3 to 0.9")
            elif sp["readings"] < 3:
                h._err(409, "record 3 readings first")
            else:
                sp.update(enrolled=True, threshold=t)
                h._json(status(sp))
                _publish(mock)
        elif keys == {"threshold"}:
            t = body["threshold"]
            if not isinstance(t, (int, float)) or isinstance(t, bool) or not 0.3 <= t <= 0.9:
                h._err(400, "threshold must be a number from 0.3 to 0.9")
            elif not sp["enrolled"]:
                h._err(409, "there is no voiceprint to tune")
            else:
                sp["threshold"] = t
                h._json(status(sp))
        elif keys == {"guest"} and isinstance(body["guest"], bool):
            sp["guest"] = body["guest"]
            h._json(status(sp))
            _publish(mock)
        else:
            h._err(400, "send one of propose, accept (with an optional threshold), threshold or guest")
    else:
        return False
    return True


def stt_verify(mock) -> tuple[int, dict] | str:
    """What `POST /stt?verify=1` does before the cloud: (status, error body) for a refusal, else the `verify` word."""
    sp = mock.speaker
    if sp["guest"]:
        return "skipped_guest"
    if not sp["enrolled"]:
        return "skipped_unenrolled"
    if not sp["installed"]:
        return 503, {"error": "speaker verification is not set up: the speaker model is not downloaded"}
    if sp["reject_next"] > 0:
        sp["reject_next"] -= 1
        sp["last"] = {"result": "rejected", "score": 0.2}
        return 422, {"error": "not the enrolled speaker"}
    sp["last"] = {"result": "matched", "score": 0.8}
    return "matched"
