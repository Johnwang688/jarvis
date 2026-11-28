"""The computer CLI's loopback requests, output and private PNG storage."""

import argparse
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import cli


IMAGE_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWioAAAAASUVORK5CYII=")
PICTURE = base64.b64decode(IMAGE_B64)
SHOT = {"image_b64": IMAGE_B64, "size": [1, 1], "app": "notepad.exe", "title": "Example"}
ACTION_ARGS = (("click", "10", "20"), ("scroll", "10", "20", "-3"), ("windows",), ("front", "2"))


class ComputerCliCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.folder = self.root / f"jarvis-computer-{os.getuid()}"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "JARVIS_COMPUTER_CODE": ""}))
        self.enterContext(patch.object(tempfile, "gettempdir", return_value=str(self.root)))
        self.enterContext(patch.object(config, "V3_API_PORT", 0))
        self.parser = argparse.ArgumentParser()
        cli.add_commands(self.parser.add_subparsers(required=True), voice=False)

    def run_cli(self, *args):
        parsed = self.parser.parse_args(["computer", *args])
        with redirect_stdout(StringIO()) as output:
            result = parsed.func(parsed)
        return result, output.getvalue().splitlines()

    def reply(self, body=SHOT, status=200):
        connection = self.enterContext(patch.object(cli, "HTTPConnection"))
        response = connection.return_value.getresponse.return_value
        response.status = status
        response.read.return_value = json.dumps(body).encode()
        return connection.return_value

    def test_default_save_is_private_and_uses_explicit_code_first(self):
        connection = self.reply()
        os.environ["JARVIS_COMPUTER_CODE"] = "fake-env-code"
        result, lines = self.run_cli("screenshot", "--code", "fake-explicit-code")
        self.assertEqual(result, 0, lines)
        self.assertEqual(lines[1:], ["window: notepad.exe - Example", "size: 1x1"])
        path = Path(lines[0])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.folder)
        self.assertRegex(path.name, r"^shot-\d{8}-\d{6}-[0-9a-f]{8}\.png$")
        self.assertEqual(path.read_bytes(), PICTURE)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        connection.request.assert_called_once()
        method, route, body, headers = connection.request.call_args.args
        self.assertEqual((method, route), ("POST", "/v3/owner/computer/do"))
        self.assertEqual(json.loads(body), {"op": "screenshot", "code": "fake-explicit-code"})
        self.assertEqual(headers, {"Host": "127.0.0.1:0", "Origin": "http://127.0.0.1:0",
                                   "Content-Type": "application/json"})

    def test_default_folder_refuses_symlink(self):
        self.reply()
        target = self.root / "private"
        target.mkdir(mode=0o700)
        self.folder.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_cli("screenshot", "--code", "fake-code")[0], 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_default_folder_refuses_group_access(self):
        self.reply()
        self.folder.mkdir(mode=0o700)
        self.folder.chmod(0o750)
        self.assertEqual(self.run_cli("screenshot", "--code", "fake-code")[0], 1)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o750)

    def test_default_folder_refuses_a_file(self):
        self.reply()
        self.folder.write_bytes(b"fake file")
        self.assertEqual(self.run_cli("screenshot", "--code", "fake-code")[0], 1)
        self.assertEqual(self.folder.read_bytes(), b"fake file")

    def test_default_folder_refuses_a_different_owner(self):
        self.reply()
        self.folder.mkdir(mode=0o700)
        info = SimpleNamespace(st_uid=os.getuid() + 1, st_mode=stat.S_IFDIR | 0o700)
        with patch.object(os, "fstat", return_value=info):
            self.assertEqual(self.run_cli("screenshot", "--code", "fake-code")[0], 1)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_out_uses_environment_code_and_refuses_replacement(self):
        connection = self.reply()
        os.environ["JARVIS_COMPUTER_CODE"] = "fake-env-code"
        output = self.root / "custom.png"
        self.folder.mkdir(mode=0o700)
        old_shots = {self.folder / f"shot-fake-{index}.png" for index in range(21)}
        for old in old_shots:
            old.touch()
        result, lines = self.run_cli("screenshot", "--out", str(output))
        self.assertEqual((result, lines[0]), (0, str(output)))
        self.assertEqual(json.loads(connection.request.call_args.args[2])["code"], "fake-env-code")
        self.assertEqual(output.read_bytes(), PICTURE)
        self.assertEqual(set(self.folder.iterdir()), old_shots)
        output.write_bytes(b"original")
        self.assertEqual(self.run_cli("screenshot", "--out", str(output))[0], 1)
        self.assertEqual(output.read_bytes(), b"original")
        link = self.root / "link.png"
        missing = self.root / "missing.png"
        link.symlink_to(missing)
        self.assertEqual(self.run_cli("screenshot", "--out", str(link))[0], 1)
        self.assertTrue(link.is_symlink())
        self.assertFalse(missing.exists())

    def test_default_prunes_only_old_screenshots(self):
        self.reply()
        self.folder.mkdir(mode=0o700)
        old_shots = [self.folder / f"shot-fake-{index:02}.png" for index in range(22)]
        for index, old in enumerate(old_shots, start=1):
            old.touch()
            os.utime(old, (index, index))
        other = self.folder / "keep.png"
        other.touch()
        result, lines = self.run_cli("screenshot", "--code", "fake-code")
        self.assertEqual(result, 0, lines)
        self.assertEqual(set(self.folder.iterdir()), {other, Path(lines[0]), *old_shots[3:]})

    def test_missing_code_prints_help_without_http(self):
        with patch.object(cli, "HTTPConnection") as connection:
            result, lines = self.run_cli("screenshot")
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("--code", lines[0])
        self.assertIn("JARVIS_COMPUTER_CODE", lines[0])
        connection.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_unavailable_daemon_prints_one_line(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            with patch.object(config, "V3_API_PORT", reserved.getsockname()[1]):
                result, lines = self.run_cli("screenshot", "--code", "fake-code")
        self.assertEqual(result, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("daemon", lines[0])
        self.assertFalse(self.folder.exists())

    def test_invalid_base64_does_not_write_a_file(self):
        self.reply({**SHOT, "image_b64": IMAGE_B64[:4] + "!" + IMAGE_B64[4:]})
        self.assertEqual(self.run_cli("screenshot", "--code", "fake-code")[0], 1)
        self.assertFalse(self.folder.exists())

    def test_refusal_prints_daemon_message(self):
        self.reply({"message": "Example refusal."}, status=409)
        self.assertEqual(self.run_cli("screenshot", "--code", "fake-code"), (1, ["Example refusal."]))
        self.assertFalse(self.folder.exists())

    def test_owner_actions_use_owner_route_and_expected_output(self):
        connection = self.reply()
        read = connection.getresponse.return_value.read
        for action, body, line_count in (
            ("start", {"code": "fake-code", "message": "Started."}, 2),
            ("stop", {"stopped": True, "message": "Stopped."}, 1),
            ("status", {"enabled": True, "driving": False, "bridge": True}, 1),
        ):
            with self.subTest(action=action):
                read.return_value = json.dumps(body).encode()
                result, lines = self.run_cli(action)
                self.assertEqual(result, 0, lines)
                self.assertEqual(len(lines), line_count)
                if action == "start":
                    self.assertEqual(lines[0], "fake-code")
                    self.assertIn("--code", lines[1])
                method, route, payload, _ = connection.request.call_args.args
                self.assertEqual((method, route, json.loads(payload)),
                                 ("POST", "/v3/owner/computer", {"action": action}))

    def test_action_output_and_integer_request_fields(self):
        label = {"app": "example\napp.exe", "title": "Window\r\nTitle"}
        control = {"name": "Open\nitem", "role": "push\nbutton"}
        connection = self.reply()
        read = connection.getresponse.return_value.read
        os.environ["JARVIS_COMPUTER_CODE"] = "fake-env-code"
        for args, fields, reply, expected in (
            (ACTION_ARGS[0], {"x": 10, "y": 20}, {**label, "control": control},
             ['clicked: push button "Open item" in example app.exe - Window Title']),
            (ACTION_ARGS[1], {"x": 10, "y": 20, "amount": -3}, {**label, "control": control, "amount": -3},
             ['scrolled -3: push button "Open item" in example app.exe - Window Title']),
            (ACTION_ARGS[2], {}, {"windows": [
                {"n": 1, **label, "front": False, "access": "drivable"},
                {"n": 2, **label, "front": True, "access": "see_only"},
            ]}, ["1. example app.exe - Window Title", "2. example app.exe - Window Title (front) (view only)"]),
            (ACTION_ARGS[3], {"n": 2}, label, ["front: example app.exe - Window Title"]),
        ):
            with self.subTest(args=args):
                read.return_value = json.dumps(reply).encode()
                self.assertEqual(self.run_cli(*args), (0, expected))
                method, route, payload, _ = connection.request.call_args.args
                self.assertEqual((method, route), ("POST", "/v3/owner/computer/do"))
                self.assertEqual(json.loads(payload), {"op": args[0], "code": "fake-env-code", **fields})

    def test_each_action_prints_refusal_on_one_line(self):
        self.reply({"message": "Refused: Try again\nafter refreshing."}, status=409)
        for args in ACTION_ARGS:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args, "--code", "fake-code"),
                                 (1, ["Refused: Try again after refreshing."]))

    def test_each_action_needs_a_code_before_contacting_daemon(self):
        with patch.object(cli, "HTTPConnection") as connect:
            for args in ACTION_ARGS:
                with self.subTest(args=args):
                    self.assertEqual(self.run_cli(*args),
                                     (1, ["Pass the code with --code or set JARVIS_COMPUTER_CODE."]))
            connect.assert_not_called()

    def test_invalid_action_replies_print_one_line_and_fail(self):
        connection = self.reply()
        read = connection.getresponse.return_value.read
        for args, reply in zip(ACTION_ARGS, (
            {"app": "Editor", "title": "Example", "control": {"name": "Open", "role": None}},
            {"app": "Editor", "title": "Example", "control": {"name": "Open", "role": "button"}, "amount": True},
            {"windows": [{"n": 1, "app": "Editor", "title": "Example", "front": False, "access": "drivable"},
                         {"n": 2, "app": "Editor", "title": "Example", "front": 1, "access": "see_only"}]},
            {"app": "Editor", "title": []},
        ), strict=True):
            with self.subTest(args=args):
                read.return_value = json.dumps(reply).encode()
                result, lines = self.run_cli(*args, "--code", "fake-code")
                self.assertEqual(result, 1)
                self.assertEqual(len(lines), 1)

    def test_empty_window_list(self):
        self.reply({"windows": []})
        self.assertEqual(self.run_cli("windows", "--code", "fake-code"), (0, ["No windows are available."]))

    def test_guide_prints_the_exact_file_without_daemon_or_code(self):
        guide = Path(__file__).resolve().parents[2] / "jarvis" / "v3" / "prompts" / "computer_guide.md"
        parsed = self.parser.parse_args(["computer", "guide"])
        with patch.object(cli, "HTTPConnection") as connect, redirect_stdout(StringIO()) as output:
            self.assertEqual(parsed.func(parsed), 0)
            connect.assert_not_called()
        self.assertEqual(output.getvalue().encode("utf-8"), guide.read_bytes())


if __name__ == "__main__":
    unittest.main()
