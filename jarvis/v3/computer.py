"""Memory-only permission to capture the front Windows window."""

import base64
from dataclasses import dataclass
import hmac
import json
import secrets
import threading

from jarvis import config
from jarvis.desktop import DesktopError


class ComputerError(Exception):
    """A reason safe to show the driver."""


@dataclass(frozen=True, repr=False)
class _Grant:
    code: str
    scope: str


class ComputerDrive:
    def __init__(self, desktop, *, enabled=config.desktop_drive):
        self._desktop = desktop
        self._enabled = enabled
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
            return self._check_enabled() and self._grant is grant

    def _request(self, grant: _Grant, op: str, **fields):
        if not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        try:
            result = self._desktop.request(op, timeout=30, require_pair=True, scope=grant.scope,
                                           hud_sensitive=None, current=lambda: self._current(grant), **fields)
        except DesktopError as exc:
            raise ComputerError(str(exc)) from None
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            raise ComputerError("The desktop bridge returned an invalid reply.") from None
        if not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        return result

    def screenshot(self, code) -> dict:
        with self._lock:
            if not self._check_enabled():
                raise ComputerError("Desktop driving is switched off.")
            grant = self._grant
            if (grant is None or not isinstance(code, str) or not code.isascii()
                    or not hmac.compare_digest(code, grant.code)):
                raise ComputerError("The desktop driving code is invalid or expired.")

        result = self._request(grant, "windows")
        rows = result.get("windows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ComputerError("The desktop bridge returned an invalid window list.")
        front = [row for row in rows if row.get("front") is True
                 and row.get("access") in ("drivable", "see_only")
                 and isinstance(row.get("window"), str) and row["window"]]
        if len(front) != 1:
            raise ComputerError("There is no single front window available to capture.")

        window = front[0]
        shot = self._request(grant, "window_screenshot", window=window["window"])
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
        if not self._current(grant):
            raise ComputerError("The desktop driving grant ended.")
        app, title = window.get("app"), window.get("title")
        return {"image_b64": image, "size": size,
                "app": app if isinstance(app, str) else "",
                "title": title if isinstance(title, str) else ""}
