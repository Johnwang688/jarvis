"""(A2 verifier r1, orchestrator-owned; the fixer must not edit this file.) The walk must cover Python modules in a linked package."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.imports import Violation, violations


class LinkedPackage(unittest.TestCase):
    def test_relative_import_in_linked_package(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            v3 = base / "v3"
            v3.mkdir()
            shared = base / "shared"
            shared.mkdir()
            (shared / "__init__.py").write_text(
                "from ...v2 import model\n", encoding="utf-8"
            )
            linked = v3 / "linked"
            linked.symlink_to(shared, target_is_directory=True)
            self.assertEqual(
                violations(v3),
                [Violation(linked / "__init__.py", 1, "jarvis.v2.model")],
            )


if __name__ == "__main__":
    unittest.main()
