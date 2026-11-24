"""Review of GB-3 (5d975cbc..e70cd729): commands that asked on the parent and
reach outside the VEX folder, yet no longer ask. Each assertion passes on
5d975cbc and fails on e70cd729."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.v2 import permissions

TAIL = "AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode"


class ReviewGb3(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.users = Path(tmp.name) / "Users"
        (self.users / "Fixture" / TAIL / "sdk").mkdir(parents=True)
        self.auth = self.users / "Fixture/AppData/Roaming/Code/User/globalStorage/github.auth"
        self.auth.mkdir(parents=True)
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

    # F1 (HIGH): an option that moves the program into the excepted folder,
    # then a relative operand climbs out of it.
    def test_f1_chdir_option_then_relative_operand(self):
        self.check(
            f"tar -C {self.one} -cf /tmp/o.tar ../github.auth",
            f"env -C {self.one} cat ../github.auth/token",
            f"env --chdir={self.one} cat ../github.auth/token",
            f"git -C {self.one} diff --no-index ../github.auth/token /dev/null",
            f"make -C {self.one} -f ../github.auth/token",
            f"find {self.one} -maxdepth 0 -exec cat {{}}/../github.auth/token ';'",
        )

    # F2 (HIGH): a link made inside the folder on the same line (or earlier)
    # does not exist when the line is judged, so `realpath` equals the path.
    def test_f2_link_made_in_the_folder(self):
        self.check(
            f"ln -s ../github.auth {self.one}/l; cat {self.one}/l/token",
            f"ln -s ../github.auth {self.one}/l && grep -R token {self.one}",
            f"ln -s ../github.auth {self.one}/l && zip -r /tmp/o.zip {self.one}",
        )

    def test_f2b_walk_follows_an_existing_link_below_the_folder(self):
        (Path(self.one) / "l").symlink_to(self.auth)
        self.check(f"grep -R token {self.one}", f"zip -r /tmp/o.zip {self.one}",
                   f"find -L {self.one} -type f", f"cp -rL {self.one} /tmp/x")

    # F3 (MED): the `*` glob skips Public/Default/Default User/All Users
    # without checking them, but the shell's `*` matches them (All Users is a
    # junction to ProgramData, writable by any user).
    def test_f3_glob_skips_shared_profiles_the_shell_matches(self):
        shared = self.users / "All Users" / Path(TAIL).parent
        shared.mkdir(parents=True)
        (shared / "vexrobotics.vexcode").symlink_to(self.auth)
        self.check(f"cat {self.users}/*/{TAIL}/token")

    # F4 (MED): bash extglob in the profile's place (`@`, `+`, `(`, `)` and
    # space are plain name characters to `_VEX_SDK_PIECE`), so the profile
    # and link checks look at a name that does not exist.
    def test_f4_extglob_profile(self):
        public = self.users / "Public" / Path(TAIL).parent
        public.mkdir(parents=True)
        (public / "vexrobotics.vexcode").symlink_to(self.auth)
        self.check(f"shopt -s extglob\ncat {self.users}/@(Public)/{TAIL}/token",
                   f"shopt -s extglob\ncat '{self.users}/'+(Public)/{TAIL}/token")

    # F5 (LOW): Unicode case folding: `ſ`.casefold() == 's', `K` (Kelvin) == 'k';
    # on a case-sensitive file system these are other AppData folders.
    def test_f5_non_ascii_casefold(self):
        self.check(
            f"cat {self.users}/Fixture/AppData/Roaming/Code/User/globalſtorage/vexrobotics.vexcode/x",
            f"cat {self.users}/Fixture/AppData/Roaming/Code/User/globalStorage/vexrobotic\u017f.vexcode/x",
        )


if __name__ == "__main__":
    unittest.main()
