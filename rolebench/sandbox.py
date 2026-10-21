"""Real confinement for every agent cell: bubblewrap inside a named, memory-capped unit.

Each run is `systemd-run --user --scope --unit=rolebench-<id> ... bwrap ... -- <cmd>`.
The unit gives one cgroup to kill on a timeout (setsid children included); bwrap
gives a mount namespace in which only these exist:

    /usr (+ the /bin, /lib* links), /etc read-only (ssh and gitconfig masked)
    the work tree (the snapshot, or a grading/judging copy) read-write
    the venv read-only at /opt/rolebench/venv (its editable .pth points at a
        checkout that is not mounted, so nothing imports from it)
    a tmpfs HOME and a tmpfs /tmp, a fresh /proc and /dev
    per harness, only the login it needs (see `profile`)

Not mounted, whatever the harness: the owner's checkout, ~/.config/gh,
~/.gitconfig, ~/.ssh, /mnt/c (all of /mnt but WSL's resolv.conf), /run (so no
user bus: nothing inside can ask systemd to start a process outside), and the
lead's /tmp/claude-* session directories. The environment is an allowlist.

Network is shared with the host for the cells whose CLI must reach its API
(claude, codex, jarvis, the judge) and unshared for grading. That is the
remaining exposure: a cell with network can reach the internet and this
machine's loopback ports. Codex's own sandbox keeps its *commands* off the
network; Claude's Bash and Jarvis's shell are not.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

BWRAP = "/usr/bin/bwrap"
OPT = Path("/opt/rolebench")
VENV = OPT / "venv"
PYTHON = str(VENV / "bin" / "python")
RUNTIME = OPT / "runtime"
IO = OPT / "io"
BIN = OPT / "bin"
CODEX = OPT / "codex"
GITCONFIG = OPT / "gitconfig"
CODEX_HOME_NAME = ".codex-rolebench"

GITCONFIG_TEXT = """\
[user]
\tname = rolebench
\temail = rolebench@localhost
[credential]
\thelper =
[core]
\tfsmonitor = false
\thooksPath = /dev/null
[commit]
\tgpgsign = false
[safe]
\tdirectory = *
"""

# Passed through from the harness's own environment when set; everything
# else a cell sees is set below. Never CLAUDE_CODE_*, ANTHROPIC_*, OPENAI_*,
# GH_*, SSH_AUTH_SOCK, DBUS_*, XDG_RUNTIME_DIR or any *_KEY / *_TOKEN.
PASS_ENV = ("LANG", "TERM", "TZ")
PASS_PREFIX = ("LC_",)

PROFILES = ("claude", "codex", "jarvis", "grade", "judge")
NETWORK = {"claude": True, "codex": True, "jarvis": True, "judge": True, "grade": False}
MASK_ETC = ("/etc/ssh", "/etc/ssl/private")


class SandboxError(RuntimeError):
    pass


def available() -> str | None:
    """None when bwrap and a user systemd can host a cell, else why not."""
    if not os.access(BWRAP, os.X_OK):
        return f"{BWRAP} not found"
    if not shutil.which("systemd-run"):
        return "systemd-run not found"
    return None


@dataclass
class Cell:
    profile: str
    work: Path
    work_ro: bool = False
    io_dir: Path | None = None
    runtime: Path | None = None
    env: dict = field(default_factory=dict)
    chdir: Path | None = None
    network: bool | None = None

    def __post_init__(self):
        if self.profile not in PROFILES:
            raise SandboxError(f"unknown sandbox profile {self.profile!r}")
        if self.network is None:
            self.network = NETWORK[self.profile]


def sandbox_home() -> str:
    """HOME inside a cell: the real home's path, but a tmpfs."""
    return config.real_home()


def _python_links(venv: Path) -> list[str]:
    """The venv's interpreter is a symlink chain (uv: .venv/bin/python ->
    ~/.local/bin/python -> /usr/bin/python3.x). Recreate the links that live
    outside /usr inside the cell, pointing straight at the real interpreter."""
    args, link = [], venv / "bin" / "python"
    seen = 0
    while link.is_symlink() and seen < 8:
        target = Path(os.readlink(link))
        if not target.is_absolute():
            target = link.parent / target
        if not str(target).startswith("/usr/") and not str(target).startswith(str(venv)):
            args += ["--symlink", os.path.realpath(target), str(target)]
        link, seen = target, seen + 1
    return args


