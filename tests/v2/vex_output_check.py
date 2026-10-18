"""VEX's unattached Windows startup handshake, output and cleanup regression.

The fake make and vexcom model the Windows PTY query. No VEX tool or device
is used. `BrainStartup` is VX-UI-4b (2026-10-17): the brain terminal hung
blank on the owner's machine, and these hold its startup reply, its
`--quiet --user <port>` argv, its output reaching an attached window, typed
input still reaching the program, and its diagnostic record.
"""
from __future__ import annotations
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from jarvis.v2 import terminals as T, vex
from tests.v2.vex_check import DaemonBase, Sockets, eventually


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
                 ("vex-brain", True, b"banner\x1b[6n"),
                 ("vex-brain", True, b"\x1b[?6n"),
                 ("vex-brain", True, b"\x1b[6x\x1b[6n"),
                 ("vex-brain", True, b"\x1b[16n"),
                 ("vex-brain", True, b"\r\n\x1b[6n"),
                 ("vex-other", True, b"\x1b[6n"),
                 (None, True, b"\x1b[6n"),
                 (None, False, b"\x1b[6n")]
        # Each case one byte per read and as one read: a rule that answered
        # the query anywhere in the first read (`query in initial`) passes the
        # first and fails the second (review LOW of #240).
        for program, input_ok, data in cases:
            for split in ("bytes", "whole"):
                with self.subTest(program=program, input_ok=input_ok, data=data, split=split):
                    term = self.terminal(program=program, input_ok=input_ok)
                    with patch.object(term, "queue_input") as input_:
                        for chunk in ([bytes([b]) for b in data] if split == "bytes" else [data]):
                            term._output(chunk)
                        input_.assert_not_called()
                    self.assertEqual(term.history().data, data)

    def test_every_input_disabled_vex_program_is_answered(self):
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

    def test_the_brain_is_answered_its_first_exact_query_once_and_nothing_after(self):
        # VX-UI-4b: the brain terminal hung blank (2026-10-17). It takes input,
        # and is the only such program answered.
        self.assertEqual(T.STARTUP_REPLY_INPUT_PROGRAMS, {"vex-brain"})
        query = b"\x1b[6n"
        for mask in range(8):                     # every split of the query
            with self.subTest(mask=mask):
                term = self.terminal(program="vex-brain", input_ok=True)
                viewer = unittest.mock.Mock()
                viewer.send_binary.return_value = True
                term._attachment = viewer          # an xterm attached already never sees it
                chunks, start = [], 0
                for i in range(1, 4):
                    if mask & (1 << (i - 1)):
                        chunks.append(query[start:i]); start = i
                chunks.append(query[start:])
                with patch.object(term, "queue_input") as input_:
                    for chunk in chunks:
                        term._output(chunk)
                    # A later query and a lookalike mid-stream are the
                    # program's (or the attached xterm's) business, never answered.
                    term._output(b"$RUN\r\n\x1b[6n")
                    term._output(b"$POSE,1\x1b[6n\r\n")
                    term._output(query)
                    input_.assert_called_once_with(b"\x1b[1;1R")
                rest = b"$RUN\r\n\x1b[6n$POSE,1\x1b[6n\r\n\x1b[6n"
                self.assertEqual(term.history().data, rest)
                self.assertEqual(b"".join(c.args[0] for c in viewer.send_binary.call_args_list), rest)
                self.assertTrue(term.startup_replied)
                self.assertEqual(term.head(), rest)
        term = self.terminal(program="vex-brain", input_ok=True)
        with patch.object(term, "queue_input") as input_:
            term._output(b"$RUN\r\n")
            term._output(query)
            input_.assert_not_called()
        self.assertFalse(term.startup_replied)

    def test_the_reply_is_protocol_input_and_the_owners_input_still_flows(self):
        # The reply goes to the writer's queue, never through the socket's
        # input guard; typed input afterwards is queued as before.
        term = self.terminal(program="vex-brain", input_ok=True)
        written = []
        with patch.object(term, "_write", lambda data, keep: written.append(bytes(data))):
            term._writer = T.threading.Thread(target=term._input_loop, daemon=True)
            term._writer.start()
            term._output(b"\x1b[6n")
            self.assertTrue(term.queue_input(b"heading?\r"))
            eventually(lambda: len(written) == 2, timeout=3, what="writes")
        self.assertEqual(written, [b"\x1b[1;1R", b"heading?\r"])
        self.assertTrue(term.input_ok)

    def test_the_head_is_capped_and_kept_for_program_terminals_only(self):
        term = self.terminal(program="vex-brain", input_ok=True)
        term._output(b"\x1b[6n")
        for _ in range(100):
            term._output(b"$POSE," + b"9" * 95 + b"\r\n")
        self.assertEqual(len(term.head()), T.HEAD_CAP)
        self.assertTrue(term.head().startswith(b"$POSE,999"))
        self.assertEqual(term.output_bytes, 100 * 103)
        shell = self.terminal()
        shell._output(b"secret prompt\r\n")
        self.assertEqual(shell.head(), b"")

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


