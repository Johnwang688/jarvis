"""VEX V5 in the HUD: the backend (VX-1; editor plan §2.9, decisions V-1 to V-5).

**What it runs, and from where.** The Windows `vexcom.exe` and C++ toolchain
(`clang.exe`, `arm-none-eabi-{ar,ld,objcopy,size}.exe`, `make.exe`) that the
owner's VS Code VEX extension already downloaded, **run in place through WSL
interop** (V-1, V-5). Nothing is copied, bundled or committed: VEX's licence
forbids redistribution and decompiling, so the only `vexcom` flags used here
are the ones DishPy's MIT source uses (`--name --slot --write --progress` for
a download, `--user` for the brain's user port). Which files those are is
`config.VEX_PATH` (`vex.json`), written only by the human-run `jarvis vex
setup` (`vex_setup.py`) and refused to every agent write tool
(`tools/files._protected_state`). **Every path in it is re-checked at each
use** (`load_config`): absolute, no symlink anywhere in it, a regular
executable file or a folder, and inside the extension's own folders
(`config.VEX_STORAGE_ROOTS`, `config.VEX_EXTENSION_ROOTS`) — otherwise the
action is refused with "run `jarvis vex setup` again". Those folders are
under AppData, which agents *can* write, so setup also records **a SHA-256 of
every tool** and each one is hashed again immediately before every spawn
(`verify_tools`): a tool rewritten since setup is refused. What remains is
stated there.

**A request never names an executable, a path to run or an argument** (the
`DESKTOP_APPS` pattern). The argv of every program is built here, from the
tables below; the project folder comes from a project id (as the terminals
resolve theirs), never from a path string; a build takes only `{action}`, a
download only `{slot, name}`, both validated before anything runs.

**Physical actions are the owner's clicks.** Download to the robot and the
brain terminal exist only as owner-only HUD routes; there is no tool, MCP
tool, CLI or Discord verb for them, and an agent command line that names
`vexcom` is DENY in `rules.py` (V-2). There is no "run after download" (V-3).
Agents may *build*, through `vexbuild` (this module's `cli`, `build` only):
it asks like any command unless the owner allowlists exactly it, and it
cannot reach the device.

**Programs run as program terminals** (`terminals.create_program`, called
from nowhere else): their output lives in the terminal ring and the owner's
window only — never on the bus, in a log or on disk. The daemon log gets
lifecycle lines only (started, ended, the exit code, the slot, the project by
name), and the bus one id-only record per device action, `vex_device
{action, at}`. One build per project at a time, under a lock file `vexbuild` shares;
one device action at a time (a download, a device-info read, or the brain
terminal's user port); a 5-minute build timeout.

**Not a boundary — the residual gap, stated.** A project's makefile is
project content and runs under `make`; a build refuses a project whose make
files name `vexcom`, failing closed where it cannot read them all, but a
makefile that assembles the name at run time is not caught. Nor is the owner's
button a boundary: everything here runs as the owner, so an agent can still
reach a plugged-in brain through a script it writes and runs, `make -f` on a
makefile of its own, or a POST to the download route with a forged Origin.
The `vexcom` DENY stops the obvious spellings; the `vex_device` record makes
a device action nobody clicked visible. Closing the gap needs the device to
belong to a user the agent's processes are not.

**Spike items** (the owner runs them once; the plan's §2.9): the build's
`P=`/`T=` against VS Code's own build log, whether stopping the Linux side of
an interop process ends `make.exe` on Windows, VEX's Python download flag
(Python download is refused until then), and the device-info flag from
`vexcom --help` (`INFO_FLAG`, None until recorded: the route says so).
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
import datetime
import fcntl
import hashlib
import io
import itertools
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile

from jarvis import config, rules, untrusted

LOG = logging.getLogger(__name__)

CONFIG_CAP = 64 * 1024
SETTINGS_REL = (".vscode", "vex_project_settings.json")
SETTINGS_CAP = 64 * 1024
MAKEFILE_CAP = 256 * 1024
MAKEFILE_LIMIT = 32
SOURCE_LIMIT = 10_000
BUILD_TIMEOUT_S = 300.0
DOWNLOAD_TIMEOUT_S = 180.0
INFO_TIMEOUT_S = 10.0
KILL_GRACE_S = 2.0
RECORDS_KEPT = 32
ERRORS_KEPT = 200
INFO_TEXT_CAP = 16 * 1024
ACTIONS = ("build", "rebuild", "clean")
SLOTS = range(1, 9)
# The name a program gets on the brain (plan §2.9): never starts with `-`, so
# it can never read as a flag, and has no character a Windows command line
# could misread. The spike confirms the brain's own length limit.
DOWNLOAD_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,19}", re.ASCII)
# A project name that reaches `make` as `P=` and cmd.exe recipes as $(PROJECT).
PROJECT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", re.ASCII)
# The device-info flag comes from `vexcom --help`, which the owner runs once
# (spike); until it is recorded here, the route answers that it waits on it.
INFO_FLAG: str | None = None
TEMPLATE_FILE = re.compile(r"(cpp|py)_v5_vsc_([a-z0-9_]+)\.zip", re.ASCII)
TEMPLATE_ENTRIES = 200
TEMPLATE_ENTRY_CAP = 1024 * 1024
TEMPLATE_TOTAL_CAP = 10 * 1024 * 1024
TEMPLATE_ZIP_CAP = 32 * 1024 * 1024
ORIGIN_NEW = "HUD: new VEX project"
SETUP_HINT = "run `jarvis vex setup` again"

EXE_KEYS = ("vexcom", "clang", "ar", "ld", "objcopy", "size", "make")
# What `make.exe` itself runs during a build: hashed with it before each build.
BUILD_TOOLS = ("clang", "ar", "ld", "objcopy", "size")
STORAGE_DIR_KEYS = ("cpp_sdk", "python_sdk")
OPTIONAL_KEYS = ("python_sdk", "templates", "project_folder", "extension_version",
                 "vexcom_version")
CONFIG_KEYS = frozenset({"version", *EXE_KEYS, *STORAGE_DIR_KEYS, "templates", "project_folder",
                         "extension_version", "vexcom_version", "sha256"})
TOOL_CAP = 1 << 30                 # no VEX tool is a gigabyte; a bigger "tool" is refused
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EXTENSION_DIR = re.compile(r"vexrobotics\.vexcode-[0-9][0-9.]*", re.ASCII)
_VERSION_TEXT = re.compile(r"[0-9][0-9.]{0,31}", re.ASCII)
# Characters a Windows command line, or make's cmd.exe recipes, could read as
# syntax: never in a path handed to a Windows program.
_CMD_UNSAFE = frozenset('"<>|?*%^&!();,=\\')
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)
_ID = re.compile(r"[0-9a-f]{8}")


class VexError(Exception):
    """An action that cannot run, in a sentence (HTTP 409)."""


# -- configuration ----------------------------------------------------------------------

@dataclass(frozen=True)
class VexConfig:
    vexcom: str
    clang: str
    ar: str
    ld: str
    objcopy: str
    size: str
    make: str
    cpp_sdk: str
    python_sdk: str | None = None
    templates: str | None = None
    project_folder: str = ""
    extension_version: str | None = None
    vexcom_version: str | None = None
    # (key, sha256) for every executable, recorded by `jarvis vex setup` and
    # checked immediately before each spawn (`verify_tools`).
    sha256: tuple = ()

    def digest(self, key: str) -> str | None:
        return dict(self.sha256).get(key)

    @property
    def cpp_sdk_version(self) -> str:
        return os.path.basename(self.cpp_sdk)

    @property
    def python_sdk_version(self) -> str | None:
        return os.path.basename(self.python_sdk) if self.python_sdk else None


def _roots(values) -> list[Path]:
    """Each configured root as an absolute, symlink-free path."""
    out = []
    for value in values:
        text = str(value or "").strip()
        if text and os.path.isabs(text):
            out.append(Path(os.path.realpath(os.path.normpath(text))))
    return out


def storage_roots() -> list[Path]:
    return _roots(config.VEX_STORAGE_ROOTS)


def extension_roots() -> list[Path]:
    return _roots(config.VEX_EXTENSION_ROOTS)


def _unseen(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 or untrusted.unseen(ch) for ch in text)


def check_path(value, *, key: str, kind: str = "file", where: str = "storage") -> str:
    """The rule every `vex.json` path passes at every use, or VexError.

    Absolute, already normal, **no symlink anywhere in it** (its realpath is
    itself), a regular executable file (or a folder), and strictly inside one
    of the extension's folders: its downloads (`storage`) or its install
    (`extension`, under a `vexrobotics.vexcode-<version>` folder)."""
    if not isinstance(value, str) or not value:
        raise VexError(f"vex.json has no {key}; {SETUP_HINT}")
    if _unseen(value) or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise VexError(f"vex.json's {key} is not an absolute, plain path; {SETUP_HINT}")
    if os.path.realpath(value) != value:
        raise VexError(f"vex.json's {key} is or runs through a symlink, which Jarvis never "
                       f"follows here; {SETUP_HINT}")
    try:
        info = os.lstat(value)
    except OSError:
        raise VexError(f"vex.json's {key} ({value}) is missing — the VEX extension may have "
                       f"updated; {SETUP_HINT}") from None
    if kind == "file":
        if not stat.S_ISREG(info.st_mode) or not os.access(value, os.X_OK):
            raise VexError(f"vex.json's {key} is not an executable file; {SETUP_HINT}")
    elif not stat.S_ISDIR(info.st_mode):
        raise VexError(f"vex.json's {key} is not a folder; {SETUP_HINT}")
    path = Path(value)
    roots = storage_roots() if where == "storage" else extension_roots()
    for root in roots:
        if path != root and path.is_relative_to(root):
            if where == "extension" and not _EXTENSION_DIR.fullmatch(path.relative_to(root).parts[0]):
                continue
            return value
    named = "VS Code's or Cursor's VEX extension folders"
    raise VexError(f"vex.json's {key} is outside {named}; {SETUP_HINT}")


def _read_config_bytes(path: Path) -> bytes:
    """vex.json, never followed through a symlink, never a FIFO or a device,
    never read past its cap (the PR #20 catalog loader's rule)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise VexError("VEX is not set up: run `jarvis vex setup` in a terminal") from None
    except OSError:
        raise VexError(f"vex.json cannot be opened (a symlink is refused); {SETUP_HINT}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise VexError(f"vex.json is not a regular file; {SETUP_HINT}")
        data = os.read(fd, CONFIG_CAP + 1)
    finally:
        os.close(fd)
    if len(data) > CONFIG_CAP:
        raise VexError(f"vex.json is larger than {CONFIG_CAP} bytes; {SETUP_HINT}")
    return data


def parse_config(doc) -> VexConfig:
    """A `vex.json` document, every path checked. Unknown keys are refused:
    a file this does not recognise was not written by setup."""
    if not isinstance(doc, dict):
        raise VexError(f"vex.json is not a JSON object; {SETUP_HINT}")
    unknown = set(doc) - CONFIG_KEYS
    if unknown:
        raise VexError(f"vex.json has keys setup never writes ({', '.join(sorted(unknown))}); "
                       f"{SETUP_HINT}")
    values = {}
    for key in EXE_KEYS:
        values[key] = check_path(doc.get(key), key=key, kind="file", where="storage")
    values["cpp_sdk"] = check_path(doc.get("cpp_sdk"), key="cpp_sdk", kind="dir", where="storage")
    if doc.get("python_sdk") is not None:
        values["python_sdk"] = check_path(doc["python_sdk"], key="python_sdk", kind="dir",
                                          where="storage")
    if doc.get("templates") is not None:
        values["templates"] = check_path(doc["templates"], key="templates", kind="dir",
                                         where="extension")
    folder = doc.get("project_folder", config.VEX_PROJECT_FOLDER)
    if not isinstance(folder, str) or _unseen(folder) or not os.path.isabs(folder) \
            or os.path.normpath(folder) != folder:
        raise VexError(f"vex.json's project_folder is not an absolute, plain path; {SETUP_HINT}")
    values["project_folder"] = folder
    for key in ("extension_version", "vexcom_version"):
        text = doc.get(key)
        if text is not None:
            if not isinstance(text, str) or not _VERSION_TEXT.fullmatch(text):
                raise VexError(f"vex.json's {key} is not a version; {SETUP_HINT}")
            values[key] = text
    hashes = doc.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(EXE_KEYS) \
            or not all(isinstance(v, str) and _HEX64.fullmatch(v) for v in hashes.values()):
        raise VexError(f"vex.json has no fingerprint for each VEX tool; {SETUP_HINT}")
    values["sha256"] = tuple(sorted(hashes.items()))
    return VexConfig(**values)


# -- the tools are the ones setup saw ----------------------------------------------------------

def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_sha256(path: str) -> tuple[str, tuple]:
    """(hex digest, identity) of the file at `path`, read through one fd that
    was opened without following a symlink, never a FIFO or a device, capped.
    The identity is that fd's `fstat`, so a caller can tell whether the path
    still names the file that was hashed."""
    import hashlib
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise VexError(f"{os.path.basename(path)} cannot be opened; {SETUP_HINT}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > TOOL_CAP:
            raise VexError(f"{os.path.basename(path)} is not a regular file; {SETUP_HINT}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), _identity(info)


TOOLS_CHANGED = ("the VEX tools changed since `jarvis vex setup` ({key}); run it again")


def verify_tools(cfg: VexConfig, program: str, also: tuple[str, ...] = ()) -> None:
    """Every named tool must hash to what setup recorded, immediately before
    `program` (the executable about to be spawned) runs; then `program`'s
    path must still name the very file that was hashed (same device, inode,
    size, modification and change times). A mismatch refuses.

    **What remains, stated:** a file swapped in the instant between that last
    `stat` and the kernel's `exec` is not seen, and the tools `make.exe` runs
    (`clang.exe`, the `arm-none-eabi-*`) are hashed here but opened by Windows
    later, during the build. A program running as the owner could win either
    race; this check turns the easy case — an agent's `cp` over a tool between
    builds — into a refusal."""
    for key in (*also, program):
        digest, identity = file_sha256(getattr(cfg, key))
        if digest != cfg.digest(key):
            raise VexError(TOOLS_CHANGED.format(key=key))
        if key == program:
            try:
                now = _identity(os.stat(getattr(cfg, key), follow_symlinks=False))
            except OSError:
                now = None
            if now != identity:
                raise VexError(TOOLS_CHANGED.format(key=key))


def load_config() -> VexConfig:
    """`vex.json`, re-read and every path re-checked — called at each use."""
    data = _read_config_bytes(Path(config.VEX_PATH))
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VexError(f"vex.json is not valid JSON; {SETUP_HINT}") from None
    return parse_config(doc)


# -- Windows paths -------------------------------------------------------------------------

def _mount() -> Path:
    return Path(os.path.normpath(str(config.VEX_MOUNT_ROOT)))


def on_windows_drive(path) -> bool:
    try:
        rel = Path(os.path.normpath(str(path))).relative_to(_mount())
    except ValueError:
        return False
    return bool(rel.parts) and re.fullmatch(r"[A-Za-z]", rel.parts[0]) is not None


def to_windows(path, *, spaces: bool = True) -> str:
    """`/mnt/c/Users/x` → `C:\\Users\\x` — the one function every argument a
    Windows program receives goes through. A path not on a Windows drive, or
    holding a character a Windows command line (or make's cmd.exe recipes)
    could misread — anything outside printable ASCII included — is refused
    rather than quoted around."""
    text = str(path)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise VexError(f"{text} is not an absolute, plain path")
    if not on_windows_drive(text):
        raise VexError(f"{text} is not on a Windows drive ({_mount()}/<drive>/…); VEX's "
                       "Windows toolchain works only there")
    rel = Path(text).relative_to(_mount()).parts
    for part in rel[1:]:
        # ASCII only: a program that takes its command line in the ANSI code
        # page gets Windows' "best fit" for anything else, and that maps
        # U+FF02 to `"`, U+FF3C to `\\` and U+2215 to `/` — syntax again.
        if not part.isascii() or _unseen(part) or set(part) & _CMD_UNSAFE or ":" in part:
            raise VexError(f"{text} holds a character a Windows command line could misread")
        if part.endswith((".", " ")) or _WINDOWS_RESERVED.match(part) \
                or _WINDOWS_RESERVED.match(part.split(".")[0]):
            raise VexError(f"{text} holds a name Windows cannot hold as written ({part[:40]!r})")
        if not spaces and " " in part:
            raise VexError(f"{text} holds a space, which make would split")
    return rel[0].upper() + ":\\" + "\\".join(rel[1:])


# -- detection ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Detected:
    name: str
    language: str
    slot: int
    sdk_version: str
    printf_float: bool = False
    python_main: str = ""

    def view(self, root) -> dict:
        drive = on_windows_drive(root)
        return {"vex": True, "name": self.name, "language": self.language, "slot": self.slot,
                "sdk_version": self.sdk_version, "printf_float": self.printf_float,
                "python_main": self.python_main or None, "on_windows_drive": drive,
                "builds": self.language == "cpp" and drive}


class NotVex(VexError):
    """The project is not a VEX V5 project this can build (the sentence says why)."""


def _no_symlink_below(root: Path, rel: tuple[str, ...]) -> Path:
    path = root
    for part in rel:
        path = path / part
        if os.path.islink(path):
            raise NotVex(f"{'/'.join(rel)} runs through a symlink; Jarvis does not follow it")
    return path


def read_settings(root) -> dict:
    """`.vscode/vex_project_settings.json` parsed, or NotVex. Never through a
    symlink, never past 64 KB."""
    root = Path(root)
    path = _no_symlink_below(root, SETTINGS_REL)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise NotVex("no .vscode/vex_project_settings.json here") from None
    except OSError:
        raise NotVex("the VEX settings file cannot be opened") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotVex("the VEX settings file is not a regular file")
        data = os.read(fd, SETTINGS_CAP + 1)
    finally:
        os.close(fd)
    if len(data) > SETTINGS_CAP:
        raise NotVex(f"the VEX settings file is larger than {SETTINGS_CAP // 1024} KB")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotVex("the VEX settings file is not valid JSON") from None
    if not isinstance(doc, dict):
        raise NotVex("the VEX settings file is not a JSON object")
    return doc


def parse_settings(doc: dict) -> Detected:
    """The extension's v2 shape: `extension.json` 2, platform V5, language
    cpp or python, slot 1–8. Anything else is reported, never guessed around."""
    extension, project = doc.get("extension"), doc.get("project")
    if not isinstance(extension, dict) or extension.get("json") != 2:
        raise NotVex("the VEX settings file is not the extension's version 2 format")
    if not isinstance(project, dict):
        raise NotVex("the VEX settings file has no project section")
    if project.get("platform") != "V5":
        raise NotVex(f"this is a {str(project.get('platform'))[:20]} project; only V5 is supported")
    language = project.get("language")
    if language not in ("cpp", "python"):
        raise NotVex("the project's language must be cpp or python")
    slot = project.get("slot")
    if type(slot) is not int or slot not in SLOTS:
        raise NotVex("the project's slot must be a number from 1 to 8")
    name = project.get("name")
    if not isinstance(name, str) or not name.strip() or _unseen(name) or len(name) > 200:
        raise NotVex("the project has no usable name")
    sdk = project.get("sdkVersion", "")
    if not isinstance(sdk, str) or _unseen(sdk) or len(sdk) > 100:
        raise NotVex("the project's sdkVersion is not text")
    cpp = project.get("cpp") if isinstance(project.get("cpp"), dict) else {}
    python = project.get("python") if isinstance(project.get("python"), dict) else {}
    main = python.get("main", "")
    return Detected(name=name, language=language, slot=slot, sdk_version=sdk,
                    printf_float=cpp.get("printf_float") is True,
                    python_main=main if isinstance(main, str) else "")


def detect(root) -> dict:
    """What `GET /projects/{id}/vex` says about a project folder."""
    root = Path(root)
    if os.path.lexists(root / "project.pros"):
        return {"vex": False, "pros": True, "reason": "PROS projects aren't supported"}
    if not os.path.lexists(root / ".vscode" / "vex_project_settings.json"):
        return {"vex": False, "reason": "not a VEX project (no .vscode/vex_project_settings.json)"}
    try:
        return parse_settings(read_settings(root)).view(root)
    except NotVex as exc:
        return {"vex": False, "malformed": True, "reason": str(exc)}


# -- make files ------------------------------------------------------------------------------

_INCLUDE = re.compile(r"^\s*-?s?include\s+(.+)$", re.MULTILINE)
INCLUDE_WORD_CAP = 256


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_make_file(path: Path, rel: str) -> str:
    """One make file's text — never through a symlink, never a FIFO, never
    past the cap; anything else is a refusal (the guard fails closed)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise VexError(f"{rel} cannot be opened (a symlinked make file is refused), so this "
                       "project is not built") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VexError(f"{rel} is not a regular file, so this project is not built")
        data = os.read(fd, MAKEFILE_CAP + 1)
    finally:
        os.close(fd)
    if len(data) > MAKEFILE_CAP:
        raise VexError(f"{rel} is larger than {MAKEFILE_CAP // 1024} KB, too large to check, so "
                       "this project is not built")
    return data.decode("utf-8", "replace")


def makefile_refusal(root) -> str | None:
    """Why this project's make files may not be built, or None.

    A makefile is project content that `make` runs; one that called vexcom
    would make a build a download. So a build refuses a project whose make
    files — the makefile, every `*.mk` at the top or in `vex/`, and what they
    `include` — name it (`rules.vexcom_reading`, the same accident guard as
    the DENY), and it **fails closed** wherever it cannot read all of them:
    more than `MAKEFILE_LIMIT` files, one past `MAKEFILE_CAP`, a symlink or a
    non-regular file among them, a `vex/` folder that is a symlink, a glob in
    one past the guard's limits (too complex to check), or an `include` it
    cannot resolve here — a `$(…)` in the name, a backslash, an absolute path,
    a path outside the project, or a file that is not in the project (make
    would go on to search other folders for it, `-include` and `sinclude`
    alike). Lines are read as make reads them: a backslash-newline joins the
    next line on with a space, so an `include` continued onto a second line
    includes what that line names. Not a boundary: a makefile can still
    assemble the name at run time (`$(shell …)`, `$(wildcard …)`, `$(eval
    include …)`)."""
    root = Path(root)
    queue: list[Path] = []
    for name in ("makefile", "Makefile", "GNUmakefile"):
        if os.path.lexists(root / name):
            queue.append(root / name)
    for folder in (root, root / "vex"):
        if os.path.islink(folder):
            return f"{folder.relative_to(root).as_posix() or '.'} is a symlink, so this project is not built"
        if folder.is_dir():
            queue += sorted(p for p in folder.iterdir() if p.suffix == ".mk")
    seen: set[Path] = set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        if len(seen) >= MAKEFILE_LIMIT:
            return (f"the project has more than {MAKEFILE_LIMIT} make files, too many to check, "
                    "so it is not built")
        seen.add(path)
        rel = path.relative_to(root).as_posix()
        try:
            text = _read_make_file(path, rel)
        except VexError as exc:
            return str(exc)
        reading = rules.vexcom_reading(text)
        if reading == rules.NAMES:
            return (f"{rel} names vexcom, so Jarvis will not build this project: a build must "
                    "never reach the robot. Remove it, or build in VS Code")
        if reading == rules.COMPLEX:
            return (f"{rel} holds a glob too complex to check for vexcom, so this project is "
                    "not built")
        joined = text.replace("\r\n", "\n").replace("\\\n", " ")
        for match in _INCLUDE.finditer(joined):
            for word in match.group(1).split("#", 1)[0].split():
                shown = word[:60]
                if "$" in word:
                    return (f"{rel} includes a file named by a variable ({shown}), which "
                            "cannot be checked here, so this project is not built")
                if "\\" in word or len(word) > INCLUDE_WORD_CAP:
                    return (f"{rel} includes a file this check cannot read as make does "
                            f"({shown}), so this project is not built")
                if word.startswith("/"):
                    return (f"{rel} includes a file outside the project ({shown}), so this "
                            "project is not built")
                candidates = [root / word]
                if any(c in word for c in "*?["):
                    try:
                        candidates = sorted(itertools.islice(root.glob(word), MAKEFILE_LIMIT + 1))
                    except (ValueError, OSError):
                        return (f"{rel} includes a file this check cannot read as make does "
                                f"({shown}), so this project is not built")
                    if len(candidates) > MAKEFILE_LIMIT:
                        return (f"{rel} includes more than {MAKEFILE_LIMIT} files ({shown}), too "
                                "many to check, so this project is not built")
                if not candidates:
                    return (f"{rel} includes {shown}, which is not in the project (make would "
                            "search other folders for it), so this project is not built")
                for candidate in candidates:
                    resolved = Path(os.path.normpath(candidate))
                    if not resolved.is_relative_to(root):
                        return (f"{rel} includes a file outside the project ({shown}), so "
                                "this project is not built")
                    if not os.path.lexists(resolved):
                        return (f"{rel} includes {shown}, which is not in the project (make "
                                "would search other folders for it), so this project is not "
                                "built")
                    queue.append(resolved)
    return None


# -- argv tables -----------------------------------------------------------------------------

def build_argv(cfg: VexConfig, project: Detected, action: str) -> list[str]:
    """`make.exe` with every tool as a command-line variable, which beats the
    makefile's own `CC = clang` (so nothing touches PATH): `P` the project,
    `T` the SDK folder (the one holding `vexv5/`), `PRINTF_FLOAT` when the
    settings say so. `rebuild` is make's `-B`; `clean` the makefile's own
    `clean` target."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if not PROJECT_NAME.fullmatch(project.name):
        raise VexError("the project's name must be letters, digits, _ and - (at most 64) to "
                       "build here; rename it in .vscode/vex_project_settings.json")

    def tool(key):
        return to_windows(getattr(cfg, key), spaces=False)

    argv = [cfg.make, f"P={project.name}", f"T={to_windows(cfg.cpp_sdk)}",
            f"CC={tool('clang')}", f"CXX={tool('clang')}", f"OBJCOPY={tool('objcopy')}",
            f"SIZE={tool('size')}", f"LINK={tool('ld')}", f"ARCH={tool('ar')}"]
    if project.printf_float:
        argv.append("PRINTF_FLOAT=1")
    if action == "rebuild":
        argv.append("-B")
    elif action == "clean":
        argv.append("clean")
    return argv


def check_slot(slot) -> int:
    if type(slot) is not int or slot not in SLOTS:
        raise ValueError("slot must be a whole number from 1 to 8")
    return slot


def check_name(name) -> str:
    if not isinstance(name, str) or not DOWNLOAD_NAME.fullmatch(name):
        raise ValueError("name must be 1-20 letters, digits, _ or -, not starting with -")
    return name


def download_argv(cfg: VexConfig, name: str, slot: int, binary_windows: str) -> list[str]:
    """DishPy's download flags, exactly: `--name --slot --write --progress`."""
    return [cfg.vexcom, "--name", check_name(name), "--slot", str(check_slot(slot)),
            "--write", binary_windows, "--progress"]


def brain_argv(cfg: VexConfig) -> list[str]:
    """The brain's user port, both ways (DishPy's `terminal`)."""
    return [cfg.vexcom, "--user"]


def info_argv(cfg: VexConfig) -> list[str]:
    if not INFO_FLAG:
        raise VexError("Device info waits on one step of the setup spike: run `vexcom --help` "
                       "once and record its device-info option (design §18)")
    return [cfg.vexcom, INFO_FLAG]


# -- output ----------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_DIAG = re.compile(r"^(?P<file>[^\n:]*(?::[\\/][^\n:]*)?):(?P<line>\d{1,7}):(?P<col>\d{1,5}):\s+"
                   r"(?P<sev>fatal error|error|warning):\s*(?P<msg>.*)$")


def plain(data: bytes) -> str:
    """Program output as text: escapes stripped, CRLF read as LF, and a line
    redrawn with CR (a progress bar) kept as its last state."""
    text = _ANSI.sub("", data.decode("utf-8", "replace")).replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def parse_errors(text: str, root_windows: str | None = None) -> list[dict]:
    """`file:line:col: error|warning: message` lines, the build's own output
    (not a language server). A file under the project comes back relative,
    with `/`, so the HUD can open it at its line."""
    out = []
    prefix = (root_windows.rstrip("\\") + "\\").casefold() if root_windows else None
    for line in text.split("\n"):
        match = _DIAG.match(line.strip())
        if not match:
            continue
        name = match["file"].strip()
        inside = not re.match(r"^[A-Za-z]:[\\/]|^/", name)
        if prefix and name.casefold().startswith(prefix):
            name, inside = name[len(prefix):], True
        name = name.replace("\\", "/")
        if inside and (".." in name.split("/")):
            inside = False
        out.append({"file": name[:400], "line": int(match["line"]), "col": int(match["col"]),
                    "severity": "error" if "error" in match["sev"] else "warning",
                    "message": match["msg"][:300], "in_project": inside})
        if len(out) >= ERRORS_KEPT:
            break
    return out


def device_text(data: bytes) -> str:
    text = plain(data)
    text = "".join(ch for ch in text if ch in "\n\t" or not (ord(ch) < 32 or untrusted.unseen(ch)))
    return text[-INFO_TEXT_CAP:]


# -- the binary and its freshness --------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def binary(root, name: str) -> dict | None:
    """`build/<name>.bin`: its project-relative path, size and time, or None.
    The name comes from the project's own (agent-writable) settings file, so it
    is held to the `P=` rule first: no `..`, no separator, nothing odd."""
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
        return None
    rel = f"build/{name}.bin"
    path = Path(root) / "build" / f"{name}.bin"
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or os.path.islink(Path(root) / "build"):
        return None
    return {"path": rel, "size": info.st_size, "mtime": _iso(info.st_mtime), "_ts": info.st_mtime}


def newest_source(root) -> float:
    """The newest mtime among what a build reads: `src/`, `include/`, the
    makefile and `vex/*.mk` (bounded)."""
    root = Path(root)
    newest, count = 0.0, 0
    tops = [root / "makefile", root / "Makefile"] + sorted((root / "vex").glob("*.mk"))
    for path in tops:
        if _regular(path):
            newest = max(newest, os.lstat(path).st_mtime)
    for top in (root / "src", root / "include"):
        if not top.is_dir() or top.is_symlink():
            continue
        for folder, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
            for name in files:
                count += 1
                if count > SOURCE_LIMIT:
                    return newest
                try:
                    newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
                except OSError:
                    continue
    return newest


def _public(binary_info: dict | None) -> dict | None:
    return None if binary_info is None else {k: v for k, v in binary_info.items() if k != "_ts"}


# -- the build lock (shared with vexbuild) ------------------------------------------------------

def lock_path(root) -> Path:
    key = hashlib.sha256(os.path.realpath(str(root)).encode("utf-8", "surrogateescape")).hexdigest()
    return Path(config.V2_DATA_DIR) / "vex" / f"build-{key[:20]}.lock"


class BuildLock:
    """One build per project at a time, across the daemon and `vexbuild`: an
    exclusive `flock` on a file under Jarvis's data folder, keyed by the
    project's realpath. Non-blocking: a second build is refused, not queued."""

    def __init__(self, root):
        self.path = lock_path(root)
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


# -- templates -----------------------------------------------------------------------------------

def _label(stem: str, index: dict[str, str]) -> str:
    named = index.get(stem + ".zip")
    if named:
        return named[:80]
    words = TEMPLATE_FILE.fullmatch(stem + ".zip").group(2).replace("_", " ").strip()
    return (words[:1].upper() + words[1:])[:80] or stem


def _index_names(folder: Path) -> dict[str, str]:
    """Display names for template files, from the folder's `index.json`
    (`examples[].samples[] = {name, files: [{name}]}`), where it names them.
    Opened as the templates are: never through a symlink, never a FIFO or a
    device, never read past 2 MB."""
    cap = 2 * 1024 * 1024
    try:
        fd = os.open(folder / "index.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}                   # never a FIFO (it would block) or a device
        data = os.read(fd, cap + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    if len(data) > cap:
        return {}
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    names = {}
    for category in doc.get("examples", []) if isinstance(doc, dict) else []:
        for sample in category.get("samples", []) if isinstance(category, dict) else []:
            if not isinstance(sample, dict) or not isinstance(sample.get("name"), str):
                continue
            for item in sample.get("files", []) if isinstance(sample.get("files"), list) else []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    label = " ".join(sample["name"].split())
                    if label and not _unseen(label):
                        names[item["name"]] = label
    return names


def list_templates(cfg: VexConfig) -> list[dict]:
    """`[{id, language, label}]`: the extension's VS Code templates
    (`cpp_v5_vsc_*.zip`, `py_v5_vsc_*.zip`) in the configured folder."""
    if not cfg.templates:
        raise VexError(f"vex.json names no templates folder; {SETUP_HINT}")
    folder = Path(cfg.templates)
    index = _index_names(folder)
    out = []
    for path in sorted(folder.iterdir()):
        match = TEMPLATE_FILE.fullmatch(path.name)
        if match and _regular(path):
            stem = path.name[:-4]
            out.append({"id": stem, "language": "cpp" if match.group(1) == "cpp" else "python",
                        "label": _label(stem, index)})
    out.sort(key=lambda t: (t["language"], t["label"].casefold()))
    return out


def _entry_refusal(name: str) -> str | None:
    if not name or len(name) > 1024 or _unseen(name):
        return "has an unreadable name"
    if "\\" in name or ":" in name:
        return "uses a Windows path or a drive letter"
    if name.startswith("/"):
        return "is an absolute path"
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "climbs out of its folder (`..`)"
    for part in parts:
        if part.casefold() in (".git", ".jarvis") or len(part.encode("utf-8")) > 255:
            return "names a folder a template may not write"
        if _WINDOWS_RESERVED.match(part) or _WINDOWS_RESERVED.match(part.split(".")[0]) \
                or set(part) & set('<>"|?*') or part.endswith((".", " ")):
            return "is not a name Windows can hold"
    return None


def read_template(cfg: VexConfig, template_id: str) -> list[tuple[str, bytes]]:
    """Every file of a template, checked **before anything is written**: no
    absolute paths, no `..`, no drive letters, no symlinks, at most 200
    entries, 1 MB each and 10 MB in all (read with a cap, never trusting the
    zip's own sizes). `.v5code` entries and a template's own settings file
    are skipped (Jarvis writes the settings)."""
    if not isinstance(template_id, str) or not TEMPLATE_FILE.fullmatch(template_id + ".zip"):
        raise ValueError("template must be one of GET /vex/templates' ids")
    if template_id not in {t["id"] for t in list_templates(cfg)}:
        raise ValueError(f"no template {template_id}")
    path = Path(cfg.templates) / f"{template_id}.zip"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise VexError("the template cannot be opened (a symlink is refused)") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise VexError("the template is not a regular file")
        blob = handle.read(TEMPLATE_ZIP_CAP + 1)
    if len(blob) > TEMPLATE_ZIP_CAP:
        raise VexError("the template archive is too large")
    return unpack_plan(blob)


def unpack_plan(blob: bytes) -> list[tuple[str, bytes]]:
    """The (relative path, bytes) a template archive would write, or VexError
    naming the first entry that refuses."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise VexError("the template is not a readable zip archive") from None
    if len(entries) > TEMPLATE_ENTRIES:
        raise VexError(f"the template has more than {TEMPLATE_ENTRIES} entries")
    out, names, total = [], set(), 0
    for entry in entries:
        name = entry.filename
        why = _entry_refusal(name)
        if why:
            raise VexError(f"the template entry {name[:80]!r} {why}")
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise VexError(f"the template entry {name[:80]!r} is a symlink")
        if entry.is_dir() or name.endswith("/"):
            continue
        if name.lower().endswith(".v5code") or name == "/".join(SETTINGS_REL):
            continue
        key = name.casefold()
        if key in names:
            raise VexError(f"the template has {name[:80]!r} twice")
        names.add(key)
        with archive.open(entry) as handle:
            data = handle.read(TEMPLATE_ENTRY_CAP + 1)
        if len(data) > TEMPLATE_ENTRY_CAP:
            raise VexError(f"the template entry {name[:80]!r} is larger than 1 MB")
        total += len(data)
        if total > TEMPLATE_TOTAL_CAP:
            raise VexError("the template is larger than 10 MB in all")
        out.append((name, data))
    return out


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _write_new(folder_fd: int, rel: str, data: bytes) -> None:
    """One file, created (never replaced) inside the folder `folder_fd` holds
    open: each folder on the way is made, then opened `O_DIRECTORY |
    O_NOFOLLOW` relative to the one before it (openat-style), and the file is
    created `O_EXCL | O_NOFOLLOW` relative to the last — so nothing that
    appears meanwhile, a symlink included, is written through or over."""
    parts = rel.split("/")
    held: list[int] = []
    current = folder_fd
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=current)
            except FileExistsError:
                pass
            try:
                current = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError:
                raise VexError(f"{rel} would be written through something that is not a "
                               "folder") from None
            held.append(current)
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o644, dir_fd=current)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    finally:
        for fd in held:
            os.close(fd)


def _creation_date(now: datetime.datetime) -> str:
    hour = now.hour % 12 or 12
    half = "AM" if now.hour < 12 else "PM"
    return f"{now.month}/{now.day}/{now.year}, {hour}:{now.minute:02d}:{now.second:02d} {half}"


def settings_document(name: str, language: str, cfg: VexConfig, now=None) -> dict:
    """`.vscode/vex_project_settings.json` in the extension's v2 shape (the
    schema's required fields: platform, name, description, creationDate,
    slot, language), so the project also opens in VS Code."""
    now = now or datetime.datetime.now()
    project = {"name": name, "description": "", "creationDate": _creation_date(now),
               "platform": "V5", "language": language, "slot": 1}
    if language == "cpp":
        project["sdkVersion"] = cfg.cpp_sdk_version
        project["cpp"] = {"includePath": [], "printf_float": False}
    else:
        project["sdkVersion"] = cfg.python_sdk_version or ""
        project["python"] = {"main": "src/main.py"}
    return {"extension": {"version": cfg.extension_version or "", "json": 2}, "project": project}


def unpack(entries: list[tuple[str, bytes]], folder, settings: dict) -> None:
    """Write a checked template, then its settings, into a new or empty
    folder. Jarvis's own code does this — never a tool."""
    folder = Path(folder)
    if os.path.islink(folder) or not folder.is_dir():
        raise VexError(f"{folder} is not a folder")
    # Held open from here on, so every write lands inside this very folder.
    # What remains: the folder's own path is resolved once, here, after the
    # owner's yes — a parent swapped for a symlink in that instant by a
    # program running as the owner would carry it elsewhere.
    try:
        folder_fd = os.open(folder, _DIR_FLAGS)
    except OSError:
        raise VexError(f"{folder} is not a folder") from None
    try:
        for rel, data in entries:
            _write_new(folder_fd, rel, data)
        _write_new(folder_fd, "/".join(SETTINGS_REL),
                   (json.dumps(settings, indent="\t") + "\n").encode("utf-8"))
    finally:
        os.close(folder_fd)


# -- records ------------------------------------------------------------------------------------

@dataclass
class Build:
    id: str
    project_id: str
    project: str
    action: str
    root: str
    root_windows: str
    started: str
    state: str = "running"
    terminal_id: str | None = None
    exit_code: int | None = None
    errors: list = field(default_factory=list)
    bin: dict | None = None
    ended: str | None = None
    timed_out: bool = False
    lock: BuildLock | None = None
    timer: threading.Timer | None = None
    started_ts: float = 0.0

    def view(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "action": self.action,
                "state": self.state, "terminal_id": self.terminal_id,
                "exit_code": self.exit_code, "errors": list(self.errors),
                "bin": _public(self.bin), "started": self.started, "ended": self.ended}


@dataclass
class NewProject:
    id: str
    name: str
    path: str
    state: str = "asking"
    project_id: str | None = None
    message: str = ""

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "path": self.path, "state": self.state,
                "project_id": self.project_id, "message": self.message}


def _utcnow() -> str:
    from .model import utcnow
    return utcnow()


def _api(status: int, text: str):
    from .daemon import APIError
    return APIError(status, text)


# -- the daemon's VEX state ------------------------------------------------------------------

class Vex:
    """Builds, downloads, the brain terminal and new projects for one daemon."""

    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()
        self._builds: collections.OrderedDict[str, Build] = collections.OrderedDict()
        self._news: collections.OrderedDict[str, NewProject] = collections.OrderedDict()
        # One device action at a time: {"kind": "download"|"info"|"brain", "terminal_id"}.
        self._device: dict | None = None
        self._last: dict[tuple, str] = {}
        self._timers: set[threading.Timer] = set()
        self._held: set[BuildLock] = set()
        self._stopped = False

    # -- helpers ----------------------------------------------------------------------

    def _project(self, project_id):
        from .projects import refuse_archived_project
        project = self.daemon.require(self.daemon.stores.projects, project_id)
        refuse_archived_project(project)
        if not os.path.isabs(project.root) or not Path(project.root).is_dir():
            raise VexError(f"the folder {project.root} does not exist any more")
        return project

    def _detected(self, project) -> Detected:
        root = Path(project.root)
        if os.path.lexists(root / "project.pros"):
            raise VexError("PROS projects aren't supported")
        try:
            return parse_settings(read_settings(root))
        except NotVex as exc:
            raise VexError(f"{project.name} is not a VEX V5 project Jarvis can use: {exc}") from None

    def _taken_roots(self) -> list[str]:
        from .daemon import safe_list
        return [p.root for p in safe_list(self.daemon.stores.projects)]

    def _id(self, table) -> str:
        new = secrets.token_hex(4)
        while new in table:
            new = secrets.token_hex(4)
        return new

    def _keep(self, table, key, record) -> None:
        table[key] = record
        while len(table) > RECORDS_KEPT:
            table.popitem(last=False)

    def _replace(self, key) -> None:
        """Close the previous program terminal of this kind (and project) if it
        has ended, so a new build replaces the last one's tab."""
        from .daemon import APIError
        old = self._last.pop(key, None)
        if old is None:
            return
        try:
            terminal = self.daemon.terminals.get(old)
        except APIError:
            return
        if terminal.exited:
            try:
                self.daemon.terminals.close(old, grace=0)
            except APIError:
                pass
        else:
            self._last[key] = old

    def _start(self, argv, *, cfg, tool, also=(), folder, label, program, title, project_id=None,
               input_ok=False, on_exit=None, key=None):
        """Spawn `argv` as a program terminal. `argv[0]` is `cfg.<tool>`; it and
        the tools in `also` are hashed against `vex.json` immediately before
        the spawn (`verify_tools`), and nothing else runs in between."""
        from .terminals import TerminalError
        if self._stopped:
            raise VexError("Jarvis is stopping")
        if argv[0] != getattr(cfg, tool):
            raise VexError("internal: the program is not the configured tool")
        if key is not None:
            self._replace(key)
        verify_tools(cfg, tool, also)
        try:
            terminal = self.daemon.terminals.create_program(
                argv, folder=folder, label=label, program=program, title=title,
                project_id=project_id, input_ok=input_ok, on_exit=on_exit)
        except TerminalError as exc:
            raise VexError(str(exc)) from None
        if key is not None:
            self._last[key] = terminal.id
        return terminal

    def _timer(self, seconds, action, *args) -> threading.Timer:
        timer = threading.Timer(seconds, action, args)
        timer.daemon = True
        with self._lock:
            self._timers.add(timer)
        timer.start()
        return timer

    def _stop_program(self, terminal) -> None:
        """A timeout: SIGTERM the program's session, then SIGKILL what is left.
        The terminal stays listed, so its output stays readable."""
        terminal.signal_program(signal.SIGTERM)
        until = time.monotonic() + KILL_GRACE_S
        while terminal.alive() and time.monotonic() < until:
            time.sleep(0.05)
        terminal.signal_program(signal.SIGKILL)

    def _reserve_device(self, kind: str):
        """The device, for one action. -> `(release, bind)`, which only this
        reservation's holder has: `release()` frees it at most once and never
        another holder's reservation; `bind(terminal_id)` names its terminal."""
        with self._lock:
            busy = self._device
            if busy is not None:
                what = {"brain": "The brain terminal is using the brain's port; close it first",
                        "download": "A download is running",
                        "info": "A device-info read is running"}[busy["kind"]]
                raise VexError(f"{what}. One device action at a time.")
            token = secrets.token_hex(8)
            self._device = {"kind": kind, "terminal_id": None, "token": token}

        def release() -> bool:
            with self._lock:
                if self._device is not None and self._device["token"] == token:
                    self._device = None
                    return True
            return False

        def bind(terminal_id) -> None:
            with self._lock:
                if self._device is not None and self._device["token"] == token:
                    self._device["terminal_id"] = terminal_id

        return release, bind

    def _announce(self, action: str) -> None:
        """`vex_device` on the bus — the action and the time, nothing else, as
        WP-C's `terminal_attached` — so a device action the owner did not click
        (a forged-Origin POST from a program running as the owner) is visible."""
        try:
            self.daemon.bus.publish({"kind": "vex_device",
                                     "data": {"action": action, "at": _utcnow()}})
        except Exception as exc:  # noqa: BLE001 — a bus problem never stops the owner's action
            LOG.warning("vex %s: not announced (%s)", action, type(exc).__name__)

    # -- status and detection ---------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            device = {k: v for k, v in self._device.items() if k != "token"} if self._device else None
        view = {"configured": False, "hint": None, "vexcom_version": None, "toolchain": None,
                "sdks": None, "extension_version": None, "templates": False,
                "project_folder": None,
                "device": device["kind"] if device else None,
                "brain": device["terminal_id"] if device and device["kind"] == "brain" else None}
        try:
            cfg = load_config()
        except VexError as exc:
            view["hint"] = str(exc)
            return view
        view.update(configured=True, vexcom_version=cfg.vexcom_version,
                    toolchain={k: getattr(cfg, k) for k in EXE_KEYS if k != "vexcom"},
                    sdks={"cpp": cfg.cpp_sdk_version, "python": cfg.python_sdk_version},
                    extension_version=cfg.extension_version, templates=bool(cfg.templates),
                    project_folder=cfg.project_folder)
        return view

    def project(self, project_id) -> dict:
        project = self.daemon.require(self.daemon.stores.projects, project_id)
        return {"project_id": project.id, **detect(project.root)}

    # -- build ------------------------------------------------------------------------

    def start_build(self, project_id, action) -> dict:
        if action not in ACTIONS:
            raise _api(400, f"action must be one of {', '.join(ACTIONS)}")
        project = self._project(project_id)
        detected = self._detected(project)
        if detected.language != "cpp":
            raise VexError("a Python project has nothing to build")
        cfg = load_config()
        root = project.root
        root_windows = to_windows(root)
        argv = build_argv(cfg, detected, action)
        refusal = makefile_refusal(root)
        if refusal:
            raise VexError(refusal)
        lock = BuildLock(root)
        if not lock.acquire():
            raise VexError("a build of this project is already running (here or through "
                           "vexbuild); wait for it to finish")
        record = Build(id="", project_id=project.id, project=project.name, action=action,
                       root=root, root_windows=root_windows, started=_utcnow(), lock=lock,
                       started_ts=time.time())
        with self._lock:
            record.id = self._id(self._builds)
            self._keep(self._builds, record.id, record)
            self._held.add(lock)
        try:
            terminal = self._start(argv, cfg=cfg, tool="make", also=BUILD_TOOLS, folder=root,
                                   label=project.name, program="vex-build",
                                   title=f"vex {action} · {project.name}", project_id=project.id,
                                   on_exit=lambda t: self._build_ended(record, t),
                                   key=("vex-build", project.id))
        except BaseException:
            with self._lock:
                self._builds.pop(record.id, None)
                self._held.discard(lock)
            lock.release()
            raise
        record.terminal_id = terminal.id
        LOG.info("vex %s started for project %s", action, project.name)
        timer = self._timer(BUILD_TIMEOUT_S, self._build_timeout, record, terminal)
        with self._lock:
            if record.state == "running":
                record.timer = timer
            else:
                timer.cancel()
        return {"build_id": record.id, "terminal_id": terminal.id}

    def _build_timeout(self, record: Build, terminal) -> None:
        with self._lock:
            if record.state != "running":
                return
            record.timed_out = True
        LOG.info("vex %s for project %s timed out after %d s", record.action, record.project,
                 int(BUILD_TIMEOUT_S))
        self._stop_program(terminal)

    def _build_ended(self, record: Build, terminal) -> None:
        errors = parse_errors(plain(terminal.history().data), record.root_windows)
        found = None
        if record.action != "clean":
            try:
                detected_name = parse_settings(read_settings(record.root)).name
                found = binary(record.root, detected_name)
            except (NotVex, OSError):
                found = None
            if found is not None and found["_ts"] + 1 < record.started_ts:
                found = None                     # an old binary, not this build's
        with self._lock:
            if record.state != "running":
                return
            record.terminal_id = terminal.id
            record.exit_code = terminal.exit_code
            record.errors = errors
            record.bin = found
            record.ended = _utcnow()
            record.state = ("timed_out" if record.timed_out
                            else "succeeded" if terminal.exit_code == 0 else "failed")
            timer, lock = record.timer, record.lock
            record.timer = record.lock = None
            if lock is not None:
                self._held.discard(lock)
        if timer is not None:
            timer.cancel()
        if lock is not None:
            lock.release()
        LOG.info("vex %s for project %s ended (%s, exit %s)", record.action, record.project,
                 record.state, record.exit_code)

    def build(self, build_id) -> dict:
        if not isinstance(build_id, str) or not _ID.fullmatch(build_id):
            raise _api(400, "build id must be eight lowercase hex characters")
        with self._lock:
            record = self._builds.get(build_id)
            if record is None:
                raise _api(404, f"build {build_id} not found")
            return record.view()

    # -- download -----------------------------------------------------------------------

    def download(self, project_id, slot, name) -> dict:
        slot, name = check_slot(slot), check_name(name)
        project = self._project(project_id)
        detected = self._detected(project)
        if detected.language != "cpp":
            raise VexError("Python download waits on one step of the setup spike (which option "
                           "VEX's Python download takes); download Python projects in VS Code "
                           "for now")
        if not PROJECT_NAME.fullmatch(detected.name):
            raise VexError("the project's name must be letters, digits, _ and - (at most 64) to "
                           "download here; rename it in .vscode/vex_project_settings.json")
        cfg = load_config()
        root = project.root
        found = binary(root, detected.name)
        if found is None:
            raise VexError(f"there is no build/{detected.name}.bin yet; Build first")
        if found["_ts"] < newest_source(root):
            raise VexError(f"build/{detected.name}.bin is older than the sources; Build first, "
                           "then download")
        argv = download_argv(cfg, name, slot, to_windows(str(Path(root) / found["path"])))
        # The device first (one device action at a time, said as such), then
        # the project's build lock, held for the download so no build rewrites
        # the binary while it is being sent.
        release, bind = self._reserve_device("download")
        lock = BuildLock(root)
        if not lock.acquire():
            release()
            raise VexError("a build of this project is running; download when it has finished")
        with self._lock:
            self._held.add(lock)

        def ended(terminal):
            release()
            with self._lock:
                self._held.discard(lock)
            lock.release()
            LOG.info("vex download to slot %d for project %s ended (exit %s)", slot, project.name,
                     terminal.exit_code)

        try:
            terminal = self._start(argv, cfg=cfg, tool="vexcom", folder=root, label=project.name,
                                   program="vex-download",
                                   title=f"vex download · {project.name} → slot {slot}",
                                   project_id=project.id, on_exit=ended,
                                   key=("vex-download", project.id))
        except BaseException:
            release()
            with self._lock:
                self._held.discard(lock)
            lock.release()
            raise
        bind(terminal.id)
        self._announce("download")
        LOG.info("vex download to slot %d started for project %s", slot, project.name)
        self._timer(DOWNLOAD_TIMEOUT_S, self._device_timeout, terminal)
        return {"terminal_id": terminal.id}

    def _device_timeout(self, terminal) -> None:
        if not terminal.exited:
            LOG.info("vex %s timed out", terminal.program)
            self._stop_program(terminal)

    # -- the brain terminal -----------------------------------------------------------

    def brain(self) -> dict:
        with self._lock:
            busy = self._device
            if busy is not None and busy["kind"] == "brain" and busy["terminal_id"]:
                return {"terminal_id": busy["terminal_id"], "started": False}
        cfg = load_config()
        argv = brain_argv(cfg)
        release, bind = self._reserve_device("brain")

        def ended(terminal):
            release()
            LOG.info("vex brain terminal ended (exit %s)", terminal.exit_code)

        try:
            terminal = self._start(argv, cfg=cfg, tool="vexcom",
                                   folder=os.path.dirname(cfg.vexcom), label="brain",
                                   program="vex-brain", title="vex brain", input_ok=True,
                                   on_exit=ended, key=("vex-brain",))
        except BaseException:
            release()
            raise
        if not terminal.exited:
            bind(terminal.id)
        self._announce("brain")
        LOG.info("vex brain terminal started")
        return {"terminal_id": terminal.id, "started": True}

    # -- device info --------------------------------------------------------------------

    def info(self) -> dict:
        cfg = load_config()
        argv = info_argv(cfg)
        release, bind = self._reserve_device("info")
        done = threading.Event()

        def ended(terminal):
            release()
            done.set()

        try:
            terminal = self._start(argv, cfg=cfg, tool="vexcom",
                                   folder=os.path.dirname(cfg.vexcom), label="device",
                                   program="vex-info", title="vex device info", on_exit=ended)
        except BaseException:
            release()
            raise
        bind(terminal.id)
        self._announce("info")
        answered = done.wait(INFO_TIMEOUT_S)
        if not answered:
            self._stop_program(terminal)
            done.wait(KILL_GRACE_S + 1)
        text = device_text(terminal.history().data)
        code = terminal.exit_code
        from .daemon import APIError
        try:
            self.daemon.terminals.close(terminal.id, grace=0)   # fires `ended`, which releases
        except APIError:
            pass
        if release():                       # only if the exit hook never ran
            LOG.warning("vex device info: released after its terminal closed without an exit")
        LOG.info("vex device info read (exit %s)", code)
        if not answered:
            raise VexError(f"vexcom did not answer in {int(INFO_TIMEOUT_S)} s")
        return {"text": text, "exit_code": code}

    # -- new project (the backend VX-3's dialog uses) ------------------------------------

    def templates(self) -> list[dict]:
        return list_templates(load_config())

    def new_project(self, name, language, template, location=None) -> dict:
        from . import folders
        if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
            raise _api(400, "name must be 1-64 letters, digits, _ or - (VEX's make refuses spaces)")
        if language not in ("cpp", "python"):
            raise _api(400, "language must be cpp or python")
        cfg = load_config()
        try:
            chosen = next((t for t in list_templates(cfg) if t["id"] == template), None)
        except OSError:
            raise VexError(f"the templates folder cannot be listed; {SETUP_HINT}") from None
        if chosen is None or chosen["language"] != language:
            raise _api(400, f"template must be a {language} id from GET /vex/templates")
        entries = read_template(cfg, template)          # checked before anyone is asked
        parent = cfg.project_folder if location is None else location
        if not isinstance(parent, str) or not parent.strip():
            raise _api(400, "location must be a folder path")
        target = parent.rstrip("/") + "/" + name
        if not on_windows_drive(os.path.normpath(os.path.expanduser(target))):
            raise VexError(f"{target} is not on a Windows drive: VEX's toolchain builds only "
                           "there (V-4)")
        try:
            check = folders.check_project_folder(target, taken_roots=self._taken_roots())
        except folders.FolderRefused as exc:
            raise VexError(f"I can't use that folder: {exc}. Nothing was made.") from None
        if check.state == "nonempty":
            raise VexError(f"{check.path} is not empty; a template goes into a new or empty folder")
        settings = settings_document(name, language, cfg)
        record = NewProject(id="", name=name, path=str(check.path))
        with self._lock:
            record.id = self._id(self._news)
            self._keep(self._news, record.id, record)
        threading.Thread(target=self._create, args=(record, check, entries, settings),
                         name=f"jarvis-vex-new-{record.id}", daemon=True).start()
        LOG.info("vex new project %s: asking", name)
        return record.view()

    def _create(self, record: NewProject, check, entries, settings) -> None:
        from .projects import confirm_folder, create_project
        said: list[str] = []
        try:
            path = confirm_folder(self.daemon.approvals, check, record.name, origin=ORIGIN_NEW,
                                  say=said.append, taken_roots=self._taken_roots)
            if path is None:
                record.state, record.message = "declined", said[-1] if said else "Not approved."
                LOG.info("vex new project %s: not made", record.name)
                return
            unpack(entries, path, settings)
            created = create_project(self.daemon, record.name, str(path))
            record.project_id, record.state = created.id, "created"
            record.message = f"Made project {created.name} at {path}."
            LOG.info("vex new project %s: created", created.name)
        except Exception as exc:  # noqa: BLE001 — said in the record, never raised away
            text = str(exc) if isinstance(exc, VexError) else type(exc).__name__
            record.state, record.message = "failed", f"The project was not made: {text}"
            LOG.warning("vex new project %s failed (%s)", record.name, type(exc).__name__)

    def new_state(self, request_id) -> dict:
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise _api(400, "id must be eight lowercase hex characters")
        with self._lock:
            record = self._news.get(request_id)
            if record is None:
                raise _api(404, f"new project request {request_id} not found")
            return record.view()

    # -- shutdown -------------------------------------------------------------------------

    def stop(self) -> None:
        """Daemon stop, after the terminals ended: no new action, every timer
        cancelled, and any build lock still held released."""
        with self._lock:
            self._stopped = True
            timers, self._timers = list(self._timers), set()
            held, self._held = list(self._held), set()
        for timer in timers:
            timer.cancel()
        for lock in held:
            lock.release()


# -- routes (owner-only, the HUD listener) ------------------------------------------------------

# TODO(VX-1 / PR #41): switch to `projects.is_owner` once #41 (the shared
# owner rule, which also accepts a same-origin GET's Sec-Fetch-Site) lands —
# whichever of the two merges second makes the switch.
def _owner(handler, daemon) -> None:
    """Owner-only, as `projects.owner_only` is — the HUD's listener, a strict
    Host, the HUD's Origin — with one difference the browser forces: a
    **same-origin `GET` carries no Origin** (Chromium, measured 2026-10-10), so
    a GET is accepted with no Origin only when the browser's own
    `Sec-Fetch-Site: same-origin` says the HUD sent it. Every other method
    must carry the HUD's Origin."""
    port = handler.server.server_address[1]
    host = handler.headers.get("Host", "")
    if port != daemon.face_port or host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        raise _api(403, "only the owner can do this, from the HUD")
    origin = handler.headers.get("Origin")
    if origin is not None:
        if origin != f"http://{host}":
            raise _api(403, "only the owner can do this, from the HUD")
    elif handler.command != "GET" or handler.headers.get("Sec-Fetch-Site") != "same-origin":
        raise _api(403, "only the owner can do this, from the HUD")


def route(handler, daemon, parts, query):
    """`/vex/…` and `/projects/{id}/vex…`, or None. Every route is owner-only."""
    is_vex = bool(parts) and parts[0] == "vex"
    is_project = len(parts) >= 3 and parts[0] == "projects" and parts[2] == "vex"
    if not (is_vex or is_project):
        return None
    from .daemon import _object
    _owner(handler, daemon)
    vex = daemon.vex
    method = handler.command
    try:
        if is_project:
            project_id = parts[1]
            if parts[3:] == [] and method == "GET":
                _object(query, ())
                return 200, vex.project(project_id)
            if parts[3:] == ["build"] and method == "POST":
                _object(query, ())
                body = _object(handler._body(), ("action",), ("action",))
                return 202, vex.start_build(project_id, body["action"])
            if parts[3:] == ["download"] and method == "POST":
                _object(query, ())
                body = _object(handler._body(), ("slot", "name"), ("slot", "name"))
                try:
                    return 202, vex.download(project_id, body["slot"], body["name"])
                except ValueError as exc:
                    raise _api(400, str(exc)) from None
            raise _api(404, "route not found")
        if parts == ["vex", "status"] and method == "GET":
            _object(query, ())
            return 200, vex.status()
        if len(parts) == 3 and parts[1] == "builds" and method == "GET":
            _object(query, ())
            return 200, vex.build(parts[2])
        if parts == ["vex", "device", "info"] and method == "POST":
            _object(query, ())
            _object(handler._body(), ())
            return 200, vex.info()
        if parts == ["vex", "brain"] and method == "POST":
            _object(query, ())
            _object(handler._body(), ())
            return 200, vex.brain()
        if parts == ["vex", "templates"] and method == "GET":
            _object(query, ())
            return 200, vex.templates()
        if parts == ["vex", "projects"] and method == "POST":
            _object(query, ())
            body = _object(handler._body(), ("name", "language", "template", "location"),
                           ("name", "language", "template"))
            try:
                return 202, vex.new_project(body["name"], body["language"], body["template"],
                                            body.get("location"))
            except ValueError as exc:
                raise _api(400, str(exc)) from None
        if len(parts) == 3 and parts[1] == "projects" and method == "GET":
            _object(query, ())
            return 200, vex.new_state(parts[2])
    except VexError as exc:
        raise _api(409, str(exc)) from None
    raise _api(404, "route not found")


# -- vexbuild: the agents' build (V-2) ---------------------------------------------------------

def cli(argv=None, *, cwd=None, out=None) -> int:
    """`python -m jarvis.v2.vex build [--rebuild | --clean]`, which `vexbuild`
    runs: build the VEX C++ project in the current folder, with the same argv
    table and the same lock as the HUD's Build, and nothing else. There is
    no download here, and no way to name a program or a path."""
    import argparse
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="vexbuild", description=(
        "Build the VEX V5 C++ project in this folder with the VS Code extension's toolchain. "
        "It never downloads to the robot: that is only the owner's click in the HUD."))
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build the project in the current folder")
    mode = build.add_mutually_exclusive_group()
    mode.add_argument("--rebuild", action="store_true", help="rebuild everything (make -B)")
    mode.add_argument("--clean", action="store_true", help="remove the build folder")
    argv = list(sys.argv[1:] if argv is None else argv)
    # The wrapper already says `build` (`vexbuild` is `… -m jarvis.v2.vex
    # build "$@"`), so `vexbuild build` arrives as `build build`.
    if argv[:2] == ["build", "build"]:
        argv = argv[1:]
    args = parser.parse_args(argv)
    action = "clean" if args.clean else "rebuild" if args.rebuild else "build"

    def say(text):
        print(f"vexbuild: {text}", file=out, flush=True)

    root = os.path.realpath(cwd or os.getcwd())
    try:
        cfg = load_config()
        if os.path.lexists(os.path.join(root, "project.pros")):
            raise VexError("PROS projects aren't supported")
        try:
            detected = parse_settings(read_settings(root))
        except NotVex as exc:
            raise VexError(f"run vexbuild in a VEX V5 project's folder (the one holding "
                           f".vscode/vex_project_settings.json): {exc}") from None
        if detected.language != "cpp":
            raise VexError("a Python project has nothing to build")
        root_windows = to_windows(root)
        command = build_argv(cfg, detected, action)
        refusal = makefile_refusal(root)
        if refusal:
            raise VexError(refusal)
    except VexError as exc:
        say(str(exc))
        return 2
    lock = BuildLock(root)
    if not lock.acquire():
        say("a build of this project is already running (in the HUD or another vexbuild)")
        return 75
    from .terminals import program_environment
    captured = bytearray()
    try:
        try:
            verify_tools(cfg, "make", BUILD_TOOLS)          # immediately before the spawn
        except VexError as exc:
            say(str(exc))
            return 2
        say(f"{action} of {detected.name} started")
        proc = subprocess.Popen(command, cwd=root, env=program_environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)

        def pump():
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                captured.extend(chunk)
                del captured[:-(1 << 20)]
                out.write(chunk.decode("utf-8", "replace"))
                out.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=BUILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=KILL_GRACE_S)
                    break
                except subprocess.TimeoutExpired:
                    continue
            reader.join(KILL_GRACE_S)
            proc.stdout.close()
            say(f"timed out after {int(BUILD_TIMEOUT_S)} s")
            return 124
        reader.join(KILL_GRACE_S)
        proc.stdout.close()
    finally:
        lock.release()
    errors = parse_errors(plain(bytes(captured)), root_windows)
    count = sum(e["severity"] == "error" for e in errors)
    warnings = len(errors) - count
    if code == 0 and action != "clean":
        found = binary(root, detected.name)
        size = f" ({found['size']} bytes)" if found else ""
        say(f"built {found['path'] if found else 'no binary'}{size}; {count} error(s), "
            f"{warnings} warning(s)")
    elif code == 0:
        say("cleaned")
    else:
        say(f"failed (exit {code}); {count} error(s), {warnings} warning(s)")
    return code


if __name__ == "__main__":
    sys.exit(cli())
