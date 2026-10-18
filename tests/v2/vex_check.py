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
* **What a line runs is judged strictly** (review round 2): a glob, or a
  word holding an expansion, in program position — past a wrapper, as the
  program a runner starts (its options parsed), inside `sh -c` or `$(…)` —
  that could be vexcom is DENY; a program that cannot be told and could be
  vexcom is asked, never allowlist-covered, and always asked in v2.
* **A here-document's body is data** (review round 3): a `* item` in a commit
  message, a C doc comment, a crontab line is never read as a command; a
  body fed to a shell still is, and the lexer never sees an operator where
  bash would not (quotes, comments, `${…}`, arithmetic, backticks). Its
  delimiter is formed as bash forms it, and where bash's reading is unsure
  (round 3b) the line is too complex to judge, never a hidden marker.
* **A glob past the caps is too complex to judge** — asked, and judged in
  well under a second however it is built; the make file guard refuses one.
* **`vexbuild` asks like any command** (and its module through `python -m`,
  `uv run -m`, `python -m runpy`, and through every runner, options parsed).
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
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import websocket  # noqa: E402  (websocket-client, already a dependency)

from jarvis import config, permissions as v1_permissions, rules, runtime, tools  # noqa: E402
from jarvis.tools import files as v1_files, shell  # noqa: E402
from jarvis.v2 import permissions as v2_permissions, terminals as T, vex, vex_setup  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import PermissionProfile as PP, ProviderName as PN, Role  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
from tests import loadclock  # noqa: E402

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
               "project_folder": str(self.robotics), "sha256": self.fingerprints()}
        doc.update(over)
        return {k: v for k, v in doc.items() if v is not None}

    def fingerprints(self) -> dict:
        import hashlib
        tools = {"vexcom": self.vexcom, **self.exes}
        return {key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for key, path in tools.items()}

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
            "OPENROUTER_API_KEY": "placeholder-not-a-key", "JARVIS_PROBE_X": "1",
            "WSLENV": "JARVIS_PROBE_X/u:OPENROUTER_API_KEY/u"})
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
    "glob": "find /mnt/c -name 'vexc*.exe' -exec {} --user \\;",
    "glob ?": "ls ve?com.exe",
    "glob class": "ls /opt/ve[a-z]com",
    "default expansions": "${A:-vex}${B:-com} --user",
    "assign expansions": "echo ${A:=vex}${B:+com}",
    "powershell en dash": "powershell \u2013enc " + base64.b64encode("vexcom --user".encode("utf-16-le")).decode(),
    "powershell colon": "pwsh -EncodedCommand:" + base64.b64encode("vexcom --user".encode("utf-16-le")).decode(),
    "powershell slash": "powershell /ec " + base64.b64encode("vexcom".encode("utf-16-le")).decode(),
    "copy": f"cp {VEXCOM_EXE} /tmp/renamed.exe",
    "xargs": "xargs vexcom < args.txt",
    "fullwidth": "ＶＥＸＣＯＭ --user",
    # A bracket is read both ways (bash's POSIX classes, fnmatch's first `]`).
    "posix class argument": "ls ve[[:alpha:]]com",
    "fnmatch bracket argument": "ls ve[x[:]co[m:]",
}
ORDINARY = {   # stays as it was: never DENY for not naming vexcom
    "make -j4": rules.ALLOW, "ls *.exe": rules.ALLOW, "ls *": rules.ALLOW,
    "cat .vscode/vex_project_settings.json": rules.ALLOW, "git commit -m 'fix the build'": rules.ALLOW,
    "grep -rn vex_project src": rules.ALLOW, "echo ${HOME}/vex/com": rules.ALLOW,
    "printf '%s %d' a 1": rules.ALLOW, "python3 -m pytest": rules.ALLOW,
    # The review's false positives: globs and names a VEX project uses daily.
    "git tag -l 'v*'": rules.ALLOW, "git tag --list 'v*'": rules.ALLOW, "ls include/vex*": rules.ALLOW,
    "find . -name '*vex*'": rules.ALLOW, "rg -g '*vex*' motor": rules.ALLOW,
    "grep -r --include='*vex*' motor .": rules.ALLOW, "ls v*": rules.ALLOW,
    "ls v*m.exe": rules.ALLOW, "cat ~/.local/bin/vexbuild": rules.ALLOW,
    "grep -rn jarvis.v2.vex tests": rules.ALLOW,
    # Naming the wrapper is not running it.
    "cp ~/.local/bin/vexbuild ~/backup/": rules.ALLOW, "make ~/.local/bin/vexbuild": rules.ALLOW,
    "python3 -m pytest tests/v2/vex_check.py": rules.ALLOW, "npm run vexbuild": rules.ALLOW,
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
        # dispatch refuses a call with no toolset bound (PR #42): this test
        # stands where an agent's turn would, holding run_command alone.
        bound = runtime.tools_bound({"run_command"})
        bound.__enter__()
        self.addCleanup(bound.__exit__, None, None, None)

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
                        "uv run python -m jarvis.v2.vex build", "uv run vexbuild",
                        "python -Im jarvis.v2.vex build",
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

    def test_exact_argv_for_download_the_brain_and_the_device_reads(self):
        cfg = vex.load_config()
        self.assertEqual(vex.download_argv(cfg, "auton_1", 3, "C:\\x\\build\\b.bin"),
                         [str(self.world.vexcom), "--name", "auton_1", "--slot", "3",
                          "--write", "C:\\x\\build\\b.bin", "--progress"])
        # The vendor extension's own user-port invocation (plan §5.2, offset
        # 69190): `--quiet --user`, and the controller's port when known.
        self.assertEqual(vex.brain_argv(cfg), [str(self.world.vexcom), "--quiet", "--user"])
        self.assertEqual(vex.brain_argv(cfg, "COM3"), [str(self.world.vexcom), "--quiet", "--user", "COM3"])
        for port in ("COM0", "COM1000", "com3", "COM3 ", "COM3&calc", "\\\\.\\COM3", "/dev/ttyACM0",
                     "", "COM３", "COM3\n", 3, ["COM3"], "LPT1", "--write"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                vex.brain_argv(cfg, port)
        self.assertEqual(vex.info_argv(cfg), [str(self.world.vexcom), "--json"])
        self.assertEqual(vex.info_argv(cfg, "system"), [str(self.world.vexcom), "--json"])
        self.assertEqual(vex.info_argv(cfg, "programs"), [str(self.world.vexcom), "--programs"])
        for read in ("--json", "--erase", "erase", "", None, ["system"], "System"):
            with self.subTest(read=read), self.assertRaises(ValueError):
                vex.info_argv(cfg, read)

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
        # Never over or through what is there: a second unpack refuses, and its
        # take-back (review LOW-2) removes nothing the first one wrote.
        before = sorted(str(p.relative_to(target)) for p in target.rglob("*"))
        with self.assertRaises(vex.UnpackFailed) as caught:
            vex.unpack(entries, target, settings)
        self.assertIn("makefile already exists", caught.exception.cause)   # named, not "File exists"
        self.assertEqual(caught.exception.left, [])
        self.assertEqual(sorted(str(p.relative_to(target)) for p in target.rglob("*")), before)

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
                ("POST", "/vex/projects/check", {"name": "x", "language": "cpp",
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
    def test_live_output_and_diagnostics_survive_the_terminal_being_replaced(self):
        (self.folder / "src" / "MARKER").write_text("live-build")
        (self.folder / "HOLD").write_text("")
        first = self.build(wait=False)
        view = eventually(lambda: (r if "OUTPUT-LIVE-BUILD" in r["output"] else None)
                          if (r := self.owner("GET", f"/vex/builds/{first['build_id']}")) else None,
                          what="live build output")
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["errors"], [])
        terminal = self.daemon.terminals.get(first["terminal_id"])
        terminal._output(b"\x1b[31msrc/main.cpp:12:5: error: expected ';'\x1b[0m\r\n")
        live = self.owner("GET", f"/vex/builds/{first['build_id']}")
        self.assertIn("expected ';'", live["output"])
        self.assertNotIn("\x1b", live["output"])
        self.assertEqual(live["errors"][0]["line"], 12)
        (self.folder / "HOLD").unlink()
        eventually(lambda: self.owner("GET", f"/vex/builds/{first['build_id']}")["state"] == "succeeded")
        self.build("clean")
        kept = self.owner("GET", f"/vex/builds/{first['build_id']}")
        self.assertIn("OUTPUT-LIVE-BUILD", kept["output"])
        self.assertIn("expected ';'", kept["output"])
        self.assertLessEqual(len(kept["output"]), vex.BUILD_OUTPUT_CAP)

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

    # -- VX-2 review M2 and its delta review (MED-1, LOW-1..3): what may be sent ------------

    def _built_then_older(self):
        """A built binary, made older than its sources (as if built a while ago)."""
        self.build()
        binary = self.folder / "build" / "override.bin"
        old = time.time() - 600
        os.utime(binary, (old, old))
        return binary

    def _header(self, name="pid.hpp", text="// edited\n"):
        (self.folder / "include").mkdir(exist_ok=True)
        (self.folder / "include" / name).write_text(text)

    def test_the_build_first_refusal_carries_a_code(self):
        body = self.download(status=409)
        self.assertEqual(body.get("code"), "build_first", body)
        self._built_then_older()
        body = self.download(status=409)
        self.assertEqual((body.get("code"), "older than the sources" in body["error"]),
                         ("build_first", True), body)
        # Other refusals carry no code: the HUD never builds on them.
        hold = self.world.vexcom_dir / "HOLD"
        self.build()
        hold.write_text("")
        first = self.download()["terminal_id"]
        eventually(lambda: self.world.calls("vexcom"), what="download started")
        busy = self.download(status=409)
        self.assertNotIn("code", busy, busy)
        hold.unlink()
        self.ended(first)

    def test_files_a_build_never_reads_make_nothing_stale(self):
        """M2: a notes file or an editor backup beside the sources never asks for a build."""
        self._built_then_older()
        self.build()                      # a fresh binary
        for name in ("NOTES.txt", "main.cpp~", ".main.cpp.swp", "todo.md"):
            (self.folder / "src" / name).write_text("x\n")
            later = time.time() + 30
            os.utime(self.folder / "src" / name, (later, later))
        self.ended(self.download()["terminal_id"])

    def test_a_make_with_nothing_to_do_proves_nothing_a_rebuild_does(self):
        """MED-1, the reviewer's repro: VEX's makefile lists only include/*.h, so an edited
        include/pid.hpp leaves make nothing to do — and the old binary must not be sent."""
        binary = self._built_then_older()
        self._header()
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        (self.folder / "NOOP").write_text("")
        result = self.build()
        self.assertEqual(result["state"], "succeeded", result)
        self.assertLess(binary.stat().st_mtime, time.time() - 500, "make touched nothing")
        self.assertEqual(self.download(status=409).get("code"), "build_first",
                         "a make that wrote nothing vouched for the old binary")
        # The chain's next step: one rebuild (make -B), which writes it.
        self.assertEqual(self.build("rebuild")["state"], "succeeded")
        self.assertGreater(binary.stat().st_mtime, time.time() - 60)
        self.ended(self.download()["terminal_id"])
        self.assertEqual(len(self.world.calls("vexcom")), 1)

    def test_a_noop_make_after_a_written_build_still_needs_the_edit(self):
        """MED-1 through the known-binary branch: the last build wrote it, then a header is
        edited; a no-op make keeps what is known, and the edit still asks for a build."""
        self.build()
        self._header()
        later = time.time() + 5
        os.utime(self.folder / "include" / "pid.hpp", (later, later))
        (self.folder / "NOOP").write_text("")
        self.assertEqual(self.build()["state"], "succeeded")
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_a_future_dated_source_hides_no_edit(self):
        """LOW-1, the reviewer's repro: a teammate's file dated a year ahead keeps the binary
        'older than the sources' forever; the digest lets a build vouch for it, and an edit
        after that build is never hidden by the future date."""
        ahead = time.time() + 365 * 86400
        (self.folder / "src" / "teammate.cpp").write_text("int t() { return 1; }\n")
        os.utime(self.folder / "src" / "teammate.cpp", (ahead, ahead))
        self.build()
        first = self.download()["terminal_id"]          # just built: may be sent
        self.ended(first)
        (self.folder / "src" / "main.cpp").write_text("int main() { return 2; }\n")
        body = self.download(status=409)
        self.assertEqual(body.get("code"), "build_first", body)
        # A deletion, a rename and an older-mtime copy change the digest too.
        self.build()
        self.ended(self.download()["terminal_id"])
        older = time.time() - 3600
        (self.folder / "src" / "copied.cpp").write_text("int c() { return 3; }\n")
        os.utime(self.folder / "src" / "copied.cpp", (older, older))
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.build()
        self.ended(self.download()["terminal_id"])
        (self.folder / "src" / "copied.cpp").rename(self.folder / "src" / "moved.cpp")
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.build()
        (self.folder / "src" / "moved.cpp").unlink()
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(len(self.world.calls("vexcom")), 3)

    def test_a_source_saved_while_it_built_is_never_made_current(self):
        self._built_then_older()
        hold = self.folder / "HOLD"
        hold.write_text("")
        started = self.build(wait=False)
        eventually(lambda: len(self.world.calls("make")) == 2, what="the build started")
        later = time.time() + 5
        (self.folder / "src" / "auton.cpp").write_text("int auton() { return 2; }\n")
        os.utime(self.folder / "src" / "auton.cpp", (later, later))
        hold.unlink()
        eventually(lambda: self.owner("GET", f"/vex/builds/{started['build_id']}")["state"] == "succeeded",
                   what="the build ended")
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_a_rename_during_a_build_records_nothing(self):
        """LOW-2: the binary make wrote is the `P=` name it started with; an old binary of
        the new name is never vouched for."""
        ahead = time.time() + 365 * 86400
        (self.folder / "src" / "teammate.cpp").write_text("int t() { return 1; }\n")
        os.utime(self.folder / "src" / "teammate.cpp", (ahead, ahead))
        (self.folder / "build").mkdir(exist_ok=True)
        stale = self.folder / "build" / "renamed.bin"
        stale.write_bytes(b"\x00OLDBIN" * 8)
        hold = self.folder / "HOLD"
        hold.write_text("")
        started = self.build(wait=False)
        eventually(lambda: self.world.calls("make"), what="the build started")
        self.assertIn("P=override", self.world.calls("make")[-1]["argv"])
        settings = self.folder / ".vscode" / "vex_project_settings.json"
        doc = json.loads(settings.read_text())
        doc["project"]["name"] = "renamed"
        settings.write_text(json.dumps(doc))
        hold.unlink()
        ended = eventually(lambda: (lambda b: b if b["state"] != "running" else None)(
            self.owner("GET", f"/vex/builds/{started['build_id']}")), what="the build ended")
        self.assertEqual(ended["state"], "succeeded")
        self.assertEqual((ended["bin"] or {}).get("path"), "build/override.bin", ended)
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(self.world.calls("vexcom"), [])

    def test_an_unfinished_build_refuses_until_one_succeeds(self):
        """LOW-3: make killed while writing leaves a truncated binary with a fresh mtime —
        a cancelled, timed-out or failed build refuses the download until a success, across
        a restart too (the mark is on disk, written before make runs)."""
        binary = self.folder / "build" / "override.bin"
        self.build()
        fresh = time.time() + 60

        def truncated():
            binary.write_bytes(b"\x00VEX")
            os.utime(binary, (fresh, fresh))

        # Cancelled: its terminal closed mid-build.
        (self.folder / "src" / "SLOW").write_text("")
        running = self.build(wait=False)
        eventually(lambda: len(self.world.calls("make")) == 2, what="the slow build started")
        self.assertTrue(vex.is_unfinished(self.folder), "marked before make ran")
        self.owner("DELETE", f"/terminals/{running['terminal_id']}")
        (self.folder / "src" / "SLOW").unlink()
        truncated()
        body = self.download(status=409)
        self.assertEqual(body.get("code"), "build_first", body)
        self.assertIn("did not finish", body["error"])
        # A failed one too.
        self.build()
        (self.folder / "src" / "FAIL").write_text("src/main.cpp:1:1: error: x\n")
        self.assertEqual(self.build()["state"], "failed")
        (self.folder / "src" / "FAIL").unlink()
        truncated()
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        # A timed-out one too.
        (self.folder / "src" / "SLOW").write_text("")
        with patch.object(vex, "BUILD_TIMEOUT_S", 1.0), patch.object(vex, "KILL_GRACE_S", 0.3):
            self.assertEqual(self.build()["state"], "timed_out")
        (self.folder / "src" / "SLOW").unlink()
        truncated()
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        # A daemon that never saw it end (a restart) still refuses: the mark is a file.
        self.assertTrue(vex.unfinished_path(self.folder).is_file())
        # vexbuild keeps the same mark: a success clears it, a failure sets it.
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 0)
        self.assertFalse(vex.is_unfinished(self.folder))
        (self.folder / "src" / "FAIL").write_text("src/main.cpp:1:1: error: x\n")
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 2)
        (self.folder / "src" / "FAIL").unlink()
        self.assertTrue(vex.is_unfinished(self.folder))
        truncated()
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=io.StringIO()), 0)
        self.assertFalse(vex.is_unfinished(self.folder))
        self.ended(self.download()["terminal_id"])
        self.assertEqual(len(self.world.calls("vexcom")), 1)

    def test_only_the_last_build_counts(self):
        binary = self._built_then_older()
        ahead = time.time() + 365 * 86400
        (self.folder / "src" / "teammate.cpp").write_text("int t() { return 1; }\n")
        os.utime(self.folder / "src" / "teammate.cpp", (ahead, ahead))
        self.build()
        self.ended(self.download()["terminal_id"])      # vouched for by the build that wrote it
        # A clean forgets it, and so does a binary replaced since.
        self.assertEqual(self.build("clean")["state"], "succeeded")
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.build()
        binary.write_bytes(b"\x00OTHER" * 8)
        self.assertEqual(self.download(status=409).get("code"), "build_first")
        self.assertEqual(len(self.world.calls("vexcom")), 1)

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

    # -- device reads: `vexcom --json` and `vexcom --programs` (the owner's `vexcom --help`) ---

    def test_device_info_runs_vexcom_json_exactly_and_answers_pretty_text(self):
        answer = self.owner("POST", "/vex/device/info")
        call = self.world.calls("vexcom")[-1]
        self.assertEqual(call["argv"], ["--json"], "exactly `vexcom --json`, nothing added")
        self.assertEqual(Path(call["cwd"]).resolve(), self.world.vexcom_dir.resolve())
        self.assertEqual(call["wslenv"], "")
        self.assertEqual((answer["read"], answer["exit_code"]), ("system", 0))
        text = answer["text"]
        self.assertIn('\n      "vexos": "1.1.5",\n', text, "pretty-printed, two-space indent")
        self.assertIn('"name": "<b onmouseover=x>BLUE-1</b>"', text)   # markup is text
        self.assertNotIn("\x1b", text, "a JSON string's escape stays spelled out, never raw")
        self.assertIn('"team": "1234A\\u001b[31m red"', text)
        self.assertIsNone(self.owner("GET", "/vex/status")["device"], "released")
        self.assertEqual([t for t in self.owner("GET", "/terminals") if t["program"] == "vex-info"],
                         [], "a device-info read leaves no terminal behind")

    def test_programs_read_runs_vexcom_programs_exactly(self):
        answer = self.owner("POST", "/vex/device/info", {"read": "programs"})
        self.assertEqual(self.world.calls("vexcom")[-1]["argv"], ["--programs"])
        self.assertEqual((answer["read"], answer["exit_code"]), ("programs", 0))
        self.assertIn("<img src=x onerror=y>skills", answer["text"])
        self.assertNotIn("\x1b", answer["text"])
        self.assertIsNone(self.owner("GET", "/vex/status")["device"])

    def test_a_device_read_is_only_one_of_the_table(self):
        for body in ({"read": "erase"}, {"read": "--write"}, {"read": "--json"}, {"read": ["system"]},
                     {"read": None}, {"read": "system", "flag": "--erase"}):
            with self.subTest(body=body):
                self.request("POST", "/vex/device/info", body, status=400)
        Path(config.VEX_PATH).unlink()       # the 400 comes before anything is looked up
        self.request("POST", "/vex/device/info", {"read": "erase"}, status=400)
        self.assertEqual(self.world.calls("vexcom"), [])
        self.assertEqual(vex.DEVICE_READS, {"system": "--json", "programs": "--programs"},
                         "only the reads the owner's `vexcom --help` lists")

    def test_verify_tools_runs_before_the_device_read_is_spawned(self):
        order = []
        real_verify, real_create = vex.verify_tools, self.daemon.terminals.create_program

        def verify(cfg, program, also=()):
            order.append(("verify", program, tuple(also)))
            return real_verify(cfg, program, also)

        def create(argv, **kw):
            order.append(("spawn", kw["program"]))
            return real_create(argv, **kw)

        with patch.object(vex, "verify_tools", verify), \
                patch.object(self.daemon.terminals, "create_program", create):
            self.owner("POST", "/vex/device/info")
            self.owner("POST", "/vex/device/info", {"read": "programs"})
        self.assertEqual(order, [("verify", "vexcom", ()), ("spawn", "vex-info")] * 2)

    def test_a_device_read_holds_the_device_and_waits_its_turn(self):
        # Refused while a download runs …
        self.build()
        hold = self.world.vexcom_dir / "HOLD"
        hold.write_text("")
        sending = self.download()["terminal_id"]
        eventually(lambda: self.world.calls("vexcom"), what="download started")
        for body in ({}, {"read": "programs"}):
            status, answer = self.request("POST", "/vex/device/info", body)
            self.assertEqual(status, 409)
            self.assertIn("download is running", answer["error"])
        hold.unlink()
        self.ended(sending)
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="released")
        # … and while the brain terminal holds the port.
        brain = self.owner("POST", "/vex/brain")["terminal_id"]
        eventually(lambda: len(self.world.calls("vexcom")) == 2, what="brain started")
        status, answer = self.request("POST", "/vex/device/info", {})
        self.assertEqual(status, 409)
        self.assertIn("brain terminal", answer["error"])
        self.owner("DELETE", f"/terminals/{brain}")
        eventually(lambda: self.owner("GET", "/vex/status")["device"] is None, what="released")
        self.assertEqual([c["argv"] for c in self.world.calls("vexcom")][1:], [["--quiet", "--user"]],
                         "neither refused read ran")
        # While a read runs, it holds the device: no download, brain or second read.
        (self.world.vexcom_dir / "HOLD_INFO").write_text("")
        result = {}
        reader = threading.Thread(target=lambda: result.update(
            answer=self.request("POST", "/vex/device/info", {})))
        reader.start()
        try:
            eventually(lambda: self.owner("GET", "/vex/status")["device"] == "info", what="reading")
            self.assertIn("device-info read is running", self.download(status=409)["error"])
            status, answer = self.request("POST", "/vex/brain", {})
            self.assertEqual(status, 409)
            self.assertIn("device-info read is running", answer["error"])
            status, answer = self.request("POST", "/vex/device/info", {"read": "programs"})
            self.assertEqual(status, 409)
        finally:
            (self.world.vexcom_dir / "HOLD_INFO").unlink()
            reader.join(15)
        self.assertEqual(result["answer"][0], 200)
        self.assertIsNone(self.owner("GET", "/vex/status")["device"], "released at its end")
        self.ended(self.download()["terminal_id"])

    def test_device_info_times_out_and_releases(self):
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


