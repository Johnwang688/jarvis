"""Owner operations for switching the systemd user service between v2 and v3."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import socket
import stat
from subprocess import CompletedProcess
import tempfile
import time

from jarvis import config
from jarvis.v2 import owner_ops
from .daemon import SERVICE_NAME

V2_UNIT = owner_ops.UNIT
V3_UNIT = "jarvis-agent"
WAIT = 30.0


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


def _read_v3_status() -> dict | None:
    port = config.V3_API_PORT
    connection = HTTPConnection("127.0.0.1", port, timeout=owner_ops.STATUS_TIMEOUT)
    try:
        connection.request("GET", "/v3/status", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        if response.status == 200:
            status = json.loads(response.read(owner_ops.STATUS_CAP))
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


def default_env() -> Env:
    def systemctl(*args: str) -> CompletedProcess:
        timeout = owner_ops.SYSTEMCTL_TIMEOUT if args[0] in ("stop", "enable", "restart") else 15.0
        return owner_ops.systemctl(*args, timeout=timeout)

    return Env(systemctl, owner_ops.read_status, _read_v3_status, _port_free,
               time.monotonic, time.sleep, print)


class SwitchError(RuntimeError):
    def __init__(self, unit: str, message: str, *, missing: bool = False):
        super().__init__(message)
        self.unit = unit
        self.missing = missing


def _systemctl(env: Env, unit: str, *args: str) -> CompletedProcess:
    try:
        return env.systemctl(*args)
    except owner_ops.OpsRefused as exc:
        raise SwitchError(unit, str(exc)) from exc


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
    if result.returncode == 0 or state in (*owner_ops.UP, "deactivating", "maintenance"):
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


def _down(env: Env, unit: str, port: int) -> bool:
    return not _unit_up(env, unit) and env.port_free(port)


def _wait_for(env: Env, unit: str, condition: Callable[[], bool], wait: float, what: str) -> None:
    deadline = env.clock() + wait
    while True:
        ready = condition()
        remaining = deadline - env.clock()
        if ready and remaining >= 0:
            return
        if remaining <= 0:
            raise SwitchError(unit, f"Timed out after {wait:g} s waiting for {what}.")
        env.sleep(min(owner_ops.POLL, remaining))


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


def to_v3(env: Env, *, force: bool = False, wait: float = WAIT) -> int:
    try:
        if _is_v3(env.v3_status()) and _down(env, V2_UNIT, config.DAEMON_PORT):
            env.out(f"{V3_UNIT} is already running.")
            return 0

        status, why = env.v2_status()
        if status is None and why == owner_ops.REFUSED:
            refusal = None
        elif status is None or status.get("version") != 2:
            refusal = (f"Can't check {V2_UNIT} for running turns or approvals "
                       f"({why or 'unexpected status'}). Pass --force to switch anyway.")
        else:
            refusal = owner_ops.busy_refusal(status)
        if refusal:
            if not force:
                env.out(str(refusal))
                return 3
            env.out(f"--force overrides the {V2_UNIT} refusal. {refusal}")

        _stop_and_disable(env, V2_UNIT)
        _wait_down(env, V2_UNIT, config.DAEMON_PORT, wait)
        try:
            _enable(env, V3_UNIT)
        except SwitchError as exc:
            if exc.missing:
                env.out("Run `jarvis agent install` to install the v3 user unit.")
                try:
                    _start_v2(env, wait)
                except SwitchError as recovery:
                    _report_failure(env, recovery)
            raise
        _wait_for(env, V3_UNIT, lambda: _is_v3(env.v3_status()), wait, f"{V3_UNIT} status")
        env.out(f"{V3_UNIT} is running.")
        return 0
    except SwitchError as exc:
        return _report_failure(env, exc)


def _check_v3_idle(env: Env) -> None:
    """A6+ hook for refusing to stop a v3 daemon with work in progress."""


def to_v2(env: Env, *, wait: float = WAIT) -> int:
    try:
        if v3_active(env):
            _check_v3_idle(env)
            _stop_and_disable(env, V3_UNIT)
        _wait_down(env, V3_UNIT, config.V3_API_PORT, wait)
        _start_v2(env, wait)
        return 0
    except SwitchError as exc:
        return _report_failure(env, exc)


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
        data = _unit_text(python, repo)
        try:
            with unit_path.open("rb") as handle:
                existing = handle.read()
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        except FileNotFoundError:
            existing = None
        if existing == data:
            env.out(f"{unit_path} unchanged.")
            return 0
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = unit_path.with_name(f"{unit_path.name}.bak-{stamp}")
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(existing)
            env.out(f"Backed up {unit_path} to {backup}.")
        _write_unit(unit_path, data)
        try:
            _change(env, V3_UNIT, "daemon-reload")
        except SwitchError:
            # Restore the old bytes so a retry will attempt the reload again.
            if existing is None:
                unit_path.unlink()
            else:
                _write_unit(unit_path, existing, mode)
            raise
        env.out(f"Installed {unit_path}.")
        return 0
    except (OSError, ValueError) as exc:
        env.out(f"Could not install {unit_path}: {exc}")
        return 1
    except SwitchError as exc:
        return _report_failure(env, exc)
