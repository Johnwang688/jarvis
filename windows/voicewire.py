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
import wave

PROTOCOL = "jarvis-voice-link/1"
MAX_CLIP_S = 120
MAX_LINE = 6 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


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


def send_json(stream, value):
    stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
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

    def _request(self, payload):
        if self._stream is None:
            raise LinkError("Jarvis isn't running")
        try:
            self._id += 1
            send_json(self._stream, {**payload, "id": self._id})
            got = read_json(self._stream)
            if got.get("id") != self._id or type(got.get("ok")) is not bool:
                raise ValueError()
        except Exception:
            self.close()
            raise LinkError("The voice link disconnected.") from None
        if not got["ok"]:
            # Never display arbitrary peer exception text or paths.
            raise LinkError("The clip could not be transcribed.")
        return got

    def ping(self):
        self._request({"op": "ping"})

    def dictate(self, wav):
        got = self._request({"op": "dictate", "wav_b64": base64.b64encode(wav).decode("ascii")})
        if not isinstance(got.get("text"), str) or got.get("backend") not in ("local", "cloud"):
            self.close()
            raise LinkError("The voice link sent an invalid reply.")
        return {"text": got["text"], "backend": got["backend"]}

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