# -- review round 1: fingerprints, fail-closed guards, holders, coverage -----------------------------

class Fingerprints(DaemonBase):
    """M3: the tools `vex.json` names live under AppData, which agents can write.
    Setup records each one's SHA-256; it is checked immediately before each spawn."""

    @staticmethod
    def tamper(path: Path) -> None:
        with open(path, "a") as handle:
            handle.write("\n# rewritten after setup\n")

    def test_a_rewritten_make_or_compiler_refuses_the_build(self):
        for key in ("make", "clang"):
            with self.subTest(key=key):
                original = self.world.exes[key].read_bytes()
                self.tamper(self.world.exes[key])
                status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                            {"action": "build"})
                self.assertEqual(status, 409)
                self.assertIn("changed since `jarvis vex setup`", body["error"])
                self.assertIn(key, body["error"])
                self.world.exes[key].write_bytes(original)
        self.assertEqual(self.world.calls("make"), [])
        self.assertEqual(self.build()["state"], "succeeded")     # the lock was not left held

    def test_a_rewritten_vexcom_refuses_every_device_action(self):
        self.build()
        self.tamper(self.world.vexcom)
        for method, path, body in (("POST", f"/projects/{self.project.id}/vex/download",
                                    {"slot": 1, "name": "a"}), ("POST", "/vex/brain", {}),
                                   ("POST", "/vex/device/info", {}),
                                   ("POST", "/vex/device/info", {"read": "programs"})):
            with self.subTest(path=path, body=body):
                status, answer = self.request(method, path, body)
                self.assertEqual(status, 409)
                self.assertIn("changed since", answer["error"])
        self.assertEqual(self.world.calls("vexcom"), [])
        self.assertIsNone(self.owner("GET", "/vex/status")["device"], "every refusal released it")

    def test_vexbuild_refuses_a_rewritten_tool(self):
        self.tamper(self.world.exes["ar"])
        out = io.StringIO()
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=out), 2)
        self.assertIn("changed since", out.getvalue())
        self.assertEqual(self.world.calls("make"), [])
        self.assertTrue(vex.BuildLock(self.folder).acquire(), "the lock went with the refusal")

    def test_a_config_without_every_fingerprint_is_refused(self):
        good = self.world.doc()
        for label, hashes in (("missing", None), ("not a map", "abc"),
                              ("one short", {k: v for k, v in list(good["sha256"].items())[1:]}),
                              ("not hex", {**good["sha256"], "make": "x" * 64}),
                              ("upper case", {**good["sha256"], "make": good["sha256"]["make"].upper()})):
            with self.subTest(label):
                doc = {**good, "sha256": hashes} if hashes is not None else \
                    {k: v for k, v in good.items() if k != "sha256"}
                self.assert_refused(lambda: vex.parse_config(doc), "fingerprint")

    def test_the_path_must_still_name_the_file_that_was_hashed(self):
        cfg = vex.load_config()
        real = vex.file_sha256

        def stale(path):
            digest, _identity = real(path)
            return digest, (0, 0, 0, 0, 0)          # the file hashed is not the one there now
        with patch.object(vex, "file_sha256", stale):
            self.assert_refused(lambda: vex.verify_tools(cfg, "make"), "changed since")
        vex.verify_tools(cfg, "make", vex.BUILD_TOOLS)           # the real thing passes
        fifo = self.world.chain / "tools/bin/fifo.exe"
        os.mkfifo(fifo)
        self.assert_refused(lambda: vex.file_sha256(str(fifo)), "not a regular file")


