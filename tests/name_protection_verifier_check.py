"""PR #388 verifier counterexamples: each test fails at the reviewed HEAD.

Only fake contents and temporary HOME/project folders; no network or daemon.
Run with the task's memory-limited systemd scope and PYTHONPATH=.
ADS is a lexical Windows probe, not an assertion of NTFS support on Linux.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import patch

# config imports a checkout .env automatically; never load the owner's one.
_checkout_env = Path(__file__).resolve().parents[1] / ".env"
_path_exists = Path.exists
with tempfile.TemporaryDirectory(prefix="nameprot-import-home-") as _import_home:
    with patch.dict(os.environ, {"HOME": _import_home}), \
         patch.object(Path, "exists", lambda p: False if p == _checkout_env else _path_exists(p)):
        from jarvis import config, runtime
        from jarvis.tools import dispatch, search, secrets
        from jarvis.v2 import permissions

MARKER = "VERIFIER_SENTINEL_388_fake_only"


def call(name: str, **args) -> str:
    with runtime.tools_bound({"read_file", "grep_files", "run_readonly"}):
        return dispatch(name, json.dumps(args)).text


class Counterexamples(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nameprot-verifier-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        for context in (
            patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""),
                                   "LANG": "C.UTF-8",
                                   "JARVIS_WINDOWS_USERS": str(self.root / "Users")}, clear=True),
            patch.object(config, "V2_DATA_DIR", self.root / "data"),
            patch.object(config, "BROWSER_DIR", self.root / "browser"),
            patch.object(runtime, "working_dir", return_value=str(self.project)),
            # Keep the real output scrubber, but point all value discovery at fixtures.
            patch.object(secrets, "_search_dirs", return_value=[self.home, self.project]),
            *(patch.object(config, key, self.root / key) for key in
              ("GOOGLE_TOKEN_PATH", "ONSHAPE_TOKEN_PATH", "DISCORD_TOKEN_PATH", "SPOTIFY_TOKEN_PATH")),
        ):
            context.start()
            self.addCleanup(context.stop)

    def payload(self):
        p = self.project / "payload.txt"
        p.write_text(MARKER + "\n")
        return p

    def test_grep_files_keeps_protected_names_before_resolving_root(self):
        """root.resolve() launders both own protected names and intermediate hops."""
        self.payload()
        protected = self.project / ".netrc"
        protected.symlink_to("payload.txt")
        alias = self.project / "alias.txt"
        alias.symlink_to(".netrc")
        rg = shutil.which("rg")
        self.assertIsNotNone(rg, "This verifier environment must provide real ripgrep")
        failures = []
        for backend in ("ripgrep", "python"):
            with patch.object(search.shutil, "which", return_value=rg if backend == "ripgrep" else None):
                for path in (protected, alias):
                    for mode in search.MODES:
                        out = call("grep_files", pattern="VERIFIER", path=str(path),
                                   mode=mode, context_lines=1)
                        if not out.startswith(("No matches", "Error:")):
                            evidence = f"{backend}/{mode}/{path.name}: {out!r}"
                            print("LEAK", evidence, flush=True)
                            failures.append(evidence)
        self.assertFalse(failures, "grep_files returned protected-root results in "
                         f"{len(failures)} backend/mode/path combinations")

    def test_glued_short_options_judge_bare_relative_alias(self):
        protected = self.project / ".netrc"
        protected.write_text(MARKER + "\n")
        (self.project / "alias.txt").symlink_to(".netrc")
        failures = []
        for command in ("date -falias.txt", "file -falias.txt", "file -bfalias.txt"):
            out = call("run_readonly", command=command)
            if MARKER in out:
                print("LEAK", command, repr(out), flush=True)
                failures.append(command)
        self.assertFalse(failures, "glued relative option values printed protected contents: "
                         + ", ".join(failures))

    def test_symlink_parent_dotdot_keeps_intermediate_protected_hop(self):
        self.payload()
        real = self.project / "elsewhere"
        (real / "inner").mkdir(parents=True)
        (self.project / "bridge").symlink_to("elsewhere/inner", target_is_directory=True)
        (real / ".netrc").symlink_to("../payload.txt")
        (real / "alias.txt").symlink_to(".netrc")
        path = self.project / "bridge" / ".." / "alias.txt"
        failures = []
        for tool, args in (
            ("read_file", {"path": str(path)}),
            ("run_readonly", {"command": "cat " + shlex.quote(str(path))}),
            ("grep_files", {"pattern": "VERIFIER", "path": str(path)}),
        ):
            out = call(tool, **args)
            if MARKER in out:
                print("LEAK", tool, repr(out), flush=True)
                failures.append(tool)
        for tool, args in (("Read", {"file_path": str(path)}),
                           ("Grep", {"path": str(path)}),
                           ("Glob", {"path": str(path), "pattern": "*"})):
            verdict = permissions.denied_read(tool, args, str(self.project))
            if verdict is None:
                print("ALLOWED", tool, str(path), flush=True)
                failures.append("v2 " + tool)
        self.assertFalse(failures, "lexical abspath erased a protected hop: " + ", ".join(failures))

    def test_unicode_non_casefold_equivalents_remain_readable(self):
        failures = []
        # IGNORECASE adds dotted/dotless I; neither casefolds to ASCII i.
        for name in ("dİscord_token.json", ".pypırc"):
            self.assertFalse(secrets.is_credential_name(name))
            path = self.project / name
            path.write_text(MARKER + "\n")
            q = shlex.quote(name)
            for command in (f"ls {q}", f"cat {q}", f"grep VERIFIER {q}", f"find . -name {q}"):
                out = call("run_readonly", command=command)
                if out.startswith("Error:"):
                    print("FALSE POSITIVE", command, repr(out), flush=True)
                    failures.append(command)
            verdict = permissions.denied_read("Grep", {"path": str(path)}, str(self.project))
            if verdict is not None:
                print("FALSE POSITIVE v2 Grep", name, repr(verdict), flush=True)
                failures.append("v2 Grep " + name)
        self.assertFalse(failures, f"{len(failures)} ordinary-name reads refused outside the casefold rule")

    def test_eacces_in_grep_pattern_does_not_refuse_readable_input(self):
        """Fail-closed lstat now judges a regex as an unreadable credential path."""
        locked = self.project / "locked"
        locked.mkdir()
        (locked / "child").write_text("ordinary\n")
        readable = self.project / "readme.txt"
        readable.write_text("locked/child\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(PermissionError):
            (locked / "child").lstat()
        out = call("run_readonly", command="grep 'locked/child' readme.txt")
        print("REGRESSION grep pattern", repr(out), flush=True)
        self.assertIn("locked/child\n[exit 0]", out,
                      "grep's pattern is not a file operand; its input is ordinary and readable")

if __name__ == "__main__":
    unittest.main(verbosity=2)
