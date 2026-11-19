"""Policy and redaction for the Windows bridge's window-scoped operations."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol

if __package__:
    from . import windowpolicy as policy
else:
    import windowpolicy as policy

Rect = policy.Rect
HUD_REGION_NAME = "Jarvis protected area"


class Desktop(Protocol):
    def top_windows(self) -> list[int]:
        """Return visible, non-minimised top-level handles, front-most first."""
        ...

    def facts(self, hwnd: int) -> policy.Window:
        """Read policy facts for a window, using None for unreadable fields."""
        ...

    def rect(self, hwnd: int) -> Rect | None:
        """Read the window rectangle in screen pixels, or None."""
        ...

    def title(self, hwnd: int) -> str:
        """Read the window's current title."""
        ...

    def pid(self, hwnd: int) -> int | None:
        """Read the current owning process ID, or None."""
        ...

    def is_hud(self, hwnd: int) -> bool:
        """Identify the Jarvis HUD window."""
        ...

    def hud_regions(self, hwnd: int) -> list[Rect] | None:
        """Read screen rectangles of nodes named HUD_REGION_NAME, or None."""
        ...

    def foreground(self) -> int | None:
        """Return the current foreground window handle, or None."""
        ...

    def window_at(self, x: int, y: int) -> int | None:
        """Return the top-level window at a screen point, or None."""
        ...


class Refused(Exception):
    """A refusal safe to show to the driving agent."""


@dataclass(frozen=True)
class Grant:
    hwnd: int
    rect: Rect
    masks: tuple[Rect, ...]

    def screenshot_point(self, x: int, y: int) -> tuple[int, int]:
        """Translate a screenshot point and check that it can be inspected."""
        point = self.rect.left + x, self.rect.top + y
        self.check_point(point)
        return point

    def check_point(self, point):
        if not policy.masked(*point, (self.rect,)):
            raise Refused("The point is outside the window.")
        if policy.masked(*point, self.masks):
            raise Refused("The point is in a protected area.")

    def check_control(self, bounds: Rect, point=None):
        """Refuse unavailable, masked or mismatched control geometry."""
        if not usable_rect(bounds) or not policy.overlaps(bounds, (self.rect,)):
            raise Refused("The requested control has unreadable or off-window bounds.")
        if policy.overlaps(bounds, self.masks):
            raise Refused("The requested control is protected.")
        if point is not None and (not policy.masked(*point, (bounds,))
                                  or policy.masked(*point, self.masks)):
            raise Refused("The point no longer identifies the expected control.")


def usable_rect(rect: Rect | None) -> bool:
    return not policy.overlaps(rect, ())


def require_expected_control(expect, info):
    if (not isinstance(expect, dict)
            or any(not isinstance(expect.get(key), str) or expect[key] != info[key]
                   for key in ("name", "role"))):
        raise Refused("The control does not match the expected name and role. Inspect it again.")


def _new_id() -> str:
    return secrets.token_hex(8)


