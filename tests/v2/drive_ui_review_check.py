"""Independent CU-3c checks. Native callbacks use fakes and wire I/O stays on loopback."""

import ast
import ctypes
from itertools import product
from pathlib import Path
import socket
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

_path_exists = Path.exists
with patch.object(Path, "exists", lambda path: path.name != ".env" and _path_exists(path)):
    from drive_ui_check import WireChecks
    from jarvis.v2 import voice_link
from tests.checklib import wait_for
from windows import driveui, voiceui, voicewire as wire


class PureRules(unittest.TestCase):
    def test_module_imports_only_platform_independent_types(self):
        tree = ast.parse(Path(driveui.__file__).read_text())
        imports = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertEqual(imports, {"typing"})

    def test_visibility_keeps_existing_voice_frame_and_adds_one_driving_frame(self):
        for driving, connected, voice in product((False, True), repeat=3):
            self.assertEqual(driveui.visibility(driving, connected, voice),
                             (connected and (voice or driving), connected and driving))

    def test_layout_and_stop_hit_test_across_small_and_offset_screens(self):
        for (left, top), width, height in product(
            ((0, 0), (-1920, -1080), (1400, -55)),
            (1, 2, 3, 9, 18, 303, 304, 305, 1920), (1, 2, 8, 33, 34, 35, 50, 1080),
        ):
            with self.subTest(left=left, top=top, width=width, height=height):
                badge, stop = driveui.badge_layout(driveui.Rect(left, top, left + width, top + height))
                self.assertTrue(left <= badge.left <= stop.left < stop.right <= badge.right <= left + width)
                self.assertTrue(top <= badge.top <= stop.top < stop.bottom <= badge.bottom <= top + height)
                self.assertLessEqual(abs(badge.left + badge.right - 2 * left - width), 1)
                self.assertLessEqual(badge.top - top, 16)
                self.assertTrue(driveui.hits_stop(stop, stop.left, stop.top))
                self.assertTrue(driveui.hits_stop(stop, stop.right - 1, stop.bottom - 1))
                self.assertFalse(driveui.hits_stop(stop, stop.right, stop.top))
                self.assertFalse(driveui.hits_stop(stop, stop.left, stop.bottom))


class CallbackChecks(unittest.TestCase):
    def setUp(self):
        self.stop = Mock()
        self.api = Mock()
        self.api.primary_screen.return_value = driveui.Rect(-320, -50, 1600, 1030)
        self.api.window.side_effect = [101, 102]
        self.api.user.SetWindowsHookExW.return_value = 103
        self.api.user.UnhookWindowsHookEx.return_value = True
        self.api.user.CallNextHookEx.return_value = 47
        self.badge = voiceui.DriveBadge(self.stop)
        self.enterContext(patch.object(voiceui, "os", SimpleNamespace(name="nt")))
        self.enterContext(patch.object(voiceui, "_DriveWin32", return_value=self.api))
        self.addCleanup(self.badge.close)

    def key_event(self, code, message, key, flags):
        event = voiceui._KeyEvent(key=key, flags=flags)
        address = ctypes.addressof(event)
        result = self.badge._key_event(code, message, address)
        self.api.user.CallNextHookEx.assert_called_once_with(None, code, message, address)
        self.assertEqual(result, 47)

    def test_only_physical_escape_down_stops_and_every_key_is_forwarded(self):
        self.assertTrue(self.badge.set_visible(True))
        for code, message, key, flags in product(
            (-1, 0, 1), (0x100, 0x101, 0x104, 0x105), (0x1B, 0x41), (0, 2, 16, 18, 32),
        ):
            with self.subTest(code=code, message=message, key=key, flags=flags):
                self.api.user.CallNextHookEx.reset_mock()
                self.stop.reset_mock()
                self.key_event(code, message, key, flags)
                if code == 0 and message in (0x100, 0x104) and key == 0x1B and flags & 18 == 0:
                    self.stop.assert_called_once_with("esc")
                else:
                    self.stop.assert_not_called()

    def test_escape_is_forwarded_when_the_stop_callback_raises(self):
        self.assertTrue(self.badge.set_visible(True))
        self.stop.side_effect = RuntimeError("fixture stop callback failed")
        try:
            self.key_event(0, 0x100, 0x1B, 0)
        finally:
            self.api.user.CallNextHookEx.assert_called_once()

    def test_hook_install_failure_paints_escape_unavailable_and_keeps_stop(self):
        self.api.user.SetWindowsHookExW.return_value = None
        self.assertTrue(self.badge.set_visible(True))
        self.assertEqual(self.badge._window_event(101, 0xF, 0, 0), 0)
        self.assertIn("Esc unavailable", self.api.paint.call_args.args[3])
        self.assertEqual(self.badge._window_event(102, 0x21, 0, 0), 3)
        self.badge._window_event(102, 0x201, 0, 0xFFFF)
        self.stop.assert_not_called()
        self.badge._window_event(102, 0x201, 0, 0)
        self.stop.assert_called_once_with("badge")
        self.badge.set_visible(False)
        self.api.user.UnhookWindowsHookEx.assert_not_called()

    def test_hide_removes_hook_and_both_windows(self):
        self.assertTrue(self.badge.set_visible(True))
        self.assertFalse(self.badge.set_visible(False))
        self.api.user.UnhookWindowsHookEx.assert_called_once_with(103)
        self.assertEqual([call.args for call in self.api.user.DestroyWindow.call_args_list], [(102,), (101,)])
        self.key_event(0, 0x100, 0x1B, 0)
        self.stop.assert_not_called()

    def test_failed_unhook_is_retried_on_close(self):
        self.api.user.UnhookWindowsHookEx.side_effect = [False, True]
        self.assertTrue(self.badge.set_visible(True))
        self.badge.set_visible(False)
        self.badge.close()
        self.assertEqual([call.args for call in self.api.user.UnhookWindowsHookEx.call_args_list],
                         [(103,), (103,)])