class MakeFilesFailClosed(Base):
    """L1: the makefile guard refuses wherever it cannot read every make file."""

    def setUp(self):
        super().setUp()
        self.folder = self.world.project()

    def refused(self, why):
        reason = vex.makefile_refusal(self.folder)
        self.assertIsNotNone(reason, why)
        out = io.StringIO()
        self.assertEqual(vex.cli(["build"], cwd=str(self.folder), out=out), 2, why)
        self.assertEqual(self.world.calls("make"), [], why)
        return reason

    def test_an_ordinary_project_passes(self):
        (self.folder / "vex" / "mkrules.mk").write_text("include vex/mkenv.mk\nclean:\n\t@echo\n")
        self.assertIsNone(vex.makefile_refusal(self.folder))

    def test_too_many_make_files(self):
        for i in range(vex.MAKEFILE_LIMIT + 1):
            (self.folder / "vex" / f"m{i:02}.mk").write_text("x = 1\n")
        self.assertIn("too many", self.refused("33 make files"))

    def test_a_make_file_past_the_cap(self):
        (self.folder / "vex" / "big.mk").write_text("# pad\n" * (vex.MAKEFILE_CAP // 5) + "x:\n\tvexcom\n")
        self.assertIn("too large", self.refused("past 256 KB"))

    def test_an_include_named_by_a_variable(self):
        (self.folder / "makefile").write_text("include vex/mkenv.mk\ninclude $(E)rules.inc\n")
        self.assertIn("variable", self.refused("$(E)rules.inc"))

    def test_an_include_outside_the_project(self):
        for line in ("include /etc/x.mk", "-include ../outside.mk", "sinclude vex/../../x.mk"):
            with self.subTest(line):
                (self.folder / "makefile").write_text(f"{line}\n")
                self.assertIn("outside the project", self.refused(line))

    def test_a_symlinked_make_file_or_vex_folder(self):
        real = self.root / "elsewhere.mk"
        real.write_text("x:\n\tvexcom --user\n")
        (self.folder / "vex" / "linked.mk").symlink_to(real)
        self.assertIn("symlink", self.refused("a symlinked .mk"))
        (self.folder / "vex" / "linked.mk").unlink()
        (self.folder / "makefile").unlink()
        (self.folder / "makefile").symlink_to(real)
        self.assertIn("symlink", self.refused("a symlinked makefile"))
        (self.folder / "makefile").unlink()
        shutil.rmtree(self.folder / "vex")
        (self.folder / "vex").symlink_to(self.root)
        self.assertIn("symlink", self.refused("a symlinked vex/"))

    def test_a_fifo_make_file_never_hangs(self):
        os.mkfifo(self.folder / "vex" / "pipe.mk")
        self.assertIn("not a regular file", self.refused("a FIFO .mk"))


class WindowsNames(Base):
    """L2: characters and names a Windows command line or file system misreads."""

    def test_every_misreadable_part_is_refused(self):
        user = self.world.user
        for part in ("a\\b", "dot.", "space ", "NUL", "con.txt", "lpt1", "a(b", "a)b", "a;b",
                     "a,b", "a=b", "a:b"):
            with self.subTest(part=part):
                self.assertRaises(vex.VexError, vex.to_windows, str(user / part))
        self.assertEqual(vex.to_windows(str(user / "My Robot")), "C:\\Users\\t\\My Robot")


class SettingsNames(DaemonBase):
    def test_a_settings_name_that_is_not_a_plain_name_never_names_a_file(self):
        self.build()
        settings = self.folder / ".vscode" / "vex_project_settings.json"
        doc = json.loads(settings.read_text())
        # What `build/../evil.bin` would reach, so only the name rule stops it.
        (self.folder / "evil.bin").write_bytes(b"not a program")
        for name in ("../evil", "a/b", "a b", "-x"):
            with self.subTest(name=name):
                doc["project"]["name"] = name
                settings.write_text(json.dumps(doc))
                status, body = self.request("POST", f"/projects/{self.project.id}/vex/download",
                                            {"slot": 1, "name": "ok"})
                self.assertEqual(status, 409)
                self.assertIn("letters, digits", body["error"])
                self.assertIsNone(vex.binary(self.folder, name))
        self.assertEqual(self.world.calls("vexcom"), [])


class DeviceText(unittest.TestCase):
    """A device read's output: JSON pretty-printed, everything else as it came,
    and nothing a JSON string carries can put a control back in the text."""

    def test_json_is_pretty_printed_with_the_lines_before_it_kept(self):
        raw = b'\x1b[32mvexcom 1.0.2b2\x1b[0m\r\n{"a": 1, "b": ["x", {"c": null}]}\r\n'
        self.assertEqual(vex.device_text(raw),
                         'vexcom 1.0.2b2\n{\n  "a": 1,\n  "b": [\n    "x",\n    {\n      "c": null\n'
                         '    }\n  ]\n}')

    def test_a_json_string_cannot_bring_back_a_control_or_an_invisible(self):
        raw = json.dumps({"name": "A\u001b[31mB‮C​D\u0007E", "html": "<i>x</i>"}).encode()
        text = vex.device_text(raw)
        # Controls stay escaped (`\u001b`, as JSON writes them); a bidi override
        # or a zero-width space, which JSON would write raw, is removed.
        self.assertFalse(any(ord(ch) < 32 and ch != "\n" or vex.untrusted.unseen(ch) and ch != "\n"
                             for ch in text), repr(text))
        self.assertEqual(json.loads(text), {"name": "A\x1b[31mBCD\x07E", "html": "<i>x</i>"})

    def test_text_that_is_not_json_comes_back_as_text(self):
        for raw, want in ((b"slot 1: override\nslot 3: {skills}\n", "slot 1: override\nslot 3: {skills}\n"),
                          (b"{not json\n", "{not json\n"), (b"", "")):
            with self.subTest(raw=raw):
                self.assertEqual(vex.device_text(raw), want)

    def test_deep_or_huge_json_never_raises(self):
        deep = b"[" * 100_000 + b"]" * 100_000        # under the parse cap: RecursionError
        self.assertEqual(len(vex.device_text(deep)), vex.INFO_TEXT_CAP)     # left as text, capped
        big = b'{"x": "' + b"a" * (vex.INFO_PARSE_CAP + 10) + b'"}'
        self.assertTrue(vex.device_text(big).endswith('aaa"}'))


class DeviceHolders(unittest.TestCase):
    """L3: a reservation is released only by its holder, and at most once."""

    def test_a_stale_release_never_frees_another_holders_reservation(self):
        v = vex.Vex(SimpleNamespace())
        release_a, bind_a = v._reserve_device("info")
        self.assertTrue(release_a())
        self.assertFalse(release_a(), "at most once")
        release_b, bind_b = v._reserve_device("download")
        self.assertFalse(release_a(), "A's late release must not free B's download")
        bind_a("deadbeef")
        self.assertEqual(v._device["kind"], "download")
        self.assertIsNone(v._device["terminal_id"], "A cannot name B's terminal")
        bind_b("cafef00d")
        self.assertEqual(v._device["terminal_id"], "cafef00d")
        with self.assertRaises(vex.VexError):
            v._reserve_device("brain")
        self.assertTrue(release_b())
        self.assertIsNone(v._device)


class TemplateFiles(Base):
    """L6: the archive is opened as a regular file only; writes never follow a
    symlink and never pass through something that is not a folder."""

    def test_a_fifo_template_is_refused_without_hanging(self):
        cfg = vex.load_config()
        os.mkfifo(self.world.templates / "cpp_v5_vsc_pipe.zip")
        listed = [{"id": "cpp_v5_vsc_pipe", "language": "cpp", "label": "Pipe"}]
        with patch.object(vex, "list_templates", lambda _cfg: listed):
            started = time.monotonic()
            self.assert_refused(lambda: vex.read_template(cfg, "cpp_v5_vsc_pipe"),
                                "not a regular file")
            self.assertLess(time.monotonic() - started, 3)

    def test_a_file_or_a_symlink_mid_path_is_never_written_through(self):
        cfg = vex.load_config()
        settings = vex.settings_document("t", "cpp", cfg)
        blocked = self.world.robotics / "blocked"
        blocked.mkdir()
        (blocked / "src").write_text("a file where a folder would go")
        self.assert_refused(lambda: vex.unpack([("src/main.cpp", b"x")], blocked, settings),
                            "not a folder")
        self.assertTrue((blocked / "src").is_file(), "what was there before is never taken back")
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.world.robotics / "linked"
        linked.mkdir()
        (linked / "src").symlink_to(outside)
        self.assert_refused(lambda: vex.unpack([("src/main.cpp", b"x")], linked, settings),
                            "not a folder")
        self.assertEqual(list(outside.iterdir()), [], "nothing was written outside")

    def test_unpack_refuses_a_symlinked_folder(self):
        cfg = vex.load_config()
        real = self.world.robotics / "real"
        real.mkdir()
        link = self.world.robotics / "link"
        link.symlink_to(real)
        self.assert_refused(lambda: vex.unpack([("a.txt", b"x")], link,
                                               vex.settings_document("t", "cpp", cfg)),
                            "not a folder")
        self.assertEqual(list(real.iterdir()), [])


class Coverage(DaemonBase):
    """The review's test gaps: each guard, with the check that holds it."""

    def test_an_archived_project_is_refused(self):
        project = self.stores.projects.get(self.project.id)
        project.archived = "2026-10-11T00:00:00+00:00"
        self.stores.projects.save(project)
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/build",
                                    {"action": "build"})
        self.assertEqual(status, 409)
        self.assertIn("archived", body["error"])
        self.assertEqual(self.world.calls("make"), [])

    def test_daemon_stop_stops_vex_and_frees_the_build_lock(self):
        (self.folder / "src" / "SLOW").write_text("")
        self.build(wait=False)
        eventually(lambda: self.world.calls("make"), what="make started")
        self.assertFalse(vex.BuildLock(self.folder).acquire())
        self.daemon.stop()
        self.assertTrue(self.daemon.vex._stopped)
        self.assertEqual(self.daemon.vex._timers, set())
        self.assertTrue(vex.BuildLock(self.folder).acquire(), "the lock went with the daemon")
        self.assertRaises(vex.VexError, self.daemon.vex._start, ["x"], cfg=vex.load_config(),
                          tool="make", folder=str(self.folder), label="x", program="x", title="x")

    def test_a_symlinked_build_folder_is_no_binary(self):
        self.build()
        elsewhere = self.root / "elsewhere-build"
        (self.folder / "build").rename(elsewhere)
        (self.folder / "build").symlink_to(elsewhere)
        self.assertTrue((elsewhere / "override.bin").is_file())
        self.assertIsNone(vex.binary(self.folder, "override"))
        status, body = self.request("POST", f"/projects/{self.project.id}/vex/download",
                                    {"slot": 1, "name": "a"})
        self.assertEqual(status, 409)
        self.assertIn("Build first", body["error"])

    def test_program_terminals_are_capped_apart_from_shells(self):
        made = [self.daemon.terminals.create_program(
            [str(self.world.vexcom), "--user"], folder=str(self.world.vexcom_dir), label="cap",
            program="cap-test", title=f"cap {i}") for i in range(T.PROGRAM_CAP)]
        with self.assertRaises(T.TerminalError):
            self.daemon.terminals.create_program(
                [str(self.world.vexcom), "--user"], folder=str(self.world.vexcom_dir), label="cap",
                program="cap-test", title="one too many")
        # Six programs leave every shell slot free.
        shells = [self.request("POST", "/terminals", {"in": "home"}, status=201)[1]["id"]
                  for _ in range(T.TERMINAL_CAP)]
        self.assertEqual(len(shells), T.TERMINAL_CAP)
        self.request("POST", "/terminals", {"in": "home"}, status=409)
        for terminal in made:
            self.daemon.terminals.close(terminal.id, grace=0)

    def test_a_program_terminal_needs_an_absolute_program(self):
        for argv in (["vexcom.exe", "--user"], [], ["relative/make.exe"]):
            with self.subTest(argv=argv), self.assertRaises(T.TerminalError):
                self.daemon.terminals.create_program(argv, folder=str(self.folder), label="x",
                                                     program="x", title="x")

    def test_a_post_without_an_origin_is_refused_whatever_sec_fetch_site_says(self):
        p = self.project.id
        for path, body in ((f"/projects/{p}/vex/build", {"action": "build"}),
                           (f"/projects/{p}/vex/download", {"slot": 1, "name": "a"}),
                           ("/vex/brain", {}), ("/vex/device/info", {}),
                           ("/vex/projects", {"name": "x", "language": "cpp",
                                              "template": "cpp_v5_vsc_empty"}),
                           ("/vex/projects/check", {"name": "x", "language": "cpp",
                                                    "template": "cpp_v5_vsc_empty"})):
            with self.subTest(path=path):
                self.request("POST", path, body, origin=None,
                             headers={"Sec-Fetch-Site": "same-origin"}, status=403)
        self.assertEqual(self.world.calls("make") + self.world.calls("vexcom"), [])


# -- VX-3: the new-project dialog's check ---------------------------------------------------------------

class NewProjectCheck(DaemonBase):
    """`POST /vex/projects/check`: what Create would do, nothing asked or made.
    The dialog shows its `path` as the exact folder, so it must be the very
    path the card names, and every refusal must be Create's own."""

    BODY = {"name": "checkbot", "language": "cpp", "template": "cpp_v5_vsc_empty"}

    def check(self, body, status=200):
        return self.request("POST", "/vex/projects/check", body, status=status)[1]

    def test_the_path_is_the_cards_path_and_nothing_is_asked_or_made(self):
        seen = self.check(self.BODY)
        target = self.world.robotics / "checkbot"
        self.assertEqual(seen["path"], str(target))
        self.assertEqual(seen["windows_path"], self.world.windows(target))
        self.assertEqual((seen["state"], seen["asks"]), ("missing", True))
        self.assertEqual(seen["describe"], "a new, empty folder")
        time.sleep(0.2)
        self.assertEqual(self.daemon.approvals.pending(), [], "a check never raises the card")
        self.assertFalse(target.exists(), "a check makes nothing")
        self.assertEqual(self.daemon.vex._news, {}, "a check is not a request")
        # Create then names exactly that path, on the response and on the card.
        made = self.request("POST", "/vex/projects", self.BODY, status=202)[1]
        self.assertEqual(made["path"], seen["path"])
        card = eventually(lambda: self.daemon.approvals.pending(), what="the card")[0]
        self.assertEqual(card.args["path"], seen["path"])
        self.daemon.approvals.resolve(card.req_id, Decision.DENY)

    def test_an_empty_folder_is_used_without_a_card(self):
        (self.world.robotics / "checkbot").mkdir()
        seen = self.check(self.BODY)
        self.assertEqual((seen["state"], seen["asks"]), ("empty", False))
        self.assertEqual(seen["describe"], "an existing empty folder")

    def test_a_python_template_and_a_location(self):
        other = self.world.user / "OneDrive" / "Desktop"
        other.mkdir(parents=True)
        seen = self.check({"name": "pybot", "language": "python", "template": "py_v5_vsc_empty",
                           "location": str(other) + "/"})
        self.assertEqual(seen["path"], str(other / "pybot"))

    def test_every_refusal_is_creates_own(self):
        full = self.world.robotics / "fullbot"
        full.mkdir()
        (full / "x").write_text("")
        cases = [({**self.BODY, "name": "a b"}, 400), ({**self.BODY, "name": "-x"}, 400),
                 ({**self.BODY, "language": "blocks"}, 400),
                 ({**self.BODY, "language": "python"}, 400),
                 ({**self.BODY, "template": "../x"}, 400),
                 ({**self.BODY, "location": ""}, 400),
                 ({**self.BODY, "name": "fullbot"}, 409),
                 ({**self.BODY, "location": str(self.root / "linux")}, 409),
                 ({**self.BODY, "location": str(self.world.user / "nowhere")}, 409),
                 ({**self.BODY, "path": "/x"}, 400), ({"name": "x"}, 400)]
        for body, status in cases:
            with self.subTest(body=body):
                said = self.check(body, status=status)["error"]
                if status == 409 and "path" not in body:
                    created = self.request("POST", "/vex/projects", body, status=409)[1]["error"]
                    self.assertEqual(created.removesuffix(" Nothing was made."), said)
        self.assertEqual(self.daemon.approvals.pending(), [])
        self.assertEqual(self.daemon.vex._news, {})

    def test_a_folder_now_a_project_is_refused(self):
        target = self.world.robotics / "taken"
        target.mkdir()
        self.stores.projects.create("taken", str(target))
        self.assertIn("already a project", self.check({**self.BODY, "name": "taken"}, status=409)["error"])

    def test_a_folder_vexs_tools_refuse_is_refused_before_anyone_is_asked(self):
        # Review MED-1: `to_windows` refused these at build time, after the owner
        # had approved a project that could never be built.
        for shape in ("My (1)", "a&b", "Документы"):
            with self.subTest(shape=shape):
                parent = self.world.user / "Documents" / shape
                parent.mkdir(parents=True)
                body = {**self.BODY, "location": str(parent)}
                said = self.check(body, status=409)["error"]
                self.assertIn("Windows command line could misread", said)
                self.assertIn("could never build", said)
                created = self.request("POST", "/vex/projects", body, status=409)[1]["error"]
                self.assertEqual(created, said + " Nothing was made.", "Create refuses in the same words")
                self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(self.daemon.approvals.pending(), [])
        self.assertEqual(self.daemon.vex._news, {})

    def test_the_shown_path_is_normalised_as_the_card_names_it(self):
        # Review LOW-1: a location the daemon normalises — a doubled slash, a
        # trailing one — shows the very path the card names, never the raw join.
        location = str(self.world.user) + "//Documents/Robotics/"
        body = {**self.BODY, "location": location}
        seen = self.check(body)
        target = self.world.robotics / "checkbot"
        self.assertEqual(seen["path"], str(target))
        self.assertEqual(seen["windows_path"], self.world.windows(target))
        made = self.request("POST", "/vex/projects", body, status=202)[1]
        card = eventually(lambda: self.daemon.approvals.pending(), what="the card")[0]
        self.assertEqual(card.args["path"], seen["path"])
        self.assertEqual(made["path"], seen["path"])
        self.daemon.approvals.resolve(card.req_id, Decision.DENY)

    def test_a_templates_folder_that_cannot_be_listed_is_a_sentence_not_a_500(self):
        def gone(_cfg):
            raise FileNotFoundError("gone between the check and the listing")
        with patch.object(vex, "list_templates", gone):
            status, body = self.request("GET", "/vex/templates")
        self.assertEqual(status, 409, body)
        self.assertIn("vex setup", body["error"])


class HalfWritten(DaemonBase):
    """Review LOW-2: a template write that fails midway takes back what this
    request wrote — through the folder's fds, only the files it created and the
    folders it made — and says so; the folder, now empty, takes a retry."""

    def make(self, template, name="halfbot"):
        made = self.request("POST", "/vex/projects", {"name": name, "language": "cpp",
                                                      "template": template}, status=202)[1]
        card = eventually(lambda: self.daemon.approvals.pending(), what="the card")[0]
        self.daemon.approvals.resolve(card.req_id, Decision.ALLOW)
        return eventually(lambda: (lambda s: s if s["state"] != "asking" else None)(
            self.owner("GET", f"/vex/projects/{made['id']}")), what="the request")

    def assert_taken_back_and_retried(self, done, target):
        self.assertEqual(done["state"], "failed", done)
        self.assertIn(str(target), done["message"])
        self.assertIn("is empty again", done["message"])
        self.assertTrue(target.is_dir())
        self.assertEqual(sorted(p.name for p in target.iterdir()), [], "half a template was left")
        self.assertNotIn("halfbot", [p.name for p in self.stores.projects.list()])
        # The emptied folder takes a retry: used without asking (O5), and made.
        again = self.request("POST", "/vex/projects", {"name": "halfbot", "language": "cpp",
                                                       "template": "cpp_v5_vsc_empty"}, status=202)[1]
        final = eventually(lambda: (lambda s: s if s["state"] != "asking" else None)(
            self.owner("GET", f"/vex/projects/{again['id']}")), what="the retry")
        self.assertEqual(final["state"], "created", final)
        self.assertTrue((target / "src" / "main.cpp").is_file())

    def test_an_entry_that_cannot_be_written_takes_back_the_rest(self):
        self.world.zip(self.world.templates / "cpp_v5_vsc_broken.zip", {
            "makefile": "include vex/mkenv.mk\n", "vex/mkenv.mk": "CC = clang\n",
            "vex/mkenv.mk/x": "a file under a file"})
        done = self.make("cpp_v5_vsc_broken")
        self.assertIn("not a folder", done["message"])
        self.assert_taken_back_and_retried(done, self.world.robotics / "halfbot")

    def test_a_disk_error_midway_takes_back_the_rest(self):
        real = vex._write_new
        calls = []

        def flaky(folder_fd, rel, data, made=None):
            calls.append(rel)
            if len(calls) == 3:
                raise OSError(28, "No space left on device", "/somewhere/secret")
            return real(folder_fd, rel, data, made)

        with patch.object(vex, "_write_new", flaky):
            done = self.make("cpp_v5_vsc_empty")
        self.assertIn("OSError: No space left on device", done["message"])
        self.assertNotIn("/somewhere/secret", done["message"])
        self.assert_taken_back_and_retried(done, self.world.robotics / "halfbot")

    def test_what_was_there_before_is_never_taken(self):
        cfg = vex.load_config()
        target = self.world.robotics / "keep"
        (target / "vex").mkdir(parents=True)
        (target / "vex" / "notes.txt").write_text("the owner's")
        with self.assertRaises(vex.UnpackFailed) as caught:
            vex.unpack([("makefile", b"x"), ("vex/mkenv.mk", b"y"), ("vex/mkenv.mk/z", b"z")],
                       target, vex.settings_document("keep", "cpp", cfg))
        self.assertEqual(caught.exception.left, [])
        # `vex/` was the owner's: never recorded as made, so never removed.
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["vex"])
        self.assertEqual(sorted(p.name for p in (target / "vex").iterdir()), ["notes.txt"])

    # -- the delta review of 9e1fcb6: identities, not names -------------------------------

    def test_a_folder_swapped_in_mid_write_is_never_taken(self):
        # LOW-B: after the request made `vex/` and wrote `vex/mkenv.mk`, another
        # program renames `vex` away and moves an owner folder holding a
        # `mkenv.mk` into its name; then the next write fails. The take-back
        # judges what it made by (st_dev, st_ino), so the owner's are kept.
        cfg = vex.load_config()
        target = self.world.robotics / "swapbot"
        target.mkdir()
        owner = self.world.robotics / "ownerstuff"
        owner.mkdir()
        (owner / "mkenv.mk").write_text("the owner's")
        real = vex._write_new

        def swapping(folder_fd, rel, data, made=None):
            if rel == "include/vex.h":
                (target / "vex").rename(self.world.robotics / "moved-vex")
                owner.rename(target / "vex")
                raise OSError(28, "No space left on device")
            return real(folder_fd, rel, data, made)

        with patch.object(vex, "_write_new", swapping), self.assertRaises(vex.UnpackFailed) as caught:
            vex.unpack([("makefile", b"m"), ("vex/mkenv.mk", b"ours"), ("include/vex.h", b"h")],
                       target, vex.settings_document("swapbot", "cpp", cfg))
        self.assertEqual((target / "vex" / "mkenv.mk").read_text(), "the owner's")
        self.assertFalse((target / "makefile").exists(), "what it made, still in place, goes")
        self.assertEqual(sorted(caught.exception.left), ["vex", "vex/mkenv.mk"])
        self.assertEqual(caught.exception.remaining, ["vex"])
        self.assertNotIn("empty again", vex.unmade_message(target, caught.exception.cause,
                                                           caught.exception.left,
                                                           caught.exception.remaining))

    def test_a_folder_swapped_before_the_store_answers_is_never_taken(self):
        # LOW-A: the store refuses the project after another program swapped
        # `Robotics` for a symlink to a decoy holding the owner's own
        # `probebot/{makefile, src/main.cpp, vex/mkenv.mk}`. The take-back
        # reopens the folder and finds it is not the one the template was
        # written through: nothing is removed, here or there.
        from jarvis.v2 import projects as project_module
        from jarvis.v2.daemon import DaemonError
        decoy = self.world.user / "Documents" / "Decoy"
        owner = decoy / "probebot"
        (owner / "src").mkdir(parents=True)
        (owner / "vex").mkdir()
        mine = ("makefile", "src/main.cpp", "vex/mkenv.mk")
        for rel in mine:
            (owner / rel).write_text("the owner's " + rel)
        robotics = self.world.robotics
        moved = robotics.with_name("Robotics.real")

        def refusing(*_args, **_kwargs):
            robotics.rename(moved)
            robotics.symlink_to(decoy)
            raise DaemonError("the store refused the project")

        with patch.object(project_module, "create_project", refusing):
            done = self.make("cpp_v5_vsc_empty", name="probebot")
        self.assertEqual(done["state"], "failed", done)
        for rel in mine:
            self.assertEqual((owner / rel).read_text(), "the owner's " + rel)
        self.assertEqual(sorted(p.name for p in owner.iterdir()), ["makefile", "src", "vex"])
        self.assertIn("is no longer the folder", done["message"])
        self.assertTrue((moved / "probebot" / "src" / "main.cpp").is_file(),
                        "nothing was taken back through a path that moved")

    def test_a_folder_the_owner_filled_meanwhile_is_never_called_empty(self):
        # LOW-C: the owner's src/main.cpp and notes.txt appear while the card is
        # up; both cards are approved; the write collides. What it made is taken
        # back, the owner's stay, and the message lists what the folder holds.
        target = self.world.robotics / "probebot"
        self.request("POST", "/vex/projects", {"name": "probebot", "language": "cpp",
                                               "template": "cpp_v5_vsc_empty"}, status=202)
        first = eventually(lambda: self.daemon.approvals.pending(), what="the card")[0]
        (target / "src").mkdir(parents=True)
        (target / "src" / "main.cpp").write_text("the owner's")
        (target / "notes.txt").write_text("the owner's notes")
        self.daemon.approvals.resolve(first.req_id, Decision.ALLOW)
        second = eventually(lambda: [c for c in self.daemon.approvals.pending()
                                     if c.req_id != first.req_id], what="the second card")[0]
        self.daemon.approvals.resolve(second.req_id, Decision.ALLOW)
        news = self.daemon.vex._news
        done = eventually(lambda: (lambda r: r.view() if r.state != "asking" else None)(
            list(news.values())[-1]), what="the request")
        self.assertEqual(done["state"], "failed", done)
        self.assertNotIn("empty again", done["message"])
        self.assertIn("notes.txt", done["message"])
        self.assertIn("src/main.cpp already exists", done["message"], "the NIT: the colliding file named")
        self.assertEqual((target / "src" / "main.cpp").read_text(), "the owner's")
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["notes.txt", "src"])
        self.assertEqual(sorted(p.name for p in (target / "src").iterdir()), ["main.cpp"])


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
            brain_term = self.daemon.terminals.get(brain)
            eventually(lambda: b"user port open" in brain_term.history().data, what="brain output")
            self.owner("DELETE", f"/terminals/{brain}")
            eventually(lambda: any("vex brain terminal ended" in r for r in records), what="brain log")
        terminal = self.daemon.terminals.get(result["terminal_id"])
        self.assertIn(printed.encode(), terminal.history().data)        # the capture worked
        drained = []
        while True:
            try:
                drained.append(events.get_nowait())
            except Exception:
                break
        # One id-only record per device action (L5), as `terminal_attached`:
        # the action and the time, never output, a slot, a name or a path.
        self.assertEqual([r["kind"] for r in drained], ["vex_device", "vex_device"], drained)
        self.assertEqual([r["data"]["action"] for r in drained], ["download", "brain"])
        for record in drained:
            self.assertEqual(set(record), {"kind", "data"})
            self.assertEqual(set(record["data"]), {"action", "at"})
            self.assertRegex(record["data"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn(marker, json.dumps(drained))
        text = "\n".join(records)
        self.assertIn("vex build started for project override", text)
        self.assertIn("vex download to slot 2 started for project override", text)
        self.assertNotIn(printed, text)
        # The brain's end is logged with the shape of its first output (VX-UI-4b's
        # diagnostic): its text counted, never copied.
        ended = [r for r in records if "vex brain terminal ended" in r]
        self.assertEqual(len(ended), 1, records)
        self.assertIn("<text ", ended[0])
        self.assertNotIn("user port open", text)
        self.assertNotIn("Downloading User Program", text)
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(printed.encode(), path.read_bytes(), path)


class Reach(unittest.TestCase):
    """No tool, MCP tool, fast-path tool or Discord verb reaches download, the
    brain terminal, device control, the routes or the runners."""

    NEEDLES = re.compile(r"""['"]/vex\b|['"]vex['"]|jarvis\.v2\.vex\b|from\s+\.\s+import\s+vex\b|"""
                         r"""from\s+\.vex\s+import|\.vex\.|\bvexcom\b|create_program|\bVex\(""", re.I)
    # vex_connection.py: the USB-presence half of vex.py (5b0ac8c), imported only by it.
    ALLOWED = {"jarvis/v2/vex.py", "jarvis/v2/vex_connection.py", "jarvis/v2/vex_setup.py", "jarvis/v2/hud_api.py",
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


# -- review round 2: what a line runs, the caps, includes, names, the CLI ---------------------------

TOOLS_DIR = "/mnt/c/Users/placeholder/AppData/Roaming/Code/User/globalStorage/vexrobotics.vexcode/tools"
GLOBBED = f"{TOOLS_DIR}/v*/*/win32/v*.exe --user"
# A glob, or a word holding an expansion, in program position that could be
# vexcom by its file name: DENY, with no four-letter threshold (MED 1).
RUNS = {
    "uv run, one letter": f"uv run {TOOLS_DIR}/v*/*/*/v*.exe --name x --slot 1 --write build/x.bin",
    "uv run, stars only": f"uv run {TOOLS_DIR}/*/*/win32/*.exe --user",
    "uv run, a variable": f"X=vex; uv run {TOOLS_DIR}/${{X}}com/*/win32/${{X}}com.exe --user",
    "uv run, few letters": f"uv run {TOOLS_DIR}/*/*/win32/v*m.exe --user",
    "command position": GLOBBED,
    "uv run --no-project": f"uv run --no-project {GLOBBED}",
    "uv --directory": f"uv --directory /tmp run {GLOBBED}",
    "uv tool run": f"uv tool run {GLOBBED}",
    "uvx": f"uvx {GLOBBED}",
    "poetry run": f"poetry run {GLOBBED}",
    "poetry -q run": f"poetry -q run {GLOBBED}",
    "pipx run --spec": f"pipx run --spec x {GLOBBED}",
    "npm exec --": f"npm exec -- {GLOBBED}",
    "npx --yes": f"npx --yes {GLOBBED}",
    "npx -c": f"npx -c '{GLOBBED}'",
    "pnpm exec": f"pnpm exec {GLOBBED}",
    "pnpm dlx -c": f"pnpm dlx -c '{GLOBBED}'",
    "bun x": f"bun x {GLOBBED}",
    "bun exec": f"bun exec '{GLOBBED}'",
    "nohup": f"nohup {GLOBBED}",
    "timeout -s KILL 5": f"timeout -s KILL 5 {GLOBBED}",
    "env -C dir": f"env -C /tmp FOO=1 {GLOBBED}",
    "env -S": f"env -S '{GLOBBED}'",
    "env -u X -S": f"env -u X -S '{GLOBBED}'",
    "nice, then uv run": f"nice -n 5 uv run {GLOBBED}",
    "systemd-run": f"systemd-run --user --scope -q {GLOBBED}",
    "flock -c": f"flock /tmp/x -c '{GLOBBED}'",
    "sh -c": f"sh -c '{GLOBBED}'",
    "bash -lc": f"bash -lc '{GLOBBED}'",
    "uv run sh -c": f"uv run sh -c '{GLOBBED}'",
    "here-string": f"bash <<< '{GLOBBED}'",
    "eval": f"eval {GLOBBED}",
    "watch": f"watch -n 5 {GLOBBED}",
    "$(…)": f"echo $({GLOBBED})",
    "backticks": f"echo `{GLOBBED}`",
    "<(…)": f"cat <({GLOBBED})",
    "find -exec": f"find {TOOLS_DIR} -name x -exec {GLOBBED} \\;",
    "xargs": f"xargs {GLOBBED} < args.txt",
    "if/then": f"if true; then {GLOBBED}; fi",
    "subshell": f"(cd /tmp; {GLOBBED})",
    "a bare star": "*.exe --user",
    "an expansion then a name": "${X}com.exe --user",
    "an expansion then .exe": "$X.exe --user",
    "a substitution then a name": "$(printf vex)com --user",
    "brace expansion": "{v,x}excom.exe --user",
    "ANSI-C then a glob": "$'\\x2e/v'*.exe --user",
    # Fewer than four letters, so only the program reading can refuse these.
    "posix class": "./v[[:alpha:]]x* --user",
    "negated with ^": "./ve[^a]com --user",
    "only fnmatch's bracket reading matches": "./v[x[:]x*[m:] --user",
    "fullwidth glob": "ｖ*.exe --user",
}
# A program that cannot be told and could be vexcom: ASK in v1, never covered
# by an allowlist entry, always asked in v2 (MED 1).
UNSURE = {
    "uv run $X": "uv run $X --user",
    "a variable": "$X --user",
    "all arguments": '"$@"',
    "a substitution": "$(cat name.txt) --user",
    "two expansions": "${A}.${B} --user",
    "an option the table does not know": f"uv run --frobnicate x {GLOBBED}",
    "xargs env": "ls /mnt/c/x/*.exe | xargs env",
    "find -exec {}": f"find {TOOLS_DIR} -name 'v*.exe' -exec {{}} --user \\;",
    "xargs -I": "ls | xargs -I{} {} --user",
    "sh -c with $0": "sh -c '\"$0\" --user' x",
}
# Each ALLOW on 33dcb99 (or ASK for another reason); each runs vexbuild or its module (LOW 1).
VEXBUILD_RUNS = ("uv run --no-project vexbuild build", "uv run --python 3.14 vexbuild",
                 "uv run --no-sync vexbuild", "poetry run -q vexbuild", "npx --yes vexbuild",
                 "pipx run --spec foo vexbuild", "uv run --module jarvis.v2.vex build",
                 "python3 -m runpy jarvis.v2.vex build", "npx vexbuild", "bunx vexbuild",
                 "uvx vexbuild", "pnpx vexbuild", "uv run sh -c vexbuild",
                 "npm exec -c 'vexbuild build'", "uv run --some-future-flag x vexbuild",
                 "npm exec -- vexbuild", "bun x vexbuild", "uv tool run --from x vexbuild",
                 "uv run -Z x vexbuild")
ORDINARY_RUNNERS = {
    "uv run pytest -q": rules.ALLOW, "uv run --no-project python script.py": rules.ALLOW,
    "uv run --with rich python x.py": rules.ALLOW, "npx --yes prettier --check .": rules.ALLOW,
    "poetry run pytest": rules.ALLOW, "timeout 60 ls v*": rules.ALLOW,
    "env FOO=1 ls *.exe": rules.ALLOW, "xargs ls v*": rules.ALLOW, "nohup make": rules.ALLOW,
    "uv run --frobnicate pytest -q": rules.ALLOW, "npm exec -- prettier --check .": rules.ALLOW,
    "ls '*'": rules.ALLOW, "ls v*m.exe": rules.ALLOW, "uv pip install -e .": rules.ALLOW,
    "git commit -m 'fix the v* glob'": rules.ALLOW, "make -j4 2>&1 | tail -20": rules.ALLOW,
    "ls " + "[a-z]" * 10: rules.ALLOW, "npm run vexbuild": rules.ALLOW,
    # Asked as on main, never refused: a quoted star is a file called `*`,
    # and a brace expansion runs its first word.
    "'*' --user": rules.ASK, "arm-none-eabi-{ar,ld}.exe x": rules.ASK,
}


class ProgramPosition(Base):
    """MED 1: program position is judged strictly; the threshold is for arguments only."""

    def setUp(self):
        super().setUp()
        self.ran = []
        real = shell._run
        shell._run = lambda command, *a, **k: self.ran.append(command) or "[exit 0]"
        self.addCleanup(setattr, shell, "_run", real)
        self.addCleanup(v1_permissions.set_mode, "ask")
        # dispatch refuses a call with no toolset bound (PR #42): this test
        # stands where an agent's turn would, holding run_command alone.
        bound = runtime.tools_bound({"run_command"})
        bound.__enter__()
        self.addCleanup(bound.__exit__, None, None, None)

    def test_a_glob_or_expansion_the_line_runs_is_deny(self):
        for label, command in RUNS.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.DENY, command)
                self.assertIn("vexcom", verdict.reason)
                self.assertEqual(v1_permissions.static_verdict(command).decision, rules.DENY)
                self.assertIsNotNone(v2_permissions.denied_command(command))

    def test_runs_never_run_or_raise_a_card(self):
        Path(config.ALLOWLIST_PATH).write_text(json.dumps([
            {"tool": "run_command", "prefix": p}
            for p in ("uv", "uvx", "npx", "npm", "pnpm", "poetry", "pipx", "bun", "nohup",
                      "timeout", "env", "nice", "sh", "bash", "echo", "cat", "find", "xargs",
                      "eval", "watch", "systemd-run", "flock", "*.exe")]))
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args) or True)
        for mode in ("ask", "all"):
            v1_permissions.set_mode(mode)
            for label, command in RUNS.items():
                with self.subTest(mode=mode, shape=label):
                    result = tools.dispatch("run_command", json.dumps({"command": command,
                                                                       "reason": "probe"}), gate)
                    self.assertIn("vexcom", str(result.text))
        self.assertEqual((self.ran, asked), ([], []))
        asker = Asker()
        for profile in (PP.AUTO, PP.ASK):
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label, command in RUNS.items():
                with self.subTest(profile=profile.value, shape=label):
                    self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
        self.assertEqual(asker.seen, [])

    def test_a_program_that_cannot_be_told_is_asked_whatever_stands(self):
        stems = {"uv", "$x", "$@", "$(cat", "${a}.${b}", "ls", "xargs", "find", "sh"}
        Path(config.ALLOWLIST_PATH).write_text(json.dumps(
            [{"tool": "run_command", "prefix": p} for p in stems]))
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args["command"]) or False)
        for label, command in UNSURE.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.ASK, command)
                self.assertIn("vexcom", verdict.reason)
                self.assertIsNotNone(rules.vex_uncoverable(command))
                self.assertFalse(v1_permissions.allows("run_command", {"command": command}),
                                 "an allowlist entry covered it")
                tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}),
                               gate)
        self.assertEqual(sorted(asked), sorted(UNSURE.values()), "v1 asked about each")
        self.assertEqual(self.ran, [], "a no ran nothing")
        # v2: asked under AUTO too — never the provider's reviewer's call.
        for profile in (PP.AUTO, PP.ASK):
            asker = Asker(Decision.DENY)
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label, command in UNSURE.items():
                with self.subTest(profile=profile.value, shape=label):
                    self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
            self.assertEqual(len(asker.seen), len(UNSURE), profile)

    def test_runner_options_are_parsed_and_vexbuild_asks(self):
        for command in VEXBUILD_RUNS:
            with self.subTest(command):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.ASK, command)
                self.assertIn("vexbuild", verdict.reason)
                self.assertEqual(v1_permissions.static_verdict(command).decision, rules.ASK)

    def test_ordinary_runner_lines_keep_their_verdicts(self):
        for command, expected in ORDINARY_RUNNERS.items():
            with self.subTest(command):
                self.assertEqual(rules.decide(command).decision, expected, command)
                self.assertIsNone(rules.vex_uncoverable(command))