class BrainHelpers(unittest.TestCase):
    def test_download_port_reads_only_an_exact_port_line_near_the_start(self):
        self.assertEqual(vex.download_port("Device: COM3\nvexcom utility V1.0.2b2\n"), "COM3")
        self.assertEqual(vex.download_port("\n  Device:\tCOM12  \nx"), "COM12")
        for text in ("Device: COM0\n", "Device: COM3 & calc\n", "xDevice: COM3\n", "Device: COM3x\n",
                     "Device: \\\\.\\COM3\n", "Device: COM1000\n", "Device: com3\n",
                     "Device: COM\uff13\n", "x" * vex.DOWNLOAD_PORT_SCAN + "\nDevice: COM3\n", ""):
            with self.subTest(text=text):
                self.assertIsNone(vex.download_port(text))

    def test_escaped_spells_out_every_control_and_invisible(self):
        self.assertEqual(vex.escaped(b"\x1b[6nhi \\ \r\n\xe2\x80\xae\x00"),
                         "\\x1b[6nhi \\\\ \\r\\n\\u202e\\x00")

    def test_shape_counts_text_and_never_copies_it(self):
        out = vex.shape(b"\x1b[?9001h\x1b]0;C:\\secret\\vexcom.exe\x07\x1b[?25h$POSE,1,2\r\nbrain> hi\r\n")
        self.assertEqual(out, "\\x1b[?9001h\\x1b]<osc 25>\\x1b[?25h<text 9>\\r\\n<text 9>\\r\\n")
        for word in ("POSE", "secret", "brain", "hi"):
            self.assertNotIn(word, out)
        self.assertLessEqual(len(vex.shape(b"a\r\n" * 1000)), 601)


