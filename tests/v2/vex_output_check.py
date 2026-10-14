"""VEX's unattached Windows startup handshake, output and cleanup regression.

The fake make models the Windows PTY query. No VEX tool or device is used.
"""
from __future__ import annotations
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from jarvis.v2 import terminals as T, vex
from tests.v2.vex_check import DaemonBase, eventually


class StartupProtocol(unittest.TestCase):
    def terminal(self, **options):
        term = T.Terminal("startup-test", shell="/bin/true", folder="/tmp",
                          project_id=None, label="test", cols=80, rows=24, nonce="test",
                          **options)
        self.addCleanup(term.finish)
        return term

    def test_split_initial_query_is_answered_once_and_hidden_from_viewer(self):
        # Every possible partition, including one byte per read.
        query = b"\x1b[6n"
        for mask in range(8):
            term = self.terminal(program="vex-build", input_ok=False)
            chunks, start = [], 0
            for i in range(1, 4):
                if mask & (1 << (i - 1)):
                    chunks.append(query[start:i]); start = i
            chunks.append(query[start:])
            with patch.object(term, "queue_input") as input_:
                for chunk in chunks:
                    term._output(chunk)
                    self.assertLessEqual(len(term._build_startup or b""), 3)
                term._output(b"visible\r\n\x1b[6n")
                input_.assert_called_once_with(b"\x1b[1;1R")
            self.assertEqual(term.history().data, b"visible\r\n\x1b[6n")

    def test_query_and_output_in_one_frame_preserve_tail_and_later_query(self):
        term = self.terminal(program="vex-build", input_ok=False)
        viewer = unittest.mock.Mock()
        viewer.send_binary.return_value = True
        term._attachment = viewer
        data = b"stdout\r\nstderr\r\n\x1b[6n"
        with patch.object(term, "queue_input") as input_:
            term._output(b"\x1b[6n" + data)
            input_.assert_called_once_with(b"\x1b[1;1R")
        self.assertEqual(term.history().data, data)
        viewer.send_binary.assert_called_once_with(data)

    def test_only_initial_exact_query_in_input_disabled_build_is_answered(self):
        cases = [("vex-build", False, b"banner\x1b[6n"),
                 ("vex-build", False, b"\x1b[?6n"),
                 ("vex-build", False, b"\x1b[6x\x1b[6n"),
                 ("vex-build", True, b"\x1b[6n"),
                 ("vex-brain", False, b"\x1b[6n"),
                 (None, False, b"\x1b[6n")]
        for program, input_ok, data in cases:
            with self.subTest(program=program, input_ok=input_ok, data=data):
                term = self.terminal(program=program, input_ok=input_ok)
                with patch.object(term, "queue_input") as input_:
                    for byte in data:
                        term._output(bytes([byte]))
                    input_.assert_not_called()
                self.assertEqual(term.history().data, data)

    def test_every_input_disabled_vex_program_is_answered_but_not_the_brain(self):
        # The download, USB check and device read hung on the live machine
        # (2026-10-14) because only vex-build was answered.
        self.assertEqual(T.STARTUP_REPLY_PROGRAMS,
                         {"vex-build", "vex-download", "vex-connection", "vex-info"})
        for program in sorted(T.STARTUP_REPLY_PROGRAMS):
            with self.subTest(program=program):
                term = self.terminal(program=program, input_ok=False)
                with patch.object(term, "queue_input") as input_:
                    term._output(b"\x1b[6n{}\r\n")
                    input_.assert_called_once_with(b"\x1b[1;1R")
                self.assertEqual(term.history().data, b"{}\r\n")
        for program in ("vex-download", "vex-connection", "vex-info"):
            with self.subTest(program=program, input_ok=True):
                term = self.terminal(program=program, input_ok=True)
                with patch.object(term, "queue_input") as input_:
                    term._output(b"\x1b[6n")
                    input_.assert_not_called()

    def test_eof_preserves_partial_prefix_before_exit_hook(self):
        for prefix in (b"\x1b", b"\x1b[", b"\x1b[6"):
            term = self.terminal(program="vex-build", input_ok=False)
            ended = []
            term.on_exit = lambda t: ended.append(t.history().data)
            with tempfile.TemporaryDirectory() as folder:
                term.folder = folder
                term.spawn(["/usr/bin/python3", "-I", "-c",
                            f"import os;os.write(1,{prefix!r})"],
                           T.program_environment(env_file=Path(folder)/"absent.env"))
                eventually(lambda: ended, timeout=3, what="EOF hook")
            self.assertEqual(ended, [prefix])
            self.assertEqual(term.exit_code, 0)


