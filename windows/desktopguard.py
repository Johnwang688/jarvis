"""Policy and redaction for the Windows bridge's window-scoped operations."""

from __future__ import annotations

import ntpath
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Iterable, Protocol

if __package__:
    from . import windowpolicy as policy
else:
    import windowpolicy as policy

Rect = policy.Rect
HUD_REGION_NAME = "Jarvis protected area"
# The HUD page always carries one element with this accessible name. Chrome
# exposes a page's accessibility tree only after something first asks for it,
# so a scan that does not find the marker has not seen the page: unreadable.
HUD_MARKER_NAME = "Jarvis HUD page"
_MODIFIERS = frozenset({
    "shift", "lshift", "rshift", "ctrl", "control", "lctrl", "rctrl",
    "alt", "lalt", "ralt", "altgr",
})
# Every key name the guard passes; the bridge's key table must be exactly this
# set (it checks at import). There is no Windows key in it, by decision.
KEY_NAMES = _MODIFIERS | frozenset({
    "backspace", "tab", "enter", "return", "esc", "escape", "space",
    "pageup", "pagedown", "end", "home", "left", "up", "right", "down",
    "insert", "delete", "del",
    *"abcdefghijklmnopqrstuvwxyz0123456789", *(f"f{i}" for i in range(1, 25)),
})


class Desktop(Protocol):
    def top_windows(self) -> Iterable[int]:
        """Return visible, non-minimised top-level handles, front-most first."""
        ...

    def facts(self, hwnd: int) -> policy.Window:
        """Read policy facts for a window, using None for unreadable fields."""
        ...

    def rect(self, hwnd: int) -> Rect | None:
        """Read the visible window frame clipped to its monitor, or None."""
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

    def hud_regions(self, hwnd: int) -> Iterable[Rect] | None:
        """Read screen rectangles of nodes named HUD_REGION_NAME, or None."""
        ...

    def foreground(self) -> int | None:
        """Return the current foreground window handle, or None."""
        ...

    def window_at(self, x: int, y: int) -> int | None:
        """Return the top-level window at a screen point, or None."""
        ...


# The role of a container kept only as a step on the way to its unmasked
# children. No real control has it, so a ref can never resolve to one.
MASKED_STEP_ROLE = "masked-step"


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
        # One pixel wider: a control that only shares an edge with a mask is
        # refused too.
        near = Rect(bounds.left - 1, bounds.top - 1, bounds.right + 1, bounds.bottom + 1)
        if policy.overlaps(near, self.masks):
            raise Refused("The requested control is protected.")
        if point is not None and (not policy.masked(*point, (bounds,))
                                  or policy.masked(*point, self.masks)):
            raise Refused("The point no longer identifies the expected control.")


def usable_rect(rect: Rect | None) -> bool:
    return not policy.overlaps(rect, ())


def empty_rect(rect) -> bool:
    """True for a readable frame with no area: the window has no pixels on
    screen (Windows gives every console program a hidden zero-size
    PseudoConsoleWindow that still reports itself visible). An unreadable
    frame or one with its edges reversed is not empty; it stays protected."""
    if type(rect) is not Rect:
        return False
    edges = (rect.left, rect.top, rect.right, rect.bottom)
    if not all(isinstance(v, int) and not isinstance(v, bool) and abs(v) < 2 ** 31 for v in edges):
        return False
    return (rect.left <= rect.right and rect.top <= rect.bottom
            and (rect.left == rect.right or rect.top == rect.bottom))


def _absolute_image_path(value):
    if not isinstance(value, str) or not value:
        return None
    path = value.replace("/", "\\")
    if path[:8].casefold() == "\\\\?\\unc\\":
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    drive, rest = ntpath.splitdrive(path)
    if re.fullmatch(r"[a-zA-Z]:", drive):
        if not rest.startswith("\\"):
            return None
        components = rest.split("\\")
    elif drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2:
        server, share = drive[2:].split("\\")
        if not server or not share or server in (".", "?"):
            return None
        components = [server, share, *rest.split("\\")]
    else:
        return None
    if any(re.search(r'[\x00-\x1f\x7f<>:"|?*]|~[0-9]', part)
           or (part not in (".", "..") and part.endswith((".", " ")))
           for part in components):
        return None
    return ntpath.normpath(path).casefold()


def own_image(image_path, *, executable, prefix, base_executable, base_prefix) -> bool:
    """Identify bridge interpreter and helper paths. Unreadable paths are protected."""
    image, python, venv, base_python, base = map(
        _absolute_image_path, (image_path, executable, prefix, base_executable, base_prefix))
    if None in (image, python, venv, base_python, base) or not ntpath.basename(image):
        return True
    if image in (python, base_python):
        return True
    return any(image.startswith(folder.rstrip("\\") + "\\")
               for folder in (venv, ntpath.dirname(venv), base))


