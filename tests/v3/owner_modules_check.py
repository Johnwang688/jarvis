# Orchestrator test (Opus review, 2026-11-23). Strings only; nothing runs.
"""v3's owner modules are refused, and ordinary files that share a name are not."""

import unittest
from unittest.mock import patch
from pathlib import Path

from jarvis import config, protected_state as ps, rules


class OwnerModules(unittest.TestCase):
    def test_ordinary_scripts_of_the_same_name(self):
        for command in ("python cli.py --help", "python3 ../myapp/cli.py import data.csv", "python switch.py",
                        "uv run python src/cli.py --help", "python src/v3x/cli.py", "python -m pytest tests/cli.py",
                        "python -m click.cli", "cat jarvis/v3/cli.py", "grep -n stop jarvis/v3/switch.py",
                        "python -m myapp.v3.cli stop", "coverage run cli.py"):
            with self.subTest(command=command):
                self.assertIsNone(ps.owner_op(command))
        for command in ("python cli.py --help", "python3 ../myapp/cli.py import data.csv", "python switch.py"):
            self.assertEqual(rules.decide(command).decision, rules.ALLOW, command)

    def test_v3s_own_are_refused(self):
        for command in (
                "python jarvis/v3/cli.py agent stop", "python3 ./jarvis/v3/switch.py", "python -m jarvis.v3.cli agent stop",
                "python -m jarvis.v3.switch", "python ~/projects/Jarvis/jarvis/v3/cli.py",
                "python -c 'from jarvis.v3 import cli; cli.stop(None)'",
                "python -c 'import jarvis.v3.switch as s; s.to_v2(s.default_env())'",
                "python -c 'from jarvis.v3.switch import to_v3; to_v3(None)'",
                "python -c 'from jarvis.v3.cli import add_commands; add_commands(p)'",
                "python -c 'import runpy; runpy.run_path(\"jarvis/v3/cli.py\")[\"stop\"](None)'",
                "python -c 'import runpy; runpy.run_module(\"jarvis.v3.cli\")'",
                "python -c 'import importlib; importlib.import_module(\"jarvis.v3.switch\").stop_v3(None)'"):
            with self.subTest(command=command):
                self.assertIsNotNone(ps.owner_op(command))

    def test_an_ordinary_word_never_hides_a_later_refusal(self):
        with patch.object(config, "REPO_ROOT", Path("/home/u/projects/Jarvis")):
            for word in ("cli", "client", "switch", "agent", "agentic"):
                for n in (3, 12, 13, 16):
                    for front in ("env ", "nohup "):
                        command = front * n + f"echo {word}; jarvis restart"
                        with self.subTest(command=command[-50:]):
                            self.assertEqual(rules.decide(command).decision, rules.DENY)


if __name__ == "__main__":
    unittest.main()
