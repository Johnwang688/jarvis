"""BR10 sound helper: synthetic process/Pulse data; no Chrome or real mixer IO."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import audio as A, settings as S


class Launch(unittest.TestCase):
    def test_off_preserves_arguments_and_strips_ambient_server(self):
        args = ["--sandbox", "--mute-audio", "--proxy=fixture"]
        env = {"HOME": "/private/home", "FONTCONFIG_FILE": "/private/fonts", "PULSE_SERVER": "attacker"}
        got = A.launch_audio(args, env, False, A.Availability(True, True))
        self.assertEqual(got.args, args)
        self.assertEqual(got.env, {"HOME": "/private/home", "FONTCONFIG_FILE": "/private/fonts"})
        self.assertFalse(got.applied); self.assertIsNone(got.reason)
        self.assertEqual(env["PULSE_SERVER"], "attacker")

    def test_on_removes_only_mute_and_keeps_font_environment(self):
        args = ["--sandbox", "--mute-audio", "--proxy=fixture"]
        env = {"HOME": "/private/home", "FONTCONFIG_FILE": "/private/fonts", "PULSE_SERVER": "attacker"}
        got = A.launch_audio(args, env, True, A.Availability(True, False))
        self.assertEqual(got.args, ["--sandbox", "--proxy=fixture"])
        self.assertEqual(got.env, {**env, "PULSE_SERVER": A.PULSE_SERVER})
        self.assertTrue(got.applied); self.assertIsNone(got.reason)
        self.assertEqual(args, ["--sandbox", "--mute-audio", "--proxy=fixture"])

    def test_unavailable_is_structurally_off_and_preference_is_not_applied(self):
        got = A.launch_audio(["--sandbox"], {"PULSE_SERVER": "attacker"}, True,
                             A.Availability(False, False))
        self.assertEqual(got.args, ["--sandbox", "--mute-audio"])
        self.assertNotIn("PULSE_SERVER", got.env)
        self.assertFalse(got.applied); self.assertEqual(got.reason, A.UNAVAILABLE)
        for bad in (1, 0, "false", None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                A.launch_audio([], {}, bad)

    def test_exact_socket_and_utility_capabilities_are_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); endpoint = root / "pulse"; exe = root / "pactl"
            with patch.object(A, "PULSE_SOCKET", endpoint), patch.object(A, "PACTL", exe):
                self.assertEqual(A.availability(), A.Availability(False, False))
                endpoint.write_text("not a socket")
                self.assertFalse(A.availability().routing)
                endpoint.unlink()
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.bind(str(endpoint))
                    self.assertEqual(A.availability(), A.Availability(True, False))
                    exe.write_text("fixture"); exe.chmod(0o700)
                    self.assertEqual(A.availability(), A.Availability(True, True))
                link = root / "link"; link.symlink_to(endpoint)
                with patch.object(A, "PULSE_SOCKET", link):
                    self.assertFalse(A.availability().routing)


class Settings(unittest.TestCase):
    def test_default_strict_values_and_unknown_key_survive_save(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            settings = S.BrowserSettings(path)
            self.assertTrue(settings.value("sound")); self.assertFalse(path.exists())
            path.write_text(json.dumps({"future": {"x": True}, "sound": "true", "lan": True}))
            self.assertFalse(S.BrowserSettings(path).value("sound"))
            self.assertTrue(settings.update(sound=True)["sound"])
            self.assertEqual(json.loads(path.read_text()), {"future": {"x": True}, "sound": True, "lan": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for bad in ("true", 1, None, {}, []):
                with self.subTest(value=bad), self.assertRaises(ValueError):
                    settings.update(sound=bad)
            self.assertFalse(settings.update(sound=False)["sound"])


class State(unittest.TestCase):
    def test_off_on_before_late_mute_completion_requires_fresh_instance(self):
        state = A.InstanceAudio(True)
        self.assertTrue(state.synchronized(True))
        attempt = state.begin_mute()
        self.assertFalse(state.synchronized(True))
        self.assertEqual(state.snapshot(), (1, True, 1))
        state.finish_mute(attempt)
        self.assertEqual(state.snapshot(), (1, True, 0))
        self.assertFalse(state.synchronized(True), "ON must not cancel restart after any mute attempt")
        self.assertTrue(A.InstanceAudio(True).synchronized(True))

    def test_old_cleanup_cannot_erase_a_new_pending_mute(self):
        state = A.InstanceAudio(True)
        first = state.begin_mute(); second = state.begin_mute()
        state.finish_mute(first); state.finish_mute(first)
        self.assertEqual(state.snapshot(), (2, True, 1))
        state.finish_mute(second)
        self.assertEqual(state.snapshot(), (2, True, 0))
        self.assertFalse(state.synchronized(True))
        self.assertFalse(state.synchronized(False))
        self.assertTrue(A.InstanceAudio(False).synchronized(False))


class FakeProcesses:
    uid = 1000
    def __init__(self):
        self.rows = {100: A.Process(100, 9, 20, self.uid, A.CHROME),
                     110: A.Process(110, 100, 21, self.uid, A.CHROME),
                     120: A.Process(120, 110, 22, self.uid, A.CHROME),
                     200: A.Process(200, 9, 23, self.uid, A.CHROME)}
        self.profile_ok = True
    def identity(self, pid): return self.rows.get(pid)
    def profile(self, process, folder):
        return self.profile_ok and folder == Path("/fixture/profile") and self.rows.get(100) == process
    def owner(self): return A.OwnedChrome.pin(100, Path("/fixture/profile"), self)


class Ownership(unittest.TestCase):
    def test_descendant_exact_start_ancestry_and_profile(self):
        reader = FakeProcesses(); owner = reader.owner()
        self.assertEqual([p.pid for p in owner.chain(120)], [120, 110, 100])
        self.assertIsNone(owner.chain(200))
        reader.rows[110] = replace(reader.rows[110], uid=2000)
        self.assertIsNone(owner.chain(120))
        reader.rows[110] = replace(reader.rows[110], uid=1000, parent=120)
        self.assertIsNone(owner.chain(120), "cycle")
        reader.rows[100] = replace(reader.rows[100], started=999)
        self.assertFalse(owner.current())

    def test_executable_profile_and_parent_reuse_fail_closed(self):
        for field, value in (("executable", "/foreign"), ("uid", 999), ("parent", 200)):
            reader = FakeProcesses(); owner = reader.owner()
            reader.rows[110] = replace(reader.rows[110], **{field: value})
            with self.subTest(field=field): self.assertIsNone(owner.chain(120))
        reader = FakeProcesses(); reader.profile_ok = False
        with self.assertRaises(A.AudioRefused): reader.owner()

    def test_proc_parser_handles_parentheses_and_rechecks_reused_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); folder = root / "100"; folder.mkdir()
            fields = [b"S", b"9"] + [b"0"] * 17 + [b"44"]
            record = b"100 (name with (parentheses)) " + b" ".join(fields)
            (folder / "stat").write_bytes(record)
            (folder / "exe").symlink_to(A.CHROME)
            (folder / "cmdline").write_bytes(b"chrome\0--user-data-dir=/fixture/profile\0")
            reader = A.Processes(root)
            process = reader.identity(100)
            self.assertEqual(process, A.Process(100, 9, 44, os.getuid(), A.CHROME))
            self.assertTrue(reader.profile(process, Path("/fixture/profile")))
            (folder / "cmdline").write_bytes(b"chrome\0--user-data-dir=/fixture/profile\0--user-data-dir=/other\0")
            self.assertFalse(reader.profile(process, Path("/fixture/profile")))
            actual = A._read_small
            count = 0
            def swapping(path, cap):
                nonlocal count
                if path.name == "stat":
                    count += 1
                    if count == 2: return record[:-2] + b"55"
                return actual(path, cap)
            with patch.object(A, "_read_small", side_effect=swapping):
                self.assertIsNone(reader.identity(100))


def row(index, pid, muted=False):
    return {"index": index, "mute": muted, "properties": {"application.process.id": str(pid)},
            "media.name": "must not appear in a result"}


class Mixer:
    def __init__(self, rows):
        self.rows = rows; self.commands = []; self.hook = lambda args, mixer: None
    def run(self, args, timeout):
        self.commands.append(args); self.hook(args, self)
        if args == ("--format=json", "list", "sink-inputs"):
            return json.dumps(self.rows).encode()
        self.rows[next(i for i, r in enumerate(self.rows) if str(r["index"]) == args[1])]["mute"] = True
        return b""
    def pulse(self):
        return A.Pulse(run=self.run, available=lambda: A.Availability(True, True))
    def writes(self): return [cmd for cmd in self.commands if cmd[0] == "set-sink-input-mute"]


class Muting(unittest.TestCase):
    def test_mutes_only_exact_owned_stream_and_verifies_readback(self):
        reader = FakeProcesses(); mixer = Mixer([row(1, 120), row(2, 200), row(3, 110, True)])
        state = A.InstanceAudio(True)
        result = mixer.pulse().mute_owned(reader.owner(), state, lambda: True)
        self.assertEqual(result, A.MuteResult(1, True))
        self.assertEqual(mixer.writes(), [("set-sink-input-mute", "1", "1")])
        self.assertFalse(mixer.rows[1]["mute"])
        self.assertNotIn("media.name", repr(result))
        self.assertFalse(state.synchronized(True))

    def test_reused_index_or_pid_before_command_never_mutes(self):
        for mode in ("index", "pid", "root"):
            reader = FakeProcesses(); owner = reader.owner(); mixer = Mixer([row(1, 120)])
            def reuse(args, mixer):
                if len(mixer.commands) == 2:
                    if mode == "index": mixer.rows[0] = row(1, 200)
                    elif mode == "pid": reader.rows[120] = replace(reader.rows[120], started=999)
                    else: reader.rows[100] = replace(reader.rows[100], started=999)
            mixer.hook = reuse
            result = mixer.pulse().mute_owned(owner, A.InstanceAudio(True), lambda: True)
            with self.subTest(mode=mode):
                self.assertFalse(result.complete); self.assertEqual(mixer.writes(), [])

    def test_stale_generation_before_command_stops_without_write(self):
        reader = FakeProcesses(); mixer = Mixer([row(1, 120)]); valid = True
        def change(args, mixer):
            nonlocal valid
            if len(mixer.commands) == 2: valid = False
        mixer.hook = change
        result = mixer.pulse().mute_owned(reader.owner(), A.InstanceAudio(True), lambda: valid)
        self.assertFalse(result.complete); self.assertEqual(mixer.writes(), [])

    def test_late_off_command_after_on_retains_taint_and_no_unmute(self):
        reader = FakeProcesses(); mixer = Mixer([row(1, 120)]); state = A.InstanceAudio(True)
        entered = threading.Event(); release = threading.Event(); valid = True; results = []
        def hold(args, mixer):
            if args[0] == "set-sink-input-mute":
                entered.set(); self.assertTrue(release.wait(2))
        mixer.hook = hold
        worker = threading.Thread(target=lambda: results.append(mixer.pulse().mute_owned(reader.owner(), state, lambda: valid)))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            valid = False
            self.assertEqual(state.snapshot(), (1, True, 1))
            self.assertFalse(state.synchronized(True))
        finally:
            release.set(); worker.join(2)
        self.assertFalse(worker.is_alive()); self.assertFalse(results[0].complete)
        self.assertEqual(state.snapshot(), (1, True, 0))
        self.assertFalse(state.synchronized(True))
        self.assertEqual(mixer.writes(), [("set-sink-input-mute", "1", "1")])

    def test_timeout_after_side_effect_does_not_erase_mute_taint(self):
        reader = FakeProcesses(); mixer = Mixer([row(1, 120)]); state = A.InstanceAudio(True)
        def timeout(args, mixer):
            if args[0] == "set-sink-input-mute":
                mixer.rows[0]["mute"] = True
                raise A.AudioRefused(A.MUTE_UNCERTAIN)
        mixer.hook = timeout
        result = mixer.pulse().mute_owned(reader.owner(), state, lambda: True)
        self.assertFalse(result.complete); self.assertFalse(state.synchronized(True))
        self.assertEqual(state.snapshot(), (1, True, 0))

    def test_unsettled_native_cleanup_keeps_attempt_reserved_until_actual_exit(self):
        reader = FakeProcesses(); mixer = Mixer([row(1, 120)]); state = A.InstanceAudio(True)
        entered = threading.Event(); release = threading.Event(); finished = threading.Event()
        class Unsettled:
            def wait(self):
                entered.set(); release.wait(2); finished.set()
        def late(args, mixer):
            if args[0] == "set-sink-input-mute": raise A._UnsettledMute(Unsettled())
        mixer.hook = late
        try:
            result = mixer.pulse().mute_owned(reader.owner(), state, lambda: True)
            self.assertFalse(result.complete); self.assertTrue(entered.wait(1))
            self.assertEqual(state.snapshot(), (1, True, 1))
            before = len(mixer.commands)
            self.assertFalse(mixer.pulse().mute_owned(reader.owner(), state, lambda: True).complete)
            self.assertEqual(len(mixer.commands), before, "pending utility must prevent another batch")
        finally:
            release.set(); self.assertTrue(finished.wait(1))
        # Join the exact cleanup thread so completion, not a guessed delay, is tested.
        for thread in threading.enumerate():
            if thread.name == "browser-mute-cleanup": thread.join(1)
        self.assertEqual(state.snapshot(), (1, True, 0))
        self.assertFalse(state.synchronized(True))

    def test_readback_identity_reuse_or_still_unmuted_is_not_success(self):
        for mode in ("index", "mute"):
            reader = FakeProcesses(); mixer = Mixer([row(1, 120)])
            def change(args, mixer):
                if len(mixer.commands) == 4:
                    mixer.rows[0] = row(1, 200 if mode == "index" else 120)
            mixer.hook = change
            result = mixer.pulse().mute_owned(reader.owner(), A.InstanceAudio(True), lambda: True)
            with self.subTest(mode=mode): self.assertFalse(result.complete)

    def test_malformed_rows_and_bounds_never_write(self):
        bad = [b"private utility failure", b"{}", b'[{"index":1,"mute":false}]', json.dumps([row(1, 120), row(1, 200)]).encode(),
               json.dumps([row(True, 120)]).encode(), json.dumps([row(1, "0120")]).encode(),
               b"x" * (A.OUTPUT_CAP + 1), json.dumps([row(i, 120) for i in range(A.STREAM_CAP + 1)]).encode()]
        for raw in bad:
            with self.subTest(raw_size=len(raw)), self.assertRaises(A.AudioRefused): A.sink_inputs(raw)
        mixer = Mixer([row(i, 120) for i in range(A.MUTE_CAP + 1)])
        result = mixer.pulse().mute_owned(FakeProcesses().owner(), A.InstanceAudio(True), lambda: True)
        self.assertFalse(result.complete); self.assertEqual(mixer.writes(), [])

    def test_unavailable_has_no_command_and_empty_batch_is_not_silence_claim(self):
        pulse = A.Pulse(run=lambda *_: self.fail("utility used"), available=lambda: A.Availability(True, False))
        self.assertEqual(pulse.mute_owned(FakeProcesses().owner(), A.InstanceAudio(True), lambda: True),
                         A.MuteResult(0, False, A.MUTE_UNAVAILABLE))
        self.assertEqual(Mixer([]).pulse().mute_owned(FakeProcesses().owner(), A.InstanceAudio(True), lambda: True),
                         A.MuteResult(0, True))


class Utility(unittest.TestCase):
    def test_no_global_volume_unmute_or_arbitrary_command(self):
        with patch.object(A.subprocess, "Popen") as start:
            for args in (("set-sink-mute", "0", "1"), ("set-sink-input-mute", "1", "0"),
                         ("set-sink-input-volume", "1", "0"), ("info",),
                         ("set-sink-input-mute", str(2**32 - 1), "1")):
                with self.subTest(args=args), self.assertRaises(A.AudioRefused): A._pactl(args, 1)
            start.assert_not_called()

    def test_actual_bounded_child_receives_only_curated_environment(self):
        real = subprocess.Popen
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "utility.py"
            script.write_text("import json,os; print(json.dumps(dict(os.environ)))")
            received = []
            def start(argv, **kwargs):
                received.append((argv, kwargs["env"]))
                return real([sys.executable, str(script)], **kwargs)
            with patch.object(A.subprocess, "Popen", side_effect=start):
                raw = A._pactl(("--format=json", "list", "sink-inputs"), 2)
            self.assertEqual(received[0][0], [str(A.PACTL), "--format=json", "list", "sink-inputs"])
            self.assertEqual(received[0][1], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PULSE_SERVER": A.PULSE_SERVER})
            self.assertNotIn("HOME", json.loads(raw))

    def test_failed_utility_returns_fixed_reason_without_output(self):
        real = subprocess.Popen
        def start(_argv, **kwargs):
            return real([sys.executable, "-c", "import sys; print('PRIVATE'); sys.stderr.write('PRIVATE'); sys.exit(1)"], **kwargs)
        with patch.object(A.subprocess, "Popen", side_effect=start):
            with self.assertRaises(A.AudioRefused) as found:
                A._pactl(("--format=json", "list", "sink-inputs"), 2)
        self.assertEqual(str(found.exception), A.MUTE_UNCERTAIN)

    def test_cleanup_timeout_returns_pending_process_and_closes_pipe(self):
        real = subprocess.Popen; processes = []
        def start(_argv, **kwargs):
            process = real([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            wait = process.wait
            def controlled(timeout=None):
                if timeout == 1: raise subprocess.TimeoutExpired("fixture", timeout)
                return wait(timeout=timeout)
            process.wait = controlled
            return process
        try:
            with patch.object(A.subprocess, "Popen", side_effect=start):
                with self.assertRaises(A._UnsettledMute) as found:
                    A._pactl(("--format=json", "list", "sink-inputs"), .05)
            self.assertIs(found.exception.process, processes[0])
            self.assertTrue(processes[0].stdout.closed)
        finally:
            if processes: real.wait(processes[0], timeout=2)

    def test_oversized_output_and_timeout_kill_and_reap_child(self):
        real = subprocess.Popen
        for code, timeout in (("import os; os.write(1,b'x'*300000)", 2), ("import time; time.sleep(30)", .05)):
            processes = []
            def start(_argv, **kwargs):
                process = real([sys.executable, "-c", code], **kwargs); processes.append(process); return process
            with self.subTest(kind=timeout), patch.object(A.subprocess, "Popen", side_effect=start):
                with self.assertRaises(A.AudioRefused): A._pactl(("--format=json", "list", "sink-inputs"), timeout)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdout.closed)


if __name__ == "__main__": unittest.main()