class TooComplex(Base):
    """MED 2: a glob past the caps is too complex to judge — asked, never slow."""

    FLOODS = {"[": "ls " + "[" * 20000, "[a": "ls " + "[a" * 20000, "[[:": "ls " + "[[:" * 8000,
              "*?": "ls " + "*?" * 20000, "v[": "./v" + "[" * 20000, "${": "echo " + "${" * 80000,
              "many words": "ls " + " ".join(f"v{i}*" for i in range(4000)),
              "{": "{" * 20000 + " --user", "$((": "./v$((" * 8000,
              "nested $(…)": "echo " + "$(echo " * 3000 + "x" + ")" * 3000,
              "nested eval": "eval " * 3000 + "x",
              "here-documents": "cat <<E " * 15000 + "`x` " * 15000}

    # One second of calling-thread CPU at this machine's idle speed
    # (`tests/loadclock.py`); the slowest flood takes ~0.4 s idle. Pinned:
    # it sets the guard's strength (MED 2's scans took 3.4 s and 14 s).
    LIMIT_S = 1.0

    def held(self, label, work):
        """`work()`'s value, its CPU time judged by `loadclock` checked
        against `LIMIT_S` (the best of three: a pause hits one run, a
        regression every run), and its wall time against 20 s."""
        best = value = None
        for _ in range(3):
            rules._runners_unsure_cached.cache_clear()
            rules._vex_line_cached.cache_clear()
            # Parser work is synchronous; also check elapsed wall time when it returns.
            wall = time.perf_counter()
            took, value = loadclock.cpu(work)
            run = loadclock.judge(took, self.LIMIT_S)
            self.assertLess(time.perf_counter() - wall, 20.0, f"{label}: elapsed wall limit")
            best = run if best is None or run.idle < best.idle else best
            if best.idle < self.LIMIT_S:
                break
        self.assertLess(best.idle, self.LIMIT_S, f"{label}: {best}")
        return value

    def test_the_bound_is_pinned(self):
        self.assertEqual(self.LIMIT_S, 1.0)
        loadclock.pinned()

    def test_a_flood_is_judged_fast_asked_and_never_covered(self):
        Path(config.ALLOWLIST_PATH).write_text(json.dumps([
            {"tool": "run_command", "prefix": p} for p in ("ls", "echo", "./v" + "[" * 20000)]))
        for label, line in self.FLOODS.items():
            with self.subTest(label):
                verdict = self.held(label, lambda: rules.decide(line))
                if label in ("${",):
                    continue                    # no glob in it: as on main
                self.assertEqual(verdict.decision, rules.ASK, label)
                self.assertFalse(v1_permissions.allows("run_command", {"command": line}))
                self.assertIsNotNone(rules.vex_uncoverable(line))
        asker = Asker(Decision.DENY)
        permit = v2_permissions.build_permit(context(self.root), asker)
        self.assertEqual(permit("Bash", {"command": self.FLOODS["["]}), Decision.DENY)
        self.assertEqual(len(asker.seen), 1, "v2 asks under AUTO")

    def test_a_make_file_flood_is_refused_fast(self):
        folder = self.world.project()
        for label, text in (("one word", "x = " + "[" * 40000 + "\n"),
                            ("a file of them", "".join(f"{'[a' * 30}{i}\n" for i in range(2000))),
                            ("brackets that close", "x = " + "[ab]" * 20000 + "\n")):
            with self.subTest(label):
                (folder / "vex" / "big.mk").write_text(text)
                reason = self.held(label, lambda: vex.makefile_refusal(folder))
                self.assertIsNotNone(reason, label)
                self.assertIn("too complex", reason)
        self.assertEqual(vex.cli(["build"], cwd=str(folder), out=io.StringIO()), 2)
        self.assertEqual(self.world.calls("make"), [])


