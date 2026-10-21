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

Network (A1): a grade cell has none. A codex cell shares the host netns (its
own sandbox keeps its *commands* off the network). A claude, jarvis or judge
cell runs under pasta (`ROLEBENCH_NET=pasta`, the default):

    systemd-run --scope ... -- pasta <PASTA_FLAGS> -- python -I netcell.py <policy> -- bwrap ...

pasta makes a fresh user and network namespace with no port forwarding in
either direction and no address mapped to the host; netcell installs the
egress policy there (DNS to pasta's forwarder, TCP 443 to public addresses,
everything else prohibited, including every private, link-local, CGNAT and
host-owned address) and execs bwrap, which keeps that netns (`--share-net`)
but puts the agent in its own user namespace with no capabilities, so it can
never change the policy. `ROLEBENCH_NET=none` gives those cells no network at
all (no API either, so real runs are refused).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

BWRAP = "/usr/bin/bwrap"
PASTA = "/usr/bin/pasta"
NETCELL = Path(__file__).resolve().parent / "netcell.py"
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
# Cells whose network, when on, must go through a declared ROLEBENCH_NET mode
# (A1). Codex is exempt: its own sandbox keeps its commands off the network.
NET_GATED = ("claude", "jarvis", "judge")
NET_MODES = ("none", "pasta")
NET_DEFAULT = "pasta"
# pasta: a new user+net namespace, its tap configured from the host's (but see
# netcell's policy), no port forwarded either way (-t/-u inbound, -T/-U from
# the namespace's loopback to the host's: all four default to `auto`), no
# address translated to the host's loopback or to the host's own address,
# DNS only through the forwarder, IPv4 only, no ICMP, DHCP, DHCPv6 or RA.
DNS_FORWARD = "192.0.2.53"       # TEST-NET-1: never a real host; pasta answers it on port 53 only
PASTA_FLAGS = ("--config-net", "--quiet",
               "-t", "none", "-u", "none", "-T", "none", "-U", "none",
               "--no-map-gw", "--map-host-loopback", "none", "--map-guest-addr", "none",
               "--dns-forward", DNS_FORWARD, "--ipv4-only",
               "--no-icmp", "--no-dhcp", "--no-dhcpv6", "--no-ra")
# Prohibited inside a pasta cell, on top of every address the host owns: this
# network, private, CGNAT (Tailscale), link-local, IETF, benchmarking,
# multicast and reserved space. Loopback stays the cell's own (the local table
# is consulted first), so a cell's tests can still bind 127.0.0.1.
BLOCK_V4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
            "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4")
ALLOW_TCP = (443,)
CRED_FD_BASE = 200                # where netcell re-creates the credential pipes
MASK_ETC = ("/etc/ssh", "/etc/ssl/private")
# Installed software outside /usr that suites use (the Browser suites' Chrome).
SYSTEM_RO = ("/opt/google",)
NODE_SCRATCH = (".vite", ".vite-temp", ".vitest", ".cache")
# The credential fields each CLI needs; everything else (refresh tokens, the
# MCP OAuth block) is dropped and the trimmed view is passed in memory (A2).
CLAUDE_OAUTH_KEEP = ("accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier")
CODEX_TOKEN_DROP = ("refresh_token",)


class SandboxError(RuntimeError):
    pass


def available() -> str | None:
    """None when bwrap and a user systemd can host a cell, else why not."""
    if not os.access(BWRAP, os.X_OK):
        return f"{BWRAP} not found"
    if not shutil.which("systemd-run"):
        return "systemd-run not found"
    return None


def pasta_available() -> str | None:
    """None when a pasta cell can be built, else why not."""
    if not os.access(PASTA, os.X_OK):
        return f"{PASTA} not found (install the passt package)"
    if not os.access(netcell_ip(), os.X_OK):
        return f"{netcell_ip()} not found (iproute2)"
    if not NETCELL.is_file():
        return f"{NETCELL} is missing"
    return None


