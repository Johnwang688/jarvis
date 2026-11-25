"""Regressions for the owner chat HTTP parser and the first-send imports."""

from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from textwrap import dedent
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from jarvis.v3.daemon import _status_handler


class EntrypointRegression(unittest.TestCase):
    def test_send_rejects_duplicate_text_fields(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        chat = Mock()
        chat.send.return_value = {"state": "running", "steered": False}
        port = 49152
        authority = f"127.0.0.1:{port}"
        body = b'{"text":"First question.","text":"Second question."}'
        headers = (
            "POST /v3/owner/chat/send HTTP/1.1\r\n"
            f"Host: {authority}\r\nOrigin: http://{authority}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        outgoing = bytearray()
        request = Mock()
        request.makefile.return_value = BytesIO(headers + body)
        request.sendall.side_effect = outgoing.extend
        server = SimpleNamespace(server_port=port, server_address=("127.0.0.1", port))

        _status_handler(folder, folder / "hud", chat=chat)(request, server.server_address, server)

        self.assertEqual(outgoing.split(b"\r\n", 1)[0].split()[1], b"400")
        chat.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
