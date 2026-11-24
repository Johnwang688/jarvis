"""HTTP guards, shutdown and CLI checks for the v3 daemon."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from http.client import HTTPConnection, parse_headers
from http.server import ThreadingHTTPServer
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import cli
from jarvis.v3.daemon import Daemon, _status_handler


def request(port, method="GET", path="/v3/status", headers=()):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


class DaemonCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "v3"
        self.daemon = Daemon(port=0, data_dir=self.data)
        self.addCleanup(self.daemon.stop)

    def assert_protected(self, headers):
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def request(self, method="GET", path="/v3/status", extra=()):
        port = self.daemon.port
        return request(port, method, path, (("Host", f"127.0.0.1:{port}"), *extra))

    def raw_request(self, data):
        with socket.create_connection(self.daemon.address, timeout=5) as client:
            client.sendall(data)
            with client.makefile("rb") as stream:
                status = stream.readline()
                headers = parse_headers(stream)
                return status, headers, stream.read()

    def test_host_guard(self):
        self.daemon.start()
        port = self.daemon.port
        for hosts in ([], ["evil.example"], [f"LOCALHOST:{port}"], [f"localhost:{port}", "evil.example"]):
            with self.subTest(hosts=hosts):
                code, headers, body = request(
                    port, "POST", "/unknown", [("Host", value) for value in hosts]
                )
                self.assertEqual(code, 421)
                self.assert_protected(headers)
                self.assertIn("error", json.loads(body))

    def test_origin_guard(self):
        self.daemon.start()
        port = self.daemon.port
        for origins in (["null"], [""], [f"http://localhost:{port}/"], ["http://evil.example"],
                        [f"http://127.0.0.1:{port}", "http://evil.example"]):
            with self.subTest(origins=origins):
                code, headers, body = self.request(
                    "POST", "/unknown", [("Origin", value) for value in origins]
                )
                self.assertEqual(code, 403)
                self.assert_protected(headers)
                self.assertIn("error", json.loads(body))
        for host in ("127.0.0.1", "localhost"):
            code, _, _ = self.request(extra=[("Origin", f"http://{host}:{port}")])
            self.assertEqual(code, 200)

    def test_headers_on_success_and_errors(self):
        self.daemon.start()
        for method, path, expected in (
            ("GET", "/v3/status", 200), ("HEAD", "/v3/status", 200),
            ("GET", "/missing", 404), ("HEAD", "/missing", 404),
            ("GET", "//v3/status", 404),
            ("POST", "/v3/status", 405), ("FUTURE-METHOD", "/missing", 405),
        ):
            with self.subTest(method=method, path=path):
                code, headers, body = self.request(method, path)
                self.assertEqual(code, expected)
                self.assert_protected(headers)
                self.assertEqual(headers["Content-Type"], "application/json")
                if method == "HEAD":
                    self.assertEqual(body, b"")
                    self.assertGreater(int(headers["Content-Length"]), 0)
                elif expected == 200:
                    status = json.loads(body)
                    self.assertEqual(status["service"], "jarvis-agentd")
                    self.assertEqual(status["data_dir"], str(self.data))
                    self.assertEqual(datetime.fromisoformat(status["started_at"]).utcoffset(), timedelta(0))
                    self.assertGreaterEqual(status["uptime_s"], 0)
                else:
                    self.assertIn("error", json.loads(body))
                if expected == 405:
                    self.assertEqual(headers["Allow"], "GET, HEAD")

        raw_head = f"HEAD /v3/status HTTP/1.1\r\nHost: localhost:{self.daemon.port}\r\n\r\n"
        _, _, body = self.raw_request(raw_head.encode("ascii"))
        self.assertEqual(body, b"")

    def test_parser_error_has_headers(self):
        with patch("sys.stderr") as stderr:
            self.daemon.start()
            status, headers, body = self.raw_request(b"GET /v3/status HTTP/invalid\r\n\r\n")
            stderr.write.assert_not_called()
        self.assertTrue(status.startswith(b"HTTP/1.0 400 "), status)
        self.assert_protected(headers)
        self.assertIn("error", json.loads(body))

    def test_http09_has_framed_host_refusal(self):
        handler_type = _status_handler(self.data)
        handler = handler_type.__new__(handler_type)
        handler.server = SimpleNamespace(server_port=0)
        handler.rfile = BytesIO(b"GET /v3/status\r\n")
        handler.wfile = BytesIO()
        handler.handle_one_request()

        handler.wfile.seek(0)
        self.assertTrue(handler.wfile.readline().startswith(b"HTTP/1.0 421 "))
        self.assert_protected(parse_headers(handler.wfile))
        self.assertIn("error", json.load(handler.wfile))

    def test_loopback_bind(self):
        def loopback_only(address, handler):
            # Refuse an unsafe mutant before giving its address to the kernel.
            self.assertEqual(address, ("127.0.0.1", 0))
            return ThreadingHTTPServer(address, handler)

        with patch("jarvis.v3.daemon.ThreadingHTTPServer", side_effect=loopback_only):
            self.daemon.start()
        self.assertEqual(self.daemon.address, ("127.0.0.1", self.daemon.port))
        self.assertEqual(self.request()[0], 200)

    def test_control_port_parity(self):
        from jarvis.browser import BrowserPolicy
        from jarvis.v2.browser.policy import refused_ports

        self.daemon.start()
        with patch.object(config, "V3_API_PORT", self.daemon.port):
            policy = BrowserPolicy()
            for port in (8410, self.daemon.port):
                url = f"http://127.0.0.1:{port}/v3/status"
                self.assertTrue(config.is_face_origin(url))
                self.assertFalse(policy.allows(url))
                self.assertIn(port, refused_ports())

    def test_repairs_existing_data_dir_mode(self):
        self.data.mkdir()
        self.data.chmod(0)
        self.daemon.start()
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)

    def test_refuses_data_dir_symlink_before_bind(self):
        target = self.data.with_name("target")
        target.mkdir()
        target.chmod(0o750)
        self.data.symlink_to(target, target_is_directory=True)
        with patch("jarvis.v3.daemon.ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(RuntimeError, "data directory.*symlink"):
                self.daemon.start()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
            target.rmdir()
            with self.assertRaisesRegex(RuntimeError, "data directory.*symlink"):
                self.daemon.start()
            server.assert_not_called()
        self.assertFalse(target.exists())

    def test_concurrent_stop_during_request(self):
        self.daemon.start()
        entered = threading.Event()
        release = threading.Event()
        original = config.own_host_header

        def held_host_check(value, port):
            entered.set()
            return release.wait(timeout=5) and original(value, port)

        with ThreadPoolExecutor(max_workers=3) as pool, patch.object(
            config, "own_host_header", side_effect=held_host_check
        ):
            response = pool.submit(self.request)
            try:
                self.assertTrue(entered.wait(timeout=5))
                barrier = threading.Barrier(2, timeout=5)

                def stop():
                    barrier.wait()
                    self.daemon.stop()

                stops = [pool.submit(stop) for _ in range(2)]
                for stopping in stops:
                    stopping.result(timeout=4)
            finally:
                release.set()
            self.assertEqual(response.result(timeout=5)[0], 200)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(self.daemon.address, timeout=2)


class CliCheck(unittest.TestCase):
    def test_agent_help_paths_skip_model_stack(self):
        forbidden = {"jarvis.agent", "jarvis.llm", "jarvis.models", "jarvis.v2.providers"}
        for command in (["agentd", "--help"], ["agent", "status", "--help"]):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-X", "importtime", "-m", "jarvis", *command],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                imports = {line.rsplit("|", 1)[-1].strip() for line in result.stderr.splitlines()}
                self.assertFalse(imports & forbidden, imports & forbidden)

    def test_status_requires_service_identity(self):
        for body in (b'{"version":"v3"}', b'{"version":"v3","service":"inventory"}', b'not JSON'):
            with self.subTest(body=body), patch.object(config, "V3_API_PORT", 0), patch.object(
                cli, "HTTPConnection"
            ) as connection, redirect_stdout(StringIO()) as output:
                connection.return_value.getresponse.return_value = SimpleNamespace(
                    status=200, read=lambda: body
                )
                self.assertEqual(cli.status(None), 1)
                self.assertIn("Something else answers on 127.0.0.1:0", output.getvalue())

    def test_agentd_shuts_down_on_signals(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as data:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                env = {**os.environ, "JARVIS_V3_PORT": str(port), "JARVIS_V3_DIR": data}
                with subprocess.Popen(
                    [sys.executable, "-m", "jarvis", "agentd"], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) as process:
                    try:
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline and process.poll() is None:
                            try:
                                code, _, body = request(
                                    port, headers=[("Host", f"127.0.0.1:{port}")]
                                )
                            except ConnectionRefusedError:
                                time.sleep(0.05)
                                continue
                            self.assertEqual(code, 200)
                            self.assertEqual(json.loads(body)["pid"], process.pid)
                            break
                        else:
                            self.fail("agentd did not become ready")
                        process.send_signal(sig)
                        stdout, stderr = process.communicate(timeout=10)
                        self.assertEqual(process.returncode, 0, stdout + stderr)
                    finally:
                        if process.poll() is None:
                            process.kill()
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(("127.0.0.1", port), timeout=2)

    def test_agent_requires_action_and_bench_agent_help_still_works(self):
        for command, expected in ((["agent"], 2), (["bench", "agent", "--help"], 0)):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-m", "jarvis", *command],
                    capture_output=True, text=True, timeout=15,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, expected, output)
                self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
