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
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3.daemon import Daemon
from tests.v3.computer_check import FakeDesktop, SHOT, row


OWNER = "/v3/owner/computer"
DRIVER = "/v3/owner/computer/do"
PHONE_HOST = "phone.example.invalid"
DRIVER_FIELDS = {
    "screenshot": {}, "windows": {}, "click": {"x": 10, "y": 20},
    "scroll": {"x": 10, "y": 20, "amount": -3}, "front": {"n": 1},
}


class FakeBridge(FakeDesktop):
    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        if op in ("windows", "window_screenshot"):
            return super().request(op, timeout, require_pair=require_pair, current=current, **fields)
        self.calls.append((op, require_pair, fields))
        if self.during is not None:
            self.during(op)
        if op == "window_probe":
            return {"name": "Example", "role": "button"}
        if op == "window_click":
            return {"clicked": True}
        if op == "window_scroll":
            return {"scrolled": fields["amount"]}
        if op == "window_front":
            return {"front": True}
        raise AssertionError(f"Unexpected op {op}")

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

    def test_action_routes_and_start_record_before_bridge_input(self):
        self.start()
        code = self.grant()
        path = self.root / "v3" / "computer_actions.jsonl"
        at_click = []

        def observe(op):
            if op == "window_click":
                at_click.extend(path.read_text().splitlines() if path.exists() else [])

        self.bridge.during = observe

        def call(op):
            return self.post(DRIVER, {"op": op, "code": code, **DRIVER_FIELDS[op]})

        label = {"app": "notepad.exe", "title": "Notes"}
        reply = {**label, "control": {"name": "Example", "role": "button"}}
        self.assertEqual(call("windows"), (200, {"windows": [
            {"n": 1, **label, "front": False, "access": "drivable"},
            {"n": 2, **label, "front": True, "access": "drivable"},
        ]}))
        self.assertEqual(call("screenshot"), (200, {**label, "image_b64": SHOT["image_b64"],
                                                   "size": SHOT["size"]}))
        self.assertEqual(call("click"), (200, reply))
        click, = map(json.loads, at_click)
        self.assertEqual((click["state"], click["op"], click["x"], click["y"]), ("start", "click", 10, 20))
        start, outcome = map(json.loads, path.read_text().splitlines())
        self.assertEqual(start, click)
        self.assertEqual((outcome["id"], outcome["state"]), (click["id"], "done"))
        self.assertEqual(call("scroll"), (200, {**reply, "amount": -3}))
        self.assertEqual(call("front"), (200, label))
        self.assertEqual([op for op, _, _ in self.bridge.calls],
                         ["windows", "windows", "window_screenshot", "window_probe", "window_click",
                          "window_probe", "window_scroll", "window_front"])
        self.assertTrue(all(paired for _, paired, _ in self.bridge.calls))

    def test_driver_route_validates_each_shape_and_checks_the_code(self):
        self.start()
        self.grant()
        for op, fields in DRIVER_FIELDS.items():
            valid = {"op": op, "code": "fake-wrong-code", **fields}
            with self.subTest(op=op):
                status, body = self.post(DRIVER, valid)
                self.assertEqual(status, 409)
                self.assertIn("invalid or expired", body["message"])
            malformed = [dict(valid, extra=True), dict(valid, code=None)]
            malformed.extend({key: value for key, value in valid.items() if key != missing}
                             for missing in valid)
            with patch.object(self.daemon.computer, op, Mock()) as handler:
                for request in malformed:
                    with self.subTest(request=request):
                        self.assertEqual(self.post(DRIVER, request)[0], 400)
                handler.assert_not_called()
        for op in ("not-an-operation", "start", [], {}):
            with self.subTest(op=op):
                self.assertEqual(self.post(DRIVER, {"op": op, "code": "fake-code"})[0], 400)
        self.assertEqual(self.bridge.calls, [])

    def test_numeric_fields_are_validated_by_the_service(self):
        self.start()
        code = self.grant()
        self.assertEqual(self.post(DRIVER, {"op": "screenshot", "code": code})[0], 200)
        self.assertEqual(self.post(DRIVER, {"op": "windows", "code": code})[0], 200)
        self.bridge.calls.clear()
        for op, fields in (("click", {"x": "10", "y": 20}),
                           ("scroll", {"x": 10, "y": 20, "amount": False}),
                           ("front", {"n": [1]})):
            with self.subTest(op=op):
                self.assertEqual(self.post(DRIVER, {"op": op, "code": code, **fields})[0], 409)
        self.assertEqual(self.bridge.calls, [])

    def test_phone_cannot_call_any_driver_operation(self):
        self.start()
        code = self.grant()
        for op, fields in DRIVER_FIELDS.items():
            with self.subTest(op=op):
                self.assertEqual(self.post(DRIVER, {"op": op, "code": code, **fields}, phone=True)[0], 404)
        self.assertEqual(self.bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
