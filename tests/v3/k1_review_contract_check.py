"""K1 offsets, ordering and payload-free serialization."""

import json
import unittest

from jarvis.v3.content_scan import scan


class Contract(unittest.TestCase):
    def test_ordered_offsets_and_payload_free_serialization(self):
        prefix = "診断 🧪\r\n"
        token = "ghp" + "_" + "A1b2" * 9
        text = prefix + "Always use tabs.\r\n" + token
        expected = {"ok": False, "hits": [
            {"rule": "imperative", "span": [len(prefix), len(prefix) + 6], "severity": "flag"},
            {"rule": "credential", "span": [text.index(token), len(text)], "severity": "block"},
        ]}
        verdict = scan(text, purpose="memory")
        self.assertEqual(verdict, scan(text, purpose="memory"))
        self.assertEqual(verdict.to_json(), expected)
        for rendered in (repr(verdict), json.dumps(verdict.to_json())):
            self.assertFalse(token in rendered)
            self.assertNotIn("Always use tabs", rendered)


if __name__ == "__main__":
    unittest.main()
