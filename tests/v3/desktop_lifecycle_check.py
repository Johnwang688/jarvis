"""The desktop bridge session across stops, restarts and a second bridge."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from jarvis import desktop
from jarvis.v3.daemon import Daemon
from windows import desktopwire


@contextmanager
def bridge(address, key):
    with socket.create_connection(address, timeout=2) as peer, peer.makefile("rwb") as stream:
        desktopwire.prove_windows(stream, key)
        yield peer, stream


def fake_key(folder):
    path = Path(folder) / "fake-key"
    path.write_bytes(b"ab" * 32)
    path.chmod(0o600)
    return path


class DesktopLifecycleCheck(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="desktop-lifecycle-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.key_path = fake_key(self.folder)
        self.key = desktopwire.private_key(self.key_path)

    def session(self, **options):
        session = desktop.DesktopSession(port=0, key_path=self.key_path, **options)
        self.addCleanup(session.stop)
        session.start()
        return session, ("127.0.0.1", session.bound_port)

    def test_a_request_waiting_across_a_restart_does_not_run(self):
        session, _ = self.session(managed=True)
        waiting, resume = Event(), Event()
        wait = session.wait_for_bridge

        def pause(timeout):
            waiting.set()
            if not resume.wait(3):
                raise AssertionError("the restart did not complete")
            return wait(timeout)

        with ThreadPoolExecutor() as pool, patch.object(session, "wait_for_bridge", pause):
            pending = pool.submit(session.request, "ping", timeout=2)
            try:
                self.assertTrue(waiting.wait(2))
                session.stop()
                session.start()
                with bridge(("127.0.0.1", session.bound_port), self.key) as (peer, stream):
                    self.assertTrue(wait(2))
                    resume.set()
                    with self.assertRaises(desktop.DesktopError):
                        pending.result(timeout=2)
                    # Nothing was sent to the bridge that connected after the restart.
                    peer.settimeout(0.3)
                    with self.assertRaises(TimeoutError):
                        peer.recv(1, socket.MSG_PEEK)
            finally:
                resume.set()

    def test_a_second_bridge_waits_for_the_request_in_flight(self):
        session, address = self.session()
        with ThreadPoolExecutor() as pool, bridge(address, self.key) as (peer, first):
            self.assertTrue(session.wait_for_bridge(2))
            pending = pool.submit(session.request, "ping", timeout=5)
            request = desktopwire.wire.read_json(first)
            with bridge(address, self.key) as (_, second):
                peer.settimeout(0.5)
                with self.assertRaises(TimeoutError):
                    peer.recv(1, socket.MSG_PEEK)
                desktopwire.wire.send_json(first, {"id": request["id"], "ok": True})
                self.assertEqual(pending.result(timeout=2), {})
                # Afterwards the newer bridge is the one in use.
                later = pool.submit(session.request, "ping", timeout=5)
                request = desktopwire.wire.read_json(second)
                desktopwire.wire.send_json(second, {"id": request["id"], "ok": True, "result": {"n": 2}})
                self.assertEqual(later.result(timeout=2), {"n": 2})

    def test_stop_ends_a_request_while_a_second_bridge_waits(self):
        session, address = self.session(managed=True)
        with ThreadPoolExecutor() as pool, bridge(address, self.key) as (_, first):
            self.assertTrue(session.wait_for_bridge(2))
            pending = pool.submit(session.request, "ping", timeout=8)
            desktopwire.wire.read_json(first)
            with bridge(address, self.key):
                pool.submit(session.stop).result(timeout=3)
                with self.assertRaises(desktop.DesktopError):
                    pending.result(timeout=2)
        self.assertIsNone(session.bound_port)
        self.assertFalse(session.connected)

    def test_stop_closes_an_unfinished_handshake(self):
        session, address = self.session(managed=True)
        with socket.create_connection(address, timeout=2) as peer:
            # Connected, but no hello sent: the session is mid-handshake.
            deadline = Event()
            for _ in range(100):
                if session._handshake is not None:
                    break
                deadline.wait(0.01)
            self.assertIsNotNone(session._handshake)
            started = time.monotonic()
            session.stop()
            # Left open, the socket would hold stop() until the 3 second hello wait ran out.
            self.assertLess(time.monotonic() - started, 2)
            peer.settimeout(2)
            self.assertEqual(peer.recv(1), b"")

    def test_asking_a_managed_session_for_status_opens_nothing(self):
        with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "0"}):
            daemon = Daemon(port=0, data_dir=self.folder / "data", jobs=())
            try:
                daemon.start()
                with patch.object(desktop, "CONNECT_GRACE", 0):
                    self.assertIsInstance(daemon.desktop.status(), str)
                self.assertIsNone(daemon.desktop.bound_port)
            finally:
                daemon.stop()

    def test_the_older_status_command_copes_with_a_daemon_holding_the_port(self):
        with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"}):
            daemon = Daemon(port=0, data_dir=self.folder / "data", jobs=())
            try:
                daemon.start()
                older = desktop.DesktopSession(daemon.desktop.bound_port)
                try:
                    with patch.object(desktop, "CONNECT_GRACE", 0):
                        text = older.status()
                    self.assertIn("Another Jarvis process holds the desktop bridge port", text)
                finally:
                    older.stop()
            finally:
                daemon.stop()


if __name__ == "__main__":
    unittest.main()
