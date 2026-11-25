"""Keep private request and response text out of exception chains.

A title is visible in the process list via --title. Callers must keep private text out of it.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3.t3_client import T3, T3Error


class PrivateText(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        self.binary = self.home / "fake-cli"
        self.client = T3(self.binary, timeout=5)

    def fake(self, source):
        self.binary.write_text(f"#!{sys.executable} -I\n{source}\n", encoding="utf-8")
        self.binary.chmod(0o700)

    def assert_error_excludes(self, error, private):
        pending = [error]
        seen = set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            payloads = (str(current), repr(current.args), getattr(current, "doc", ""),
                        getattr(current, "object", ""))
            self.assertFalse(any(private in str(value) for value in payloads),
                             f"private input retained by {type(current).__name__}")
            pending.extend((current.__cause__, current.__context__))

    def test_unencodable_prompt_stays_out_of_exception_chain(self):
        prompt = "fake-private-thread-prompt"
        with self.assertRaises(T3Error) as caught:
            self.client.send("t:1", prompt + "\ud800")
        self.assertEqual(caught.exception.kind, "arguments")
        self.assert_error_excludes(caught.exception, prompt)

    def test_title_in_malformed_response_stays_out_of_exception_chain(self):
        title = "fake-private-thread-title"
        response = self.home / "response.json"
        response.write_text('{"thread":{"title":' + json.dumps(title), encoding="utf-8")
        self.fake("import os, sys\nfrom pathlib import Path\n"
                  "sys.stdout.buffer.write(Path(os.environ['HOME'], 'response.json').read_bytes())")
        with self.assertRaises(T3Error) as caught:
            self.client.show("t:1")
        self.assertEqual(caught.exception.kind, "shape")
        self.assert_error_excludes(caught.exception, title)


if __name__ == "__main__":
    unittest.main()
