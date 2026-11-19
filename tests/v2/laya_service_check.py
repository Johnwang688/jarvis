"""Resident Laya protocol checks with an isolated Python stand-in for CUDA."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from jarvis.v2 import laya_service
from jarvis.v2.laya_routing import CHOICES
from jarvis.v2.laya_schema import ENGLISH_REVISION, REPO


_WORKER = """
import json, sys, time
time.sleep(float(sys.argv[1]))
print('{"ready": true}')
for line in sys.stdin:
    request = json.loads(line)
    text = request["text"]
    if text == "crash":
        sys.exit(17)
    if text == "slow":
        time.sleep(1)
    if text == "bad json":
        print("{")
        continue
    scores = dict.fromkeys(sys.argv[2:], 0.01)
    choice = "open_app" if text.startswith("Launch") else "navigate"
    scores[choice] = float("nan") if text == "bad score" else 0.94
    print(json.dumps({
        "id": request["id"] + (text == "wrong id"),
        "scores": None if text == "truncated" else scores,
    }))
"""


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.workers = []
        self.startup_delay = 0
        launcher = patch.object(laya_service, "_launch_worker", self.launch)
        launcher.start()
        self.addCleanup(launcher.stop)
        self.router = laya_service.LayaRouter()
        self.addCleanup(self.router.close)

    def launch(self):
        worker = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", _WORKER, str(self.startup_delay), *CHOICES],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.workers.append(worker)
        return worker

    def test_lazy_bypasses_and_grounding(self):
        self.assertEqual(self.router.route("dictation", "Visit example.com"),
                         {"kind": "dictate", "target": "Visit example.com", "scores": {}})
        self.assertEqual(self.router.route("wake", "Jarvis, type the words Hi"),
                         {"kind": "literal", "target": "Hi", "scores": {}})
        self.assertEqual(self.workers, [])
        result = self.router.route("assistant", "Launch Calendar", apps=["Calendar"])
        self.assertEqual((result["kind"], result["target"], result["scores"]["open_app"]),
                         ("open_app", "Calendar", 0.94))
        self.assertEqual(self.router.route("assistant", "Launch Calendar")["kind"], "clarify")
        self.assertEqual(len(self.workers), 1)

    def test_concurrent_first_load_and_grounding_use_one_worker(self):
        self.startup_delay = 0.1
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            turns = [pool.submit(self.router.route, "assistant", f"Launch App{index}",
                                 apps=[f"App{index}"]) for index in range(6)]
            self.assertEqual([turn.result(timeout=4)["target"] for turn in turns],
                             [f"App{index}" for index in range(6)])
        self.assertEqual(len(self.workers), 1)

    def test_startup_timeout_then_clean_restart(self):
        self.startup_delay = 1
        with patch.object(laya_service, "_STARTUP_TIMEOUT", 0.12):
            self.assert_unavailable("Visit old.example.com")
        self.assertIsNotNone(self.workers[0].poll())
        self.startup_delay = 0
        self.assert_restarts()

    def test_request_timeout_then_clean_restart(self):
        self.assertEqual(self.router.route("assistant", "Visit old.example.com")["kind"], "navigate")
        with patch.object(laya_service, "_REQUEST_TIMEOUT", 0.12):
            self.assert_unavailable("slow")
        self.assertIsNotNone(self.workers[0].poll())
        self.assert_restarts()

    def test_worker_crash_or_malformed_output_retires_process(self):
        for text in ("crash", "bad json", "bad score", "wrong id"):
            with self.subTest(text=text):
                self.assert_unavailable(text)
                self.assertIsNotNone(self.workers[-1].poll())
                self.assertEqual(self.router.route("assistant", "Visit example.com")["target"],
                                 "https://example.com")

    def test_truncation_fails_closed_without_retiring_worker(self):
        self.assert_unavailable("truncated")
        self.assertEqual(self.router.route("assistant", "Visit example.com")["target"],
                         "https://example.com")
        self.assertEqual(len(self.workers), 1)

    def test_close_cancels_in_flight_request_and_prevents_restart(self):
        pending = threading.Event()
        write = self.router._write

        def signal_write(*args):
            write(*args)
            pending.set()

        with (patch.object(self.router, "_write", signal_write),
              concurrent.futures.ThreadPoolExecutor() as pool):
            active = pool.submit(self.router.route, "assistant", "slow")
            self.assertTrue(pending.wait(3))
            self.router.close()
            self.assertEqual(active.result(timeout=1)["kind"], "unavailable")
        self.assertIsNotNone(self.workers[0].poll())
        self.assert_unavailable("Visit example.com")
        self.assertEqual(len(self.workers), 1)
        self.router.close()

    def assert_unavailable(self, text):
        self.assertEqual(self.router.route("assistant", text),
                         {"kind": "unavailable", "target": "", "scores": {}})

    def assert_restarts(self):
        self.assertEqual(self.router.route("assistant", "Visit fresh.example.com")["target"],
                         "https://fresh.example.com")
        self.assertEqual(len(self.workers), 2)


class ManifestTests(unittest.TestCase):
    def test_launch_isolated_with_pinned_local_runtime_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "runtime.json"
            config = {"repo": REPO, "revision": ENGLISH_REVISION, "python": sys.executable,
                      "checkpoint": str(root), "device": "cuda:0"}
            manifest.write_text(json.dumps(config), encoding="utf-8")
            with (patch.object(laya_service, "DEFAULT_RUNTIME", manifest),
                  patch.dict(os.environ, {"HF_TOKEN": "unused", "PYTHONPATH": "unused"}, clear=True),
                  patch.object(subprocess, "Popen") as popen):
                laya_service._launch_worker()
                args, kwargs = popen.call_args
                self.assertEqual(args[0][:3], [sys.executable, "-I", "-B"])
                self.assertEqual(args[0][-2:], [str(root), "cuda:0"])
                self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
                self.assertEqual(kwargs["env"]["TRANSFORMERS_OFFLINE"], "1")
                self.assertNotIn("HF_TOKEN", kwargs["env"])
                self.assertNotIn("PYTHONPATH", kwargs["env"])
                self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                popen.reset_mock()
                with patch.dict(os.environ, {laya_service.RUNTIME_ENV: manifest.name}):
                    with self.assertRaises(ValueError):
                        laya_service._launch_worker()
                manifest.write_text(json.dumps(dict(config, revision="0" * 40)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    laya_service._launch_worker()
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
