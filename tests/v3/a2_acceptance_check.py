"""A2 acceptance (orchestrator-owned; the implementer must not edit this file).

PR map A2: `jarvis/v3/` exists; it imports from `jarvis.v2` only through the
allowlist in `jarvis/v3/imports.py`; v2 never imports v3; importing v3 never
changes v2's tool registry.

Interface under test (jarvis/v3/imports.py):
  ALLOWED_V2: dict[str, str]                    dotted module -> one-line reason
  Violation: has .path (Path), .line (int), .module (str)
  violations(v3_dir: Path, package: str = "jarvis.v3") -> list[Violation]
  reverse_violations(v2_dir: Path, package: str = "jarvis.v2") -> list[Violation]

Run: PYTHONPATH=<worktree> python tests/v3/a2_acceptance_check.py
"""
from __future__ import annotations

import importlib
from pathlib import Path
import pkgutil
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


def tree(files):
    tmp = tempfile.TemporaryDirectory(prefix="a2-accept-")
    base = Path(tmp.name)
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return tmp, base


class Allowlist(unittest.TestCase):
    def test_shape_and_existence(self):
        from jarvis.v3 import imports
        self.assertIsInstance(imports.ALLOWED_V2, dict)
        self.assertTrue(imports.ALLOWED_V2)
        for name, reason in imports.ALLOWED_V2.items():
            self.assertTrue(name.startswith("jarvis.v2."), name)
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip(), name)
            rel = Path(*name.split("."))
            self.assertTrue((ROOT / rel).with_suffix(".py").exists() or (ROOT / rel / "__init__.py").exists(), name)
        for required in ("jarvis.v2.redact", "jarvis.v2.quiet", "jarvis.v2.push", "jarvis.v2.stores"):
            self.assertIn(required, imports.ALLOWED_V2)


class Forward(unittest.TestCase):
    """Every way of reaching a non-allowlisted jarvis.v2 module is caught."""

    BAD = {
        "a.py": "import jarvis.v2.daemon\n",                                  # line 1
        "b.py": "x = 1\nfrom jarvis.v2.daemon import Daemon\n",               # line 2
        "c.py": "from jarvis.v2 import daemon\n",                             # line 1
        "d.py": "from jarvis import v2\n",                                    # line 1
        "e.py": "import jarvis.v2\n",                                         # line 1
        "sub/f.py": "from ...v2 import daemon\n",                             # relative from jarvis.v3.sub
        "g.py": "from ..v2.daemon import Daemon\n",                           # relative from jarvis.v3
        "h.py": "import importlib\nm = importlib.import_module('jarvis.v2.daemon')\n",  # line 2
        "i.py": "m = __import__('jarvis.v2.daemon')\n",
        "j.py": "def f():\n    import jarvis.v2.providers.claude as c\n",   # nested, line 2
        "k.py": "from jarvis.v2 import redact, daemon\n",                      # one good, one bad
    }

    GOOD = {
        "ok1.py": "import jarvis.v2.redact\n",
        "ok2.py": "from jarvis.v2.quiet import within\n",
        "ok3.py": "from jarvis.v2 import push, stores\n",
        "ok4.py": "from jarvis import rules, config\nimport jarvis.tools\n",
        "ok5.py": "from . import imports\nfrom .sub import f\n",
        "ok6.py": "import importlib\nm = importlib.import_module('jarvis.v2.redact')\n",
        "sub/__init__.py": "",
    }

    def test_bad_forms_caught_with_lines(self):
        from jarvis.v3 import imports
        tmp, base = tree({**self.BAD, **self.GOOD})
        with tmp:
            found = imports.violations(base, package="jarvis.v3")
            by_file = {}
            for v in found:
                by_file.setdefault(Path(v.path).relative_to(base).as_posix(), []).append(v)
            for rel in self.BAD:
                self.assertIn(rel, by_file, f"missed {rel}: {self.BAD[rel]!r}")
            for rel in self.GOOD:
                self.assertNotIn(rel, by_file, f"false positive {rel}: {self.GOOD[rel]!r}")
            self.assertEqual(by_file["b.py"][0].line, 2)
            self.assertEqual(by_file["h.py"][0].line, 2)
            self.assertEqual(by_file["j.py"][0].line, 2)
            self.assertTrue(all(v.module.startswith("jarvis.v2") for v in found))
            self.assertEqual([v.module for v in by_file["k.py"]], ["jarvis.v2.daemon"])
            self.assertEqual(by_file["g.py"][0].module, "jarvis.v2.daemon")
            self.assertEqual(by_file["sub/f.py"][0].module, "jarvis.v2.daemon")

    def test_unparseable_file_is_a_violation_not_a_pass(self):
        from jarvis.v3 import imports
        tmp, base = tree({"broken.py": "def (:\n"})
        with tmp:
            self.assertTrue(imports.violations(base, package="jarvis.v3"))


class Reverse(unittest.TestCase):
    def test_v2_importing_v3_is_caught(self):
        from jarvis.v3 import imports
        tmp, base = tree({
            "a.py": "import jarvis.v3.vault\n",
            "b.py": "from jarvis.v3 import vault\n",
            "c.py": "from jarvis import v3\n",
            "tools/d.py": "from ...v3 import vault\n",
            "e.py": "import importlib\nimportlib.import_module('jarvis.v3.vault')\n",
            "ok.py": "from jarvis.v2 import redact\nfrom . import stores\n",
            "tools/__init__.py": "",
        })
        with tmp:
            files = {Path(v.path).relative_to(base).as_posix() for v in imports.reverse_violations(base, package="jarvis.v2")}
            self.assertEqual(files, {"a.py", "b.py", "c.py", "tools/d.py", "e.py"})


class RealTree(unittest.TestCase):
    def test_repo_is_clean_both_ways(self):
        from jarvis.v3 import imports
        self.assertEqual(imports.violations(ROOT / "jarvis" / "v3"), [])
        self.assertEqual(imports.reverse_violations(ROOT / "jarvis" / "v2"), [])


REGISTRY_PROBE = """
import importlib, json, pkgutil, sys
from jarvis import tools
snap = lambda: [list(tools.REGISTRY), sorted(tools.EXPLICIT_ONLY), tools.default_names(),
                {n: [list(g.core), list(g.extra)] for n, g in sorted(tools.GROUPS.items())}]
assert not any(m == "jarvis.v3" or m.startswith("jarvis.v3.") for m in sys.modules), "v3 imported early"
before = snap()
import jarvis.v3
for mod in pkgutil.walk_packages(jarvis.v3.__path__, "jarvis.v3."):
    importlib.import_module(mod.name)
print(json.dumps({"before": before, "after": snap()}))
"""


class Registry(unittest.TestCase):
    def test_importing_v3_leaves_v2_tools_alone(self):
        # A fresh interpreter: earlier tests in this file import jarvis.v3.
        import json, os, subprocess, sys
        r = subprocess.run([sys.executable, "-c", REGISTRY_PROBE], capture_output=True, text=True,
                           timeout=120, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(data["before"][0], "registry should not be empty")
        self.assertEqual(data["before"], data["after"])


if __name__ == "__main__":
    unittest.main()
