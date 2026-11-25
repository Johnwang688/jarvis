"""PH1 lifecycle fault isolation and effective unit environment."""

from pathlib import Path
import socket
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3 import switch
from jarvis.v3.daemon import Daemon
from phone_listener_check import HOST


class PhoneWiringCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, "remote_dir", return_value=self.root / "remote"))

    def test_phone_save_failure_still_stops_window(self):
        daemon = Daemon(port=0, phone_host=HOST, data_dir=self.root / "data",
                        hud_dir=self.root / "hud", jobs=())
        self.addCleanup(daemon.stop)
        daemon.start()
        window_address = daemon.address
        with patch.object(daemon._phone_server.auth, "final_save", side_effect=OSError("fake disk error")):
            try:
                daemon.stop()
            except OSError:
                pass
        self.assertIsNone(daemon.phone_port)
        with socket.socket() as window:
            window.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                window.bind(window_address)
            except OSError:
                self.fail("The phone save failure left the window listener bound")



if __name__ == "__main__":
    unittest.main()