class MakeIncludes(Base):
    """LOW 2: includes are read as make reads them, and one that does not
    resolve in the project is refused rather than skipped."""

    def setUp(self):
        super().setUp()
        self.folder = self.world.project()
        (self.folder / "src" / "notes.txt").write_text("x:\n\tvexcom --user\n")

    def refused(self, makefile, pattern):
        (self.folder / "makefile").write_bytes(makefile.encode())
        reason = vex.makefile_refusal(self.folder)
        self.assertIsNotNone(reason, makefile)
        self.assertRegex(reason, pattern)

    def test_a_continued_include_is_followed(self):
        self.refused("include vex/mkenv.mk \\\n  src/notes.txt\n", "names vexcom")
        self.refused("include vex/mkenv.mk \\\r\n  src/notes.txt\r\n", "names vexcom")

    def test_a_backslash_in_an_include_is_refused(self):
        # make.exe reads `extra\rules.inc` as `extra/rules.inc`, which names
        # vexcom; on Linux the same word names a clean decoy beside it.
        (self.folder / "extra").mkdir()
        (self.folder / "extra" / "rules.inc").write_text("x:\n\tvexcom --user\n")
        (self.folder / "extra\\rules.inc").write_text("x = 1\n")
        self.refused("include vex/mkenv.mk\ninclude extra\\rules.inc\n", "cannot read as make does")

    def test_an_include_that_does_not_resolve_is_refused(self):
        for line in ("include missing.mk", "-include missing.mk", "sinclude missing.mk",
                     "include extra/*.mk"):
            with self.subTest(line):
                self.refused(line + "\n", "not in the project")

    def test_ordinary_includes_still_pass(self):
        (self.folder / "makefile").write_text("include vex/mkenv.mk\n-include vex/*.mk\n")
        self.assertIsNone(vex.makefile_refusal(self.folder))


