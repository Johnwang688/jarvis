"""The installed HUD (decision C-1; editor plan §2.7, ED-4).

`jarvis hud install` copies a finished build into `config.HUD_DIR/<id>/`
with a SHA-256 manifest, and the daemon serves the newest build there — only
files its manifest lists, still matching. These checks are free and
hermetic: every install root, HOME and build output is a temp dir, `npm` is
never run (the CLI's build step is replaced), and nothing touches the
owner's real install root or the live daemon.

What they pin:
  - an install copies byte for byte, read-only, with a manifest that reads
    back; ids sort by time; the newest is served; old builds are pruned;
  - an asset only an older kept build has is still served (a window loaded
    before an install keeps its lazy chunks), a newer build always wins;
  - a tampered, unlisted or linked file is refused, a malformed manifest
    skips that build, a link with a build's name is never a build;
  - the install root is refused when it, or any folder above it, is a
    symbolic link, when it is inside the repository, not a folder, or
    writable by others — and the install writes nothing anywhere then;
  - a build output holding a link, a special file, an odd name, a file named
    like the manifest or no index.html is refused;
  - the CLI (`jarvis hud install`) installs into `config.HUD_DIR`, prints the
    refusal and returns 1 on a symlinked target, and `jarvis hud` dispatches
    it; `config.HUD_DIST` is the install root unless JARVIS_HUD_DEV=1;
  - no tool, MCP tool or fast-path tool can reach the installer: only the
    CLI and the daemon's read side import it.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/hud_install_check.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import hud_install as I  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
T0 = 1_791_500_000.25


def make_dist(where: Path, **files: str) -> Path:
    files = files or {"index.html": "<!doctype html><title>J.A.R.V.I.S.</title>",
                      "assets/index-1a2b.js": "console.log('HUD')",
                      "assets/editor.worker-3c4d.js": "self.onmessage = () => {}"}
    for rel, text in files.items():
        path = where / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return where


def writable(path: Path) -> None:
    for here, dirs, _ in os.walk(path):
        os.chmod(here, 0o755)
        for d in dirs:
            os.chmod(Path(here) / d, 0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hud-install-"))
        self.addCleanup(lambda: (writable(self.tmp), __import__("shutil").rmtree(self.tmp)))
        self.root = self.tmp / "share" / "jarvis" / "hud"
        self.dist = make_dist(self.tmp / "dist")
        for name, value in dict(HUD_DIR=self.root, HUD_DIST=self.root, HUD_DEV=False,
                                HUD_BUILD=self.dist).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def tree(self, top: Path) -> dict:
        return {p.relative_to(top).as_posix(): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                for p in sorted(top.rglob("*"))} if top.exists() else {}


class Install(Base):
    def test_an_install_copies_byte_for_byte_read_only_with_a_manifest(self):
        built = I.install(self.dist, self.root, now=T0)
        self.assertRegex(built.id, r"^\d{8}T\d{12}Z-[0-9a-f]{12}$")
        self.assertTrue(built.id.startswith(time.strftime("%Y%m%dT%H%M%S250000Z-", time.gmtime(T0))), built.id)
        self.assertEqual(built.path, self.root / built.id)
        for rel in ("index.html", "assets/index-1a2b.js", "assets/editor.worker-3c4d.js"):
            data = (self.dist / rel).read_bytes()
            self.assertEqual((built.path / rel).read_bytes(), data, rel)
            self.assertEqual(built.files[rel], (hashlib.sha256(data).hexdigest(), len(data)))
            self.assertEqual(stat.S_IMODE(os.stat(built.path / rel).st_mode), 0o444, rel)
        self.assertEqual(stat.S_IMODE(os.stat(built.path).st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(os.stat(built.path / "assets").st_mode), 0o555)
        manifest = json.loads((built.path / I.MANIFEST).read_text())
        self.assertEqual(manifest["build"], built.id)
        self.assertEqual(set(manifest["files"]), set(built.files))
        self.assertEqual(I.newest(self.root), built)
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith(".install-")], "no temp left")
        with self.assertRaises(PermissionError):
            (built.path / "index.html").write_text("x")       # read-only, even to a shell's `>`

    def test_the_newest_is_served_and_old_builds_are_pruned(self):
        ids = []
        for i in range(5):
            (self.dist / "index.html").write_text(f"<title>J.A.R.V.I.S.</title> v{i}")
            ids.append(I.install(self.dist, self.root, now=T0 + i).id)
        self.assertEqual(ids, sorted(ids), "ids sort by time")
        self.assertEqual(I.builds(self.root), ids[::-1][:I.KEEP])
        newest = I.newest(self.root)
        self.assertEqual(newest.id, ids[-1])
        self.assertIn(b"v4", I.read(newest, "index.html"))
        self.assertEqual(I.prune(self.root, keep=1), ids[-2:-I.KEEP - 1:-1])
        self.assertEqual(I.builds(self.root), [ids[-1]])

    def test_a_clock_behind_the_newest_build_is_refused(self):
        I.install(self.dist, self.root, now=T0 + 60)
        with self.assertRaises(I.InstallRefused) as caught:
            I.install(self.dist, self.root, now=T0)
        self.assertIn("clock", str(caught.exception))
        self.assertEqual(len(I.builds(self.root)), 1)

    def test_an_older_kept_build_serves_an_asset_the_newest_lacks(self):
        I.install(self.dist, self.root, now=T0)
        (self.dist / "assets" / "index-1a2b.js").unlink()
        make_dist(self.dist, **{"index.html": "<title>J.A.R.V.I.S.</title> new",
                                "assets/index-9f9f.js": "console.log('new')"})
        I.install(self.dist, self.root, now=T0 + 1)
        self.assertEqual(I.find(self.root, "assets/index-1a2b.js"), b"console.log('HUD')",
                         "a window loaded before the install still finds its chunk")
        self.assertEqual(I.find(self.root, "assets/index-9f9f.js"), b"console.log('new')")
        self.assertIn(b"new", I.read(I.newest(self.root), "index.html"))
        self.assertIsNone(I.find(self.root, "assets/never.js"))


class Serving(Base):
    def setUp(self):
        super().setUp()
        self.built = I.install(self.dist, self.root, now=T0)

    def poke(self, rel: str, data: bytes) -> None:
        path = self.built.path / rel
        os.chmod(path.parent, 0o755)
        os.chmod(path, 0o644)
        path.write_bytes(data)

    def test_a_tampered_file_is_refused(self):
        self.assertIsNotNone(I.read(self.built, "assets/index-1a2b.js"))
        self.poke("assets/index-1a2b.js", b"fetch('/approvals')")
        self.assertIsNone(I.read(self.built, "assets/index-1a2b.js"))
        self.assertIsNone(I.find(self.root, "assets/index-1a2b.js"))
        self.poke("assets/index-1a2b.js", b"console.log('HU')")      # same size, other bytes
        self.assertIsNone(I.read(self.built, "assets/index-1a2b.js"))

    def test_an_unlisted_file_is_never_served(self):
        os.chmod(self.built.path / "assets", 0o755)
        (self.built.path / "assets" / "planted.js").write_text("approve()")
        self.assertIsNone(I.find(self.root, "assets/planted.js"))
        self.assertIsNone(I.find(self.root, "manifest.json"))

    def test_a_link_inside_a_build_is_refused(self):
        outside = self.tmp / "outside.js"
        outside.write_text("console.log('HUD')")                    # even with matching bytes
        os.chmod(self.built.path / "assets", 0o755)
        (self.built.path / "assets" / "index-1a2b.js").unlink()
        (self.built.path / "assets" / "index-1a2b.js").symlink_to(outside)
        self.assertIsNone(I.read(self.built, "assets/index-1a2b.js"))
        os.chmod(self.built.path, 0o755)
        (self.built.path / "assets").rename(self.built.path / "real-assets")
        (self.built.path / "assets").symlink_to(self.built.path / "real-assets", target_is_directory=True)
        self.assertIsNone(I.read(self.built, "assets/editor.worker-3c4d.js"), "a linked folder too")

    def test_a_link_or_a_malformed_manifest_is_not_a_build(self):
        other = make_dist(self.tmp / "elsewhere", **{"index.html": "<title>evil</title>"})
        fake = self.root / "29991231T235959999999Z-0123456789ab"
        fake.symlink_to(other, target_is_directory=True)
        self.assertEqual(I.newest(self.root).id, self.built.id, "a link named like a build is ignored")
        fake.unlink()
        fake.mkdir()
        (fake / "index.html").write_text("<title>evil</title>")
        (fake / I.MANIFEST).write_text("{not json")
        self.assertEqual(I.newest(self.root).id, self.built.id, "a malformed manifest skips the build")
        (fake / I.MANIFEST).write_text(json.dumps({"format": 1, "build": "other", "files": {}}))
        self.assertEqual(I.newest(self.root).id, self.built.id, "a manifest naming another build too")

    def test_a_symlinked_root_serves_nothing(self):
        moved = self.tmp / "moved"
        self.root.rename(moved)
        self.root.symlink_to(moved, target_is_directory=True)
        self.assertIsNone(I.newest(self.root))
        self.assertIsNone(I.find(self.root, "assets/index-1a2b.js"))


class Refusals(Base):
    def refused(self, source, root, words):
        before = self.tree(self.tmp)
        with self.assertRaises(I.InstallRefused) as caught:
            I.install(source, root, now=T0)
        self.assertIn(words, str(caught.exception))
        self.assertEqual(self.tree(self.tmp), before, "a refused install writes nothing")
        return str(caught.exception)

    def test_a_symlinked_target_is_refused(self):
        real = self.tmp / "real-target"
        real.mkdir()
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(real, target_is_directory=True)
        self.refused(self.dist, self.root, "symbolic link")
        self.assertEqual(os.listdir(real), [], "nothing reached the link's target")

    def test_a_symlinked_folder_above_the_target_is_refused(self):
        real = self.tmp / "real-share"
        real.mkdir()
        (self.tmp / "share").symlink_to(real, target_is_directory=True)
        self.refused(self.dist, self.root, "symbolic link")
        self.assertEqual(os.listdir(real), [])

    def test_other_bad_targets_are_refused(self):
        self.assertIn("absolute", I.root_refusal(Path("relative/hud")))
        self.assertIn("'..'", I.root_refusal(self.tmp / "a" / ".." / "hud"))
        self.assertIn("repository", I.root_refusal(REPO / "hud" / "installed"))
        self.assertIn("repository", I.root_refusal(REPO))
        a_file = self.tmp / "a-file"
        a_file.write_text("x")
        self.assertIn("not a folder", I.root_refusal(a_file))
        self.assertIn("not a folder", I.root_refusal(a_file / "hud"))
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o777)
        os.chmod(loose, 0o777)
        self.assertIn("other users", I.root_refusal(loose))
        self.refused(self.dist, loose, "other users")
        self.assertIsNone(I.root_refusal(self.root), "a fresh path under real folders is fine")

    def test_a_build_output_with_a_link_or_odd_entry_is_refused(self):
        cases = []
        linked = make_dist(self.tmp / "with-link")
        (linked / "assets" / "evil.js").symlink_to(self.tmp / "with-link" / "assets" / "index-1a2b.js")
        cases.append((linked, "symbolic link"))
        dir_link = make_dist(self.tmp / "with-dir-link")
        (dir_link / "more").symlink_to(self.tmp, target_is_directory=True)
        cases.append((dir_link, "symbolic link"))
        fifo = make_dist(self.tmp / "with-fifo")
        os.mkfifo(fifo / "assets" / "pipe.js")
        cases.append((fifo, "not a regular file"))
        cases.append((make_dist(self.tmp / "odd", **{"index.html": "x", "assets/a b.js": "x"}), "unexpected name"))
        cases.append((make_dist(self.tmp / "hidden", **{"index.html": "x", ".env": "KEY=1"}), "unexpected name"))
        cases.append((make_dist(self.tmp / "manifest", **{"index.html": "x", "manifest.json": "{}"}), "manifest"))
        cases.append((make_dist(self.tmp / "no-index", **{"assets/a.js": "x"}), "index.html"))
        source_link = self.tmp / "source-link"
        source_link.symlink_to(self.dist, target_is_directory=True)
        cases.append((source_link, "not a real folder"))
        cases.append((self.tmp / "absent", "does not exist"))
        for source, words in cases:
            with self.subTest(source=source.name):
                self.refused(source, self.root, words)


class Cli(Base):
    def run_main(self, build=lambda: None):
        out: list[str] = []
        code = I.main(build=build, out=out.append)
        return code, "\n".join(out)

    def test_the_cli_installs_into_hud_dir(self):
        calls = []
        code, out = self.run_main(lambda: calls.append("built"))
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, ["built"], "it builds first")
        self.assertEqual(len(I.builds(self.root)), 1)
        self.assertIn("Installed HUD build", out)
        self.assertIn("reload the HUD window", out)

    def test_the_cli_refuses_a_symlinked_target_before_building(self):
        real = self.tmp / "real"
        real.mkdir()
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(real, target_is_directory=True)
        calls = []
        code, out = self.run_main(lambda: calls.append("built"))
        self.assertEqual(code, 1)
        self.assertIn("refused", out)
        self.assertIn("symbolic link", out)
        self.assertEqual(calls, [], "nothing is built for a target that will be refused")
        self.assertEqual(os.listdir(real), [])

    def test_a_failed_build_installs_nothing(self):
        def fail():
            raise I.InstallRefused("`npm run build` failed (exit 2); nothing was installed")
        code, out = self.run_main(fail)
        self.assertEqual(code, 1)
        self.assertIn("npm run build", out)
        self.assertEqual(I.builds(self.root), [])

    def test_jarvis_hud_install_dispatches_to_the_installer(self):
        from jarvis import __main__ as cli
        with patch.object(I, "main", return_value=7) as installer:
            self.assertEqual(cli.cmd_hud(SimpleNamespace(action="install", no_window=False)), 7)
        installer.assert_called_once_with()
        source = (REPO / "jarvis" / "__main__.py").read_text()
        self.assertRegex(source, r'choices=\["open", "install"\]')

    def test_hud_dist_is_the_install_root_unless_dev(self):
        code = ("import json; from jarvis import config; print(json.dumps([str(config.HUD_DIST), "
                "str(config.HUD_DIR), str(config.HUD_BUILD), config.HUD_DEV]))")
        home = self.tmp / "home"
        home.mkdir()
        base = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_HUD")}
        base.update(HOME=str(home), PYTHONPATH=str(REPO))

        def read(**env):
            out = subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True,
                                 text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
            self.assertEqual(out.returncode, 0, out.stderr[-500:])
            return json.loads(out.stdout.strip().splitlines()[-1])

        installed = str(home / ".local" / "share" / "jarvis" / "hud")
        self.assertEqual(read(), [installed, installed, str(REPO / "hud" / "dist"), False])
        self.assertEqual(read(JARVIS_HUD_DEV="1"), [str(REPO / "hud" / "dist"), installed,
                                                    str(REPO / "hud" / "dist"), True])
        self.assertEqual(read(JARVIS_HUD_DIR="/opt/hud")[:2], ["/opt/hud", "/opt/hud"])
        self.assertEqual(read(JARVIS_HUD_DEV="0")[3], False)


class NoLever(unittest.TestCase):
    def test_only_the_cli_and_the_daemon_read_side_import_the_installer(self):
        importers = []
        for path in sorted((REPO / "jarvis").rglob("*.py")):
            if path.name == "hud_install.py":
                continue
            if re.search(r"import\s+hud_install\b|\bhud_install\s+import\b|import\s+[\w.]*\.hud_install\b",
                         path.read_text()):
                importers.append(path.relative_to(REPO).as_posix())
        self.assertEqual(importers, ["jarvis/__main__.py", "jarvis/v2/hud_api.py"], importers)
        api = (REPO / "jarvis" / "v2" / "hud_api.py").read_text()
        for writer in ("install(", "prune(", "main(", "npm_build("):
            self.assertNotIn(f"hud_install.{writer}", api, "the daemon only reads builds")

    def test_no_registered_tool_reaches_it(self):
        from jarvis import tools
        from jarvis.v2.providers import fastpath
        names = set(tools.REGISTRY)
        self.assertFalse([n for n in names if "hud" in n.lower() or "install" in n.lower()],
                         sorted(n for n in names if "hud" in n.lower() or "install" in n.lower()))
        self.assertFalse(any("hud" in n.lower() or "install" in n.lower() for n in fastpath.FAST_TOOLS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