def _root_links() -> list[str]:
    args = []
    for name in ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
        if os.path.islink(name):
            args += ["--symlink", os.readlink(name), name]
        elif os.path.isdir(name):
            args += ["--ro-bind", name, name]
    return args


def _resolv() -> list[str]:
    """WSL's /etc/resolv.conf is a link into /mnt/wsl; bind only its target."""
    link = "/etc/resolv.conf"
    if os.path.islink(link):
        target = os.path.realpath(link)
        if os.path.isfile(target):
            return ["--ro-bind", target, target]
    return []


def _gitconfig_file() -> Path:
    path = config.home() / "sandbox" / "gitconfig"
    if not path.is_file() or path.read_text(encoding="utf-8") != GITCONFIG_TEXT:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(GITCONFIG_TEXT, encoding="utf-8")
        os.replace(tmp, path)
    return path


def claude_binary() -> str | None:
    found = shutil.which("claude")
    return os.path.realpath(found) if found else None


def codex_release() -> Path | None:
    """The codex release directory (bin/codex, codex-path/rg, codex-resources)."""
    found = shutil.which("codex")
    if not found:
        return None
    real = Path(os.path.realpath(found))
    root = real.parent.parent if real.parent.name == "bin" else real.parent
    return root if (root / "bin" / "codex").exists() else None


def claude_credentials() -> Path:
    return Path(config.login_home()) / ".claude" / ".credentials.json"


def codex_auth() -> Path:
    return Path(config.login_home()) / ".codex" / "auth.json"


def env_for(cell: Cell, base: dict | None = None) -> dict:
    """The whole environment a cell gets (bwrap --clearenv, then these)."""
    base = os.environ if base is None else base
    home = sandbox_home()
    env = {k: v for k, v in base.items() if k in PASS_ENV or k.startswith(PASS_PREFIX)}
    env.setdefault("LANG", "C.UTF-8")
    path = ["/usr/local/bin", "/usr/bin", "/bin"]
    if cell.profile in ("claude", "judge"):
        path.insert(0, str(BIN))
    if cell.profile == "codex":
        path[:0] = [str(CODEX / "codex-path"), str(CODEX / "bin")]
    path.insert(0, str(VENV / "bin"))
    user = os.environ.get("USER") or Path(home).name
    env.update(PATH=":".join(path), HOME=home, USER=user, LOGNAME=user, TMPDIR="/tmp",
               GH_CONFIG_DIR="/tmp/rolebench-gh-empty", GIT_CONFIG_GLOBAL=str(GITCONFIG),
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false",
               SSH_ASKPASS="/bin/false", PYTHONNOUSERSITE="1")
    pw = Path(config.login_home()) / ".cache" / "ms-playwright"
    if pw.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(home) / ".cache" / "ms-playwright")
    if cell.profile in ("claude", "judge"):
        env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if cell.profile == "codex":
        env["CODEX_HOME"] = str(Path(home) / CODEX_HOME_NAME)
    env.update(cell.env)
    return env


def bwrap_argv(cell: Cell, command: list[str], base_env: dict | None = None) -> list[str]:
    home = sandbox_home()
    work = Path(cell.work).resolve()
    a = [BWRAP, "--unshare-all"]
    if cell.network:
        a.append("--share-net")
    a += ["--die-with-parent", "--new-session",
          "--ro-bind", "/usr", "/usr", *_root_links(),
          "--ro-bind", "/etc", "/etc"]
    for masked in MASK_ETC:
        if os.path.isdir(masked):
            a += ["--tmpfs", masked]
    if os.path.exists("/etc/gitconfig"):
        a += ["--ro-bind", "/dev/null", "/etc/gitconfig"]
    if cell.network:
        a += _resolv()
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", home,
          "--dir", "/tmp/rolebench-gh-empty"]
    venv = config.venv_dir()
    if venv.is_dir():
        a += ["--ro-bind", str(venv), str(VENV), *_python_links(venv)]
    a += ["--ro-bind", str(_gitconfig_file()), str(GITCONFIG)]
    pw = Path(config.login_home()) / ".cache" / "ms-playwright"
    if pw.is_dir():
        a += ["--ro-bind", str(pw), str(Path(home) / ".cache" / "ms-playwright")]
    nm = config.node_modules_dir()
    if nm.is_dir():
        a += ["--ro-bind", str(nm), str(nm)]
    a += ["--ro-bind" if cell.work_ro else "--bind", str(work), str(work)]
    if cell.io_dir is not None:
        a += ["--bind", str(Path(cell.io_dir).resolve()), str(IO)]
    if cell.runtime is not None:
        a += ["--ro-bind", str(Path(cell.runtime).resolve()), str(RUNTIME)]
    if cell.profile in ("claude", "judge"):
        binary, creds = claude_binary(), claude_credentials()
        if not binary:
            raise SandboxError("claude CLI not found on PATH")
        if not creds.is_file():
            raise SandboxError("no Claude CLI login (~/.claude/.credentials.json is missing)")
        a += ["--ro-bind", binary, str(BIN / "claude"),
              "--ro-bind", str(creds), str(Path(home) / ".claude" / ".credentials.json")]
    if cell.profile == "codex":
        release, auth = codex_release(), codex_auth()
        if not release:
            raise SandboxError("codex CLI not found on PATH")
        if not auth.is_file():
            raise SandboxError("no Codex CLI login (~/.codex/auth.json is missing)")
        codex_home = Path(home) / CODEX_HOME_NAME
        a += ["--ro-bind", str(release), str(CODEX), "--dir", str(codex_home),
              "--ro-bind", str(auth), str(codex_home / "auth.json")]
    a.append("--clearenv")
    for key, value in sorted(env_for(cell, base_env).items()):
        a += ["--setenv", key, value]
    a += ["--chdir", str(cell.chdir or work), "--", *command]
    return a


