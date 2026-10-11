"""VX-1: the VEX backend (editor plan §2.9 and §3, decisions V-1 to V-5).

Free and hermetic. **No real VEX tool ever runs and no device is touched**:
`vex.json` (a temp file) names a fake `make.exe` and a fake `vexcom.exe`
(`tests/v2/vex_fake/`, Python scripts started through their shebang) inside a
temp tree laid out like the extension's folders, under a temp "Windows
drive" (`config.VEX_MOUNT_ROOT` points at it). HOME and every config path are
temp; every daemon listens on ephemeral loopback ports, never 8402/8403/8405.
Gate probes are in-process: `shell._run` is a recorder, so a refused command
cannot run whatever the verdict. Placeholder names only.

What matters most, each written to fail with its guard removed:

* **Download, the brain and device info are owner-only HUD routes.** No
  tool, MCP tool, fast-path tool or Discord verb names them (a grep in the
  terminal and archive suites' pattern), and every route refuses the API
  listener, the preview listener, a missing or foreign Origin, a rebound
  Host, and a body carrying `path` or `args`.
* **An agent command that names `vexcom` is DENY**, in every spelling the
  rule lists — never run, never asked, under an allowlist entry and under
  mode "all" (v1), and never a card (v2 layer 1).
* **`vexbuild` asks like any command** (and its module through `python -m`).
* **Paths are fixed by `vex.json`**, re-checked at every use; a request never
  names an executable or a path to run; a project comes from its id.
* The exact argv of build, rebuild, clean, download and the brain terminal,
  Windows path conversion, slot and name refusals, one build per project
  (across the daemon and `vexbuild`), one device action at a time, the build
  timeout, the template unpacker's refusals, the `project_folder` card for a
  new project, the program environment, and no output on the bus, in the log
  or on disk.
"""
from __future__ import annotations

import base64
import http.client
import inspect
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import websocket  # noqa: E402  (websocket-client, already a dependency)

from jarvis import config, permissions as v1_permissions, rules, tools  # noqa: E402
from jarvis.tools import files as v1_files, shell  # noqa: E402
from jarvis.v2 import permissions as v2_permissions, terminals as T, vex, vex_setup  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import PermissionProfile as PP, ProviderName as PN, Role  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

FAKES = Path(__file__).resolve().parent / "vex_fake"
FAKE_SHELL = Path(__file__).resolve().parent / "terminal_fake" / "sh"
LIVE_PORTS = {8402, 8403, 8405}
SDK = "V5_20240802_15_00_00"
PY_SDK = "V5_1_0_1_25"
EXT = "vexrobotics.vexcode-0.8.2026020401"
TOOLS = {"clang": "clang/bin/clang.exe", "ar": "gcc/bin/arm-none-eabi-ar.exe",
         "ld": "gcc/bin/arm-none-eabi-ld.exe", "objcopy": "gcc/bin/arm-none-eabi-objcopy.exe",
         "size": "gcc/bin/arm-none-eabi-size.exe", "make": "tools/bin/make.exe"}


def eventually(predicate, timeout=8.0, what="condition"):
    until = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > until:
            raise AssertionError(f"{what} did not settle")
        time.sleep(0.02)


class Fake:
    """A provider that is never asked anything here."""

    def __init__(self, name):
        self.name = name

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


# -- the fake world ----------------------------------------------------------------------

