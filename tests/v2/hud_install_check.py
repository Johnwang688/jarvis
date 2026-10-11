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
        # A checkout of its own, so "is hud/ dirty" and "which commit" are
        # this test's, never the worktree it runs from.
        self.repo = self.tmp / "repo"
        (self.repo / "hud" / "src").mkdir(parents=True)
        (self.repo / "hud" / "src" / "main.tsx").write_text("export {}\n")
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "Test"), ("add", "."), ("commit", "-qm", "base")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
        for name, value in dict(HUD_DIR=self.root, HUD_DIST=self.root, HUD_DEV=False,
                                HUD_BUILD=self.dist, REPO_ROOT=self.repo).items():
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
        # The same size, other bytes: only the SHA-256 can tell (review of
        # PR #44, MED-1 — a shorter tamper was caught by the size alone).
        same = b"console.log('XYZ')"
        self.assertEqual(len(same), self.built.files["assets/index-1a2b.js"][1])
        self.poke("assets/index-1a2b.js", same)
        self.assertIsNone(I.read(self.built, "assets/index-1a2b.js"))
        self.assertIsNone(I.find(self.root, "assets/index-1a2b.js"))

    def test_an_unlisted_file_is_never_served(self):
        os.chmod(self.built.path / "assets", 0o755)
        (self.built.path / "assets" / "planted.js").write_text("approve()")
        self.assertIsNone(I.find(self.root, "assets/planted.js"))
        self.assertIsNone(I.read(self.built, "assets/planted.js"), "read itself vouches only for listed files")
        self.assertIsNone(I.find(self.root, "manifest.json"))
        self.assertIsNone(I.read(self.built, "manifest.json"))

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

    def test_a_newer_build_whose_page_fails_is_passed_over(self):
        # Review of PR #44, L1: it used to answer "not installed" while
        # `/status` named the broken build.
        (self.dist / "index.html").write_text("<title>J.A.R.V.I.S.</title> second")
        second = I.install(self.dist, self.root, now=T0 + 1)
        self.assertEqual(I.state(self.root)[0].id, second.id)
        os.chmod(second.path, 0o755)
        os.chmod(second.path / "index.html", 0o644)
        (second.path / "index.html").write_text("<title>J.A.R.V.I.S.</title> SECOND")  # same size
        built, state = I.state(self.root)
        # Served, but never silently: "fallback", naming what was passed over
        # (review of PR #44 at 62db94f).
        self.assertEqual((built.id, state), (self.built.id, "fallback"), "the newest intact build")
        self.assertEqual(I.passed_over(self.root, built), [second.id])
        self.assertEqual(I.newest(self.root).id, self.built.id)
        os.chmod(self.built.path, 0o755)
        os.chmod(self.built.path / "index.html", 0o644)
        (self.built.path / "index.html").write_text("tampered")
        self.assertEqual(I.state(self.root), (None, "failed verification"),
                         "builds there, none intact: not 'not installed'")
        empty = self.tmp / "empty-root"
        empty.mkdir()
        self.assertEqual(I.state(empty), (None, "not installed"))

    def test_a_hostile_manifest_is_ignored_never_raised(self):
        # Review of PR #44, L2: 200,000 `[` overflowed the JSON parser's
        # recursion out of newest()/find() — every `/` a 409, and the
        # daemon's start-up crashed into a restart loop.
        for name, body in (("29991231T235959999999Z-0123456789ab", b"[" * 200_000),
                           ("29991231T235959999998Z-0123456789ab", b" " * (I.MANIFEST_CAP + 1)),
                           ("29991231T235959999997Z-0123456789ab", b'{"format": 1, "build": [}')):
            folder = self.root / name
            os.chmod(self.root, 0o755)
            folder.mkdir()
            (folder / I.MANIFEST).write_bytes(body)
            (folder / "index.html").write_text("<title>evil</title>")
            with self.subTest(name=name):
                self.assertIsNone(I.load(self.root, name))
        self.assertEqual(I.newest(self.root).id, self.built.id)
        self.assertEqual(I.state(self.root)[0].id, self.built.id)
        self.assertIsNotNone(I.find(self.root, "assets/index-1a2b.js"))
        from jarvis.v2 import daemon as D, hud_api as HA
        self.assertEqual(HA.hud_served()["build"], self.built.id)
        D._log_hud_served()                      # never raises
        with patch.object(I, "state", side_effect=RecursionError("deep")):
            D._log_hud_served()                  # not even when reading the root does

    def test_read_uses_the_descriptor_it_read(self):
        # Review of PR #44, L3: no second stat — the fstat of the very fd
        # whose bytes were hashed. With os.stat broken, read() still works.
        with patch("os.stat", side_effect=OSError("no separate stat")):
            self.assertIsNotNone(I.read(self.built, "assets/index-1a2b.js"))

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
        self.assertIn("repository", I.root_refusal(self.repo / "hud" / "installed"))
        self.assertIn("repository", I.root_refusal(self.repo))
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

    def test_uncommitted_hud_changes_are_refused_unless_allowed(self):
        # Review of PR #44, L5: Jarvis's chats leave edits in the checkout.
        (self.repo / "hud" / "src" / "main.tsx").write_text("export const planted = 1\n")
        calls = []
        code, out = self.run_main(lambda: calls.append("built"))
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)
        self.assertEqual(calls, [], "nothing is built from a dirty hud/")
        self.assertEqual(I.builds(self.root), [])
        (self.repo / "hud" / "src" / "new.ts").write_text("x\n")          # untracked counts too
        out_lines: list[str] = []
        self.assertEqual(I.main(build=lambda: None, out=out_lines.append, allow_dirty=True), 0)
        built = I.newest(self.root)
        self.assertIs(built.dirty, True)
        self.assertIn("uncommitted", "\n".join(out_lines))
        # Changes outside hud/ do not count.
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "--", "hud"], check=True)
        (self.repo / "hud" / "src" / "new.ts").unlink()
        (self.repo / "README.md").write_text("elsewhere\n")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)

    def test_the_manifest_records_the_commit_and_a_clean_tree(self):
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        built = I.newest(self.root)
        self.assertEqual((built.git_head, built.dirty), (head, False))
        manifest = json.loads((built.path / I.MANIFEST).read_text())
        self.assertEqual((manifest["git_head"], manifest["dirty"]), (head, False))
        self.assertIn(head[:12], out)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL).stdout.strip()

    def test_an_edit_during_the_build_is_never_recorded_clean(self):
        def edit_mid_build():
            (self.repo / "hud" / "src" / "main.tsx").write_text("export const late = 1\n")
        code, out = self.run_main(edit_mid_build)
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)
        self.assertEqual(I.builds(self.root), [], "nothing installed")
        lines: list[str] = []
        self.assertEqual(I.main(build=edit_mid_build, out=lines.append, allow_dirty=True), 0)
        self.assertIs(I.newest(self.root).dirty, True, "clean before the build, dirty after: dirty")

    def test_a_checkout_that_moves_during_the_build_is_refused(self):
        def commit_mid_build():
            (self.repo / "README.md").write_text("moved\n")
            self.git("add", "README.md")
            self.git("commit", "-qm", "moved")
        code, out = self.run_main(commit_mid_build)
        self.assertEqual(code, 1)
        self.assertIn("moved while it built", out)
        self.assertEqual(I.builds(self.root), [])

    def test_git_runs_hardened(self):
        # A repository's own fsmonitor program ran under the plain `git -C
        # REPO status` (review of PR #44 at 62db94f).
        marker = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))
        subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True,
                       stdin=subprocess.DEVNULL)
        self.assertTrue(marker.exists(), "sanity: plain git runs it")
        marker.unlink()
        self.assertIs(I.hud_dirty(), False)
        self.assertIsNotNone(I.git_head())
        self.assertFalse(marker.exists(), "the installer's git never runs the repository's fsmonitor")
        # A program in the repository's own config: not read at all.
        self.git("config", "filter.x.clean", str(hook))
        self.assertIsNone(I.hud_dirty())
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot say", out)
        self.assertFalse(marker.exists())
        self.git("config", "--unset", "filter.x.clean")
        with patch.dict(os.environ, {"GIT_DIR": str(self.tmp / "nowhere")}):
            self.assertIs(I.hud_dirty(), False, "an inherited GIT_DIR is dropped")

    def test_the_start_up_warning_looks_at_hud_only(self):
        first = self.git("rev-parse", "HEAD")
        (self.repo / "README.md").write_text("python only\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "python only")
        self.assertIs(I.hud_changed_since(first), False, "a merge outside hud/ leaves the HUD current")
        (self.repo / "hud" / "src" / "main.tsx").write_text("export const v2 = 1\n")
        self.git("commit", "-qam", "hud change")
        self.assertIs(I.hud_changed_since(first), True)
        self.assertIsNone(I.hud_changed_since("0" * 40), "an unknown commit: cannot say")
        self.assertIsNone(I.hud_changed_since(None))

    def test_jarvis_hud_install_dispatches_to_the_installer(self):
        from jarvis import __main__ as cli
        with patch.object(I, "main", return_value=7) as installer:
            self.assertEqual(cli.cmd_hud(SimpleNamespace(action="install", no_window=False)), 7)
            self.assertEqual(cli.cmd_hud(SimpleNamespace(action="install", no_window=False,
                                                         allow_dirty=True)), 7)
        self.assertEqual(installer.call_args_list[0].kwargs, {"allow_dirty": False})
        self.assertEqual(installer.call_args_list[1].kwargs, {"allow_dirty": True})
        # The owner's CLI still parses the command (MED-2 refuses it to Jarvis only).
        out = subprocess.run([sys.executable, "-m", "jarvis", "hud", "--help"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                             env={**os.environ, "HOME": str(self.tmp / "home"), "PYTHONPATH": str(REPO)})
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        self.assertIn("install", out.stdout)
        self.assertIn("--allow-dirty", out.stdout)
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


class NoAgentInstall(Base):
    """Review of PR #44, MED-2: `jarvis hud install` is the owner's and the
    lead's, never Jarvis's — DENY in every spelling, through one detector
    (`protected_state.installs_hud`, asked by `rules.decide`, so v1's gate and
    v2's layer 1 alike) — and a shell write into the install root asks."""

    INSTALLS = (
        "jarvis hud install", "jarvis hud install --allow-dirty", "jarvis hud --no-window install",
        "python -m jarvis hud install", "python3 -mjarvis hud install",
        "/home/x/projects/Jarvis/.venv/bin/jarvis hud install", "~/.local/bin/jarvis hud install",
        ".venv/bin/python -m jarvis.__main__ hud install", "python3 -m jarvis.v2.hud_install",
        "python jarvis/v2/hud_install.py", "uv run jarvis hud install",
        "uv run --directory /x python -m jarvis hud install",
        "env JARVIS_HUD_DIR=/tmp/x nohup timeout 600 jarvis hud install",
        "systemd-run --user --scope -q -- /x/.venv/bin/python -m jarvis hud install",
        "bash -lc 'cd ~/projects/Jarvis && jarvis hud install'", 'sh -c "jarvis hud install"',
        "flock /tmp/l bash -c 'jarvis hud install'", "eval jarvis hud install",
        'env -S "jarvis hud install"', "J=jarvis; $J hud install", "echo $(jarvis hud install)",
        "git pull && jarvis hud install", "jarvis \\\n hud install", "ssh localhost jarvis hud install",
        'python -c "from jarvis.v2 import hud_install; hud_install.main()"',
        "python3 - <<'EOF'\nfrom jarvis.v2 import hud_install\nhud_install.main()\nEOF",
        'python -c "import jarvis.__main__ as m; m.main([\'hud\', \'install\'])"',
    )
    INSTALLS += (
        # Review of PR #44 at 62db94f, the cheap additions.
        "ln -s /x/.venv/bin/jarvis /tmp/jv && /tmp/jv hud install",
        "echo install | xargs jarvis hud", "printf 'hud\\ninstall\\n' | xargs jarvis",
        "cat args.txt | xargs -n2 /x/.venv/bin/jarvis",
        # Obfuscated, and resolved the way the shell resolves them.
        "j=jar; ${j}vis hud install", "jarvis hud ins\\tall", 'eval "jar""vis hud install"',
        "x=h; jarvis ${x}ud install", "jarvis $'\\x68ud' install", "jarvis 'h'\"u\"d install",
        "bash <<EOF\njarvis hud install\nEOF", 'git commit -m "run `jarvis hud install` after"',
        "cat > notes.md <<EOF\nrun `jarvis hud install`\nEOF", "cat <(jarvis hud install)",
        'python3 -c "import os; os.system(\'jarvis hud install\')"',
        'python3 -c "import subprocess; subprocess.run([\'jarvis\', \'hud\', \'install\'])"',
        'python3 -c "import runpy, sys; sys.argv = [\'jarvis\', \'hud\', \'install\']; '
        'runpy.run_module(\'jarvis\', run_name=\'__main__\')"',
        'python3 -c "from jarvis.v2.hud_install import main as go; go()"',
        'python3 -c "import jarvis.v2.hud_install as h; h.install(1, 2)"',
    )
    ORDINARY = (
        "jarvis hud", "jarvis hud --no-window", "jarvis install hud", "cat jarvis/v2/hud_install.py",
        "grep -n main jarvis/v2/hud_install.py", "python tests/v2/hud_install_check.py",
        "python -m pytest tests/v2/hud_install_check.py", 'git commit -m "docs: jarvis hud install after merges"',
        'echo "jarvis hud install"', "npm run build", "ls ~/.local/share/jarvis/hud",
        # The five a differential at 62db94f found DENY: a DENY cannot be
        # approved, so a false one blocks the owner too.
        'grep -rn "hud_install.main" jarvis/ && python3 tests/v2/hud_install_check.py',
        "rg -n 'import hud_install' jarvis && python -m pytest tests",
        "echo Run jarvis hud install after merging",
        'python3 -c "from jarvis.v2 import hud_install as I; print(I.builds(\'/tmp/x\'))"',
        'python3 -c "print(\'hud\', \'install\')"',
        # Text that never runs: single quotes, a quoted here-document, data.
        "git commit -m 'run `jarvis hud install` after merges'", "echo 'see $(jarvis hud install)'",
        "cat > notes.md <<'EOF'\nrun `jarvis hud install`\nEOF", "cat > notes.md <<EOF\njarvis hud install\nEOF",
        "xargs -n1 echo < files.txt", "ls | xargs wc -l", "echo jarvis | xargs printf",
    )

    def test_the_cheap_filter_never_skips_a_line_the_parse_refuses(self):
        # Review of PR #44 at 62db94f: the parse made `rules.decide` 7x
        # slower on nested substitutions; the filter in front of it must
        # never let a refusal through. Over every spelling here and every
        # string literal of the gate suites.
        from jarvis import protected_state as S
        corpus = list(self.INSTALLS) + list(self.ORDINARY)
        import ast as _ast
        for name in ("tests/rules_check.py", "tests/v2/permissions_check.py", "tests/v2/hud_install_check.py",
                     "tests/permissions_check.py"):
            tree = _ast.parse((REPO / name).read_text())
            corpus += [n.value for n in _ast.walk(tree)
                       if isinstance(n, _ast.Constant) and isinstance(n.value, str) and len(n.value) < 4000]
        corpus.append("ln -s /x/jarvis /tmp/jv && x=h && /tmp/jv ${x}ud install")
        refused = 0
        for line in corpus:
            full = S.owner_op(line, prefilter=False)       # neither the line's nor a segment's filter
            if full:
                refused += 1
                self.assertTrue(S._could_be_owner_op(line), f"the line filter skips {line!r}")
                self.assertTrue(S.owner_op(line), f"the filters together skip {line!r}")
        self.assertGreater(refused, len(self.INSTALLS) - 1)

    def test_every_spelling_is_refused_and_never_asked(self):
        from jarvis import permissions as P, rules
        from jarvis import protected_state as S
        for line in self.INSTALLS:
            with self.subTest(line=line):
                self.assertTrue(S.installs_hud(line), "the detector sees it")
                self.assertEqual(rules.decide(line).decision, rules.DENY)
                self.assertEqual(P.static_verdict(line).decision, rules.DENY)
                self.assertEqual(P.command_verdict("run_command", {"command": line}).decision, rules.DENY)

    def test_ordinary_lines_are_not_refused_for_it(self):
        from jarvis import rules
        from jarvis import protected_state as S
        for line in self.ORDINARY:
            with self.subTest(line=line):
                self.assertIsNone(S.installs_hud(line))
                self.assertNotEqual(rules.decide(line).decision, rules.DENY)

    def test_run_command_refuses_it_even_with_no_approver(self):
        from jarvis.tools import shell
        ran = []
        with patch.object(shell, "_run", side_effect=lambda c: ran.append(c) or "ran"):
            out = shell.run_command("python -m jarvis hud install", reason="install the HUD")
        self.assertIn("Refused", out)
        self.assertEqual(ran, [])

    def test_a_shell_write_into_the_install_root_asks(self):
        from jarvis import permissions as P, rules
        built = I.install(self.dist, self.root, now=T0)
        for line in (f"cp /tmp/x.js {built.path}/assets/x.js", f"rsync -a /tmp/dist/ {self.root}/new/",
                     f"echo pwned > {built.path}/index.html", f"mv /tmp/b {self.root}/b",
                     f"rm -rf {self.root.parent}"):
            with self.subTest(line=line):
                verdict = P.static_verdict(line)
                self.assertEqual(verdict.decision, rules.ASK, verdict)
                self.assertIn("installed HUD", verdict.reason)
        # Readers stay reads.
        self.assertEqual(P.static_verdict(f"cat {built.path}/index.html").decision, rules.ALLOW)

    def test_no_allowlist_entry_covers_a_write_into_it(self):
        from jarvis import permissions as P
        built = I.install(self.dist, self.root, now=T0)
        allow = self.tmp / "allowlist.json"
        allow.write_text(json.dumps([{"tool": "run_command", "prefix": "cp"},
                                     {"tool": "run_command", "prefix": "jarvis"},
                                     {"tool": "run_command", "prefix": "python"}]))
        with patch.object(config, "ALLOWLIST_PATH", allow):
            self.assertFalse(P.allows("run_command", {"command": f"cp /tmp/x {built.path}/x.js"}))
            self.assertTrue(P.allows("run_command", {"command": "cp /tmp/a /tmp/b"}), "sanity")

    def test_the_write_tools_refuse_the_install_root_in_dev_mode_too(self):
        # Review of PR #44, L4: a process with JARVIS_HUD_DEV=1 serves
        # hud/dist, and its tools used to leave the install root writable.
        from jarvis.tools import files
        dev_dist = self.tmp / "repo-dist"
        dev_dist.mkdir()
        with patch.object(config, "HUD_DEV", True), patch.object(config, "HUD_DIST", dev_dist):
            for target in (self.root / "b" / "index.html", dev_dist / "index.html"):
                with self.subTest(target=target):
                    self.assertTrue(files._hud_build(target.resolve()))
            self.assertFalse(files._hud_build((self.tmp / "elsewhere.js").resolve()))


class NoLever(unittest.TestCase):
    def test_only_the_cli_and_the_daemon_read_side_import_the_installer(self):
        importers = []
        for path in sorted((REPO / "jarvis").rglob("*.py")):
            if path.name == "hud_install.py":
                continue
            if re.search(r"import\s+hud_install\b|\bhud_install\s+import\b|import\s+[\w.]*\.hud_install\b",
                         path.read_text()):
                importers.append(path.relative_to(REPO).as_posix())
        self.assertEqual(importers, ["jarvis/__main__.py", "jarvis/v2/daemon.py", "jarvis/v2/hud_api.py"],
                         importers)
        for name in ("hud_api.py", "daemon.py"):
            text = (REPO / "jarvis" / "v2" / name).read_text()
            for writer in ("install(", "prune(", "main(", "npm_build("):
                self.assertNotIn(f"hud_install.{writer}", text, f"{name} only reads builds")

    def test_no_registered_tool_reaches_it(self):
        from jarvis import tools
        from jarvis.v2.providers import fastpath
        names = set(tools.REGISTRY)
        self.assertFalse([n for n in names if "hud" in n.lower() or "install" in n.lower()],
                         sorted(n for n in names if "hud" in n.lower() or "install" in n.lower()))
        self.assertFalse(any("hud" in n.lower() or "install" in n.lower() for n in fastpath.FAST_TOOLS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
