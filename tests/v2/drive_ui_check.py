"""CU-3c pure indicator rules and authenticated voice-link integration."""
from itertools import product
import os
import socket
from threading import Event, Thread
import unittest
from unittest.mock import patch

import desktop_driver_check as desktop_fixture
from jarvis import config
from tests.checklib import wait_for
from windows import driveui, voicewire as wire

KEY = bytes(range(32))


class DisplayRules(unittest.TestCase):
    def test_physical_escape_only_on_noninjected_key_down(self):
        for down, key, flags in product(
            (False, True), (0x1B, 0x41, 0x11), (0, 0x20, 0x10, 0x02, 0x12, 0x30, 0x22),
        ):
            with self.subTest(down=down, key=key, flags=flags):
                self.assertEqual(driveui.physical_escape(key, flags, down),
                                 down and key == 0x1B and flags in (0, 0x20))

    def test_visibility(self):
        cases = {
            (False, False, False): (False, False),
            (False, False, True): (False, False),
            (True, False, False): (False, False),
            (True, False, True): (False, False),
            (False, True, False): (False, False),
            (False, True, True): (True, False),
            (True, True, False): (True, True),
            (True, True, True): (True, True),
        }
        for inputs, expected in cases.items():
            with self.subTest(inputs=inputs):
                self.assertEqual(driveui.visibility(*inputs), expected)

    def test_layout_on_offset_and_tiny_screens(self):
        for screen in (driveui.Rect(-1300, 80, -499, 1080), driveui.Rect(0, 0, 1920, 1080),
                       driveui.Rect(0, 0, 1, 1), driveui.Rect(0, 0, 6, 2),
                       driveui.Rect(2, 4, 4, 130), driveui.Rect(2, 4, 400, 5)):
            with self.subTest(screen=screen):
                badge, stop = driveui.badge_layout(screen)
                self.assertLessEqual(abs(badge.left + badge.right - screen.left - screen.right), 1)
                self.assertLessEqual(badge.top - screen.top, 16)
                for outer, inner in ((screen, badge), (badge, stop)):
                    self.assertLessEqual(outer.left, inner.left)
                    self.assertLessEqual(outer.top, inner.top)
                    self.assertLess(inner.left, inner.right)
                    self.assertLess(inner.top, inner.bottom)
                    self.assertLessEqual(inner.right, outer.right)
                    self.assertLessEqual(inner.bottom, outer.bottom)
        for invalid in ((0, 0, 0, 8), (0, 0, 8, 0), (1, 1, 0, 0)):
            self.assertIsNone(driveui.badge_layout(driveui.Rect(*invalid)))

    def test_badge_names_unavailable_escape(self):
        self.assertEqual(driveui.badge_text(True), "Jarvis is driving")
        self.assertEqual(driveui.badge_text(False), "Jarvis is driving    Esc unavailable")

    def test_clicks_on_half_open_stop_rectangle(self):
        badge, stop = driveui.badge_layout(driveui.Rect(0, 0, 1920, 1080))
        x, y = stop.left, stop.top
        right, bottom = stop.right, stop.bottom
        for point in ((x, y), (right - 1, bottom - 1)):
            self.assertTrue(driveui.hits_stop(stop, *point))
        for point in ((x - 1, y), (x, y - 1), (right, y), (x, bottom),
                      (badge.left, badge.top), (0, 0)):
            self.assertFalse(driveui.hits_stop(stop, *point))