class World:
    """A temp tree shaped like the VEX extension's folders on a Windows drive,
    holding fakes only."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.mnt = root / "mnt"
        self.user = self.mnt / "c" / "Users" / "t"
        self.storage = self.user / "AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode"
        self.extensions = self.user / ".vscode" / "extensions"
        self.templates = self.extensions / EXT / "resources/templates/projects/V5"
        self.robotics = self.user / "Documents" / "Robotics"
        self.vexcom_dir = self.storage / "tools/vexcom/1_0_2_2/win32"
        self.chain = self.storage / "tools/cpp/toolchain_win32"
        for d in (self.home, self.vexcom_dir, self.templates, self.robotics,
                  self.storage / "sdk/cpp/V5" / SDK / "vexv5",
                  self.storage / "sdk/python/V5" / PY_SDK):
            d.mkdir(parents=True, exist_ok=True)
        self.vexcom = self._install(FAKES / "vexcom.py", self.vexcom_dir / "vexcom.exe")
        self.exes = {}
        for key, rel in TOOLS.items():
            source = FAKES / "make.py" if key == "make" else None
            self.exes[key] = self._install(source, self.chain / rel)
        self._templates()

    @staticmethod
    def _install(source, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source is None:
            target.write_text("#!/bin/sh\nexit 99\n")
        else:
            shutil.copyfile(source, target)
        target.chmod(0o755)
        return target

    def _templates(self):
        cpp = {"makefile": "include vex/mkenv.mk\nall: build\n", "vex/mkenv.mk": "CC = clang\n",
               "vex/mkrules.mk": "clean:\n\t@echo clean\n", "include/vex.h": "#pragma once\n",
               "src/main.cpp": "int main() { return 0; }\n", "skip.v5code": "x"}
        self.zip(self.templates / "cpp_v5_vsc_empty.zip", cpp)
        self.zip(self.templates / "py_v5_vsc_empty.zip", {"src/main.py": "print('hi')\n"})
        self.zip(self.templates / "cpp_v5_vsc_competition_template.zip", cpp)
        (self.templates / "index.json").write_text(json.dumps({"version": "1", "examples": [
            {"category": "Templates", "samples": [
                {"name": "Empty   Project", "files": [{"name": "cpp_v5_vsc_empty.zip"}]}]}]}))
        (self.templates / "Clawbot_Competition.zip").write_bytes(b"not a vsc template")

    @staticmethod
    def zip(path: Path, entries: dict) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, text in entries.items():
                archive.writestr(name, text)

    def doc(self, **over) -> dict:
        doc = {"version": 1, "vexcom": str(self.vexcom), "vexcom_version": "1.0.2.2",
               **{k: str(v) for k, v in self.exes.items()},
               "cpp_sdk": str(self.storage / "sdk/cpp/V5" / SDK),
               "python_sdk": str(self.storage / "sdk/python/V5" / PY_SDK),
               "templates": str(self.templates), "extension_version": "0.8.2026020401",
               "project_folder": str(self.robotics)}
        doc.update(over)
        return {k: v for k, v in doc.items() if v is not None}

    def write_config(self, **over) -> None:
        path = Path(config.VEX_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.doc(**over)))

    def project(self, name="override", language="cpp", slot=1, printf_float=True,
                folder=None) -> Path:
        root = folder or (self.robotics / name)
        (root / ".vscode").mkdir(parents=True, exist_ok=True)
        project = {"name": name, "description": "", "creationDate": "x", "platform": "V5",
                   "language": language, "slot": slot, "sdkVersion": SDK}
        if language == "cpp":
            project["cpp"] = {"includePath": [], "printf_float": printf_float}
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "main.cpp").write_text("int main() {}\n")
            (root / "vex").mkdir(exist_ok=True)
            (root / "vex" / "mkenv.mk").write_text("CC = clang\n")
            (root / "makefile").write_text("include vex/mkenv.mk\n")
        else:
            project["python"] = {"main": "src/main.py"}
        (root / ".vscode" / "vex_project_settings.json").write_text(json.dumps(
            {"extension": {"version": "0.8.2026020401", "json": 2}, "project": project}))
        return root

    def calls(self, tool: str) -> list[dict]:
        path = (self.chain / "tools/bin" if tool == "make" else self.vexcom_dir) / "calls.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def windows(self, path: Path) -> str:
        rel = path.relative_to(self.mnt).parts
        return rel[0].upper() + ":\\" + "\\".join(rel[1:])


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.world = World(self.root)
        env = patch.dict(os.environ, {
            "HOME": str(self.world.home), "XDG_DATA_HOME": str(self.root / "xdg"),
            "TMPDIR": str(self.root), "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json"),
            # Set so the program-environment check can see they never pass.
            "OPENROUTER_API_KEY": "placeholder-not-a-key", "JARVIS_PROBE_X": "1"})
        env.start()
        self.addCleanup(env.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, "tempdir", None)
        cfg = self.world.home / ".config" / "jarvis"
        for name, value in dict(
                V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=cfg / "allowlist.json",
                MODELS_PATH=cfg / "models.json", PROVIDER_DEFAULTS_PATH=cfg / "provider_defaults.json",
                DISCORD_GUILD_PATH=cfg / "discord_guild.json", ROUTING_PATH=cfg / "routing.json",
                CODEX_CATALOG_PATH=self.root / "codex-models.json", VEX_PATH=cfg / "vex.json",
                VEX_STORAGE_ROOTS=(str(self.world.storage),),
                VEX_EXTENSION_ROOTS=(str(self.world.extensions),),
                VEX_MOUNT_ROOT=str(self.world.mnt), VEX_PROJECT_FOLDER=str(self.world.robotics),
                VEXBUILD_PATH=self.world.home / ".local" / "bin" / "vexbuild",
                PROJECT_FOLDER_ROOTS=("~", str(self.world.user)), PROJECT_WORK_DIR="~/jarvis-work",
                V2_CREDENTIAL_DIRS=("~/.ssh",), TERMINAL_SHELL=str(FAKE_SHELL)).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.world.write_config()

    def assert_refused(self, fn, pattern, exc=vex.VexError):
        with self.assertRaises(exc) as caught:
            fn()
        self.assertRegex(str(caught.exception), pattern)
        return str(caught.exception)


class DaemonBase(Base):
    def setUp(self):
        super().setUp()
        self.stores = Stores()
        self.daemon = Daemon(self.stores, {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        ports = {self.daemon.port, self.daemon.face_port, self.daemon.workshop_port}
        self.assertFalse(ports & LIVE_PORTS, "a test daemon must never hold a live port")
        self.folder = self.world.project()
        self.project = self.stores.projects.create("override", str(self.folder))

    def request(self, method, path, body=None, *, port=None, origin="hud", host=None,
                headers=None, status=None):
        port = port or self.daemon.face_port
        sent = dict(headers or {})
        if body is not None:
            sent["Content-Type"] = "application/json"
        if origin == "hud":
            sent["Origin"] = f"http://127.0.0.1:{port}"
        elif origin is not None:
            sent["Origin"] = origin
        if host is not None:
            sent["Host"] = host
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, sent)
            response = conn.getresponse()
            content = response.read()
        finally:
            conn.close()
        if status is not None:
            self.assertEqual(response.status, status, content[:400])
        return response.status, (json.loads(content) if content else None)

    def owner(self, method, path, body=None, status=200):
        if body is None and method == "POST":
            body = {}
        return self.request(method, path, body, status=status)[1]

    def build(self, action="build", wait=True):
        started = self.owner("POST", f"/projects/{self.project.id}/vex/build",
                             {"action": action}, status=202)
        if not wait:
            return started
        return eventually(lambda: (lambda b: b if b["state"] != "running" else None)(
            self.owner("GET", f"/vex/builds/{started['build_id']}")), what="the build")

    def ended(self, tid):
        return eventually(lambda: self.daemon.terminals.get(tid).exited, what=f"terminal {tid}")


# -- configuration: fixed paths, checked at every use -------------------------------------------

class Configuration(Base):
    def test_a_good_config_loads_and_status_reports_it(self):
        cfg = vex.load_config()
        self.assertEqual(cfg.vexcom, str(self.world.vexcom))
        self.assertEqual(cfg.cpp_sdk_version, SDK)
        self.assertEqual(cfg.python_sdk_version, PY_SDK)

    def test_a_path_outside_the_extension_folders_is_refused(self):
        outside = World._install(FAKES / "make.py", self.root / "elsewhere" / "make.exe")
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=str(outside))),
                            "make is outside")
        # A folder beside the extension's, under the same parent, is outside too.
        sibling = World._install(FAKES / "make.py",
                                 self.world.storage.parent / "someone.else" / "make.exe")
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=str(sibling))),
                            "make is outside")
        folder = self.root / "sdk-elsewhere" / SDK
        folder.mkdir(parents=True)
        self.assert_refused(lambda: vex.parse_config(self.world.doc(cpp_sdk=str(folder))),
                            "cpp_sdk is outside")

    def test_a_relative_or_unnormal_path_is_refused(self):
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make="tools/bin/make.exe")),
                            "absolute")
        dotted = str(self.world.storage) + "/tools/../tools/cpp/toolchain_win32/tools/bin/make.exe"
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=dotted)), "absolute")

    def test_a_symlink_anywhere_in_the_path_is_refused(self):
        link = self.world.chain / "tools/bin/make-link.exe"
        link.symlink_to(self.world.exes["make"])
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=str(link))), "symlink")
        folder = self.world.chain / "linked"
        folder.symlink_to(self.world.chain / "tools")
        self.assert_refused(lambda: vex.parse_config(
            self.world.doc(make=str(folder / "bin/make.exe"))), "symlink")
        # Even one pointing outside to a plausible file.
        out = World._install(FAKES / "make.py", self.root / "evil" / "make.exe")
        planted = self.world.chain / "tools/bin/planted.exe"
        planted.symlink_to(out)
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=str(planted))), "symlink")

    def test_a_missing_or_unrunnable_file_or_unknown_key_is_refused(self):
        self.assert_refused(lambda: vex.parse_config(
            self.world.doc(make=str(self.world.chain / "tools/bin/gone.exe"))), "missing")
        plain = self.world.chain / "tools/bin/plain.exe"
        plain.write_text("x")
        plain.chmod(0o644)
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=str(plain))), "executable")
        self.assert_refused(lambda: vex.parse_config({**self.world.doc(), "args": ["-x"]}),
                            "keys setup never writes")
        self.assert_refused(lambda: vex.parse_config(self.world.doc(make=None)), "no make")

    def test_templates_must_sit_in_a_vexcode_install(self):
        other = self.world.extensions / "someone.else-1.0" / "t"
        other.mkdir(parents=True)
        self.assert_refused(lambda: vex.parse_config(self.world.doc(templates=str(other))),
                            "templates is outside")

    def test_the_file_itself_is_never_followed_read_past_its_cap_or_a_fifo(self):
        path = Path(config.VEX_PATH)
        real = path.with_name("real.json")
        path.rename(real)
        path.symlink_to(real)
        self.assert_refused(vex.load_config, "symlink")
        path.unlink()
        os.mkfifo(path)
        self.assert_refused(vex.load_config, "not a regular file")   # and did not hang
        path.unlink()
        path.write_text(" " * (vex.CONFIG_CAP + 1))
        self.assert_refused(vex.load_config, "larger")
        path.unlink()
        self.assert_refused(vex.load_config, "not set up")

    def test_each_use_rechecks_the_paths(self):
        cfg = vex.load_config()
        self.assertTrue(cfg.make)
        self.world.exes["make"].unlink()
        self.assert_refused(vex.load_config, "jarvis vex setup")


# -- vex.json is gate state, v1 and v2 alike ---------------------------------------------------------

class GateState(Base):
    def test_every_agent_write_tool_refuses_vex_json(self):
        target = str(config.VEX_PATH)
        before = Path(target).read_bytes()
        protected = {p for p in v1_files._protected_state()}
        self.assertIn(Path(target).resolve(), protected)
        self.assertEqual(v2_permissions.protected_paths(), v1_files._protected_state())
        # The sibling of the allowlist is protected by name even if VEX_PATH moves.
        self.assertIn(Path(config.ALLOWLIST_PATH).parent.resolve() / "vex.json", protected)
        # v1 write tools.
        self.assertTrue(v1_files._self_protected(Path(target)))
        self.assertIn("safety layer", v1_files.write_file(target, "{}"))
        self.assertIn("safety layer", v1_files.edit_file(target, "{", "["))
        # v2 file tools and layer 1, for Claude's and Codex's spellings.
        asker = Asker()
        permit = v2_permissions.build_permit(context(self.root), asker)
        for tool, args in (("Write", {"file_path": target, "content": "{}"}),
                           ("Edit", {"file_path": target, "old_string": "{", "new_string": "["}),
                           ("apply_patch", {"path": target}), ("write_file", {"path": target})):
            self.assertEqual(permit(tool, args), Decision.DENY, tool)
        self.assertEqual(asker.seen, [])
        # A shell write: certain → DENY in v2; never ALLOW in v1.
        line = f"cp /tmp/x.json {target}"
        self.assertIsNotNone(v2_permissions.denied_command(line))
        self.assertNotEqual(v1_permissions.static_verdict(line).decision, rules.ALLOW)
        self.assertEqual(Path(target).read_bytes(), before)

    def test_vex_json_is_protected_wherever_it_lives(self):
        # By its config path when it is not beside the allowlist…
        elsewhere = self.root / "elsewhere" / "vex-paths.json"
        with patch.object(config, "VEX_PATH", elsewhere):
            self.assertIn(elsewhere.resolve(), v1_files._protected_state())
            self.assertTrue(v1_files._self_protected(elsewhere))
            self.assertIsNotNone(v2_permissions.denied_file(str(elsewhere)))
        # …and by name beside the allowlist even when the config path moved.
        beside = Path(config.ALLOWLIST_PATH).parent / "vex.json"
        with patch.object(config, "VEX_PATH", elsewhere):
            self.assertTrue(v1_files._self_protected(beside))


# -- the vexcom DENY and the vexbuild ASK -----------------------------------------------------------

VEXCOM_EXE = "/mnt/c/Users/placeholder/AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode/tools/vexcom/1_0_2_2/win32/vexcom.exe"
SHAPES = {
    "bare": "vexcom --slot 1 --write a.bin",
    "exe": "vexcom.exe --user",
    "interop path": f"{VEXCOM_EXE} --user",
    "linux binary path": "/mnt/c/Users/placeholder/x/linux-x64/vexcom --user",
    "upper case": "/opt/x/VEXCOM.EXE --user",
    "windows path": r"cmd.exe /c C:\Users\placeholder\x\vexcom.exe --slot 1",
    "cmd caret": "cmd.exe /c vex^com.exe --slot 1",
    "cmd variable": "cmd /c vex%X%com.exe --slot 1",
    "wrapper": "env FOO=1 nohup timeout 5 vexcom --user",
    "sh -c": "sh -c 'vexcom --user'",
    "bash -lc": 'bash -lc "/mnt/c/a/vexcom.exe --slot 2 --write b.bin"',
    "after a separator": "ls && vexcom --user",
    "in a substitution": "echo $(vexcom --user)",
    "powershell -c": "powershell.exe -c \"& 'C:\\x\\vexcom.exe' --user\"",
    "powershell backtick": 'pwsh -Command "& vex`com.exe --user"',
    "powershell encoded": "powershell -EncodedCommand "
                          + base64.b64encode("& 'C:\\a\\vexcom.exe' --user".encode("utf-16-le")).decode(),
    "powershell -enc": "pwsh -enc " + base64.b64encode("vexcom --user".encode("utf-16-le")).decode(),
    "quotes": "v'e'x\"c\"om --user",
    "backslash": "vex\\com --user",
    "ansi-c": "$'\\x76excom' --user",
    "empty expansion": "vex${X}com --user",
    "empty substitution": "vex$(true)com --user",
    "brace": "vex{com,} --user",
    "continuation": "vex\\\ncom --user",
    "glob": "find /mnt/c -name 'v*m.exe' -exec {} --user \\;",
    "copy": f"cp {VEXCOM_EXE} /tmp/renamed.exe",
    "xargs": "xargs vexcom < args.txt",
    "fullwidth": "ＶＥＸＣＯＭ --user",
}
ORDINARY = {   # stays as it was: never DENY for not naming vexcom
    "make -j4": rules.ALLOW, "ls *.exe": rules.ALLOW, "ls *": rules.ALLOW,
    "cat .vscode/vex_project_settings.json": rules.ALLOW, "git commit -m 'fix the build'": rules.ALLOW,
    "grep -rn vex_project src": rules.ALLOW, "echo ${HOME}/vex/com": rules.ALLOW,
    "printf '%s %d' a 1": rules.ALLOW, "python3 -m pytest": rules.ALLOW,
}


class Asker:
    human_backed = True

    def __init__(self, decision=Decision.ALLOW):
        self.decision, self.seen = decision, []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


def context(cwd, profile=PP.AUTO):
    return v2_permissions.PermitContext(
        thread_id="abcd1234", brief=Brief(role=Role.IMPLEMENTER, cwd=str(cwd), profile=profile),
        provider="claude")


class Rules(Base):
    def setUp(self):
        super().setUp()
        self.ran = []
        real = shell._run
        shell._run = lambda command, *a, **k: self.ran.append(command) or "[exit 0]"
        self.addCleanup(setattr, shell, "_run", real)
        self.addCleanup(v1_permissions.set_mode, "ask")

    def test_every_spelling_that_names_vexcom_is_deny(self):
        for label, command in SHAPES.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.DENY, command)
                self.assertIn("vexcom", verdict.reason)
                self.assertEqual(v1_permissions.command_verdict(
                    "run_command", {"command": command}).decision, rules.DENY)
                self.assertIsNotNone(v2_permissions.denied_command(command))

    def test_ordinary_commands_keep_their_verdicts(self):
        for command, expected in ORDINARY.items():
            self.assertEqual(rules.decide(command).decision, expected, command)
        # The accepted cost, stated: a line that merely mentions the word is refused too.
        self.assertEqual(rules.decide("grep -rn vexcom docs").decision, rules.DENY)

    def test_v1_never_runs_or_asks_even_allowlisted_or_in_mode_all(self):
        Path(config.ALLOWLIST_PATH).write_text(json.dumps([
            {"tool": "run_command", "prefix": p}
            for p in ("vexcom", "vexcom.exe", "cmd.exe", "cmd", "sh", "bash", "powershell.exe",
                      "pwsh", "env", "ls", "cp", "find", "xargs", "echo")]))
        asked = []

        def approver(tool, args):
            asked.append(args)
            return True
        gate = v1_permissions.gate(approver)
        for mode in ("ask", "all"):
            v1_permissions.set_mode(mode)
            for label, command in SHAPES.items():
                with self.subTest(mode=mode, shape=label):
                    result = tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}), gate)
                    self.assertIn("vexcom", str(result.text))
        self.assertEqual(self.ran, [], "a command naming vexcom ran")
        self.assertEqual(asked, [], "a command naming vexcom was asked about")

    def test_v2_layer_1_denies_and_never_raises_a_card(self):
        asker = Asker()
        for profile in (PP.AUTO, PP.ASK):
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label, command in SHAPES.items():
                with self.subTest(profile=profile.value, shape=label):
                    self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
            # Codex hands an argv array.
            self.assertEqual(permit("shell", {"command": ["bash", "-lc", "vexcom --user"]}),
                             Decision.DENY)
        self.assertEqual(asker.seen, [], "a card was raised for vexcom")

    def test_vexbuild_asks_like_any_command(self):
        for command in ("vexbuild", "vexbuild --clean", "python -m jarvis.v2.vex build",
                        "python3 -mjarvis.v2.vex build", "/x/.venv/bin/python -I -m jarvis.v2.vex build"):
            self.assertEqual(rules.decide(command).decision, rules.ASK, command)
        # v1: the human is asked, and a no runs nothing.
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args) or False)
        tools.dispatch("run_command", json.dumps({"command": "vexbuild", "reason": "probe"}), gate)
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.ran, [])
        # …unless the owner allowlisted exactly this tool.
        Path(config.ALLOWLIST_PATH).write_text(json.dumps([{"tool": "run_command",
                                                            "prefix": "vexbuild"}]))
        tools.dispatch("run_command", json.dumps({"command": "vexbuild", "reason": "probe"}), gate)
        self.assertEqual((len(asked), self.ran), (1, ["vexbuild"]))
        # v2 under ASK: the human is asked.
        asker = Asker(Decision.DENY)
        permit = v2_permissions.build_permit(context(self.root, PP.ASK), asker)
        self.assertEqual(permit("Bash", {"command": "vexbuild"}), Decision.ALLOW)  # allowlisted
        Path(config.ALLOWLIST_PATH).write_text("[]")
        self.assertEqual(permit("Bash", {"command": "vexbuild"}), Decision.DENY)
        self.assertEqual(len(asker.seen), 1)


# -- Windows paths, the argv tables, slot and name ------------------------------------------------

class Tables(Base):
    def test_windows_paths(self):
        w = self.world
        self.assertEqual(vex.to_windows(str(w.user / "Documents" / "Robotics" / "a b")),
                         "C:\\Users\\t\\Documents\\Robotics\\a b")
        self.assertEqual(vex.to_windows(str(w.mnt / "d")), "D:\\")
        for bad, why in ((str(self.root / "home"), "not on a Windows drive"),
                         ("relative/x", "absolute"), (str(w.user) + "/../x", "absolute"),
                         (str(w.user / "a&calc"), "misread"), (str(w.user / "a%PATH%"), "misread"),
                         (str(w.user / 'a"b'), "misread"), (str(w.user / "a^b"), "misread"),
                         (str(w.user / "a\u202eb"), "misread"), (str(w.mnt / "cc" / "x"), "drive")):
            with self.subTest(bad):
                self.assert_refused(lambda: vex.to_windows(bad), why)
        self.assert_refused(lambda: vex.to_windows(str(w.user / "a b"), spaces=False), "space")

    def test_exact_argv_for_build_rebuild_and_clean(self):
        cfg = vex.load_config()
        project = vex.Detected(name="override", language="cpp", slot=1, sdk_version=SDK,
                               printf_float=True)
        win = self.world.windows
        common = [str(self.world.exes["make"]), "P=override",
                  f"T={win(self.world.storage / 'sdk/cpp/V5' / SDK)}",
                  f"CC={win(self.world.exes['clang'])}", f"CXX={win(self.world.exes['clang'])}",
                  f"OBJCOPY={win(self.world.exes['objcopy'])}", f"SIZE={win(self.world.exes['size'])}",
                  f"LINK={win(self.world.exes['ld'])}", f"ARCH={win(self.world.exes['ar'])}",
                  "PRINTF_FLOAT=1"]
        self.assertEqual(vex.build_argv(cfg, project, "build"), common)
        self.assertEqual(vex.build_argv(cfg, project, "rebuild"), common + ["-B"])
        self.assertEqual(vex.build_argv(cfg, project, "clean"), common + ["clean"])
        plain = vex.Detected(name="bot", language="cpp", slot=1, sdk_version=SDK)
        self.assertNotIn("PRINTF_FLOAT=1", vex.build_argv(cfg, plain, "build"))
        for name in ("a b", "a&calc", "-B", "x" * 65, "naïve"):
            bad = vex.Detected(name=name, language="cpp", slot=1, sdk_version=SDK)
            self.assert_refused(lambda: vex.build_argv(cfg, bad, "build"), "letters, digits")
        with self.assertRaises(ValueError):
            vex.build_argv(cfg, project, "download")

    def test_exact_argv_for_download_and_the_brain(self):
        cfg = vex.load_config()
        self.assertEqual(vex.download_argv(cfg, "auton_1", 3, "C:\\x\\build\\b.bin"),
                         [str(self.world.vexcom), "--name", "auton_1", "--slot", "3",
                          "--write", "C:\\x\\build\\b.bin", "--progress"])
        self.assertEqual(vex.brain_argv(cfg), [str(self.world.vexcom), "--user"])
        self.assert_refused(lambda: vex.info_argv(cfg), "spike")

    def test_slot_and_name_refusals(self):
        for slot in (0, 9, -1, "3", 3.0, True, None):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                vex.check_slot(slot)
        for name in ("-x", "a b", "a;b", "naïve", "x" * 21, "", "a\nb", "a/b", "--slot", None, 7):
            with self.subTest(name=name), self.assertRaises(ValueError):
                vex.check_name(name)
        self.assertEqual(vex.check_name("x" * 20), "x" * 20)
        self.assertEqual(vex.check_slot(8), 8)

    def test_errors_are_parsed_from_a_fixture_log(self):
        log = ("\x1b[37mwindows build for platform vexv5\x1b[0m\r\n"
               "CXX src/main.cpp\r\n"
               "src/main.cpp:12:5: error: expected ';' after expression\r\n"
               "C:\\Users\\t\\Documents\\Robotics\\override\\include\\robot.h:3:1: warning: unused variable 'x'\r\n"
               "C:\\other\\lib.h:9:2: fatal error: 'lib.h' file not found\r\n"
               "src/auton.cpp:40:10: note: declared here\r\n"
               "make: *** [vex/mkrules.mk:13: build/src/main.o] Error 1\r\n")
        errors = vex.parse_errors(vex.plain(log.encode()),
                                  "C:\\Users\\t\\Documents\\Robotics\\override")
        self.assertEqual([(e["file"], e["line"], e["col"], e["severity"], e["in_project"])
                          for e in errors],
                         [("src/main.cpp", 12, 5, "error", True),
                          ("include/robot.h", 3, 1, "warning", True),
                          ("C:/other/lib.h", 9, 2, "error", False)])
        self.assertEqual(errors[0]["message"], "expected ';' after expression")


# -- detection ---------------------------------------------------------------------------------------

class Detection(Base):
    def test_a_project_is_detected_and_malformed_ones_reported(self):
        root = self.world.project()
        self.assertEqual(vex.detect(root)["name"], "override")
        self.assertTrue(vex.detect(root)["builds"])
        settings = root / ".vscode" / "vex_project_settings.json"
        good = json.loads(settings.read_text())
        for change, why in (({"extension": {"json": 1}}, "version 2"),
                            ({"project": {**good["project"], "platform": "IQ2"}}, "only V5"),
                            ({"project": {**good["project"], "slot": 9}}, "slot"),
                            ({"project": {**good["project"], "slot": True}}, "slot"),
                            ({"project": {**good["project"], "language": "blocks"}}, "language")):
            with self.subTest(why):
                settings.write_text(json.dumps({**good, **change}))
                found = vex.detect(root)
                self.assertFalse(found["vex"])
                self.assertTrue(found["malformed"])
                self.assertIn(why, found["reason"])
        settings.write_text(" " * (vex.SETTINGS_CAP + 1))
        self.assertIn("larger", vex.detect(root)["reason"])
        settings.unlink()
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps(good))
        settings.symlink_to(real)
        self.assertIn("symlink", vex.detect(root)["reason"])
        (root / "project.pros").write_text("{}")
        self.assertEqual(vex.detect(root)["reason"], "PROS projects aren't supported")


# -- templates and the settings file -----------------------------------------------------------------

def zip_of(entries) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            if isinstance(entry, zipfile.ZipInfo):
                archive.writestr(entry, "target")
            else:
                archive.writestr(*entry)
    return out.getvalue()


class Templates(Base):
    def test_listing_comes_from_the_configured_folder(self):
        listed = vex.list_templates(vex.load_config())
        self.assertEqual({t["id"] for t in listed},
                         {"cpp_v5_vsc_empty", "cpp_v5_vsc_competition_template", "py_v5_vsc_empty"})
        labels = {t["id"]: t["label"] for t in listed}
        self.assertEqual(labels["cpp_v5_vsc_empty"], "Empty Project")      # from index.json
        self.assertEqual(labels["cpp_v5_vsc_competition_template"], "Competition template")

    def test_every_unsafe_archive_is_refused_before_anything_is_written(self):
        link = zipfile.ZipInfo("src/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = {
            "zip-slip": [("../escape.txt", "x")], "deep zip-slip": [("src/../../escape", "x")],
            "absolute": [("/etc/escape", "x")], "drive letter": [("C:/escape", "x")],
            "windows separator": [("src\\..\\escape", "x")], "symlink": [link],
            "git internals": [(".git/hooks/pre-commit", "x")],
            "reserved name": [("src/CON.cpp", "x")],
            "oversize entry": [("big.bin", b"\0" * (vex.TEMPLATE_ENTRY_CAP + 1))],
            "too many entries": [(f"f{i}", "x") for i in range(vex.TEMPLATE_ENTRIES + 1)],
            "too large in all": [(f"m{i}", b"\0" * vex.TEMPLATE_ENTRY_CAP) for i in range(11)],
            "duplicate": [("a.txt", "x"), ("A.TXT", "y")],
        }
        for label, entries in cases.items():
            with self.subTest(label):
                self.assertRaises(vex.VexError, vex.unpack_plan, zip_of(entries))
        self.assertRaises(vex.VexError, vex.unpack_plan, b"not a zip")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_a_good_template_unpacks_and_the_settings_match_the_v2_shape(self):
        cfg = vex.load_config()
        entries = vex.read_template(cfg, "cpp_v5_vsc_empty")
        self.assertNotIn("skip.v5code", [rel for rel, _ in entries])
        target = self.world.robotics / "fresh"
        target.mkdir()
        settings = vex.settings_document("fresh", "cpp", cfg)
        vex.unpack(entries, target, settings)
        self.assertTrue((target / "src" / "main.cpp").is_file())
        self.assertFalse((target / "skip.v5code").exists())
        written = json.loads((target / ".vscode" / "vex_project_settings.json").read_text())
        for key in ("platform", "name", "description", "creationDate", "slot", "language"):
            self.assertIn(key, written["project"])               # the schema's required fields
        self.assertEqual(written["extension"], {"version": "0.8.2026020401", "json": 2})
        self.assertEqual(written["project"]["sdkVersion"], SDK)
        self.assertEqual(vex.detect(target)["language"], "cpp")
        py = vex.settings_document("pybot", "python", cfg)["project"]
        self.assertEqual((py["python"], py["sdkVersion"]), ({"main": "src/main.py"}, PY_SDK))
        # Never over or through what is there: a second unpack refuses.
        self.assertRaises(FileExistsError, vex.unpack, entries, target, settings)

    def test_an_unknown_template_id_is_refused(self):
        cfg = vex.load_config()
        for bad in ("../cpp_v5_vsc_empty", "Clawbot_Competition", "cpp_v5_vsc_nothere", ""):
            self.assertRaises(ValueError, vex.read_template, cfg, bad)


# -- the routes: who can reach them -------------------------------------------------------------------

class Routes(DaemonBase):
    def every(self):
        p = self.project.id
        return [("GET", "/vex/status", None), ("GET", f"/projects/{p}/vex", None),
                ("POST", f"/projects/{p}/vex/build", {"action": "build"}),
                ("GET", "/vex/builds/deadbeef", None),
                ("POST", f"/projects/{p}/vex/download", {"slot": 1, "name": "x"}),
                ("POST", "/vex/device/info", {}), ("POST", "/vex/brain", {}),
                ("GET", "/vex/templates", None),
                ("POST", "/vex/projects", {"name": "x", "language": "cpp",
                                           "template": "cpp_v5_vsc_empty"}),
                ("GET", "/vex/projects/deadbeef", None)]

    def test_every_route_refuses_every_client_but_the_hud(self):
        port, preview = self.daemon.face_port, self.daemon.workshop_port
        for method, path, body in self.every():
            with self.subTest(path=path, method=method):
                self.request(method, path, body, port=self.daemon.port, status=403)   # the API listener
                self.request(method, path, body, origin="http://evil.example", status=403)
                self.request(method, path, body, origin=f"http://127.0.0.1:{preview}", status=403)
                self.request(method, path, body, origin="null", status=403)
                self.request(method, path, body, host=f"rebound.example:{port}", status=403)
                self.assertEqual(self.request(method, path, body, port=preview)[0], 404)
                if method != "GET":
                    self.request(method, path, body, origin=None, status=403)          # no Origin
                else:
                    self.request(method, path, body, origin=None, status=403)
                    for site in ("cross-site", "same-site", "none"):
                        self.request(method, path, body, origin=None,
                                     headers={"Sec-Fetch-Site": site}, status=403)
        self.assertEqual(self.world.calls("make") + self.world.calls("vexcom"), [])

    def test_a_same_origin_get_without_an_origin_is_the_huds(self):
        # Chromium sends no Origin on a same-origin GET (measured); its own
        # Sec-Fetch-Site, which a page cannot set, says it was the HUD.
        status, view = self.request("GET", "/vex/status", origin=None,
                                    headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertTrue(view["configured"])
        self.assertEqual(view["vexcom_version"], "1.0.2.2")

    def test_no_body_can_name_a_path_an_argument_or_a_program(self):
        p = self.project.id
        for path, body in ((f"/projects/{p}/vex/build", {"action": "build", "path": "/x"}),
                           (f"/projects/{p}/vex/build", {"action": "build", "args": ["-f", "x"]}),
                           (f"/projects/{p}/vex/build", {"action": "make clean"}),
                           (f"/projects/{p}/vex/download", {"slot": 1, "name": "x", "path": "/x"}),
                           (f"/projects/{p}/vex/download", {"slot": 1, "name": "x", "args": ["-u"]}),
                           ("/vex/brain", {"program": "/bin/sh"}), ("/vex/device/info", {"args": []}),
                           ("/vex/projects", {"name": "x", "language": "cpp",
                                              "template": "cpp_v5_vsc_empty", "path": "/x"})):
            with self.subTest(path=path, body=body):
                self.request("POST", path, body, status=400)
        self.assertEqual(self.world.calls("make") + self.world.calls("vexcom"), [])

    def test_slot_and_name_refusals_at_the_route(self):
        p = self.project.id
        for body in ({"slot": 0, "name": "a"}, {"slot": 9, "name": "a"}, {"slot": "3", "name": "a"},
                     {"slot": 1, "name": "-x"}, {"slot": 1, "name": "a b"}, {"slot": 1, "name": "a;b"},
                     {"slot": 1, "name": "naïve"}, {"slot": 1, "name": "x" * 21}):
            with self.subTest(body=body):
                self.request("POST", f"/projects/{p}/vex/download", body, status=400)
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_the_project_comes_from_its_id(self):
        self.request("GET", "/projects/zzzz/vex", status=400)
        self.request("POST", "/projects/deadbeef/vex/build", {"action": "build"}, status=404)
        self.assertEqual(self.owner("GET", f"/projects/{self.project.id}/vex")["name"], "override")


# -- build -----------------------------------------------------------------------------------------------

class Build(DaemonBase):
    def test_build_runs_the_exact_argv_in_the_project_and_reports(self):
        result = self.build()
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["bin"]["path"], "build/override.bin")
        self.assertEqual(result["errors"], [])
        call = self.world.calls("make")[-1]
        cfg = vex.load_config()
        detected = vex.parse_settings(vex.read_settings(self.folder))
        self.assertEqual(call["argv"], vex.build_argv(cfg, detected, "build")[1:])
        self.assertEqual(Path(call["cwd"]).resolve(), self.folder.resolve())
        self.assertEqual(self.build("rebuild")["state"], "succeeded")
        self.assertEqual(self.world.calls("make")[-1]["argv"][-1], "-B")
        cleaned = self.build("clean")
        self.assertEqual((cleaned["state"], cleaned["bin"]), ("succeeded", None))
        self.assertEqual(self.world.calls("make")[-1]["argv"][-1], "clean")
        listed = {t["id"]: t for t in self.owner("GET", "/terminals")}
        self.assertEqual(listed[cleaned["terminal_id"]]["program"], "vex-build")
        self.assertEqual(len([t for t in listed.values() if t["program"] == "vex-build"]), 1,
                         "a new build replaces the last one's ended terminal")

    def test_errors_come_back_as_a_list(self):
        (self.folder / "src" / "FAIL").write_text(
            "src/main.cpp:12:5: error: expected ';'\nsrc/main.cpp:3:1: warning: unused\n")
        result = self.build()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual([(e["file"], e["line"], e["severity"]) for e in result["errors"]],
                         [("src/main.cpp", 12, "error"), ("src/main.cpp", 3, "warning")])

    def test_one_build_per_project_across_the_hud_and_vexbuild(self):
        (self.folder / "src" / "SLOW").write_text("")
        first = self.build(wait=False)
        eventually(lambda: self.world.calls("make"), what="make started")
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                    {"action": "build"})
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])
        out = io.StringIO()
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=out), 75)
        self.assertIn("already running", out.getvalue())
        self.assertEqual(len(self.world.calls("make")), 1)
        (self.folder / "src" / "SLOW").unlink()
        self.owner("DELETE", f"/terminals/{first['terminal_id']}")
        # The lock went with the program: both doors work again.
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 0)
        self.assertEqual(self.build()["state"], "succeeded")

    def test_the_timeout_kills_the_build(self):
        (self.folder / "src" / "SLOW").write_text("")
        with patch.object(vex, "BUILD_TIMEOUT_S", 1.0), patch.object(vex, "KILL_GRACE_S", 0.5):
            started = time.monotonic()
            result = self.build()
        self.assertEqual(result["state"], "timed_out", result)
        self.assertLess(time.monotonic() - started, 8)
        terminal = self.daemon.terminals.get(result["terminal_id"])
        self.assertFalse(terminal.alive(), "the fake make outlived the timeout")
        # The output stays readable: the terminal is still listed.
        self.assertIn(result["terminal_id"], {t["id"] for t in self.owner("GET", "/terminals")})

    def test_a_makefile_that_names_vexcom_is_never_built(self):
        (self.folder / "vex" / "mkrules.mk").write_text(
            "all:\n\t$(VEX_TOOLS)\\vexcom.exe --slot 1 --write build/x.bin\n")
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                    {"action": "build"})
        self.assertEqual(status, 409)
        self.assertIn("names vexcom", body["error"])
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 2)
        # …and one an `include` pulls in.
        (self.folder / "vex" / "mkrules.mk").unlink()
        (self.folder / "extra").mkdir()
        (self.folder / "extra" / "dl.inc").write_text("x:\n\tVEXCOM --user\n")
        (self.folder / "makefile").write_text("include vex/mkenv.mk\ninclude extra/dl.inc\n")
        self.request("POST", f"/projects/{self.project.id}/vex/build", {"action": "build"},
                     status=409)
        self.assertEqual(self.world.calls("make"), [])

    def test_a_bad_config_refuses_before_anything_runs(self):
        self.world.write_config(make=str(self.root / "elsewhere.exe"))
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                    {"action": "build"})
        self.assertEqual(status, 409)
        self.assertIn("jarvis vex setup", body["error"])
        self.assertEqual(self.world.calls("make"), [])

    def test_a_project_off_the_windows_drive_or_in_python_does_not_build(self):
        linux = self.world.project(folder=self.root / "linux" / "bot")
        other = self.stores.projects.create("linuxbot", str(linux))
        status, body = self.request("POST", f"/projects/{other.id}/vex/build", {"action": "build"})
        self.assertEqual(status, 409)
        self.assertIn("Windows drive", body["error"])
        py = self.world.project("pybot", language="python")
        pyp = self.stores.projects.create("pybot", str(py))
        self.request("POST", f"/projects/{pyp.id}/vex/build", {"action": "build"}, status=409)
        status, body = self.request("POST", f"/projects/{pyp.id}/vex/download",
                                    {"slot": 1, "name": "py"})
        self.assertEqual(status, 409)
        self.assertIn("spike", body["error"])
        self.assertEqual(self.world.calls("make") + self.world.calls("vexcom"), [])

    def test_the_program_environment_carries_nothing_of_the_daemons(self):
        self.build()
        call = self.world.calls("make")[-1]
        self.assertEqual(call["wslenv"], "")
        leaked = [n for n in call["env"] if n.startswith(("JARVIS_", "OPENROUTER", "ANTHROPIC",
                                                          "CLAUDE_", "CODEX_", "VIRTUAL_ENV"))]
        self.assertEqual(leaked, [])


# -- the CLI: vexbuild ---------------------------------------------------------------------------------

class VexBuildCli(Base):
    def test_it_builds_with_the_same_table_and_nothing_else(self):
        folder = self.world.project()
        out = io.StringIO()
        self.assertEqual(vex.cli(["build"], cwd=str(folder), out=out), 0)
        self.assertIn("built build/override.bin", out.getvalue())
        cfg = vex.load_config()
        detected = vex.parse_settings(vex.read_settings(folder))
        self.assertEqual(self.world.calls("make")[-1]["argv"],
                         vex.build_argv(cfg, detected, "build")[1:])
        self.assertEqual(vex.cli(["build", "--rebuild"], cwd=str(folder), out=io.StringIO()), 0)
        self.assertEqual(self.world.calls("make")[-1]["argv"][-1], "-B")
        self.assertEqual(vex.cli(["build", "--clean"], cwd=str(folder), out=io.StringIO()), 0)
        self.assertEqual(self.world.calls("make")[-1]["argv"][-1], "clean")
        for argv in (["download"], ["build", "--slot", "1"], ["build", "--write", "x"], ["brain"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    patch("sys.stderr", io.StringIO()):
                vex.cli(argv, cwd=str(folder), out=io.StringIO())
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_it_refuses_a_folder_that_is_not_a_vex_project(self):
        out = io.StringIO()
        self.assertEqual(vex.cli(["build"], cwd=str(self.world.robotics), out=out), 2)
        self.assertIn("VEX V5 project", out.getvalue())
        self.assertEqual(self.world.calls("make"), [])


# -- device actions ---------------------------------------------------------------------------------------

class Device(DaemonBase):
    def download(self, status=202, **body):
        body = {"slot": 3, "name": "auton_1", **body}
        return self.request("POST", f"/projects/{self.project.id}/vex/download", body, status=status)[1]

    def test_download_runs_dishpys_flags_on_the_built_binary(self):
        self.build()
        tid = self.download()["terminal_id"]
        self.ended(tid)
        call = self.world.calls("vexcom")[-1]
        bin_win = self.world.windows(self.folder / "build" / "override.bin")
        self.assertEqual(call["argv"], ["--name", "auton_1", "--slot", "3", "--write", bin_win,
                                        "--progress"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.folder.resolve())
        self.assertEqual(call["wslenv"], "")

    def test_no_binary_or_a_stale_one_is_refused(self):
        self.assertIn("Build first", self.download(status=409)["error"])
        self.build()
        old = time.time() - 600
        os.utime(self.folder / "build" / "override.bin", (old, old))
        self.assertIn("older than the sources", self.download(status=409)["error"])
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_one_device_action_at_a_time(self):
        self.build()
        hold = self.world.vexcom_dir / "HOLD"
        hold.write_text("")
        first = self.download()["terminal_id"]
        eventually(lambda: self.world.calls("vexcom"), what="download started")
        self.assertIn("download is running", self.download(status=409)["error"])
        status, body = self.request("POST", "/vex/brain", {})
        self.assertEqual(status, 409)
        hold.unlink()
        self.ended(first)
        eventually(lambda: self.request("POST", "/vex/brain", {})[0] == 200, what="brain")
        brain = self.owner("POST", "/vex/brain")
        self.assertFalse(brain["started"], "one brain terminal: the open one comes back")
        self.assertIn("brain terminal", self.download(status=409)["error"])
        self.assertEqual(self.owner("GET", "/vex/status")["device"], "brain")
        self.owner("DELETE", f"/terminals/{brain['terminal_id']}")
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="released")
        self.ended(self.download()["terminal_id"])

    def test_a_download_and_a_build_of_one_project_never_overlap(self):
        self.build()
        hold = self.world.vexcom_dir / "HOLD"
        hold.write_text("")
        sending = self.download()["terminal_id"]
        eventually(lambda: self.world.calls("vexcom"), what="download started")
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                    {"action": "build"})
        self.assertEqual(status, 409, "a build would rewrite the binary being sent")
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 75)
        hold.unlink()
        self.ended(sending)
        (self.folder / "src" / "SLOW").write_text("")
        later = time.time() + 60          # the binary still newer than every source
        os.utime(self.folder / "build" / "override.bin", (later, later))
        building = self.build(wait=False)["terminal_id"]
        eventually(lambda: len(self.world.calls("make")) == 2, what="second build started")
        self.assertIn("build of this project is running", self.download(status=409)["error"])
        self.assertIsNone(self.owner("GET", "/vex/status")["device"], "the refusal released it")
        self.owner("DELETE", f"/terminals/{building}")

    def test_device_info_waits_on_the_spike_then_reads_as_text_with_a_timeout(self):
        status, body = self.request("POST", "/vex/device/info", {})
        self.assertEqual(status, 409)
        self.assertIn("vexcom --help", body["error"])
        with patch.object(vex, "INFO_FLAG", "--test-info"):
            text = self.owner("POST", "/vex/device/info")["text"]
            self.assertIn("<b onmouseover=x>BLUE-1</b>", text)      # markup is text; the HUD draws text
            self.assertNotIn("\x1b", text)
            (self.world.vexcom_dir / "HOLD_INFO").write_text("")
            with patch.object(vex, "INFO_TIMEOUT_S", 1.0), patch.object(vex, "KILL_GRACE_S", 0.3):
                status, body = self.request("POST", "/vex/device/info", {})
            self.assertEqual(status, 409)
            self.assertIn("did not answer", body["error"])
            (self.world.vexcom_dir / "HOLD_INFO").unlink()
            self.assertIsNone(self.owner("GET", "/vex/status")["device"])
            self.assertIn("BLUE-1", self.owner("POST", "/vex/device/info")["text"])
        self.assertEqual([t for t in self.owner("GET", "/terminals") if t["program"] == "vex-info"],
                         [], "a device-info read leaves no terminal behind")


class Sockets(DaemonBase):
    def attach(self, tid):
        ticket = self.owner("POST", f"/terminals/{tid}/ticket")["ticket"]
        url = f"ws://127.0.0.1:{self.daemon.face_port}/terminals/{tid}/attach?ticket={ticket}"
        conn = websocket.create_connection(
            url, timeout=5, origin=f"http://127.0.0.1:{self.daemon.face_port}")
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def until(conn, predicate, timeout=6.0):
        seen_text, seen_bytes = [], b""
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            conn.settimeout(max(0.05, until - time.monotonic()))
            try:
                opcode, data = conn.recv_data()
            except websocket.WebSocketTimeoutException:
                break
            if opcode == websocket.ABNF.OPCODE_TEXT:
                seen_text.append(json.loads(data))
            elif opcode == websocket.ABNF.OPCODE_BINARY:
                seen_bytes += data
            if predicate(seen_text, seen_bytes):
                return seen_text, seen_bytes
        raise AssertionError(f"not seen: {seen_text[-3:]} {seen_bytes[-200:]!r}")

    def test_the_brain_takes_typing_and_a_build_refuses_it(self):
        brain = self.owner("POST", "/vex/brain")["terminal_id"]
        conn = self.attach(brain)
        self.until(conn, lambda t, b: b"user port open" in b)
        conn.send_binary(b"heading?\n")
        self.until(conn, lambda t, b: b"brain> heading?" in b)
        (self.folder / "src" / "SLOW").write_text("")
        build = self.build(wait=False)["terminal_id"]
        conn = self.attach(build)
        self.until(conn, lambda t, b: any(m.get("type") == "replayed" for m in t))
        conn.send_binary(b"y\n")
        texts, _ = self.until(conn, lambda t, b: any(m.get("type") == "input_refused" for m in t))
        self.assertEqual([m for m in texts if m.get("type") == "input_refused"][0]["bytes"], 2)


# -- a new project goes through the project_folder card ------------------------------------------------

class NewProject(DaemonBase):
    def create(self, name):
        return self.request("POST", "/vex/projects", {"name": name, "language": "cpp",
                                                      "template": "cpp_v5_vsc_empty"}, status=202)[1]

    def card(self):
        return eventually(lambda: self.daemon.approvals.pending(), what="the card")[0]

    def state(self, rid):
        return eventually(lambda: (lambda s: s if s["state"] != "asking" else None)(
            self.owner("GET", f"/vex/projects/{rid}")), what="the request")

    def test_deny_makes_nothing(self):
        made = self.create("newbot")
        target = self.world.robotics / "newbot"
        self.assertEqual(made["path"], str(target))
        card = self.card()
        self.assertEqual(card.tool, "project_folder")
        self.assertEqual((card.args["path"], card.args["action"]), (str(target), "create"))
        self.assertFalse(card.allowlistable)
        self.daemon.approvals.resolve(card.req_id, Decision.DENY)
        self.assertEqual(self.state(made["id"])["state"], "declined")
        self.assertFalse(target.exists())
        self.assertNotIn("newbot", [p.name for p in self.stores.projects.list()])

    def test_approve_makes_the_folder_the_files_and_the_project(self):
        made = self.create("bot2")
        card = self.card()
        self.daemon.approvals.resolve(card.req_id, Decision.ALLOW)
        done = self.state(made["id"])
        self.assertEqual(done["state"], "created", done)
        target = self.world.robotics / "bot2"
        self.assertTrue((target / "src" / "main.cpp").is_file())
        self.assertFalse((target / "skip.v5code").exists())
        self.assertEqual(vex.detect(target)["name"], "bot2")
        project = self.stores.projects.get(done["project_id"])
        self.assertEqual(project.root, str(target))

    def test_an_empty_folder_is_used_without_asking_and_a_full_one_is_refused(self):
        (self.world.robotics / "emptybot").mkdir()
        made = self.create("emptybot")
        self.assertEqual(self.state(made["id"])["state"], "created")
        full = self.world.robotics / "fullbot"
        full.mkdir()
        (full / "x").write_text("")
        status, body = self.request("POST", "/vex/projects", {"name": "fullbot", "language": "cpp",
                                                              "template": "cpp_v5_vsc_empty"})
        self.assertEqual(status, 409)
        self.assertIn("not empty", body["error"])

    def test_bad_requests_are_refused_before_any_card(self):
        for body in ({"name": "a b", "language": "cpp", "template": "cpp_v5_vsc_empty"},
                     {"name": "ok", "language": "blocks", "template": "cpp_v5_vsc_empty"},
                     {"name": "ok", "language": "python", "template": "cpp_v5_vsc_empty"},
                     {"name": "ok", "language": "cpp", "template": "../x"}):
            self.request("POST", "/vex/projects", body, status=400)
        status, body = self.request("POST", "/vex/projects", {
            "name": "ok", "language": "cpp", "template": "cpp_v5_vsc_empty",
            "location": str(self.root / "linux")})
        self.assertEqual(status, 409)
        self.assertEqual(self.daemon.approvals.pending(), [])


# -- leaks and reach --------------------------------------------------------------------------------------

class capture_logs:
    def __enter__(self):
        self.records: list[str] = []
        outer = self

        class Keep(logging.Handler):
            def emit(self, record):
                if not record.name.startswith("websocket"):
                    outer.records.append(record.getMessage())

        self.handler = Keep(level=logging.DEBUG)
        self.root = logging.getLogger()
        self.level = self.root.level
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.DEBUG)
        return self.records

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.level)


class Leaks(DaemonBase):
    def test_output_reaches_no_bus_no_log_and_no_file(self):
        marker = "zq" + secrets.token_hex(6)
        printed = "OUTPUT-" + marker.upper()
        (self.folder / "src" / "MARKER").write_text(marker)
        events = self.daemon.bus.subscribe()
        with capture_logs() as records:
            result = self.build()
            tid = self.request("POST", f"/projects/{self.project.id}/vex/download",
                               {"slot": 2, "name": "m"}, status=202)[1]["terminal_id"]
            self.ended(tid)
            brain = self.owner("POST", "/vex/brain")["terminal_id"]
            self.owner("DELETE", f"/terminals/{brain}")
        terminal = self.daemon.terminals.get(result["terminal_id"])
        self.assertIn(printed.encode(), terminal.history().data)        # the capture worked
        drained = []
        while True:
            try:
                drained.append(events.get_nowait())
            except Exception:
                break
        self.assertEqual(drained, [], "a VEX action put a record on the bus")
        text = "\n".join(records)
        self.assertIn("vex build started for project override", text)
        self.assertIn("vex download to slot 2 started for project override", text)
        self.assertNotIn(printed, text)
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(printed.encode(), path.read_bytes(), path)


class Reach(unittest.TestCase):
    """No tool, MCP tool, fast-path tool or Discord verb reaches download, the
    brain terminal, device control, the routes or the runners."""

    NEEDLES = re.compile(r"""['"]/vex\b|['"]vex['"]|jarvis\.v2\.vex\b|from\s+\.\s+import\s+vex\b|"""
                         r"""from\s+\.vex\s+import|\.vex\.|\bvexcom\b|create_program|\bVex\(""", re.I)
    ALLOWED = {"jarvis/v2/vex.py", "jarvis/v2/vex_setup.py", "jarvis/v2/hud_api.py",
               "jarvis/v2/daemon.py", "jarvis/v2/terminals.py", "jarvis/rules.py",
               "jarvis/config.py", "jarvis/__main__.py"}

    def test_no_tool_name_or_discord_verb_is_for_vex(self):
        from jarvis.v2 import commands, mcp
        from jarvis.v2.providers import fastpath
        reachable = set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS) | set(tools.REGISTRY)
        self.assertTrue({"task_propose", "schedule_delete"} <= reachable)
        self.assertEqual(sorted(n for n in reachable if re.search("vex|brain", n, re.I)), [])
        verbs = json.dumps([getattr(c, "__dict__", str(c)) for c in commands.REGISTRY], default=str)
        self.assertNotRegex(verbs.lower(), r"vex|brain|download")

    def test_nothing_but_the_daemon_and_its_routes_reaches_vex(self):
        offenders = []
        for path in sorted((REPO / "jarvis").rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel not in self.ALLOWED and self.NEEDLES.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
        self.assertEqual(offenders, [])
        from jarvis.v2 import mcp
        from jarvis.v2.providers import fastpath
        modules = {mcp, fastpath}
        for name in set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS) | set(tools.REGISTRY):
            func = getattr(tools.REGISTRY.get(name), "func", None)
            if func is not None:
                modules.add(inspect.getmodule(func))
        for module in modules:
            self.assertIsNone(self.NEEDLES.search(inspect.getsource(module)), module.__name__)

    def test_only_vex_py_creates_a_program_terminal(self):
        callers = sorted(p.relative_to(REPO).as_posix() for p in (REPO / "jarvis").rglob("*.py")
                         if "create_program(" in p.read_text(encoding="utf-8"))
        self.assertEqual(callers, ["jarvis/v2/terminals.py", "jarvis/v2/vex.py"])
        terminals_src = (REPO / "jarvis/v2/terminals.py").read_text()
        self.assertEqual(terminals_src.count("create_program("), 1, "only its definition")
        routes = inspect.getsource(T.route)
        self.assertNotIn("create_program", routes)

    def test_no_route_or_cli_downloads(self):
        main = (REPO / "jarvis" / "__main__.py").read_text()
        self.assertEqual(re.findall(r'vexcmd\.add_argument\("action", choices=(\[[^\]]*\])', main),
                         ['["setup"]'], "jarvis vex has setup and nothing else")
        # Each device argv has one caller: its own owner-only Vex method.
        source = (REPO / "jarvis" / "v2" / "vex.py").read_text()
        for table, owner in (("download_argv(", "def download(self"),
                             ("brain_argv(", "def brain(self"), ("info_argv(", "def info(self")):
            calls = [m.start() for m in re.finditer(re.escape(table), source)
                     if not source[max(0, m.start() - 4):m.start()].endswith("def ")]
            self.assertEqual(len(calls), 1, table)
            method = source.rfind("    def ", 0, calls[0])
            self.assertTrue(source.startswith("    " + owner, method), table)
        self.assertEqual(re.findall(r'sub\.add_parser\("(\w+)"', inspect.getsource(vex.cli)),
                         ["build"], "vexbuild has one subcommand")

    def test_the_needles_would_catch_a_tool_that_reached_in(self):
        for text in ('client.post("/vex/brain")', "daemon.vex.download(p, 1, 'x')",
                     "from jarvis.v2.vex import Vex", "from . import vex",
                     "subprocess.run(['VEXCOM.exe'])", "terminals.create_program(argv)"):
            self.assertIsNotNone(self.NEEDLES.search(text), text)
        self.assertIsNone(self.NEEDLES.search("from .model import TERMINAL_STATES"))


# -- setup ------------------------------------------------------------------------------------------------

class Setup(Base):
    def test_it_refuses_without_a_terminal(self):
        Path(config.VEX_PATH).unlink()
        said = []
        self.assertEqual(vex_setup.run(ask=lambda q: "y", out=said.append, tty=False), 1)
        self.assertFalse(Path(config.VEX_PATH).exists())

    def test_it_writes_what_it_found_and_offers_vexbuild(self):
        Path(config.VEX_PATH).unlink()
        answers = iter(["", "y", "y"])
        said = []
        self.assertEqual(vex_setup.run(ask=lambda q: next(answers), out=said.append, tty=True), 0)
        written = json.loads(Path(config.VEX_PATH).read_text())
        self.assertEqual(written["vexcom"], str(self.world.vexcom))
        self.assertEqual(written["make"], str(self.world.exes["make"]))
        self.assertEqual(written["project_folder"], str(self.world.robotics))
        self.assertEqual(stat.S_IMODE(os.stat(config.VEX_PATH).st_mode), 0o600)
        vex.load_config()                                         # passes the daemon's own rule
        wrapper = Path(config.VEXBUILD_PATH)
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertRegex(wrapper.read_text(), r"-m jarvis\.v2\.vex build \"\$@\"\n$")
        self.assertNotRegex(wrapper.read_text(), r"--write|--user|--slot")
        self.assertEqual(rules.decide("vexbuild").decision, rules.ASK)

    def test_no_writes_nothing(self):
        Path(config.VEX_PATH).unlink()
        answers = iter(["", "n"])
        self.assertEqual(vex_setup.run(ask=lambda q: next(answers), out=lambda s: None, tty=True), 1)
        self.assertFalse(Path(config.VEX_PATH).exists())
        self.assertFalse(Path(config.VEXBUILD_PATH).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
