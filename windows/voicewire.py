"""Pure jarvis-voice-link/1 framing, pairing proofs and hotkey state machine."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import wave

PROTOCOL = "jarvis-voice-link/1"
MAX_CLIP_S = 120
MAX_LINE = 6 * 1024 * 1024
# Local TTS WAVs are larger than microphone uploads (typically 24 kHz).
MAX_AUDIO_LINE = 32 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
ERRORS = frozenset(("The clip could not be transcribed.", "That didn't sound like you.",
                    "The voice request is invalid.", "The text could not be spoken.",
                    "The voice question could not be answered.",
                    "The voice answer took too long. Please check the Voice chat.",
                    "The clip must be a 16 kHz mono 16-bit WAV of at most 120 seconds."))
KINDS = frozenset(("dictate", "ask", "open_app", "switch_app", "search", "read_selection"))


class LinkError(Exception):
    pass


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


class Client:
    def __init__(self, host, port, key, timeout=10.0):
        self.host, self.port, self.key, self.timeout = host, port, key, timeout
        self._socket = self._stream = None
        self._id = 0
        self._request_lock = threading.Lock()

    def connect(self):
        self.close()
        try:
            self._socket = socket.create_connection((self.host, self.port), min(self.timeout, 5.0))
            self._stream = self._socket.makefile("rwb")
            a = new_nonce()
            send_json(self._stream, {"hello": "jarvis-voice-agent", "v": 1, "nonce_a": a})
            got = read_json(self._stream, 4096)
            d, proof = got.get("nonce_d"), got.get("proof")
            if (got.get("hello") != "jarvis-voice-link" or type(got.get("v")) is not int
                    or got["v"] != 1 or not isinstance(d, str) or not HEX64.fullmatch(d)
                    or not isinstance(proof, str) or not HEX64.fullmatch(proof)
                    or not hmac.compare_digest(daemon_proof(self.key, a, d), proof)):
                raise NotJarvis("That wasn't Jarvis — refused")
            send_json(self._stream, {"proof": agent_proof(self.key, d, a)})
            self._socket.settimeout(self.timeout)
        except Exception as exc:
            self.close()
            if isinstance(exc, NotJarvis):
                raise
            raise LinkError("Jarvis isn't running") from None

    def _request(self, payload, on_delta=None, on_status=None):
        with self._request_lock:
            return self._exchange(payload, on_delta, on_status)

    def _exchange(self, payload, on_delta, on_status):
        if self._stream is None:
            raise LinkError("Jarvis isn't running")
        try:
            self._id += 1
            send_json(self._stream, {**payload, "id": self._id})
            asking = payload.get("op") == "ask"
            deadline = time.monotonic() + 185.0
            while True:
                if asking and self._socket is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError()
                    self._socket.settimeout(remaining)
                got = read_json(self._stream, MAX_AUDIO_LINE if payload.get("op") == "say" else MAX_LINE)
                if type(got.get("id")) is not int or got["id"] != self._id:
                    raise ValueError()
                if asking and "event" in got:
                    if got["event"] == "delta" and isinstance(got.get("text"), str):
                        if on_delta:
                            on_delta(got["text"])
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
            if self._socket is not None:
                self._socket.settimeout(self.timeout)
        if not got["ok"]:
            # Never display arbitrary peer exception text or paths.
            error = got.get("error")
            raise LinkError(error if isinstance(error, str) and error in ERRORS
                            else "The voice request failed.")
        return got

    def ping(self, *, wake=None):
        self._request({"op": "ping"} if wake is None else {"op": "ping", "wake": wake})

    def dictate(self, wav, source="hotkey"):
        got = self._request({"op": "dictate", "wav_b64": base64.b64encode(wav).decode("ascii"),
                             "source": source})
        if not isinstance(got.get("text"), str) or got.get("backend") not in ("local", "cloud"):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return {"text": got["text"], "backend": got["backend"]}

    def route(self, text, source):
        got = self._request({"op": "route", "text": text, "source": source})
        value = got.get("intent")
        if (not isinstance(value, dict) or set(value) != {"kind", "text", "target"}
                or not isinstance(value["kind"], str) or value["kind"] not in KINDS
                or not isinstance(value["text"], str) or len(value["text"]) > 2000
                or not isinstance(value["target"], str) or len(value["target"]) > 80):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return value

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
