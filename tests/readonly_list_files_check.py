"""`run_readonly` refuses options that read paths or settings from another
file (docs/plans/2026-11-16-run-readonly-list-files.md).

`run_readonly` judges every word on the line as a path. PR #388's Sol
verifier showed a program that takes its paths from another file: with
`list.txt` naming a protected file and a `%s` magic rule, `file -m magic.txt
-f list.txt` printed it. The audit found five more forms at main 07a9b4ac:
`file -m A:B` and `file -m DIR` (magic parse warnings quote the files read),
`env -f VARS ls` (PATH from a file: ran any program), `env -C DIR cat rel`
(relative operands judged in the wrong folder: printed the key), and
`wc`/`du --files0-from`, `find -files0-from` (names, counts, sizes).
The Sol verifier then added three: `file` loading a planted `~/.magic`
symlink (or a `X.mgc` companion), a glued bare tail naming a relative
symlink (`date -fkeylink`), and `ionice -p PID`, which changed a running
process.

Options whose named file is itself the input stay allowed and are pinned
here as negatives: `grep -f`, `date -f`, `du -X`, `file -m ONE_FILE`.

Hermetic: temporary HOME and project, a fake key, `runtime.working_dir`
patched, no network, no daemon.

Run: PYTHONPATH=. .venv/bin/python tests/readonly_list_files_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# config loads a checkout `.env` at import; never the owner's one.
_checkout_env = ROOT / ".env"
_path_exists = Path.exists
with tempfile.TemporaryDirectory(prefix="listfiles-import-") as _import_home:
    with patch.dict(os.environ, {"HOME": _import_home}), \
         patch.object(Path, "exists",
                      lambda p: False if p == _checkout_env else _path_exists(p)):
        from jarvis import config, runtime  # noqa: E402
        from jarvis.tools import dispatch, secrets  # noqa: E402

SENTINEL = "listfiles-sentinel-fake-key-0123456789"


def run_readonly(command: str) -> str:
    with runtime.tools_bound({"run_readonly"}):
        return dispatch("run_readonly", json.dumps({"command": command})).text


class ListFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="listfiles-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        self.project = root / "project"
        (self.home / ".ssh").mkdir(parents=True)
        self.project.mkdir()
        self.key = self.home / ".ssh" / "id_rsa"
        self.key.write_text(SENTINEL + "\n")
        p = self.project
        (p / "list.txt").write_text(f"{self.key}\n")
        (p / "list0").write_text(f"{self.key}\0")
        (p / "magic.txt").write_text("0 string x %s\n")
        # A magic file whose name starts with `f`: `-mfoo.magic` is not `-f`.
        (p / "foo.magic").write_text("0 string x %s\n")
        (p / "mdir").mkdir()
        (p / "mdir" / "link").symlink_to(self.key)
        (p / "bin").mkdir()
        fake = p / "bin" / "ls"
        fake.write_text(f"#!/bin/sh\necho ran > {p}/PWNED\n")
        fake.chmod(0o755)
        (p / "vars.env").write_text(f"PATH={p}/bin\n")
        (p / "pats").write_text("string\n")
        (p / "dates").write_text("2026-01-01\n")
        for context in (
            patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""),
                                    "LANG": "C.UTF-8",
                                    "JARVIS_WINDOWS_USERS": str(root / "Users")}, clear=True),
            patch.object(config, "V2_DATA_DIR", root / "data"),
            patch.object(config, "BROWSER_DIR", root / "browser"),
            patch.object(runtime, "working_dir", return_value=str(p)),
            patch.object(secrets, "_search_dirs", return_value=[self.home, p]),
        ):
            context.start()
            self.addCleanup(context.stop)

    def assertRefused(self, command: str, why: str | None):
        out = run_readonly(command)
        self.assertNotIn(SENTINEL, out, command)
        self.assertFalse((self.project / "PWNED").exists(), f"{command} ran the fake program")
        self.assertTrue(out.startswith("Error:"), f"{command} ran: {out[:200]!r}")
        if why is not None:
            self.assertIn(why, out, command)
            self.assertIn("cannot judge", out, command)

    def assertRuns(self, command: str):
        out = run_readonly(command)
        self.assertFalse(out.startswith("Error:"), f"{command} refused: {out[:200]!r}")
        self.assertNotIn(SENTINEL, out, command)

    def test_file_files_from(self):
        for command in ("file -b -m magic.txt -f list.txt",
                        "file --brief --magic-file=magic.txt --files-from=list.txt",
                        "file -bm magic.txt -flist.txt",
                        "file -bf list.txt",
                        "file -f list.txt -m magic.txt",
                        "file --files=list.txt",
                        "file --files-f list.txt",
                        "file -- -f list.txt"):
            self.assertRefused(command, "file -f/--files-from")

    def test_file_magic_list_or_folder(self):
        key = self.key
        for command in (f"file -b -m magic.txt:{key} magic.txt",
                        f"file -c -m magic.txt:{key}",
                        f"file -bmmagic.txt:{key} magic.txt",
                        f"file --magic-file=magic.txt:{key} magic.txt",
                        f"file --magic magic.txt:{key} magic.txt"):
            self.assertRefused(command, "colon-separated list")
        for command in ("file -c -m mdir", "file -b -m mdir magic.txt",
                        "file -bmmdir magic.txt", "file --magic-file=mdir magic.txt",
                        "file -m ~ magic.txt", "file -m", "file --magic-file= magic.txt",
                        "file -m nosuch.magic magic.txt"):
            self.assertRefused(command, "reads every file in a folder")

    def test_files0_from(self):
        for command in ("wc --files0-from=list0", "wc --files0-from list0", "wc --files0 list0",
                        "wc --f=list0", "du --files0-from=list0", "du -a --files0-from list0",
                        "du --fi=list0"):
            self.assertRefused(command, "--files0-from opens the paths listed")
        for command in ("find -files0-from list0", "find . -files0-from list0 -prune"):
            self.assertRefused(command, "find -files0-from")
        # Any allowlisted program that grows GNU's spelling of it.
        self.assertRefused("cat --files0-from=list0", "cat --files0-from")
        self.assertRefused("stat --files-from list.txt", "stat --files-from")

    def test_env_file_never_sets_what_runs(self):
        for command in ("env -f vars.env ls", "env --file=vars.env ls", "env -fvars.env ls",
                        "env --fil=vars.env ls", "env -i -f vars.env ls", "env -if vars.env ls",
                        "env - -f vars.env ls", "env LANG=C -f vars.env ls",
                        "env -u FOO -f vars.env ls", "env --unset FOO --file vars.env ls",
                        "timeout 5 env -f vars.env ls", "env -i env -f vars.env ls",
                        "nice env --file vars.env ls", "env -f vars.env"):
            self.assertRefused(command, "env -f/--file")

    def test_env_chdir_never_moves_relative_operands(self):
        ssh = self.home / ".ssh"
        for command in (f"env -C {ssh} cat id_rsa", f"env --chdir={ssh} cat id_rsa",
                        f"env -iC{ssh} cat id_rsa",
                        f"env -C {self.home} cat .ssh/id_rsa", "env -C /tmp"):
            self.assertRefused(command, "env -C/--chdir")
        # A long prefix with its value in the next word: `unwrap_sure` does
        # not know the prefix, so it refuses first; refused either way.
        self.assertRefused(f"env --ch {ssh} cat id_rsa", None)
        self.assertRefused(f"env --chd={ssh} cat id_rsa", "env -C/--chdir")

    def test_env_split_string(self):
        for command in ("env -S'-f vars.env' ls", "env --split-string='-f vars.env' ls",
                        "env --sp '-C /tmp' ls"):
            self.assertRefused(command, "env -S/--split-string")

    def test_reads_of_named_files_still_run(self):
        for command in ("file -b magic.txt", "file -m magic.txt magic.txt",
                        "file -bmmagic.txt magic.txt", "file --mag=magic.txt magic.txt",
                        "file --magic-file magic.txt magic.txt", "file -e ascii magic.txt",
                        "file -Ff magic.txt", "file -mfoo.magic magic.txt",
                        "wc -l magic.txt", "du -sh .", "du -X pats .",
                        "grep -n env -C 2 magic.txt", "grep -f pats magic.txt",
                        "grep -r --exclude-from=pats x .", "date -f dates", "date -r magic.txt",
                        "env LANG=C ls", "env -u FOO ls", "env -i ls", "env",
                        "timeout 5 ls", "find . -name x", "cat list.txt", "ls bin"):
            self.assertRuns(command)

    def test_home_magic_and_companions(self):
        # Sol verifier, 2026-11-16: with no `-m`, file loads ~/.magic.mgc,
        # ~/.magic or ~/.magic/magic.mgc; a planted symlink printed the key.
        for name in (".magic", ".magic.mgc"):
            link = self.home / name
            link.symlink_to(self.key)
            self.assertRefused("file -b magic.txt", f"file loads ~/{name} as magic")
            self.assertRefused("file magic.txt pats", f"file loads ~/{name} as magic")
            link.unlink()
        # A link to a name-protected file outside every credential folder:
        # only the "never a symlink" rule sees it.
        # (A fake `.env` in the temporary project.)
        named = self.project / ".env"
        named.write_text(SENTINEL + "\n")
        (self.home / ".magic").symlink_to(named)
        self.assertRefused("file -b magic.txt", "file loads ~/.magic as magic")
        (self.home / ".magic").unlink()
        named.unlink()
        (self.home / ".magic").mkdir()
        (self.home / ".magic" / "magic.mgc").symlink_to(self.key)
        self.assertRefused("file -b magic.txt", "file loads ~/.magic/magic.mgc as magic")
        # A magic file named on the line is loaded instead of the home ones.
        self.assertRuns("file -m magic.txt magic.txt")
        (self.home / ".magic" / "magic.mgc").unlink()
        (self.home / ".magic").rmdir()
        (self.home / ".magic").write_text("0 string zz %s\n")
        self.assertRuns("file -b magic.txt")
        # libmagic tries `X.mgc` before `X`.
        (self.project / "two.magic").write_text("0 string x %s\n")
        (self.project / "two.magic.mgc").symlink_to(self.key)
        for command in ("file -m two.magic magic.txt", "file -bmtwo.magic magic.txt",
                        "file --magic-file=two.magic magic.txt"):
            self.assertRefused(command, "compiled .mgc companion")
        # A magic file that is itself a symlink, glued or as a word.
        (self.project / "keylink").symlink_to(self.key)
        self.assertRefused("file -m keylink magic.txt", None)
        self.assertRefused("file -mkeylink magic.txt", None)

    def test_glued_bare_tail_symlink(self):
        # Sol verifier, 2026-11-16 (older than this change): a value glued to
        # a short option was judged only when it looked like a path.
        (self.project / "keylink").symlink_to(self.key)
        for command in ("date -fkeylink", "grep -fkeylink magic.txt", "ss -Fkeylink",
                        "file -bmkeylink magic.txt", "du -Xkeylink .", "head -n1 -ckeylink"):
            self.assertRefused(command, None)
        for command in ("head -n1 magic.txt", "grep -rn string .", "ls -la", "du -sh .",
                        "tail -n5 magic.txt", "ps -ef", "df -h ."):
            self.assertRuns(command)

    def test_ionice_never_changes_running_processes(self):
        import subprocess
        child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        pid = child.pid

        def io_class():
            return subprocess.run(["ionice", "-p", str(pid)], capture_output=True,
                                  text=True).stdout.strip()

        before = io_class()
        for command in (f"ionice -c 3 -p {pid} cat magic.txt", f"ionice -c3 -p{pid} ls",
                        f"ionice -tp {pid} ls", f"ionice --pid={pid} ls",
                        f"ionice -P {pid} ls", f"ionice -u 0 ls",
                        f"ionice --uid=0 ls", f"nice ionice -c 3 -p {pid} ls",
                        f"ionice -n 7 -c 3 -p {pid} ls"):
            self.assertRefused(command, "changes processes already running")
            self.assertEqual(io_class(), before, command)
        # A long prefix with its value in the next word: unwrap_sure refuses.
        self.assertRefused(f"ionice --pi {pid} ls", None)
        self.assertRefused(f"ionice --pi={pid} ls", "changes processes already running")
        self.assertEqual(io_class(), before)
        self.assertRuns("ionice -c 3 ls")
        self.assertRuns("ionice -c 2 -n 7 cat magic.txt")

    def test_fake_program_never_runs(self):
        self.assertRefused("env -f vars.env ls", "env -f/--file")
        out = run_readonly("ls")
        self.assertIn("vars.env", out)
        self.assertFalse((self.project / "PWNED").exists())


if __name__ == "__main__":
    unittest.main()
