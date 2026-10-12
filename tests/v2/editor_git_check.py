"""The editor's git (ED-9): HEAD's text for the gutter, and the explorer's
colours.

Free and hermetic, on `editor_files_check`'s world: a real daemon on three
ephemeral loopback ports (never 8402, 8403 or 8405), every config path and
HOME temporary, real repositories made under the temp HOME with fake
credentials only. Git runs through `gitops._git`, so the owner's global git
configuration is the temp HOME's.

What it pins:

  - **owner-only**: both routes refuse the API listener and a request with no
    Origin and no same-origin fetch metadata, and run no git when refused;
  - **HEAD's text** for a committed file (the gutter's base), `new` for a file
    HEAD does not hold, `no_repo`, `no_head`, `binary` and the size cap;
  - **refusals**, judged before git runs: a withheld file, a credential env
    file, the gate's state — and the **name in git**: a staged rename from
    `.env` (or from a withheld or gate file) is refused, its text never sent;
  - **a repository whose own config names a program** is refused by both
    routes, and the program does not run;
  - **the status**: modified, added, untracked, ignored (folders whole),
    renamed, deleted and conflict; project-relative when the project is a
    folder inside a repository; withheld names, the gate's state and
    `SKIP_DIRS` dropped; cached for 2 s; no repository says so.

Run: PYTHONPATH=. .venv/bin/python tests/v2/editor_git_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from editor_files_check import FAKE_SECRET, Daemonised  # noqa: E402

from jarvis import config  # noqa: E402
from jarvis.v2 import editor_git as G  # noqa: E402


def git(root: Path, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=root, env=env, check=check, capture_output=True, text=True)


class Repo(Daemonised):
    def setUp(self):
        super().setUp()
        G.clear_cache()
        self.addCleanup(G.clear_cache)
        r = self.proj = self.home / "proj"
        (r / "src").mkdir(parents=True)
        (r / "src" / "main.cpp").write_text("int main() {\n  return 0;\n}\n")
        (r / "plain.txt").write_text("one\ntwo\nthree\n")
        (r / "blob.bin").write_bytes(b"ab\x00cd\n")
        (r / ".env").write_text(f"API_KEY={FAKE_SECRET}\n")
        (r / ".env.production").write_text(f"API_KEY={FAKE_SECRET}\n")
        (r / "google_token.json").write_text(f'{{"refresh_token": "{FAKE_SECRET}"}}')
        (r / ".gitignore").write_text("build/\n*.log\nnode_modules/\n")
        git(r, "init", "-q")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "first")
        project = self.daemon.stores.projects.create("Proj", str(r))
        self.purl = f"/projects/{project.id}"

    def base(self, path, status=200, url=None):
        return self.owner("GET", f"{url or self.purl}/git/base?" + urlencode({"path": path}), status=status)

    def status(self, status=200, url=None):
        return self.owner("GET", f"{url or self.purl}/git/status", status=status)

    def project_at(self, root: Path, name="P"):
        project = self.daemon.stores.projects.create(name, str(root))
        return f"/projects/{project.id}"


class OwnerOnly(Repo):
    def test_the_api_listener_and_a_bare_request_are_refused(self):
        api = self.daemon.port
        calls = []
        real = G._git
        with patch.object(G, "_git", lambda *a, **k: calls.append(a) or real(*a, **k)):
            for path in (f"{self.purl}/git/base?path=plain.txt", f"{self.purl}/git/status",
                         f"{self.purl}/git/base?path=.env"):
                for port, headers in ((api, {}), (api, {"Origin": f"http://127.0.0.1:{api}"}),
                                      (api, {"Sec-Fetch-Site": "same-origin"}),
                                      (self.face, {}), (self.face, {"Sec-Fetch-Site": "cross-site"}),
                                      (self.face, {"Origin": "null"}),
                                      (self.face, {"Origin": f"http://127.0.0.1:{self.daemon.workshop_port}"})):
                    with self.subTest(path=path, port=port, headers=headers):
                        status, payload = self.raw("GET", path, headers, port=port)
                        self.assertEqual(status, 403, str(payload)[:200])
                        text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
                        self.assertNotIn("three", text)
                        self.assertNotIn(FAKE_SECRET, text)
        self.assertEqual(calls, [], "a refused request ran git")

    def test_a_missing_project_is_still_403_for_anyone_else(self):
        status, _ = self.raw("GET", "/projects/deadbeef/git/status", {}, port=self.daemon.port)
        self.assertEqual(status, 403)
        self.owner("GET", "/projects/deadbeef/git/status", status=404)

    def test_a_same_origin_read_is_the_owners(self):
        status, payload = self.raw("GET", f"{self.purl}/git/base?path=plain.txt",
                                   {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((status, payload["state"], payload["text"]), (200, "ok", "one\ntwo\nthree\n"))


class Base(Repo):
    def test_heads_text_follows_head_not_the_disk(self):
        (self.proj / "plain.txt").write_text("one\nTWO\nthree\nfour\n")
        got = self.base("plain.txt")
        head = git(self.proj, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual((got["state"], got["text"], got["head"], got["renamed"]),
                         ("ok", "one\ntwo\nthree\n", head, False))
        self.assertEqual(self.base("src/main.cpp")["text"], "int main() {\n  return 0;\n}\n")

    def test_new_untracked_and_added_files(self):
        (self.proj / "new.txt").write_text("x\n")
        self.assertEqual((self.base("new.txt")["state"], self.base("new.txt")["text"]), ("new", None))
        (self.proj / "added.txt").write_text("y\n")
        git(self.proj, "add", "added.txt")
        self.assertEqual(self.base("added.txt")["state"], "new")

    def test_no_repo_no_head(self):
        (self.home / "loose").mkdir()
        (self.home / "loose" / "a.txt").write_text("a\n")
        url = self.project_at(self.home / "loose")
        self.assertEqual(self.base("a.txt", url=url)["state"], "no_repo")
        self.assertEqual(self.status(url=url), {"repo": False, "head": None, "branch": None, "files": {},
                                                "dirs": {}, "truncated": False})
        fresh = self.home / "fresh"
        fresh.mkdir()
        (fresh / "a.txt").write_text("a\n")
        git(fresh, "init", "-q")
        url = self.project_at(fresh, "Fresh")
        self.assertEqual(self.base("a.txt", url=url)["state"], "no_head")
        self.assertIsNone(self.status(url=url)["head"])

    def test_binary_and_the_size_cap(self):
        got = self.base("blob.bin")
        self.assertEqual((got["state"], got["text"]), ("binary", None))
        with patch.object(G, "BASE_CAP", 5):
            got = self.base("plain.txt")
        self.assertEqual((got["state"], got["text"], got["size"]), ("too_large", None, 14))

    def test_a_folder_and_a_missing_file(self):
        self.base("src", status=404)
        self.base("nope.txt", status=404)
        self.base("../outside.txt", status=403)

    def test_a_plain_rename_reads_its_source(self):
        git(self.proj, "mv", "plain.txt", "moved.txt")
        got = self.base("moved.txt")
        self.assertEqual((got["state"], got["text"], got["renamed"]), ("ok", "one\ntwo\nthree\n", True))


class Refusals(Repo):
    def refused(self, path, url=None):
        calls = []
        real = G._git
        with patch.object(G, "_git", lambda *a, **k: calls.append(a) or real(*a, **k)):
            got = self.base(path, status=403, url=url)
        self.assertNotIn(FAKE_SECRET, json.dumps(got))
        return got, calls

    def test_env_withheld_and_gate_are_refused_before_git_runs(self):
        for path in (".env", ".env.production", "google_token.json"):
            with self.subTest(path=path):
                got, calls = self.refused(path)
                self.assertEqual(calls, [], f"git ran for {path}")
        self.assertIn("env file", self.refused(".env")[0]["error"])
        self.assertIn("credential", self.refused("google_token.json")[0]["error"])

    def test_a_credential_folder_in_a_home_repository(self):
        h = self.home
        git(h, "init", "-q")
        git(h, "add", ".ssh/id_test", "notes.txt")
        git(h, "commit", "-qm", "home")
        _, calls = self.refused(".ssh/id_test", url=self.url)
        self.assertEqual(calls, [])
        self.assertEqual(self.base("notes.txt", url=self.url)["text"], "hello\n")

    def test_the_gates_state(self):
        cj = self.home / ".config" / "jarvis"
        (cj / "allowlist.json").write_text('[{"tool": "x"}]\n')
        (cj / "notes.md").write_text("ok\n")
        git(cj, "init", "-q")
        git(cj, "add", "allowlist.json", "notes.md")
        git(cj, "commit", "-qm", "gate")
        url = self.project_at(cj, "Cfg")
        # The editor itself shows the gate's state read-only (E-8) …
        self.assertEqual(self.owner("GET", f"{url}/file?path=allowlist.json")["kind"], "gate")
        # … but its committed text is never served for a gutter.
        _, calls = self.refused("allowlist.json", url=url)
        self.assertEqual(calls, [])

    def test_a_rename_from_env_is_judged_by_its_name_in_git(self):
        git(self.proj, "mv", ".env", "notes.txt")
        got, _ = self.refused("notes.txt")
        self.assertIn("env file", got["error"])
        git(self.proj, "mv", ".env.production", "settings.ini")
        self.refused("settings.ini")
        git(self.proj, "mv", "google_token.json", "data.json")
        got, _ = self.refused("data.json")
        self.assertIn("credential", got["error"])

    def test_a_rename_from_the_gates_state(self):
        # The gate's state where no credential folder holds it, so only the
        # name in git can refuse the rename.
        gate = self.proj / "gate"
        gate.mkdir()
        (gate / "allowlist.json").write_text('[{"tool": "x"}]\n')
        git(self.proj, "add", "gate/allowlist.json")
        git(self.proj, "commit", "-qm", "gate")
        git(self.proj, "mv", "gate/allowlist.json", "gate/list.txt")
        with patch.object(config, "ALLOWLIST_PATH", gate / "allowlist.json"):
            got, calls = self.refused("gate/list.txt")
        self.assertIn("permission state", got["error"])
        self.assertTrue(calls, "refused by the name in git, after git said where it came from")
        self.assertEqual(self.base("gate/list.txt")["text"], '[{"tool": "x"}]\n',
                         "control: an ordinary file of that name is served")

    def test_a_repository_config_program_is_refused_and_never_runs(self):
        marker = self.root / "filter-ran"
        script = self.root / "clean.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        script.chmod(0o755)
        (self.proj / ".gitattributes").write_text("*.txt filter=probe\n")
        git(self.proj, "config", "filter.probe.clean", str(script))
        # The same size and a moved mtime: git must hash it, through the filter.
        (self.proj / "plain.txt").write_text("one\ntwo\nthreE\n")
        os.utime(self.proj / "plain.txt", (1, 1))
        origin = {"Origin": f"http://127.0.0.1:{self.face}"}
        status, got = self.raw("GET", f"{self.purl}/git/status", origin)
        self.assertFalse(marker.exists(), "the repository's filter program ran")
        self.assertEqual(status, 403)
        self.assertIn("filter.probe.clean", got["error"])
        got = self.base("plain.txt", status=403)
        self.assertIn("filter.probe.clean", got["error"])
        self.assertFalse(marker.exists(), "the repository's filter program ran")

    def test_a_submodules_config_program_never_runs(self):
        # A submodule's own config is not the superproject's: the status never
        # goes into it, and its file's base is judged by the submodule's config.
        lib = self.root / "libsrc"
        lib.mkdir()
        (lib / "a.txt").write_text("a\n")
        git(lib, "init", "-q")
        git(lib, "add", "a.txt")
        git(lib, "commit", "-qm", "lib")
        git(self.proj, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(lib), "lib")
        git(self.proj, "commit", "-qm", "sub")
        marker = self.root / "sub-filter-ran"
        script = self.root / "subclean.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
        script.chmod(0o755)
        sub = self.proj / "lib"
        (sub / ".gitattributes").write_text("*.txt filter=probe\n")
        git(sub, "config", "filter.probe.clean", str(script))
        (sub / "a.txt").write_text("b\n")  # same size, moved mtime: hashed through the filter
        os.utime(sub / "a.txt", (1, 1))
        got = self.status()
        self.assertFalse(marker.exists(), "the submodule's filter program ran")
        self.assertNotIn("lib/a.txt", got["files"])
        # (A submodule's config also sets `core.worktree`, which gitops refuses first.)
        self.assertIn("own git config sets", self.base("lib/a.txt", status=403)["error"])
        self.assertFalse(marker.exists(), "the submodule's filter program ran")

    def test_a_cached_refusal_stays_a_refusal(self):
        git(self.proj, "config", "core.sshCommand", "/bin/false")
        self.status(status=403)
        self.status(status=403)


class Status(Repo):
    def test_each_state(self):
        r = self.proj
        (r / "plain.txt").write_text("one\nTWO\n")
        (r / "added.txt").write_text("a\n")
        git(r, "add", "added.txt")
        (r / "untracked.txt").write_text("u\n")
        (r / "newdir").mkdir()
        (r / "newdir" / "f.txt").write_text("f\n")
        (r / "build").mkdir()
        (r / "build" / "out.o").write_text("o\n")
        (r / "debug.log").write_text("l\n")
        (r / "node_modules" / "pkg").mkdir(parents=True)
        (r / "node_modules" / "pkg" / "i.js").write_text("i\n")
        git(r, "mv", "src/main.cpp", "src/robot.cpp")
        (r / "blob.bin").unlink()
        got = self.status()
        head = git(r, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual((got["repo"], got["head"], got["truncated"]), (True, head, False))
        self.assertTrue(got["branch"])
        self.assertEqual(got["files"], {"plain.txt": "modified", "added.txt": "added",
                                        "untracked.txt": "untracked", "debug.log": "ignored",
                                        "src/robot.cpp": "renamed", "blob.bin": "deleted"})
        self.assertEqual(got["dirs"], {"newdir": "untracked", "build": "ignored"})
        self.assertNotIn("src/main.cpp", json.dumps(got), "a rename's source is never returned")

    def test_withheld_names_and_the_gate_are_dropped(self):
        h = self.home
        git(h, "init", "-q")
        (h / ".ssh" / "known").write_text("k\n")
        got = self.status(url=self.url)
        names = set(got["files"]) | set(got["dirs"])
        for never in (".ssh", ".ssh/id_test", ".ssh/known", ".claude/.credentials.json", ".codex/auth.json",
                      ".git-credentials", "google_token.json", ".config/jarvis", ".config/jarvis/allowlist.json"):
            self.assertNotIn(never, names)
        self.assertIn("notes.txt", names)
        self.assertIn("app", names)  # an untracked folder, env files and all, is a name only
        self.assertNotIn(FAKE_SECRET, json.dumps(got))

    def test_a_conflict(self):
        r = self.proj
        git(r, "checkout", "-qb", "side")
        (r / "plain.txt").write_text("side\n")
        git(r, "commit", "-qam", "side")
        git(r, "checkout", "-q", "-")
        (r / "plain.txt").write_text("main\n")
        git(r, "commit", "-qam", "main")
        git(r, "merge", "side", check=False)
        self.assertEqual(self.status()["files"].get("plain.txt"), "conflict")

    def test_a_project_inside_a_repository(self):
        url = self.project_at(self.proj / "src", "Src")
        (self.proj / "src" / "main.cpp").write_text("changed\n")
        (self.proj / "plain.txt").write_text("changed\n")
        (self.proj / "src" / "new.h").write_text("h\n")
        got = self.status(url=url)
        self.assertEqual(got["files"], {"main.cpp": "modified", "new.h": "untracked"})
        self.assertEqual(self.base("main.cpp", url=url)["text"], "int main() {\n  return 0;\n}\n")

    def test_a_project_inside_an_untracked_folder(self):
        (self.proj / "lab" / "robot").mkdir(parents=True)
        (self.proj / "lab" / "robot" / "a.cpp").write_text("a\n")
        url = self.project_at(self.proj / "lab" / "robot", "Lab")
        got = self.status(url=url)
        self.assertEqual((got["files"], got["dirs"]), ({}, {"": "untracked"}))

    def test_cached_for_two_seconds(self):
        clock = [100.0]
        calls = []
        real = G._git
        fake_time = SimpleNamespace(monotonic=lambda: clock[0])
        with patch.object(G, "_git", lambda *a, **k: calls.append(a) or real(*a, **k)), \
                patch.object(G, "time", fake_time):
            self.status()
            n = len(calls)
            (self.proj / "plain.txt").write_text("changed\n")
            clock[0] += 1.9
            self.assertEqual(self.status()["files"], {})
            self.assertEqual(len(calls), n, "a second read inside the TTL ran git again")
            clock[0] += 0.2
            self.assertEqual(self.status()["files"], {"plain.txt": "modified"})

    def test_a_slow_status_rests_three_times_as_long(self):
        clock = [100.0]
        real = G._status_now

        def slow(*a, **k):
            clock[0] += 1.5  # the status took 1.5 s: cached for 4.5
            return real(*a, **k)
        with patch.object(G, "_status_now", slow), \
                patch.object(G, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            self.status()
            (self.proj / "plain.txt").write_text("changed\n")
            clock[0] += 4.4
            self.assertEqual(self.status()["files"], {})
            clock[0] += 0.2
            self.assertEqual(self.status()["files"], {"plain.txt": "modified"})

    def test_capped(self):
        for i in range(5):
            (self.proj / f"u{i}.txt").write_text("u\n")
        with patch.object(G, "STATUS_CAP", 3):
            got = self.status()
        self.assertEqual((len(got["files"]) + len(got["dirs"]), got["truncated"]), (3, True))

    def test_a_credential_folder_root_is_refused(self):
        url = self.project_at(self.home / ".ssh", "Keys")
        self.status(status=403, url=url)


if __name__ == "__main__":
    unittest.main(verbosity=1)
