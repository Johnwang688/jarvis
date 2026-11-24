"""K1 must see commands in ordinary Markdown containers and transcripts."""

import unittest

from jarvis.v3.content_scan import scan
from k1_acceptance_check import BLOCK


class Markdown(unittest.TestCase):
    def test_command_hits_survive_markdown_containers(self):
        command = BLOCK["rm -rf root"][1].splitlines()[3]
        cases = {
            "ordered list": f"1. ```bash\n   {command}\n   ```\n",
            "quoted fence with tabs and CRLF": f"> ```sh\r\n>\t{command}\r\n> ```\r\n",
            "nested quote": f"> > ~~~bash\n> > {command}\n> > ~~~\n",
            "named shell prompt": f"```console\nuser@example.invalid:~/demo$ {command}\n```\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                start = text.index(command)
                verdict = scan(text, purpose="skill")
                self.assertFalse(verdict.ok)
                self.assertIn(
                    {"rule": "denied-shell", "span": [start, start + len(command)], "severity": "block"},
                    verdict.to_json()["hits"],
                )


if __name__ == "__main__":
    unittest.main()