class BestFitNames(Base):
    """LOW 3: Windows' best fit maps these to syntax; a path part is ASCII."""

    def test_a_path_part_outside_ascii_is_refused(self):
        for part in ("a\uff02b", "a\uff3cb", "a\u2215b", "caf\u00e9", "a\uff1bb"):
            with self.subTest(part=part):
                self.assertRaises(vex.VexError, vex.to_windows, str(self.world.user / part))


class Polish(Base):
    """LOW 5: the template index is opened as the templates are; the
    natural `vexbuild build` works; the CLI closes what it opened."""

    def test_an_index_swapped_for_a_fifo_never_hangs(self):
        import threading
        index = self.world.templates / "index.json"
        index.unlink()
        os.mkfifo(index)
        result = []
        # As if it had been a regular file when looked at, and swapped since.
        with patch.object(vex, "_regular", lambda path: True):
            reader = threading.Thread(
                target=lambda: result.append(vex._index_names(self.world.templates)), daemon=True)
            reader.start()
            reader.join(3)
        self.assertFalse(reader.is_alive(), "the listing hung on a FIFO")
        self.assertEqual(result, [{}])
        index.unlink()
        index.symlink_to(self.root / "elsewhere.json")
        (self.root / "elsewhere.json").write_text(json.dumps({"examples": [{"samples": [
            {"name": "Planted", "files": [{"name": "cpp_v5_vsc_empty.zip"}]}]}]}))
        with patch.object(vex, "_regular", lambda path: True):
            self.assertEqual(vex._index_names(self.world.templates), {})
        # A FIFO with a writer and a document waiting in it: still not read.
        index.unlink()
        os.mkfifo(index)
        keep = os.open(index, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(index, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, keep)
        self.addCleanup(os.close, writer)
        os.write(writer, (self.root / "elsewhere.json").read_bytes())
        with patch.object(vex, "_regular", lambda path: True):
            self.assertEqual(vex._index_names(self.world.templates), {})

    def test_vexbuild_build_and_no_unclosed_pipe(self):
        import gc
        import warnings
        folder = self.world.project()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.assertEqual(vex.cli(["build", "build"], cwd=str(folder), out=io.StringIO()), 0)
            self.assertEqual(vex.cli(["build", "build", "--clean"], cwd=str(folder),
                                     out=io.StringIO()), 0)
            gc.collect()
        self.assertEqual(self.world.calls("make")[-1]["argv"][-1], "clean")
        leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [])


# -- review round 3: a here-document's body is data, not commands -----------------------------------

R3 = "./v*.exe --user"
# Ordinary commands that hold a here-document: never DENY, as on main (each was DENY at c12959c).
# Each body holds a `*` or `/*` line, which read alone as a command would run a glob that could be
# vexcom, so since round 4 the body backstop asks about each (approvable, never covered).
HEREDOC_ORDINARY = {
    "a commit message": "git commit -m \"$(cat <<'EOF'\nFix the drivetrain PID\n\n* raise kP\n* clamp output\nEOF\n)\"",
    "gh pr create": "gh pr create --title x --body \"$(cat <<'EOF'\n## Summary\n* one\nEOF\n)\"",
    "git commit -F -": "git commit -F - <<'EOF'\nFix the drivetrain PID\n\n* raise kP\nEOF",
    "a C doc comment": "cat > include/auton.h <<'EOF'\n/**\n * @brief Runs the autonomous routine.\n */\nvoid auton();\nEOF",
    "a C comment, unquoted": "cat > a.c <<EOF\n/* x */\nint main(void) { return 0; }\nEOF",
    "a markdown file": "cat > notes.md <<'EOF'\n* one\n* two\nEOF",
    "tee": "tee README.md <<EOF\n# Title\n\n* one\nEOF",
    "tab-stripped": "cat <<-EOF\n\t* one\n\tEOF",
    "crontab": "crontab - <<'EOF'\n* * * * * /path/script\nEOF",
    "python on stdin": "python3 - <<'EOF'\nx = (1\n  * 2)\nprint(x)\nEOF",
    "two on one line": "cat <<A - <<B\n* one\nA\n/* two */\nB",
    "a paren in a message": "git commit -m \"$(cat <<'EOF'\nSteps:\n1) first\n* second\nEOF\n)\"",
    "a spaced operator": "cat << 'EOF'\n* one\nEOF",
    "a double-quoted delimiter": "cat <<\"EOF\"\n/* doc */\nEOF",
    "a command after it": "cat > f.md <<'EOF'\n* one\nEOF\nls -la",
    "inside backticks": "x=`cat <<X\n* one\nX`",
    "a quote in a message": "git commit -m \"$(cat <<'EOF'\nMount the 5\" wheel\n* one\nEOF\n)\"",
    "a paren in an unquoted substitution": "notes=$(cat <<'EOF'\n1) first\n* second\nEOF\n)",
    "a backtick body with no terminator": "echo `cat <<X\n* a\n* b`",
}
# Still read: a body fed to a shell, a substitution an unquoted body runs, and
# every line the lexer must not mistake for a body (each would hide a command).
HEREDOC_RUNS = {
    "bash <<EOF": f"bash <<'EOF'\n{GLOBBED}\nEOF",
    "sh -s": f"sh -s <<EOF\n{R3}\nEOF",
    "env bash": f"env bash <<'EOF'\n{R3}\nEOF",
    "a shell in $(…)": f"x=$(bash <<'EOF'\n{R3}\nEOF\n)",
    "after the terminator": f"cat <<'EOF'\n* one\nEOF\n{R3}",
    "on the operator's line": f"cat <<X; {R3}\nbody\nX",
    "after two here-documents": f"cat <<X <<Y\none\nX\ntwo\nY\n{R3}",
    "after a tab-stripped one": f"cat <<-X\n\tbody\n\tX\n{R3}",
    "a substitution in an unquoted body": f"cat <<EOF\n$({R3})\nEOF",
    "a quoted body ending in a backslash": f"cat <<'X'\nfoo \\\nX\n{R3}",
    "a shift in $((…))": f"echo $((1<<2))\n{R3}",
    "a shift in ((…))": f"((x<<=1))\n{R3}",
    "a shift in $[…]": f"echo $[1<<X]\n{R3}",
    "`<<` in ${…}": f"echo ${{x:-<<X}}\n{R3}",
    "`<<` in quotes": f"echo 'a <<X b'\n{R3}",
    "a here-string, then a line": f"bash <<<'{R3}'\nls",
    "`<<` in a comment": f"# <<X\n{R3}",
    "a backtick's own here-document": f"echo `cat <<X`\n{R3}\nX",
    "a body that ends with its backtick": f"echo `cat <<X\nbody\nX`\n{R3}",
}

