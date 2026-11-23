"""(A3a verifier r1, orchestrator-owned; the fixer must not edit.) An unrelated service's API version is insufficient to identify Jarvis."""

from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import cli


class StatusIdentityCheck(unittest.TestCase):
    def test_unrelated_api_version_does_not_report_a_running_daemon(self):
        output = StringIO()
        with patch.object(config, "V3_API_PORT", 0), patch.object(
            cli, "HTTPConnection"
        ) as connection, redirect_stdout(output):
            connection.return_value.getresponse.return_value = SimpleNamespace(
                status=200, read=lambda: b'{"service":"inventory","version":"v3"}'
            )
            code = cli.status(None)

        connection.return_value.request.assert_called_once_with(
            "GET", "/v3/status", headers={"Host": "127.0.0.1:0"}
        )
        text = output.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn("127.0.0.1:0", text)
        self.assertNotIn("running", text.replace("not running", ""), text)  # never claims a v3 daemon
        # Orchestrator rewrite (r2): the card asks for "something else answers on the port"; the
        # reviewer's exact "not running" string was one acceptable wording, not the contract.


if __name__ == "__main__":
    unittest.main()
