"""Memory-only grants for driving Windows, with a private action log."""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading

from jarvis import config
from jarvis.desktop import DesktopError
from ._paths import open_directory


class ComputerError(Exception):
    """A reason safe to show the driver."""


class ComputerActionLog:
    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> None:
        line = (json.dumps(record) + "\n").encode("ascii")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        with self._lock, open_directory(self._data_dir) as parent:
            fd = os.open("computer_actions.jsonl", flags, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise OSError("The desktop action log must be a private regular file.")
                if os.write(fd, line) != len(line):
                    raise OSError("Incomplete desktop action log write.")
            finally:
                os.close(fd)


@dataclass(frozen=True)
class _Window:
    id: str
    app: str
    title: str


@dataclass(eq=False, repr=False)
class _Grant:
    code: str
    scope: str
    screenshot: tuple[_Window, int, int] | None = None
    windows: tuple[_Window, ...] | None = None


class ComputerDrive:
    def __init__(self, desktop, *, enabled=config.desktop_drive, log=None):
        self._desktop = desktop
        self._enabled = enabled
        self._log = log
        self._lock = threading.Lock()
        self._grant = None

    def _check_enabled(self) -> bool:
        """Read the switch and revoke any grant when off, with the lock held."""
        enabled = bool(self._enabled())
        if not enabled:
            self._grant = None
        return enabled

    def start(self) -> str:
        with self._lock:
            if not self._check_enabled():
                raise ComputerError("Desktop driving is switched off.")
            self._grant = _Grant(secrets.token_urlsafe(32), "computer-" + secrets.token_hex(16))
            return self._grant.code

    def stop(self, reason: str = "") -> bool:
        with self._lock:
            active = self._check_enabled() and self._grant is not None
            self._grant = None
            return active

    def status(self) -> dict:
        with self._lock:
            enabled = self._check_enabled()
            driving = self._grant is not None
        return {"enabled": enabled, "driving": driving, "bridge": bool(self._desktop.connected)}

    def _current(self, grant: _Grant) -> bool:
        with self._lock:
            return self._current_locked(grant)

    def _current_locked(self, grant: _Grant) -> bool:
        return self._check_enabled() and self._grant is grant

    def _grant_for_code(self, code) -> _Grant:
        """Authenticate the code with the lock held."""
        if not self._check_enabled():
            raise ComputerError("Desktop driving is switched off.")
        grant = self._grant
        if (grant is None or not isinstance(code, str) or not code.isascii()
                or not hmac.compare_digest(code, grant.code)):
            raise ComputerError("The desktop driving code is invalid or expired.")
        return grant

    def _request(self, grant: _Grant, op: str, *, input_op=False, **fields):
        if not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        try:
            result = self._desktop.request(op, timeout=30, require_pair=True, scope=grant.scope,
                                           hud_sensitive=None, current=lambda: self._current(grant), **fields)
        except DesktopError as exc:
            if not self._current(grant):
                # The session's own wording for a call that is no longer wanted is about chats.
                raise ComputerError("The desktop driving grant ended.") from None
            raise ComputerError(str(exc)) from None
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            raise ComputerError("The desktop bridge returned an invalid reply.") from None
        # Once input was sent, keep its outcome even if the grant has ended.
        if not input_op and not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        return result

    def screenshot(self, code) -> dict:
        with self._lock:
            grant = self._grant_for_code(code)
            grant.screenshot = None

        rows = self._window_rows(grant)
        front = [row for row in rows if row.get("front") is True
                 and row.get("access") in ("drivable", "see_only")
                 and isinstance(row.get("window"), str) and row["window"]]
        if len(front) != 1:
            raise ComputerError("There is no single front window available to capture.")

        window = front[0]
        shot = self._request(grant, "window_screenshot", window=window["window"], front=True)
        if not isinstance(shot, dict):
            raise ComputerError("The desktop bridge returned an invalid screenshot.")
        image, size = shot.get("image_b64"), shot.get("size")
        if (not isinstance(image, str) or not image or not isinstance(size, list)
                or len(size) != 2 or any(type(side) is not int or side < 1 for side in size)):
            raise ComputerError("The desktop bridge returned an invalid screenshot.")
        try:
            base64.b64decode(image, validate=True)
        except ValueError:
            raise ComputerError("The desktop bridge returned an invalid screenshot.") from None
        app, title = window.get("app"), window.get("title")
        app = app if isinstance(app, str) else ""
        title = title if isinstance(title, str) else ""
        with self._lock:
            self._require_current_locked(grant)
            grant.screenshot = (_Window(window["window"], app[:200], title[:200]), *size)
        return {"image_b64": image, "size": size,
                "app": app, "title": title}

    def _require_current_locked(self, grant: _Grant) -> None:
        if not self._current_locked(grant):
            raise ComputerError("The desktop driving grant ended.")

    def _window_rows(self, grant: _Grant) -> list[dict]:
        result = self._request(grant, "windows")
        rows = result.get("windows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ComputerError("The desktop bridge returned an invalid window list.")
        return rows

    def windows(self, code) -> list[dict]:
        with self._lock:
            grant = self._grant_for_code(code)
            grant.windows = None
        rows = self._window_rows(grant)
        windows, listed, ids = [], [], set()
        for n, row in enumerate(rows, 1):
            window, access = row.get("window"), row.get("access")
            if (not isinstance(window, str) or not window or window in ids
                    or access not in ("drivable", "see_only")):
                raise ComputerError("The desktop bridge returned an invalid window list.")
            ids.add(window)
            app, title = row.get("app"), row.get("title")
            app = app[:200] if isinstance(app, str) else ""
            title = title[:200] if isinstance(title, str) else ""
            windows.append(_Window(window, app, title))
            listed.append({"n": n, "app": app, "title": title, "front": row.get("front") is True,
                           "access": access})
        with self._lock:
            self._require_current_locked(grant)
            grant.windows = tuple(windows)
        return listed

    def click(self, code, x, y) -> dict:
        return self._mouse(code, "click", x, y)

    def scroll(self, code, x, y, amount) -> dict:
        return self._mouse(code, "scroll", x, y, amount)

    def _mouse(self, code, op: str, x, y, amount=None) -> dict:
        with self._lock:
            grant = self._grant_for_code(code)
            shot = grant.screenshot
        if shot is None:
            raise ComputerError("Take a screenshot before using coordinates.")
        window, width, height = shot
        if type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
            raise ComputerError("Use whole-number coordinates inside the latest screenshot.")
        extra = {}
        if op == "scroll":
            if type(amount) is not int or not 1 <= abs(amount) <= 100:
                raise ComputerError("Use a signed scroll amount of 1 to 100 wheel notches.")
            extra["amount"] = amount
        self._require_log()
        point = {"x": x, "y": y}
        probe = self._request(grant, "window_probe", window=window.id, **point)
        name, role = (probe.get("name"), probe.get("role")) if isinstance(probe, dict) else (None, None)
        if not isinstance(name, str) or not isinstance(role, str) or not role:
            raise ComputerError("The desktop bridge returned an invalid control.")
        control = {"name": name[:200], "role": role[:200]}
        self._act(grant, op, window, {**point, "control": dict(control), **extra},
                  {**point, "expect": {"name": name, "role": role}, **extra})
        return {"control": control, "app": window.app, "title": window.title, **extra}

    def front(self, code, n) -> dict:
        with self._lock:
            grant = self._grant_for_code(code)
            windows = grant.windows
        if windows is None:
            raise ComputerError("List windows before selecting one.")
        if type(n) is not int or not 1 <= n <= len(windows):
            raise ComputerError("Use a window number from the latest list.")
        self._require_log()
        window = windows[n - 1]
        self._act(grant, "front", window, {}, {})
        return {"app": window.app, "title": window.title}

    def _require_log(self) -> None:
        if self._log is None:
            raise ComputerError("The desktop action log is unavailable.")

    def _act(self, grant: _Grant, op: str, window: _Window, record: dict, fields: dict) -> None:
        if not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        id_ = secrets.token_hex(16)
        try:
            self._log({"id": id_, "state": "start", "at": datetime.now(timezone.utc).isoformat(),
                       "op": op, "app": window.app, "title": window.title, **record})
        except OSError:
            raise ComputerError("Could not write the desktop action log.") from None
        state, detail = "failed", ""
        try:
            self._request(grant, f"window_{op}", input_op=True, window=window.id, **fields)
            if op == "front":
                with self._lock:
                    if self._grant is grant:
                        grant.screenshot = None
        except ComputerError as exc:
            detail = str(exc)
            state = "refused" if detail.startswith("Refused:") else "failed"
            raise
        else:
            state = "done"
        finally:
            try:
                self._log({"id": id_, "state": state, "at": datetime.now(timezone.utc).isoformat(),
                           "detail": detail[:200]})
            except OSError:
                # The input may already have happened. Preserve its outcome for the caller.
                pass