def _one_key_chord(keys) -> bool:
    if policy.refused_keys(keys):
        return False
    if not all(isinstance(key, str) and key.isascii() for key in keys):
        return False
    names = [key.strip().lower() for key in keys]
    # A closed vocabulary: a name the bridge has no key for ("VK_LWIN",
    # "win+r", "ctrl+a" as one string) is not one ordinary key, it is unknown.
    if not (all(name in KEY_NAMES for name in names) and len(set(names)) == len(names)
            and sum(name not in _MODIFIERS for name in names) == 1):
        return False
    # Chords that open the window switcher or Start, which show every window.
    held = {"ctrl" if name in ("control", "lctrl", "rctrl") else
            "alt" if name in ("lalt", "ralt", "altgr") else
            "shift" if name in ("lshift", "rshift") else
            "esc" if name == "escape" else name for name in names}
    return not ("alt" in held and held & {"tab", "esc"}) and not {"ctrl", "esc"} <= held


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
        self._extra_password_managers = extra_password_managers
        self._new_id = new_id
        self._sensitive = None
        self._ids = {}
        self._issued = set()

    def set_hud(self, sensitive):
        self._sensitive = (
            sensitive if isinstance(sensitive, int) and not isinstance(sensitive, bool)
            and sensitive >= 0 else None
        )

    def _read(self, method, *args, materialize=False, fallback=None):
        try:
            read = getattr(self._desktop, method, fallback)
            result = read(*args)
            if materialize and result is not None:
                if method == "hud_regions" and isinstance(result, (str, bytes, bytearray, Mapping)):
                    return None
                return tuple(result)
            return result
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
        # Even zero is only the daemon's last report. A region may have appeared since.
        regions = self._read("hud_regions", hwnd, materialize=True) if self._sensitive is not None else None
        hud_kind, masks = policy.hud_view(self._sensitive, regions, 0.0)
        return (kind if hud_kind == policy.DRIVABLE else hud_kind), masks

    def windows(self) -> list[dict]:
        previous = {identity: window_id for window_id, identity in self._ids.items()}
        current = {}
        result = []
        for hwnd in self._window_order():
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
        order = self._window_order()
        if hwnd not in order:
            self._ids.pop(window_id)
            raise Refused("The window is no longer available.")
        facts = self._read("facts", hwnd)
        if self._identity(hwnd, facts) != identity:
            self._ids.pop(window_id)
            raise Refused("The window has changed. List windows again.")
        kind, masks = self._classify(hwnd, facts)
        return hwnd, order, kind, masks

    def _window_order(self):
        order = self._read("top_windows", materialize=True)
        if order is None:
            raise Refused("Windows could not be inspected safely.")
        return order

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
        # Windows behind the target count as much as windows in front. The
        # bridge copies screen pixels, and nothing it can read says a window is
        # opaque: acrylic terminals, shaped windows and windows on another
        # virtual desktop all show what lies behind them.
        for other in order:
            if other == hwnd:
                continue
            # An overlapping window can extend onto the target's monitor.
            bounds = self._read("occlusion_rect", other, fallback=lambda h: self._desktop.rect(h))
            if empty_rect(bounds):
                continue
            if not usable_rect(bounds):
                overlapping.append(policy.PROTECTED)
            elif policy.overlaps(rect, (bounds,)):
                facts = self._read("facts", other)
                other_kind, other_masks = self._classify(other, facts)
                if (other_kind == policy.PROTECTED and getattr(facts, "own", None) is True
                        and getattr(facts, "elevated", None) is False):
                    # Jarvis's own helper windows (the driving badge, the
                    # frame, the voice pop-up) lie over every maximised
                    # window while a chat drives, so refusing here stopped
                    # all driving (live trial 2). They are blacked out of the
                    # capture and left out of the tree like a HUD card.
                    # Every other protected window still refuses.
                    masks += (bounds,)
                    continue
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
            if not _one_key_chord(keys):
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
    """Prune masked nodes and traverse partially masked containers without exposing them."""

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
        if node is self.root and not self._inspect(node):
            return []
        children = self._visible[id(node)][2]
        if children is None:
            children = self._backend.children(node)
        return [child for child in children if self._inspect(child)]

    def _inspect(self, node):
        info = self._backend.describe(node)
        bounds = info.get("rect")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            bounds = Rect(*bounds)
        if not usable_rect(bounds) or any(not usable_rect(mask) for mask in self._masks):
            return False
        if any(mask.left <= bounds.left and bounds.right <= mask.right
               and mask.top <= bounds.top and bounds.bottom <= mask.bottom for mask in self._masks):
            return False
        children = None
        if any(bounds.left <= mask.right and mask.left <= bounds.right
               and bounds.top <= mask.bottom and mask.top <= bounds.bottom for mask in self._masks):
            children = tuple(self._backend.children(node))
            if not children:
                return False
            info = {"role": MASKED_STEP_ROLE, "name": "", "value": "", "interactive": False,
                    "offscreen": info["offscreen"], "enabled": False, "checked": None}
        self._visible[id(node)] = (node, info, children)
        return True


def black_out(image, origin: tuple[int, int], masks):
    """Paste black over the visible part of each screen rectangle in place."""
    x, y = origin
    for mask in masks:
        left, top = max(0, mask.left - x), max(0, mask.top - y)
        right = min(image.width, mask.right - x)
        bottom = min(image.height, mask.bottom - y)
        if left < right and top < bottom:
            image.paste(0, (left, top, right, bottom))