class BrainStartup(DaemonBase):
    """The brain terminal against the fake vexcom, which (with `STARTUP_DSR`)
    hangs on interop's cursor query until it gets the reply, as the owner's
    blank Brain tab suggests the real one does (2026-10-17)."""

    def setUp(self):
        super().setUp()
        (self.world.vexcom_dir / "STARTUP_DSR").write_text("")

    attach = Sockets.attach
    until = staticmethod(Sockets.until)

    def open_brain(self):
        answer = self.owner("POST", "/vex/brain")
        self.addCleanup(lambda: self.request("DELETE", f"/terminals/{answer['terminal_id']}"))
        return answer, self.daemon.terminals.get(answer["terminal_id"])

    def test_with_no_window_attached_it_starts_and_its_output_waits_in_the_ring(self):
        answer, term = self.open_brain()
        eventually(lambda: b"brain user port open" in term.history().data, timeout=4,
                   what="vexcom past the cursor query with no window attached")
        self.assertNotIn(b"\x1b[6n", term.history().data)
        self.assertTrue(term.alive())
        call = self.world.calls("vexcom")[-1]
        self.assertEqual(call["argv"], ["--quiet", "--user"], "no port known: vexcom picks, as the download")
        self.assertNotIn(b"vexcom utility", term.history().data, "--quiet: no banner")
        self.assertIsNone(answer["port"])
        # A window that attaches later gets it in the replay, with no query in it.
        conn = self.attach(term.id)
        texts, data = self.until(conn, lambda t, b: any(m.get("type") == "replayed" for m in t))
        self.assertIn(b"brain user port open", data)
        self.assertNotIn(b"\x1b[6n", data)

    def test_streamed_telemetry_reaches_the_window_live_and_typing_reaches_the_program(self):
        (self.world.vexcom_dir / "STREAM").write_text("")
        _, term = self.open_brain()
        conn = self.attach(term.id)
        self.until(conn, lambda t, b: any(m.get("type") == "replayed" for m in t))
        # Output after the replay arrives live, line after line.
        _, data = self.until(conn, lambda t, b: b.count(b"$POSE,") >= 10)
        self.assertNotIn(b"\x1b[6n", data)
        self.assertIn(b"$RUN,1,fake", term.history().data)
        conn.send_binary(b"heading?\r")
        _, data = self.until(conn, lambda t, b: b"brain> heading?" in b)
        self.assertGreater(term.history().data.count(b"$POSE,"), 10)

    def test_a_busy_port_ends_the_terminal_frees_the_device_and_is_recorded(self):
        (self.world.vexcom_dir / "PORT_BUSY").write_text("")
        _, term = self.open_brain()
        eventually(lambda: term.exited, timeout=4, what="vexcom exit")
        self.assertEqual(term.exit_code, 3)
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="the device freed")
        record = eventually(lambda: (r := self.owner("GET", "/vex/brain/record")["record"])
                            and r["ended"] and r, what="the record")
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("port is in use", record["head"])
        self.assertTrue(record["startup_replied"])
        (self.world.vexcom_dir / "PORT_BUSY").unlink()
        _, again = self.open_brain()
        eventually(lambda: b"user port open" in again.history().data, timeout=4, what="the next Connect")

    def test_without_the_reply_it_hangs_blank(self):
        # The owner's 2026-10-17 symptom, reproduced: the same fake with the
        # brain left out of the reply prints nothing at all.
        with patch.object(T, "STARTUP_REPLY_INPUT_PROGRAMS", frozenset()):
            _, term = self.open_brain()
            eventually(lambda: b"\x1b[6n" in term.history().data, timeout=4, what="the query")
            time.sleep(0.5)
            self.assertNotIn(b"user port open", term.history().data)
            self.assertTrue(term.alive())

    def test_the_record_says_what_ran_and_what_came_back(self):
        status, _ = self.request("GET", "/vex/brain/record", origin="evil")
        self.assertEqual(status, 403)
        self.assertEqual(self.owner("GET", "/vex/brain/record"), {"record": None})
        _, term = self.open_brain()
        eventually(lambda: b"user port open" in term.history().data, timeout=4, what="output")
        record = self.owner("GET", "/vex/brain/record")["record"]
        self.assertEqual((record["terminal_id"], record["args"], record["port"]),
                         (term.id, ["--quiet", "--user"], None))
        self.assertTrue(record["startup_replied"])
        self.assertIsNone(record["exit_code"])
        self.assertIsNone(record["ended"])
        self.assertGreater(record["bytes"], 0)
        self.assertIsInstance(record["first_output_s"], float)
        self.assertIn("brain user port open\\r\\n", record["head"])
        self.assertNotIn("\x1b", record["head"], "escaped, never raw")
        self.assertNotIn("\\x1b[6n", record["head"], "the answered query is not in the ring")
        self.owner("DELETE", f"/terminals/{term.id}")
        record = eventually(lambda: (r := self.owner("GET", "/vex/brain/record")["record"])
                            and r["ended"] and r, what="ended")
        self.assertTrue(record["exited"])
        json.dumps(record)


