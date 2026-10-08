"""B2: `jarvis/v2/folders.py` — free, offline, temp roots only.

Every case points `HOME`, `config.PROJECT_FOLDER_ROOTS`,
`config.PROJECT_WORK_DIR`, the credential/data/repo dirs at a temporary
directory, so nothing here can make, adopt or even list a folder of the
owner's. The `/mnt/c` cases are refused on their *names* before anything is
looked up on disk.

What matters most, each written to fail if a rule is relaxed:

* **One `os.mkdir`, nothing inside.** No parents, no `exist_ok`, no file
  written, and an existing empty folder is adopted with no mkdir at all (O5).
* **A symlinked parent cannot carry the folder out** of the allowed roots or
  into a hidden or protected folder: the realpath of the nearest existing
  ancestor is judged too.
* **A race asks again.** What the owner approved is re-checked right before the
  mkdir; a folder that appeared or changed raises `FolderChanged`.
* **Only the confirmation handler can make one** (a grep over the tree).
"""
from __future__ import annotations

import ast
import builtins
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import folders
from jarvis.v2.approvals import ApprovalRequest
from jarvis.v2.folders import FolderChanged, FolderRefused, check_project_folder
from jarvis.v2.provider import Decision

REPO = Path(__file__).resolve().parents[2]


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(os.path.realpath(tmp.name))
        self.home = self.t / "home"
        self.work = self.home / "jarvis-work"
        self.outside = self.t / "outside"
        for d in (self.home, self.work, self.outside):
            d.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (
                ("PROJECT_FOLDER_ROOTS", ("~", "/mnt/c/Users/johnw")),
                ("PROJECT_WORK_DIR", "~/jarvis-work"),
                ("V2_CREDENTIAL_DIRS", ("~/creds", "~/.ssh")),
                ("V2_DATA_DIR", self.home / "data" / "v2"),
                ("REPO_ROOT", self.home / "code" / "Jarvis")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def refused(self, raw, pattern=None, **kwargs):
        with self.assertRaises(FolderRefused) as caught:
            check_project_folder(raw, **kwargs)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        return str(caught.exception)

    def approve(self, check, name="robotics"):
        request = ApprovalRequest(tool=folders.TOOL, args=check.approval_args(name))
        return folders.approved(request, Decision.ALLOW)


class InputChecks(Harness):
    def test_a_bare_name_maps_to_the_work_dir_slugged(self):
        self.assertEqual(check_project_folder("robotics").path, self.work / "robotics")
        self.assertEqual(check_project_folder("Robotics Club!").path,
                         self.work / "robotics-club")
        self.assertEqual(folders.folder_slug("École  Notes"), "ecole-notes")
        self.assertEqual(folders.folder_slug("..hidden"), "hidden")
        self.refused("!!!", "no letters or digits")

    def test_a_leading_tilde_slash_expands_once_and_nothing_else_does(self):
        self.assertEqual(check_project_folder("~/robotics").path, self.home / "robotics")
        self.assertEqual(check_project_folder("~/jarvis-work/x").path, self.work / "x")
        (self.home / "~").mkdir()
        # Only the leading one: a second `~` is a folder name, not $HOME again.
        self.assertEqual(check_project_folder("~/~/x").path, self.home / "~" / "x")
        self.refused("~root/x", "only a leading")
        self.refused("~", "only a leading")

    def test_a_full_path_is_used_as_given(self):
        self.assertEqual(check_project_folder(f"{self.home}/robotics/").path,
                         self.home / "robotics")
        self.assertEqual(check_project_folder(f"{self.home}//robotics").path,
                         self.home / "robotics")

    def test_relative_paths_dots_and_control_characters_are_refused(self):
        self.refused("a/b", "absolute")
        self.refused(f"{self.home}/../outside/x", r"`\.` or `\.\.`")
        self.refused(f"{self.home}/./x", r"`\.` or `\.\.`")
        self.refused(f"{self.home}/a\nb", "control character")
        self.refused(f"{self.home}/a\x00b", "control character")
        self.refused(f"{self.home}/a‮b", "control character")     # RTL override
        self.refused("", "no folder")
        self.refused(None, "text")

    def test_length_limits(self):
        self.refused(f"{self.home}/" + "a" * 256, "255 bytes")
        self.refused(f"{self.home}/" + "é" * 128, "255 bytes")         # 256 bytes
        self.assertEqual(check_project_folder(f"{self.home}/" + "a" * 255).state, "missing")
        deep = "/".join(["b" * 200] * 21)
        self.refused(f"{self.home}/{deep}", "4096")


class RefusalChecks(Harness):
    def test_hidden_components(self):
        self.refused("~/.ssh/x", "hidden")
        (self.home / "a").mkdir()
        self.refused("~/a/.secret", "hidden")
        self.refused("~/.config", "hidden")

    def test_only_strictly_below_a_root(self):
        self.refused(str(self.home), "is a root")
        self.refused("/mnt/c/Users/johnw", "is a root")
        self.refused(str(self.outside / "x"), "not inside")
        self.refused("/etc/jarvis", "not inside")

    def test_the_work_dir_itself_is_never_a_project_folder(self):
        self.refused("~/jarvis-work", "holds project folders")
        self.refused(str(self.work) + "/", "holds project folders")

    def test_credential_data_and_repo_dirs_inside_and_around(self):
        for d in ("creds", "data/v2", "code/Jarvis"):
            (self.home / d).mkdir(parents=True)
        self.refused("~/creds/x", "Jarvis's own")
        self.refused("~/data/v2/x", "Jarvis's own")
        self.refused("~/data", "Jarvis's own")             # holds the data dir
        self.refused("~/code/Jarvis/x", "Jarvis's own")
        self.refused("~/code", "Jarvis's own")             # holds the repo
        self.assertEqual(check_project_folder("~/data2").state, "missing")

    def test_appdata_and_windows_names_under_mnt_c(self):
        self.refused("/mnt/c/Users/johnw/AppData/x", "Jarvis's own")
        self.refused("/mnt/c/Users/johnw/appdata/Local/x", "Jarvis's own")
        for name in ("CON", "nul", "Com1", "LPT9", "aux.txt", "COM¹"):
            self.refused(f"/mnt/c/Users/johnw/{name}", "Windows reserves")
        for name in ("a:b", "what?", "trail.", "trail /x", 'q"uote', "pi|pe"):
            self.refused(f"/mnt/c/Users/johnw/{name}", "Windows can")
        # The same names are fine on the Linux side.
        self.assertEqual(check_project_folder("~/CON").state, "missing")

    def test_another_projects_root(self):
        (self.home / "school").mkdir()
        self.refused("~/school", "already a project", taken_roots=[str(self.home / "school")])
        self.refused("~/school", "already a project",
                     taken_roots=[str(self.home / "school") + "/"])

    def test_a_file_or_a_symlink_at_the_path(self):
        (self.home / "notes.txt").write_text("x")
        self.refused("~/notes.txt", "is a file")
        (self.home / "real").mkdir()
        (self.home / "alias").symlink_to(self.home / "real")
        self.refused("~/alias", "symlink")
        (self.home / "dangling").symlink_to(self.home / "nowhere")
        self.refused("~/dangling", "symlink")

    def test_a_symlinked_parent_cannot_escape(self):
        (self.home / "out").symlink_to(self.outside)
        self.refused("~/out/x", "resolves to")
        (self.home / ".hidden").mkdir()
        (self.home / "innocent").symlink_to(self.home / ".hidden")
        self.refused("~/innocent/x", "hidden")
        (self.home / "creds").mkdir()
        (self.home / "looks-fine").symlink_to(self.home / "creds")
        self.refused("~/looks-fine/x", "Jarvis's own")
        # A symlinked parent that stays inside the roots is fine.
        (self.home / "real").mkdir()
        (self.home / "via").symlink_to(self.home / "real")
        self.assertEqual(check_project_folder("~/via/x").path, self.home / "via" / "x")

    def test_the_parent_must_exist(self):
        self.refused("~/a/b", "parent folder .* does not exist")
        self.work.rmdir()
        message = self.refused("robotics", "does not exist yet")
        self.assertIn(str(self.work), message)


class StateChecks(Harness):
    def test_missing_asks_and_an_empty_folder_is_used_silently(self):
        missing = check_project_folder("robotics")
        self.assertEqual((missing.state, missing.action, missing.needs_confirmation),
                         ("missing", "create", True))
        (self.work / "robotics").mkdir()
        empty = check_project_folder("robotics")
        self.assertEqual((empty.state, empty.action, empty.needs_confirmation),
                         ("empty", "adopt", False))
        with patch.object(os, "mkdir", side_effect=AssertionError("mkdir")):
            self.assertEqual(folders.make_project_folder(empty, None), self.work / "robotics")

    def test_a_nonempty_folder_asks_with_its_count_and_git_and_reads_no_contents(self):
        target = self.work / "robotics"
        (target / ".git").mkdir(parents=True)
        (target / "secret.txt").write_text("never read")
        (target / "secret.txt").chmod(0)
        (target / "sub").mkdir()
        with patch.object(builtins, "open", side_effect=AssertionError("read a file")):
            check = check_project_folder("robotics")
        self.assertEqual((check.state, check.entries, check.git, check.needs_confirmation),
                         ("nonempty", 3, True, True))
        self.assertEqual(check.approval_args("Robotics"),
                         {"action": "adopt", "path": str(target), "name": "Robotics",
                          "entries": 3, "git": True})
        self.assertIn("3 entries, a git repository", check.describe())
        with self.assertRaises(PermissionError):
            folders.make_project_folder(check, None)
        with patch.object(os, "mkdir", side_effect=AssertionError("mkdir")):
            self.assertEqual(folders.make_project_folder(check, self.approve(check)), target)

    def test_exactly_one_mkdir_with_nothing_inside(self):
        check = check_project_folder("robotics")
        calls = []
        real_mkdir = os.mkdir

        def counting(*args, **kwargs):
            calls.append((args, kwargs))
            return real_mkdir(*args, **kwargs)

        with patch.object(os, "mkdir", side_effect=counting), \
                patch.object(os, "makedirs", side_effect=AssertionError("makedirs")), \
                patch.object(builtins, "open", side_effect=AssertionError("wrote a file")):
            made = folders.make_project_folder(check, self.approve(check))
        self.assertEqual(made, self.work / "robotics")
        self.assertEqual(calls, [((self.work / "robotics", 0o755), {})])
        self.assertTrue(made.is_dir())
        self.assertEqual(os.listdir(made), [])

    def test_a_missing_folder_is_never_made_without_the_yes(self):
        check = check_project_folder("robotics")
        with self.assertRaises(PermissionError):
            folders.make_project_folder(check, None)
        with self.assertRaises(PermissionError):
            folders.approved(ApprovalRequest(tool=folders.TOOL,
                                             args=check.approval_args("r")), Decision.DENY)
        with self.assertRaises(PermissionError):
            folders.approved(ApprovalRequest(tool="Bash", args={}), Decision.ALLOW)
        # A yes for another path is not a yes for this one.
        other = folders.Approved({**check.approval_args("r"), "path": str(self.work / "x")})
        with self.assertRaises(FolderChanged):
            folders.make_project_folder(check, other)
        self.assertFalse((self.work / "robotics").exists())

    def test_a_race_after_the_yes_asks_again(self):
        check = check_project_folder("robotics")
        approval = self.approve(check)
        (self.work / "robotics").mkdir()
        (self.work / "robotics" / "planted").mkdir()
        with self.assertRaises(FolderChanged) as caught:
            folders.make_project_folder(check, approval)
        self.assertEqual((caught.exception.check.state, caught.exception.check.entries),
                         ("nonempty", 1))

    def test_a_folder_appearing_inside_the_mkdir_asks_again(self):
        check = check_project_folder("robotics")
        real_mkdir = os.mkdir

        def racing(path, mode=0o777):
            real_mkdir(path)                       # somebody else won
            raise FileExistsError(path)

        with patch.object(os, "mkdir", side_effect=racing):
            with self.assertRaises(FolderChanged) as caught:
                folders.make_project_folder(check, self.approve(check))
        self.assertEqual(caught.exception.check.state, "empty")

    def test_a_count_change_in_an_approved_nonempty_folder_asks_again(self):
        target = self.work / "robotics"
        target.mkdir()
        (target / "a").mkdir()
        check = check_project_folder("robotics")
        approval = self.approve(check)
        (target / "b").mkdir()
        with self.assertRaises(FolderChanged):
            folders.make_project_folder(check, approval)


class ReachabilityChecks(unittest.TestCase):
    def test_only_the_confirmation_handler_calls_make_project_folder(self):
        allowed = {REPO / "jarvis" / "v2" / "folders.py",
                   REPO / "jarvis" / "v2" / "discord" / "project_commands.py"}
        callers = [p for p in (REPO / "jarvis").rglob("*.py")
                   if "make_project_folder" in p.read_text(encoding="utf-8")]
        self.assertEqual(set(callers), allowed)
        handler = (REPO / "jarvis" / "v2" / "discord" / "project_commands.py").read_text()
        self.assertEqual(handler.count("make_project_folder("), 1)

    def test_one_mkdir_in_folders_py_and_no_parents(self):
        tree = ast.parse((REPO / "jarvis" / "v2" / "folders.py").read_text(encoding="utf-8"))
        calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertEqual(calls.count("os.mkdir"), 1)
        mkdir = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "os.mkdir")
        self.assertEqual([ast.unparse(a) for a in mkdir.args], ["fresh.path", "493"])   # 0o755
        self.assertEqual(mkdir.keywords, [])
        for forbidden in ("os.makedirs", "Path.mkdir", "shutil.copytree"):
            self.assertNotIn(forbidden, calls)
        self.assertFalse([c for c in calls if c.endswith(".mkdir") and c != "os.mkdir"])
        self.assertFalse([c for c in calls if c.endswith((".write_text", ".write_bytes",
                                                          ".touch"))])

    def test_no_tool_route_or_mcp_reaches_folder_creation(self):
        from jarvis.v2.providers import fastpath
        self.assertNotIn("project_folder", fastpath.FAST_TOOLS)
        self.assertNotIn("project_propose", fastpath.FAST_TOOLS)
        for path in [REPO / "jarvis" / "v2" / "mcp.py", REPO / "jarvis" / "v2" / "hud_api.py",
                     REPO / "jarvis" / "v2" / "daemon.py",
                     *(REPO / "jarvis" / "tools").rglob("*.py"),
                     *(REPO / "jarvis" / "v2" / "tools").rglob("*.py")]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("make_project_folder", text, path.name)
            self.assertNotIn("check_project_folder", text, path.name)


if __name__ == "__main__":
    unittest.main()
