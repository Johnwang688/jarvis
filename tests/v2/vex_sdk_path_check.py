"""GB-3 (owner decision CU-D12, 2026-11-18): v2's shell rule `credentials-path`
does not ask for a path at or below a Windows profile's VEX extension folder
(`config.WINDOWS_VEX_SDK`), and asks for everything else it asked before."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import permissions

TAIL = "AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode"


class VexSdkPath(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.users = Path(tmp.name) / "Users"
        for profile in ("Fixture", "Other"):
            (self.users / profile / TAIL / "sdk" / "cpp" / "V5").mkdir(parents=True)
            (self.users / profile / TAIL / "tools").mkdir()
            (self.users / profile / ".ssh").mkdir()
        (self.users / "Public").mkdir()
        env = patch.dict(os.environ, {"JARVIS_WINDOWS_USERS": str(self.users)})
        env.start()
        self.addCleanup(env.stop)
        self.one = f"{self.users}/Fixture/{TAIL}"
        self.any = f"{self.users}/*/{TAIL}"

    def rule(self, command):
        got = permissions.always_ask_match("Bash", {"command": command})
        return got[0] if got else None

    def test_sdk_reads_listings_and_walks_do_not_ask(self):
        for command in (
                f"sed -n 30,60p {self.one}/sdk/cpp/V5/vexv5/include/vex_optical.h",
                f"ls {self.one}/sdk/cpp/V5/ 2>&1",
                f"ls {self.one}/sdk",
                f"strings -a {self.one}/sdk/cpp/V5/libv5rt.a | grep -i '^vexTask' | sort -u",
                f"grep -rn pressed {self.one}/sdk/cpp/V5/include",
                f"find {self.one}/sdk -name '*.h'",
                f"ls -d {self.any}/sdk/cpp/V5 2>/dev/null | head -1",
                f"grep -rn pressed {self.any}/sdk/cpp",
                f"ls {self.one.replace('AppData', 'APPDATA').replace('sdk', 'SDK')}/sdk",
                f"ls {self.one.replace('/Code/', '/Cursor/')}/sdk",
                f"ls -d {self.any} 2>&1 | head",                    # the folder itself (owner, 2026-11-19)
                f"ls {self.one}",
                f"ls {self.one}/tools"):
            with self.subTest(command=command):
                self.assertIsNone(self.rule(command))

    def test_everything_around_the_sdk_still_asks(self):
        for command in (
                f"cat {self.one}/sdk/../tools/x",                   # any `..` gets no exception
                f"ar x {self.one}/sdk/cpp/V5/libv5rt.a 2>&1",       # not on the readers' list
                f"cp {self.one}/sdk/a.h /tmp/a.h",
                f"touch {self.one}/sdk/new",
                f"ln -s ../github.auth {self.one}/l",
                f"sed -i s/a/b/ {self.one}/sdk/a.h",
                f"timeout 5 cat {self.one}/sdk/a.h",                # a wrapper in front: asked
                f"cat {self.one}/../github.auth/x",                 # out of the folder through `..`
                f"cat {self.one}/sdk:{self.users}/Fixture/AppData/Roaming/secret/token",  # a second path
                f"cat {self.one}/sdk={self.users}/Fixture/AppData/Roaming/secret/token",
                f"cat {self.one}/sdk/a:stream",                     # an alternate data stream
                f"cat {self.one}/sdk\\\\..\\\\..\\\\github.auth",    # Windows separators, escaped for the shell
                f"cat {self.one}//sdk/a",
                f"cat {self.one}/sdk/a {self.users}/Fixture/AppData/Roaming/secret/token",
                f"cp {self.users}/Fixture/AppData/Roaming/secret/token {self.one}/sdk/",
                f"cd {self.one} && cat ../github.auth/x",
                f"cat {self.one}/sdk/../../github.auth/x",
                f"cat {self.one}./x",                               # Windows trims the dot: another spelling
                f"cat '{self.one} /x'",
                f"ls {self.users}/*/AppData/Roaming/Code/User/globalStorage",
                f"ls {self.users}/*/AppData/Roaming/Code/User/globalStorage/*",
                f"ls {self.users}/*/AppData/*/Code/User/globalStorage/vexrobotics.vexcode/sdk",
                f"ls {self.users}/*/{TAIL}/sdk/*/../../tools",
                f"ls {self.users}/{{Fixture,Other}}/AppData",
                f"cat {self.users}/Fixture/AppData/Roaming/Code/User/globalStorage/github.auth/x",
                f"cat {self.users}/Fixture/AppData/Roaming/Code/User/settings.json",
                f"grep -r token {self.users}/Fixture/AppData",
                f"find {self.users}/*/AppData/Roaming/Code -name '*.json'",
                f"cat {self.users}/Fixture/.ssh/id_ed25519",
                f"cat {self.users}/.hidden/{TAIL}/sdk/x",           # a dot entry is no profile
                f"cat {self.users}/Public/{TAIL}/sdk/x"):
            with self.subTest(command=command):
                self.assertEqual(self.rule(command), "credentials-path")

    def test_a_link_on_the_way_gets_no_exception(self):
        secret = self.users / "Fixture" / "AppData" / "Roaming" / "secrets"
        secret.mkdir()
        (self.users / "Fixture" / TAIL / "sdk" / "linked").symlink_to(secret)
        (self.users / "Fixture" / TAIL / "tools" / "linked").symlink_to(secret)
        self.assertEqual(self.rule(f"cat {self.one}/tools/linked/token"), "credentials-path")
        self.assertEqual(self.rule(f"cat {self.one}/sdk/linked/token"), "credentials-path")
        # The SDK folder itself a link, in a profile the glob can match.
        other = self.users / "Other" / TAIL / "sdk"
        for child in sorted(other.rglob("*"), reverse=True):
            child.rmdir()
        other.rmdir()
        other.symlink_to(secret)
        self.assertEqual(self.rule(f"ls {self.users}/Other/{TAIL}/sdk"), "credentials-path")
        self.assertEqual(self.rule(f"ls {self.any}/sdk"), "credentials-path")
        self.assertIsNone(self.rule(f"ls {self.one}/sdk"))  # the unlinked profile keeps it

    def test_an_unlistable_users_folder_gets_no_glob_exception(self):
        with patch.object(permissions.os, "listdir", side_effect=OSError):
            self.assertEqual(self.rule(f"ls {self.any}/sdk"), "credentials-path")
        with patch.object(permissions, "_VEX_SDK_PROFILES", 1):
            self.assertEqual(self.rule(f"ls {self.any}/sdk"), "credentials-path")

    def test_the_exception_is_exactly_the_vex_folder(self):
        self.assertEqual({sdk[-2:] for sdk in config.WINDOWS_VEX_SDK}, {("globalstorage", "vexrobotics.vexcode")})
        sibling = f"ls {self.users}/Fixture/AppData/Roaming/Code/User/globalStorage/github.auth"
        widened = tuple(sdk[:-1] for sdk in config.WINDOWS_VEX_SDK)
        with patch.object(config, "WINDOWS_VEX_SDK", widened):  # the mutation this suite must catch
            self.assertIsNone(self.rule(sibling))
        self.assertEqual(self.rule(sibling), "credentials-path")


if __name__ == "__main__":
    unittest.main()
