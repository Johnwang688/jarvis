"""Ordinary docs, image links, and memory statements should pass cleanly."""

import unittest

from jarvis.v3.content_scan import scan


class BenignContent(unittest.TestCase):
    def test_configuration_placeholder_is_not_a_credential(self):
        text = "```yaml\npassword: " + "your-password" + "-here\n```\n"
        self.assertEqual(scan(text, purpose="skill").hits, ())

    def test_embedded_report_image_with_literal_dollar_path(self):
        text = "![Coverage](https://reports.example.invalid/$web/coverage.png)"
        self.assertEqual(scan(text, purpose="brief").hits, ())

    def test_shell_lookup_does_not_run_the_program_it_names(self):
        self.assertEqual(scan("```bash\ncommand -v dd\n```", purpose="skill").hits, ())


if __name__ == "__main__":
    unittest.main()