class StartupOutput(DaemonBase):
    def setUp(self):
        super().setUp()
        (self.folder / "STARTUP_DSR").write_text("")

    def test_clean_and_build_show_both_streams_live_and_end_without_timeout(self):
        for action in ("build", "clean", "build"):
            hold = self.folder / "HOLD"
            # Clean exits immediately; builds stay live until output is observed.
            if action != "clean":
                hold.write_text("")
            started = self.build(action, wait=False)
            path = f"/vex/builds/{started['build_id']}"
            record = eventually(lambda: (lambda r: r if "WINDOWS-STARTUP-STDERR" in r["output"] else None)(
                                self.owner("GET", path)),
                                timeout=4, what="Windows startup output")
            self.assertIn("WINDOWS-STARTUP-STDOUT", record["output"])
            if action != "clean":
                self.assertEqual(record["state"], "running")
                hold.unlink()
            record = eventually(lambda: (lambda r: r if r["state"] != "running" else None)(
                                self.owner("GET", path)), timeout=4)
            self.assertEqual((record["state"], record["exit_code"]), ("succeeded", 0))
            term = self.daemon.terminals.get(started["terminal_id"])
            self.assertNotIn(b"\x1b[6n", term.history().data)
            self.assertFalse(term.alive())

    def test_nonzero_exit_releases_build_lock_and_retains_output(self):
        failure = self.folder / "src" / "FAIL"
        failure.write_text("src/main.cpp:1:1: error: synthetic failure\n")
        result = self.build()
        self.assertEqual((result["state"], result["exit_code"]), ("failed", 2))
        self.assertIn("WINDOWS-STARTUP-STDERR", result["output"])
        self.assertIn("synthetic failure", result["output"])
        failure.unlink()
        self.assertEqual(self.build()["state"], "succeeded")

    def test_timeout_and_close_still_kill_started_program_and_release_lock(self):
        slow = self.folder / "src" / "SLOW"
        slow.write_text("")
        with patch.object(vex, "BUILD_TIMEOUT_S", 0.5), patch.object(vex, "KILL_GRACE_S", 0.1):
            result = self.build()
        self.assertEqual(result["state"], "timed_out")
        self.assertIn("WINDOWS-STARTUP-STDOUT", result["output"])
        self.assertFalse(self.daemon.terminals.get(result["terminal_id"]).alive())
        slow.unlink()
        (self.folder / "HOLD").write_text("")
        started = self.build(wait=False)
        term = self.daemon.terminals.get(started["terminal_id"])
        eventually(lambda: b"WINDOWS-STARTUP-STDOUT" in term.history().data)
        self.owner("DELETE", f"/terminals/{term.id}")
        self.assertFalse(term.alive())
        (self.folder / "HOLD").unlink()
        self.assertEqual(self.build()["state"], "succeeded")


class UsbConsoleOutput(unittest.TestCase):
    # The shape the live console wrote around `vexcom --usb` under a sized
    # PTY after the reply (2026-10-14); the path is a stand-in.
    LIVE = (b'\x1b[?9001h\x1b[?1004h\x1b[m\x1b]0;C:\\fake\\vexcom.exe\x07\x1b[?25h{\r\n'
            b'\x1b[?9001l\x1b[?1004l  "usb": {\r\n    "dfu": [\r\n    ],\r\n    "vex": [\r\n'
            b'      {"vid":"0x2888", "pid":"0x0503", "family":"v5", "device":"ctrl"}\r\n'
            b'    ]\r\n  }\r\n}\r\n')

    def test_console_sequences_around_the_json_are_ignored(self):
        from jarvis.v2.vex_connection import usb_presence
        self.assertEqual(usb_presence(self.LIVE), (False, True))

    def test_anything_else_beside_the_json_still_refuses(self):
        from jarvis.v2.vex_connection import usb_presence
        for extra in (b"banner\r\n", b"\x1b[?25h(\r\n", b"\xff"):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                usb_presence(extra + self.LIVE)


class DeviceStartup(DaemonBase):
    """The fake vexcom waits for interop's cursor reply, as the real one does
    under a sized PTY, and exits 91 without it."""

    def setUp(self):
        super().setUp()
        (self.world.vexcom_dir / "STARTUP_DSR").write_text("")

    def test_usb_check_completes(self):
        self.owner("POST", "/vex/device/connection", {})
        ready = eventually(lambda: r if (r := self.owner("POST", "/vex/device/connection", {}))[
            "state"] in ("ready", "error") else None, timeout=4, what="USB check")
        self.assertEqual(ready["state"], "ready", ready["message"])
        self.assertTrue(ready["brain"])

    def test_download_completes_without_the_query_in_its_output(self):
        self.build()
        sent = self.owner("POST", f"/projects/{self.project.id}/vex/download",
                          {"slot": 3, "name": "fixture"}, status=202)
        done = eventually(lambda: r if (r := self.owner("GET", f"/vex/downloads/{sent['download_id']}"))[
            "state"] != "running" else None, timeout=4, what="download")
        self.assertEqual((done["state"], done["exit_code"]), ("succeeded", 0))
        self.assertIn("Finished", done["output"])
        term = self.daemon.terminals.get(sent["terminal_id"])
        self.assertNotIn(b"\x1b[6n", term.history().data)

    def test_device_reads_complete(self):
        for read, expect in (("system", '"vexos"'), ("programs", "override")):
            with self.subTest(read=read):
                answer = self.owner("POST", "/vex/device/info", {"read": read})
                self.assertEqual(answer["exit_code"], 0)
                self.assertIn(expect, answer["text"])


if __name__ == "__main__":
    unittest.main()
