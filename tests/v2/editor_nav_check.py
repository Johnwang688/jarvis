"""The editor's navigation routes (ED-7): quick open's list, find in files,
and the explorer's new file, new folder, rename and delete.

Free and hermetic, on `editor_files_check`'s world: a real daemon on three
ephemeral loopback ports (never 8402, 8403 or 8405), every config path, HOME
and the trash (`XDG_DATA_HOME`) temporary, fake credentials only.

What it pins:

  - **search parity**: `grep_files` (ripgrep and the Python fallback) and the
    route refuse exactly the same protected and env files, through one
    function (`tools.search.refused`);
  - **quick open** lists dotfiles but never a credential folder, the gate's
    state, a withheld file, a symlink out of the project or `SKIP_DIRS`, in a
    repository (gitignore respected) and out of one, within its bounds;
  - **find in files** finds, bounds itself (2,000 matches, 10 s), respects
    `.gitignore` with a toggle, never shows a withheld, gate or env hit, and
    is refused on the Inbox;
  - **fs** refuses protected names, escapes, symlinks out, the gate's state,
    SELF_PROTECTED, git's internals and the HUD install root (and folders
    that hold any of them); a rename never replaces; an env file keeps an env
    name; a delete lands in the (temp) trash with a `.trashinfo`, never an
    unlink;
  - **owner-only**: all three refuse the API listener and a bare request,
    and do nothing when refused.

Run: PYTHONPATH=. .venv/bin/python tests/v2/editor_nav_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from editor_files_check import FAKE_SECRET, Daemonised  # noqa: E402

from jarvis import config, runtime  # noqa: E402
from jarvis.tools import search as S  # noqa: E402
from jarvis.v2 import editor_nav as N  # noqa: E402

NEEDLE = "NEEDLE_ED7"


class Nav(Daemonised):
    def files(self, status=200, url=None):
        return self.owner("GET", f"{url or self.url}/files", status=status)

    def search(self, status=200, url=None, **body):
        return self.owner("POST", f"{url or self.url}/search", body, status=status)

    def fs(self, op, path, to=None, status=200, url=None):
        body = {"op": op, "path": path}
        if to is not None:
            body["to"] = to
        return self.owner("POST", f"{url or self.url}/fs", body, status=status)

    def project_at(self, root: Path, name="P"):
        project = self.daemon.stores.projects.create(name, str(root))
        return f"/projects/{project.id}"

    def git(self, root: Path, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# quick open
# ---------------------------------------------------------------------------

class QuickOpen(Nav):
    def setUp(self):
        super().setUp()
        h = self.home
        (h / ".gitignore").write_text("build/\n")
        (h / ".github" / "workflows").mkdir(parents=True)
        (h / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (h / "node_modules" / "pkg").mkdir(parents=True)
        (h / "node_modules" / "pkg" / "index.js").write_text("x\n")
        (h / "linked-in").symlink_to(h / "notes.txt")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("out\n")
        (h / "linked-out").symlink_to(outside / "secret.txt")
        (h / "dir-link").symlink_to(outside)

    def test_lists_dotfiles_never_credentials_or_gate(self):
        got = self.files()
        names = set(got["files"])
        self.assertEqual(got["source"], "walk")
        for want in ("notes.txt", ".gitignore", ".github/workflows/ci.yml", "app/main.py",
                     "app/.env.example", "app/.env.production", "linked-in", ".gitconfig"):
            self.assertIn(want, names)
        self.assertIn("app/.env.production", got["env"])
        self.assertNotIn("app/.env.example", got["env"])
        for never in (".ssh/id_test", ".ssh/.env", ".claude/.credentials.json", ".codex/auth.json",
                      ".git-credentials", "google_token.json", ".config/jarvis/allowlist.json",
                      ".config/jarvis/discord_token.json", "node_modules/pkg/index.js",
                      "linked-out", "dir-link/secret.txt"):
            self.assertNotIn(never, names)
        self.assertFalse(any(n.startswith((".ssh/", ".config/jarvis/")) for n in names), names)
        self.assertFalse(got["truncated"])

    def test_bounded(self):
        with patch.object(N, "LIST_CAP", 3):
            got = self.files()
        self.assertEqual(len(got["files"]), 3)
        self.assertTrue(got["truncated"])

    def test_a_repository_lists_what_git_does(self):
        repo = self.home / "proj"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.cpp").write_text("int main() {}\n")
        (repo / ".gitignore").write_text("build/\n")
        (repo / "build").mkdir()
        (repo / "build" / "out.o").write_text("bin\n")
        (repo / ".clang-format").write_text("x\n")
        (repo / "google_token.json").write_text('{"t": "fake"}')
        self.git(repo, "init", "-q")
        self.git(repo, "add", "src/main.cpp", ".gitignore")
        self.git(repo, "commit", "-qm", "x")
        (repo / "untracked.h").write_text("//\n")
        url = self.project_at(repo)
        got = self.files(url=url)
        self.assertEqual(got["source"], "git")
        names = set(got["files"])
        self.assertEqual(names, {"src/main.cpp", ".gitignore", ".clang-format", "untracked.h"})

    def test_a_credential_folder_root_is_refused(self):
        url = self.project_at(self.home / ".ssh", "Keys")
        self.files(url=url, status=403)


# ---------------------------------------------------------------------------
# find in files, and its parity with grep_files
# ---------------------------------------------------------------------------

PARITY_NAMES = ("plain.txt", "main.py", ".env", ".env.local", ".env.production", ".env.staging",
                ".ENV.Local", ".env.example", ".env.local.example", "google_token.json",
                "discord_token.json", "spotify_token.json", "onshape_keys.json", "myapp.env", ".envrc")


class Search(Nav):
    def setUp(self):
        super().setUp()
        self.par = self.home / "par"
        self.par.mkdir()
        for name in PARITY_NAMES:
            (self.par / name).write_text(f"x = '{NEEDLE}'\n")
        self.par_url = self.project_at(self.par, "Par")

    def grep_names(self, use_rg: bool) -> set[str]:
        """Which fixture files `grep_files` shows a match from, each asked by
        itself (a single file is searched even when hidden), on one backend."""
        found = set()
        which = shutil.which if use_rg else (lambda name: None)
        with patch.object(S.shutil, "which", which), runtime.tools_bound({"grep_files"}):
            for name in PARITY_NAMES:
                for mode in ("content", "files", "count"):
                    out = S.grep_files(NEEDLE, str(self.par / name), mode=mode)
                    if not out.startswith(("No matches", "Error")):
                        found.add(name)
        return found

    def route_names(self, use_rg: bool) -> set[str]:
        real = N.search_bounded

        def forced(*a, **kw):
            kw["use_rg"] = use_rg
            return real(*a, **kw)

        with patch.object(N, "search_bounded", forced):
            got = self.search(url=self.par_url, query=NEEDLE)
        return {m["path"] for m in got["matches"]}

    def test_parity_with_grep_files(self):
        self.assertIs(N.search_refused, S.refused, "one filter, not a copy")
        refused = {n for n in PARITY_NAMES if S.refused(self.par / n)}
        self.assertEqual(refused, {".env", ".env.local", ".env.production", ".env.staging", ".ENV.Local",
                                   "google_token.json", "discord_token.json", "spotify_token.json",
                                   "onshape_keys.json"})
        everything = set(PARITY_NAMES)
        backends = [False] + ([True] if shutil.which("rg") else [])
        if not shutil.which("rg"):
            print("PARITY NOT VERIFIED ON RIPGREP: rg is not installed")
        for use_rg in backends:
            with self.subTest(rg=use_rg):
                grep = self.grep_names(use_rg)
                route = self.route_names(use_rg)
                self.assertEqual(everything - grep, refused, f"grep_files refused {everything - grep}")
                self.assertEqual(everything - route, refused, f"the route refused {everything - route}")
                self.assertEqual(grep, route)

    def test_finds_with_ranges_and_bounds(self):
        (self.par / "two.txt").write_text(f"a {NEEDLE} b {NEEDLE}\n\nno\n{NEEDLE.lower()}\n")
        got = self.search(url=self.par_url, query=NEEDLE, case=True)
        rows = [m for m in got["matches"] if m["path"] == "two.txt"]
        self.assertEqual([(m["line"], m["ranges"]) for m in rows], [(1, [[2, 12], [15, 25]])])
        got = self.search(url=self.par_url, query=NEEDLE)
        self.assertEqual(sorted(m["line"] for m in got["matches"] if m["path"] == "two.txt"), [1, 4])
        with patch.object(N, "SEARCH_CAP", 2):
            got = self.search(url=self.par_url, query=NEEDLE)
        self.assertEqual(len(got["matches"]), 2)
        self.assertTrue(got["truncated"])
        got = self.search(url=self.par_url, query="needle_ed7", word=True)
        self.assertTrue(got["matches"])
        got = self.search(url=self.par_url, query="NEEDLE_E", word=True)
        self.assertEqual(got["matches"], [])
        got = self.search(url=self.par_url, query=r"NEED(LE)_ED\d?7", regex=True)
        self.assertTrue(got["matches"])
        self.search(url=self.par_url, query="(unclosed", regex=True, status=400)
        got = self.search(url=self.par_url, query="a.b")  # a literal, not a pattern
        self.assertEqual(got["matches"], [])

    def test_gitignore_respected_with_a_toggle(self):
        repo = self.home / "repo2"
        (repo / "build").mkdir(parents=True)
        (repo / "src.c").write_text(f"{NEEDLE}\n")
        (repo / "build" / "gen.c").write_text(f"{NEEDLE}\n")
        (repo / ".gitignore").write_text("build/\n")
        self.git(repo, "init", "-q")
        url = self.project_at(repo, "R2")
        self.assertEqual({m["path"] for m in self.search(url=url, query=NEEDLE)["matches"]}, {"src.c"})
        self.assertEqual({m["path"] for m in self.search(url=url, query=NEEDLE, ignore=False)["matches"]},
                         {"src.c", "build/gen.c"})

    def test_never_withheld_gate_env_or_out(self):
        (self.home / ".ssh" / "found.txt").write_text(f"{NEEDLE}\n")
        (self.home / ".config" / "jarvis" / "allowlist.json").write_text(f'["{NEEDLE}"]\n')
        (self.home / "app" / ".env.production").write_text(f"API_KEY={FAKE_SECRET}\nX={NEEDLE}\n")
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "o.txt").write_text(f"{NEEDLE}\n")
        (self.home / "out-link.txt").symlink_to(outside / "o.txt")
        (self.home / "key-link.txt").symlink_to(self.home / ".ssh" / "found.txt")
        for use_rg in [False] + ([True] if shutil.which("rg") else []):
            real = N.search_bounded
            with self.subTest(rg=use_rg), patch.object(
                    N, "search_bounded", lambda *a, _r=use_rg, **kw: real(*a, **{**kw, "use_rg": _r})):
                got = self.search(query=NEEDLE)
                paths = {m["path"] for m in got["matches"]}
                self.assertIn("par/plain.txt", paths)
                for never in (".ssh/found.txt", ".config/jarvis/allowlist.json", "app/.env.production",
                              "out-link.txt", "key-link.txt"):
                    self.assertNotIn(never, paths)
                got = self.search(query=FAKE_SECRET)
                self.assertEqual(got["matches"], [])
        self.assertNotIn(FAKE_SECRET, self.bus_text())

    def test_refused_on_the_inbox(self):
        inbox = self.daemon.stores.projects.inbox()
        got = self.search(url=f"/projects/{inbox.id}", query=NEEDLE, status=400)
        self.assertIn("Inbox", json.dumps(got))

    def test_bad_bodies(self):
        for body in ({}, {"query": ""}, {"query": "x" * 1001}, {"query": "x", "case": "yes"},
                     {"query": "x", "other": 1}):
            with self.subTest(body=body):
                self.owner("POST", f"{self.par_url}/search", body, status=400)


# ---------------------------------------------------------------------------
# the explorer's operations
# ---------------------------------------------------------------------------

class Fs(Nav):
    def setUp(self):
        super().setUp()
        self.trash = self.root / "xdg" / "Trash"

    def test_new_file_and_folder(self):
        got = self.fs("new_file", "app/new.py")
        self.assertEqual((got["path"], got["kind"]), ("app/new.py", "text"))
        self.assertEqual((self.home / "app" / "new.py").read_bytes(), b"")
        self.assertEqual((self.home / "app" / "new.py").stat().st_mode & 0o777, 0o644)
        self.fs("new_file", "app/new.py", status=409)
        self.fs("new_folder", "app/deep/er")
        self.assertTrue((self.home / "app" / "deep" / "er").is_dir())
        self.fs("new_file", "app/x/y/z.txt")
        self.assertTrue((self.home / "app" / "x" / "y" / "z.txt").is_file())
        got = self.fs("new_file", "app/.env.local")
        self.assertEqual(got["kind"], "env")
        self.assertEqual((self.home / "app" / ".env.local").stat().st_mode & 0o777, 0o600)

    def test_rename_never_replaces(self):
        (self.home / "app" / "a.txt").write_text("A\n")
        (self.home / "app" / "b.txt").write_text("B\n")
        self.fs("rename", "app/a.txt", "app/b.txt", status=409)
        self.assertEqual(((self.home / "app" / "a.txt").read_text(), (self.home / "app" / "b.txt").read_text()),
                         ("A\n", "B\n"))
        got = self.fs("rename", "app/a.txt", "app/c.txt")
        self.assertEqual((got["path"], got["from"]), ("app/c.txt", "app/a.txt"))
        self.assertEqual((self.home / "app" / "c.txt").read_text(), "A\n")
        self.assertFalse((self.home / "app" / "a.txt").exists())
        self.fs("rename", "app", "app/inside", status=400)
        self.fs("rename", "app/c.txt", "nowhere/c.txt", status=400)

    def test_env_files_keep_an_env_name(self):
        got = self.fs("rename", "app/.env.production", "app/notes-from-env.txt", status=403)
        self.assertIn("env", json.dumps(got))
        self.assertTrue((self.home / "app" / ".env.production").exists())
        got = self.fs("rename", "app/.env.production", "app/.env.staging")
        self.assertEqual(got["kind"], "env")
        (self.home / "app" / "plain.txt").write_text("x\n")
        self.assertEqual(self.fs("rename", "app/plain.txt", "app/.env.dev")["kind"], "env")

    def test_delete_goes_to_the_trash(self):
        (self.home / "app" / "gone.txt").write_text("keep me\n")
        with patch("os.unlink", side_effect=AssertionError("unlink")), \
                patch("os.remove", side_effect=AssertionError("remove")):
            got = self.fs("delete", "app/gone.txt")
        self.assertEqual(got["trash"]["where"], "linux")
        self.assertFalse((self.home / "app" / "gone.txt").exists())
        self.assertEqual((self.trash / "files" / "gone.txt").read_text(), "keep me\n")
        info = (self.trash / "info" / "gone.txt.trashinfo").read_text()
        self.assertIn("[Trash Info]", info)
        self.assertIn("Path=" + str(self.home / "app" / "gone.txt"), info)
        (self.home / "app" / "sub").mkdir()
        (self.home / "app" / "sub" / "f.txt").write_text("f\n")
        self.fs("delete", "app/sub")
        self.assertEqual((self.trash / "files" / "sub" / "f.txt").read_text(), "f\n")
        self.fs("delete", "app/nothing-here", status=404)

    def test_refusals(self):
        h = self.home
        outside = self.root / "out"
        outside.mkdir()
        (outside / "o.txt").write_text("o\n")
        (h / "out-link").symlink_to(outside)
        (h / "file-out").symlink_to(outside / "o.txt")
        (h / "holder").mkdir()
        (h / "holder" / "google_token.json").write_text('{"t": "fake"}')
        refused = [
            ("new_file", "google_token.json", None, 403),          # a protected name
            ("new_file", "app/discord_token.json", None, 403),
            ("delete", "google_token.json", None, 403),
            ("rename", "app/main.py", "app/google_token.json", 403),
            ("rename", "google_token.json", "app/x.json", 403),
            ("new_file", "../escape.txt", None, 403),             # an escape
            ("rename", "app/main.py", "../escape.py", 403),
            ("new_file", "out-link/x.txt", None, 403),            # a symlink out
            ("delete", "file-out", None, 403),
            ("rename", "file-out", "f2", 403),
            ("delete", ".config/jarvis/allowlist.json", None, 403),  # the gate's state
            ("rename", ".config/jarvis/allowlist.json", "a.json", 403),
            ("new_file", ".config/jarvis/models.json", None, 403),
            ("delete", ".ssh", None, 403),                         # a credential folder
            ("new_file", ".ssh/x", None, 403),
            ("delete", ".claude/.credentials.json", None, 403),    # a login file
            ("delete", ".gitconfig", None, 403),                   # git's own configuration
            ("delete", "holder", None, 403),                       # a folder holding a credential
            ("delete", ".config", None, 403),                      # … or the gate's state
            ("new_file", "node_modules/x.js", None, 403),          # SKIP_DIRS
            ("new_file", "a‮b.txt", None, 400),               # invisible
            ("new_file", "a\nb", None, 400),
            ("new_file", "/etc/x", None, 400),
            ("delete", "", None, 400),
            ("delete", ".", None, 400),
        ]
        before = sorted(str(p) for p in h.rglob("*"))
        for op, path, to, status in refused:
            with self.subTest(op=op, path=path, to=to):
                self.fs(op, path, to, status=status)
        self.assertEqual(sorted(str(p) for p in h.rglob("*")), before)
        self.assertEqual((outside / "o.txt").read_text(), "o\n")
        self.assertFalse(self.trash.exists())

    def test_self_protected_git_and_the_hud(self):
        repo = self.repo
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n")
        (repo / "jarvis" / "other.py").write_text("x\n")
        url = self.project_at(repo, "Jarvis")
        for op, path, to in (("delete", "jarvis/tools/secrets.py", None),
                             ("rename", "jarvis/tools/secrets.py", "jarvis/tools/s2.py", ),
                             ("new_file", "jarvis/tools/secrets.py", None),
                             ("delete", "jarvis", None),
                             ("rename", "jarvis", "jarvis2"),
                             ("delete", ".git/config", None),
                             ("new_file", ".git/hooks/pre-commit", None),
                             ("delete", "hud/dist/index.html", None),
                             ("new_file", "hud/dist/evil.js", None),
                             ("delete", "hud", None)):
            with self.subTest(op=op, path=path):
                self.fs(op, path, to, status=403, url=url)
        self.assertTrue((repo / "jarvis" / "tools" / "secrets.py").exists())
        self.assertFalse((repo / "hud" / "dist" / "evil.js").exists())
        self.fs("delete", "jarvis/other.py", url=url)  # an ordinary file beside them

    def test_a_folder_holding_the_hud_install_root_past_the_walk(self):
        # The walk never enters SKIP_DIRS; the absolute anchors still see what
        # sits below one: the HUD install root here.
        holder = self.home / "tools"
        hud = holder / "node_modules" / "hud-install"
        hud.mkdir(parents=True)
        (hud / "index.html").write_text("<title>x</title>")
        (holder / "readme.txt").write_text("r\n")
        with patch.object(config, "HUD_DIR", hud):
            self.fs("delete", "tools", status=403)
            self.fs("rename", "tools", "tools2", status=403)
        self.assertTrue((hud / "index.html").exists())

    def test_windows_names_are_judged_where_the_path_lands(self):
        # LOW (review of PR #53): by the resolved target, not the project root.
        for bad in ("CON.txt", "a.", "b ", "c?d", "dir/NUL"):
            with self.subTest(bad=bad), self.assertRaises(Exception) as caught:
                N._check_windows(bad, Path("/mnt/c/Users/x/Robotics") / bad)
            self.assertEqual(getattr(caught.exception, "status", None), 400)
        N._check_windows("src/main.cpp", Path("/mnt/c/Users/x/Robotics/src/main.cpp"))
        N._check_windows("CON.txt", self.home / "CON.txt")  # not a Windows drive: allowed
        for bad in ("~/x", "/etc/x", "a//b", "./x"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                N._check_name(bad)


# ---------------------------------------------------------------------------
# the review of PR #53
# ---------------------------------------------------------------------------

class ReviewSearch(Nav):
    def setUp(self):
        super().setUp()
        self.proj = self.home / "proj"
        self.proj.mkdir()
        self.url2 = self.project_at(self.proj, "Proj")

    def test_high1_a_catastrophic_pattern_never_starves_the_daemon(self):
        """`(\w+\s?)+:` on a line with a long word: rg matches its start, and
        Python's `re` on the rest of the line backtracks exponentially with the
        GIL held. The pattern must never run in this process."""
        if not shutil.which("rg"):
            self.skipTest("ripgrep is not installed")
        (self.proj / "boom.txt").write_text("a" * 25 + ": " + "a" * 25 + "\n")
        gaps, stop = [], threading.Event()

        def beat():
            last = time.monotonic()
            while not stop.is_set():
                time.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        t = threading.Thread(target=beat, daemon=True)
        t.start()
        start = time.monotonic()
        try:
            got = self.search(url=self.url2, query=r"(\w+\s?)+:", regex=True)
        finally:
            elapsed = time.monotonic() - start
            stop.set()
            t.join(2)
        self.assertEqual([m["path"] for m in got["matches"]], ["boom.txt"])
        self.assertEqual(got["matches"][0]["ranges"], [[0, 26]])
        self.assertLess(elapsed, N.SEARCH_SECONDS + 2)
        self.assertLess(max(gaps), 1.0, f"a daemon thread was starved for {max(gaps):.1f} s")

    def test_ranges_come_from_ripgrep_in_utf16(self):
        if not shutil.which("rg"):
            self.skipTest("ripgrep is not installed")
        (self.proj / "u.txt").write_text("é😀 needle é needle\n", encoding="utf-8")
        got = self.search(url=self.url2, query="needle")
        row = got["matches"][0]
        text = row["text"]
        for a, z in row["ranges"]:
            # UTF-16 columns, as the window counts them
            self.assertEqual(text.encode("utf-16-le")[2 * a:2 * z].decode("utf-16-le"), "needle")
        self.assertEqual(len(row["ranges"]), 2)
        # ripgrep's own syntax works: it validates, Python never compiles it
        got = self.search(url=self.url2, query=r"\p{L}+dle", regex=True)
        self.assertTrue(got["matches"])
        self.search(url=self.url2, query="(unclosed", regex=True, status=400)

    def test_no_ripgrep_refuses_a_regex_and_matches_a_literal(self):
        (self.proj / "l.txt").write_text("x = a.b(c)\n")
        real = N.search_bounded
        with patch.object(N, "search_bounded", lambda *a, **kw: real(*a, **{**kw, "use_rg": False})):
            got = self.search(url=self.url2, query=r"(\w+\s?)+:", regex=True, status=400)
            self.assertIn("ripgrep", json.dumps(got))
            got = self.search(url=self.url2, query="a.b(c)")
            self.assertEqual([(m["path"], m["ranges"]) for m in got["matches"]], [("l.txt", [[4, 10]])])

    def test_a_newer_search_from_the_same_window_cancels_the_older(self):
        slot, ev = N._search_slot("win-1")
        slot2, ev2 = N._search_slot("win-1")
        self.assertTrue(ev.is_set())
        self.assertFalse(ev2.is_set())
        N._search_done(slot, ev)          # the older one's end never removes the newer
        self.assertIs(N._SEARCHES.get("win-1"), ev2)
        N._search_done(slot2, ev2)
        held = [N._search_slot(f"k{i}") for i in range(N.MAX_SEARCHES)]
        try:
            with self.assertRaises(Exception) as caught:
                N._search_slot("one-too-many")
            self.assertEqual(getattr(caught.exception, "status", None), 429)
        finally:
            for s_, e_ in held:
                N._search_done(s_, e_)
        cancel = threading.Event()
        cancel.set()
        found = S.search_bounded("x", self.proj, cancel=cancel)
        self.assertTrue(found.cancelled or not found.hits)

    def test_med3_an_unreadable_file_is_a_partial_result(self):
        if not shutil.which("rg") or os.geteuid() == 0:
            self.skipTest("needs ripgrep and a non-root user")
        (self.proj / "ok.txt").write_text(f"{NEEDLE}\n")
        locked = self.proj / "locked.txt"
        locked.write_text(f"{NEEDLE}\n")
        locked.chmod(0)
        shut = self.proj / "shut"
        shut.mkdir()
        shut.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        self.addCleanup(shut.chmod, 0o755)
        got = self.search(url=self.url2, query=NEEDLE)
        self.assertEqual([m["path"] for m in got["matches"]], ["ok.txt"])
        self.assertTrue(got["partial"])
        (self.proj / "ok.txt").write_text("nothing\n")
        got = self.search(url=self.url2, query=NEEDLE)
        self.assertEqual((got["matches"], got["partial"]), ([], True))
        # grep_files keeps its own behaviour
        with runtime.tools_bound({"grep_files"}):
            out = S.grep_files(NEEDLE, str(self.proj))
        self.assertTrue(out.startswith(("Error: search failed", "No matches")), out[:80])

    def test_nit_grep_files_fallback_keeps_the_walk_order(self):
        for name in ("b.txt", "a.txt", "c.txt"):
            (self.proj / name).write_text(f"{NEEDLE}\n")
        walk = [str(Path(d) / f) for d, _, fs in os.walk(self.proj) for f in fs]
        hits, _ = S._python_hits(NEEDLE, self.proj, "", "files", 0, False)
        self.assertEqual([h.path for h in hits], [p for p in walk if p.endswith(".txt")])


class ReviewFs(Nav):
    def test_low1_an_extra_folder_never_reaches_a_credential(self):
        app = self.home / "app"
        project = self.daemon.stores.projects.create("App", str(app), extra_dirs=[str(self.home)])
        url = f"/projects/{project.id}"
        (app / "keys").symlink_to(self.home / ".ssh")
        self.fs("rename", "~/.ssh/id_test", "key.txt", status=400, url=url)
        self.fs("rename", "keys/id_test", "key.txt", status=403, url=url)
        self.fs("delete", "keys/id_test", status=403, url=url)
        self.fs("rename", "main.py", "keys/stolen.py", status=403, url=url)
        self.assertTrue((self.home / ".ssh" / "id_test").exists())
        self.assertTrue((app / "main.py").exists())

    def test_low2_jarvis_records_and_project_folders(self):
        data = self.home / ".local" / "share" / "jarvis" / "v2"
        data.mkdir(parents=True)
        (data / "x.json").write_text("{}")
        for op, path, to in (("delete", ".local/share/jarvis/v2/x.json", None),
                             ("rename", ".local/share/jarvis/v2/x.json", "x.json"),
                             ("new_file", ".local/share/jarvis/v2/y.json", None),
                             ("delete", ".local", None), ("rename", ".local", "local2")):
            with self.subTest(op=op, path=path):
                self.fs(op, path, to, status=403)
        other = self.home / "work" / "app2"
        other.mkdir(parents=True)
        (other / "f.txt").write_text("f\n")
        self.daemon.stores.projects.create("App2", str(other))
        for op, path, to in (("delete", "work/app2", None), ("rename", "work/app2", "app3"),
                             ("delete", "work", None), ("rename", "work", "work2")):
            with self.subTest(op=op, path=path):
                self.fs(op, path, to, status=403)
        self.assertTrue((data / "x.json").exists() and (other / "f.txt").exists())
        self.fs("new_file", "work/app2/new.txt")  # inside another project: a file is fine
        self.fs("delete", "work/app2/f.txt")

    def test_low3_a_second_deleted_env_file_keeps_its_name(self):
        trash = self.root / "xdg" / "Trash"
        for n in (1, 2):
            (self.home / "app" / ".env.local").write_text(f"K=fake-{n}\n")
            self.fs("delete", "app/.env.local")
        names = sorted(p.relative_to(trash / "files").as_posix() for p in (trash / "files").rglob("*") if p.is_file())
        self.assertEqual(names, [".env.local", "jarvis-deleted/.env.local"])
        self.assertFalse(any(".env.local." in n for n in names))
        info = (trash / "info" / "jarvis-deleted.trashinfo").read_text()
        self.assertIn("Path=" + str(self.home / "app" / ".env.local"), info)

    def test_low5_a_folder_on_the_way_is_never_followed(self):
        (self.home / "inner").symlink_to(self.home / "app")
        self.fs("rename", "inner/main.py", "app/moved.py", status=403)
        self.fs("new_file", "inner/x.py", status=403)
        self.assertTrue((self.home / "app" / "main.py").exists())
        self.assertFalse((self.home / "app" / "x.py").exists())
        a = os.open(str(self.home / "app"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            (self.home / "app" / "one").write_text("1")
            (self.home / "app" / "two").write_text("2")
            with self.assertRaises(FileExistsError):
                N._rename_noreplace(a, "one", a, "two")
        finally:
            os.close(a)
        self.assertEqual((self.home / "app" / "two").read_text(), "2")

    def test_nit_a_folder_named_env_is_a_folder(self):
        venv = self.home / "app" / ".env"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr\n")
        got = self.fs("rename", "app/.env", "app/pyenv")
        self.assertTrue((self.home / "app" / "pyenv" / "pyvenv.cfg").exists())
        self.assertNotEqual(got["kind"], "env")
        self.fs("rename", "app/.env.production", "app/readable.txt", status=403)


# ---------------------------------------------------------------------------
# owner-only
# ---------------------------------------------------------------------------

class OwnerOnly(Nav):
    def test_the_api_listener_and_a_bare_request_are_refused(self):
        api = self.daemon.port
        routes = [("GET", f"{self.url}/files", None),
                  ("POST", f"{self.url}/search", {"query": "hello"}),
                  ("POST", f"{self.url}/fs", {"op": "new_file", "path": "made-by-api.txt"}),
                  ("POST", f"{self.url}/fs", {"op": "delete", "path": "notes.txt"}),
                  ("POST", f"{self.url}/fs", {"op": "rename", "path": "notes.txt", "to": "n2.txt"})]
        for method, path, body in routes:
            for port, headers in ((api, {}), (api, {"Origin": f"http://127.0.0.1:{api}"}),
                                  (api, {"Sec-Fetch-Site": "same-origin"}),
                                  (self.face, {}), (self.face, {"Sec-Fetch-Site": "cross-site"}),
                                  (self.face, {"Origin": "null"}),
                                  (self.face, {"Origin": f"http://127.0.0.1:{self.daemon.workshop_port}"})):
                with self.subTest(method=method, path=path, port=port, headers=headers):
                    status, payload = self.raw(method, path, headers, port=port, body=body)
                    self.assertEqual(status, 403, str(payload)[:200])
                    self.assertNotIn("notes.txt", json.dumps(payload) if isinstance(payload, dict) else "")
        self.assertFalse((self.home / "made-by-api.txt").exists())
        self.assertEqual((self.home / "notes.txt").read_text(), "hello\n")
        self.assertFalse((self.home / "n2.txt").exists())

    def test_the_owner_may(self):
        self.assertIn("notes.txt", self.files()["files"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