# Round 3b: a delimiter is read as bash reads it (each checked with `bash -n` on bash 5.3, which
# names the delimiter it wanted), or the line is too complex to judge. `M` runs only if the body
# ends where bash ends it; read as body, it would be hidden.
M = "bin/*"
# bash's terminator is found, so the marker after it is a command: DENY.
HEREDOC_DELIMITERS = {
    '"E\\"F"': f'cat <<"E\\"F"\nx\nE"F\n{M}',
    '"E\\\\F"': f'cat <<"E\\\\F"\nx\nE\\F\n{M}',
    '"E\\$F"': f'cat <<"E\\$F"\nx\nE$F\n{M}',
    '"E\\`F"': f'cat <<"E\\`F"\nx\nE`F\n{M}',
    'a backslash-newline in "…"': f'cat <<"E\\\nF"\nx\nEF\n{M}',
    '$"EOF"': f'cat <<$"EOF"\nx\nEOF\n{M}',
    "$'EOF'": f"cat <<$'EOF'\nx\nEOF\n{M}",
    "$'E\\tF'": f"cat <<$'E\\tF'\nx\nE\tF\n{M}",
    "$'E\\'F'": f"cat <<$'E\\'F'\nx\nE'F\n{M}",
    "$NAME stays literal": f"cat <<$EOF\nx\n$EOF\n{M}",
    "an empty delimiter": f"cat <<''\nx\n\n{M}",
}
# Where bash's reading is unsure: too complex — ASK, never allowlist-covered, always asked in v2.
HEREDOC_UNSURE = {
    "EOF) inside $(…)": f"x=$(cat <<EOF\nhi\nEOF)\n{M}",
    "a body line starting with the delimiter, then a )": f"x=$(cat <<'E'\nE{M} x)\nE\n)",
    "a $(…) that closes before its body": f"x=$(cat <<E)\nbody\n{M}\nE",
    "$(…) in the delimiter": f'cat <<"E$(echo q)F"\nx\nE$(echo q)F\n{M}',
    "${…} in the delimiter": f"cat <<${{X}}\nx\n${{X}}\n{M}",
    "a backtick in the delimiter": f"cat <<E`echo q`F\nx\nE`echo q`F\n{M}",
    "a backtick in a quoted delimiter": f'cat <<"E`echo q`F"\nx\nE`echo q`F\n{M}',
    "an unbalanced quote": f'cat <<"EOF\nx\nEOF\n{M}',
    "an unbalanced single quote": f"cat <<'EOF\nx\nEOF\n{M}",
    "an unquoted backslash-newline": f"cat <<E\\\nF\nx\nEF\n{M}",
    "an ANSI-C escape not decoded exactly": f"cat <<$'E\\x4'\nx\nE\x04\n{M}",
    "an extglob": f"cat <<E@(x)\nx\nE@(x)\n{M}",
    # Round 4 (the review's LOW): a process substitution is read as bash 5.3 reads a `$(…)`
    # (checked with `bash -n`), and backticks too, where `bash -n` cannot check.
    "EOF) inside <(…)": f"cat <(cat <<EOF\nhi\nEOF)\n{M}",
    "EOF) inside >(…)": f"tee >(cat <<EOF\nhi\nEOF)\n{M}",
    "a <(…) that closes before its body": f"cat <(cat <<E)\nbody\n{M}\nE",
    "a >(…) that closes before its body": f"tee >(cat <<E)\nbody\n{M}\nE",
    "a body line in backticks starting with the delimiter, then a )": f"x=`cat <<E\nE {M} )\nE\n`",
    # Round 4: bash takes backslashes out of backtick text before it reads a body there, and
    # drops a NUL as it reads a body line.
    "a backslash in a body line in backticks": f"x=`cat <<EOF\nhi\nEO\\\nF\n{M}\n`",
    "a backslash in a delimiter in backticks": f"x=`cat <<\\EOF\nhi\nEOF\n{M}\n`",
    "a NUL in a body line": f"cat <<'EOF'\nx\nEO\x00F\n{M}",
}
# Round 4 (LOW): a here-document in backticks that close before its body. Its lines after are
# read as commands (a marker there is DENY, `HEREDOC_RUNS`), and the line is unsure.
HEREDOC_TICK_CLOSES = {
    "backticks that close before the body": "echo `cat <<X`\nbody\nX",
    "nested in $(…)": "x=$(echo `cat <<X`\nbody\nX\n)",
    "in a sh -c string": "sh -c 'echo `cat <<X`\nbody\nX'",
}
# A body bash does not end there: its lines are data — asked by the body backstop (round 4) when
# one would be DENY read as a command, never DENY.
HEREDOC_DATA = {
    "a terminator with trailing whitespace": f"cat <<'EOF'\nx\nEOF \n{M}",
    "an empty delimiter's body": f"cat <<''\n{M}\n\nls",
    'a "E\\"F" body': f'cat <<"E\\"F"\n{M}\nE"F',
}
HEREDOC_NO_BODY = {
    "an unsure delimiter with no line after it": 'cat <<"E$(echo q)F"; cat <<E`echo q`F',
}

# Round 4 (the review's HIGH): bash 5.3 removes a backslash-newline while it reads an *unquoted*
# body, before it compares the line with the delimiter, so these bodies end where the pieces join
# into the delimiter and the marker after them runs (each checked with `bash -n` on bash 5.3: a
# syntax error on the line after shows it read as code). At 243b0cf each was ASK, not uncoverable.
BS = "\\"
HEREDOC_JOINED = {
    "EO\\ then F": f"cat <<EOF\nx\nEO{BS}\nF\n{M}",
    "E\\ O\\ F": f"cat <<EOF\nx\nE{BS}\nO{BS}\nF\n{M}",
    "EO\\ then a lone \\ then F": f"cat <<EOF\nx\nEO{BS}\n{BS}\nF\n{M}",
    "EOF\\ then an empty line": f"cat <<EOF\nx\nEOF{BS}\n\n{M}",
    "<<-: a tab, EO\\, then F": f"cat <<-EOF\nx\n\tEO{BS}\nF\n{M}",
    "<<-: tabs, E\\, then OF": f"cat <<-EOF\nx\n\t\tE{BS}\nOF\n{M}",
    "in $(…), then )": f"x=$(cat <<EOF\nhi\nEO{BS}\nF\n)\n{M}",
    "in $(…), the marker inside": f"x=$(cat <<EOF\nhi\nEO{BS}\nF\n{M}\n)",
    "in ( … ) in $(…)": f"x=$( (cat <<EOF\nhi\nEO{BS}\nF\n) )\n{M}",
    "the first of two": f"cat <<A <<B\nx\nA{BS}\n\ny\nB\n{M}",
    "an even run, then EOF": f"cat <<EOF\nx{BS}{BS}\nEOF\n{M}",
    "<<-: a tab-led delimiter, unstripped": f"cat <<-$'\\tE'\nx\n\tE\n{M}",
    '<<-: a tab-led "…" delimiter, unstripped': f'cat <<-"\tE"\nx\n\tE\n{M}',
}
# Bash does not join these (a quoted delimiter, an even run, a tab after the join point, a join
# that makes another word): the marker is body, asked by the backstop, never DENY.
HEREDOC_UNJOINED = {
    "a quoted delimiter": f"cat <<'EOF'\nx\nEO{BS}\nF\n{M}",
    "a double-quoted delimiter": f'cat <<"EOF"\nx\nEO{BS}\nF\n{M}',
    "a backslashed delimiter": f"cat <<{BS}EOF\nx\nEO{BS}\nF\n{M}",
    '$"EOF"': f'cat <<$"EOF"\nx\nEO{BS}\nF\n{M}',
    "an even run": f"cat <<EOF\nx\nEO{BS}{BS}\nF\n{M}",
    "an odd run of three makes EO\\\\F": f"cat <<EOF\nx\nEO{BS}{BS}{BS}\nF\n{M}",
    "x\\ then EOF makes xEOF": f"cat <<EOF\nx{BS}\nEOF\n{M}",
    "x\\\\\\ then EOF makes x\\\\EOF": f"cat <<EOF\nx{BS}{BS}{BS}\nEOF\n{M}",
    "<<-: EO\\ then a tab and F": f"cat <<-EOF\nx\nEO{BS}\n\tF\n{M}",
    "a CR after the backslash": f"cat <<EOF\nx\nEO{BS}\r\nF\n{M}",
}

# Round 4 (the body backstop): a marker anywhere in a body — any here-document, any quoting —
# is asked, never DENY, never allowlist-covered, always asked in v2. At 243b0cf each was ASK
# and coverable.
HEREDOC_BACKSTOP = {
    "a quoted body": f"cat > notes.txt <<'EOF'\n{M} --user\nEOF",
    "an unquoted body": f"cat > notes.txt <<EOF\nrun {M} later\n{M}\nEOF",
    "a <<- body": f"cat <<-EOF\n\t{M}\n\tEOF",
    'a $"…" body': f'cat <<$"EOF"\n{M}\nEOF',
    "a body in $(…)": f"git commit -m \"$(cat <<'EOF'\nNotes\n\n{M}\nEOF\n)\"",
    "a body in a sh -c string": f"sh -c 'cat <<E\n{M}\nE'",
    "an argument glob that names it": "cat <<'EOF'\nls ve?com\nEOF",
    "a glob only its continuation makes": f"cat <<'EOF'\nls ve{BS}\n?com\nEOF",
    "a body with no terminator": f"cat <<'EOF'\nfirst\n{R3}",
    "a body bash ends at a later line": f"cat <<'EOF'\nx\nEOF \n{M}\nEOF",
}
# The round 4 delta review: a body line that is only unsure read alone, yet ALLOW by every general
# rule, marks the body too — hidden by a misread body end, nothing else would ask it, so an
# allowlist entry or a human-backed v1 surface would run it. At 0f947ba each was ASK and coverable.
HEREDOC_UNSURE_ALLOW_LINES = {
    "an ALLOW stem with a glob past the caps": "ls " + "[" * 70,
}
# Unsure alone but ASK by the general rules: the general ASK already asks, so nothing is marked.
# A runner handed an expansion as its program was ALLOW by the general rules until `decide`
# read what runners start (2026-10-12, `rules._judge_started`): now it asks there too.
HEREDOC_UNSURE_ASK_LINES = {
    "an expansion as the program": "$X --user",
    "a code span as the program": "`kP` is now 0.8",
    "an unknown program with a glob past the caps": "./" + "a*" * 70,
    "npx with an option its table does not know": "npx --frobnicate $X thing",
    "uv run, the same": "uv run --frobnicate $X thing",
    "poetry run, the same": "poetry run --frobnicate $TOOL build",
}
# DENY alone: marked, as since round 4.
HEREDOC_DENY_LINES = {"an argument glob that names it": "ls ve?com", "the bin/* marker": M}


def in_a_body(line):
    return f"cat > notes.txt <<'EOF'\n{line}\nEOF"


# Plain bodies: nothing in them would be DENY read as a command, so nothing new is asked. A line
# led by a code span (`` `kP` is … ``) is only *unsure* read alone — its program is an expansion —
# and the backstop does not ask for that (most pull-request bodies hold one).
HEREDOC_PLAIN = {
    "a commit message": "git commit -m \"$(cat <<'EOF'\nFix the drivetrain PID\n\n- raise `kP`\n- clamp output (to 127)\n`kP` is now 0.8\nEOF\n)\"",
    "gh pr create": "gh pr create --title x --body \"$(cat <<'EOF'\n## Summary\n- one\n\n| a | b |\n|---|---|\nEOF\n)\"",
    "a markdown file": "cat > notes.md <<'EOF'\n# Notes\n- one\n- two\nEOF",
    "python on stdin": "python3 - <<'EOF'\nprint(1 + 2)\nEOF",
}


