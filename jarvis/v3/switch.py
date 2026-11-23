"""Owner operations for switching the systemd user service between v2 and v3."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
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
    with socket.socket() as probe:
        # A closed server can still have connections in TIME_WAIT.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _run_systemctl(*args: str) -> CompletedProcess:
    executable = shutil.which("systemctl")
    if not executable or executable.startswith("/mnt/"):
        raise OSError("systemctl is not on PATH")
    timeout = SYSTEMCTL_TIMEOUT if args[0] in ("stop", "enable", "restart") else 15.0
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


def _unit_up(env: Env, unit: str) -> bool:
    args = ("is-active", unit)
    result = _systemctl(env, unit, *args)
    state = (result.stdout or "").strip()
    if result.returncode == 0 or state in (*UP, "deactivating", "maintenance"):
        return True
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


def _down(env: Env, unit: str, port: int) -> bool:
    return not _unit_up(env, unit) and env.port_free(port)


def _wait_for(env: Env, unit: str, condition: Callable[[], bool], wait: float, what: str) -> None:
    deadline = env.clock() + wait
    while env.clock() <= deadline:
        ready = condition()
        remaining = deadline - env.clock()
        if ready and remaining >= 0:
            return
        if remaining <= 0:
            break
        env.sleep(min(POLL, remaining))
    raise SwitchError(unit, f"Timed out after {wait:g} s waiting for {what}.")


def _wait_down(env: Env, unit: str, port: int, wait: float) -> None:
    _wait_for(env, unit, lambda: _down(env, unit, port), wait,
              f"{unit} to stop and port {port} to be free")


def _stop_and_disable(env: Env, unit: str) -> None:
    env.out(f"Stopping and disabling {unit}.")
    _change(env, unit, "stop", unit, absent_ok=True)
    _change(env, unit, "disable", unit, absent_ok=True)


def _enable(env: Env, unit: str) -> None:
    env.out(f"Enabling and starting {unit}.")
    _change(env, unit, "enable", "--now", unit)


def _v2_ready(env: Env) -> bool:
    status, _ = env.v2_status()
    return status is not None and status.get("version") == 2


def _start_v2(env: Env, wait: float) -> None:
    _enable(env, V2_UNIT)
    _wait_for(env, V2_UNIT, lambda: _v2_ready(env), wait, f"{V2_UNIT} status")
    env.out(f"{V2_UNIT} is running.")


def _report_failure(env: Env, exc: SwitchError) -> int:
    env.out(str(exc))
    env.out(f"journalctl --user -u {exc.unit} -n 50 --no-pager")
    env.out("Run `jarvis hud` to go back to v2.")
    return 1


def _restore_source(env: Env, source: str, target: str, target_port: int, wait: float) -> None:
    env.out(f"Rolling back to {source}.")
    try:
        _stop_and_disable(env, target)
        _wait_down(env, target, target_port, wait)
        if source == V2_UNIT:
            _start_v2(env, wait)
        else:
            _enable(env, V3_UNIT)
            _wait_for(env, V3_UNIT, lambda: v3_responds(env), wait, f"{V3_UNIT} status")
            env.out(f"{V3_UNIT} is running.")
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
    source_stopped = False
    try:
        if v3_responds(env) and _down(env, V2_UNIT, config.DAEMON_PORT):
            env.out(f"{V3_UNIT} is already running.")
            return 0

        status, why = env.v2_status()
        if status is None and why == REFUSED:
            refusal = None
        elif status is None or status.get("version") != 2:
            refusal = (f"Can't check {V2_UNIT} for running turns or approvals "
                       f"({why or 'unexpected status'}). Pass --force to switch anyway.")
        else:
            refusal = _busy_refusal(status)
        if refusal:
            if not force:
                env.out(str(refusal))
                return 3
            env.out(f"--force overrides the {V2_UNIT} refusal. {refusal}")

        env.out(f"Stopping and disabling {V2_UNIT}.")
        _change(env, V2_UNIT, "stop", V2_UNIT, absent_ok=True)
        source_stopped = True
        _change(env, V2_UNIT, "disable", V2_UNIT, absent_ok=True)
        _wait_down(env, V2_UNIT, config.DAEMON_PORT, wait)
        _enable(env, V3_UNIT)
        _wait_for(env, V3_UNIT, lambda: v3_responds(env), wait, f"{V3_UNIT} status")
        env.out(f"{V3_UNIT} is running.")
        return 0
    except SwitchError as exc:
        if exc.unit == V3_UNIT and exc.missing:
            env.out("Run `jarvis agent install` to install the v3 user unit.")
        _report_failure(env, exc)
        if source_stopped:
            _restore_source(env, V2_UNIT, V3_UNIT, config.V3_API_PORT, wait)
        return 1


def _check_v3_idle(env: Env) -> None:
    """A6+ hook for refusing to stop a v3 daemon with work in progress."""


def to_v2(env: Env, *, wait: float = WAIT) -> int:
    source_stopped = False
    try:
        if v3_active(env):
            _check_v3_idle(env)
            env.out(f"Stopping and disabling {V3_UNIT}.")
            _change(env, V3_UNIT, "stop", V3_UNIT, absent_ok=True)
            source_stopped = True
            _change(env, V3_UNIT, "disable", V3_UNIT, absent_ok=True)
        _wait_down(env, V3_UNIT, config.V3_API_PORT, wait)
        _start_v2(env, wait)
        return 0
    except SwitchError as exc:
        _report_failure(env, exc)
        if source_stopped:
            _restore_source(env, V3_UNIT, V2_UNIT, config.DAEMON_PORT, wait)
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


def restart_v3(env: Env, *, wait: float = WAIT) -> int:
    try:
        if not _down(env, V2_UNIT, config.DAEMON_PORT):
            raise SwitchError(V2_UNIT, f"{V2_UNIT} is still up or port {config.DAEMON_PORT} is in use. "
                              "Run `jarvis agent start` to switch to v3.")
        _check_v3_idle(env)
        previous = env.v3_status() or {}
        _change(env, V3_UNIT, "restart", V3_UNIT)

        def restarted() -> bool:
            status = env.v3_status()
            return _is_v3(status) and any(
                status.get(key) != previous.get(key) or key not in previous
                for key in ("pid", "started_at")
            )

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
