import threading
import unittest
from unittest.mock import Mock, patch

from voice_helper_check import FakeDevices, FakePopup
from windows import v3voice_main as main
from windows.v3voice_client import wire


class ClickDelivery(unittest.TestCase):
    def test_click_waiting_in_helper_queue_expires_before_send(self):
        for delivery in (109.9, 110.1):
            with self.subTest(delivery=delivery):
                clock = Mock(return_value=100.0)
                transport = Mock()
                helper = main.Helper("127.0.0.1", 12345, None, logger=Mock(),
                                     devices=FakeDevices(), make_canceller=lambda: None,
                                     make_client=Mock(return_value=transport),
                                     make_key=Mock(), make_popup=FakePopup)
                stopped = threading.Event()

                def end_pump(_):
                    stopped.set()

                helper._connected = True
                with patch.object(main.time, "monotonic", clock), \
                        patch.object(helper._capture, "get", side_effect=end_pump):
                    helper._event({"kind": "asking", "id": "card", "digest": "fake-digest",
                                   "sentence": "Example approval", "expires_in": 10})
                    clock.return_value = 109.8
                    helper._popup.click("approve")
                    clock.return_value = delivery
                    helper._capture_loop(stopped)

                if delivery < 110:
                    transport.send.assert_called_once_with(
                        wire.CLICK, {"id": "card", "digest": "fake-digest", "decision": "approve"})
                else:
                    transport.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
