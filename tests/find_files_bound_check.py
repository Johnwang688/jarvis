"""find_files has a wall-clock bound and matches as Path.glob did.

Found live on 2026-11-17: a Voice chat turn ran find_files('**/*[Cc]odex*')
over /mnt/c/Users/johnw. Over WSL's 9p mount the walk ran for minutes, and
Path.glob returns control only on a match, so nothing could stop it. Every
later voice question queued behind that turn.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_HOME = tempfile.mkdtemp(prefix="jarvis-findfiles-home-")
os.environ["HOME"] = _HOME
os.environ.pop("JARVIS_CREDENTIAL_FOLDERS", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.tools import files, search  # noqa: E402


class Clock:
    """Advances by `step` seconds every time find_files reads it."""
    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def tree(root: Path):
    for rel in ("a.py", "b.txt", "B.PY", "src/c.py", "src/d.txt", "src/deep/e.py",
                "src/deep/deeper/f.txt", "docs/c", "docs/notes/g.md", "abc/x.py",
                ".hidden/h.py", "src/.cache/i.py"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


def glob_baseline(root: Path, pattern: str):
    out = set()
    for m in root.glob(pattern):
        if any(part.startswith(".") for part in m.parts) or m == root:
            continue
        out.add(os.path.relpath(m, root))
    return out


class Matching(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ff-")).resolve()
        tree(self.root)

    def found(self, pattern):
        out = files.find_files(pattern, str(self.root))
        if out.startswith("No files matching"):
            return set()
        return set(out.splitlines())

    def test_same_matches_as_path_glob(self):
        for pattern in ("*.py", "**/*.py", "src/*.py", "src/**/*.txt", "*/c*", "**/c",
                        "[ab]*", "*.PY", "**/deep", "src/deep/*", "**/*.md", "docs/**/*.md",
                        "*", "*/*/*", "nothing*", "src/*/e.py"):
            with self.subTest(pattern=pattern):
                self.assertEqual(self.found(pattern), glob_baseline(self.root, pattern))

    def test_hidden_folders_stay_out(self):
        self.assertNotIn(".hidden/h.py", self.found("**/*.py"))
        self.assertNotIn("src/.cache/i.py", self.found("**/*.py"))

    def test_a_flat_pattern_walks_only_what_can_match(self):
        seen = []
        real = Path.walk

        def walk(self_, *args, **kwargs):
            for item in real(self_, *args, **kwargs):
                seen.append(item[0])
                yield item

        with patch.object(Path, "walk", walk):
            self.assertEqual(self.found("*.py"), {"a.py"})
        self.assertEqual(seen, [self.root])
        seen.clear()
        with patch.object(Path, "walk", walk):
            self.found("src/*.py")
        self.assertEqual(seen, [self.root, self.root / "src"])

    def test_escaping_or_absolute_patterns_are_refused(self):
        for pattern in ("../*", "src/../../*", "/etc/*", "~/x", ""):
            with self.subTest(pattern=pattern):
                self.assertTrue(files.find_files(pattern, str(self.root)).startswith("Error"))

    def test_match_cap(self):
        many = self.root / "many"
        many.mkdir()
        for i in range(files.FIND_MAX + 5):
            (many / f"m{i:03}.log").write_text("")
        out = files.find_files("many/*.log", str(self.root)).splitlines()
        self.assertEqual(len(out), files.FIND_MAX + 1)
        self.assertEqual(out[-1], "[...more matches truncated]")


class Deadline(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ff-")).resolve()
        for i in range(30):
            d = self.root / f"d{i:02}"
            d.mkdir()
            (d / ("hit.py" if i < 2 else "miss.txt")).write_text("")

    def test_a_long_walk_stops_and_keeps_its_partial_hits(self):
        # One clock read for the deadline, then one per directory: 1 s each.
        with patch.object(files, "_clock", Clock(1.0)), patch.object(files, "FIND_SECONDS", 4.5):
            out = files.find_files("**/hit.py", str(self.root))
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["d00/hit.py", "d01/hit.py"])
        self.assertIn("stopped after 4.5 s", lines[-1])
        self.assertIn("Narrow", lines[-1])

    def test_a_long_walk_with_no_match_still_returns(self):
        with patch.object(files, "_clock", Clock(1.0)), patch.object(files, "FIND_SECONDS", 2.5):
            out = files.find_files("**/*codex*", str(self.root))
        self.assertTrue(out.startswith("No files matching"))
        self.assertIn("stopped after 2.5 s", out)

    def test_a_quick_walk_is_not_cut(self):
        out = files.find_files("**/hit.py", str(self.root))
        self.assertEqual(out.splitlines(), ["d00/hit.py", "d01/hit.py"])


class GrepFallback(unittest.TestCase):
    def test_python_fallback_is_bounded_too(self):
        root = Path(tempfile.mkdtemp(prefix="ff-")).resolve()
        (root / "a.txt").write_text("needle\n")
        with patch.object(search.shutil, "which", return_value=None):
            self.assertIn("a.txt", search.grep_files("needle", str(root)))
            with patch.object(search, "TIMEOUT_S", -1):
                out = search.grep_files("needle", str(root))
        self.assertTrue(out.startswith("Error: search timed out"), out)


if __name__ == "__main__":
    unittest.main()
