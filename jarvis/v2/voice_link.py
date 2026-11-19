"""Owner-started Windows microphone link, jarvis-voice-link/1.

A physical dictation shortcut authorizes transcription with
verify=False, as on the paired phone. The owner may change that decision.
The existing desk STT switches still decide whether audio may reach cloud.
Wake clips use the desk's speaker check. Questions go to the selected HUD chat.
Approvals remain on the owner's existing surfaces.
Audio and dictation stay in memory. Questions use the normal chat history;
this transport keeps no transcript or audio log of its own.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import re
import select
import socket
import stat
import subprocess
import threading
import time
import unicodedata

from jarvis import config
from windows import voicewire as wire
from windows.voicetype import navigation_target, spoken_span, spoken_websites, web_destination
from . import remote_stt, dictation_guard, dictation_rules
from .stores import StoreError
from .voice_chat import NoVoiceTarget
from . import voice_intent

LOG = logging.getLogger(__name__)
WSL_INTEROP_SOCKET = Path("/run/WSL/1_interop")

HANDSHAKE_TIMEOUT_S = 5.0
MAX_UNAUTHENTICATED = 4
BAD_CLIP = "The clip must be a 16 kHz mono 16-bit WAV of at most 120 seconds."
BAD_REQUEST = "The voice request is invalid."
FAILED = "The clip could not be transcribed."
NOT_OWNER = "That didn't sound like you."
ASK_FAILED = "The voice question could not be answered."
ASK_NO_TARGET = "Open a chat in the HUD, then ask again."
ASK_TIMEOUT = "The voice answer took too long. Check the selected chat in the HUD."
VOICE_DOWN = "Voice commands and answers are unavailable right now."
LAYA_DOWN = ("Local voice command routing (Laya) is unavailable; Right Ctrl and wake "
             "commands answer in conversation only. Ctrl+D dictation still works.")
LAYA_UPDATE = ("The Windows voice helper is out of date: Right Ctrl and Ctrl+D share one mode. "
               "Stop and start voice to update it.")
LAYA_NAME_CHARS = 80
SAY_FAILED = "The text could not be spoken."
SAY_CHARS = 2000
CLEAN_CHARS = 2000
CLEAN_PASSES = ("off", "rules", "raw", "model")
ASK_TIMEOUT_S = 180.0
ACTION_RECEIPT_S = 300.0
KINDS = wire.KINDS
WAKE_STATUS = {
    "starting": "Starting the Windows wake listener…",
    "ready": "Say Jarvis, then a command.",
    "microphone_unavailable": "The Windows wake listener cannot use the microphone.",
    "recognizer_unavailable": "The Windows wake recognizer is unavailable.",
}


class NotOwner(Exception):
    pass


def _clean(text, cap):
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))[:cap]


def parse_intent(raw, source, text):
    addressed = source == "wake" or voice_intent.addressed(text)
    fallback = {"kind": "ask" if addressed else "dictate",
                "text": _clean(text, 2000), "target": "" if addressed else "literal"}
    try:
        value = raw
        if isinstance(raw, str):
            if len(raw) > 16000:
                return fallback
            # Previous deciders returned JSON strings, sometimes fenced.
            start, end = raw.find("{"), raw.rfind("}")
            value = json.loads(raw[start:end + 1])
        if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
                or value["kind"] not in KINDS
                or not isinstance(value.get("text", text), str)
                or not isinstance(value.get("target", ""), str)):
            return fallback
        if value["kind"] == "navigate":
            url = navigation_target(text, source)
            if not url or value.get("target") != url:
                return fallback
            return {"kind": "navigate", "text": fallback["text"], "target": url}
        return {"kind": value["kind"], "text": _clean(value.get("text", text), 2000),
                "target": _clean(value.get("target", ""), 80)}
    except (ValueError, TypeError, RecursionError):
        return fallback


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class VoiceLink:
    def __init__(self, daemon, *, port=None, key_path=None, transcribe=None,
                 transcribe_wake=None, route_model=None, asker=None, speaker=None,
                 laya_router=None, converser=None, route_feedback=None):
        self.daemon = daemon
        self.port = config.VOICE_LINK_PORT if port is None else port
        self.key_path = Path(config.voice_link_key() if key_path is None else key_path)
        self.transcribe = self._transcribe if transcribe is None else transcribe
        self.transcribe_wake = self._transcribe_wake if transcribe_wake is None else transcribe_wake
        self.route_model = self._route_model if route_model is None else route_model
        self.asker = self._ask if asker is None else asker
        self.speaker = self._speaker if speaker is None else speaker
        # Conversation-only answers while Laya is unavailable: no tools, no tasks.
        self.converser = self._converse if converser is None else converser
        # Both made on first use. Neither may change what a route dispatches.
        self._laya_factory = laya_router
        self._laya = None
        self._feedback = route_feedback
        self._laya_lock = threading.Lock()
        # True only between a successful start() and the next stop(), changed
        # under both locks. A router is never made outside it, so a handler
        # still finishing after stop() cannot start a worker nobody will close.
        self._laya_open = False
        self._laya_owner = None  # the listening socket whose requests may route
        self._laya_issue = ""
        self._laya_helper = None  # None until the active helper pings with features
        self._voice_chat = getattr(daemon, "voice_chat", None)
        self._context = threading.local()
        self.address = None
        self.status = "Not started."
        self._server = self._active = self._thread = None
        self._connections = set()
        self._pending = 0
        self._lock = threading.Lock()
        self._launch_lock = threading.Lock()
        self._launched_agent = self._launched_instance = None
        self._launch_deadline = 0.0
        self._launch_issue = ""
        self._lost_pairing = False
        self._lost_instance = None
        self._agent_wake = None
        self._control_session = self._instance = self._mic = None
        self._subscribers = set()
        self._stopped_instance = None
        self._release = None
        self._hud_mic_paused = False
        self._key = None
        self._cleanup_counts = {name: {"typed": 0, "scratched": 0} for name in CLEAN_PASSES}

    def agent_status(self):
        with self._lock:
            result = self._status_locked()
            result["shared_mic"] = self._mic_ready_locked()
            result["shared_capable"] = self._instance is not None
            result["can_stop"] = self._instance is not None and self._mic is not None
            result["hud_mic_paused"] = self._hud_mic_paused
            return result

    def _settle_exited_locked(self):
        # An interop child handle is release evidence only for the same
        # instance. A separate manually started helper can pair at any time.
        if (self._release is not None and self._stopped_instance is not None
                and self._launched_instance == self._stopped_instance
                and self._launched_agent is not None and self._launched_agent.poll() is not None):
            self._release.set()

    def _status_locked(self):
        self._settle_exited_locked()
        if self._server is None:
            return {"state": "unavailable", "detail": self.status}
        if self._release is not None and not self._release.is_set():
            if self._mic is None and self._active is None:
                return {"state": "stop_unconfirmed",
                        "detail": "The Windows helper disconnected before confirming release. Start to reconnect."}
            return {"state": "stopping", "detail": "Releasing the Windows microphone…"}
        if self._active is None or self._hud_mic_paused:
            child = self._launched_agent
            if self._lost_pairing and not self._hud_mic_paused:
                if (self._lost_instance is not None and self._lost_instance == self._launched_instance
                        and child is not None and child.poll() is not None):
                    return {"state": "launch_failed",
                            "detail": "The Windows voice helper exited after connecting. Start voice to retry."}
                return {"state": "connect_failed",
                        "detail": "The Windows voice helper disconnected. Waiting for it to reconnect; retry Start if needed."}
            if child is not None and child.poll() is None and not self._hud_mic_paused:
                if time.monotonic() < self._launch_deadline:
                    return {"state": "starting", "detail": "Waiting for the Windows agent to connect…"}
                return {"state": "connect_failed", "detail": "The Windows agent hasn’t connected. Check pairing and retry."}
            if self._launch_issue:
                return {"state": "launch_failed", "detail": self._launch_issue}
            return {"state": "off", "detail": "Start voice to use the wake word in other apps."}
        wake = self._agent_wake
        # A stale helper or a Laya outage is shown, never hidden behind "ready".
        note = (" " + LAYA_UPDATE if self._laya_helper is False
                else " " + self._laya_issue if self._laya_issue else "")
        if wake is None:
            return {"state": "connected", "detail": "Connected; waiting for wake listener status." + note}
        return {"state": wake, "detail": WAKE_STATUS[wake] + note}

    def _mic_ready_locked(self):
        return (self._active is not None and self._mic is not None
                and self._agent_wake == "ready" and not self._hud_mic_paused)

    def subscribe_microphone(self):
        from .daemon import APIError
        with self._lock:
            if not self._mic_ready_locked():
                raise APIError(409, "The Windows microphone is not ready.")
            if len(self._subscribers) >= 4:
                raise APIError(429, "The Windows microphone is already shared with four HUD windows.")
            frames = queue.Queue(maxsize=4)
            self._subscribers.add(frames)
            return frames

    def unsubscribe_microphone(self, frames):
        with self._lock:
            self._subscribers.discard(frames)

    def _end_subscriptions_locked(self):
        for frames in self._subscribers:
            # Drop queued sound on release; even a lagging consumer ends promptly.
            while True:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    break
            frames.put_nowait(None)
        self._subscribers.clear()

    def stop_windows_agent(self):
        """The local owner can stop an authenticated helper, including a busy one."""
        from .daemon import APIError
        with self._launch_lock:
            with self._lock:
                if self._active is None and self._release is None:
                    if self._launched_agent is not None and self._launched_agent.poll() is None:
                        raise APIError(409, "The Windows helper is still connecting.")
                    self._hud_mic_paused = True
                    self._launch_issue = ""
                    return
                if self._release is None and (self._instance is None or self._mic is None):
                    raise APIError(409, "The Windows helper has no stop connection. Reopen the updated helper.")
                self._hud_mic_paused = True
                self._stopped_instance = self._instance or self._stopped_instance
                self._end_subscriptions_locked()
                if self._release is None:
                    self._release = threading.Event()
                release = self._release
            if not release.wait(5):
                with self._lock:
                    self._settle_exited_locked()
                    if not release.is_set():
                        raise APIError(503, self._status_locked()["detail"])

            with self._lock:
                self._launched_agent = None
                self._launch_issue = ""

    def start_windows_agent(self):
        """Only the local HUD's explicit start button calls this."""
        from .daemon import APIError
        with self._launch_lock:
            with self._lock:
                if self._server is None:
                    raise APIError(409, "Voice link is offline. Check voice-link setup and the daemon.")
                self._settle_exited_locked()
                if self._release is not None and not self._release.is_set():
                    if self._active is not None or self._mic is not None:
                        raise APIError(409, "Waiting for Windows to release the microphone.")
                    # Owner explicitly restarted after losing both connections.
                    # A reconnecting surviving helper may resume the session.
                    self._stopped_instance = None
                self._hud_mic_paused = False
                self._lost_pairing = False
                self._release = None
                if self._active is not None and self._instance != self._stopped_instance:
                    return
                if self._active is not None and self._instance is None:
                    return
            self._launch_issue = ""
            try:
                child = self._launched_agent
                if child is None or child.poll() is not None:
                    child = self._launch_windows_process()
                    self._launched_agent = child
                # A manual launcher can also connect through the same socket.
                deadline = self._launch_deadline = time.monotonic() + 8
                while True:
                    with self._lock:
                        if self._active is not None and (
                                self._instance is None or self._instance != self._stopped_instance):
                            return
                    code = child.poll()
                    if code is not None:
                        LOG.warning("Jarvis voice helper exited before connecting (status %s)", code)
                        raise APIError(503, "The Windows voice agent exited before connecting. Try starting it again.")
                    if time.monotonic() >= deadline:
                        raise APIError(503, "The Windows voice agent hasn’t connected. Check pairing and retry.")
                    time.sleep(0.1)
            except APIError as exc:
                with self._lock:
                    self._launch_issue = str(exc)
                raise

    def _launch_windows_process(self):
        from jarvis.voice_link_setup import windows_folder_closed
        from .daemon import APIError

        def convert(flag, path):
            return subprocess.run(["wslpath", flag, str(path)], check=True,
                                  capture_output=True, text=True, timeout=3).stdout.strip()

        try:
            bridge = Path(convert("-u", config.DESKTOP_BRIDGE_DIR))
            if not bridge.is_absolute() or not windows_folder_closed(bridge):
                raise APIError(409, "Check the Windows voice-link installation folder.")
            python = bridge / "venv" / "Scripts" / "pythonw.exe"
            key = bridge / "voice-link.key"
            if not python.is_file() or not key.is_file():
                raise APIError(409, "Voice agent setup is missing. Run jarvis voice-link setup.")
            script = convert("-w", config.REPO_ROOT / "windows" / "voice_agent.py")
            env = os.environ.copy()
            interop = env.get("WSL_INTEROP")
            # The service manager does not inherit an interactive WSL shell.
            # Use WSL's system interop socket when that shell socket is absent.
            if not interop or not Path(interop).is_socket():
                if not WSL_INTEROP_SOCKET.is_socket():
                    raise APIError(503, "Windows launch is unavailable. Open WSL and retry.")
                env["WSL_INTEROP"] = str(WSL_INTEROP_SOCKET)
            self._launched_instance = wire.new_nonce()
            return subprocess.Popen(
                [str(python), script, "127.0.0.1", str(self.port), convert("-w", key),
                 self._launched_instance],
                cwd=bridge, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            raise APIError(503, "Could not start the Windows voice agent. Check Windows interop.") from None

    def cleanup_counts(self):
        """Counts only, copied under the connection lock; never retain words."""
        with self._lock:
            return {name: dict(counts) for name, counts in self._cleanup_counts.items()}

    def _clean_dictation(self, text, app):
        from jarvis import stt_settings
        if stt_settings.load_cleanup() == "off":
            typed, which = text, "off"
        else:
            app = "other" if app == "other" else "terminal"
            typed, which = dictation_guard.choose(text, dictation_rules.clean(text, app), None)
        with self._lock:
            self._cleanup_counts[which]["typed"] += 1
        return {"text": typed, "pass": which}

    def _transcribe(self, data):
        return self._dictate(data, verify=False)

    def _transcribe_wake(self, data):
        return self._dictate(data, verify=True)

    def _dictate(self, data, *, verify):
        from . import hud_api
        from .daemon import APIError
        from .remote_api import _scrub
        hud_api.watch_stt(self.daemon)
        try:
            _kind, got = hud_api.dictate(self.daemon, data, "audio/wav",
                                        budget=remote_stt.BUDGET_S, verify=verify)
        except APIError as exc:
            if verify and exc.status == 422:
                raise NotOwner() from None
            raise
        return {"text": _scrub(str(got.text or ""))[:remote_stt.TEXT_CHARS] if got else "",
                "backend": got.backend if got else "local"}

    def _route_model(self, text, source):
        return voice_intent.route(text, source)

    def _laya_router(self, server=None):
        """The router of the listener that accepted the request, or raise.

        ``server`` is the listening socket the request arrived on. A request
        from a listener that has since stopped never gets the router a later
        start() made: its decision would belong to a connection that is gone.
        None (no connection, as in a direct call) means the running listener.
        """
        with self._laya_lock:
            if not self._laya_open or (server is not None and server is not self._laya_owner):
                raise RuntimeError("Voice link stopped")
            if self._laya is None:
                if self._laya_factory is not None:
                    self._laya = self._laya_factory()
                else:
                    from .laya_service import LayaRouter
                    self._laya = LayaRouter()  # starts no worker until the first route
            return self._laya

    def _route_feedback(self):
        with self._laya_lock:
            if self._feedback is None:
                from .route_feedback import RouteFeedback
                self._feedback = RouteFeedback(self.daemon.stores.root)
            return self._feedback

    def _feedback_record(self, mode, text, decision):
        """Journal a new routed command for owner review. Never affects dispatch."""
        try:
            token = self._route_feedback().record(mode, text, decision)
            return token if isinstance(token, str) else None
        except Exception:
            LOG.warning("Could not record voice route feedback")
            return None

    def _feedback_outcome(self, token, status):
        if not token:
            return
        try:
            self._route_feedback().outcome(token, status)
        except Exception:
            LOG.warning("Could not record voice route outcome")

    def _laya_decide(self, mode, text, apps, windows, server=None):
        """One full-utterance Laya decision, or None when the router failed."""
        try:
            decision = self._laya_router(server).route(mode, text, apps=apps, windows=windows)
        except Exception:
            return None
        return decision if isinstance(decision, dict) else None

    @staticmethod
    def _laya_intent(decision, text, apps, windows):
        """Map a router decision to a grounded wire intent; never a typing step.

        Returns (intent, feedback kind). Targets are rechecked here against the
        transcript and the exact names the helper offered, and again on the
        helper before anything runs.
        """
        words = _clean(text, 2000)
        clarify = {"kind": "clarify", "text": "", "target": ""}
        kind = decision.get("kind") if decision else "unavailable"
        target = decision.get("target", "") if decision else ""
        if not isinstance(target, str) or kind == "unavailable" or not isinstance(kind, str):
            return {"kind": "ask", "text": words, "target": "conversation"}, "unavailable"
        if kind == "ask":
            return {"kind": "ask", "text": words, "target": ""}, kind
        if kind == "read_selection":
            return {"kind": "read_selection", "text": words, "target": ""}, kind
        sites = spoken_websites(text)
        if kind == "navigate":
            if target and web_destination(target) == target and sites == (target,):
                return {"kind": "navigate", "text": words, "target": target}, kind
        elif kind in ("open_app", "switch_app"):
            offered = apps if kind == "open_app" else windows
            if target in offered and spoken_span(text, target):
                return {"kind": kind, "text": words, "target": target}, kind
        elif kind == "search":
            if 0 < len(target) <= 500 and spoken_span(text, target):
                return {"kind": "search", "text": words, "target": target}, kind
        elif kind == "clarify" and len(sites) == 2:
            scores = decision.get("scores")
            ranked = sorted(((v, k) for k, v in scores.items()
                             if isinstance(k, str) and isinstance(v, (int, float))
                             and not isinstance(v, bool)), reverse=True)[:2] if isinstance(scores, dict) else []
            if any(k == "navigate" for _, k in ranked):
                return {"kind": "clarify", "text": "", "target": "website", "choices": list(sites)}, "clarify"
        return clarify, "clarify"

    def _converse(self, text, on_delta):
        from . import voice_model
        return voice_model.converse(text, on_delta,
                                    cancelled=getattr(self._context, "cancelled", lambda: False))

    def _ask(self, text, on_delta):
        return self._voice_chat.ask(text, on_delta, cancelled=getattr(self._context, "cancelled", lambda: False),
                                    timeout=ASK_TIMEOUT_S, on_reset=getattr(self._context, "reset", None))

    @staticmethod
    def _speaker(text):
        from jarvis import voice
        return voice.tts(text[:SAY_CHARS], local_only=True)

    def _current(self, conn, server):
        with self._lock:
            return self._active is conn and self._server is server and not self._hud_mic_paused

    def _stream_ask(self, conn, server, stream, ident, text, *, speak=False, defer_speech=False,
                    conversation_only=False):
        """Bound the wait independently of providers; replacement ends this stream.

        The queue is bounded. A stalled reader cannot retain an unbounded answer,
        and the worker never writes a socket after this request has ended.
        """
        events = queue.Queue(maxsize=256)
        stopped = threading.Event()
        deadline = time.monotonic() + ASK_TIMEOUT_S

        def cancelled():
            return stopped.is_set() or not self._current(conn, server)

        def offer(kind, value):
            while not cancelled():
                try:
                    events.put((kind, value), timeout=0.1)
                    return
                except queue.Full:
                    pass
            raise ConnectionError()

        # The answer worker and local synthesizer run independently. Only this
        # request thread touches the wire. At most SAY_CHARS enter the TTS queue.
        interruptible = speak
        sentences = queue.Queue()
        partial = ""
        spoken_until = 0
        queued_chars = 0
        final = None

        def render():
            try:
                while not cancelled():
                    try:
                        sentence = sentences.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if sentence is None:
                        offer("speech_done", "")
                        return
                    wav = self.speaker(sentence)
                    if not isinstance(wav, bytes) or not wav or len(wav) > (wire.MAX_AUDIO_LINE - 1024) * 3 // 4:
                        raise ValueError()
                    sent = threading.Event()
                    offer("audio", (wav, sent))
                    while not sent.wait(0.1) and not cancelled():
                        pass
            except Exception:
                if not cancelled():
                    offer("speech_failed", "")

        def queue_sentences(value, *, finish=False):
            nonlocal spoken_until, queued_chars
            # Across pre-tool and post-tool steps keep the original per-answer
            # speech cap. Reserve room for a confirmed task receipt.
            remaining = max(0, SAY_CHARS - queued_chars - (0 if finish else 64))
            tail = value[spoken_until:spoken_until + remaining]
            for match in re.finditer(r".+?(?:[.!?]+(?=\s)|\n+)", tail, re.S):
                sentence = match.group().strip()
                if sentence:
                    sentences.put(sentence)
                spoken_until += len(match.group())
                queued_chars += len(match.group())
            if finish:
                rest = value[spoken_until:spoken_until + SAY_CHARS - queued_chars].strip()
                if rest:
                    sentences.put(rest)
            if finish:
                sentences.put(None)

        if speak:
            threading.Thread(target=render, name="voice-sentence-tts", daemon=True).start()

        def work():
            self._context.cancelled = cancelled
            self._context.reset = lambda: offer("reset", "")
            try:
                answer = (self.converser if conversation_only else self.asker)(
                    text, lambda piece: offer("delta", piece))
                result = ("done", answer)
            except NoVoiceTarget:
                result = ("error", ASK_NO_TARGET)
            except TimeoutError:
                result = ("error", ASK_TIMEOUT)
            except Exception:
                result = ("error", VOICE_DOWN if conversation_only else ASK_FAILED)
            try:
                offer(*result)
            except ConnectionError:
                pass

        threading.Thread(target=work, name="jarvis-voice-answer", daemon=True).start()
        try:
            while self._current(conn, server):
                if interruptible and select.select([conn], [], [], 0)[0]:
                    # During an ask the agent may only withdraw this exact reply.
                    if wire.read_json(stream, 256) != {"op": "interrupt", "id": ident}:
                        return None
                    return {"id": ident, "ok": True, "done": True, "text": ""}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"id": ident, "ok": False, "error": ASK_TIMEOUT}
                try:
                    kind, value = events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if kind == "reset":
                    # Pre-tool commentary and post-tool replies are distinct
                    # model steps. Never use one as a prefix of the other.
                    spoken_until, partial = 0, ""
                    continue
                if kind == "speech_done":
                    return final
                if kind == "speech_failed":
                    speak = False
                    if final is not None:
                        return final
                    continue
                if kind == "audio":
                    wav, sent = value
                    wire.send_json(stream, {"id": ident, "event": "audio",
                                            "wav_b64": base64.b64encode(wav).decode("ascii")})
                    sent.set()
                    continue
                if kind == "error":
                    return {"id": ident, "ok": False, "error": value}
                if not isinstance(value, str):
                    return {"id": ident, "ok": False, "error": ASK_FAILED}
                if kind == "done":
                    reply = {"id": ident, "ok": True, "done": True, "text": value}
                else:
                    reply = {"id": ident, "event": "delta", "text": value}
                # Check the exact wire bytes, including JSON escaping and the
                # newline, before writing any part of an oversized answer.
                if len(wire.json_line(reply)) > wire.MAX_LINE:
                    return {"id": ident, "ok": False, "error": ASK_FAILED}
                if kind == "done":
                    if not speak:
                        return reply
                    final = reply
                    # Final-only providers still speak. A revised final must
                    # never append an unrelated suffix to already-spoken text.
                    if not spoken_until or value.startswith(partial[:spoken_until]):
                        queue_sentences(value, finish=True)
                    else:
                        sentences.put(None)
                elif self._current(conn, server):
                    wire.send_json(stream, reply)
                    if speak and not defer_speech:
                        partial = (partial + value[:SAY_CHARS])[:SAY_CHARS]
                        queue_sentences(partial)
            return None
        finally:
            stopped.set()

    def _laya_route(self, conn, server, request):
        """Validate and route one negotiated command; None if superseded.

        Returns (reply, mode, text, decision, feedback kind, receipt kind), or
        False for a malformed request. The model sees the full utterance once.
        """
        mode, text = request.get("source"), request.get("text")
        apps, windows = request.get("apps", []), request.get("windows", [])

        def names(value):
            return (isinstance(value, list) and len(value) <= wire.LAYA_NAMES
                    and all(isinstance(name, str) and 0 < len(name) <= LAYA_NAME_CHARS
                            and name == _clean(name, LAYA_NAME_CHARS) for name in value))

        if (mode not in ("assistant", "wake") or not isinstance(text, str) or not text.strip()
                or len(text) > 2000 or not names(apps) or not names(windows)
                or request.get("web_navigation") is not True):
            return False
        snapshot = self._voice_chat.selected() if request.get("receipt") is True and self._voice_chat else None
        if not self._current(conn, server):
            return None
        decision = self._laya_decide(mode, text, tuple(apps), tuple(windows), server)
        if not self._current(conn, server):
            return None
        intent, feedback_kind = self._laya_intent(decision, text, tuple(apps), tuple(windows))
        with self._lock:
            self._laya_issue = LAYA_DOWN if feedback_kind == "unavailable" else ""
        if feedback_kind == "unavailable":
            LOG.warning("Laya voice routing unavailable; answering in conversation only")
        kind = intent["kind"]
        receipt_kind = (kind if kind in ("open_app", "switch_app", "search", "read_selection", "navigate")
                        else "navigate" if kind == "clarify" and intent["target"] == "website" else None)
        reply = {"ok": True, "intent": intent, "turn": wire.new_nonce(), "_snapshot": snapshot}
        return reply, mode, text, decision, feedback_kind, receipt_kind

    @staticmethod
    def _receive_capture(conn, stream):
        # A partial or malformed upload never reaches STT. A fresh connection
        # cannot resume an earlier recording. Bound bytes, frames and wall time.
        pcm = bytearray()
        expiry = threading.Timer(wire.MAX_CLIP_S + 10, _shutdown, args=(conn,))
        expiry.daemon = True
        expiry.start()
        try:
            for _ in range(wire.MAX_PCM // wire.PACKET_BYTES + 2):
                frame = wire.read_json(stream, 2 * wire.PACKET_BYTES)
                if frame == {"end": "cancel"}:
                    return None
                if frame == {"end": "commit"} and pcm:
                    return wire.wav_bytes(pcm)
                if set(frame) != {"pcm"} or not isinstance(frame["pcm"], str):
                    break
                try:
                    part = base64.b64decode(frame["pcm"], validate=True)
                except ValueError:
                    break
                if not 0 < len(part) <= wire.PACKET_BYTES or len(part) % 2 or len(pcm) + len(part) > wire.MAX_PCM:
                    break
                pcm.extend(part)
            raise wire.LinkError(BAD_CLIP)
        finally:
            expiry.cancel()

    def start(self):
        with self._lock:
            if self._server is not None:
                return True
            try:
                # Refuse links, FIFOs/devices (including before open, to avoid
                # blocking), and permissions; fstat checks the opened inode.
                before = self.key_path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077:
                    raise wire.KeyFileError()
                fd = os.open(self.key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    info = os.fstat(f.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                        raise wire.KeyFileError()
                    key = wire.parse_key(f.read(66))
            except (OSError, wire.KeyFileError):
                self.status = "Pairing key missing or unsafe; run jarvis voice-link setup."
                return False
            server = socket.socket()
            try:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", self.port))
                server.listen(8)
                server.settimeout(0.25)
            except OSError:
                server.close()
                self.status = "Voice link port unavailable."
                return False
            self._key, self._server = key, server
            self.address = server.getsockname()
            self.port = self.address[1]
            self.status = "Listening."
            with self._laya_lock:
                self._laya_open, self._laya_owner = True, server
            self._thread = threading.Thread(target=self._accept, args=(server,),
                                            name="jarvis-voice-link", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
            sockets = list(self._connections)
            self._active = None
            self._end_subscriptions_locked()
            self.address = None
            self.status = "Stopped."
            # Detach the router atomically with the server: a route still in a
            # handler gets no new router, and a start() racing this stop gets
            # a fresh one rather than this one's closed state.
            with self._laya_lock:
                self._laya_open, self._laya_owner = False, None
                router, self._laya = self._laya, None
        if server:
            _shutdown(server)
        for conn in sockets:
            _shutdown(conn)
        if self._thread:
            self._thread.join(1)
        if router is not None:
            try:
                router.close()
            except Exception:
                pass

    def _accept(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                if self._server is not server or self._pending >= MAX_UNAUTHENTICATED:
                    conn.close()
                    continue
                self._pending += 1
                self._connections.add(conn)
            threading.Thread(target=self._serve, args=(conn, server, self._key),
                             name="jarvis-voice-agent", daemon=True).start()

    def _serve(self, conn, server, key):
        pending = True
        stream = None
        # A wall-clock deadline, even if a peer trickles one byte at a time.
        timer = threading.Timer(HANDSHAKE_TIMEOUT_S, _shutdown, args=(conn,))
        timer.daemon = True
        timer.start()
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(HANDSHAKE_TIMEOUT_S)
            stream = conn.makefile("rwb")
            hello = wire.read_json(stream, 4096)
            a = hello.get("nonce_a")
            microphone = hello.get("hello") == "jarvis-hud-microphone"
            session = hello.get("session")
            if microphone:
                if not isinstance(session, str) or not wire.HEX64.fullmatch(session):
                    return
                key = wire.microphone_key(key, session)
            if ((not microphone and hello.get("hello") != "jarvis-voice-agent") or type(hello.get("v")) is not int
                    or hello["v"] != 1 or not isinstance(a, str) or not wire.HEX64.fullmatch(a)):
                return
            d = wire.new_nonce()
            wire.send_json(stream, {"hello": "jarvis-voice-link", "v": 1, "nonce_d": d,
                                    "proof": wire.daemon_proof(key, a, d)})
            proof = wire.read_json(stream, 4096).get("proof")
            if (not isinstance(proof, str) or not wire.HEX64.fullmatch(proof)
                    or not hmac.compare_digest(wire.agent_proof(key, d, a), proof)):
                return
            timer.cancel()
            conn.settimeout(None)
            with self._lock:
                if self._server is not server:
                    return
                if microphone:
                    if (session != self._control_session or self._active is None
                            or self._instance is None or self._mic is not None):
                        return
                    self._mic = conn
                    session_instance = self._instance
                else:
                    old, self._active = self._active, conn
                    self._laya_helper = None
                    self._control_session = d
                    self._instance = None
                    self._agent_wake = None
                    self._launch_issue = ""
                    self._lost_pairing = False
                    self._end_subscriptions_locked()
                    if self._mic is not None:
                        _shutdown(self._mic)
                        self._mic = None
                    if old:
                        _shutdown(old)
                self._pending -= 1
                pending = False
            if microphone:
                self._serve_microphone(conn, stream, session_instance)
                return
            # One in-flight action per sequential helper, bound to this authenticated
            # connection. Only a non-sensitive action description survives routing.
            pending_action = None
            # A negotiated helper's latest routed turn on this connection:
            # {"handle", "kind", "token"}. Feedback only; never authority.
            pending_turn = None
            laya_conn = False
            while True:
                after_reply = None
                try:
                    request = wire.read_json(stream)
                except (wire.LinkError, ValueError):
                    wire.send_json(stream, {"id": None, "ok": False, "error": BAD_REQUEST})
                    return  # no unbounded draining of an oversize line
                with self._lock:
                    if self._active is not conn or self._server is not server:
                        return
                    if (request.get("op") != "ping" and self._instance is not None
                            and self._instance == self._stopped_instance):
                        return
                ident = request.get("id")
                reply = {"id": ident if type(ident) is int else None, "ok": False, "error": BAD_REQUEST}
                if type(ident) is int and request.get("op") == "interrupt":
                    # A release may race the final reply. It has no response.
                    continue
                if type(ident) is int and request.get("op") == "ping":
                    wake = request.get("wake")
                    if "wake" not in request or (isinstance(wake, str) and wake in WAKE_STATUS):
                        with self._lock:
                            if self._active is not conn:
                                return
                            instance = request.get("instance")
                            if (isinstance(instance, str) and wire.HEX64.fullmatch(instance)
                                    and self._instance in (None, instance)):
                                self._instance = instance
                                if instance != self._stopped_instance:
                                    self._hud_mic_paused = False
                                    self._release = None
                            if wake is not None and self._mic is None:
                                self._agent_wake = wake
                        reply = {"id": ident, "ok": True}
                        if request.get("features") is True:
                            reply["streaming"] = True
                            reply["action_receipts"] = True
                            reply["web_navigation"] = True
                            if self._instance is not None:
                                reply["shared_mic"] = True
                            # Paired negotiation: granted only to a helper that asks.
                            laya_conn = request.get("laya_routing") is True
                            if laya_conn:
                                reply["laya_routing"] = True
                            with self._lock:
                                if self._active is conn:
                                    self._laya_helper = laya_conn
                elif (type(ident) is int and request.get("op") == "action_done"
                      and set(request) == {"id", "op", "receipt"}):
                    recorded = False
                    if pending_action and pending_action[0] == request["receipt"]:
                        _, expiry, thread_id, kind, target = pending_action
                        pending_action = None  # a retry can never append twice
                        if time.monotonic() < expiry:
                            try:
                                recorded = self._voice_chat.note_action(thread_id, kind, target)
                            except (OSError, StoreError):
                                pass
                    reply = {"id": ident, "ok": True, "recorded": recorded}
                elif (type(ident) is int and request.get("op") == "route_outcome" and laya_conn
                      and set(request) == {"id", "op", "turn", "status"}
                      and request["status"] in wire.LAYA_OUTCOMES):
                    # Observational only: what the helper saw happen. Never a label.
                    # Written before the reply; the action itself already ran.
                    if pending_turn and pending_turn["handle"] == request["turn"]:
                        self._feedback_outcome(pending_turn["token"], request["status"])
                    reply = {"id": ident, "ok": True}
                elif type(ident) is int and request.get("op") == "clean":
                    text = request.get("text")
                    if isinstance(text, str) and len(text) <= CLEAN_CHARS:
                        try:
                            result = self._clean_dictation(text, request.get("app"))
                            reply = {"id": ident, "ok": True, **result}
                        except Exception:
                            pass  # fixed BAD_REQUEST; no exception or text escapes
                elif type(ident) is int and request.get("op") == "scratch":
                    which = request.get("pass")
                    if isinstance(which, str):
                        with self._lock:
                            if which in self._cleanup_counts:
                                self._cleanup_counts[which]["scratched"] += 1
                        reply = {"id": ident, "ok": True}
                elif type(ident) is int and request.get("op") in ("dictate", "dictate_stream"):
                    source = request.get("source", "hotkey")
                    if source not in ("hotkey", "wake"):
                        wire.send_json(stream, reply)
                        continue
                    try:
                        if request["op"] == "dictate_stream":
                            if source != "hotkey":
                                return
                            from jarvis import stt_local
                            stt_local.prewarm_for_voice()
                            data = self._receive_capture(conn, stream)
                            if data is None:
                                wire.send_json(stream, {"id": ident, "ok": True, "cancelled": True})
                                continue
                            if not self._current(conn, server):
                                return
                        else:
                            encoded = request.get("wav_b64")
                            if not isinstance(encoded, str):
                                raise ValueError()
                            data = base64.b64decode(encoded, validate=True)
                        remote_stt.check_wav(data, max_s=wire.MAX_CLIP_S)
                    except (ValueError, remote_stt._Refused):
                        reply["error"] = BAD_CLIP
                    else:
                        try:
                            transcribe = self.transcribe_wake if source == "wake" else self.transcribe
                            got = transcribe(data)  # never hold the connection lock here
                            if not isinstance(got["text"], str) or got["backend"] not in ("local", "cloud"):
                                raise ValueError()
                            reply = {"id": ident, "ok": True, "text": got["text"], "backend": got["backend"]}
                        except NotOwner:
                            reply["error"] = NOT_OWNER
                        except Exception:
                            reply["error"] = FAILED
                elif (type(ident) is int and request.get("op") == "route" and request.get("laya") is True
                      and laya_conn):
                    pending_action = pending_turn = None
                    routed = self._laya_route(conn, server, request)
                    if routed is None:
                        return  # superseded during inference: never deliver its decision
                    if routed is False:
                        wire.send_json(stream, reply)
                        continue
                    reply, mode, text, decision, feedback_kind, receipt_kind = routed
                    reply["id"] = ident
                    handle = reply["turn"]
                    pending_turn = {"handle": handle, "kind": feedback_kind, "token": None}
                    if receipt_kind and request.get("receipt") is True and self._voice_chat:
                        snapshot = reply.pop("_snapshot", None)
                        if snapshot:
                            token = wire.new_nonce()
                            target = reply["intent"]["target"] if receipt_kind in ("open_app", "switch_app") else ""
                            pending_action = (token, time.monotonic() + ACTION_RECEIPT_S, snapshot,
                                              receipt_kind, target)
                            reply["receipt"] = token
                    reply.pop("_snapshot", None)

                    def after_reply(turn=pending_turn, mode=mode, text=text, decision=decision):
                        # After the reply, so feedback adds no dispatch latency
                        # and its failure cannot change the decision.
                        turn["token"] = self._feedback_record(
                            mode, text, decision if decision is not None else {"kind": "unavailable"})
                        if turn["kind"] in ("clarify", "unavailable"):
                            self._feedback_outcome(turn["token"], "clarified" if turn["kind"] == "clarify"
                                                   else "unavailable")
                elif type(ident) is int and request.get("op") in ("route", "ask", "say"):
                    op, text = request["op"], request.get("text")
                    source = request.get("source")
                    if (isinstance(text, str) and text.strip()
                            and (op != "route" or source in ("hotkey", "wake"))):
                        if op == "route":
                            pending_action = None
                            snapshot = None
                            addressed = source == "wake" or voice_intent.addressed(text)
                            shortcut_target = (voice_intent.shortcut_open_target(text)
                                               if not addressed and request.get("exact_open") is True else "")
                            navigation = navigation_target(text, source)
                            navigation_capable = request.get("web_navigation") is True
                            if (request.get("receipt") is True and self._voice_chat
                                    and (addressed or shortcut_target or (navigation and navigation_capable))):
                                snapshot = self._voice_chat.selected()
                            try:
                                raw = self.route_model(text[:2000], source)
                            except Exception:
                                raw = None
                            intent = parse_intent(raw, source, text)
                            if navigation and (not navigation_capable or intent["kind"] != "navigate"):
                                # Empty literal dictation is safe on legacy helpers too:
                                # it neither inserts text on a hotkey nor asks on wake.
                                intent = {"kind": "dictate", "text": "", "target": "literal"}
                            kind = intent["kind"]
                            exact_open = (kind == "open_app" and shortcut_target
                                          and shortcut_target.casefold() == intent["target"].casefold())
                            if not addressed and kind not in ("dictate", "navigate") and not exact_open:
                                # Unaddressed launches require a helper that promises
                                # exact catalog resolution.
                                kind = "dictate"
                                intent = {"kind": kind, "text": _clean(text, 2000), "target": "literal"}
                            reply = {"id": ident, "ok": True, "intent": intent}
                            if snapshot and kind in ("open_app", "switch_app", "search", "read_selection", "navigate"):
                                token = wire.new_nonce()
                                target = intent["target"] if kind in ("open_app", "switch_app") else ""
                                pending_action = (token, time.monotonic() + ACTION_RECEIPT_S, snapshot, kind, target)
                                reply["receipt"] = token
                        elif op == "ask":
                            # After a Laya outage the answer is conversation only,
                            # whatever the helper asks: no tools and no task.
                            routed = (pending_turn if laya_conn and pending_turn
                                      and request.get("turn") == pending_turn["handle"] else None)
                            conversation_only = bool(laya_conn and pending_turn
                                                     and pending_turn["kind"] == "unavailable")
                            reply = self._stream_ask(conn, server, stream, ident, text,
                                                     speak=request.get("speech") is True,
                                                     defer_speech=request.get("defer_speech") is True,
                                                     conversation_only=conversation_only)
                            if reply is None:
                                return
                            if routed and reply.get("ok") is True and reply.get("text"):
                                self._feedback_outcome(routed["token"], "answered")
                        else:
                            try:
                                audio = self.speaker(text.strip()[:SAY_CHARS])
                                if not isinstance(audio, bytes) or not audio:
                                    raise ValueError()
                                encoded = base64.b64encode(audio).decode("ascii")
                                if len(encoded) > wire.MAX_AUDIO_LINE - 1024:
                                    raise ValueError()
                                reply = {"id": ident, "ok": True, "wav_b64": encoded}
                            except Exception:
                                reply["error"] = SAY_FAILED
                with self._lock:
                    if self._active is not conn or self._server is not server:
                        return
                wire.send_json(stream, reply)
                if after_reply is not None:
                    after_reply()
        except (OSError, ValueError, wire.LinkError):
            pass
        finally:
            timer.cancel()
            _shutdown(conn)
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
            with self._lock:
                self._connections.discard(conn)
                if pending:
                    self._pending -= 1
                if self._mic is conn:
                    self._mic = None
                    self._end_subscriptions_locked()
                if self._active is conn:
                    if self._server is server and not self._hud_mic_paused:
                        # TCP EOF can arrive just before WSL reaps the helper.
                        # Retain the loss even if only a later poll sees its exit.
                        self._lost_pairing = True
                        self._lost_instance = self._instance
                    self._active = None
                    self._laya_helper = None
                    self._control_session = self._instance = self._agent_wake = None
                    self._end_subscriptions_locked()
                    # On Stop the side channel outlives the main connection
                    # just long enough for the actual device-release ACK.
                    if self._mic is not None and self._release is None:
                        _shutdown(self._mic)
                        self._mic = None

    def _serve_microphone(self, conn, stream, session_instance):
        # Each heartbeat or fixed-size PCM frame is followed by exactly one
        # demand/idle/stop byte. No audio flows back to any native link.
        states = {code: name for name, code in wire.MIC_STATES.items()}
        conn.settimeout(3)
        while True:
            kind = stream.read(1)
            if kind not in (b"A", b"P"):
                return
            wake = states.get(stream.read(1))
            if wake is None:
                return
            pcm = stream.read(wire.MIC_PACKET_BYTES) if kind == b"A" else b""
            if kind == b"A" and len(pcm) != wire.MIC_PACKET_BYTES:
                return
            with self._lock:
                if self._mic is not conn:
                    return
                if session_instance == self._stopped_instance:
                    command = b"X"
                    release = self._release
                else:
                    if self._active is None:
                        return
                    self._agent_wake = wake
                    if wake != "ready":
                        self._end_subscriptions_locked()
                    # A slow reader loses old frames instead of delaying WASAPI.
                    if pcm:
                        for frames in self._subscribers:
                            if frames.full():
                                try:
                                    frames.get_nowait()
                                except queue.Empty:
                                    pass
                            frames.put_nowait(pcm)
                    command = b"A" if self._subscribers else b"I"
            stream.write(command)
            stream.flush()
            if command == b"X":
                conn.settimeout(10)
                if stream.read(1) == b"X" and release is not None:
                    with self._lock:
                        if self._mic is conn and self._release is release:
                            release.set()
                return