class WireReview(WireChecks):
    def relay(self, parent):
        relay = wire.DesktopRelay(parent)
        self.addCleanup(relay.thread.join, 2)
        self.addCleanup(relay.close)
        relay.thread.start()
        wait_for(relay.indicator, "initial desktop indicator", timeout=2)
        relay.badge_shown(relay.indicator())
        self.wait_badge(True)
        return relay

    def test_indicator_clears_within_one_second_on_grant_end_or_side_socket_loss(self):
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        parent = self.paired()
        relay = self.relay(parent)
        self.d.set_desktop_driving(chat.id, False)
        wait_for(lambda: relay.indicator() is None, "indicator after grant end", timeout=1)
        self.wait_badge(False)
        self.d.set_desktop_driving(chat.id, True)
        wait_for(relay.indicator, "indicator on second grant", timeout=2)
        relay.badge_shown(relay.indicator())
        self.wait_badge(True)
        with self.link._lock:
            self.link._desktop.shutdown(socket.SHUT_RDWR)
        wait_for(lambda: relay.indicator() is None, "indicator after socket loss", timeout=1)
        self.wait_badge(False)

    def test_stop_from_previous_grant_cannot_stop_its_successor(self):
        first, second = self.chat(), self.chat()
        self.d.set_desktop_driving(first.id, True)
        parent = self.paired()
        peer = self.desktop(parent)
        previous = wire.read_json(peer._stream, 256)
        wire.send_json(peer._stream, {"op": "desktop_badge", "shown": True})
        self.wait_badge(True)
        self.assertEqual(wire.read_json(peer._stream, 256), previous)
        self.d.set_desktop_driving(first.id, False)
        self.d.set_desktop_driving(second.id, True)
        # The helper responds to the heartbeat it received for the visible first grant.
        wire.send_json(peer._stream, {"op": "desktop_stop", "reason": "badge"})
        wire.read_json(peer._stream, 256)
        self.assertEqual(self.hud()["thread_id"], second.id)

    def test_acknowledgement_for_old_revision_cannot_confirm_new_grant(self):
        first, second = self.chat(), self.chat()
        self.d.set_desktop_driving(first.id, True)
        peer = self.desktop(self.paired())
        previous = wire.read_json(peer._stream, 256)
        self.d.set_desktop_driving(first.id, False)
        self.d.set_desktop_driving(second.id, True)
        old_receipt = {"op": "desktop_badge", "shown": True, "revision": previous["revision"]}
        wire.send_json(peer._stream, old_receipt)
        current = wire.read_json(peer._stream, 256)
        self.assertGreater(current["revision"], previous["revision"])
        self.assertFalse(self.hud()["badge_showing"])
        # Replay the explicit old revision in the next response slot.
        wire.send_json(peer._stream, old_receipt)
        wire.read_json(peer._stream, 256)
        self.assertFalse(self.hud()["badge_showing"])

    def test_desktop_connection_refuses_old_sessions_and_voice_operations(self):
        old_parent = self.paired()
        parent = self.paired()
        old_peer = self.desktop(old_parent)
        self.assertEqual(old_peer._stream.readline(), b"")
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        peer = self.desktop(parent)
        self.assertTrue(wire.read_json(peer._stream, 256)["driving"])
        with patch.object(self.link, "_dictate") as dictate:
            wire.send_json(peer._stream, {"op": "dictate", "id": 1, "thread_id": chat.id})
            self.assertEqual(peer._stream.readline(), b"")
            dictate.assert_not_called()
        self.assertEqual(self.hud()["thread_id"], chat.id)
        parent.ping()

    def test_badge_callback_stops_via_side_wire_during_active_dictation(self):
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        parent = self.paired()
        relay = self.relay(parent)
        badge = voiceui.DriveBadge(relay.request_stop)
        badge.layout = driveui.badge_layout(driveui.Rect(0, 0, 1920, 1080))
        badge.button = 102
        entered, release = Event(), Event()

        def dictate(_audio, *, verify):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("fixture dictation was never released")
            return {"text": "fixture", "backend": "local"}

        self.enterContext(patch.object(self.link, "_dictate", side_effect=dictate))
        worker = Thread(target=parent.dictate, args=(wire.wav_bytes(b"\x01\x00" * 8000),), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(self.d, "stop_desktop_driving", wraps=self.d.stop_desktop_driving) as stop:
                self.assertEqual(badge._window_event(badge.button, 0x201, 0, 0), 0)
                wait_for(lambda: self.hud()["thread_id"] is None, "badge stop during dictation", timeout=1)
                stop.assert_called_once_with("badge")
                self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        parent.ping()

    def test_primary_wire_requires_integer_id_and_refuses_desktop_start_or_switch(self):
        first, second = self.chat(), self.chat()
        parent = self.paired()
        with self.link._lock:
            self.assertIsNone(self.link._desktop)
        with patch.object(self.d, "stop_desktop_driving", wraps=self.d.stop_desktop_driving) as stop:
            self.d.set_desktop_driving(first.id, True)
            for op, ident in (("desktop_stop", True), ("desktop_start", 21), ("set_desktop_driving", 22)):
                wire.send_json(parent._stream, {"op": op, "id": ident, "thread_id": second.id, "on": True})
                reply = wire.read_json(parent._stream)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], voice_link.BAD_REQUEST)
                self.assertEqual(self.hud()["thread_id"], first.id)
            stop.assert_not_called()
        parent.ping()

    def test_stop_reason_is_fixed_regardless_of_message_fields(self):
        chat = self.chat()
        self.d.set_desktop_driving(chat.id, True)
        peer = self.desktop(self.paired())
        wire.read_json(peer._stream, 256)
        with patch.object(self.d, "stop_desktop_driving") as stop:
            wire.send_json(peer._stream, {"op": "desktop_stop"})
            wire.read_json(peer._stream, 256)
            wire.send_json(peer._stream, {"op": "desktop_stop", "reason": "esc", "thread_id": "other"})
            wire.read_json(peer._stream, 256)
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(stop.call_args_list[0], stop.call_args_list[1])

    def test_stopped_voice_instance_cannot_stop_driving_over_its_retired_primary_wire(self):
        chat = self.chat()
        parent = self.paired()
        instance = wire.new_nonce()
        parent.ping(instance=instance)
        with self.link._lock:
            self.assertEqual(self.link._instance, instance)
            self.link._stopped_instance = instance
        self.d.set_desktop_driving(chat.id, True)
        wire.send_json(parent._stream, {"op": "desktop_stop", "id": 51})
        self.assertEqual((parent._stream.readline(), self.hud()["thread_id"]), (b"", chat.id))


if __name__ == "__main__":
    # Inherit the fixture helpers and run its original tests separately.
    suite = unittest.TestSuite()
    for case in (PureRules, CallbackChecks, WireReview):
        names = unittest.defaultTestLoader.getTestCaseNames(case)
        suite.addTests(case(name) for name in names if name in case.__dict__)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
