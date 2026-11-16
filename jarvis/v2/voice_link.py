"""Owner-started Windows microphone link, jarvis-voice-link/1 (VA-1).

Decision VA-D1/D2: a physical Right Ctrl gesture authorizes dictation with
verify=False, as on the paired phone. The owner may change that decision.
The existing desk STT switches still decide whether audio may reach cloud.
VA-1 returns overlay text only: no typing, tools, send or approval path.
Clips and words stay in memory; neither is logged or written here.
"""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path
import socket
import stat
import threading

from jarvis import config
from windows import voicewire as wire
from . import remote_stt

HANDSHAKE_TIMEOUT_S = 5.0
MAX_UNAUTHENTICATED = 4
BAD_CLIP = "The clip must be a 16 kHz mono 16-bit WAV of at most 120 seconds."
BAD_REQUEST = "The voice request is invalid."
FAILED = "The clip could not be transcribed."


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class VoiceLink:
    def __init__(self, daemon, *, port=None, key_path=None, transcribe=None):
        self.daemon = daemon
        self.port = config.VOICE_LINK_PORT if port is None else port
        self.key_path = Path(config.voice_link_key() if key_path is None else key_path)
        self.transcribe = self._transcribe if transcribe is None else transcribe
        self.address = None
        self.status = "Not started."
        self._server = self._active = self._thread = None
        self._connections = set()
        self._pending = 0
        self._lock = threading.Lock()
        self._key = None

    def _transcribe(self, data):
        from . import hud_api
        from .remote_api import _scrub
        hud_api.watch_stt(self.daemon)
        _kind, got = hud_api.dictate(self.daemon, data, "audio/wav",
                                    budget=remote_stt.BUDGET_S, verify=False)
        return {"text": _scrub(str(got.text or ""))[:remote_stt.TEXT_CHARS] if got else "",
                "backend": got.backend if got else "local"}

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
            self._thread = threading.Thread(target=self._accept, args=(server,),
                                            name="jarvis-voice-link", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
            sockets = list(self._connections)
            self._active = None
            self.address = None
            self.status = "Stopped."
        if server:
            _shutdown(server)
        for conn in sockets:
            _shutdown(conn)
        if self._thread:
            self._thread.join(1)

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
            conn.settimeout(HANDSHAKE_TIMEOUT_S)
            stream = conn.makefile("rwb")
            hello = wire.read_json(stream, 4096)
            a = hello.get("nonce_a")
            if (hello.get("hello") != "jarvis-voice-agent" or type(hello.get("v")) is not int
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
                old, self._active = self._active, conn
                self._pending -= 1
                pending = False
                if old:
                    _shutdown(old)
            while True:
                try:
                    request = wire.read_json(stream)
                except (wire.LinkError, ValueError):
                    wire.send_json(stream, {"id": None, "ok": False, "error": BAD_REQUEST})
                    return  # no unbounded draining of an oversize line
                with self._lock:
                    if self._active is not conn or self._server is not server:
                        return
                ident = request.get("id")
                reply = {"id": ident if type(ident) is int else None, "ok": False, "error": BAD_REQUEST}
                if type(ident) is int and request.get("op") == "ping":
                    reply = {"id": ident, "ok": True}
                elif type(ident) is int and request.get("op") == "dictate":
                    try:
                        encoded = request.get("wav_b64")
                        if not isinstance(encoded, str):
                            raise ValueError()
                        data = base64.b64decode(encoded, validate=True)
                        remote_stt.check_wav(data, max_s=wire.MAX_CLIP_S)
                    except (ValueError, remote_stt._Refused):
                        reply["error"] = BAD_CLIP
                    else:
                        try:
                            got = self.transcribe(data)  # never hold the connection lock here
                            if not isinstance(got["text"], str) or got["backend"] not in ("local", "cloud"):
                                raise ValueError()
                            reply = {"id": ident, "ok": True, "text": got["text"], "backend": got["backend"]}
                        except Exception:
                            reply["error"] = FAILED
                with self._lock:
                    if self._active is not conn or self._server is not server:
                        return
                wire.send_json(stream, reply)
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
                if self._active is conn:
                    self._active = None
