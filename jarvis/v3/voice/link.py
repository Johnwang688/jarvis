"""Paired loopback transport for a v3 voice session."""

from collections import deque
from dataclasses import dataclass
from inspect import getattr_static
import logging
import math
from operator import index as as_index
import os
from pathlib import Path
import select
import socket
import stat
import subprocess
import threading
import time

from jarvis import config
from . import wire


LOG = logging.getLogger(__name__)
_BACKLOG = 8
# Bound queue metadata and bytes even at unusually high sample rates or with
# many very short chunks. The PCM duration limit applies independently.
_MAX_CHUNKS = 256


def _owner_file(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _load_key(path):
    before = path.lstat()
    if not _owner_file(before):
        raise wire.ProtocolError("Unsafe voice pairing file.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _owner_file(opened) or not os.path.samestat(before, opened):
            raise wire.ProtocolError("Unsafe voice pairing file.")
        return wire.derive_k3(wire.parse_key(stream.read(66)))


def _wait(poller, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not poller.poll(math.ceil(remaining * 1000)):
        raise TimeoutError()


class _Reader:
    """Carry read-ahead across the JSON/binary boundary under one read deadline."""

    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        self.buffer = bytearray()
        self.poller = select.poll()
        self.poller.register(sock, select.POLLIN)

    def _fill(self, limit):
        while True:
            _wait(self.poller, self.deadline)
            try:
                data = self.sock.recv(min(limit, 8192))
            except BlockingIOError:
                continue
            if not data:
                raise EOFError()
            self.buffer.extend(data)
            return

    def _take(self, size):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def readline(self, cap):
        while len(self.buffer) < cap:
            end = self.buffer.find(b"\n")
            if end >= 0:
                return self._take(end + 1)
            self._fill(cap - len(self.buffer))
        end = self.buffer.find(b"\n", 0, cap)
        return self._take(cap if end < 0 else end + 1)

    def read(self, size):
        while len(self.buffer) < size:
            self._fill(size - len(self.buffer))
        return self._take(size)


@dataclass
class _Sentence:
    index: int
    last_sent_at: float | None = None


class _Peer:
    def __init__(self, sock):
        self.sock = sock
        self.deadline = time.monotonic() + wire.HANDSHAKE_TIMEOUT_S
        self.closed = threading.Event()
        self.pending = True
        self.version = None
        self.level = None
        self.level_times = deque()
        self._changed = threading.Condition()
        self._queue = deque()
        self._audio_s = 0.0
        self._chunks = 0
        self._controls = 0
        self._sentences = {}
        self._writing_last = None
        self._next_index = 0
        self._live_until = None

    def interrupt(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def abort(self):
        with self._changed:
            self.closed.set()
            self._queue.clear()
            self._sentences.clear()
            self._changed.notify_all()

    def received_frame(self, now):
        with self._changed:
            self._live_until = now + wire.LIVENESS_S

    def _control(self, kind, payload):
        if self._controls >= wire.MAX_QUEUED_EVENTS:
            self.abort()
            return False
        self._queue.append((kind, payload, 0.0, None))
        self._controls += 1
        self._changed.notify()
        return True

    def pong(self, payload):
        with self._changed:
            if not self.closed.is_set():
                self._control(wire.PONG, payload)

    def event(self, kind, fields):
        with self._changed:
            if self.closed.is_set():
                return False
            if kind == "say":
                ident = fields["id"]
                if ident in self._sentences or len(self._sentences) >= wire.MAX_QUEUED_EVENTS:
                    self.abort()
                    return False
                self._sentences[ident] = _Sentence(self._next_index)
                fields["n"] = self._next_index
                self._next_index += 1
            elif kind == "flush":
                self._sentences.clear()
                kept = deque()
                for frame in self._queue:
                    if frame[0] == wire.AUDIO_OUT:
                        self._audio_s -= frame[2]
                        self._chunks -= 1
                    else:
                        kept.append(frame)
                self._queue = kept
                self._audio_s = max(0.0, self._audio_s)
            return self._control(wire.EVENT, wire.encode_event(kind, **fields))

    def on_event(self, kind, **fields):
        if kind == "ignored":
            return  # The session reports ignored speech in its status counters.
        try:
            self.event(kind, fields)
        except Exception:
            LOG.warning("Voice link could not encode a session event.")
            if kind != "asking":
                self.abort()

    def played(self, ident, received_at):
        with self._changed:
            sentence = self._sentences.get(ident)
            if sentence is None or sentence.last_sent_at is None or received_at < sentence.last_sent_at:
                return False
            del self._sentences[ident]
            return True

    def on_audio(self, ident, pcm, rate, first, last=None):
        try:
            self.audio(ident, pcm, as_index(rate), first, last)
        except Exception:
            LOG.warning("Voice link could not encode session audio.")
            self.abort()

    def audio(self, ident, pcm, rate, first, last):
        # NumPy is not needed to import the daemon or start an idle listener.
        import numpy as np

        with self._changed:
            if self.closed.is_set() or ident not in self._sentences:
                return
            pcm = np.asarray(pcm)
            if pcm.ndim != 1 or pcm.dtype.type is not np.int16:
                raise wire.ProtocolError("Invalid session PCM.")
            size = wire.audio_chunk_samples(rate)
            samples = len(pcm)
            if last is None:
                # Four-argument callers may hand off full 200 ms fragments
                # or a whole sentence. Explicit flags also cover exact-size endings.
                last = samples != rate * wire.CHUNK_MS // 1000
            count = (samples + size - 1) // size
            if (self._audio_s + samples / rate > wire.MAX_QUEUED_AUDIO_S + 1e-12
                    or self._chunks + count > _MAX_CHUNKS):
                self.abort()
                return
            sentence = self._sentences[ident]
            for offset in range(0, samples, size):
                chunk = pcm[offset:offset + size].astype("<i2", copy=False)
                flags = (wire.FIRST if first and offset == 0 else 0)
                if last and offset + len(chunk) == samples:
                    flags |= wire.LAST
                payload = wire.encode_payload(wire.AUDIO_OUT, (sentence.index, flags, rate, chunk.tobytes()))
                duration = len(chunk) / rate
                self._queue.append((wire.AUDIO_OUT, payload, duration, sentence if flags & wire.LAST else None))
                self._audio_s += duration
                self._chunks += 1
            self._changed.notify()

    def send(self, data, deadline=None):
        poller = select.poll()
        poller.register(self.sock, select.POLLOUT)
        remaining = memoryview(data)
        while remaining:
            if self.closed.is_set():
                raise EOFError()
            if deadline is not None:
                _wait(poller, deadline)
            else:
                remaining_ms = math.ceil((self._live_until - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError()
                if not poller.poll(min(100, remaining_ms)):
                    continue  # Observe close/overflow even if the socket is full.
            try:
                # Sample before the nonblocking write so even an immediate reply
                # is later. Publish the time only when the whole frame is sent.
                with self._changed:
                    at = time.monotonic() if self._writing_last is not None else None
                    sent = self.sock.send(remaining)
                    if at is not None and sent == len(remaining):
                        self._writing_last.last_sent_at = at
            except BlockingIOError:
                continue
            if sent == 0:
                raise EOFError()
            remaining = remaining[sent:]

    def write(self, ks):
        frames = wire.FrameWriter(ks, wire.DOWN)
        try:
            while True:
                with self._changed:
                    while not self.closed.is_set() and not self._queue:
                        remaining = self._live_until - time.monotonic()
                        if remaining <= 0:
                            return
                        self._changed.wait(remaining)
                    if self.closed.is_set():
                        return
                    kind, payload, duration, self._writing_last = self._queue.popleft()
                self.send(frames.encode(kind, payload))
                with self._changed:
                    if duration:
                        self._audio_s = max(0.0, self._audio_s - duration)
                        self._chunks -= 1
                    else:
                        self._controls -= 1
        except (OSError, EOFError, wire.ProtocolError):
            pass
        finally:
            self.abort()
            self.interrupt()


class VoiceLink:
    """Serialize access to the voice session across authenticated helpers.

    The factory receives callbacks bound to each peer and may return a reusable
    session with a detach method. A session supplied directly can use the public
    callbacks and must stop emitting before its replacement helper starts.
    """

    def __init__(self, session_factory, *, port=None, key_path=None):
        self._factory = session_factory
        self._port = config.VOICE_LINK_PORT if port is None else port
        self._key_path = Path(config.voice_link_key() if key_path is None else key_path)
        self._lifecycle = threading.Lock()
        self._dispatch = threading.Lock()
        self._lock = threading.Lock()
        self._server = self._acceptor = self._active = self._session = None
        self._peers = set()
        self._workers = set()
        self._pending = 0
        self._reason = "not_started"
        self._clicks = 0
        self._helper_process = None
        self.address = None

    @property
    def port(self):
        address = self.address
        return self._port if address is None else address[1]

    @property
    def clicks(self):
        with self._lock:
            return self._clicks

    def status(self):
        with self._lock:
            peer = self._active
            connected = peer is not None and peer.version is not None and not peer.closed.is_set()
            status = {"listening": self._server is not None, "connected": connected}
            if self._reason:
                status["reason"] = self._reason
            if connected:
                status["helper_version"] = peer.version
            if connected and peer.level is not None:
                status["level"] = peer.level
            return status

    def launch_helper(self):
        from . import installation

        with self._lifecycle:
            status = self.status()
            if not status["listening"]:
                raise installation.HelperUnavailable("The voice link is unavailable.")
            if status["connected"] or (self._helper_process is not None
                                       and self._helper_process.poll() is None):
                return False
            try:
                self._helper_process = installation.launch(self.port)
            except (OSError, subprocess.SubprocessError):
                raise installation.HelperUnavailable("Could not start the Windows voice helper.") from None
            return True

    def request_quit(self):
        with self._lock:
            peer = self._active
            if peer is None or peer.version is None:
                return False
        # The helper recognizes this notice only after authenticating the peer.
        return peer.event("notice", {"title": "", "text": "", "helper_action": "quit"})

    def _unavailable(self, reason):
        with self._lock:
            self._reason = reason
        return False

    def start(self):
        with self._lifecycle:
            if self._server is not None:
                return True
            try:
                key = _load_key(self._key_path)
            except FileNotFoundError:
                return self._unavailable("key_missing")
            except (OSError, wire.ProtocolError):
                return self._unavailable("key_invalid")
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server.bind(("127.0.0.1", self._port))
                    server.listen(_BACKLOG)
                    server.settimeout(0.25)
                except BaseException:
                    server.close()
                    raise
            except OSError:
                LOG.warning("Voice link could not bind its port.")
                return self._unavailable("bind_failed")
            acceptor = threading.Thread(target=self._accept, args=(server, key), name="jarvis-voice-link",
                                        daemon=True)
            with self._lock:
                self._server, self._acceptor = server, acceptor
                self.address, self._reason = server.getsockname(), None
                try:
                    acceptor.start()
                except RuntimeError:
                    server.close()
                    self._server = self._session = self._acceptor = self.address = None
                    self._reason = "start_failed"
                    LOG.warning("Voice link listener could not start.")
                    return False
            return True

    def close(self):
        with self._lifecycle:
            with self._lock:
                server, self._server = self._server, None
                peer_list = tuple(self._peers)
                self.address, self._reason = None, "stopped"
            if server is None:
                return
            server.close()
            for peer in peer_list:
                peer.abort()
                peer.interrupt()
            self._acceptor.join()
            for worker in self._workers:
                worker.join()
            self._workers.clear()
            self._acceptor = self._session = None

    def _accept(self, server, key):
        while True:
            try:
                sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                if self._server is not server or self._pending >= wire.MAX_UNAUTHENTICATED:
                    sock.close()
                    continue
                peer = _Peer(sock)
                worker = threading.Thread(target=self._serve, args=(peer, key), name="jarvis-voice-peer",
                                          daemon=True)
                self._workers = {thread for thread in self._workers if thread.is_alive()}
                self._peers.add(peer)
                self._pending += 1
                try:
                    worker.start()
                except RuntimeError:
                    self._peers.remove(peer)
                    self._pending -= 1
                    sock.close()
                    LOG.warning("Voice link connection worker could not start.")
                else:
                    self._workers.add(worker)

    def _handshake(self, peer, key, reader):
        # The timer interrupts the socket even with a peer trickling bytes.
        timer = threading.Timer(max(0, peer.deadline - time.monotonic()), peer.interrupt)
        timer.name = "jarvis-voice-handshake"
        timer.daemon = True
        timer.start()
        try:
            a = wire.check_helper_hello(wire.read_json(reader))
            d = wire.new_nonce()
            peer.send(wire.json_line(wire.daemon_hello(key, a, d)), peer.deadline)
            wire.check_helper_proof(key, a, d, wire.read_json(reader))
            if time.monotonic() >= peer.deadline:
                raise TimeoutError()
            return wire.derive_ks(key, a, d)
        finally:
            timer.cancel()
            timer.join()

    def _deactivate(self, peer):
        # Caller holds _dispatch. Callbacks may run during stop, so unpublish
        # the peer before calling into the session and keep _lock available.
        with self._lock:
            if self._active is not peer:
                return
            self._active = None
            session, self._session = self._session, None
        peer.abort()
        peer.interrupt()
        if session is None:
            return
        try:
            session.stop()
        except Exception:
            LOG.warning("Voice link could not stop its session.")
        try:
            detach = getattr(session, "detach", None)
            if detach is not None:
                detach()
        except Exception:
            LOG.warning("Voice link could not detach its session.")

    def _activate(self, peer):
        with self._dispatch:
            with self._lock:
                if self._server is None or peer.closed.is_set():
                    return False
                old = self._active
            if old is not None:
                self._deactivate(old)
            with self._lock:
                if self._server is None or peer.closed.is_set():
                    return False
                self._active = peer
                self._pending -= 1
                peer.pending = False
            session = self._factory
            if callable(session) and (isinstance(session, type) or getattr_static(session, "feed", None) is None):
                session = session(on_event=peer.on_event, on_audio=peer.on_audio)
            self._session = session
            if session is None:
                return False
            session.start()
            return True

    def _serve(self, peer, key):
        writer = None
        try:
            peer.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer.sock.setblocking(False)
            reader = _Reader(peer.sock, peer.deadline)
            ks = self._handshake(peer, key, reader)
            now = time.monotonic()
            peer.received_frame(now)
            reader.deadline = now + wire.LIVENESS_S
            worker = threading.Thread(target=peer.write, args=(ks,), name="jarvis-voice-writer", daemon=True)
            worker.start()
            writer = worker
            if not self._activate(peer):
                return
            frames = wire.FrameReader(ks, wire.UP)
            while not peer.closed.is_set():
                kind, value = frames.read(reader)
                now = time.monotonic()
                if now >= reader.deadline:
                    raise TimeoutError()
                reader.deadline = now + wire.LIVENESS_S
                peer.received_frame(now)
                with self._dispatch:
                    with self._lock:
                        if self._server is None or peer.closed.is_set() or self._active is not peer:
                            return
                    self._deliver(peer, kind, value, now)
        except (EOFError, OSError, wire.ProtocolError):
            pass
        except Exception:
            LOG.warning("Voice link connection stopped after an internal error.")
        finally:
            peer.abort()
            peer.interrupt()
            with self._dispatch:
                self._deactivate(peer)
            if writer is not None:
                writer.join()
            peer.sock.close()
            with self._lock:
                self._peers.remove(peer)
                self._pending -= peer.pending

    def _deliver(self, peer, kind, value, now):
        if (kind == wire.HELLO) != (peer.version is None):
            raise wire.ProtocolError("Voice link HELLO is out of order.")
        if kind == wire.HELLO:
            with self._lock:
                peer.version = value["version"]
        elif kind == wire.AUDIO:
            import numpy as np

            raw, cancelled = value
            self._session.feed(np.frombuffer(raw, dtype="<i2"),
                               None if cancelled is None else np.frombuffer(cancelled, dtype="<i2"))
        elif kind == wire.KEY:
            self._session.key(value)
        elif kind == wire.PLAYED:
            if peer.played(value["id"], now):
                self._session.played(value["id"])
        elif kind == wire.FLUSHED:
            self._session.playback_stopped(value)
        elif kind == wire.CLICK:
            with self._lock:
                self._clicks += 1
            click = getattr(self._session, "click", None)
            if click is not None:
                click(value["id"], value["digest"], value["decision"])
        elif kind == wire.PING:
            peer.pong(value)
        elif kind == wire.LEVEL:
            while peer.level_times and now - peer.level_times[0] >= 1:
                peer.level_times.popleft()
            if len(peer.level_times) >= wire.LEVEL_PER_SECOND:
                raise wire.ProtocolError("Voice link LEVEL is too frequent.")
            peer.level_times.append(now)
            with self._lock:
                peer.level = value

    def on_event(self, kind, **fields):
        with self._lock:
            peer = self._active if self._server is not None else None
        if peer is not None:
            peer.on_event(kind, **fields)

    def on_audio(self, id, pcm_int16, sample_rate, first, last=None):
        """Queue PCM with the sentence boundary supplied by the sender."""
        with self._lock:
            peer = self._active if self._server is not None else None
        if peer is not None:
            peer.on_audio(id, pcm_int16, sample_rate, first, last)