class HereDocuments(Base):
    """Round 3: a here-document's body is read only where a shell is fed it."""

    def setUp(self):
        super().setUp()
        self.ran = []
        real = shell._run
        shell._run = lambda command, *a, **k: self.ran.append(command) or "[exit 0]"
        self.addCleanup(setattr, shell, "_run", real)
        self.addCleanup(v1_permissions.set_mode, "ask")
        bound = runtime.tools_bound({"run_command"})     # where an agent's turn would stand
        bound.__enter__()
        self.addCleanup(bound.__exit__, None, None, None)

    def assert_backstop(self, command):
        """Asked by the body backstop: ASK, never DENY, never covered, approvable."""
        verdict = rules.decide(command)
        self.assertEqual(verdict.decision, rules.ASK, command)
        self.assertIn("here-document's body", verdict.reason, command)
        self.assertEqual(v1_permissions.static_verdict(command).decision, rules.ASK)
        self.assertIsNone(v2_permissions.denied_command(command))
        self.assertIn("here-document's body", rules.vex_uncoverable(command) or "", command)

    def test_ordinary_here_documents_are_never_refused(self):
        approve = v1_permissions.gate(lambda tool, args: True)
        for label, command in HEREDOC_ORDINARY.items():
            with self.subTest(label):
                self.assert_backstop(command)
                tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}),
                               approve)
        self.assertEqual(sorted(self.ran), sorted(HEREDOC_ORDINARY.values()), "a yes runs each")

    def test_plain_here_documents_ask_nothing_new(self):
        auto = Asker()
        permit = v2_permissions.build_permit(context(self.root, PP.AUTO), auto)
        for label, command in HEREDOC_PLAIN.items():
            with self.subTest(label):
                self.assertEqual(rules.decide(command).decision, rules.ASK, command)
                self.assertIsNone(rules.vex_uncoverable(command), command)
                self.assertIsNone(v2_permissions.denied_command(command))
                self.assertNotEqual(permit("Bash", {"command": command}), Decision.DENY)
        self.assertEqual(auto.seen, [], "no card under AUTO")

    def test_what_a_body_feeds_or_hides_is_still_read(self):
        for label, command in HEREDOC_RUNS.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.DENY, command)
                self.assertIn("vexcom", verdict.reason)
                self.assertIsNotNone(v2_permissions.denied_command(command))

    def test_v2_never_denies_a_commit_message(self):
        asker = Asker()
        for profile in (PP.AUTO, PP.ASK):
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label in ("a commit message", "a C doc comment", "crontab"):
                with self.subTest(profile=profile.value, shape=label):
                    self.assertNotEqual(permit("Bash", {"command": HEREDOC_ORDINARY[label]}),
                                        Decision.DENY)
        # Under AUTO a `* bullet` message is asked (the body backstop, round 4); a plain one is
        # the provider's reviewer's, as before (`test_plain_here_documents_ask_nothing_new`).
        auto = Asker()
        permit = v2_permissions.build_permit(context(self.root, PP.AUTO), auto)
        self.assertEqual(permit("Bash", {"command": HEREDOC_ORDINARY["a commit message"]}),
                         Decision.ALLOW)
        self.assertEqual(len(auto.seen), 1)

    def test_a_delimiter_is_read_as_bash_reads_it(self):
        for label, command in HEREDOC_DELIMITERS.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.DENY, command)
                self.assertIn("`bin/*`", verdict.reason)
                self.assertEqual(v1_permissions.static_verdict(command).decision, rules.DENY)
                self.assertIsNotNone(v2_permissions.denied_command(command))

    def test_an_unsure_delimiter_is_too_complex(self):
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args["command"]) or False)
        for label, command in HEREDOC_UNSURE.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.ASK, command)
                self.assertEqual(verdict.reason, rules.TOO_COMPLEX)
                self.assertEqual(v1_permissions.static_verdict(command).decision, rules.ASK)
                self.assertEqual(rules.vex_uncoverable(command), rules.TOO_COMPLEX)
                self.assertIsNone(v2_permissions.denied_command(command))
                stems = {w for pair in rules.command_targets(command) for w in pair}
                stems |= {"cat", "x"}       # an entry for every program the line names
                Path(config.ALLOWLIST_PATH).write_text(json.dumps(
                    [{"tool": "run_command", "prefix": p} for p in stems]))
                self.assertFalse(v1_permissions.allows("run_command", {"command": command}),
                                 "an allowlist entry covered it")
                tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}),
                               gate)
        self.assertEqual(sorted(asked), sorted(HEREDOC_UNSURE.values()), "v1 asked about each")
        self.assertEqual(self.ran, [], "a no ran nothing")
        for profile in (PP.AUTO, PP.ASK):
            asker = Asker(Decision.DENY)
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label, command in HEREDOC_UNSURE.items():
                with self.subTest(profile=profile.value, shape=label):
                    self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
            self.assertEqual(len(asker.seen), len(HEREDOC_UNSURE), profile)

    def test_a_body_bash_does_not_end_stays_data(self):
        for label, command in HEREDOC_DATA.items():
            with self.subTest(label):
                self.assert_backstop(command)       # data, so never DENY; asked (round 4)
        for label, command in HEREDOC_NO_BODY.items():
            with self.subTest(label):
                self.assertNotEqual(rules.decide(command).decision, rules.DENY, command)
                self.assertIsNone(rules.vex_uncoverable(command), command)

    def test_an_unquoted_body_ends_where_its_continuations_join_the_delimiter(self):
        for label, command in HEREDOC_JOINED.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.DENY, command)
                self.assertIn("`bin/*`", verdict.reason)
                self.assertEqual(v1_permissions.static_verdict(command).decision, rules.DENY)
                self.assertIsNotNone(v2_permissions.denied_command(command))
        for label, command in HEREDOC_UNJOINED.items():
            with self.subTest(label):
                self.assert_backstop(command)

    def test_a_marker_in_any_body_is_asked_never_denied_never_covered(self):
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args["command"]) or False)
        for label, command in HEREDOC_BACKSTOP.items():
            with self.subTest(label):
                self.assert_backstop(command)
                stems = {w for pair in rules.command_targets(command) for w in pair}
                stems |= {"cat", "git", "sh", "ls", "bin/*", "*"}
                Path(config.ALLOWLIST_PATH).write_text(json.dumps(
                    [{"tool": "run_command", "prefix": p} for p in stems]))
                self.assertFalse(v1_permissions.allows("run_command", {"command": command}),
                                 "an allowlist entry covered it")
                tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}),
                               gate)
        self.assertEqual(sorted(asked), sorted(HEREDOC_BACKSTOP.values()), "v1 asked about each")
        self.assertEqual(self.ran, [], "a no ran nothing")
        for profile in (PP.AUTO, PP.ASK):
            asker = Asker(Decision.DENY)
            permit = v2_permissions.build_permit(context(self.root, profile), asker)
            for label, command in HEREDOC_BACKSTOP.items():
                with self.subTest(profile=profile.value, shape=label):
                    self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
            self.assertEqual(len(asker.seen), len(HEREDOC_BACKSTOP), f"v2 asked ({profile})")

    @staticmethod
    def alone(line):
        """(the vex reading of a body line read alone, `decide`'s with the vex reading off)."""
        with patch.object(rules, "_vex_line", lambda command: rules.VexLine()):
            general = rules.decide(line).decision
        return rules._vex_line_uncached(line, backstop=False), general

    def test_an_unsure_line_the_general_rules_allow_marks_the_body(self):
        asked = []
        gate = v1_permissions.gate(lambda tool, args: asked.append(args["command"]) or False)
        commands = []
        for label, line in HEREDOC_UNSURE_ALLOW_LINES.items():
            with self.subTest(label):
                reading, general = self.alone(line)
                self.assertIsNone(reading.deny, line)
                self.assertIsNotNone(reading.unsure, line)
                self.assertEqual(general, rules.ALLOW, line)
                command = in_a_body(line)
                commands.append(command)
                self.assert_backstop(command)
                Path(config.ALLOWLIST_PATH).write_text(json.dumps(
                    [{"tool": "run_command", "prefix": p}
                     for p in ("cat", "npx", "uv", "poetry", "ls", "$X", "$TOOL", "*")]))
                self.assertFalse(v1_permissions.allows("run_command", {"command": command}),
                                 "an allowlist entry covered it")
                tools.dispatch("run_command", json.dumps({"command": command, "reason": "probe"}),
                               gate)
        self.assertEqual(sorted(asked), sorted(commands), "v1 asked about each")
        self.assertEqual(self.ran, [], "a no ran nothing")
        asker = Asker(Decision.DENY)
        permit = v2_permissions.build_permit(context(self.root, PP.AUTO), asker)
        for command in commands:
            self.assertEqual(permit("Bash", {"command": command}), Decision.DENY)
        self.assertEqual(len(asker.seen), len(commands), "v2 asked under AUTO")

    def test_an_unsure_line_the_general_rules_ask_marks_nothing(self):
        for label, line in HEREDOC_UNSURE_ASK_LINES.items():
            with self.subTest(label):
                reading, general = self.alone(line)
                self.assertIsNone(reading.deny, line)
                self.assertIsNotNone(reading.unsure, line)
                self.assertEqual(general, rules.ASK, line)
                command = in_a_body(line)
                self.assertEqual(rules.decide(command).decision, rules.ASK, command)
                self.assertIsNone(rules.vex_uncoverable(command), command)

    def test_a_deny_line_still_marks_the_body(self):
        for label, line in HEREDOC_DENY_LINES.items():
            with self.subTest(label):
                reading, _ = self.alone(line)
                self.assertIsNotNone(reading.deny, line)
                self.assert_backstop(in_a_body(line))

    def test_a_marker_on_a_command_line_after_the_body_is_still_deny(self):
        for label, command in {"after a quoted body": f"cat <<'EOF'\n{M}\nEOF\n{M}",
                               "after an unquoted one": f"cat <<EOF\nok\nEOF\n{R3}",
                               "on the operator's line": f"cat <<'EOF'; {M}\n{M}\nEOF"}.items():
            with self.subTest(label):
                self.assertEqual(rules.decide(command).decision, rules.DENY, command)
                self.assertIsNotNone(v2_permissions.denied_command(command))

    def test_backticks_that_close_before_the_body_are_unsure(self):
        for label, command in HEREDOC_TICK_CLOSES.items():
            with self.subTest(label):
                verdict = rules.decide(command)
                self.assertEqual(verdict.decision, rules.ASK, command)
                self.assertEqual(verdict.reason, rules.TOO_COMPLEX)
                self.assertEqual(rules.vex_uncoverable(command), rules.TOO_COMPLEX)

    def test_the_body_backstop_is_judged_fast(self):
        floods = {
            "2000 distinct glob lines": "cat <<'EOF'\n" + "\n".join(
                f"ls {i} " + "[" * 60 for i in range(2000)) + "\nEOF",
            "a 400 KB body line": "cat <<'EOF'\n" + "a " * 200000 + "\nEOF",
            "50,000 continuations": "cat <<EOF\n" + ("ab" + BS + "\n") * 50000 + "EOF\nls",
            "3200 distinct prose lines": "cat > n.md <<'EOF'\n" + "\n".join(
                f"- note {i}: `x{i}` is {i} * 2 for $HOME/a{i}" for i in range(3200)) + "\nEOF"}
        for label, command in floods.items():
            with self.subTest(label):
                started = time.perf_counter()
                self.assertNotEqual(rules.decide(command).decision, rules.DENY)
                rules.vex_uncoverable(command)
                self.assertLess(time.perf_counter() - started, 1.5, label)

    def test_a_large_here_document_is_not_too_complex(self):
        body = "\n".join(f"int *p{i} = a{i} * b[{i}] /* [{i}] */;" for i in range(3000))
        command = f"cat > big.c <<'EOF'\n{body}\nEOF"
        started = time.perf_counter()
        self.assertEqual(rules.decide(command).decision, rules.ASK)
        self.assertIsNone(rules.vex_uncoverable(command))
        self.assertLess(time.perf_counter() - started, 1.5)


# -- setup ------------------------------------------------------------------------------------------------

class Setup(Base):
    def test_it_refuses_without_a_terminal(self):
        # Answers that would succeed: only the TTY check can stop this.
        Path(config.VEX_PATH).unlink()
        for tty in (False, None):                  # None: judged from the real stdin
            with self.subTest(tty=tty), patch("sys.stdin", io.StringIO("")):
                answers, said = iter(["", "y", "y"]), []
                self.assertEqual(vex_setup.run(ask=lambda q: next(answers), out=said.append,
                                               tty=tty), 1)
                self.assertIn("at a terminal", " ".join(said))
                self.assertFalse(Path(config.VEX_PATH).exists())
                self.assertFalse(Path(config.VEXBUILD_PATH).exists())

    def test_it_writes_what_it_found_and_offers_vexbuild(self):
        Path(config.VEX_PATH).unlink()
        answers = iter(["", "y", "y"])
        said = []
        self.assertEqual(vex_setup.run(ask=lambda q: next(answers), out=said.append, tty=True), 0)
        written = json.loads(Path(config.VEX_PATH).read_text())
        self.assertEqual(written["vexcom"], str(self.world.vexcom))
        self.assertEqual(written["make"], str(self.world.exes["make"]))
        self.assertEqual(written["project_folder"], str(self.world.robotics))
        self.assertEqual(written["sha256"], self.world.fingerprints())
        self.assertEqual(stat.S_IMODE(os.stat(config.VEX_PATH).st_mode), 0o600)
        vex.load_config()                                         # passes the daemon's own rule
        wrapper = Path(config.VEXBUILD_PATH)
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertRegex(wrapper.read_text(), r" -I -m jarvis\.v2\.vex build \"\$@\"\n$")
        self.assertNotRegex(wrapper.read_text(), r"--write|--user|--slot")
        self.assertEqual(rules.decide("vexbuild").decision, rules.ASK)

    def test_the_wrapper_never_runs_a_package_planted_in_the_project(self):
        # M2: `python -m` puts the working folder first on sys.path, and a
        # project folder is agent-writable. The wrapper's own exec line, with
        # only its module swapped for one that exists solely in the project
        # folder, must not find it — from the folder, or through PYTHONPATH.
        import subprocess
        folder = self.world.project()
        planted = folder / "vexplanted"
        planted.mkdir()
        (planted / "__init__.py").write_text("")
        flag = self.root / "PLANTED-RAN"
        (planted / "__main__.py").write_text(f"open({str(flag)!r}, 'w').write('ran')\n")
        text = vex_setup.wrapper_text(sys.executable).replace("jarvis.v2.vex build", "vexplanted")
        script = self.root / "wrapper-probe.sh"
        script.write_text(text)
        script.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.world.home), "PYTHONPATH": str(folder)}
        proc = subprocess.run([str(script)], cwd=folder, env=env, capture_output=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(flag.exists(), "a module planted in the project folder ran")

    def test_no_writes_nothing(self):
        Path(config.VEX_PATH).unlink()
        answers = iter(["", "n"])
        self.assertEqual(vex_setup.run(ask=lambda q: next(answers), out=lambda s: None, tty=True), 1)
        self.assertFalse(Path(config.VEX_PATH).exists())
        self.assertFalse(Path(config.VEXBUILD_PATH).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
