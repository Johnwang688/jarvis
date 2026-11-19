"""Platform-independent rules for the desktop-driving indicator."""
from typing import NamedTuple

VK_ESCAPE = 0x1B
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10


class Visibility(NamedTuple):
    frame: bool
    badge: bool


class Rect(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int


class BadgeLayout(NamedTuple):
    badge: Rect
    stop: Rect


def physical_escape(virtual_key: int, flags: int, key_down: bool) -> bool:
    return key_down and virtual_key == VK_ESCAPE and not flags & (
        LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)


def visibility(driving: bool, connected: bool, voice_active: bool) -> Visibility:
    badge = connected and driving
    return Visibility(connected and (badge or voice_active), badge)


def badge_text(esc_available: bool) -> str:
    return "Jarvis is driving" if esc_available else "Jarvis is driving    Esc unavailable"


def badge_layout(screen: Rect) -> BadgeLayout | None:
    left, top, right, bottom = screen
    width = min(304, right - left)
    height = min(34, bottom - top)
    if width <= 0 or height <= 0:
        return None
    x = left + (right - left - width) // 2
    y = top + min(16, bottom - top - height)
    badge = Rect(x, y, x + width, y + height)
    inset = min(4, (width - 1) // 2, (height - 1) // 2)
    stop_right = badge.right - inset
    stop_width = min(56, max(1, width // 3), width - 2 * inset)
    stop = Rect(stop_right - stop_width, y + inset, stop_right, badge.bottom - inset)
    return BadgeLayout(badge, stop)


def hits_stop(stop: Rect, x: int, y: int) -> bool:
    return stop.left <= x < stop.right and stop.top <= y < stop.bottom
