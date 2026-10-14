"""Fake-only actual daemon/PTY Download and strict USB presence regressions."""
import json
import threading
import time
import unittest
from unittest.mock import patch
from tests.v2.vex_check import DaemonBase, eventually, vex
from jarvis.v2.vex_connection import usb_presence


class USBParser(unittest.TestCase):
    def test_known_complete_snapshot_and_empty_lists(self):
        self.assertEqual(usb_presence(b'{"usb":{"vex":[],"dfu":[]}}'), (False, False))
        data = {"usb": {"vex": [{"vid": "0x2888", "pid": "0503"}, {"vid": "2888", "pid": "0501"}], "dfu": []}}
        self.assertEqual(usb_presence(json.dumps(data).encode()), (True, True))

    def test_every_unknown_or_incomplete_identity_refuses_entire_snapshot(self):
        for raw in [b'{}', b'{"usb":{"vex":[]}}', b'{"usb":{"vex":[],"dfu":[{}]}}',
                    b'{"usb":{"vex":[{"vid":"2888","pid":"ffff"}],"dfu":[]}}',
                    b'{"usb":{"vex":[],"dfu":[],"vex":[]}}', b'[]', b'{', b'x' * 65537]:
            with self.subTest(raw=raw[:80]), self.assertRaises((ValueError, UnicodeError)):
                usb_presence(raw)


class Feedback(DaemonBase):
    def download(self):
        return self.owner("POST", f"/projects/{self.project.id}/vex/download", {"slot": 3, "name": "fixture"}, status=202)

    def test_actual_download_live_output_then_final_record_survives_terminal_close(self):
        self.build()
        hold = self.world.vexcom_dir / "HOLD"
        hold.write_text("")
        sent = self.download()
        read = lambda: self.owner("GET", f"/vex/downloads/{sent['download_id']}")
        live = eventually(lambda: (r if "Downloading" in r["output"] else None) if (r := read()) else None)
        self.assertEqual(live["state"], "running")
        self.assertIsNone(live["exit_code"])
        hold.unlink()
        done = eventually(lambda: (r if r["state"] != "running" else None) if (r := read()) else None)
        self.assertEqual((done["state"], done["exit_code"]), ("succeeded", 0))
        self.assertIn("Finished", done["output"])
        self.owner("DELETE", f"/terminals/{sent['terminal_id']}")
        self.assertEqual(read(), done)
        self.assertIsNone(self.daemon.vex._device)
        self.assertFalse(self.daemon.vex._held)

    def test_download_timeout_is_final_failure_not_success(self):
        self.build()
        (self.world.vexcom_dir / "HOLD").write_text("")
        with patch.object(vex, "DOWNLOAD_TIMEOUT_S", .2), patch.object(vex, "KILL_GRACE_S", .1):
            sent = self.download()
            done = eventually(lambda: r if (r := self.owner("GET", f"/vex/downloads/{sent['download_id']}"))["state"] != "running" else None)
        self.assertEqual(done["state"], "timed_out")
        self.assertIsNotNone(done["ended"])
        eventually(lambda: self.daemon.vex._device is None)
        self.assertFalse(self.daemon.vex._held)

    def check_usb(self):
        return self.owner("POST", "/vex/device/connection", {})

    def force_next(self):
        c = self.daemon.vex.connection
        with c.lock:
            c.next_at = 0

    def test_usb_coalesces_and_busy_unknown_never_disconnect(self):
        c = self.daemon.vex.connection
        for _ in range(10): self.check_usb()
        ready = eventually(lambda: r if (r := self.check_usb())["state"] == "ready" else None)
        self.assertTrue(ready["brain"])
        self.assertFalse(ready["controller"])
        self.assertEqual(len(self.world.calls("vexcom")), 1)
        self.assertEqual(self.world.calls("vexcom")[0]["argv"], ["--usb"])
        self.assertNotIn("FAKE-NO-DEVICE", json.dumps(ready))
        brain = self.owner("POST", "/vex/brain", {})["terminal_id"]
        self.force_next()
        busy = self.check_usb()
        self.assertEqual(busy["state"], "busy")
        self.assertTrue(busy["brain"])
        self.assertEqual(busy["events"], ready["events"])
        self.owner("DELETE", f"/terminals/{brain}")
        eventually(lambda: self.daemon.vex._device is None)
        (self.world.vexcom_dir / "USB_RESPONSE").write_text('{"usb":{"vex":[],"dfu":[{}]}}')
        self.force_next(); self.check_usb()
        failed = eventually(lambda: r if (r := self.check_usb())["state"] == "error" else None)
        self.assertTrue(failed["brain"])
        self.assertEqual(failed["checked_at"], ready["checked_at"])
        self.assertEqual(failed["events"], ready["events"])
        (self.world.vexcom_dir / "USB_RESPONSE").write_text('{"usb":{"vex":[],"dfu":[]}}')
        self.force_next(); self.check_usb()
        absent = eventually(lambda: r if (r := self.check_usb())["state"] == "ready" else None)
        self.assertFalse(absent["brain"])
        self.assertEqual(absent["events"][-1]["device"], "brain")
        self.assertFalse(absent["events"][-1]["connected"])

    def test_probe_timeout_cannot_release_before_actual_pty_settlement(self):
        host = self.daemon.vex
        (self.world.vexcom_dir / "HOLD_USB").write_text("")
        stopping, permit = threading.Event(), threading.Event()
        real_stop = host._stop_program
        def held_stop(terminal):
            stopping.set()
            if not permit.wait(3): raise AssertionError("test cleanup barrier timed out")
            real_stop(terminal)
        from jarvis.v2 import vex_connection
        with patch.object(vex_connection, "PROBE_TIMEOUT", .1), patch.object(host, "_stop_program", held_stop):
            self.check_usb()
            self.assertTrue(stopping.wait(2))
            self.assertEqual(host._device["kind"], "connection")
            self.assertTrue(host.connection.running)
            # A probe demand cannot spawn a second native child while the first settles.
            self.check_usb()
            self.assertEqual(len(self.world.calls("vexcom")), 1)
            permit.set()
            eventually(lambda: host._device is None)
            eventually(lambda: not host.connection.running)
            self.assertFalse(any(t["program"] == "vex-connection" for t in self.owner("GET", "/terminals")))
        self.assertEqual(self.check_usb()["state"], "error")

    def test_thread_start_failure_releases_exact_unused_holder(self):
        with patch("jarvis.v2.vex_connection.threading.Thread.start", side_effect=RuntimeError("no worker")):
            result = self.daemon.vex.connection.want()
        self.assertEqual(result["state"], "error")
        self.assertIsNone(self.daemon.vex._device)
        self.assertFalse(self.daemon.vex.connection.running)

    def test_connection_reservation_atomically_defers_to_waiting_owner(self):
        host = self.daemon.vex
        with host._lock:
            host._device_waiters = 1
        try:
            with self.assertRaises(vex.VexError): host._reserve_device("connection")
            self.assertIsNone(host._device)
        finally:
            with host._lock: host._device_waiters = 0

    def test_new_routes_are_owner_only_and_refuse_arguments(self):
        for method, route, body in [("POST", "/vex/device/connection", {}), ("GET", "/vex/downloads/deadbeef", None)]:
            self.request(method, route, body, origin="http://evil.example", status=403)
        self.owner("POST", "/vex/device/connection", {"args": []}, status=400)
        self.assertEqual(self.world.calls("vexcom"), [])


if __name__ == "__main__": unittest.main()
