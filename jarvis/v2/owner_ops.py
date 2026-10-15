"""The owner's operations: restart the v2 daemon, rebuild the HUD (2026-10-11).

The owner asked for "a way for me to restart the daemon and a command to
force the hud to rebuild with latest changes in the jarvis cli":

  jarvis restart [--force] [--wait S]            (alias: jarvis daemon2 restart)
  jarvis hud rebuild [--pull] [--restart] [--force] [--clean] [--allow-dirty]

Both are **the owner's, from their own terminal, never a tool**, and DENY for
every agent (`protected_state`'s owner-ops detector, asked by `rules.decide`
and `run_command`): the daemon runs Jarvis's agents and holds the owner's
pending approvals, and the installed HUD is the code the approval window
runs, so an agent restarting or reinstalling either would be touching its
own control plane. The owner's terminal is outside Jarvis's gate, so the
refusal costs the owner nothing.

`restart` (the warning in 3 is given first, and a hand-off's grace waited
out first, so the busy check in 1 is the last step before the restart —
review 2 of PR #48):
  1. ask `/status`; a running turn or a waiting approval refuses unless
     `--force` — a restart cuts a turn off mid-step and denies anything it
     waits on. A daemon systemd says is up but that does not answer (busy,
     slow, or this shell's JARVIS_DAEMON_PORT is not its port) is asked
     again, longer, and then refused the same way: "can't check" is never
     "nothing running" (review of PR #48, H1). Only a refused connection to
     a unit that is not running, or a unit that is inactive or failed, is
     restarted without that check;
  2. the systemd user unit must be installed **and be what runs the
     daemon**: a daemon started another way (`jarvis hud`, a shell) holds the
     ports, and systemd would start a second one that cannot bind them; and
     the daemon `/status` answered for must be the unit's (its `pid` is
     systemd's MainPID), or its idle answer is "can't check";
  3. warn about uncommitted Python in the checkout the daemon runs from —
     the new daemon loads it (Jarvis's chats leave edits on main);
  4. `systemctl --user restart jarvis-daemon2` (argv, never a shell);
  5. wait for a **new** daemon to answer `/status` — its uptime younger than
     the restart, so the old one's last answer never passes — up to `--wait`;
  6. print its uptime, every provider's health and the HUD it serves, and
     ask for Ctrl+R in the HUD (its terminals ended with the old daemon).
  Typed into a HUD terminal — which lives in the daemon's own cgroup, so the
  restart would end it mid-wait — the checks run there and the restart is
  handed to a transient unit outside it (`systemd-run --user`), whose report
  `journalctl --user -u <unit>` shows (review of PR #48, M4).
  A turn can still start between the check and the restart: the check
  narrows the window, it cannot close it.

`rebuild` — "build the HUD with the latest changes and make the daemon
serve it":
  1. `--pull`: fast-forward `main` from `origin/main` — refused off `main` or
     with uncommitted tracked changes; never stashes, resets or forces;
  2. the install root and the clean-`hud/` check **before** `npm ci` (review
     of PR #48, M3: `npm ci` obeys `hud/.npmrc` — and ran package.json's
     scripts until 2026-10-15 — so an edited one ran before the refusal);
  3. `npm ci --ignore-scripts` in `hud/` (no package's install script runs:
     the owner's decision of 2026-10-15, `NPM_CI_ARGS`) when `node_modules`
     is missing, or older than `package-lock.json`, or `--clean` says so;
  4. `jarvis hud install`'s own path (`hud_install.install_checkout`: the
     clean-`hud/` refusal again, `npm run build`, the read-only install);
  5. `--restart`: `restart()`'s own path, refusals included (checked before
     anything is built, and again before restarting) — off by default, since
     an install needs only a window reload (ED-4);
  6. check `/status` serves the new build, from HEAD, installed.

Exit codes: 0 done (and, for rebuild, verified); 1 refused or failed; 3 a
turn is running or an approval waiting (or that could not be checked), so
nothing was restarted; 4 done but not verified (the daemon did not come
back, does not serve the new build, or the restart was handed off from a
HUD terminal).

Nothing here runs a shell: every program is an argv list with a timeout.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

from jarvis import config
from jarvis.v2 import hud_install

UNIT = "jarvis-daemon2"
UNIT_FILE = "~/.config/systemd/user/jarvis-daemon2.service"

OK, FAILED, BUSY, UNVERIFIED = 0, 1, 3, 4

WAIT = 30.0                    # seconds for a restarted daemon to answer /status
POLL = 0.25
STATUS_TIMEOUT = 2.0           # the first /status read
RETRY_TIMEOUT = 10.0           # the second, for a daemon that is up but did not answer
SYSTEMCTL_TIMEOUT = 90.0       # a restart waits for the stop (TimeoutStopSec=30)
NPM_CI_TIMEOUT = 15 * 60
GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 180.0
STATUS_CAP = 1 << 20
HANDOFF_GRACE = 1.5            # a handed-off restart waits this long, so the terminal's
                               # last lines reach the HUD before the daemon goes
CGROUP = "/proc/self/cgroup"
HANDED_OFF = "--handed-off"    # the hidden flag a handed-off command runs with

JOURNAL = f"journalctl --user -u {UNIT} -n 50 --no-pager"
REFUSED = "refused"            # read_status: nothing listens on the port
UP = ("active", "activating", "reloading", "refreshing")


class OpsRefused(Exception):
    """A step that must not (or could not) happen; the message is the sentence."""

    def __init__(self, message: str, code: int = FAILED):
        super().__init__(message)
        self.code = code


# -- the seams the tests replace -----------------------------------------------------


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def read_status(port: int | None = None, timeout: float = STATUS_TIMEOUT) -> tuple[dict | None, str | None]:
    """The daemon's `GET /status` on loopback: (its body, None), or (None, why
    not) — `REFUSED` when nothing listens on the port, else a sentence (no
    answer in time, another status, not a JSON object). The two are not the
    same: a refused connection means no daemon there; anything else means
    one is there and did not say what it is doing (review of PR #48, H1)."""
    port = config.DAEMON_PORT if port is None else port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/status")
        response = conn.getresponse()
        if response.status != 200:
            return None, f"/status answered HTTP {response.status}"
        body = json.loads(response.read(STATUS_CAP))
        if not isinstance(body, dict):
            return None, "/status did not answer with a JSON object"
        return body, None
    except ConnectionRefusedError:
        return None, REFUSED
    except (TimeoutError, socket.timeout):
        return None, f"/status did not answer within {timeout:g} s"
    except ValueError:
        return None, "/status did not answer with JSON"
    except Exception as exc:  # noqa: BLE001 — any other failure is "no answer", never "nobody there"
        return None, f"/status could not be read ({type(exc).__name__})"
    finally:
        conn.close()


def fetch_status(port: int | None = None, timeout: float = STATUS_TIMEOUT) -> dict | None:
    """`read_status`'s body, or None."""
    return read_status(port, timeout)[0]


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found or found.startswith("/mnt/"):
        return ""
    return found


def _systemctl_path() -> str:
    found = _which("systemctl")
    if not found:
        raise OpsRefused("systemctl is not on PATH, so there is no systemd here to restart "
                         f"{UNIT} with; restart the daemon the way it was started")
    return found


def _systemd_run_path() -> str:
    return _which("systemd-run")


def _cgroup_text() -> str:
    try:
        with open(CGROUP, encoding="utf-8", errors="replace") as handle:
            return handle.read(64 * 1024)
    except OSError:
        return ""


def in_daemon_cgroup() -> bool:
    """Whether this process runs inside the daemon's own cgroup — a HUD
    terminal does (the daemon starts it), and a restart ends it."""
    for line in _cgroup_text().splitlines():
        path = line.split(":", 2)[-1]
        if f"{UNIT}.service" in path.split("/"):
            return True
    return False


def systemctl(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """`systemctl --user ARGS…` as an argv list, output captured."""
    argv = [_systemctl_path(), "--user", *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise OpsRefused(f"`systemctl --user {' '.join(args)}` did not return within {timeout:g} s; "
                         f"see `systemctl --user status {UNIT}` and `{JOURNAL}`") from None
    except OSError as exc:
        raise OpsRefused(f"could not run systemctl: {exc.strerror or type(exc).__name__}") from None


def _git(*args: str, timeout: float = GIT_TIMEOUT, repo=None) -> subprocess.CompletedProcess:
    """git in the checkout, run the way `gitops` runs it (as `hud_install`
    does since #44's review): an absolute git, inherited `GIT_*` dropped,
    prompts, pager and editors pinned, hooks and fsmonitor off, no shell."""
    from jarvis import gitops
    code, out, err = gitops._git(Path(repo or config.REPO_ROOT), list(args), timeout=timeout)
    if code == 124:
        raise OpsRefused(f"`git {' '.join(args)}` did not finish within {timeout:g} s")
    if code == 127:
        raise OpsRefused(f"could not run git: {_first_lines(err)}")
    return subprocess.CompletedProcess(["git", *args], code, out, err)


def _first_lines(text: str, n: int = 3) -> str:
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    out = "; ".join(lines[:n])
    return out[:400] if out else "no message"


# -- restart ---------------------------------------------------------------------------


def unit_state() -> dict[str, str]:
    """The unit's LoadState, ActiveState, SubState, Result, ExecStart and
    MainPID, from `systemctl show`."""
    result = systemctl("show", UNIT,
                       "--property=LoadState,ActiveState,SubState,Result,ExecStart,MainPID")
    if result.returncode != 0:
        why = _first_lines(result.stderr)
        hint = (" — in WSL, systemd must be on (`[boot] systemd=true` in /etc/wsl.conf)"
                if "bus" in why.lower() else "")
        raise OpsRefused(f"systemctl --user could not read {UNIT} ({why}){hint}")
    props = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def _start_limit_hint(unit: dict | None = None) -> str:
    """The way out of systemd's start limit, when the unit is in it."""
    if unit is None:
        try:
            unit = unit_state()
        except OpsRefused:
            return ""
    if unit.get("Result") == "start-limit-hit":
        return (f" {UNIT} hit systemd's start limit (it failed to start too often): "
                f"`systemctl --user reset-failed {UNIT}`, then `jarvis restart` again, once "
                f"`{JOURNAL}` says why it failed.")
    return ""


def _count(value) -> int:
    return value if type(value) is int and value > 0 else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _is_v2(status: dict | None) -> bool:
    return isinstance(status, dict) and status.get("version") == 2


def _waiting(status: dict | None) -> tuple[int, int]:
    """(turns running, approvals waiting on the owner) by `/status`."""
    status = status or {}
    return _count(status.get("turns_running")), _count(status.get("approvals_pending"))


def _waiting_words(turns: int, asks: int) -> str:
    parts = []
    if turns:
        parts.append(f"{_plural(turns, 'turn')} {'is' if turns == 1 else 'are'} running")
    if asks:
        parts.append(f"{_plural(asks, 'approval')} {'is' if asks == 1 else 'are'} waiting on you")
    return " and ".join(parts)


def busy_refusal(status: dict | None) -> OpsRefused | None:
    """The refusal a running turn or a waiting approval earns, or None."""
    turns, asks = _waiting(status)
    if not turns and not asks:
        return None
    if turns:
        them = "it" if turns == 1 and not asks else "them"
        cut = (f"a restart would cut {them} off mid-step and deny anything waiting on you. Let "
               f"{them} finish (or press Stop in the HUD)")
    else:
        them = "it" if asks == 1 else "them"
        cut = f"a restart would deny {them}. Answer {them} (or let {them} time out)"
    return OpsRefused(f"{_waiting_words(turns, asks)}; {cut} and run this again, or pass --force "
                      "to restart anyway.", BUSY)


def _fresh(status: dict | None, since: float) -> bool:
    """A v2 daemon that started after `since` (our restart), by its uptime."""
    if not _is_v2(status):
        return False
    uptime = status.get("uptime")
    return (isinstance(uptime, (int, float)) and not isinstance(uptime, bool)
            and uptime <= _clock() - since + 1.0)


def wait_healthy(since: float, wait: float) -> dict | None:
    """Poll `/status` until a daemon started after `since` answers, or None
    once `wait` seconds have passed."""
    deadline = _clock() + wait
    while True:
        status, _ = read_status(timeout=1.0)
        if _fresh(status, since):
            return status
        if _clock() >= deadline:
            return None
        _sleep(POLL)


class _Preflight:
    """What `_preflight` found: the daemon's `/status` (None when there was
    no answer), the unit, and what a `--force` overrode."""

    def __init__(self, status, unit, unchecked: str = ""):
        self.status, self.unit, self.unchecked = status, unit, unchecked

    def say_forced(self, out) -> None:
        """What `--force` is overriding, said before the restart."""
        turns, asks = _waiting(self.status)
        if turns or asks:
            out(f"--force: {_waiting_words(turns, asks)}; restarting anyway — "
                + ("a running turn is cut off mid-step and " if turns else "")
                + "anything waiting on you is denied.")
        if self.unchecked:
            out(f"--force: can't check for running turns ({self.unchecked}); restarting anyway.")


def _parent_pid(pid: int) -> int | None:
    """The parent of `pid`, from /proc, or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read(4096).rpartition(")")[2].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


def _main_pid(unit: dict | None) -> int:
    value = (unit or {}).get("MainPID", "")
    return int(value) if value.isdigit() else 0


def _not_the_units(status: dict | None, unit: dict | None) -> str:
    """Why the daemon that answered `/status` is not the unit's (its `pid`
    is not systemd's MainPID, nor a child of it), or "". Says nothing when
    either is unknown — a daemon from before `/status` had a `pid`, a unit
    with no MainPID (review 2 of PR #48, L-4)."""
    pid, main = (status or {}).get("pid"), _main_pid(unit)
    if type(pid) is not int or pid <= 0 or not main:
        return ""
    here = pid
    for _ in range(4):
        if here == main:
            return ""
        here = _parent_pid(here)
        if not here or here <= 1:
            break
    port = config.DAEMON_PORT
    return (f"the v2 daemon answering on 127.0.0.1:{port} is pid {pid}, but systemd's {UNIT} is "
            f"pid {main} — another daemon holds this shell's port (is JARVIS_DAEMON_PORT, {port} "
            "here, different from the daemon's?), so the unit's running turns cannot be seen")


def _no_answer_detail(port: int, why: str, active: str) -> str:
    if why == REFUSED:
        return (f"systemd says {UNIT} is {active}, but nothing answers on 127.0.0.1:{port} — "
                f"is JARVIS_DAEMON_PORT different in this shell ({port} here) from the daemon's?")
    return (f"the daemon holds 127.0.0.1:{port} (systemd says {active}) but {why}, asked twice, "
            f"the second time for {RETRY_TIMEOUT:g} s — it may be busy")


def _preflight(force: bool, unit: dict | None = None) -> _Preflight:
    """Everything that can refuse a restart before it is issued; raises
    OpsRefused. `/status` is read first, so a busy daemon is refused before
    anything else is asked; `unit` is `unit_state()` when the caller has
    just read it (the uncommitted-Python warning does, before this — the busy
    check is the last step before the restart, review 2 of PR #48)."""
    port = config.DAEMON_PORT
    status, why = read_status()
    if status is not None:
        if not _is_v2(status):
            raise OpsRefused(f"something other than the v2 daemon answers on 127.0.0.1:{port} "
                             "(the v1 `jarvis daemon`?); stop it first")
        busy = busy_refusal(status)
        if busy is not None and not force:
            raise busy
    unit = unit if unit is not None else unit_state()
    load = unit.get("LoadState", "")
    if load != "loaded":
        raise OpsRefused(
            f"the systemd user unit {UNIT} is not installed (LoadState={load or 'unknown'}). "
            f"Write {UNIT_FILE} (ExecStart=<repo>/.venv/bin/python -m jarvis daemon2, "
            "WorkingDirectory=%h), then `systemctl --user daemon-reload && "
            f"systemctl --user enable --now {UNIT}`")
    active = unit.get("ActiveState", "")
    unchecked = ""
    if status is None and active in UP and not (why == REFUSED and active != "active"):
        # systemd says the daemon runs, and it did not say what it is doing:
        # ask once more, longer, before calling that "nothing running".
        status, again = read_status(timeout=RETRY_TIMEOUT)
        if status is None:
            unchecked = _no_answer_detail(port, again or why or "?", active)
            if not force:
                raise OpsRefused(f"can't check for running turns: {unchecked}. Wait and run this "
                                 "again, or pass --force to restart anyway.", BUSY)
        elif not _is_v2(status):
            raise OpsRefused(f"something other than the v2 daemon answers on 127.0.0.1:{port} "
                             "(the v1 `jarvis daemon`?); stop it first")
        else:
            busy = busy_refusal(status)
            if busy is not None and not force:
                raise busy
    if status is not None and active != "active":
        raise OpsRefused(
            f"a daemon answers on 127.0.0.1:{port}, but systemd's {UNIT} is {active or 'unknown'}: "
            "that daemon was started another way (`jarvis hud`, `jarvis daemon2` in a shell). "
            f"Stop it, then `systemctl --user start {UNIT}`; restarting through systemd now would "
            "start a second daemon that cannot bind the ports")
    # The daemon that answered must be the unit's: an idle one on this
    # shell's port must not stand in for a busy unit (L-4).
    other = _not_the_units(status, unit)
    if other:
        if not force:
            raise OpsRefused(f"can't check for running turns: {other}. Run this where the daemon's "
                             "port is set, or pass --force to restart anyway.", BUSY)
        status, unchecked = None, other
    return _Preflight(status, unit, unchecked=unchecked)


def daemon_checkout(unit: dict | None) -> Path:
    """The checkout the daemon's code comes from: `<checkout>/.venv/bin/python`
    in the unit's ExecStart, else this CLI's own `config.REPO_ROOT`."""
    m = re.search(r"\bpath=([^ ;]+)", (unit or {}).get("ExecStart", ""))
    if m:
        python = Path(m.group(1))
        if python.parent.name == "bin" and python.parent.parent.name == ".venv":
            return python.parent.parent.parent
    return Path(config.REPO_ROOT)


def uncommitted_python(repo: Path) -> list[str] | None:
    """`git status --porcelain -- jarvis pyproject.toml` in `repo`, untracked
    files included; None when git cannot say (or must not run there)."""
    if hud_install.config_risk(repo):
        return None
    try:
        result = _git("status", "--porcelain", "--untracked-files=normal", "--", "jarvis",
                      "pyproject.toml", repo=repo)
    except OpsRefused:
        return None
    if result.returncode != 0:
        return None
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def warn_uncommitted(unit: dict | None, out) -> None:
    """Warn — never refuse — when the daemon's checkout holds uncommitted
    Python: the new daemon loads it (review of PR #48, M5)."""
    repo = daemon_checkout(unit)
    changed = uncommitted_python(repo)
    if changed is None:
        out(f"(Could not check {repo} for uncommitted Python; the new daemon loads whatever is there.)")
    elif changed:
        listed = ", ".join(changed[:8]) + (f" and {len(changed) - 8} more" if len(changed) > 8 else "")
        out(f"Warning: {repo}, the checkout the daemon runs from, has uncommitted changes the new "
            f"daemon will load: {listed}. Jarvis's chats leave edits on main — commit, stash or "
            "discard them first if they are not meant to run.")


def _warned_unit(out) -> dict | None:
    """Read the unit and warn about uncommitted Python in its checkout — the
    slow part (git status) done before the busy check, never after it
    (review 2 of PR #48). The unit, or None when systemd cannot say (the
    busy check then reads it, and refuses with the reason)."""
    try:
        unit = unit_state()
    except OpsRefused:
        unit = None
    warn_uncommitted(unit, out)
    return unit


def _restart(*, force: bool, wait: float, out, handed_off: bool = False) -> dict:
    """Restart the unit and return the new daemon's `/status`; raises
    OpsRefused. Everything slow — the hand-off's grace, the uncommitted-
    Python warning — comes first, so the busy check is the last step before
    `systemctl restart` (review 2 of PR #48)."""
    port = config.DAEMON_PORT
    if handed_off:
        # The terminal that handed this off already warned, and its last
        # lines reach the HUD in this pause.
        _sleep(HANDOFF_GRACE)
        unit = None
    else:
        unit = _warned_unit(out)
    checked = _preflight(force, unit)
    checked.say_forced(out)
    out(f"Restarting {UNIT}: open HUD terminals end with the old daemon, and the HUD window "
        "needs a reload (Ctrl+R) once it is back.")
    if checked.status is None and not checked.unchecked:
        out(f"(Nothing answers on 127.0.0.1:{port} right now; restarting the unit anyway.)")
    issued = _clock()
    result = systemctl("restart", UNIT, timeout=SYSTEMCTL_TIMEOUT)
    if result.returncode != 0:
        raise OpsRefused(f"`systemctl --user restart {UNIT}` failed (exit {result.returncode}): "
                         f"{_first_lines(result.stderr)}. See `{JOURNAL}`.{_start_limit_hint()}")
    status = wait_healthy(issued, wait)
    if status is None:
        try:
            now = unit_state()
            seen = f"systemd says {now.get('ActiveState', '?')}/{now.get('SubState', '?')}"
        except OpsRefused:
            now, seen = {}, "systemd's state could not be read"
        raise OpsRefused(f"{UNIT} was restarted but no new daemon answered /status on "
                         f"127.0.0.1:{port} within {wait:g} s ({seen}). See `{JOURNAL}`."
                         f"{_start_limit_hint(now) if now else ''}", UNVERIFIED)
    if type(status.get("pid")) is int:
        # The daemon that answered must be the unit's new one (L-4).
        try:
            other = _not_the_units(status, unit_state())
        except OpsRefused:
            other = ""
        if other:
            raise OpsRefused(f"{UNIT} was restarted, but {other}. See `{JOURNAL}`.", UNVERIFIED)
    return status


def hand_off(argv: list[str], out) -> str:
    """Run `jarvis ARGV --handed-off` as a transient user unit, outside the
    daemon's cgroup (`systemd-run --user`), and return the unit's name.
    Raises OpsRefused when it cannot."""
    run = _systemd_run_path()
    if not run:
        raise OpsRefused("this terminal runs inside the daemon, so a restart from here would end it "
                         "mid-wait, and systemd-run is not on PATH to run the restart outside it. "
                         "Run it from a terminal outside the HUD (a WSL shell)")
    name = f"jarvis-owner-op-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    # `-P`: the working directory is not put on sys.path, so a `jarvis.py`
    # or `jarvis/` planted in $HOME is never what runs (review 2 of PR #48);
    # the CLI comes from the venv's own install.
    command = [run, "--user", "--collect", "--quiet", f"--unit={name}",
               f"--working-directory={Path.home()}", "--", sys.executable, "-P", "-m", "jarvis",
               *argv, HANDED_OFF]
    out(f"This terminal runs inside the daemon ({UNIT}.service), so the restart would end it "
        f"mid-wait. Handing the restart to the transient unit {name}; its report: "
        f"`journalctl --user -u {name} --no-pager`. This terminal closes when the daemon "
        "restarts — press Ctrl+R in the HUD window then.")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise OpsRefused("systemd-run did not return within 30 s; nothing was handed off. Run it "
                         "from a terminal outside the HUD (a WSL shell)") from None
    except OSError as exc:
        raise OpsRefused(f"could not run systemd-run ({exc.strerror or type(exc).__name__}); "
                         "run it from a terminal outside the HUD (a WSL shell)") from None
    if result.returncode != 0:
        raise OpsRefused(f"systemd-run failed (exit {result.returncode}): {_first_lines(result.stderr)}; "
                         "nothing was handed off. Run it from a terminal outside the HUD (a WSL shell)")
    out(f"Handed off to {name}.")
    return name


def _clean(text, cap: int = 200) -> str:
    return " ".join(str(text).split())[:cap]


def report(status: dict, out) -> None:
    """Uptime, every provider's health, and the HUD the daemon serves."""
    uptime = status.get("uptime")
    shown = f"{uptime:.1f} s" if isinstance(uptime, (int, float)) and not isinstance(uptime, bool) else "?"
    out(f"The daemon is up (uptime {shown}).")
    providers = status.get("providers")
    if isinstance(providers, dict) and providers:
        for name, health in sorted(providers.items()):
            health = health if isinstance(health, dict) else {}
            reason = _clean(health.get("reason", ""))
            mark = "ok" if health.get("ok") is True else "NOT OK"
            out(f"  {_clean(name, 40):<10} {mark}" + (f" — {reason}" if reason else ""))
    out(hud_line(status))


def hud_line(status: dict) -> str:
    build, state = status.get("hud_build"), status.get("hud_state")
    head = status.get("hud_git_head")
    if build and state == "installed":
        line = f"HUD: build {_clean(build, 80)}, installed" + (
            f" from {_clean(head, 12)}" if isinstance(head, str) and head else "")
        checkout = hud_install.git_head()
        if checkout and isinstance(head, str) and head and head != checkout:
            line += (f" — the checkout is at {checkout[:12]}; `jarvis hud rebuild` if hud/ "
                     "changed since")
        return line + "."
    if build and state == "fallback":
        over = status.get("hud_passed_over")
        over = ", ".join(_clean(b, 80) for b in over[:3]) if isinstance(over, list) and over else "a newer build"
        return (f"HUD: build {_clean(build, 80)} — a fallback: {over} failed verification and was "
                "passed over; `jarvis hud rebuild` to install a good one.")
    if state == "dev":
        return "HUD: hud/dist (JARVIS_HUD_DEV=1 is set for the daemon)."
    return (f"HUD: {_clean(state or 'unknown', 120)} — nothing is served; run "
            "`jarvis hud rebuild`.")


def _restart_argv(force: bool, wait: float) -> list[str]:
    return ["restart", *(["--force"] if force else []), "--wait", f"{wait:g}"]


def restart(*, force: bool = False, wait: float = WAIT, out=print, handed_off: bool = False) -> int:
    """`jarvis restart`."""
    try:
        if in_daemon_cgroup():
            if handed_off:
                raise OpsRefused("the handed-off restart still runs inside the daemon's cgroup, "
                                 "so it would end itself; nothing was restarted")
            # A HUD terminal: check here, so a refusal is read where it was
            # typed, then restart from outside the cgroup the restart ends.
            checked = _preflight(force, _warned_unit(out))
            checked.say_forced(out)
            hand_off(_restart_argv(force, wait), out)
            return UNVERIFIED
        status = _restart(force=force, wait=wait, out=out, handed_off=handed_off)
    except OpsRefused as exc:
        out(f"jarvis restart: {exc}")
        return exc.code
    report(status, out)
    out("Press Ctrl+R in the HUD window to reconnect it.")
    return OK


# -- rebuild ---------------------------------------------------------------------------


def npm_ci_reason(hud: Path, clean: bool = False) -> str | None:
    """Why `npm ci` must run before the build, or None.

    `node_modules/.package-lock.json` is npm's record of what it installed;
    a `package-lock.json` newer than it means a pull moved the dependencies."""
    if clean:
        return "--clean"
    modules = Path(hud) / "node_modules"
    if not modules.is_dir():
        return "node_modules is missing"
    try:
        installed = (modules / ".package-lock.json").stat().st_mtime_ns
    except OSError:
        return "node_modules has no .package-lock.json"
    try:
        wanted = (Path(hud) / "package-lock.json").stat().st_mtime_ns
    except OSError:
        return None             # no lock file: npm ci cannot run; the build will say so
    if wanted > installed:
        return "package-lock.json is newer than node_modules"
    return None


# `npm ci` never runs a package's install scripts (the owner's decision of
# 2026-10-15: an install script is code from the registry that runs at install
# time with the owner's rights). The flag, not an .npmrc: a command-line option
# outranks every npm config file, so no `.npmrc` can turn the scripts back on.
# The HUD needs none of them — the lock's 14 (node-gyp-build for tree-sitter's
# native Node bindings, which the browser build never loads; esbuild's
# install.js, whose binary arrives as its optional @esbuild/* package anyway;
# protobufjs's version notice) — see docs/notes/hud-workspace.md.
NPM_CI_ARGS = ("ci", "--ignore-scripts")


def npm_ci(hud: Path, timeout: float = NPM_CI_TIMEOUT) -> None:
    """`npm ci --ignore-scripts` in `hud`, its output on the owner's terminal."""
    try:
        npm = hud_install.npm_path()
    except hud_install.InstallRefused as exc:
        raise OpsRefused(str(exc)) from None
    try:
        result = subprocess.run([npm, *NPM_CI_ARGS], cwd=str(hud), stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OpsRefused(f"`npm ci` did not finish within {timeout:g} s; nothing was installed") from None
    except OSError as exc:
        raise OpsRefused(f"could not run npm: {exc.strerror or type(exc).__name__}") from None
    if result.returncode != 0:
        raise OpsRefused(f"`npm ci` failed (exit {result.returncode}); nothing was installed")


def _head() -> str | None:
    result = _git("rev-parse", "HEAD")
    head = result.stdout.strip()
    return head if result.returncode == 0 and head else None


def pull(out) -> tuple[str | None, str | None]:
    """Fast-forward `main` from `origin/main`; (HEAD before, HEAD after).

    Refuses — never stashes, resets or forces — when the checkout is not on
    `main`, when tracked files have uncommitted changes, when `main` cannot
    be fast-forwarded, and when the checkout's git config names a program or
    cannot be read."""
    repo = config.REPO_ROOT
    risky = hud_install.config_risk(Path(repo))
    if risky and "could not be read" in risky:
        raise OpsRefused(f"--pull does not run git in {repo}: {risky} — pull it yourself, or "
                         "drop --pull")
    if risky:
        raise OpsRefused(f"the checkout's own git config names a program git would run ({risky}); "
                         "--pull does not run git there — pull it yourself, or drop --pull")
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    name = branch.stdout.strip() if branch.returncode == 0 else ""
    if name != "main":
        raise OpsRefused(f"--pull fast-forwards main, and the checkout at {repo} is on "
                         f"{name or 'a detached HEAD'}; switch to main yourself, or drop --pull")
    changed = _git("status", "--porcelain", "--untracked-files=no")
    if changed.returncode != 0:
        raise OpsRefused(f"git status failed in {repo}: {_first_lines(changed.stderr)}")
    lines = [line for line in changed.stdout.splitlines() if line.strip()]
    if lines:
        listed = ", ".join(line[3:] for line in lines[:8]) + (" …" if len(lines) > 8 else "")
        raise OpsRefused(f"tracked files in {repo} have uncommitted changes ({listed}); commit or "
                         "stash them yourself, or drop --pull. This never stashes, resets or forces")
    before = _head()
    fetched = _git("fetch", "origin", "main", timeout=FETCH_TIMEOUT)
    if fetched.returncode != 0:
        raise OpsRefused(f"`git fetch origin main` failed: {_first_lines(fetched.stderr)}")
    merged = _git("merge", "--ff-only", "origin/main")
    if merged.returncode != 0:
        raise OpsRefused("main cannot be fast-forwarded to origin/main "
                         f"({_first_lines(merged.stderr or merged.stdout)}); sort it out yourself — "
                         "this never merges, rebases, resets or forces")
    after = _head()
    if before == after:
        ahead = _git("rev-list", "--count", "origin/main..HEAD")
        n = ahead.stdout.strip() if ahead.returncode == 0 else ""
        if n.isdigit() and int(n) > 0:
            out(f"main is {_plural(int(n), 'commit')} ahead of origin/main ({(after or '?')[:12]}): "
                "nothing to pull; it builds what is here, unpushed commits included.")
        else:
            out(f"main is up to date with origin/main ({(after or '?')[:12]}).")
    else:
        count = _git("rev-list", "--count", f"{before}..{after}")
        n = count.stdout.strip() if count.returncode == 0 else "?"
        out(f"Fast-forwarded main {(before or '?')[:12]} → {(after or '?')[:12]} ({n} commit(s)).")
    return before, after


def python_changed(before: str | None, after: str | None) -> list[str]:
    """Files a pull changed that the running daemon has loaded (Python under
    `jarvis/`, and `pyproject.toml`)."""
    if not before or not after or before == after:
        return []
    diff = _git("diff", "--name-only", f"{before}..{after}")
    if diff.returncode != 0:
        return []
    return [path for path in diff.stdout.splitlines()
            if (path.startswith("jarvis/") and path.endswith(".py")) or path == "pyproject.toml"]


def _status_for_verify() -> tuple[dict | None, str | None]:
    """`/status` for the check after an install: a daemon that did not
    answer is asked once more, longer, before it counts as down."""
    status, why = read_status()
    if status is None and why != REFUSED:
        status, why = read_status(timeout=RETRY_TIMEOUT)
    return status, why


def verify(installed: hud_install.Build, status: dict | None, out, why: str | None = REFUSED) -> int:
    """Does the daemon serve `installed`, from HEAD, as an installed build?"""
    port = config.DAEMON_PORT
    if not _is_v2(status):
        if status is not None:
            out(f"Installed {installed.id}, but something other than the v2 daemon answers on "
                f"127.0.0.1:{port}, so what the daemon serves cannot be checked.")
        elif why != REFUSED:
            # Up but slow or busy — never "start it" (review of PR #48, H1).
            out(f"Installed {installed.id}, but the daemon on 127.0.0.1:{port} did not answer "
                f"({why}), so what it serves cannot be checked. It is not down: reload the HUD "
                "window (Ctrl+R) in a moment, and `jarvis restart` only if it stays silent.")
        else:
            try:
                active = unit_state().get("ActiveState", "")
            except OpsRefused:
                active = ""
            if active in UP:
                out(f"Installed {installed.id}, but nothing answers on 127.0.0.1:{port} although "
                    f"systemd says {UNIT} is {active} — is JARVIS_DAEMON_PORT different in this "
                    "shell from the daemon's? What it serves cannot be checked from here.")
            else:
                out(f"Installed {installed.id}, but no v2 daemon answers on 127.0.0.1:{port}, so what "
                    f"it serves cannot be checked. Start it: `systemctl --user start {UNIT}`.")
        return UNVERIFIED
    served, state, head = status.get("hud_build"), status.get("hud_state"), status.get("hud_git_head")
    checkout = hud_install.git_head()
    wrong = []
    over = status.get("hud_passed_over")
    if state == "dev":
        wrong.append("the daemon runs with JARVIS_HUD_DEV=1 and serves hud/dist, not installed builds")
    elif isinstance(over, list) and installed.id in over:
        wrong.append(f"the daemon passed {installed.id} over (it failed the daemon's verification) "
                     f"and serves {_clean(served or 'nothing', 80)} instead")
    elif served != installed.id:
        wrong.append(f"the daemon serves {_clean(served or 'nothing', 80)} ({_clean(state or '?', 80)}), "
                     f"not {installed.id} — was it installed where the daemon looks? (this shell "
                     f"installed into {config.HUD_DIR}; JARVIS_HUD_DIR must agree)")
    elif state != "installed":
        wrong.append(f"its state is {_clean(state or '?', 80)}, not installed")
    if not wrong and checkout and head != checkout:
        wrong.append(f"it was built from {_clean(head or '?', 12)}, but the checkout is at {checkout[:12]}")
    if wrong:
        out("Not verified: " + "; ".join(wrong) + ".")
        return UNVERIFIED
    out(f"The daemon serves {installed.id} from {(head or '?')[:12]}, installed"
        + (" (with uncommitted hud/ changes)" if installed.dirty else "") + ".")
    out("Press Ctrl+R in the HUD window to load it.")
    return OK


def rebuild(*, pull_first: bool = False, restart_after: bool = False, clean: bool = False,
            allow_dirty: bool = False, force: bool = False, wait: float = WAIT, out=print,
            build=None) -> int:
    """`jarvis hud rebuild`."""
    hud = Path(config.REPO_ROOT) / "hud"
    try:
        if restart_after:
            # Before anything is pulled or built: a refusal costs nothing now.
            try:
                _preflight(force)
            except OpsRefused as exc:
                raise OpsRefused(f"--restart: {exc}", exc.code) from None
        moved: list[str] = []
        if pull_first:
            moved = python_changed(*pull(out))
        # The install root and a clean hud/ before `npm ci`, which obeys
        # hud/.npmrc (review of PR #48, M3); it runs no install scripts
        # (`NPM_CI_ARGS`, owner 2026-10-15).
        why = hud_install.root_refusal(Path(config.HUD_DIR))
        if why:
            raise hud_install.InstallRefused(why)
        hud_install._clean_enough(hud_install.hud_dirty(), allow_dirty)
        reason = npm_ci_reason(hud, clean)
        if reason:
            out(f"Running `npm ci --ignore-scripts` in {hud} ({reason}) …")
            npm_ci(hud)
        out(f"Building and installing {hud} …")
        kwargs = {"allow_dirty": allow_dirty}
        if build is not None:
            kwargs["build"] = build
        installed = hud_install.install_checkout(**kwargs)
    except hud_install.InstallRefused as exc:
        out(f"jarvis hud rebuild: refused: {exc}")
        return FAILED
    except OpsRefused as exc:
        out(f"jarvis hud rebuild: {exc}")
        return exc.code
    out(hud_install.describe(installed))
    if moved and not restart_after:
        out(f"Python changed in that pull ({_plural(len(moved), 'file')}"
            + (", pyproject.toml among them — `uv pip install -e .` if dependencies moved"
               if "pyproject.toml" in moved else "")
            + "): the daemon runs the old code until it restarts — `jarvis restart` when no turn "
            "is running, or pass --restart next time.")
    why: str | None = None
    if restart_after:
        try:
            if in_daemon_cgroup():
                checked = _preflight(force, _warned_unit(out))
                checked.say_forced(out)
                hand_off(_restart_argv(force, wait), out)
                out(f"Installed {installed.id}; the restart's report, with the HUD it serves, is "
                    "in that unit's journal.")
                return UNVERIFIED
            status = _restart(force=force, wait=wait, out=out)
        except OpsRefused as exc:
            out(f"jarvis hud rebuild: installed {installed.id}, but did not restart: {exc}")
            return exc.code
        report(status, out)
    else:
        status, why = _status_for_verify()
    return verify(installed, status, out, why)
