"""The owner's speaker enrollment and Guest voice routes (voice plan V12, 3.4, 3.13).

    GET    /speaker             -> status: state, counts, threshold, Guest switch, engine
    POST   /speaker/reading     audio/wav body (one prompted reading) -> {accepted, problems, metrics, readings}
    POST   /speaker {"propose": true}                 -> the proposed threshold and both score distributions (stores nothing)
    POST   /speaker {"accept": true, "threshold"?: n, "anyway"?: bool} -> builds the voiceprint from the readings:
                                verification on. 409 "record the readings again" when they do not separate the owner's
                                voice clearly (proposal `confirm`), unless `anyway` is true
    POST   /speaker {"threshold": n}                  -> tune an enrolled threshold
    POST   /speaker {"guest": bool}                   -> Guest voice on/off (persisted until the owner turns it off)
    DELETE /speaker             -> removes the readings, the voiceprint and the Guest switch (answers the status)

Owner-only: the HUD's listener with the HUD's Origin (`projects.owner_only`),
checked before anything else, the body included. No route returns audio or an
embedding. The audio body is on the explicit set of routes `check_origin` lets
carry audio (`hud_api.AUDIO_ROUTES`), and is capped here before it is read.

A change of state is published on the bus as `speaker_guest`
`{guest, enrolled, mode}` so every HUD window agrees and the GUEST VOICE label
never lags (plan 3.4.1).
"""
from __future__ import annotations

import threading

from jarvis import speaker


_seen_lock = threading.Lock()


def _publish(daemon, status: dict) -> None:
    key = (status["guest"], status["enrolled"], status["mode"])
    with _seen_lock:
        daemon._speaker_seen = key
        daemon.bus.publish({"kind": "speaker_guest", "data": {"guest": key[0], "enrolled": key[1], "mode": key[2]}})


def watch(daemon) -> None:
    """Publish `speaker_guest` when the mode the daemon observes is not the one last published (M3).

    The routes publish their own changes; this catches the ones they do not see: `guest.json` edited by
    hand, `voiceprint.json` deleted or damaged, a model that went missing. `/stt` and `GET /speaker` call it,
    so the HUD's GUEST VOICE / VOICE CHECK OFF label follows the file, not the last click. The first call
    publishes (nothing has been published since the daemon started).
    """
    try:
        status = speaker.status()
        key = (status["guest"], status["enrolled"], status["mode"])
        with _seen_lock:
            if getattr(daemon, "_speaker_seen", None) == key:
                return
            daemon._speaker_seen = key
            daemon.bus.publish({"kind": "speaker_guest", "data": {"guest": key[0], "enrolled": key[1], "mode": key[2]}})
    except Exception:  # noqa: BLE001 - watching never breaks a request
        pass


def route(handler, daemon, parts, query):
    """`/speaker` and `/speaker/reading`, or None."""
    if parts not in (["speaker"], ["speaker", "reading"]):
        return None
    from .daemon import APIError, _object
    from .hud_api import fail
    from .projects import owner_only

    owner_only(handler, daemon)
    _object(query, ())
    method = handler.command
    try:
        if parts == ["speaker", "reading"]:
            if method != "POST":
                return None
            return 200, _reading(handler)
        if method == "GET":
            watch(daemon)
            return 200, speaker.status()
        if method == "DELETE":
            result = speaker.delete()
            _publish(daemon, result)
            return 200, result
        if method != "POST":
            return None
        try:
            if int(handler.headers.get("Content-Length", "0")) > 4096:
                fail(413, "request body exceeds 4 KiB")
        except ValueError:
            fail(400, "invalid body length")
        body = _object(handler._body(), ("propose", "accept", "threshold", "guest", "anyway"))
        keys = set(body)
        if keys == {"propose"} and body["propose"] is True:
            return 200, speaker.propose()
        if keys <= {"accept", "threshold", "anyway"} and body.get("accept") is True:
            if "anyway" in body and not isinstance(body["anyway"], bool):
                fail(400, "anyway must be a boolean")
            result = speaker.accept(body.get("threshold"), body.get("anyway") is True)
            _publish(daemon, result)
            return 200, result
        if keys == {"threshold"}:
            return 200, speaker.set_threshold(body["threshold"])
        if keys == {"guest"}:
            if not isinstance(body["guest"], bool):
                fail(400, "guest must be a boolean")
            result = speaker.set_guest(body["guest"])
            _publish(daemon, result)
            return 200, result
        fail(400, "send one of propose, accept (with an optional threshold), threshold or guest")
    except speaker.VerifyUnavailable as exc:
        raise APIError(503, str(exc)) from exc
    except (speaker.EnrollError, speaker.StoreError) as exc:
        raise APIError(409, str(exc)) from exc
    except ValueError as exc:
        raise APIError(400, str(exc)) from exc


def _reading(handler) -> dict:
    """One reading: audio/wav, at most `speaker.MAX_READING_BYTES`, size judged before it is read."""
    from .hud_api import fail

    mime = handler.headers.get("Content-Type", "").split(";", 1)[0]
    if not mime.startswith("audio/"):
        fail(400, "expected audio/wav")
    if handler.headers.get("Transfer-Encoding"):
        fail(400, "chunked bodies are not supported")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        fail(400, "invalid body length")
    if length <= 0:
        fail(400, "audio is empty")
    if length > speaker.MAX_READING_BYTES:
        fail(413, "a reading is at most 4 MiB")
    data = handler.rfile.read(length)
    if len(data) != length:
        fail(400, "incomplete request body")
    return speaker.add_reading(data, mime)