class WireChecks(desktop_fixture.fixture.DaemonHarness):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"}))
        self.enterContext(patch.object(config, "VOICE_LINK_PORT", 0))
        super().setUp()
        self.link = self.d.voice_link
        self.assertNotIn(self.link.port, range(8402, 8410))

    def make_daemon(self, stores=None, providers=None):
        key = self.root / "fixture-voice.key"
        key.write_text(KEY.hex() + "\n")
        key.chmod(0o600)
        self.stack.enter_context(patch.object(config, "voice_link_key", return_value=key))
        return desktop_fixture.Checks.make_daemon(self, stores, providers)

    def client(self, key=KEY):
        client = wire.Client("127.0.0.1", self.link.port, key)
        self.addCleanup(client.close)
        return client

    def paired(self):
        client = self.client()
        client.connect()
        client.ping()  # The server has registered the authenticated parent.
        return client

    def desktop(self, parent):
        peer = self.client()
        peer.connect(desktop_session=parent.session)
        return peer

    def hud(self):
        status, body = desktop_fixture.Checks.owner(self, "GET", "/desktop-driving")
        self.assertEqual(status, 200)
        return body

    def wait_badge(self, showing):
        wait_for(lambda: self.d.desktop_driving_state()["badge_showing"] == showing,
                 f"HUD badge state {showing}", timeout=2)
        self.assertEqual(self.hud()["badge_showing"], showing)

    def test_push_on_connect_and_change_and_receipt_updates_hud(self):
        # Set the periodic heartbeat beyond the read deadline to require a push.
        self.enterContext(patch.object(wire, "DESKTOP_HEARTBEAT_S", 10))
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        self.assertEqual(self.hud(), {"enabled": True, "thread_id": chat.id, "badge_showing": False})
        parent = self.paired()
        peer = self.desktop(parent)
        peer._socket.settimeout(1)
        events = self.d.bus.subscribe({"kind": "desktop_driving"})
        self.addCleanup(self.d.bus.unsubscribe, events)
        first = wire.read_json(peer._stream, 256)
        self.assertEqual(first["op"], "desktop_state")
        self.assertIs(first["driving"], True)
        self.assertFalse(self.hud()["badge_showing"])
        wire.send_json(peer._stream, {"op": "desktop_badge", "shown": True})
        self.assertEqual(events.get(timeout=1), {"kind": "desktop_driving"})
        self.assertTrue(self.hud()["badge_showing"])
        self.d.set_desktop_driving(chat.id, False)
        ended = wire.read_json(peer._stream, 256)
        self.assertIs(ended["driving"], False)
        self.assertGreater(ended["revision"], first["revision"])
        wire.send_json(peer._stream, {"op": "desktop_badge", "shown": True})
        self.assertFalse(self.hud()["badge_showing"])
        self.d.set_desktop_driving(chat.id, True)
        started = wire.read_json(peer._stream, 256)
        self.assertIs(started["driving"], True)
        self.assertGreater(started["revision"], ended["revision"])
        self.assertFalse(self.hud()["badge_showing"])

    def test_parent_disconnect_hides_badge_and_reconnect_reads_live_grant(self):
        chat = self.chat()
        parent = self.paired()
        relay = wire.DesktopRelay(parent)
        self.addCleanup(relay.thread.join, 2)
        self.addCleanup(relay.close)
        relay.thread.start()
        self.d.set_desktop_driving(chat.id, True)
        wait_for(relay.indicator, "helper indicator", timeout=2)
        relay.badge_shown(relay.indicator())
        self.wait_badge(True)
        parent.close()
        wait_for(lambda: relay.indicator() is None, "helper hides on wire loss", timeout=1)
        self.wait_badge(False)
        self.assertEqual(self.hud()["thread_id"], chat.id)
        peer = self.desktop(self.paired())
        self.assertIs(wire.read_json(peer._stream, 256)["driving"], True)

    def test_physical_escape_stop_reaches_daemon_while_voice_request_is_busy(self):
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        parent = self.paired()
        relay = wire.DesktopRelay(parent)
        self.addCleanup(relay.thread.join, 2)
        self.addCleanup(relay.close)
        relay.thread.start()
        wait_for(relay.indicator, "helper indicator", timeout=2)
        relay.badge_shown(relay.indicator())
        self.wait_badge(True)
        entered, release = Event(), Event()

        def transcribe(_data, *, verify):
            entered.set()
            release.wait(5)
            return {"text": "fixture", "backend": "local"}

        self.enterContext(patch.object(self.link, "_dictate", side_effect=transcribe))
        worker = Thread(target=parent.dictate, args=(wire.wav_bytes(b"\x01\x00" * 8000),), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(self.d, "stop_desktop_driving", wraps=self.d.stop_desktop_driving) as stop:
                relay.request_stop("esc")
                wait_for(lambda: relay.indicator() is None, "helper hides after Esc", timeout=1)
                self.assertIsNone(self.hud()["thread_id"])
                stop.assert_called_once_with("esc")
                self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(2)

    def test_badge_stops_running_chat_ignoring_extra_fields_and_idle_is_noop(self):
        first, second = self.chat(), self.chat()
        self.d.set_desktop_driving(first.id, True)
        release = desktop_fixture.Checks.hold_turn(self, first)
        parent = self.paired()
        peer = self.desktop(parent)
        self.assertTrue(wire.read_json(peer._stream, 256)["driving"])
        request = {"op": "desktop_stop", "thread_id": second.id, "on": True,
                   "reason": {"untrusted": "esc"}, "unlock": False}
        with patch.object(self.claude, "interrupt", side_effect=lambda _handle: release.set()) as interrupt, \
                patch.object(self.d, "interrupt", wraps=self.d.interrupt) as daemon_interrupt, \
                patch.object(self.d, "stop_desktop_driving", wraps=self.d.stop_desktop_driving) as stop:
            turn_id = self.d._sessions[first.id].turn_id
            wire.send_json(peer._stream, request)
            self.assertFalse(wire.read_json(peer._stream, 256)["driving"])
            self.assertIsNone(self.hud()["thread_id"])
            self.wait_idle(first.id)
            wire.send_json(peer._stream, request)
            self.assertFalse(wire.read_json(peer._stream, 256)["driving"])
            interrupt.assert_called()
            daemon_interrupt.assert_called_once_with(first.id, expected_turn_id=turn_id)
            self.assertEqual([call.args for call in stop.call_args_list], [("badge",), ("badge",)])
            self.assertTrue(all(not call.kwargs for call in stop.call_args_list))

    def test_unauthenticated_or_unbound_sender_cannot_stop(self):
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        wrong_key = self.client(bytes([1]) * 32)
        with self.assertRaises(wire.NotJarvis):
            wrong_key.connect()
        with socket.create_connection(("127.0.0.1", self.link.port), timeout=2) as sock, sock.makefile("rwb") as stream:
            wire.send_json(stream, {"op": "desktop_stop", "id": 1})
            self.assertEqual(stream.readline(), b"")
        peer = self.client()
        peer.connect(desktop_session=wire.new_nonce())
        self.assertEqual(peer._stream.readline(), b"")
        self.assertEqual(self.hud()["thread_id"], chat.id)

    def test_wire_cannot_start_a_grant(self):
        parent = self.paired()
        peer = self.desktop(parent)
        self.assertFalse(wire.read_json(peer._stream, 256)["driving"])
        wire.send_json(peer._stream, {"op": "desktop_start", "on": True})
        self.assertEqual(peer._stream.readline(), b"")
        for op in ("desktop_start", "desktop_driving", "set_desktop_driving"):
            with self.assertRaises(wire.LinkError):
                parent._request({"op": op, "on": True})
        self.assertTrue(parent._request({"op": "desktop_stop", "on": True})["ok"])
        self.assertIsNone(self.hud()["thread_id"])


if __name__ == "__main__":
    unittest.main()
