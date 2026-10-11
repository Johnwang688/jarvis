"""The owner's operations: restart the v2 daemon, rebuild the HUD (2026-10-11).

The owner asked for "a way for me to restart the daemon and a command to
force the hud to rebuild with latest changes in the jarvis cli":

  jarvis restart [--force] [--wait S]            (alias: jarvis daemon2 restart)
  jarvis hud rebuild [--pull] [--restart] [--force] [--clean] [--allow-dirty] [--wait S]

Both are **the owner's, from their own terminal, never a tool**, and DENY for
every agent (`protected_state`'s owner-ops detector, asked by `rules.decide`
and `run_command`): the daemon runs Jarvis's agents and holds the owner's
pending approvals, and the installed HUD is the code the approval window
runs, so an agent restarting or reinstalling either would be touching its
own control plane. The owner's terminal is outside Jarvis's gate, so the
refusal costs the owner nothing.

`restart`:
  1. ask `/status`; a running turn refuses unless `--force` — a restart cuts
     it off mid-step and denies anything it waits on;
  2. the systemd user unit must be installed **and be what runs the
     daemon**: a daemon started another way (`jarvis hud`, a shell) holds the
     ports, and systemd would start a second one that cannot bind them;
  3. `systemctl --user restart jarvis-daemon2` (argv, never a shell);
  4. wait for a **new** daemon to answer `/status` — its uptime younger than
     the restart, so the old one's last answer never passes — up to `--wait`;
  5. print its uptime, every provider's health and the HUD it serves, and
     ask for Ctrl+R in the HUD (its terminals ended with the old daemon).

`rebuild` — "build the HUD with the latest changes and make the daemon
serve it":
  1. `--pull`: fast-forward `main` from `origin/main` — refused off `main` or
     with uncommitted tracked changes; never stashes, resets or forces;
  2. `npm ci` in `hud/` when `node_modules` is missing, or older than
     `package-lock.json`, or `--clean` says so;
  3. `jarvis hud install`'s own path (`hud_install.install_checkout`: the
     clean-`hud/` refusal, `npm run build`, the read-only install);
  4. `--restart`: `restart()`'s own path, refusal on a running turn included
     (checked before anything is built, and again before restarting) — off
     by default, since an install needs only a window reload (ED-4);
  5. check `/status` serves the new build, from HEAD, installed.

Exit codes: 0 done (and, for rebuild, verified); 1 refused or failed; 3 a
turn is running, so nothing was restarted; 4 done but not verified (the
daemon did not come back, or does not serve the new build).

Nothing here runs a shell: every program is an argv list with a timeout.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from jarvis import config
from jarvis.v2 import hud_install

UNIT = "jarvis-daemon2"
UNIT_FILE = "~/.config/systemd/user/jarvis-daemon2.service"

OK, FAILED, BUSY, UNVERIFIED = 0, 1, 3, 4

WAIT = 30.0                    # seconds for a restarted daemon to answer /status
POLL = 0.25
SYSTEMCTL_TIMEOUT = 90.0       # a restart waits for the stop (TimeoutStopSec=30)
NPM_CI_TIMEOUT = 15 * 60
GIT_TIMEOUT = 60.0
FETCH_TIMEOUT = 180.0
STATUS_CAP = 1 << 20

JOURNAL = f"journalctl --user -u {UNIT} -n 50 --no-pager"


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


def fetch_status(port: int | None = None, timeout: float = 2.0) -> dict | None:
    """The daemon's `GET /status` on loopback, or None when nothing answers
    with a JSON object."""
    port = config.DAEMON_PORT if port is None else port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/status")
        response = conn.getresponse()
        if response.status != 200:
            return None
        body = json.loads(response.read(STATUS_CAP))
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 — not answering is an answer here
        return None
    finally:
        conn.close()


def _systemctl_path() -> str:
    found = shutil.which("systemctl")
    if not found or found.startswith("/mnt/"):
        raise OpsRefused("systemctl is not on PATH, so there is no systemd here to restart "
                         f"{UNIT} with; restart the daemon the way it was started")
    return found


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


def _git(*args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(["git", "-C", str(config.REPO_ROOT), *args], capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise OpsRefused(f"`git {' '.join(args)}` did not finish within {timeout:g} s") from None
    except OSError as exc:
        raise OpsRefused(f"could not run git: {exc.strerror or type(exc).__name__}") from None


def _first_lines(text: str, n: int = 3) -> str:
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    out = "; ".join(lines[:n])
    return out[:400] if out else "no message"


# -- restart ---------------------------------------------------------------------------


def unit_state() -> dict[str, str]:
    """The unit's LoadState, ActiveState and SubState, from `systemctl show`."""
    result = systemctl("show", UNIT, "--property=LoadState,ActiveState,SubState")
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


def _count(value) -> int:
    return value if type(value) is int and value > 0 else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _is_v2(status: dict | None) -> bool:
    return isinstance(status, dict) and status.get("version") == 2


def busy_refusal(status: dict | None) -> OpsRefused | None:
    """The refusal a running turn earns, or None."""
    busy = _count((status or {}).get("turns_running"))
    if not busy:
        return None
    return OpsRefused(
        f"{_plural(busy, 'turn')} {'is' if busy == 1 else 'are'} running; a restart would cut "
        f"{'it' if busy == 1 else 'them'} off mid-step and deny anything waiting on you. Let "
        f"{'it' if busy == 1 else 'them'} finish (or press Stop in the HUD) and run this again, "
        "or pass --force to restart anyway.", BUSY)


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
        status = fetch_status(timeout=1.0)
        if _fresh(status, since):
            return status
        if _clock() >= deadline:
            return None
        _sleep(POLL)


