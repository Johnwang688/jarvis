"""Which Windows windows the driving agent may see and control.

Shared by the Windows bridge and the WSL daemon (no Windows imports), so both
sides give one answer for one window. Plan:
`docs/plans/2026-11-19-computer-use-plan.md` section 2; owner decisions
CU-D1, D2, D3, D15 and D18.

A window is classified by the **program that owns it** and whether that
program is elevated, never by its title: a title is whatever the page or
document inside the window chose to call itself.

    protected   no capture, no accessibility tree, no input
    see-only    capture and tree, no input (terminals: the agent has a
                terminal tool, and that one is read by the command judge)
    drivable    capture, tree and input

Anything the bridge could not read is protected. This module lists what is
protected or see-only and calls the rest drivable, which is the owner's
choice (CU-D1): it blocks the known routes, not every window that could ever
hold a secret or run a command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

PROTECTED = "protected"
SEE_ONLY = "see_only"
DRIVABLE = "drivable"

# Windows' own prompts for a password, a PIN or consent to elevate.
SYSTEM_PROMPT_IMAGES = frozenset({
    "consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe",
    "credentialenrollmentmanager.exe",
})
SYSTEM_PROMPT_CLASSES = frozenset({"credential dialog xaml host"})

# Seed list; the owner adds to it through `extra_password_managers`.
PASSWORD_MANAGER_IMAGES = frozenset({
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
    "lastpass.exe", "dashlane.exe", "protonpass.exe", "nordpass.exe",
    "enpass.exe", "keeperpasswordmanager.exe", "roboform.exe",
})

TERMINAL_IMAGES = frozenset({
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "powershell_ise.exe", "wsl.exe",
    "wslhost.exe", "bash.exe", "ubuntu.exe", "mintty.exe", "putty.exe",
    "alacritty.exe", "wezterm-gui.exe", "kitty.exe", "tabby.exe",
    "hyper.exe", "conemu.exe", "conemu64.exe", "cmder.exe",
})
# A console program (python.exe, ssh.exe, ...) is shown in a console window
# whatever its own image is, so the class decides too.
TERMINAL_CLASSES = frozenset({
    "consolewindowclass", "cascadia_hosting_window_class", "pseudoconsolewindow",
    "mintty", "virtualconsoleclass",
})

# A HUD mask older than this is not trusted to still cover its card.
MASK_MAX_AGE_S = 1.0
# Pixels added on every side of a mask.
MASK_MARGIN = 8

SEE_ONLY_MESSAGE = (
    "This is a terminal window: you can look at it but not type or click in "
    "it. Run the command with your terminal tool instead.")
PROTECTED_MESSAGE = "This window is protected: it cannot be seen or controlled."


@dataclass(frozen=True)
class Window:
    """What the bridge read about one top-level window. None means unreadable."""
    image: Optional[str]            # owning process image, path or basename
    elevated: Optional[bool]        # that process's token is elevated
    window_class: Optional[str] = ""
    own: bool = False               # a window of Jarvis's own Windows helper


@dataclass(frozen=True)
class Rect:
    """Screen pixels; right and bottom are exclusive."""
    left: int
    top: int
    right: int
    bottom: int


def image_name(value) -> Optional[str]:
    """The folded basename of a process image, or None when it cannot be trusted.

    Windows ignores trailing dots and spaces in a file name and folds case, so
    `KeePass.EXE. ` is the same program as `keepass.exe`.
    """
    if not isinstance(value, str):
        return None
    name = value.replace("/", "\\").rsplit("\\", 1)[-1]
    name = name.rstrip(" .").casefold()
    if not name or name != name.strip() or any(ord(c) < 32 or c in '<>:"|?*' for c in name):
        return None
    return name if name.endswith(".exe") else name + ".exe"


def _class_name(value) -> Optional[str]:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    return value.strip().casefold()


def _images(values) -> frozenset:
    return frozenset(name for name in map(image_name, values or ()) if name)


def classify(window: Window, *, extra_password_managers: Iterable[str] = ()) -> str:
    """PROTECTED, SEE_ONLY or DRIVABLE for one window. Unreadable is PROTECTED."""
    if not isinstance(window, Window):
        return PROTECTED
    image = image_name(window.image)
    window_class = _class_name(window.window_class)
    if image is None or window_class is None:
        return PROTECTED
    if window.elevated is not False or window.own is not False:
        return PROTECTED
    if image in SYSTEM_PROMPT_IMAGES or window_class in SYSTEM_PROMPT_CLASSES:
        return PROTECTED
    if image in PASSWORD_MANAGER_IMAGES or image in _images(extra_password_managers):
        return PROTECTED
    if image in TERMINAL_IMAGES or window_class in TERMINAL_CLASSES:
        return SEE_ONLY
    return DRIVABLE


def may_capture(target: str, overlapping: Iterable[str] = ()) -> bool:
    """A window may be captured or read when it is not protected and no
    protected window lies over any part of it."""
    if target not in (SEE_ONLY, DRIVABLE):
        return False
    return all(kind in (SEE_ONLY, DRIVABLE) for kind in overlapping)


def may_input(target: str) -> bool:
    return target == DRIVABLE


def refusal(target: str) -> str:
    """The text an agent gets for an input it may not make; '' when it may."""
    if target == DRIVABLE:
        return ""
    return SEE_ONLY_MESSAGE if target == SEE_ONLY else PROTECTED_MESSAGE


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid(rect) -> bool:
    return (isinstance(rect, Rect) and all(map(_whole, (rect.left, rect.top, rect.right, rect.bottom)))
            and rect.left < rect.right and rect.top < rect.bottom)


def hud_view(sensitive, masks: Optional[Sequence[Rect]], age_s) -> tuple:
    """The HUD's class and the areas to hide (CU-D18).

    `sensitive` is how many approval cards and credential editors the daemon
    knows are showing. `masks` are the rectangles the HUD last reported for
    them and `age_s` how old that report is. The HUD stays drivable around
    its masks only when every sensitive thing has a fresh rectangle; a
    missing, stale, short or malformed report protects the whole window.
    """
    if not _whole(sensitive) or sensitive < 0:
        return PROTECTED, ()
    if sensitive == 0:
        return DRIVABLE, ()
    if (masks is None or isinstance(masks, (str, bytes)) or isinstance(age_s, bool)
            or not isinstance(age_s, (int, float)) or not 0 <= age_s <= MASK_MAX_AGE_S):
        return PROTECTED, ()
    try:
        masks = tuple(masks)
    except TypeError:
        return PROTECTED, ()
    if len(masks) < sensitive or not all(map(_valid, masks)):
        return PROTECTED, ()
    return DRIVABLE, tuple(
        Rect(m.left - MASK_MARGIN, m.top - MASK_MARGIN, m.right + MASK_MARGIN, m.bottom + MASK_MARGIN)
        for m in masks)


def masked(x, y, masks: Iterable[Rect]) -> bool:
    """True when a point may not be clicked or shown. Unreadable is masked."""
    if not (_whole(x) and _whole(y)):
        return True
    try:
        return any(m.left <= x < m.right and m.top <= y < m.bottom for m in masks)
    except (AttributeError, TypeError):
        return True


def overlaps(rect: Rect, masks: Iterable[Rect]) -> bool:
    """True when any part of `rect` (an element's bounds) lies under a mask."""
    if not _valid(rect):
        return True
    try:
        return any(rect.left < m.right and m.left < rect.right
                   and rect.top < m.bottom and m.top < rect.bottom for m in masks)
    except (AttributeError, TypeError):
        return True


_KEY_NAMES = {"del": "delete", "lshift": "shift", "rshift": "shift"}


def refused_keys(keys: Iterable[str]) -> bool:
    """True for a key press held to be destructive by itself: Shift+Delete
    (permanent delete, past the Recycle Bin), with any other keys beside it.
    `keys` are the names pressed together. Unreadable is refused."""
    try:
        names = {_KEY_NAMES.get(k.strip().casefold(), k.strip().casefold()) for k in keys}
    except (AttributeError, TypeError):
        return True
    return {"shift", "delete"} <= names
