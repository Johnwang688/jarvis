"""Computer control on the real v3 HTTP handlers with an isolated bridge fake."""

import base64
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.daemon import Daemon
from tests.v3.computer_check import FakeDesktop, SHOT, row


OWNER = "/v3/owner/computer"
DRIVER = "/v3/owner/computer/do"
PHONE_HOST = "phone.example.invalid"


class FakeBridge(FakeDesktop):
    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False


class ComputerRoutesCheck(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = root
        self.enterContext(patch.dict(os.environ, {"HOME": str(root), "JARVIS_DESKTOP_DRIVE": "0"}))
        self.enterContext(patch.object(config, "remote_dir", return_value=root / "remote"))
        self.enterContext(patch.object(config, "voice_link_key", return_value=root / "voice-key"))
        self.bridge = FakeBridge([row("a" * 16), row("b" * 16, front=True)])
        with patch("jarvis.v3.daemon.DesktopSession", return_value=self.bridge):
            self.daemon = Daemon(port=0, phone_port=0, phone_host=PHONE_HOST, voice_link_port=0,
                                 desktop_port=0, data_dir=root / "v3", hud_dir=root / "hud", jobs=())
        self.addCleanup(self.daemon.stop)

    def start(self, *, enabled=True):
        os.environ["JARVIS_DESKTOP_DRIVE"] = "1" if enabled else "0"
        self.daemon.start()

    def post(self, path, body, *, phone=False, origin=True):
        port = self.daemon.phone_port if phone else self.daemon.port
        self.assertGreater(port, 0)
        host = PHONE_HOST if phone else f"127.0.0.1:{port}"
        headers = {"Host": host, "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"https://{host}" if phone else f"http://{host}"
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            raw = body if isinstance(body, bytes) else json.dumps(body)
            connection.request("POST", path, raw, headers)
            response = connection.getresponse()
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def grant(self):
        status, body = self.post(OWNER, {"action": "start"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["message"])
        self.assertTrue(body["code"])
        return body["code"]

    def test_start_screenshot_status_and_stop(self):
        self.start()
        code = self.grant()
        self.assertEqual(self.post(OWNER, {"action": "status"}),
                         (200, {"enabled": True, "driving": True, "bridge": True}))
        self.assertEqual(self.post(DRIVER, {"op": "screenshot", "code": code}),
                         (200, {"image_b64": SHOT["image_b64"], "size": [800, 600],
                                "app": "notepad.exe", "title": "Notes"}))
        self.assertEqual([op for op, _, _ in self.bridge.calls], ["windows", "window_screenshot"])
        self.assertEqual(self.bridge.calls[1][2]["window"], "b" * 16)
        for expected in (True, False):
            status, body = self.post(OWNER, {"action": "stop"})
            self.assertEqual(status, 200)
            self.assertIs(body["stopped"], expected)
            self.assertTrue(body["message"])

    def test_wrong_code_is_conflict_without_bridge_use(self):
        self.start()
        self.grant()
        status, body = self.post(DRIVER, {"op": "screenshot", "code": "fake-wrong-code"})
        self.assertEqual(status, 409)
        self.assertEqual(set(body), {"message"})
        self.assertEqual(self.bridge.calls, [])

    def test_cli_screenshot_calls_the_daemon_and_saves_the_picture(self):
        self.start()
        code = self.grant()
        path = self.root / "example.png"
        env = {**os.environ, "JARVIS_V3_PORT": str(self.daemon.port), "JARVIS_COMPUTER_CODE": code}
        result = subprocess.run([sys.executable, "-m", "jarvis", "computer", "screenshot", "--out", str(path)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [str(path), "window: notepad.exe - Notes", "size: 800x600"])
        self.assertEqual(path.read_bytes(), base64.b64decode(SHOT["image_b64"]))

    def test_switched_off_is_conflict(self):
        self.start(enabled=False)
        for path, request in ((OWNER, {"action": "start"}), (DRIVER, {"op": "screenshot", "code": "fake-code"})):
            with self.subTest(path=path):
                status, body = self.post(path, request)
                self.assertEqual(status, 409)
                self.assertIn("switched off", body["message"])
        self.assertEqual(self.bridge.calls, [])

    def test_both_routes_require_owner_headers(self):
        self.start()
        for path, request in ((OWNER, {"action": "start"}), (DRIVER, {"op": "screenshot", "code": "fake-code"})):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, request, origin=False)[0], 403)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.daemon.computer.status()["driving"])

    def test_malformed_bodies_are_bad_requests(self):
        self.start()
        for path, bodies in (
            (OWNER, ({}, [], b"{", {"action": "unknown"}, {"action": "start", "code": "fake-code"})),
            (DRIVER, ({}, [], b"{", {"op": "click", "code": "fake-code"},
                      {"op": "screenshot"}, {"op": "screenshot", "code": None})),
        ):
            for request in bodies:
                with self.subTest(path=path, request=request):
                    self.assertEqual(self.post(path, request)[0], 400)
        self.assertEqual(self.bridge.calls, [])

    def test_phone_has_neither_route(self):
        self.start()
        for path, request in ((OWNER, {"action": "start"}), (DRIVER, {"op": "screenshot", "code": "fake-code"})):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, request, phone=True)[0], 404)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.daemon.computer.status()["driving"])

    def test_daemon_stop_ends_grant_across_restart(self):
        self.start()
        code = self.grant()
        self.daemon.stop()
        self.start()
        self.assertEqual(self.post(DRIVER, {"op": "screenshot", "code": code})[0], 409)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.daemon.computer.status()["driving"])


if __name__ == "__main__":
    unittest.main()
