"""Round 2 review of GB-3 (6a43f78e): commands that asked on the parent
(31a05bda) and no longer ask, although they write into the VEX folder or
follow a link below it out of the folder. Each assertion passes on the
parent and fails on 6a43f78e."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.v2 import permissions

TAIL = "AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode"


class ReviewGb3Round2(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.users = Path(tmp.name) / "Users"
        (self.users / "Fixture" / TAIL / "sdk").mkdir(parents=True)
        (self.users / "Fixture" / TAIL / "a").write_text("x")
        self.auth = self.users / "Fixture/AppData/Roaming/Code/User/globalStorage/github.auth"
        self.auth.mkdir(parents=True)
        (self.auth / "token").write_text("s")
        self.work = Path(tmp.name) / "work"
        self.work.mkdir()
        env = patch.dict(os.environ, {"JARVIS_WINDOWS_USERS": str(self.users)})
        env.start()
        self.addCleanup(env.stop)
        self.one = f"{self.users}/Fixture/{TAIL}"

    def rule(self, command):
        got = permissions.always_ask_match("Bash", {"command": command}, cwd=str(self.work))
        return got[0] if got else None

    def check(self, *commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.rule(command), "credentials-path")

    # R1 (HIGH): a redirection on a reader segment writes into the folder
    # (its tools/ hold the toolchain and device tool the owner's editor runs).
    # `_vex_reader` looks at the program and options, never at redirections.
    def test_r1_redirection_writes_into_the_folder(self):
        self.check(
            f"echo hi > {self.one}/tools-x",
            f"cat {self.one}/a > {self.one}/b",
            f"echo hi >> {self.one}/b",
            f"echo hi >| {self.one}/b",
            f"cat {self.one}/a &> {self.one}/b",
            f"cat {self.one}/a 2> {self.one}/b",
            f"cat <> {self.one}/b",
            f"cat {self.one}/a | grep x > {self.one}/b",
            f"cd {self.one}; echo hi > b",
            f"cd {self.one}; file -C -m a",        # writes a.mgc in the cwd = the folder
        )

    # R2 (HIGH): an allowed reader follows a link already below the folder
    # out of it, through an option the exclusion list misses. GNU grep takes
    # `--deref` / `--dereference` as `--dereference-recursive`; GNU diff -r
    # follows links by default and -N prints a one-sided file whole.
    def test_r2_reader_follows_existing_link_with_content(self):
        (Path(self.one) / "l").symlink_to(self.auth)
        self.check(
            f"grep --deref s {self.one}",
            f"grep --dereference -n s {self.one}",
            f"diff -rN {self.one}/sdk {self.one}",
            f"diff -r {self.one}/sdk {self.one}",
        )

    # R3 (MED): names and sizes beyond the link (`ls -L -R`, `du -L`).
    def test_r3_reader_follows_existing_link_names_sizes(self):
        (Path(self.one) / "l").symlink_to(self.auth)
        self.check(
            f"ls -LR {self.one}",
            f"ls -R --dereference {self.one}",
            f"du -aL {self.one}",
            f"du --dereference -a {self.one}",
        )


if __name__ == "__main__":
    unittest.main()