class BrainPort(DaemonBase):
    """The controller's port, from the download that used it (VX-UI-4b)."""

    def reading(self, controller, brain):
        with self.daemon.vex.connection.lock:
            self.daemon.vex.connection.value.update(controller=controller, brain=brain)

    def change(self, device, connected, at):
        """A confirmed USB change, as `Connection.run` records one."""
        with self.daemon.vex.connection.lock:
            value = self.daemon.vex.connection.value
            value[device] = connected
            value["events"].append({"device": device, "connected": connected, "at": at})

    def download(self, wait_state="succeeded"):
        self.build()
        sent = self.owner("POST", f"/projects/{self.project.id}/vex/download",
                          {"slot": 3, "name": "fixture"}, status=202)
        done = eventually(lambda: r if (r := self.owner("GET", f"/vex/downloads/{sent['download_id']}"))[
            "state"] != "running" else None, timeout=4, what="download")
        self.assertEqual(done["state"], wait_state)

    def brain_args(self):
        before = len(self.world.calls("vexcom"))
        answer = self.owner("POST", "/vex/brain")
        eventually(lambda: len(self.world.calls("vexcom")) > before, what="vexcom started")
        self.owner("DELETE", f"/terminals/{answer['terminal_id']}")
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="freed")
        return self.world.calls("vexcom")[-1]["argv"], answer["port"]

    def test_a_controller_alone_gets_the_port_its_download_used(self):
        self.reading(True, False)
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None), "no download yet")
        self.download()
        self.assertEqual(self.brain_args(), (["--quiet", "--user", "COM7"], "COM7"))
        # The owner's Disconnect (a close, exit 129) is no failure: still COM7.
        self.assertEqual(self.brain_args(), (["--quiet", "--user", "COM7"], "COM7"))

    def test_a_usb_change_since_the_download_forgets_the_port(self):
        # Review LOW of #240: the controller moved sockets (COM7 → COM8).
        self.change("controller", True, "2026-10-17T10:00:00+00:00")
        self.reading(True, False)
        self.download()
        self.assertEqual(self.brain_args()[1], "COM7")
        self.change("controller", False, "2026-10-17T10:01:00+00:00")
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))
        self.change("controller", True, "2026-10-17T10:01:10+00:00")
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None),
                         "the same reading again is not the same socket")
        self.download()
        self.assertEqual(self.brain_args()[1], "COM7", "a new download teaches it again")

    def test_the_first_reading_after_the_download_forgets_it_too(self):
        self.download()
        self.change("controller", True, "2026-10-17T10:00:00+00:00")
        self.reading(True, False)
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))

    def test_a_brain_terminal_that_fails_by_itself_with_the_port_forgets_it(self):
        self.reading(True, False)
        self.download()
        (self.world.vexcom_dir / "PORT_BUSY").write_text("")
        before = len(self.world.calls("vexcom"))
        answer = self.owner("POST", "/vex/brain")
        term = self.daemon.terminals.get(answer["terminal_id"])
        eventually(lambda: term.exited, timeout=4, what="vexcom exit")
        self.assertEqual((answer["port"], term.exit_code), ("COM7", 3))
        self.assertEqual(self.world.calls("vexcom")[before]["argv"], ["--quiet", "--user", "COM7"])
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="freed")
        (self.world.vexcom_dir / "PORT_BUSY").unlink()
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))

    def test_a_brain_on_usb_or_an_unknown_reading_lets_vexcom_pick(self):
        self.download()
        for controller, brain in ((True, True), (False, True), (None, None), (True, None), (False, False)):
            with self.subTest(controller=controller, brain=brain):
                self.reading(controller, brain)
                self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))

    def test_a_failed_download_or_an_odd_port_line_teaches_nothing(self):
        self.reading(True, False)
        (self.world.vexcom_dir / "FAIL_DOWNLOAD").write_text("")
        self.download("failed")
        self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))
        (self.world.vexcom_dir / "FAIL_DOWNLOAD").unlink()
        for line in ("Device: COM3 & calc", "Device: \\\\.\\COM3", "Device: COM0"):
            with self.subTest(line=line):
                (self.world.vexcom_dir / "DOWNLOAD_PORT").write_text(line)
                self.download()
                self.assertEqual(self.brain_args(), (["--quiet", "--user"], None))
        (self.world.vexcom_dir / "DOWNLOAD_PORT").write_text("Device: COM4")
        self.download()
        self.assertEqual(self.brain_args(), (["--quiet", "--user", "COM4"], "COM4"))


if __name__ == "__main__":
    unittest.main()
