"""A3a acceptance (orchestrator-owned; the implementer must not edit this file).

PR map A3 (split): A3a is the v3 daemon skeleton — `jarvis agentd` runs it,
`jarvis agent status` reads it, a loopback API on `config.V3_API_PORT`
(default 8410) that is part of Jarvis's control plane, data under
`config.V3_DIR`, and no model call at start. (A3b — agent refusals in the
shared gate — and A4 — the switch and `install` — come later.)

Interface under test:
  config.V3_API_PORT: int   (env JARVIS_V3_PORT, default 8410)
  config.V3_DIR: Path       (env JARVIS_V3_DIR, default ~/.local/share/jarvis/v3)
  jarvis.v3.daemon.Daemon(port: int | None = None, data_dir: Path | None = None)
      .start() -> None   binds 127.0.0.1:<port> (0 = ephemeral), serves in a thread
      .port -> int       the bound port
      .stop() -> None    releases the port; idempotent
  GET /v3/status -> 200 JSON {"version": "v3", "pid": int, "started_at": str, ...}

Run: PYTHONPATH=<worktree> HOME=$(mktemp -d) python tests/v3/a3a_acceptance_check.py
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def get(port, path="/v3/status", host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


class Config(unittest.TestCase):
    def test_defaults_and_control_plane(self):
        from jarvis import config
        if "JARVIS_V3_PORT" not in os.environ:
            self.assertEqual(config.V3_API_PORT, 8410)
        self.assertIn(8410, config.control_ports())
        with mock.patch.object(config, "V3_API_PORT", 9411):
            ports = config.control_ports()
            self.assertIn(9411, ports)      # the configured port, read at call time
            self.assertIn(8410, ports)      # and its default (another process may use it)
        if "JARVIS_V3_DIR" not in os.environ:
            self.assertEqual(Path(config.V3_DIR), Path.home() / ".local/share/jarvis/v3")

    def test_env_overrides(self):
        env = {**os.environ, "JARVIS_V3_PORT": "9412", "JARVIS_V3_DIR": "/tmp/x-v3-accept"}
        code = "from jarvis import config; print(config.V3_API_PORT, config.V3_DIR)"
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=60, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["9412", "/tmp/x-v3-accept"])


class Daemon(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="a3a-accept-")
        self.data = Path(self._tmp.name) / "v3"

    def tearDown(self):
        self._tmp.cleanup()

    def start(self):
        from jarvis.v3.daemon import Daemon
        d = Daemon(port=0, data_dir=self.data)
        d.start()
        self.addCleanup(d.stop)
        return d

    def test_status_and_data_dir(self):
        d = self.start()
        resp, body = get(d.port)
        self.assertEqual(resp.status, 200, body)
        data = json.loads(body)
        self.assertEqual(data["version"], "v3")
        self.assertEqual(data["pid"], os.getpid())
        self.assertTrue(data["started_at"])
        self.assertTrue(self.data.is_dir())
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)

    def test_refuses_framing_on_every_response(self):
        d = self.start()
        for path in ("/v3/status", "/no-such-route"):
            resp, _ = get(d.port, path)
            self.assertEqual(resp.getheader("X-Frame-Options"), "DENY", path)
            self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy") or "", path)

    def test_host_guard(self):
        d = self.start()
        for host in ("evil.example", f"127.0.0.1.nip.io:{d.port}", f"LOCALHOST:{d.port}", "", f"[::1]:{d.port}"):
            resp, _ = get(d.port, host=host)
            self.assertIn(resp.status, (400, 403, 421), host)
        resp, _ = get(d.port, host=f"localhost:{d.port}")
        self.assertEqual(resp.status, 200)

    def test_origin_guard(self):
        d = self.start()
        resp, _ = get(d.port, origin="http://evil.example")
        self.assertEqual(resp.status, 403)
        resp, _ = get(d.port, origin="null")
        self.assertEqual(resp.status, 403)
        resp, _ = get(d.port, origin=f"http://127.0.0.1:{d.port}")
        self.assertEqual(resp.status, 200)

    def test_loopback_only(self):
        d = self.start()
        with socket.create_connection(("127.0.0.1", d.port), timeout=5):
            pass
        bound = d.address if hasattr(d, "address") else None
        if bound is not None:
            self.assertEqual(bound[0], "127.0.0.1")

    def test_stop_releases_port_and_is_idempotent(self):
        from jarvis.v3.daemon import Daemon
        d = Daemon(port=0, data_dir=self.data)
        d.start()
        port = d.port
        d.stop()
        d.stop()
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))

    def test_port_in_use_is_a_clear_error(self):
        from jarvis.v3.daemon import Daemon
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            taken = s.getsockname()[1]
            d = Daemon(port=taken, data_dir=self.data)
            with self.assertRaises(Exception) as cm:
                d.start()
            self.assertIn(str(taken), str(cm.exception))

    def test_start_never_calls_a_model(self):
        import jarvis.llm as llm
        boom = mock.Mock(side_effect=AssertionError("model called"))
        patches = [mock.patch.object(llm, name, boom) for name in dir(llm)
                   if not name.startswith("_") and callable(getattr(llm, name))
                   and getattr(getattr(llm, name), "__module__", "") == llm.__name__]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        d = self.start()
        get(d.port)
        boom.assert_not_called()


class Cli(unittest.TestCase):
    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_status_when_not_running(self):
        env = {**os.environ, "JARVIS_V3_PORT": str(self.free_port())}
        r = subprocess.run([sys.executable, "-m", "jarvis", "agent", "status"], env=env,
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not running", (r.stdout + r.stderr).lower())

    def test_status_when_running(self):
        from jarvis.v3.daemon import Daemon
        tmp = tempfile.TemporaryDirectory(prefix="a3a-cli-")
        self.addCleanup(tmp.cleanup)
        d = Daemon(port=0, data_dir=Path(tmp.name) / "v3")
        d.start()
        self.addCleanup(d.stop)
        env = {**os.environ, "JARVIS_V3_PORT": str(d.port)}
        r = subprocess.run([sys.executable, "-m", "jarvis", "agent", "status"], env=env,
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("v3", r.stdout)

    def test_agentd_help(self):
        r = subprocess.run([sys.executable, "-m", "jarvis", "agentd", "--help"],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)


class Boundary(unittest.TestCase):
    def test_still_clean(self):
        from jarvis.v3 import imports
        self.assertEqual(imports.violations(ROOT / "jarvis" / "v3"), [])
        self.assertEqual(imports.reverse_violations(ROOT / "jarvis" / "v2"), [])


if __name__ == "__main__":
    unittest.main()
