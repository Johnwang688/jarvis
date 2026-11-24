"""(A3a verifier r1, orchestrator-owned; the fixer must not edit.) A legacy request without Host gets a framed HTTP refusal."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest

from jarvis.v3.daemon import _status_handler


class Http09Check(unittest.TestCase):
    def test_hostless_http09_request_gets_a_framed_refusal(self):
        handler_type = _status_handler(Path("unused"))
        handler = handler_type.__new__(handler_type)
        handler.server = SimpleNamespace(server_port=0)
        handler.rfile = BytesIO(b"GET /v3/status\r\n")
        handler.wfile = BytesIO()
        handler.handle_one_request()

        head, _, _ = handler.wfile.getvalue().partition(b"\r\n\r\n")
        status, *headers = head.split(b"\r\n")
        self.assertIn(status.split()[1], (b"403", b"421"))
        self.assertIn(b"X-Frame-Options: DENY", headers)
        (policy,) = [h for h in headers if h.startswith(b"Content-Security-Policy: ")]
        self.assertIn(b"frame-ancestors 'none'", policy)
        self.assertIn(b"Cache-Control: no-store", headers)
        self.assertIn(b"X-Content-Type-Options: nosniff", headers)


if __name__ == "__main__":
    unittest.main()
