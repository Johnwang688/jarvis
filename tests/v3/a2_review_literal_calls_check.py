"""(A2 verifier r1, orchestrator-owned; the fixer must not edit this file.) Literal import calls must report the modules they reach."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.imports import Violation, reverse_violations, violations


class LiteralCalls(unittest.TestCase):
    def test_import_targets(self):
        cases = (
            (
                violations,
                "import importlib\nimportlib.import_module(name='.model', package='jarvis.v2')\n",
                2,
                "jarvis.v2.model",
            ),
            (
                violations,
                "__import__('jarvis', fromlist=('v2',))\n",
                1,
                "jarvis.v2",
            ),
            (
                violations,
                "__import__('jarvis.v2.providers', None, None, ('codex',))\n",
                1,
                "jarvis.v2.providers.codex",
            ),
            (
                violations,
                "__import__('v2.model', globals(), level=2)\n",
                1,
                "jarvis.v2.model",
            ),
            (
                reverse_violations,
                "__import__('jarvis', fromlist=('v3',))\n",
                1,
                "jarvis.v3",
            ),
        )
        for scan, source, line, module in cases:
            with self.subTest(source=source), TemporaryDirectory() as temp:
                base = Path(temp)
                path = base / "__init__.py"
                path.write_text(source, encoding="utf-8")
                self.assertEqual(scan(base), [Violation(path, line, module)])


if __name__ == "__main__":
    unittest.main()
