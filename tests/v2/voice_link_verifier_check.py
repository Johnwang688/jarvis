"""Independent VA-1 counterexamples and adversarial probes.

Only fake keys/audio/transcribers, temporary HOME, ephemeral loopback ports.
Run with the shared venv, PYTHONPATH=., and the checkout's systemd memory cap.
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jarvis import config, runtime
from jarvis.tools import dispatch, files, search, secrets, shell
from jarvis.v2 import permissions, remote_stt, voice_link
from windows import voicewire as wire

KEY = bytes(range(32))
CLIP = wire.wav_bytes(b"\x01\x00" * 1600)


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        threading.Event().wait(0.005)


def tool_result(name, arguments):
    with runtime.tools_bound({"read_file", "grep_files", "run_readonly"}):
        return dispatch(name, json.dumps(arguments)).text


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="va1-verifier-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        guard = patch.dict(os.environ, {"HOME": str(self.home), "JARVIS_WINDOWS_USERS": str(self.root / "Users")})
        guard.start()
        self.addCleanup(guard.stop)
        for name, value in {"V2_DATA_DIR": self.root / "data", "VOICE_LINK_PORT": 0,
                            "VOICE_LINK_KEY": config._VOICE_LINK_KEY_DEFAULT,
                            "BROWSER_DIR": self.root / "browser"}.items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.key = config.voice_link_key()
        self.key.parent.mkdir(parents=True, mode=0o700)
        self.key.write_text(KEY.hex() + "\n")
        self.key.chmod(0o600)

    def link(self, key=None, transcribe=None):
        link = voice_link.VoiceLink(None, port=0, key_path=key or self.key,
                                    transcribe=transcribe or (lambda _: {"text": "fake", "backend": "local"}))
        self.addCleanup(link.stop)
        return link


class PairingProbes(TempCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.server = self.link(transcribe=lambda data: self.calls.append(data) or {"text": "fake", "backend": "local"})
        self.assertTrue(self.server.start())
        self.assertNotIn(self.server.port, range(8402, 8410))

    def raw(self):
        sock = socket.create_connection(self.server.address, timeout=2)
        self.addCleanup(sock.close)
        stream = sock.makefile("rwb")
        self.addCleanup(stream.close)
        return sock, stream

    def hello(self, nonce=None):
        sock, stream = self.raw()
        a = wire.new_nonce() if nonce is None else nonce
        wire.send_json(stream, {"hello": "jarvis-voice-agent", "v": 1, "nonce_a": a})
        return sock, stream, a, wire.read_json(stream)

    def closed(self, stream):
        try:
            self.assertEqual(stream.readline(), b"")
        except (ConnectionResetError, BrokenPipeError):
            pass

    def client(self):
        client = wire.Client(*self.server.address, KEY, timeout=2)
        self.addCleanup(client.close)
        client.connect()
        client.ping()  # observe completion of the server's authentication
        return client

    def request_bytes(self):
        return (json.dumps({"op": "dictate", "id": 1, "wav_b64": base64.b64encode(CLIP).decode()}) + "\n").encode()

    def test_reflection_replay_and_nonce_reuse_do_not_authorize(self):
        _, first, a, got = self.hello()
        recorded = wire.agent_proof(KEY, got["nonce_d"], a)
        wire.send_json(first, {"proof": recorded})
        wire.send_json(first, {"op": "ping", "id": 1})
        self.assertTrue(wire.read_json(first)["ok"])
        for kind in ("replay", "reflection", "wrong-key"):
            with self.subTest(kind=kind):
                _, stream, same_a, challenge = self.hello(a)
                self.assertNotEqual(challenge["nonce_d"], got["nonce_d"])
                proof = {"replay": recorded, "reflection": challenge["proof"],
                         "wrong-key": wire.agent_proof(b"x" * 32, challenge["nonce_d"], same_a)}[kind]
                stream.write((json.dumps({"proof": proof}) + "\n").encode() + self.request_bytes())
                stream.flush()
                self.closed(stream)
                eventually(lambda: self.server._pending == 0)
        self.assertEqual(self.calls, [])

    def test_uppercase_nonce_authenticates_only_with_the_bound_proof(self):
        _, stream, a, challenge = self.hello("AB" * 32)
        wire.send_json(stream, {"proof": wire.agent_proof(KEY, challenge["nonce_d"], a)})
        wire.send_json(stream, {"op": "ping", "id": 8})
        self.assertEqual(wire.read_json(stream), {"id": 8, "ok": True})
        _, stream, a, challenge = self.hello("AB" * 32)
        stream.write((json.dumps({"proof": wire.agent_proof(KEY, challenge["nonce_d"], a.lower())}) + "\n").encode() + self.request_bytes())
        stream.flush()
        self.closed(stream)
        self.assertEqual(self.calls, [])

    def test_bad_hello_and_proof_shapes_release_slots(self):
        for nonce in (None, 1, [], {}, "g" * 64, "a" * 63, "a" * 65, "é" * 64, "a" * 5000):
            with self.subTest(nonce=repr(nonce)[:35]):
                _, stream = self.raw()
                wire.send_json(stream, {"hello": "jarvis-voice-agent", "v": 1, "nonce_a": nonce})
                self.closed(stream)
                eventually(lambda: self.server._pending == 0)
        for proof in (None, 1, [], {}, "g" * 64, "0" * 63, "0" * 65, "é" * 64, "0" * 5000):
            with self.subTest(proof=repr(proof)[:35]):
                _, stream, _, _ = self.hello()
                stream.write((json.dumps({"proof": proof}) + "\n").encode() + self.request_bytes())
                stream.flush()
                self.closed(stream)
                eventually(lambda: self.server._pending == 0)
        self.assertEqual(self.calls, [])
        self.client().ping()

    def test_partial_binary_oversize_and_pipelined_hellos_never_transcribe(self):
        for data in (b"\xff\n", b"[]\n", b"null\n", b"{" + b"x" * 4096,
                     self.request_bytes(), b'{"hello":"jarvis-voice-agent","v":true,"nonce_a":"' + b"a" * 64 + b'"}\n'):
            with self.subTest(data=data[:50]):
                sock, stream = self.raw()
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)
                self.closed(stream)
                eventually(lambda: self.server._pending == 0)
        sock, stream, _, _ = self.hello()
        sock.sendall(self.request_bytes() * 2)  # requests where the proof must be
        self.closed(stream)
        eventually(lambda: self.server._pending == 0)
        self.assertEqual(self.calls, [])

    def test_wall_clock_deadline_and_cap_recover_after_trickled_proof(self):
        self.assertEqual(voice_link.HANDSHAKE_TIMEOUT_S, 5.0)
        with patch.object(voice_link, "HANDSHAKE_TIMEOUT_S", 0.25):
            connections = [self.hello() for _ in range(voice_link.MAX_UNAUTHENTICATED)]
            eventually(lambda: self.server._pending == voice_link.MAX_UNAUTHENTICATED)
            _, extra = self.raw()
            self.closed(extra)
            start = time.monotonic()
            sock, stream, _, _ = connections[0]
            sock.sendall(b'{"proof":"')
            threading.Event().wait(0.1)
            sock.sendall(b"a")
            self.closed(stream)
            self.assertLess(time.monotonic() - start, 0.6)
            eventually(lambda: self.server._pending == 0)
        self.client().ping()

    def test_replacement_and_stop_suppress_inflight_result_and_release_threads(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def transcribe(_):
            entered.set()
            release.wait(3)
            return {"text": "private inflight result", "backend": "local"}
        self.server.transcribe = transcribe
        old = self.client()
        old._stream.write(self.request_bytes())
        old._stream.flush()
        self.assertTrue(entered.wait(2))
        new = self.client()
        self.closed(old._stream)
        _, pending = self.raw()
        eventually(lambda: self.server._pending == 1)
        threads = [t for t in threading.enumerate() if t.name in ("jarvis-voice-link", "jarvis-voice-agent")]
        self.server.stop()
        self.closed(new._stream)
        self.closed(pending)
        release.set()
        eventually(lambda: not self.server._connections and self.server._pending == 0)
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive(), thread.name)

    def test_request_line_cap_precedes_json_parsing(self):
        client = self.client()
        with patch.object(wire.json, "loads", wraps=json.loads) as loads:
            client._stream.write(b" " * (wire.MAX_LINE + 1))
            client._stream.flush()
            line = client._stream.readline()
            self.assertIn(b'"ok":false', line)
            self.closed(client._stream)
            loads.assert_not_called()
        self.assertEqual(self.calls, [])


class KeyFileProbes(TempCase):
    def test_lstat_open_races_reject_symlink_fifo_and_new_permissions(self):
        real_open = os.open
        for replacement in ("symlink", "fifo", "permissions"):
            with self.subTest(replacement=replacement):
                self.key.unlink(missing_ok=True)
                self.key.write_text(KEY.hex() + "\n")
                self.key.chmod(0o600)
                switched = False
                def race(path, flags, *args, **kwargs):
                    nonlocal switched
                    if Path(path) == self.key and not switched:
                        switched = True
                        self.key.unlink()
                        if replacement == "symlink":
                            other = self.root / "other.key"
                            other.write_text(KEY.hex() + "\n")
                            other.chmod(0o600)
                            self.key.symlink_to(other)
                        elif replacement == "fifo":
                            os.mkfifo(self.key, 0o600)
                        else:
                            self.key.write_text(KEY.hex() + "\n")
                            self.key.chmod(0o640)
                    return real_open(path, flags, *args, **kwargs)
                link = self.link()
                with patch.object(voice_link.os, "open", race):
                    self.assertFalse(link.start())
                self.assertIsNone(link.address)
                self.assertTrue(switched)

    def test_all_group_other_bits_and_trailing_junk_are_refused(self):
        for bit in (0o40, 0o20, 0o10, 0o4, 0o2, 0o1):
            self.key.chmod(0o600 | bit)
            self.assertFalse(self.link().start(), oct(bit))
        self.key.chmod(0o600)
        for junk in (b"\nmore", b"\x00", b"\r\n", b"\n\n", b" " * 10000):
            self.key.write_bytes(KEY.hex().encode() + junk)
            with self.assertRaises(wire.KeyFileError):
                wire.load_key(self.key)
            self.assertFalse(self.link().start())

    def test_setup_refuses_symlinked_pairing_folder(self):
        from jarvis.voice_link_setup import write_keys
        alias = self.root / "alias"
        alias.symlink_to(self.key.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            write_keys(alias / "voice-link.key", self.root / "win" / "voice-link.key")
        self.assertEqual(wire.load_key(self.key), KEY)

    def test_windows_copy_never_requires_unix_chmod(self):
        from jarvis import voice_link_setup
        win = self.root / "windows-bridge" / "voice-link.key"
        real_fchmod = os.fchmod
        rejected = []
        def drvfs_fchmod(fd, mode):
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if path.parent == win.parent:
                rejected.append(path.name)
                raise PermissionError("simulated /mnt/c without Unix chmod support")
            return real_fchmod(fd, mode)
        error = None
        with patch.object(voice_link_setup.os, "fchmod", drvfs_fchmod):
            try:
                voice_link_setup.write_keys(self.key, win)
            except OSError as exc:
                error = str(exc)
        self.assertIsNone(error, "write_keys requires chmod on the Windows copy: " + str(error))
        self.assertEqual(rejected, [])
        self.assertEqual(wire.load_key(self.key), wire.load_key(win))
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)

    def test_staging_failure_preserves_existing_keys_and_removes_temporary_files(self):
        from jarvis import voice_link_setup
        win = self.root / "windows-bridge" / "voice-link.key"
        win.parent.mkdir()
        win.write_text(KEY.hex() + "\n")
        real_stage = voice_link_setup._stage
        def fail_second(path, data):
            if path == win:
                raise OSError("simulated staging failure")
            return real_stage(path, data)
        with patch.object(voice_link_setup, "_stage", fail_second):
            with self.assertRaises(OSError):
                voice_link_setup.write_keys(self.key, win)
        self.assertEqual(wire.load_key(self.key), KEY)
        self.assertEqual(wire.load_key(win), KEY)
        self.assertEqual(list(self.root.rglob(".env.voice-link-*")), [])

    def test_each_key_replacement_is_atomic_and_wsl_modes_are_private(self):
        from jarvis import voice_link_setup
        win = self.root / "windows-bridge" / "voice-link.key"
        win.parent.mkdir()
        win.write_text(KEY.hex() + "\n")
        real_replace = os.replace
        observations = []
        def observe(source, dest):
            # The old copy remains complete until replace, and the staged
            # copy is complete and private before it takes the final name.
            self.assertEqual(wire.load_key(dest), KEY)
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            observations.append(wire.load_key(source))
            real_replace(source, dest)
        with patch.object(voice_link_setup.os, "replace", observe):
            voice_link_setup.write_keys(self.key, win)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0], observations[1])
        self.assertNotEqual(observations[0], KEY)
        self.assertEqual(wire.load_key(self.key), wire.load_key(win))
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.root.rglob(".env.voice-link-*")), [])


class CredentialProbes(TempCase):
    def test_folder_and_exact_names_refused_by_all_reader_families(self):
        ordinary = self.key.parent / "innocent.txt"
        ordinary.write_text("sentinel-private-pairing-folder")
        copied = self.root / "bridge" / "voice-link.key"
        copied.parent.mkdir()
        copied.write_text(KEY.hex() + "\n")
        alias = self.root / "alias.txt"
        alias.symlink_to(self.key)
        for path in (self.key, ordinary, copied, alias):
            with self.subTest(path=path.name):
                self.assertTrue(files.read_file(str(path)).startswith("Error:"))
                self.assertTrue(shell.run_readonly("cat " + shlex.quote(str(path))).startswith("Error:"))
                for mode in ("content", "count", "files"):
                    result = search.grep_files(".", str(path), mode=mode)
                    self.assertNotIn(KEY.hex(), result)
                    self.assertNotIn("sentinel-private-pairing-folder", result)
                self.assertIsNotNone(permissions.denied_read("Read", {"file_path": str(path)}, str(self.root)))
        for tool, args in (("Read", {"file_path": str(ordinary)}),
                           ("Grep", {"path": str(self.key.parent), "pattern": "."}),
                           ("Grep", {"path": str(config.V2_DATA_DIR), "pattern": "."}),
                           ("read_file", {"path": str(self.key)})):
            self.assertIsNotNone(permissions._credential_folder_read(tool, args, str(self.root)), tool)
        self.assertNotIn("sentinel-private-pairing-folder", search.grep_files("sentinel", str(config.V2_DATA_DIR)))

    def test_key_location_and_folder_guard_follow_data_root_changes(self):
        for root in (self.root / "one", self.root / "two"):
            with patch.object(config, "V2_DATA_DIR", root):
                expected = root / "voice-link" / "voice-link.key"
                self.assertEqual(config.voice_link_key(), expected)
                self.assertIn(str(expected.parent), config.credential_dirs())
                self.assertTrue(files.credential_folder_judge()(expected.parent / "ordinary.txt"))


class DaemonProbes(unittest.TestCase):
    def test_unsearchable_key_parent_does_not_crash_daemon_start(self):
        # Reuse only the existing hermetic temporary-HOME/ephemeral-port harness.
        spec = importlib.util.spec_from_file_location("va1_verifier_listener", ROOT / "tests/v2/remote_listener_check.py")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        base = harness.Base()
        base.setUp()
        self.addCleanup(base.doCleanups)
        key = config.voice_link_key()
        key.parent.mkdir(parents=True, mode=0o700)
        key.write_text(KEY.hex() + "\n")
        key.chmod(0o600)
        daemon = harness.Daemon(harness.Stores(), {name: harness.Fake(name) for name in harness.PN},
                                lambda *_: lambda *_: harness.Decision.DENY, 0, remote_host=None)
        self.addCleanup(daemon.stop)
        # Daemon.start has already bound these servers when its exists() raises;
        # track them so this failing counterexample itself leaks no sockets.
        real_http = __import__("jarvis.v2.daemon", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer
        opened = []
        def http(*args, **kwargs):
            result = real_http(*args, **kwargs)
            opened.append(result)
            self.addCleanup(result.server_close)
            return result
        key.parent.chmod(0)
        error = None
        try:
            with patch("jarvis.v2.daemon.ThreadingHTTPServer", http), patch.object(config, "VOICE_LINK_PORT", 0):
                try:
                    daemon.start()
                except OSError as exc:
                    error = type(exc).__name__
        finally:
            key.parent.chmod(0o700)
        self.assertIsNone(error, "Daemon.start crashed on an unusable pairing key: " + str(error))
        self.assertIsNotNone(daemon._server_thread)
        self.assertFalse({server.server_address[1] for server in opened} & set(range(8402, 8410)))


class PhoneCompatibilityProbes(unittest.TestCase):
    def test_phone_check_wav_matches_main_status_and_exact_error_bytes(self):
        old = subprocess.run(["git", "show", "main:jarvis/v2/remote_stt.py"], cwd=ROOT,
                             capture_output=True, check=True, text=True).stdout
        tree = ast.parse(old)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check_wav")
        namespace = dict(vars(remote_stt))
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main:remote_stt.py", "exec"), namespace)
        baseline = namespace["check_wav"]
        long_header = bytearray(wire.wav_bytes(b"\x01\x00"))
        struct.pack_into("<I", long_header, 40, 16000 * 61 * 2)
        clips = [b"", b"x", b"RIFF not a wave", CLIP, bytes(long_header),
                 wire.wav_bytes(b""), wire.wav_bytes(b"\x00\x00" * (16000 * 60)),
                 wire.wav_bytes(b"\x00\x00" * (16000 * 60 + 1)),
                 b"x" * remote_stt.BODY_CAP, b"x" * (remote_stt.BODY_CAP + 1)]
        clips += [CLIP[:n] for n in range(45)]
        clips += [CLIP + b"x" * (remote_stt.BODY_CAP - len(CLIP) + n) for n in (0, 1)]
        def outcome(fn, data):
            try:
                fn(data)
                return None
            except remote_stt._Refused as exc:
                return exc.status, exc.word.encode("utf-8")
        for index, data in enumerate(clips):
            with self.subTest(index=index, size=len(data)):
                self.assertEqual(outcome(remote_stt.check_wav, data), outcome(baseline, data))
        self.assertEqual(remote_stt.MAX_S, 60)
        self.assertEqual(remote_stt.BODY_CAP, 1_924_140)


class ClientAndHotkeyProbes(TempCase):
    def test_notjarvis_closes_before_proof_and_subsequent_dictate(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        observed, errors = [], []
        def impostor():
            try:
                with server.accept()[0] as conn, conn.makefile("rwb") as stream:
                    wire.read_json(stream)
                    wire.send_json(stream, {"hello": "jarvis-voice-link", "v": 1,
                                            "nonce_d": "a" * 64, "proof": "0" * 64})
                    observed.append(stream.read())
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=impostor, daemon=True)
        thread.start()
        client = wire.Client(*server.getsockname(), KEY, timeout=1)
        self.addCleanup(client.close)
        with patch.object(wire, "agent_proof", wraps=wire.agent_proof) as proof:
            with self.assertRaises(wire.NotJarvis):
                client.connect()
            with self.assertRaises(wire.LinkError):
                client.dictate(CLIP)
            proof.assert_not_called()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [b""])

    def test_hotkey_exact_tap_boundary_toggle_chords_and_release_latches(self):
        for release, expected in ((299, []), (300, ["stop"]), (301, ["stop"])):
            machine = wire.HotkeyMachine()
            self.assertEqual(machine.feed(0, True, False), ["start"])
            self.assertEqual(machine.feed(release, False, False), expected)
        machine = wire.HotkeyMachine(max_ms=1000)
        self.assertEqual(machine.feed(0, True, True), [])
        self.assertEqual(machine.feed(1, True, False), [])
        self.assertEqual(machine.feed(2, False, False), [])
        self.assertEqual(machine.feed(3, True, False), ["start"])
        self.assertEqual(machine.feed(4, False, False), [])
        self.assertEqual(machine.feed(100, False, True), [])
        self.assertEqual(machine.feed(101, True, True), ["stop"])
        self.assertEqual(machine.feed(102, True, False), [])
        self.assertEqual(machine.feed(103, False, False), [])
        self.assertEqual(machine.feed(104, True, False), ["start"])
        self.assertEqual(machine.feed(1104, True, False), ["stop"])
        self.assertEqual(machine.feed(1105, True, False), [])
        self.assertEqual(machine.feed(1106, False, False), [])


class WindowsGlueProbes(TempCase):
    def agent_module(self):
        spec = importlib.util.spec_from_file_location("va1_verifier_voice_agent", ROOT / "windows/voice_agent.py")
        module = importlib.util.module_from_spec(spec)
        # Neither Tk nor Windows nor an audio device is instantiated.
        with patch.dict(sys.modules, {"voicewire": wire, "sounddevice": SimpleNamespace(), "tkinter": SimpleNamespace()}):
            spec.loader.exec_module(module)
        return module

    def bare_agent(self, module):
        agent = module.Agent.__new__(module.Agent)
        agent.done = threading.Event()
        agent.events = queue.Queue()
        agent.clips = queue.Queue(maxsize=4)
        agent.generation = 1
        agent.capture_lock = threading.Lock()
        agent.capture = None
        agent.chunks = []
        agent.frames = 0
        agent.audio_bad = False
        return agent

    def test_capture_cap_cancel_and_transcript_never_print_or_persist(self):
        module = self.agent_module()
        agent = self.bare_agent(module)
        class Samples:
            def __getitem__(self, part):
                return SimpleNamespace(tobytes=lambda: b"\x01\x00" * part.stop)
        agent.audio(Samples(), module.MAX_FRAMES + 1000, None, False)
        self.assertEqual(agent.frames, module.MAX_FRAMES)
        self.assertEqual(sum(map(len, agent.chunks)), module.MAX_FRAMES * 2)
        agent.audio(Samples(), 1000, None, False)
        self.assertEqual(agent.frames, module.MAX_FRAMES)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", output):
            agent.finish_capture(cancel=True)
            self.assertEqual(agent.chunks, [])
            self.assertTrue(agent.clips.empty())
            agent.chunks = [b"\x01\x00" * 1600]
            agent.finish_capture()
            self.assertEqual(agent.clips.get_nowait(), (1, CLIP))
            agent.host, agent.port, agent.key_path = "127.0.0.1", 0, self.key
            agent.clips.put((2, CLIP))
            class FakeClient:
                def __init__(self, *args, **kwargs):
                    pass
                def connect(self):
                    pass
                streaming = False
                def ping(self):
                    pass
                def dictate(self, data):
                    agent.done.set()
                    return {"text": "private fake transcript", "backend": "local"}
                def close(self):
                    pass
            with patch.object(module, "Client", FakeClient):
                agent.network()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertIn((2, "private fake transcript", True), list(agent.events.queue))

    def test_network_notjarvis_discards_queued_audio_without_dictating(self):
        module = self.agent_module()
        agent = self.bare_agent(module)
        agent.host, agent.port, agent.key_path = "127.0.0.1", 0, self.key
        agent.clips.put((1, CLIP))
        attempts = []
        class ImpostorClient:
            def __init__(self, *args, **kwargs):
                pass
            def connect(self):
                raise wire.NotJarvis("That wasn't Jarvis — refused")
            def dictate(self, clip):
                attempts.append(clip)
            def close(self):
                pass
        with patch.object(module, "Client", ImpostorClient), patch.object(agent.done, "wait", side_effect=lambda _: agent.done.set()):
            agent.network()
        self.assertEqual(attempts, [])
        self.assertTrue(agent.clips.empty())
        self.assertEqual(list(agent.events.queue), [(1, "That wasn't Jarvis — refused", True)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