class Guard:
    def __init__(self, desktop: Desktop, *, extra_password_managers=(), new_id=_new_id):
        self._desktop = desktop
        self._extra_password_managers = tuple(extra_password_managers)
        self._new_id = new_id
        self._sensitive = None
        self._ids = {}
        self._issued = set()

    def set_hud(self, sensitive):
        self._sensitive = (
            sensitive if isinstance(sensitive, int) and not isinstance(sensitive, bool)
            and sensitive >= 0 else None
        )

    def _read(self, method, *args):
        try:
            return getattr(self._desktop, method)(*args)
        except Exception:
            raise Refused("Windows could not be inspected safely.") from None

    def _identity(self, hwnd, facts):
        pid = self._read("pid", hwnd)
        if (isinstance(facts, policy.Window) and type(pid) is int and pid > 0
                and policy.image_name(facts.image) is not None):
            return hwnd, pid, facts.image
        return None

    def _classify(self, hwnd, facts):
        kind = policy.classify(facts, extra_password_managers=self._extra_password_managers)
        if isinstance(facts, policy.Window) and facts.window_class is None:
            kind = policy.PROTECTED
        if kind == policy.PROTECTED or not self._read("is_hud", hwnd):
            return kind, ()
        regions = self._read("hud_regions", hwnd) if self._sensitive else None
        hud_kind, masks = policy.hud_view(self._sensitive, regions, 0.0)
        return (kind if hud_kind == policy.DRIVABLE else hud_kind), masks

    def windows(self) -> list[dict]:
        previous = {identity: window_id for window_id, identity in self._ids.items()}
        current = {}
        result = []
        for hwnd in self._read("top_windows"):
            facts = self._read("facts", hwnd)
            identity = self._identity(hwnd, facts)
            if identity is None:
                continue
            kind, _ = self._classify(hwnd, facts)
            window_id = previous.get(identity)
            if window_id is not None:
                current[window_id] = identity
            if kind == policy.PROTECTED:
                continue
            if window_id is None:
                window_id = self._new_id()
                if window_id in self._issued:
                    raise Refused("Windows could not be listed safely.")
                self._issued.add(window_id)
                current[window_id] = identity
            result.append({"window": window_id, "title": self._read("title", hwnd),
                           "app": policy.image_name(facts.image), "access": kind})
        self._ids = current
        return result

    def _target(self, window_id):
        identity = self._ids.get(window_id) if isinstance(window_id, str) else None
        if identity is None:
            raise Refused("Unknown window. List windows first.")
        hwnd = identity[0]
        order = self._read("top_windows")
        if hwnd not in order:
            self._ids.pop(window_id)
            raise Refused("The window is no longer available.")
        facts = self._read("facts", hwnd)
        if self._identity(hwnd, facts) != identity:
            self._ids.pop(window_id)
            raise Refused("The window has changed. List windows again.")
        kind, masks = self._classify(hwnd, facts)
        return hwnd, order, kind, masks

    def _rect(self, hwnd):
        rect = self._read("rect", hwnd)
        if not usable_rect(rect):
            raise Refused("The window's screen bounds are unavailable.")
        return rect

    def capture(self, window_id) -> Grant:
        hwnd, order, kind, masks = self._target(window_id)
        if not policy.may_capture(kind):
            raise Refused(policy.refusal(kind))
        rect = self._rect(hwnd)
        overlapping = []
        for other in order[:order.index(hwnd)]:
            bounds = self._read("rect", other)
            if not usable_rect(bounds):
                overlapping.append(policy.PROTECTED)
            elif policy.overlaps(rect, (bounds,)):
                other_kind, other_masks = self._classify(other, self._read("facts", other))
                overlapping.append(other_kind)
                masks += tuple(mask for mask in other_masks if policy.overlaps(rect, (mask,)))
        if not policy.may_capture(kind, overlapping):
            raise Refused("A protected window covers part of this window.")
        return Grant(hwnd, rect, masks)

    def act(self, window_id, *, point=None, element=None, keys=None, focus=None) -> Grant:
        hwnd, _, kind, masks = self._target(window_id)
        if not policy.may_input(kind):
            raise Refused(policy.refusal(kind))
        if point is None and element is None and keys is None:
            raise Refused("Specify a point, element, or keys.")
        rect = self._rect(hwnd)
        if keys is not None:
            if isinstance(keys, (str, bytes)) or policy.refused_keys(keys):
                raise Refused("This key combination is refused.")
            if self._read("foreground") != hwnd:
                raise Refused("The target window must be in the foreground.")
            if masks and policy.overlaps(focus, masks):
                raise Refused("Focus is protected or cannot be verified.")
        if point is not None:
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise Refused("Specify a valid screen point.")
            x, y = point
            if policy.masked(x, y, ()) or not policy.masked(x, y, (rect,)):
                raise Refused("The point is outside the window.")
            if self._read("window_at", x, y) != hwnd:
                raise Refused("Another window covers this point.")
            if policy.masked(x, y, masks):
                raise Refused("The point is in a protected area.")
        if element is not None and policy.overlaps(element, masks):
            raise Refused("The control is protected or has unreadable bounds.")
        return Grant(hwnd, rect, masks)


class MaskedTree:
    """Prune masked nodes before the snapshot renderer can assign them refs."""

    def __init__(self, backend, masks):
        self.root = backend.root
        self._backend = backend
        self._masks = tuple(masks)
        self._visible = {}

    def describe(self, node):
        if not self._masks:
            return self._backend.describe(node)
        return self._visible[id(node)][1]

    def children(self, node):
        if not self._masks:
            return self._backend.children(node)
        if node is self.root and self._hidden(node):
            return []
        return [child for child in self._backend.children(node) if not self._hidden(child)]

    def _hidden(self, node):
        info = self._backend.describe(node)
        if policy.overlaps(Rect(*info["rect"]), self._masks):
            return True
        self._visible[id(node)] = (node, info)
        return False


def black_out(image, origin: tuple[int, int], masks):
    """Paste black over the visible part of each screen rectangle in place."""
    x, y = origin
    for mask in masks:
        left, top = max(0, mask.left - x), max(0, mask.top - y)
        right = min(image.width, mask.right - x)
        bottom = min(image.height, mask.bottom - y)
        if left < right and top < bottom:
            image.paste(0, (left, top, right, bottom))
