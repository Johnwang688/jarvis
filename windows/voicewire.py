"""Pure jarvis-voice-link/1 framing, pairing proofs and hotkey state machine."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import queue
import secrets
import socket
import threading
import time
import wave

if __package__:
    from .voicetype import navigation_target
else:
    from voicetype import navigation_target

PROTOCOL = "jarvis-voice-link/1"
MAX_CLIP_S = 120
MAX_LINE = 6 * 1024 * 1024
# Local TTS WAVs are larger than microphone uploads (typically 24 kHz).
MAX_AUDIO_LINE = 32 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
ERRORS = frozenset(("The clip could not be transcribed.", "That didn't sound like you.",
                    "The voice request is invalid.", "The text could not be spoken.",
                    "The voice question could not be answered.",
                    "Open a chat in the HUD, then ask again.",
                    "The voice answer took too long. Check the selected chat in the HUD.",
                    "The voice answer took too long. Please check the Voice chat.",
                    "The clip must be a 16 kHz mono 16-bit WAV of at most 120 seconds."))
KINDS = frozenset(("dictate", "ask", "open_app", "switch_app", "search", "read_selection", "navigate"))


class LinkError(Exception):
    pass


class VoiceQuestionError(LinkError):
    """The daemon rejected this question while the paired connection was healthy."""


class NotJarvis(LinkError):
    pass


class KeyFileError(Exception):
    pass


def new_nonce():
    return secrets.token_hex(32)


def daemon_proof(key, nonce_a, nonce_d):
    return hmac.new(key, f"daemon|{nonce_a}|{nonce_d}".encode("ascii"), hashlib.sha256).hexdigest()


def agent_proof(key, nonce_d, nonce_a):
    return hmac.new(key, f"agent|{nonce_d}|{nonce_a}".encode("ascii"), hashlib.sha256).hexdigest()


def parse_key(data):
    if not re.fullmatch(rb"[0-9a-fA-F]{64}\n?", data):
        raise KeyFileError("Not paired: run jarvis voice-link setup")
    return bytes.fromhex(data.decode("ascii"))


def load_key(path):
    try:
        with Path(path).open("rb") as f:
            return parse_key(f.read(66))
    except (OSError, ValueError):
        raise KeyFileError("Not paired: run jarvis voice-link setup") from None


def wav_bytes(pcm: bytes, rate=16000):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def json_line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def send_json(stream, value):
    stream.write(json_line(value))
    stream.flush()


def read_json(stream, cap=MAX_LINE):
    line = stream.readline(cap + 1)
    if not line or len(line) > cap or not line.endswith(b"\n"):
        raise LinkError("The voice link closed or sent an invalid reply.")
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        raise LinkError("The voice link sent an invalid reply.") from None
    if not isinstance(value, dict):
        raise LinkError("The voice link sent an invalid reply.")
    return value


PACKET_BYTES = 4096  # 128 ms of mono 16 kHz signed 16-bit PCM
MAX_PCM = MAX_CLIP_S * 32000


# Native WASAPI delivers exactly 100 ms at 16 kHz, mono, signed little-endian.
MIC_PACKET_BYTES = 3200
MIC_STATES = {"starting": b"S", "ready": b"R", "microphone_unavailable": b"M",
              "recognizer_unavailable": b"K"}


def microphone_key(key, session):
    # Purpose and parent connection are part of BOTH directions of authentication.
    return hmac.digest(key, b"hud-microphone/1|" + session.encode("ascii"), "sha256")


class Capture:
    """The audio callback only appends. One network worker drains complete packets."""
    def __init__(self):
        self.ready = threading.Condition()
        self.pending = bytearray()
        self.closed = self.cancelled = False
        self.release_at = self.speech_end = None

    def append(self, pcm):
        with self.ready:
            if not self.closed:
                self.pending.extend(pcm)
                if len(self.pending) >= PACKET_BYTES:
                    self.ready.notify()

    def finish(self, cancel=False, *, release_at=None):
        with self.ready:
            self.closed = True
            self.release_at = release_at
            if cancel:
                self.cancelled = True
                self.pending.clear()
            self.ready.notify_all()

    def packets(self):
        while True:
            with self.ready:
                if not self.ready.wait_for(lambda: self.closed or len(self.pending) >= PACKET_BYTES,
                                           timeout=MAX_CLIP_S + 5):
                    self.cancelled = self.closed = True
                    self.pending.clear()
                if not self.pending:
                    return
                packet = bytes(self.pending[:PACKET_BYTES])
                del self.pending[:len(packet)]
            yield packet


class Client:
    def __init__(self, host, port, key, timeout=10.0):
        self.host, self.port, self.key, self.timeout = host, port, key, timeout
        self._socket = self._stream = None
        self._id = 0
        self.streaming = False
        self.session = None
        self.shared_capable = False
        self.action_receipts = False
        self.web_navigation = False
        self._request_lock = threading.Lock()
        self._speech_lock = threading.Lock()
        self._speaking_id = None

    def connect(self, *, microphone_session=None):
        self.close()
        try:
            self._socket = socket.create_connection((self.host, self.port), min(self.timeout, 5.0))
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._stream = self._socket.makefile("rwb")
            a = new_nonce()
            hello = {"hello": "jarvis-voice-agent", "v": 1, "nonce_a": a}
            proof_key = self.key
            if microphone_session is not None:
                hello.update(hello="jarvis-hud-microphone", session=microphone_session)
                proof_key = microphone_key(self.key, microphone_session)
            send_json(self._stream, hello)
            got = read_json(self._stream, 4096)
            d, proof = got.get("nonce_d"), got.get("proof")
            if (got.get("hello") != "jarvis-voice-link" or type(got.get("v")) is not int
                    or got["v"] != 1 or not isinstance(d, str) or not HEX64.fullmatch(d)
                    or not isinstance(proof, str) or not HEX64.fullmatch(proof)
                    or not hmac.compare_digest(daemon_proof(proof_key, a, d), proof)):
                raise NotJarvis("That wasn't Jarvis — refused")
            send_json(self._stream, {"proof": agent_proof(proof_key, d, a)})
            self._socket.settimeout(self.timeout)
            self.session = d
        except Exception as exc:
            self.close()
            if isinstance(exc, NotJarvis):
                raise
            raise LinkError("Jarvis isn't running") from None

    def _request(self, payload, on_delta=None, on_status=None):
        with self._request_lock:
            return self._exchange(payload, on_delta, on_status)

    def _exchange(self, payload, on_delta, on_status, on_audio=None, cancelled=None):
        if self._stream is None:
            raise LinkError("Jarvis isn't running")
        try:
            self._id += 1
            with self._speech_lock:
                if cancelled is not None and cancelled.is_set():
                    return {"done": True, "text": ""}
                send_json(self._stream, {**payload, "id": self._id})
                if on_audio:
                    self._speaking_id = self._id
            asking = payload.get("op") == "ask"
            deadline = time.monotonic() + 185.0
            while True:
                if asking and self._socket is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError()
                    self._socket.settimeout(remaining)
                got = read_json(self._stream, MAX_AUDIO_LINE if on_audio or payload.get("op") == "say" else MAX_LINE)
                if type(got.get("id")) is not int or got["id"] != self._id:
                    raise ValueError()
                if asking and "event" in got:
                    if got["event"] == "delta" and isinstance(got.get("text"), str):
                        if on_delta:
                            on_delta(got["text"])
                    elif got["event"] == "audio" and on_audio and isinstance(got.get("wav_b64"), str):
                        on_audio(base64.b64decode(got["wav_b64"], validate=True))
                    elif got["event"] == "status" and isinstance(got.get("text"), str):
                        if on_status:
                            on_status(got["text"])
                    else:
                        raise ValueError()
                    continue
                if type(got.get("ok")) is not bool:
                    raise ValueError()
                break
        except Exception:
            self.close()
            raise LinkError("The voice link disconnected.") from None
        finally:
            with self._speech_lock:
                self._speaking_id = None
            if self._socket is not None:
                self._socket.settimeout(self.timeout)
        if not got["ok"]:
            # Never display arbitrary peer exception text or paths.
            error = got.get("error")
            message = error if isinstance(error, str) and error in ERRORS else "The voice request failed."
            if asking:
                raise VoiceQuestionError(message)
            raise LinkError(message)
        return got

    def ping(self, *, wake=None, instance=None):
        got = self._request({"op": "ping", "features": True, **({"wake": wake} if wake is not None else {}),
                             **({"instance": instance} if instance is not None else {})})
        self.streaming = got.get("streaming") is True
        self.shared_capable = got.get("shared_mic") is True
        self.action_receipts = got.get("action_receipts") is True
        self.web_navigation = got.get("web_navigation") is True

    def dictate(self, wav, source="hotkey"):
        got = self._request({"op": "dictate", "wav_b64": base64.b64encode(wav).decode("ascii"),
                             "source": source})
        if not isinstance(got.get("text"), str) or got.get("backend") not in ("local", "cloud"):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return {"text": got["text"], "backend": got["backend"]}

    def dictate_capture(self, capture):
        if not self.streaming:
            pcm = b"".join(capture.packets())
            return self.dictate(wav_bytes(pcm)) if pcm and not capture.cancelled else None
        with self._request_lock:
            self._id += 1
            ident = self._id
            try:
                send_json(self._stream, {"op": "dictate_stream", "id": ident})
                total = 0
                for packet in capture.packets():
                    send_json(self._stream, {"pcm": base64.b64encode(packet).decode("ascii")})
                    total += len(packet)
                cancel = capture.cancelled or not total
                send_json(self._stream, {"end": "cancel" if cancel else "commit"})
                got = read_json(self._stream)
                if type(got.get("id")) is not int or got["id"] != ident or type(got.get("ok")) is not bool:
                    raise ValueError()
                if not got["ok"]:
                    error = got.get("error")
                    raise LinkError(error if isinstance(error, str) and error in ERRORS
                                    else "The voice request failed.")
                if cancel and got.get("cancelled") is True:
                    return None
                if cancel or not isinstance(got.get("text"), str) or got.get("backend") not in ("local", "cloud"):
                    raise ValueError()
                return got
            except (ValueError, OSError):
                self.close()
                raise LinkError("The voice link disconnected.") from None

    def route(self, text, source, *, with_receipt=False):
        request = {"op": "route", "text": text, "source": source}
        if source == "hotkey":
            request["exact_open"] = True  # unaddressed launches check the local Start catalog
        if with_receipt and self.action_receipts:
            request["receipt"] = True
        if self.web_navigation:
            request["web_navigation"] = True
        got = self._request(request)
        value = got.get("intent")
        if (not isinstance(value, dict) or set(value) != {"kind", "text", "target"}
                or not isinstance(value["kind"], str) or value["kind"] not in KINDS
                or not isinstance(value["text"], str) or len(value["text"]) > 2000
                or not isinstance(value["target"], str)
                or (value["kind"] != "navigate" and len(value["target"]) > 80)
                or (value["kind"] == "navigate" and
                    (not request.get("web_navigation") or not value["target"]
                     or value["target"] != navigation_target(text, source)))):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        receipt = got.get("receipt")
        if request.get("receipt") and isinstance(receipt, str) and HEX64.fullmatch(receipt):
            return {**value, "receipt": receipt}
        return value

    def confirm_action(self, receipt):
        return self._request({"op": "action_done", "receipt": receipt}).get("recorded") is True

    def clean(self, text, app):
        got = self._request({"op": "clean", "text": text, "app": app})
        if (not isinstance(got.get("text"), str) or len(got["text"]) > 2000
                or got.get("pass") not in ("off", "rules", "raw")):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return {"text": got["text"], "pass": got["pass"]}

    def scratch(self, pass_):
        self._request({"op": "scratch", "pass": pass_})

    def ask(self, text, on_delta=None, on_status=None):
        got = self._request({"op": "ask", "text": text}, on_delta, on_status)
        if got.get("done") is not True or not isinstance(got.get("text"), str):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return got["text"]

    def interrupt_speech(self):
        with self._speech_lock:
            if self._speaking_id is not None and self._socket is not None:
                try:
                    self._socket.sendall(json_line({"op": "interrupt", "id": self._speaking_id}))
                except OSError:
                    pass
                self._speaking_id = None

    def ask_spoken(self, text, on_delta, on_audio, cancelled=None, *, defer_speech=False):
        with self._request_lock:
            payload = {"op": "ask", "text": text, "speech": True, "defer_speech": defer_speech}
            got = self._exchange(payload, on_delta, None, on_audio, cancelled)
        if got.get("done") is not True or not isinstance(got.get("text"), str):
            raise LinkError("The voice link sent an invalid reply.")
        return got["text"]

    def say(self, text):
        got = self._request({"op": "say", "text": text})
        try:
            if not isinstance(got.get("wav_b64"), str):
                raise ValueError()
            return base64.b64decode(got["wav_b64"], validate=True)
        except ValueError:
            self.close()
            raise LinkError("The voice link sent an invalid reply.") from None

    def close(self):
        self.action_receipts = False
        self.web_navigation = False
        self.session = None
        sock, stream = self._socket, self._stream
        self._socket = self._stream = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if stream:
            try:
                stream.close()
            except OSError:
                pass
        if sock:
            sock.close()


class MicrophoneRelay:
    """Demand-only PCM feed on a second paired connection, independent of STT/TTS."""

    def __init__(self, client, wake_status, stop_requested, released):
        self.host, self.port, self.key = client.host, client.port, client.key
        self.session = client.session
        self.wake_status, self.stop_requested, self.released = wake_status, stop_requested, released
        self.done = threading.Event()
        self.frames = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="voice-hud-microphone")

    def start(self):
        self.thread.start()

    def offer(self, pcm):
        # The audio callback never performs I/O, waits for capacity or retains
        # sound from before a HUD subscriber requested it.
        frames = self.frames
        if frames is None or len(pcm) != MIC_PACKET_BYTES:
            return
        try:
            frames.put_nowait(pcm)
        except queue.Full:
            pass

    def close(self):
        self.done.set()
        self.frames = None

    def _run(self):
        while not self.done.is_set():
            peer = Client(self.host, self.port, self.key, timeout=3)
            try:
                peer.connect(microphone_session=self.session)
                stream = peer._stream
                frame = b""
                while not self.done.is_set():
                    state = MIC_STATES.get(self.wake_status(), b"S")
                    stream.write((b"A" if frame else b"P") + state + frame)
                    stream.flush()
                    command = stream.read(1)
                    frame = b""
                    if command == b"X":
                        self.frames = None
                        self.stop_requested.set()
                        if self.released.wait(8):
                            stream.write(b"X")
                            stream.flush()
                        return
                    if command == b"A":
                        if self.frames is None:
                            self.frames = queue.Queue(maxsize=2)
                        try:
                            frame = self.frames.get(timeout=0.25)
                        except queue.Empty:
                            pass
                    elif command == b"I":
                        self.frames = None
                        self.done.wait(0.25)
                    else:
                        break
            except (LinkError, OSError, ValueError):
                pass
            finally:
                self.frames = None
                peer.close()
            self.done.wait(0.25)


class HotkeyMachine:
    def __init__(self, tap_ms=300, max_ms=120_000):
        self.tap_ms, self.max_ms = tap_ms, max_ms
        self.state = "idle"
        self.since = 0
        self.held = False

    def feed(self, t_ms, held: bool, other: bool):
        press, release = held and not self.held, self.held and not held
        self.held = held
        # A shortcut cancels even when its poll coincides with the time cap.
        if self.state == "hold" and other:
            self.state = "wait" if held else "idle"
            return ["cancel"]
        if self.state in ("hold", "toggle") and t_ms - self.since >= self.max_ms:
            self.state = "wait" if held else "idle"
            return ["stop"]
        if self.state == "wait":
            if not held:
                self.state = "idle"
        elif self.state == "idle" and press:
            if other:
                self.state = "wait"
            else:
                self.state, self.since = "hold", t_ms
                return ["start"]
        elif self.state == "hold":
            if release:
                if t_ms - self.since < self.tap_ms:
                    self.state = "toggle"
                else:
                    self.state = "idle"
                    return ["stop"]
        elif self.state == "toggle" and press:
            self.state = "wait"
            return ["stop"]
        return []