# --- the unit ---------------------------------------------------------------------

UNIT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def unit_name(run_id: str) -> str:
    return "rolebench-" + (UNIT_RE.sub("-", run_id).strip("-") or "x")[:200]


def unit_argv(unit: str, command: list[str]) -> list[str]:
    return ["systemd-run", "--user", "--scope", "-q", f"--unit={unit}",
            "-p", f"MemoryMax={config.MEMORY_MAX}", "-p", "MemorySwapMax=0", "--", *command]


def _launcher_env() -> dict:
    """systemd-run needs only the user bus; bwrap then clears everything."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")
    return {k: os.environ[k] for k in keep if k in os.environ}


def kill_unit(unit: str) -> None:
    subprocess.run(["systemctl", "--user", "kill", "-s", "KILL", f"{unit}.scope"],
                   capture_output=True, timeout=30)


def launch(command: list[str], *, cell: Cell, run_id: str, timeout_s: float,
           stdin: str | None = None) -> tuple[int | None, str, str, bool]:
    """(returncode, stdout, stderr, timed_out) for `command` inside the cell."""
    why = available()
    if why:
        raise SandboxError(f"cannot sandbox this run: {why}")
    unit = unit_name(run_id)
    argv = unit_argv(unit, bwrap_argv(cell, command))
    return run_unit(argv, unit=unit, timeout_s=timeout_s, stdin=stdin)


def run_unit(argv: list[str], *, unit: str | None, timeout_s: float, stdin: str | None = None,
             env: dict | None = None, cwd=None) -> tuple[int | None, str, str, bool]:
    proc = subprocess.Popen(argv, cwd=cwd or "/", env=env if env is not None else _launcher_env(), text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        if unit:
            try:
                kill_unit(unit)                 # the whole cgroup: setsid children too
            except Exception:
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, out or "", err or "", True


def unit_active(unit: str) -> bool:
    done = subprocess.run(["systemctl", "--user", "is-active", f"{unit}.scope"], capture_output=True, text=True)
    return done.stdout.strip() in ("active", "activating", "deactivating")


def wait_unit_gone(unit: str, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not unit_active(unit):
            return True
        time.sleep(0.1)
    return False


# --- the Jarvis worker's code, without the answers ---------------------------------

RUNTIME_PACKAGES = ("jarvis", "longbench")
RUNTIME_ROLEBENCH = ("__init__.py", "jarvis_worker.py")


def build_runtime(dest: Path, source: Path | None = None) -> Path:
    """A copy of the code the Jarvis worker imports: jarvis/, longbench/ and the
    worker itself. Never rolebench/cases (the answers), never .env."""
    source = Path(source or config.REPO_ROOT)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*", "node_modules")
    dest.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_PACKAGES:
        shutil.copytree(source / name, dest / name, ignore=ignore, symlinks=True)
    (dest / "rolebench").mkdir()
    for name in RUNTIME_ROLEBENCH:
        shutil.copyfile(source / "rolebench" / name, dest / "rolebench" / name)
    return dest