def _restart(*, force: bool, wait: float, out) -> dict:
    """Restart the unit and return the new daemon's `/status`; raises OpsRefused."""
    port = config.DAEMON_PORT
    before = fetch_status()
    if before is not None and not _is_v2(before):
        raise OpsRefused(f"something other than the v2 daemon answers on 127.0.0.1:{port} "
                         "(the v1 `jarvis daemon`?); stop it first")
    busy = busy_refusal(before)
    if busy is not None and not force:
        raise busy
    unit = unit_state()
    load = unit.get("LoadState", "")
    if load != "loaded":
        raise OpsRefused(
            f"the systemd user unit {UNIT} is not installed (LoadState={load or 'unknown'}). "
            f"Write {UNIT_FILE} (ExecStart=<repo>/.venv/bin/python -m jarvis daemon2, "
            "WorkingDirectory=%h), then `systemctl --user daemon-reload && "
            f"systemctl --user enable --now {UNIT}`")
    active = unit.get("ActiveState", "")
    if before is not None and active != "active":
        raise OpsRefused(
            f"a daemon answers on 127.0.0.1:{port}, but systemd's {UNIT} is {active or 'unknown'}: "
            "that daemon was started another way (`jarvis hud`, `jarvis daemon2` in a shell). "
            f"Stop it, then `systemctl --user start {UNIT}`; restarting through systemd now would "
            "start a second daemon that cannot bind the ports")
    if busy is not None:
        out(f"--force: {busy.args[0].split(';')[0]}, and will be cut off.")
    out(f"Restarting {UNIT}: open HUD terminals end with the old daemon, and the HUD window "
        "needs a reload (Ctrl+R) once it is back.")
    if before is None:
        out(f"(Nothing answers on 127.0.0.1:{port} right now; restarting the unit anyway.)")
    issued = _clock()
    result = systemctl("restart", UNIT, timeout=SYSTEMCTL_TIMEOUT)
    if result.returncode != 0:
        raise OpsRefused(f"`systemctl --user restart {UNIT}` failed (exit {result.returncode}): "
                         f"{_first_lines(result.stderr)}. See `{JOURNAL}`")
    status = wait_healthy(issued, wait)
    if status is None:
        try:
            now = unit_state()
            seen = f"systemd says {now.get('ActiveState', '?')}/{now.get('SubState', '?')}"
        except OpsRefused:
            seen = "systemd's state could not be read"
        raise OpsRefused(f"{UNIT} was restarted but no new daemon answered /status on "
                         f"127.0.0.1:{port} within {wait:g} s ({seen}). See `{JOURNAL}`",
                         UNVERIFIED)
    return status


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
    if state == "dev":
        return "HUD: hud/dist (JARVIS_HUD_DEV=1 is set for the daemon)."
    return (f"HUD: {_clean(state or 'unknown', 120)} — nothing is served; run "
            "`jarvis hud rebuild`.")


def restart(*, force: bool = False, wait: float = WAIT, out=print) -> int:
    """`jarvis restart`."""
    try:
        status = _restart(force=force, wait=wait, out=out)
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


def npm_ci(hud: Path, timeout: float = NPM_CI_TIMEOUT) -> None:
    """`npm ci` in `hud`, its output on the owner's terminal."""
    try:
        npm = hud_install.npm_path()
    except hud_install.InstallRefused as exc:
        raise OpsRefused(str(exc)) from None
    try:
        result = subprocess.run([npm, "ci"], cwd=str(hud), stdin=subprocess.DEVNULL, timeout=timeout)
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
    `main`, when tracked files have uncommitted changes, and when `main`
    cannot be fast-forwarded."""
    repo = config.REPO_ROOT
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


def verify(installed: hud_install.Build, status: dict | None, out) -> int:
    """Does the daemon serve `installed`, from HEAD, as an installed build?"""
    port = config.DAEMON_PORT
    if not _is_v2(status):
        out(f"Installed {installed.id}, but no v2 daemon answers on 127.0.0.1:{port}, so what it "
            f"serves cannot be checked. Start it: `systemctl --user start {UNIT}`.")
        return UNVERIFIED
    served, state, head = status.get("hud_build"), status.get("hud_state"), status.get("hud_git_head")
    checkout = hud_install.git_head()
    wrong = []
    if state == "dev":
        wrong.append("the daemon runs with JARVIS_HUD_DEV=1 and serves hud/dist, not installed builds")
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
        if restart_after and not force:
            # Before anything is pulled or built: a refusal costs nothing now.
            busy = busy_refusal(fetch_status())
            if busy is not None:
                raise OpsRefused(f"--restart: {busy}", busy.code)
        moved: list[str] = []
        if pull_first:
            moved = python_changed(*pull(out))
        reason = npm_ci_reason(hud, clean)
        if reason:
            out(f"Running `npm ci` in {hud} ({reason}) …")
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
    if restart_after:
        try:
            status = _restart(force=force, wait=wait, out=out)
        except OpsRefused as exc:
            out(f"jarvis hud rebuild: installed {installed.id}, but did not restart: {exc}")
            return exc.code
        report(status, out)
    else:
        status = fetch_status()
    return verify(installed, status, out)