def netcell_ip() -> str:
    from . import netcell
    return netcell.IP


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
        if self.profile == "judge":
            self.work_ro = True                   # the judge only ever reads


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


def net_mode() -> str:
    """ROLEBENCH_NET, lower-cased; unset or empty is the default, pasta."""
    return (os.environ.get("ROLEBENCH_NET") or "").strip().lower() or NET_DEFAULT


def trimmed_claude_credentials() -> bytes:
    """The Claude login a cell gets: the access token and its metadata only, no
    refresh token and no MCP OAuth block. Never printed, never on disk."""
    try:
        raw = json.loads(claude_credentials().read_text(encoding="utf-8"))
        oauth = raw["claudeAiOauth"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SandboxError(f"cannot read the Claude login: {exc}")
    keep = {k: oauth[k] for k in CLAUDE_OAUTH_KEEP if k in oauth}
    if "accessToken" not in keep:
        raise SandboxError("the Claude login has no accessToken")
    return json.dumps({"claudeAiOauth": keep}).encode()


def trimmed_codex_auth() -> bytes:
    """The Codex login a cell gets: auth.json without the refresh token."""
    try:
        raw = json.loads(codex_auth().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxError(f"cannot read the Codex login: {exc}")
    tokens = {k: v for k, v in (raw.get("tokens") or {}).items() if k not in CODEX_TOKEN_DROP}
    out = {"tokens": tokens}
    for k in ("auth_mode", "OPENAI_API_KEY", "last_refresh"):
        if k in raw:
            out[k] = raw[k]
    if not tokens.get("access_token"):
        raise SandboxError("the Codex login has no access_token")
    return json.dumps(out).encode()


def credential_binds(cell: "Cell") -> list[tuple[str, bytes]]:
    """(dest path in the cell, trimmed JSON bytes) for this profile's login."""
    home = sandbox_home()
    if cell.profile in ("claude", "judge"):
        return [(str(Path(home) / ".claude" / ".credentials.json"), trimmed_claude_credentials())]
    if cell.profile == "codex":
        return [(str(Path(home) / CODEX_HOME_NAME / "auth.json"), trimmed_codex_auth())]
    return []


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


def agent_net_refusal(profile: str) -> str | None:
    """Why an agent run on this profile is refused before any quota read or
    ledger reservation, or None. A claude, jarvis or judge cell reaches its API
    only through pasta: `none` has no network, so real runs are refused, and
    so is any other mode or a missing pasta."""
    if profile not in NET_GATED:
        return None
    mode = net_mode()
    if mode == "pasta":
        why = pasta_available()
        return f"ROLEBENCH_NET=pasta: {why} ({profile} runs are refused)" if why else None
    if mode == "none":
        return f"ROLEBENCH_NET=none: no network, so no API access ({profile} runs are refused; use pasta)"
    return f"ROLEBENCH_NET={mode!r} is not a mode (none or pasta): {profile} runs are refused"


def resolve_network(cell: Cell) -> str:
    """This cell's network: "none" (an empty netns), "host" (the host's netns)
    or "pasta" (a pasta netns with netcell's egress policy), enforcing A1.

    Only codex is ever given "host" (its own sandbox keeps its commands off
    the network). A claude/jarvis/judge cell with network gets pasta, or none
    under ROLEBENCH_NET=none, and never the host netns: any other mode, or
    pasta missing, is refused."""
    if not cell.network:
        return "none"
    if cell.profile not in NET_GATED:
        return "host"
    mode = net_mode()
    if mode == "none":
        return "none"
    if mode == "pasta":
        why = pasta_available()
        if why:
            raise SandboxError(f"ROLEBENCH_NET=pasta: {why}; never falling back to the host netns")
        return "pasta"
    raise SandboxError(
        f"a {cell.profile} cell with network needs ROLEBENCH_NET=pasta (the default) or none "
        f"(sharing the host netns is an escape; see the Network note). Got {mode!r}.")


def host_addresses() -> list[str]:
    """Every IPv4 address this host owns, as /32s (blocked in a pasta cell
    whatever range it is in). Unreadable is a refusal, never an empty list."""
    try:
        done = subprocess.run([netcell_ip(), "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=10)
        found = [a["local"] for link in json.loads(done.stdout) for a in link.get("addr_info") or ()
                 if a.get("family") == "inet" and a.get("local")]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"cannot list the host's addresses for the pasta policy: {exc}")
    if done.returncode or not found:
        raise SandboxError("cannot list the host's addresses for the pasta policy")
    return sorted({f"{a}/32" for a in found})


def pasta_policy() -> dict:
    """What netcell installs in a pasta cell's namespace."""
    return {"dns": DNS_FORWARD, "block": list(BLOCK_V4) + host_addresses(), "allow_tcp": list(ALLOW_TCP)}


PASTA_RESOLV = f"nameserver {DNS_FORWARD}\noptions timeout:2 attempts:2 no-aaaa\n"


def _pasta_resolv() -> list[str]:
    """The cell's resolv.conf names only pasta's DNS forwarder (no search list)."""
    path = config.home() / "sandbox" / "resolv-pasta.conf"
    if not path.is_file() or path.read_text(encoding="utf-8") != PASTA_RESOLV:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(PASTA_RESOLV, encoding="utf-8")
        os.replace(tmp, path)
    link = "/etc/resolv.conf"
    dest = os.path.realpath(link) if os.path.islink(link) else link
    return ["--ro-bind", str(path), dest]


def pasta_argv() -> list[str]:
    return [PASTA, *PASTA_FLAGS, "--"]


def bwrap_argv(cell: Cell, command: list[str], base_env: dict | None = None,
               cred_fds: dict[str, int] | None = None, net: str | None = None) -> list[str]:
    home = sandbox_home()
    work = Path(cell.work).resolve()
    if net is None:
        net = resolve_network(cell)
        if net == "pasta":
            # A pasta cell's bwrap shares the netns it starts in, so run bare it
            # would share the host's: only cell_command, which wraps it in pasta
            # and netcell, may build it.
            raise SandboxError("a pasta cell's bwrap argv is built only by cell_command (inside pasta)")
    a = [BWRAP, "--unshare-all"]
    if net in ("host", "pasta"):
        a.append("--share-net")                   # the host's netns for codex; pasta's for the rest
    if net == "pasta":
        # bwrap starts as root of pasta's user namespace: force its own user
        # namespace, the host uid inside, and no capabilities, so nothing in
        # the cell can touch netcell's rules.
        a += ["--unshare-user", "--uid", str(os.getuid()), "--gid", str(os.getgid()), "--cap-drop", "ALL"]
    a += ["--die-with-parent", "--new-session",
          "--ro-bind", "/usr", "/usr", *_root_links(),
          "--ro-bind", "/etc", "/etc"]
    for masked in MASK_ETC:
        if os.path.isdir(masked):
            a += ["--tmpfs", masked]
    if os.path.exists("/etc/gitconfig"):
        a += ["--ro-bind", "/dev/null", "/etc/gitconfig"]
    if net == "host":
        a += _resolv()
    elif net == "pasta":
        a += _pasta_resolv()
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
        for scratch in NODE_SCRATCH:              # vite/vitest write caches here: a per-cell tmpfs each
            (nm / scratch).mkdir(exist_ok=True)
            a += ["--tmpfs", str(nm / scratch)]
    for extra in SYSTEM_RO:
        if os.path.isdir(extra):
            a += ["--ro-bind", extra, extra]
    a += ["--ro-bind" if cell.work_ro else "--bind", str(work), str(work)]
    if cell.io_dir is not None:
        a += ["--bind", str(Path(cell.io_dir).resolve()), str(IO)]
    if cell.runtime is not None:
        a += ["--ro-bind", str(Path(cell.runtime).resolve()), str(RUNTIME)]
    if cell.profile in ("claude", "judge"):
        binary = claude_binary()
        if not binary:
            raise SandboxError("claude CLI not found on PATH")
        a += ["--ro-bind", binary, str(BIN / "claude")]
    if cell.profile == "codex":
        release = codex_release()
        if not release:
            raise SandboxError("codex CLI not found on PATH")
        a += ["--ro-bind", str(release), str(CODEX), "--dir", str(Path(home) / CODEX_HOME_NAME)]
    # The login: a trimmed, in-memory view, passed through an fd with
    # --ro-bind-data, so no refresh token reaches the cell and nothing is on
    # disk or written back (A2). cred_fds carries the live fds at launch; a
    # placeholder keeps the argv inspectable without them.
    for dest, _data in credential_binds(cell):
        fd = (cred_fds or {}).get(dest, -1)
        a += ["--perms", "0400", "--ro-bind-data", str(fd), dest]
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


def _credential_fds(cell: Cell) -> tuple[dict[str, int], list[int]]:
    """Open a read-only pipe per credential bind, writing the trimmed JSON into
    it (it is small, so the write never blocks). Returns (dest -> read fd, all
    read fds to pass and then close). The data is never on disk."""
    fds: dict[str, int] = {}
    for dest, data in credential_binds(cell):
        r, w = os.pipe()
        try:
            os.write(w, data)
        finally:
            os.close(w)
        os.set_inheritable(r, True)
        fds[dest] = r
    return fds, list(fds.values())


def cell_command(cell: Cell, command: list[str], cred_fds: dict[str, int] | None = None) -> tuple[list[str], str]:
    """(what the unit runs, the cell's network). For a pasta cell that is
    pasta, then netcell with the policy, then bwrap; otherwise bwrap alone."""
    net = resolve_network(cell)
    inner = bwrap_argv(cell, command, cred_fds=cred_fds, net=net)
    if net != "pasta":
        return inner, net
    policy = json.dumps(pasta_policy(), sort_keys=True)
    return [*pasta_argv(), sys.executable, "-I", str(NETCELL), policy, "--", *inner], net


def credential_header(cell: Cell) -> tuple[dict[str, int], str]:
    """For a pasta cell: (dest -> the fd netcell re-creates it at, the stdin
    header that carries the trimmed logins to netcell). pasta closes every
    inherited descriptor but 0-2, so a pipe cannot be passed through it."""
    fds, blob = {}, {}
    for i, (dest, data) in enumerate(credential_binds(cell)):
        fds[dest] = CRED_FD_BASE + i
        blob[str(CRED_FD_BASE + i)] = base64.b64encode(data).decode("ascii")
    body = json.dumps(blob)
    return fds, f"{len(body)}\n{body}"


def launch(command: list[str], *, cell: Cell, run_id: str, timeout_s: float,
           stdin: str | None = None) -> tuple[int | None, str, str, bool]:
    """(returncode, stdout, stderr, timed_out) for `command` inside the cell."""
    why = available()
    if why:
        raise SandboxError(f"cannot sandbox this run: {why}")
    unit = unit_name(run_id)
    if resolve_network(cell) == "pasta":
        fds, header = credential_header(cell)
        argv, _net = cell_command(cell, command, cred_fds=fds)
        return run_unit(unit_argv(unit, argv), unit=unit, timeout_s=timeout_s, stdin=header + (stdin or ""))
    fds, to_pass = _credential_fds(cell)
    try:
        argv, _net = cell_command(cell, command, cred_fds=fds)
        return run_unit(unit_argv(unit, argv), unit=unit, timeout_s=timeout_s, stdin=stdin, pass_fds=tuple(to_pass))
    finally:
        for fd in to_pass:
            try:
                os.close(fd)
            except OSError:
                pass


def run_unit(argv: list[str], *, unit: str | None, timeout_s: float, stdin: str | None = None,
             env: dict | None = None, cwd=None, pass_fds: tuple = ()) -> tuple[int | None, str, str, bool]:
    proc = subprocess.Popen(argv, cwd=cwd or "/", env=env if env is not None else _launcher_env(), text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, pass_fds=pass_fds)
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
