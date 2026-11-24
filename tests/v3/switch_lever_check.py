"""Pin the switch to the owner CLI and classify inline calls without running them."""

import inspect
from pathlib import Path
import re
import unittest

from jarvis import protected_state, tools

REPO = Path(__file__).resolve().parents[2]
SWITCH_IMPORT = re.compile(
    r"^\s*(?:"
    r"from\s+(?:jarvis\.v3|\.+v3|\.+)\s+import\s+(?:\(\s*)?"
    r"(?:\w+(?:\s+as\s+\w+)?\s*,\s*)*switch\b"
    r"|from\s+(?:jarvis\.v3\.|\.+v3\.|\.+)switch\s+import\b"
    r"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*jarvis\.v3\.switch\b"
    r")",
    re.MULTILINE,
)


class NoLever(unittest.TestCase):
    def test_only_the_cli_imports_it(self):
        importers = [
            path.relative_to(REPO).as_posix()
            for path in sorted((REPO / "jarvis").rglob("*.py"))
            if SWITCH_IMPORT.search(path.read_text())
        ]
        self.assertEqual(importers, ["jarvis/__main__.py", "jarvis/v3/cli.py"])

    def test_no_registered_tool_module_imports_it(self):
        modules = {inspect.getmodule(tool.func) for tool in tools.REGISTRY.values()}
        for module in sorted(modules, key=lambda module: module.__name__):
            with self.subTest(module=module.__name__):
                self.assertIsNone(SWITCH_IMPORT.search(inspect.getsource(module)))

    def test_python_writer_calls_are_refused(self):
        denied = (
            'python -c "from jarvis.v3 import switch; switch.to_v3(switch.default_env())"',
            'python -c "import jarvis.v3.switch as s; s.install_unit(...)"',
            'python -c "from jarvis.v3 import switch; switch.default_env().systemctl(\'stop\', switch.V3_UNIT)"',
            "python -m jarvis.v3.switch",
        )
        for command in denied:
            with self.subTest(command=command):
                self.assertIn("decision A-2", protected_state.owner_op(command) or "")

    def test_all_writer_entry_points_are_refused(self):
        for writer in (
            "to_v2", "stop_v3", "restart_v3", "_run_systemctl", "_systemctl", "_change",
            "_stop_and_disable", "_enable", "_start_v2", "_write_unit",
        ):
            with self.subTest(writer=writer):
                command = f'python -c "from jarvis.v3.switch import {writer} as op; op(...)"'
                self.assertIn("decision A-2", protected_state.owner_op(command) or "")

    def test_read_is_allowed(self):
        command = 'python -c "from jarvis.v3 import switch; print(switch.V3_UNIT)"'
        self.assertIsNone(protected_state.owner_op(command))


if __name__ == "__main__":
    unittest.main()
