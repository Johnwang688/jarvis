"""Exercise the v2/v3 import boundary without importing fixture modules."""

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.imports import ALLOWED_V2, Violation, reverse_violations, violations


ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = {
    "jarvis.v2.browser.gate",
    "jarvis.v2.browser.policy",
    "jarvis.v2.hud_install",
    "jarvis.v2.redact",
    "jarvis.v2.push",
    "jarvis.v2.quiet",
    "jarvis.v2.quiet_defer",
    "jarvis.v2.stores",
    "jarvis.v2.mcp",
    "jarvis.v2.skills_link",
    "jarvis.v2.remote_auth",
    "jarvis.v2.remote_secure",
    "jarvis.v2.providers.claude",
}


class ImportBoundary(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="import-boundary-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def write(self, relative_path, source):
        path = self.base / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def test_import_forms_in_both_directions(self):
        forms = [
            ("direct", "import jarvis.{version}.{name}", "{name}", 1),
            ("aliased", "import jarvis.{version}.{name} as other", "{name}", 1),
            ("from module", "from jarvis.{version}.{name} import Symbol as other", "{name}", 1),
            ("from package", "from jarvis.{version} import {name} as other", "{name}", 1),
            ("from parent", "from jarvis import {version} as other", "", 1),
            ("package", "import jarvis.{version}", "", 1),
            ("star", "from jarvis.{version} import *", "", 1),
            ("relative package", "from ...{version} import {name}", "{name}", 1),
            ("relative module", "from ...{version}.{name} import Symbol", "{name}", 1),
            ("relative parent", "from ... import {version}", "", 1),
            ("qualified call", "import importlib\nimportlib.import_module('jarvis.{version}.{name}')", "{name}", 2),
            ("bare call", "from importlib import import_module\nimport_module('jarvis.{version}.{name}')", "{name}", 2),
            ("builtin call", "__import__('jarvis.{version}.{name}')", "{name}", 1),
            ("module alias call", "import importlib as il\nil.import_module('jarvis.{version}.{name}')", "{name}", 2),
            ("function alias call", "from importlib import import_module as load\nload('jarvis.{version}.{name}')", "{name}", 2),
            ("relative keyword call", "import_module('...{version}.{name}', package='jarvis.v3.sub')", "{name}", 1),
            ("relative positional call", "importlib.import_module('..{version}.{name}', 'jarvis.v2')", "{name}", 1),
            ("keyword name call", "import_module(package='jarvis.v3', name='..{version}.{name}')", "{name}", 1),
            ("keyword fromlist call", "__import__('jarvis.{version}', fromlist=['{name}'])", "{name}", 1),
            ("positional fromlist call", "__import__('jarvis.{version}', None, None, ('{name}',))", "{name}", 1),
            ("keyword level call", "__import__(name='{version}.{name}', globals=globals(), level=3)", "{name}", 1),
            ("positional level call", "__import__('{version}.{name}', globals(), None, (), 3)", "{name}", 1),
            ("nested", "def f():\n    class C:\n        try:\n            import jarvis.{version}.{name}\n        except ImportError:\n            pass", "{name}", 4),
        ]
        directions = [
            (violations, "v2", "daemon", "v3"),
            (reverse_violations, "v3", "imports", "v2"),
        ]
        for scan, version, name, own_version in directions:
            for label, source, suffix, line in forms:
                with self.subTest(direction=scan.__name__, form=label):
                    # The file's package is jarvis.vN.sub, including for __init__.
                    path = self.write("sub/__init__.py", source.format(version=version, name=name))
                    module = ".".join(filter(None, ("jarvis", version, suffix.format(name=name))))
                    self.assertEqual(scan(self.base), [Violation(path, line, module)])

                    # Package imports need a name outside the restricted tree
                    # in the forward direction. Member imports can use ALLOWED_V2.
                    allowed_version = "v2" if suffix else own_version
                    self.write("sub/__init__.py", source.format(version=allowed_version, name="quiet"))
                    self.assertEqual(scan(self.base), [])

    def test_multiple_names_keep_source_order_and_paths_sort_first(self):
        tail = self.write("z.py", "import jarvis.v2.model as model, jarvis.v2.daemon\n")
        head = self.write(
            "a.py",
            "class C:\n    import jarvis.v2.daemon\n"
            "from jarvis.v2 import quiet, stores as disk, model, providers as engines\n",
        )
        self.assertEqual(
            violations(self.base),
            [
                Violation(head, 2, "jarvis.v2.daemon"),
                Violation(head, 3, "jarvis.v2.model"),
                Violation(head, 3, "jarvis.v2.providers"),
                Violation(tail, 1, "jarvis.v2.model"),
                Violation(tail, 1, "jarvis.v2.daemon"),
            ],
        )
        self.write("z.py", "from jarvis.v3 import one, two as other\n")
        self.assertEqual(
            reverse_violations(self.base),
            [Violation(tail, 1, "jarvis.v3.one"), Violation(tail, 1, "jarvis.v3.two")],
        )

    def test_exact_module_match(self):
        path = self.write(
            "x.py",
            "import jarvis.v20.daemon, jarvis.v30.vault\n"
            "from jarvis import rules\n"
            "from jarvis.v2.quiet import *\n"
            "import jarvis.v2.quiet.child\n"
            "from jarvis.v2.providers import claude, codex\n"
            "some_other_object.import_module('jarvis.v2.daemon')\n"
            "text = 'import jarvis.v2.daemon'\n",
        )
        self.assertEqual(
            violations(self.base),
            [
                Violation(path, 4, "jarvis.v2.quiet.child"),
                Violation(path, 5, "jarvis.v2.providers.codex"),
            ],
        )
        self.assertEqual(reverse_violations(self.base), [])

    def test_package_override_and_module_location(self):
        source = "from ....v2 import daemon\nfrom ....v3 import vault\n"
        first = self.write("nested/deep/__init__.py", source)
        second = self.write("nested/deep/regular.py", source)
        self.assertEqual(
            violations(self.base, "jarvis.other"),
            [Violation(path, 1, "jarvis.v2.daemon") for path in (first, second)],
        )
        self.assertEqual(
            reverse_violations(self.base, "jarvis.other"),
            [Violation(path, 2, "jarvis.v3.vault") for path in (first, second)],
        )

    def test_relative_sibling_and_child_at_package_root(self):
        path = self.write(
            "__init__.py",
            "from . import sibling\nfrom .. import v2, v3\nfrom .sub import functions\n",
        )
        self.assertEqual(violations(self.base), [Violation(path, 2, "jarvis.v2")])
        self.assertEqual(reverse_violations(self.base), [Violation(path, 2, "jarvis.v3")])

    def test_builtin_fromlist_resolves_all_modules_and_falls_back_for_symbols(self):
        path = self.write(
            "calls.py",
            "__import__('jarvis.v2', fromlist=['quiet', 'model', 'daemon', 'model'])\n"
            "__import__('jarvis.v2.quiet', fromlist=['within'])\n"
            "__import__('jarvis.v2.providers', fromlist=['claude', 'codex'])\n"
            "__import__('jarvis.v2.daemon', fromlist=['Daemon'])\n"
            "__import__('jarvis.v2.daemon', fromlist=members)\n"
            "__import__('', globals(), fromlist=['v2', 'v3'], level=2)\n"
            "__import__('jarvis.v2', fromlist=['__doc__'])\n",
        )
        self.assertEqual(
            violations(self.base),
            [
                Violation(path, 1, "jarvis.v2.model"),
                Violation(path, 1, "jarvis.v2.daemon"),
                Violation(path, 3, "jarvis.v2.providers.codex"),
                Violation(path, 4, "jarvis.v2.daemon"),
                Violation(path, 5, "jarvis.v2.daemon"),
                Violation(path, 6, "jarvis.v2"),
                Violation(path, 7, "jarvis.v2"),
            ],
        )
        self.assertEqual(reverse_violations(self.base), [Violation(path, 6, "jarvis.v3")])

    def test_linked_packages_use_each_path_and_skip_ancestor_cycles(self):
        shared = self.write("shared/__init__.py", "from ... import v2, v3\n").parent
        root = self.base / "scan"
        root.mkdir()
        aliases = [root / "first", root / "second"]
        for alias in aliases:
            alias.symlink_to(shared, target_is_directory=True)
        (shared / "back").symlink_to(root, target_is_directory=True)

        for scan, module in [(violations, "jarvis.v2"), (reverse_violations, "jarvis.v3")]:
            with self.subTest(scan=scan.__name__):
                self.assertEqual(
                    scan(root), [Violation(alias / "__init__.py", 1, module) for alias in aliases]
                )

    def test_unresolvable_and_unparseable(self):
        bad = self.write("bad.py", "\ndef (:\n")
        relative = self.write(
            "relative.py",
            "from ....x import thing\n"
            "import_module('..v2.daemon')\n"
            "import_module('..v3.vault', package=current_package)\n"
            "import_module('...v2.daemon', package='jarvis.v3')\n"
            "__import__('v2.daemon', globals(), level=3)\n"
            "__import__('v2.daemon', globals(), level=current_level)\n",
        )
        expected = [Violation(bad, 2, "<unparseable>")] + [
            Violation(relative, line, "<unresolvable>") for line in range(1, 7)
        ]
        self.assertEqual(violations(self.base), expected)
        self.assertEqual(reverse_violations(self.base), expected)

    def test_allowlist_and_real_tree(self):
        jarvis_dir = ROOT / "jarvis"
        self.assertEqual(set(ALLOWED_V2), ALLOWLIST)
        for reason in ALLOWED_V2.values():
            self.assertTrue(reason.strip())
            self.assertEqual(len(reason.splitlines()), 1)
        self.assertEqual(violations(jarvis_dir / "v3"), [])
        self.assertEqual(reverse_violations(jarvis_dir / "v2"), [])

    def test_initializer_is_only_a_docstring(self):
        init = ROOT / "jarvis" / "v3" / "__init__.py"
        tree = ast.parse(init.read_bytes())
        self.assertTrue(ast.get_docstring(tree))
        self.assertEqual(len(tree.body), 1)


if __name__ == "__main__":
    unittest.main()
