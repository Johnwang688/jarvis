"""Owner operations for switching the systemd user service between v2 and v3."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
from math import inf
import os
from pathlib import Path
import shutil
import socket
from subprocess import CompletedProcess, DEVNULL, TimeoutExpired, run
import tempfile
import time

from jarvis import config
from .daemon import SERVICE_NAME

V2_UNIT = "jarvis-daemon2"
V3_UNIT = "jarvis-agent"
WAIT = 30.0
POLL = 0.25
STATUS_TIMEOUT = 2.0
STATUS_CAP = 1 << 20
SYSTEMCTL_TIMEOUT = 90.0
REFUSED = "refused"
UP = ("active", "activating", "reloading", "refreshing")
BOOT_DISABLED = ("disabled", "masked", "not-found")


@dataclass
class Env:
    systemctl: Callable[..., CompletedProcess]
    v2_status: Callable[[], tuple[dict | None, str | None]]
    v3_status: Callable[[], dict | None]
    port_free: Callable[[int], bool]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    out: Callable[[str], None]


def _is_v3(status: dict | None) -> bool:
    return isinstance(status, dict) and status.get("service") == SERVICE_NAME


def _read_v2_status() -> tuple[dict | None, str | None]:
    """Only a refused connection means nothing is listening on the v2 port."""
    connection = HTTPConnection("127.0.0.1", config.DAEMON_PORT, timeout=STATUS_TIMEOUT)
    try:
        connection.request("GET", "/status")
        response = connection.getresponse()
        if response.status != 200:
            return None, f"/status answered HTTP {response.status}"
        status = json.loads(response.read(STATUS_CAP))
        if not isinstance(status, dict):
            return None, "/status did not answer with a JSON object"
        return status, None
    except ConnectionRefusedError:
        return None, REFUSED
    except TimeoutError:
        return None, f"/status did not answer within {STATUS_TIMEOUT:g} s"
    except ValueError:
        return None, "/status did not answer with JSON"
    except Exception as exc:
        return None, f"/status could not be read ({type(exc).__name__})"
    finally:
        connection.close()


def _read_v3_status() -> dict | None:
    port = config.V3_API_PORT
    connection = HTTPConnection("127.0.0.1", port, timeout=STATUS_TIMEOUT)
    try:
        connection.request("GET", "/v3/status", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        if response.status == 200:
            status = json.loads(response.read(STATUS_CAP))
            if _is_v3(status):
                return status
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    return None


def _port_free(port: int) -> bool:
    try:
        with socket.socket() as probe:
            # A closed server can still have connections in TIME_WAIT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _run_systemctl(*args: str) -> CompletedProcess:
    executable = shutil.which("systemctl")
    if not executable or executable.startswith("/mnt/"):
        raise OSError("systemctl is not on PATH")
    waits_for_job = args[0] in ("stop", "start", "enable", "restart") or "--now" in args
    timeout = SYSTEMCTL_TIMEOUT if waits_for_job else 15.0
    return run([executable, "--user", *args], capture_output=True, text=True,
               timeout=timeout, stdin=DEVNULL)


def default_env() -> Env:
    return Env(_run_systemctl, _read_v2_status, _read_v3_status, _port_free,
               time.monotonic, time.sleep, print)


class SwitchError(RuntimeError):
    def __init__(self, unit: str, message: str, *, missing: bool = False):
        super().__init__(message)
        self.unit = unit
        self.missing = missing


def _systemctl(env: Env, unit: str, *args: str) -> CompletedProcess:
    try:
        return env.systemctl(*args)
    except TimeoutExpired as exc:
        raise SwitchError(unit, f"systemctl --user {' '.join(args)} did not return "
                          f"within {exc.timeout:g} s.") from exc
    except OSError as exc:
        raise SwitchError(unit, f"Could not run systemctl: {exc}") from exc


def _command_error(unit: str, result: CompletedProcess, *args: str) -> SwitchError:
    detail = (result.stderr or result.stdout or "").strip()
    detail_lower = detail.lower()
    missing = unit in detail_lower and any(
        marker in detail_lower for marker in ("not found", "does not exist", "not loaded")
    )
    return SwitchError(unit, f"systemctl --user {' '.join(args)} failed ({result.returncode})"
                       f"{': ' + detail if detail else ''}", missing=missing)


def _change(env: Env, unit: str, *args: str, absent_ok: bool = False) -> None:
    result = _systemctl(env, unit, *args)
    if result.returncode:
        error = _command_error(unit, result, *args)
        if not (absent_ok and error.missing):
            raise error


def _unit_up(env: Env, unit: str, *, ready: bool = False) -> bool:
    args = ("is-active", unit)
    result = _systemctl(env, unit, *args)
    state = (result.stdout or "").strip()
    if result.returncode == 0 or state in (*UP, "deactivating", "maintenance"):
        return not ready or (result.returncode == 0 and state == "active")
    if (result.returncode == 3 and state in ("inactive", "failed")
            or result.returncode == 4 and state in ("inactive", "unknown", "") and not result.stderr):
        return False
    error = _command_error(unit, result, *args)
    if error.missing:
        return False
    raise error


def v3_active(env: Env) -> bool:
    return _unit_up(env, V3_UNIT)


def v3_responds(env: Env) -> bool:
    return _is_v3(env.v3_status())


def _check_target_port(env: Env, unit: str, port: int) -> None:
    if not _unit_up(env, unit) and not env.port_free(port):
        raise SwitchError(unit, f"Cannot start {unit}: port {port} is already in use "
                          f"while {unit} is stopped.")


def _down(env: Env, unit: str, port: int | None, *, deadline: float = inf) -> bool:
    return (not _unit_up(env, unit)
            and (port is None or env.clock() <= deadline and env.port_free(port)))


def _wait_for(env: Env, unit: str, condition: Callable[[float], bool], wait: float, what: str,
              *, retry_errors: bool = False) -> None:
    deadline = env.clock() + wait
    last_error = None
    while env.clock() <= deadline:
        try:
            ready = condition(deadline)
            last_error = None
        except SwitchError as exc:
            if not retry_errors:
                raise
            ready, last_error = False, exc
        remaining = deadline - env.clock()
        if ready and remaining >= 0:
            return
        if remaining <= 0:
            break
        env.sleep(min(POLL, remaining))
    if last_error is not None:
        raise last_error
    raise SwitchError(unit, f"Timed out after {wait:g} s waiting for {what}.")


def _wait_down(env: Env, unit: str, port: int, wait: float) -> None:
    _wait_for(env, unit, lambda deadline: _down(env, unit, port, deadline=deadline), wait,
              f"{unit} to stop and port {port} to be free")


def _stop_and_disable(env: Env, unit: str, *, recovering: bool = False) -> bool:
    """Return whether a stop job completed successfully during cleanup."""
    env.out(f"Stopping and disabling {unit}.")
    stopped = True
    try:
        _change(env, unit, "stop", unit, absent_ok=True)
    except SwitchError as exc:
        if not recovering:
            raise
        _report_failure(env, exc)
        stopped = False
    # --now can cancel a queued start if the first stop did not take effect.
    disable = ("disable", unit) if stopped else ("disable", "--now", unit)
    try:
        _change(env, unit, *disable, absent_ok=True)
    except SwitchError as exc:
        if not recovering:
            raise
        _report_failure(env, exc)
        return stopped
    return True


def _enable(env: Env, unit: str) -> None:
    env.out(f"Enabling and starting {unit}.")
    _change(env, unit, "enable", "--now", unit)


def _v2_ready(env: Env) -> bool:
    status, _ = env.v2_status()
    return isinstance(status, dict) and status.get("version") == 2


def _ready(env: Env, unit: str, *, deadline: float = inf) -> bool:
    if not _unit_up(env, unit, ready=True) or env.clock() > deadline:
        return False
    return _v2_ready(env) if unit == V2_UNIT else v3_responds(env)


def _wait_up(env: Env, unit: str, wait: float, *, other: str, other_port: int | None,
             recovering: bool = False) -> None:
    def up(deadline: float) -> bool:
        return (_ready(env, unit, deadline=deadline) and env.clock() <= deadline
                and _down(env, other, other_port, deadline=deadline))

    _wait_for(env, unit, up, wait,
              f"{unit} status with {other} stopped", retry_errors=recovering)
    env.out(f"{unit} is running.")


def _start_v2(env: Env, wait: float) -> None:
    _enable(env, V2_UNIT)
    _wait_up(env, V2_UNIT, wait, other=V3_UNIT, other_port=config.V3_API_PORT)


def _report_failure(env: Env, exc: SwitchError) -> int:
    env.out(str(exc))
    env.out(f"journalctl --user -u {exc.unit} -n 50 --no-pager")
    env.out("Run `jarvis hud` to go back to v2.")
    return 1


def _unit_enablement(env: Env, unit: str) -> str:
    args = ("is-enabled", unit)
    result = _systemctl(env, unit, *args)
    state = (result.stdout or "").strip()
    if result.returncode == 0 or state in BOOT_DISABLED:
        return state
    error = _command_error(unit, result, *args)
    detail = (result.stderr or "").lower()
    if error.missing or (unit in detail and "no such file or directory" in detail):
        return "not-found"
    raise error


def _restore_source(env: Env, source: str, source_port: int, target: str,
                    wait: float, *, target_attempted: bool) -> None:
    try:
        if _ready(env, target) and _down(env, source, source_port):
            env.out(f"{target} is running and {source} is stopped.")
            return
    except SwitchError as exc:
        _report_failure(env, exc)

    env.out(f"Rolling back to {source}.")
    try:
        stopped = not target_attempted or _stop_and_disable(env, target, recovering=True)

        def target_down(deadline: float) -> bool:
            # The source can run even when something else has bound the target's port.
            if _unit_up(env, target):
                return False
            if stopped:
                return True
            if env.clock() > deadline:
                return False
            # If neither stop command completed, an inactive unit could still
            # have a queued start job from the failed enable --now.
            args = ("list-jobs", "--no-legend", "--no-pager", f"{target}.service")
            result = _systemctl(env, target, *args)
            if result.returncode:
                raise _command_error(target, result, *args)
            return not result.stdout.strip()

        _wait_for(env, target, target_down, wait, f"{target} to stop", retry_errors=True)

        target_disabled = False
        try:
            target_disabled = _unit_enablement(env, target) in BOOT_DISABLED
            if not target_disabled:
                try:
                    _change(env, target, "disable", target, absent_ok=True)
                except SwitchError as exc:
                    _report_failure(env, exc)
                target_disabled = _unit_enablement(env, target) in BOOT_DISABLED
        except SwitchError as exc:
            _report_failure(env, exc)
        if not target_disabled:
            env.out(f"Could not confirm {target} is disabled at login.")

        started = False
        if target_disabled:
            try:
                # This also replaces a source stop job that may still be pending.
                _enable(env, source)
                started = True
            except SwitchError as exc:
                _report_failure(env, exc)
        if not started:
            env.out(f"Trying to start {source} directly.")
            try:
                _change(env, source, "start", source)
            except SwitchError as exc:
                _report_failure(env, exc)
            if target_disabled:
                try:
                    if _unit_enablement(env, source) != "enabled":
                        _change(env, source, "enable", source)
                except SwitchError as exc:
                    _report_failure(env, exc)
        _wait_up(env, source, wait, other=target, other_port=None, recovering=True)
        if not target_disabled or _unit_enablement(env, source) != "enabled":
            env.out(f"{source} is running, but rollback could not restore the login configuration.")
            return
    except SwitchError as exc:
        env.out(f"Rollback to {source} failed.")
        _report_failure(env, exc)
    else:
        env.out(f"Restored {source}.")


def _busy_refusal(status: dict) -> str | None:
    for key in ("turns_running", "approvals_pending"):
        count = status.get(key)
        if type(count) is int and count > 0:
            return (f"{V2_UNIT} has running turns or pending approvals. "
                    "Wait for them to finish or pass --force to switch anyway.")
    return None


def to_v3(env: Env, *, force: bool = False, wait: float = WAIT) -> int:
    source_stop_attempted = target_attempted = False
    try:
        if v3_responds(env) and _down(env, V2_UNIT, config.DAEMON_PORT):
            env.out(f"{V3_UNIT} is already running.")
            return 0

        status, why = env.v2_status()
        if status is None and why == REFUSED:
            refusal = None
        elif not isinstance(status, dict) or status.get("version") != 2:
            refusal = (f"Can't check {V2_UNIT} for running turns or approvals "
                       f"({why or 'unexpected status'}). Pass --force to switch anyway.")
        else:
            refusal = _busy_refusal(status)
        if refusal:
            if not force:
                env.out(str(refusal))
                return 3
            env.out(f"--force overrides the {V2_UNIT} refusal. {refusal}")

        _check_target_port(env, V3_UNIT, config.V3_API_PORT)
        source_stop_attempted = True
        _stop_and_disable(env, V2_UNIT)
        _wait_down(env, V2_UNIT, config.DAEMON_PORT, wait)
        env.out(f"{V2_UNIT} is stopped and its port is free.")
        target_attempted = True
        _enable(env, V3_UNIT)
        _wait_up(env, V3_UNIT, wait, other=V2_UNIT, other_port=config.DAEMON_PORT)
        return 0
    except SwitchError as exc:
        if exc.unit == V3_UNIT and exc.missing:
            env.out("Run `jarvis agent install` to install the v3 user unit.")
        _report_failure(env, exc)
        if source_stop_attempted:
            _restore_source(env, V2_UNIT, config.DAEMON_PORT, V3_UNIT, wait,
                            target_attempted=target_attempted)
        return 1


def _check_v3_idle(env: Env) -> None:
    """A6+ hook for refusing to stop a v3 daemon with work in progress."""


def to_v2(env: Env, *, wait: float = WAIT) -> int:
    source_stop_attempted = target_attempted = False
    try:
        _check_target_port(env, V2_UNIT, config.DAEMON_PORT)
        if v3_active(env):
            _check_v3_idle(env)
            source_stop_attempted = True
            _stop_and_disable(env, V3_UNIT)
        _wait_down(env, V3_UNIT, config.V3_API_PORT, wait)
        env.out(f"{V3_UNIT} is stopped and its port is free.")
        target_attempted = True
        _start_v2(env, wait)
        return 0
    except SwitchError as exc:
        _report_failure(env, exc)
        if source_stop_attempted:
            _restore_source(env, V3_UNIT, config.V3_API_PORT, V2_UNIT, wait,
                            target_attempted=target_attempted)
        return 1


def stop_v3(env: Env, *, wait: float = WAIT) -> int:
    try:
        _check_v3_idle(env)
        _change(env, V3_UNIT, "stop", V3_UNIT, absent_ok=True)
        _wait_down(env, V3_UNIT, config.V3_API_PORT, wait)
        env.out(f"{V3_UNIT} is stopped.")
        return 0
    except SwitchError as exc:
        return _report_failure(env, exc)


def _fresh_v3(status: dict | None, previous: dict | None, issued: datetime) -> bool:
    if not _is_v3(status):
        return False
    pid = status.get("pid")
    old_pid = previous.get("pid") if _is_v3(previous) else None
    if type(pid) is int and pid > 0 and type(old_pid) is int and old_pid > 0 and pid != old_pid:
        return True
    started_at = status.get("started_at")
    if isinstance(started_at, str):
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return False
        return started.tzinfo is not None and started > issued
    return False


def restart_v3(env: Env, *, wait: float = WAIT) -> int:
    try:
        if not _down(env, V2_UNIT, config.DAEMON_PORT):
            raise SwitchError(V2_UNIT, f"{V2_UNIT} is still up or port {config.DAEMON_PORT} is in use. "
                              "Run `jarvis agent start` to switch to v3.")
        _check_v3_idle(env)
        previous = env.v3_status()
        issued = datetime.now(timezone.utc)
        _change(env, V3_UNIT, "restart", V3_UNIT)

        def restarted(deadline: float) -> bool:
            return (_unit_up(env, V3_UNIT, ready=True) and env.clock() <= deadline
                    and _fresh_v3(env.v3_status(), previous, issued))

        _wait_for(env, V3_UNIT, restarted, wait, f"a fresh {V3_UNIT} status")
        env.out(f"{V3_UNIT} restarted.")
        return 0
    except SwitchError as exc:
        return _report_failure(env, exc)


def _unit_text(python: str, repo: Path) -> bytes:
    python, directory = str(python), str(repo)
    for path in (python, directory):
        if not Path(path).is_absolute() or any(char in path for char in "\n\r\0"):
            raise ValueError("The Python and repository paths must be absolute, single-line paths.")
    executable = python.replace("%", "%%")
    if any(char.isspace() or char in "\\\"'" for char in executable):
        executable = '"' + executable.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if "$" in executable:
        executable = ":" + executable
    directory = directory.replace("%", "%%")
    if directory.endswith("\\") or directory[-1].isspace():
        directory += "/."
    return (
        "[Unit]\nDescription=Jarvis v3 daemon\n\n"
        f"[Service]\nExecStart={executable} -m jarvis agentd\n"
        f"WorkingDirectory={directory}\nRestart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode()


def _write_unit(path: Path, data: bytes, mode: int = 0o644) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def install_unit(env: Env, *, unit_path: Path, python: str, repo: Path) -> int:
    try:
        if unit_path.is_symlink():
            env.out(f"Refusing to install {unit_path}: the unit path is a symlink.")
            return 1
        data = _unit_text(python, repo)
        try:
            existing = unit_path.read_bytes()
        except FileNotFoundError:
            existing = None
        if existing == data:
            env.out(f"{unit_path} unchanged.")
            return 0
        created_parents = []
        parent = unit_path.parent
        while not parent.exists():
            created_parents.append(parent)
            parent = parent.parent
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if existing is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = unit_path.with_name(f"{unit_path.name}.bak-{stamp}")
            os.link(unit_path, backup, follow_symlinks=False)
        try:
            _write_unit(unit_path, data)
        except OSError:
            if backup is not None:
                backup.unlink()
            for parent in created_parents:
                parent.rmdir()
            raise
        try:
            _change(env, V3_UNIT, "daemon-reload")
        except SwitchError:
            # Put the original file back so a retry will attempt the reload again.
            if backup is None:
                unit_path.unlink()
                for parent in created_parents:
                    parent.rmdir()
            else:
                backup.replace(unit_path)
            raise
        if backup is not None:
            env.out(f"Backed up {unit_path} to {backup}.")
        env.out(f"Installed {unit_path}.")
        return 0
    except (OSError, ValueError) as exc:
        env.out(f"Could not install {unit_path}: {exc}")
        return 1
    except SwitchError as exc:
        return _report_failure(env, exc)
