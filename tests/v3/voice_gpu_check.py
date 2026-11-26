"""Exercise the GPU voice boundary with local Python fixtures only."""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from jarvis import config, rules
from jarvis.tools import files
from jarvis.v3.daemon import Daemon
from jarvis.v3.voice import engines, gpu_voice, installation
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_link_check import until
from voice_session_check import Brain, CANCELLED, Detector, QUIET, RATE, RAW, Rows, Transcriber, Voice


FAKE_WORKER = """
import json
import os
from pathlib import Path
import signal
import struct
import sys
import time

root = Path(__file__).parent
def record(name, item):
    with (root / name).open("a") as log:
        print(json.dumps(item), file=log)

record("starts", {"pid": os.getpid(), "pgid": os.getpgrp(), "argv": sys.argv,
                  "env": dict(os.environ), "cwd": os.getcwd(), "files": os.listdir()})
start = (root / "startup").read_text()
if start == "slow":
    while not (root / "release").exists():
        time.sleep(0.005)
if start == "never":
    time.sleep(60)
out = sys.stdout.buffer
out.write(b'{"ready":true}\\n')
out.flush()
pcm = struct.pack("<2000f", *([0] * 700 + [0.5] * 600 + [0] * 700))
for line in sys.stdin.buffer:
    request = json.loads(line)
    record("requests", request)
    mode = (root / "response").read_text()
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    if mode == "bad header":
        out.write(json.dumps({"id": request["id"], "ok": True, "rate": True, "samples": 2000}).encode() + b"\\n")
        out.flush()
        continue
    count = {"short read": 3000, "surplus": 1, "huge count": 999999999, "long duration": 480001}.get(mode, 2000)
    request_id = request["id"] - 1 if mode == "wrong id" else request["id"]
    rate = 48001 if mode == "high rate" else 8000
    out.write(json.dumps({"id": request_id, "ok": True, "rate": rate, "samples": count}).encode() + b"\\n")
    if mode == "mid reply":
        out.write(pcm[:5])
        out.flush()
        sys.exit(2)
    if mode == "long duration":
        out.write(struct.pack("<f", 0.5) * count)
    elif mode in ("nonfinite", "clip"):
        sample = float("nan") if mode == "nonfinite" else 4.0
        out.write(struct.pack("<f", sample) * count)
    else:
        out.write(pcm)
    out.flush()
    if mode == "short read":
        sys.stdout.close()
        os.close(1)
        time.sleep(60)
"""


class WorkerCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.data = self.root / "data"
        self.python = self.root / "fixture-environment" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        (self.python.parent.parent / gpu_voice._INSTALLED).touch()
        self.script = self.root / "worker.py"
        self.script.write_text(FAKE_WORKER)
        self.enterContext(patch.object(config, "V3_VOICE_GPU_PYTHON", self.python))
        self.enterContext(patch.object(config, "KOKORO_MODEL", self.root / "fake-model"))
        self.enterContext(patch.object(config, "KOKORO_VOICES", self.root / "fake-voices"))
        self.enterContext(patch.object(gpu_voice, "_WORKER", self.script))
        self.startup("")
        self.response("")

    def voice(self):
        voice = gpu_voice.KokoroWorkerVoice(data_dir=self.data)
        self.addCleanup(voice.close)
        return voice

    def read(self, name):
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith("\n")]

    def startup(self, mode):
        (self.root / "startup").write_text(mode)

    def response(self, mode):
        (self.root / "response").write_text(mode)

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_round_trip_and_empty_input(self):
        voice = self.voice()
        for text in ("", "```python\nfixture\n```"):
            pcm, rate = voice.synthesize(text)
            self.assertEqual((pcm.size, pcm.dtype, rate), (0, np.int16, 24000))
        self.assertEqual(self.read("starts"), [])
        with patch.object(engines.KokoroVoice, "_load", return_value=SimpleNamespace(
            create=lambda *_args, **_kwargs: (np.r_[np.zeros(700, np.float32),
                np.full(600, 0.5, np.float32), np.zeros(700, np.float32)], 8000),
        )):
            expected, _ = engines.KokoroVoice(device="cpu").synthesize("A fixture.")
        voice.speed = 1.6
        pcm, rate = voice.synthesize("**A fixture.**")
        self.assertEqual((len(pcm), pcm.dtype, rate), (1560, np.int16, 8000))
        np.testing.assert_array_equal(pcm, expected)
        voice.speed = 1.1
        voice.synthesize("Another fixture.")
        self.assertEqual(self.read("requests"), [
            {"id": 1, "text": "A fixture.", "speed": 1.6},
            {"id": 2, "text": "Another fixture.", "speed": 1.1},
        ])
        self.assertEqual(len(self.read("starts")), 1)

    def test_launch_contract(self):
        expected_env = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": str(self.root)}
        extras = {name: "fake-token" for name in (
            "OPENROUTER_API_KEY", "PYTHONPATH", "LD_LIBRARY_PATH", "WSLENV", "CUDA_VISIBLE_DEVICES",
            "JARVIS_SECRET_FIXTURE",
        )}
        with patch.dict(os.environ, expected_env | extras, clear=True), patch.object(
            gpu_voice.subprocess, "Popen", wraps=subprocess.Popen,
        ) as popen:
            voice = self.voice()
            voice.synthesize("Fixture.")
        row, = self.read("starts")
        argv = [str(self.python), "-I", "-B", str(self.script), str(config.KOKORO_MODEL), str(config.KOKORO_VOICES)]
        self.assertEqual(popen.call_args.args, (argv,))
        self.assertEqual(row["argv"], argv[3:])
        self.assertEqual(row["env"], expected_env)
        self.assertEqual(row["pid"], row["pgid"])
        cwd = Path(row["cwd"])
        self.assertEqual(cwd.parent, self.data)
        self.assertEqual(stat.S_IMODE(cwd.stat().st_mode), 0o700)
        self.assertEqual(row["files"], [])
        self.assertEqual(popen.call_args.kwargs, dict(
            cwd=str(cwd), env=expected_env, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
        ))
        voice.close()
        self.assert_gone(row["pid"])
        self.assertFalse(cwd.exists())

    def test_refuses_missing_writable_and_foreign_environment(self):
        missing = "^GPU voice environment is not installed$"
        with patch.object(config, "V3_VOICE_GPU_PYTHON", self.python.with_name("missing")):
            with self.assertRaisesRegex(engines.EngineUnavailable, missing):
                self.voice()
        environment = self.python.parent.parent
        environment.chmod(0o777)
        with self.assertRaisesRegex(engines.EngineUnavailable, missing):
            self.voice()
        environment.chmod(0o700)
        alias = self.root / "linked-environment"
        alias.symlink_to(environment)
        real_stat = os.stat

        def stat_owner(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if Path(path) == alias:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return info

        with patch.object(config, "V3_VOICE_GPU_PYTHON", alias / "bin/python"), patch(
            "os.stat", side_effect=stat_owner,
        ):
            with self.assertRaisesRegex(engines.EngineUnavailable, missing):
                self.voice()
        self.assertEqual(self.read("starts"), [])

    def test_ancestor_and_resolved_interpreter_trust(self):
        environment = self.python.parent.parent
        owner = self.root / "owner"
        owner.mkdir()
        environment.rename(owner / environment.name)
        python = owner / environment.name / "bin/python"
        cache = self.root / "cache"
        cache.mkdir()
        target = cache / "python"
        target.touch(mode=0o700)
        python.unlink()
        python.symlink_to(target)
        self.assertEqual(gpu_voice.worker_python(python), python)

        for folder in (owner, cache):
            with self.subTest(folder=folder.name):
                folder.chmod(0o770)
                with self.assertRaisesRegex(engines.EngineUnavailable, "^GPU voice environment is not installed$"):
                    gpu_voice.worker_python(python)
                folder.chmod(0o1777)
                try:
                    accepted = gpu_voice.worker_python(python)
                except engines.EngineUnavailable:
                    self.fail("A trusted sticky ancestor should be accepted.")
                self.assertEqual(accepted, python)
        target.chmod(0o1777)
        with self.assertRaises(engines.EngineUnavailable):
            gpu_voice.worker_python(python)
        target.chmod(0o700)
        real_stat = os.stat

        for foreign in (owner, cache, target):
            with self.subTest(foreign=foreign.name):
                def stat_owner(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    if Path(path) == foreign:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                with patch("os.stat", side_effect=stat_owner), self.assertRaises(engines.EngineUnavailable):
                    gpu_voice.worker_python(python)

    def test_marker_required_and_rechecked_on_restart(self):
        marker = self.python.parent.parent / gpu_voice._INSTALLED
        marker.unlink()
        with self.assertRaises(engines.EngineUnavailable):
            self.voice()
        marker.touch()
        voice = self.voice()
        self.response("mid reply")
        with self.assertRaisesRegex(engines.EngineUnavailable, "worker failed"):
            voice.synthesize("First request.")
        self.assert_gone(self.read("starts")[-1]["pid"])
        (self.root / gpu_voice._INSTALLING).touch()
        with self.assertRaisesRegex(engines.EngineUnavailable, "^GPU voice environment is not installed$"):
            voice.synthesize("Cannot restart.")
        self.assertEqual(len(self.read("starts")), 1)

    def test_valid_peak_is_clipped(self):
        self.response("clip")
        pcm, _ = self.voice().synthesize("Fixture.")
        np.testing.assert_array_equal(pcm, np.full(2000, 32767, dtype=np.int16))

    def test_sample_cap_applies_even_to_short_audio(self):
        with patch.object(gpu_voice, "_MAX_SAMPLES", 1999):
            with self.assertRaisesRegex(engines.EngineUnavailable, "worker failed"):
                self.voice().synthesize("Fixture.")
        self.assert_gone(self.read("starts")[-1]["pid"])

    def test_protocol_failures_kill_the_group(self):
        with patch.multiple(gpu_voice, _START_TIMEOUT=0.7, _FIRST_TIMEOUT=0.4):
            for mode in ("never", "mid reply", "short read", "bad header", "huge count", "surplus", "hang",
                         "wrong id", "long duration", "high rate", "nonfinite"):
                with self.subTest(mode=mode):
                    self.startup("never" if mode == "never" else "")
                    self.response(mode)
                    with self.assertRaisesRegex(engines.EngineUnavailable, "^GPU voice worker failed$"):
                        self.voice().synthesize("Fixture failure.")
                    self.assert_gone(self.read("starts")[-1]["pid"])

    def test_restart_then_cooldown(self):
        offset = 0
        monotonic = time.monotonic
        clock = SimpleNamespace(monotonic=lambda: monotonic() + offset)
        with patch.object(gpu_voice, "time", clock):
            voice = self.voice()
            self.response("mid reply")
            with self.assertRaises(engines.EngineUnavailable):
                voice.synthesize("First failure.")
            self.assert_gone(self.read("starts")[-1]["pid"])
            self.response("")
            voice.synthesize("Successful restart.")
            self.response("mid reply")
            with self.assertRaises(engines.EngineUnavailable):
                voice.synthesize("Second failure.")
            self.assert_gone(self.read("starts")[-1]["pid"])
            with self.assertRaisesRegex(engines.EngineUnavailable, "temporarily unavailable"):
                voice.synthesize("Throttled.")
            self.assertEqual(len(self.read("starts")), 2)
            offset = 600
            self.response("")
            voice.synthesize("Allowed again.")
            self.assertEqual(len(self.read("starts")), 3)

    def test_warm_worker_uses_the_shorter_request_deadline(self):
        with patch.multiple(gpu_voice, _FIRST_TIMEOUT=3.0, _REQUEST_TIMEOUT=0.2):
            voice = self.voice()
            voice.synthesize("First request.")
            self.response("hang")
            started = time.monotonic()
            with self.assertRaises(engines.EngineUnavailable):
                voice.synthesize("Later request.")
            self.assertLess(time.monotonic() - started, 2)
            self.assert_gone(self.read("starts")[-1]["pid"])

    def test_close_during_request(self):
        voice = self.voice()
        self.response("hang")
        with ThreadPoolExecutor() as executor:
            active = executor.submit(voice.synthesize, "In flight.")
            try:
                until(lambda: bool(self.read("requests")))
            finally:
                started = time.monotonic()
                voice.close()
            self.assertLess(time.monotonic() - started, 5)
            with self.assertRaisesRegex(engines.EngineUnavailable, "GPU voice is closed"):
                active.result(timeout=1)
        self.assert_gone(self.read("starts")[-1]["pid"])

    def test_session_switches_and_plays_failed_sentence_once_on_cpu(self):
        self.startup("slow")
        cpu, best, brain, asr = Voice("fixture-cpu"), self.voice(), Brain(), Transcriber()
        events, audio = Rows(), Rows()
        now = 100.0
        clock = lambda: now
        turns = VoiceTurns(brain, clock=clock, on_event=lambda *_args, **_fields: None)
        self.addCleanup(turns.close)
        session = VoiceSession(
            turns, transcriber=asr, voices=(cpu, best), names=tuple, clock=clock, detector=Detector(), follow_up_s=0,
            on_event=lambda kind, **fields: events.add((kind, fields)),
            on_audio=lambda id, _pcm, rate, first, last: audio.add((id, rate, first, last)),
            quiet=lambda: False,
        )
        self.addCleanup(session.close)
        session.start()
        asr.warmed.wait(bool)

        def reply(number, sentence):
            nonlocal now
            asr.lines.append(f"Jarvis, fixture question {number}.")
            for raw, cancelled, count in ((RAW, CANCELLED, 8), (QUIET, QUIET, 25)):
                for _ in range(count):
                    now += 0.02
                    session.feed(raw, cancelled)
            brain.inputs.wait(lambda rows: len(rows) == number)
            brain.emit("reply", text=sentence)
            events.wait(lambda rows: sum(kind == "say" for kind, _ in rows) == number)
            id = [fields["id"] for kind, fields in events.rows if kind == "say"][-1]
            audio.wait(lambda rows: any(row[0] == id and row[3] for row in rows))
            session.played(id)
            until(lambda: session.status()["counters"]["played"] == number)
            rows = [row for row in audio.rows if row[0] == id]
            self.assertEqual(sum(row[2] for row in rows), 1)
            self.assertEqual(sum(row[3] for row in rows), 1)
            return rows[0][1]

        first, second, third, fourth = ("First fixture.", "Second fixture.", "Third fixture.", "Fourth fixture.")
        self.assertEqual(reply(1, first), RATE)
        self.assertFalse(session.status()["gpu_warm"])
        (self.root / "release").touch()
        until(lambda: session.status()["gpu_warm"])
        self.assertEqual(session.status()["voice"]["name"], "kokoro-gpu-worker")
        session.apply_settings(SimpleNamespace(speed=1.8, follow_up_s=0, end_silence_ms=500))
        self.assertEqual(reply(2, second), 8000)
        self.response("mid reply")
        self.assertEqual(reply(3, third), RATE)
        self.assertFalse(session.status()["gpu_warm"])
        self.assertEqual(session.status()["voice"]["name"], "fixture-cpu")
        self.assertEqual(session.status()["counters"]["voice_fallbacks"], 1)
        self.assertEqual(reply(4, fourth), RATE)
        self.assertEqual(cpu.calls.rows, [first, third, fourth])
        self.assertEqual([row["text"] for row in self.read("requests")], ["The voice is ready.", second, third])
        self.assertEqual(self.read("requests")[1]["speed"], 1.8)
        self.assertEqual(len(self.read("starts")), 1)
        self.assert_gone(self.read("starts")[-1]["pid"])

    def daemon(self, *, cuda, installed, setting="auto"):
        class FixtureBrain(Brain):
            def status(self):
                return {"brain": "fixture", "state": "idle", "turns": 0}

            def close(self):
                pass

        with patch.object(config, "V3_VOICE", setting), patch.object(
            config, "V3_VOICE_GPU_PYTHON", self.python if installed else self.python.with_name("missing"),
        ), patch.dict(sys.modules, {"onnxruntime": SimpleNamespace(
            get_available_providers=lambda: ["CUDAExecutionProvider"] if cuda else ["CPUExecutionProvider"],
        )}), patch("jarvis.v3.voice.claude_brain.ClaudeVoiceBrain", return_value=FixtureBrain()), patch.object(
            engines, "pick_transcriber", return_value=engines.EngineChoice(Transcriber(), "fixture", "fixture"),
        ):
            daemon = Daemon(port=0, voice_link_port=0, data_dir=self.data, jobs=(), phone_host=None)
        self.addCleanup(daemon.stop)
        daemon._voice_session._detector = Detector()
        return daemon

    def test_daemon_choice_and_status(self):
        with patch.object(engines.KokoroVoice, "synthesize", return_value=(np.ones(10, np.int16), 24000)):
            for cuda, installed, setting, name in (
                (True, True, "auto", "kokoro-gpu"),
                (False, True, "kokoro-gpu", "kokoro-gpu-worker"),
                (False, False, "kokoro-gpu", "kokoro-cpu"),
                (True, True, "kokoro-cpu", "kokoro-cpu"),
            ):
                with self.subTest(cuda=cuda, installed=installed, setting=setting):
                    daemon = self.daemon(cuda=cuda, installed=installed, setting=setting)
                    self.assertEqual(daemon._voice_status()["voice"]["name"], "kokoro-cpu")
                    daemon._voice_session.start()
                    until(lambda: daemon._voice_status()["voice"]["name"] == name)
                    daemon.stop()

    def test_daemon_closes_worker_during_warmup(self):
        self.response("hang")
        daemon = self.daemon(cuda=False, installed=True)
        daemon._voice_session.start()
        try:
            until(lambda: bool(self.read("requests")))
        finally:
            started = time.monotonic()
            daemon.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assert_gone(self.read("starts")[-1]["pid"])


class StandaloneWorkerCheck(unittest.TestCase):
    def test_real_worker_protocol_with_fake_libraries(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        worker = Path(gpu_voice.__file__).with_name("kokoro_worker.py")
        tree = ast.parse(worker.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name.split(".")[0] != "jarvis" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertTrue(node.level == 0 and node.module.split(".")[0] != "jarvis")
        (root / "onnxruntime.py").write_text(dedent("""
            import os
            print("fixture diagnostic", flush=True)
            class SessionOptions:
                def add_session_config_entry(self, *_args):
                    pass
            def get_available_providers():
                return ["CPUExecutionProvider"] if os.environ.get("FIXTURE_NO_CUDA") else ["CUDAExecutionProvider"]
            class InferenceSession:
                def __init__(self, model, sess_options, providers):
                    assert model == "fake-model"
                    assert providers == ["CUDAExecutionProvider"]
                    assert sess_options.intra_op_num_threads == 4
                def get_providers(self):
                    return get_available_providers()
                def disable_fallback(self):
                    self.no_fallback = True
        """))
        (root / "kokoro_onnx.py").write_text(dedent("""
            import os
            class Kokoro:
                @classmethod
                def from_session(cls, session, voices):
                    assert session.no_fallback and voices == "fake-voices"
                    return cls()
                def create(self, text, **options):
                    os.write(1, b"fixture native diagnostic")
                    assert text == "A fixture."
                    assert options == dict(voice="bm_george", speed=1.6, lang="en-gb", trim=False)
                    return [0.5, -0.5], 24000
        """))
        runner = root / "run_worker.py"
        runner.write_text(dedent("""
            import ctypes
            import sys
            from runpy import run_path
            ctypes.CDLL = lambda *_args, **_kwargs: object()
            sys.path.insert(0, sys.argv[1])
            sys.argv = sys.argv[2:]
            run_path(sys.argv[0], run_name="__main__")
        """))
        argv = [sys.executable, "-I", "-B", str(runner), str(root), str(worker), "fake-model", "fake-voices"]
        request = b'{"id":1,"text":"A fixture.","speed":1.6}\n'
        bad = b'{"id":2,"text":"A fixture.","speed":true}\n'
        missing_id = b'{"text":"A fixture.","speed":1.6}\n'
        result = subprocess.run(argv, input=request + bad + missing_id + b"x" * 9000 + b"\n" + request,
                                capture_output=True, check=True, timeout=5)
        start, header, remaining = result.stdout.split(b"\n", 2)
        self.assertEqual(json.loads(start), {"ready": True})
        self.assertEqual(json.loads(header), {"id": 1, "ok": True, "rate": 24000, "samples": 2})
        data = np.array([0.5, -0.5], dtype="<f4").tobytes()
        self.assertEqual(remaining[:8], data)
        malformed, unnumbered, oversized, last_header, last_data = remaining[8:].split(b"\n", 4)
        self.assertEqual([json.loads(line) for line in (malformed, unnumbered, oversized)],
                         [{"id": 2, "ok": False}, {"ok": False}, {"ok": False}])
        self.assertEqual(json.loads(last_header), json.loads(header))
        self.assertEqual(last_data, data)
        self.assertEqual(result.stderr, b"")
        result = subprocess.run(argv, input=b"", capture_output=True, timeout=5,
                                env=os.environ | {"FIXTURE_NO_CUDA": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [b'{"ok": false}'])
        self.assertEqual(result.stderr, b"")


class SetupCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "path"
        self.path.mkdir()
        self.data = self.root / "data"
        self.venv = self.data / "voice-gpu" / "venv"
        self.python = self.venv / "bin/python"
        self.enterContext(patch.object(config, "V3_DIR", self.data))
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.path)}))

    def executable(self, name, source):
        path = self.path / name
        path.write_text(f"#!{sys.executable}\n" + dedent(source))
        path.chmod(0o700)
        return path

    def fake_uv(self, *, fail=False):
        return self.executable("uv", f"""
            import json
            from pathlib import Path
            import sys

            with Path({str(self.root / "argv")!r}).open("a") as log:
                print(json.dumps(sys.argv), file=log)
            if sys.argv[1] == "venv":
                folder = Path(sys.argv[-1]) / "bin"
                folder.mkdir()
                folder.parent.chmod(0o755)
                (folder / "python").symlink_to(sys.executable)
            elif {fail!r}:
                sys.exit(1)
        """)

    def run_setup(self, helper=0):
        output = io.StringIO()
        with patch.object(installation, "_setup_helper", return_value=helper) as windows, redirect_stdout(output):
            self.assertEqual(installation.setup(), helper)
        windows.assert_called_once_with()
        return output.getvalue().splitlines()

    def test_install_argv_and_already_installed(self):
        self.executable("nvidia-smi", "pass")
        uv = self.fake_uv()
        real_run = subprocess.run

        def observe(argv, **kwargs):
            if argv[1:3] == ["pip", "install"]:
                self.assertTrue(self.python.is_file())
                with self.assertRaises(engines.EngineUnavailable):
                    gpu_voice.worker_python(self.python)
            return real_run(argv, **kwargs)

        with patch.object(installation.subprocess, "run", side_effect=observe):
            self.assertEqual(self.run_setup(), ["GPU voice environment installed."])
        self.assertEqual(self.run_setup(), ["GPU voice environment is already installed."])
        log = (self.root / "argv").read_text()
        entries = tomllib.loads((config.REPO_ROOT / "pyproject.toml").read_text())
        extras = entries["project"]["optional-dependencies"]
        wheels = [entry for entry in extras["voice-gpu"]
                  if entry.startswith("nvidia-") and not entry.startswith("nvidia-curand-cu12")]
        pinned = [*extras["voice-gpu-worker"], *wheels]
        install = [str(uv), "pip", "install", "--python", str(self.python)]
        # One install step, nothing resolved: every package is listed and pinned.
        self.assertEqual([json.loads(line) for line in log.splitlines()], [
            [str(uv), "venv", "--python", "3.12", str(self.venv)],
            [*install, "--no-deps", *pinned],
        ])
        self.assertIn("onnxruntime-gpu==1.23.2", pinned)
        for requirement in pinned:
            if requirement != "nvidia-curand-cu12==10.3.10.*":  # the one build wildcard
                self.assertRegex(requirement, r"^[\w-]+==[\w.+-]+$")
        for helper in ("coloredlogs", "flatbuffers", "humanfriendly", "mpmath", "packaging", "protobuf", "sympy",
                       "dlinfo", "numpy", "kokoro-onnx"):
            self.assertTrue(any(entry.startswith(helper + "==") for entry in pinned), helper)
        self.assertTrue((self.venv / gpu_voice._INSTALLED).is_file())
        self.assertFalse((self.venv.parent / gpu_voice._INSTALLING).exists())
        for folder in (self.venv.parent, self.venv):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_skip_and_failure_leave_helper_result(self):
        self.executable("nvidia-smi", "raise SystemExit(1)")
        self.assertEqual(self.run_setup(helper=1), ["No GPU was found for voice setup."])
        self.executable("nvidia-smi", "pass")
        self.assertEqual(self.run_setup(), ["GPU voice setup needs uv on PATH."])
        self.fake_uv(fail=True)
        self.assertEqual(self.run_setup(), ["GPU voice setup failed."])
        self.assertFalse(self.venv.exists())
        self.fake_uv()
        self.assertEqual(self.run_setup(), ["GPU voice environment installed."])

    def test_setup_still_asks_and_its_environment_is_protected(self):
        # The gate is unchanged by this package: an agent's `jarvis voice setup` asks the owner, as
        # every `jarvis` command does (tests/v3/v3b_review_link_check.py pins the same parity).
        for command in ("jarvis voice setup", "python -m jarvis voice setup"):
            self.assertEqual(rules.decide(command).decision, rules.decide(command.replace("setup", "status")).decision)
            self.assertNotEqual(rules.decide(command).decision, rules.ALLOW)
        self.assertTrue(files._v3_state(self.venv / "lib" / "fixture.py"))
        self.assertTrue(files._self_protected(Path(gpu_voice.__file__)))


if __name__ == "__main__":
    unittest.main()
