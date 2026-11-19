"""Classify desktop actions and render their approval statements."""

from dataclasses import dataclass
from unicodedata import category, normalize


RUN, ASK, REFUSE = "run", "ask", "refuse"
GROUPS = ("pay", "send", "delete", "install", "confirm")

_PAY = (
    "pay", "buy", "purchase", "order", "checkout", "check out", "subscribe",
    "donate", "place order", "buy now", "pay now", "complete purchase",
    "add funds", "tip", "bid", "transfer", "withdraw",
)
_SEND = (
    "send", "submit", "post", "publish", "reply", "forward", "share", "tweet",
    "comment", "send message", "reply all", "email", "invite", "apply", "sign up",
    "register", "book", "reserve", "rsvp", "upload", "resend",
)
_DELETE = (
    "delete", "remove", "erase", "discard", "trash", "uninstall",
    "empty recycle bin", "clear all", "clear history", "format", "reset",
    "wipe", "unsubscribe", "cancel subscription", "cancel my subscription",
    "cancel membership", "cancel order", "cancel plan",
)
_INSTALL = (
    "install", "update", "upgrade", "run anyway", "allow access", "grant",
    "authorize", "allow", "enable", "turn off", "disable", "restart",
    "shut down", "sign out", "log out",
)
_CONFIRM = (
    "confirm", "agree", "accept", "i agree", "save changes", "continue anyway",
    "proceed", "finish", "approve",
)
_LABEL_GROUPS = tuple(zip(GROUPS, (_PAY, _SEND, _DELETE, _INSTALL, _CONFIRM)))
# Enter in these fields looks something up; it does not submit a form. "Address"
# counts only as the whole name (a browser or Explorer address box): Enter in
# an "Email address" field submits the form around it.
_SEARCH_TERMS = ("search", "find", "url", "filter", "look up")
_SEARCH_NAMES = ("address", "address bar", "address and search bar")
_TEXT_ENTRY = ("editable", "combobox", "document")
_HIDDEN_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_KNOWN_KEYS = (
    "ctrl", "control", "alt", "altgr", "shift", "win", "windows", "meta",
    "super", "cmd", "command", "enter", "return", "delete", "del", "backspace",
    "tab", "space", "spacebar", "escape", "esc", "insert", "ins", "home", "end",
    "pageup", "pagedown", "pgup", "pgdn", "up", "down", "left", "right",
    "arrowup", "arrowdown", "arrowleft", "arrowright", "capslock", "numlock",
    "scrolllock", "printscreen", "pause", "break", "menu",
    *"abcdefghijklmnopqrstuvwxyz0123456789",
    *(f"f{number}" for number in range(1, 25)),
)
_KEY_NAMES = {key: key.title() for key in _KNOWN_KEYS}


@dataclass(frozen=True)
class Action:
    kind: str
    app: str
    name: str = ""
    role: str = ""
    keys: tuple = ()
    submit: bool = False
    purpose: str = ""
    risky: str = ""


@dataclass(frozen=True)
class Verdict:
    decision: str
    group: str = ""
    reasons: tuple = ()


def _normalise(name):
    name = normalize("NFKC", name).casefold().replace("&", "")
    return " ".join(
        "".join(char if char.isalpha() or char.isdigit() else " " for char in name).split()
    )


def _contains_term(normalised, terms):
    padded = f" {normalised} "
    return any(f" {term} " in padded for term in terms)


def _role(role):
    return role.strip().casefold()


def label_group(name, role="") -> str:
    if not isinstance(name, str) or not isinstance(role, str):
        return ""
    if _role(role) in ("editable", "combobox", "search"):
        return ""
    name = _normalise(name)
    if len(name.split()) > 12 or not any(char.isalpha() for char in name):
        return ""
    for group, terms in _LABEL_GROUPS:
        if _contains_term(name, terms):
            return group
    return ""


def enter_asks(name, role) -> bool:
    if not isinstance(name, str) or not isinstance(role, str):
        return True
    role, plain = _role(role), _normalise(name)
    if role == "search":
        return False
    if role in _TEXT_ENTRY:
        return not (plain in _SEARCH_NAMES or _contains_term(plain, _SEARCH_TERMS))
    # Not a text field: Enter presses the focused control, so its label decides.
    return bool(label_group(name))


def _invalid_reasons(action):
    if not isinstance(action, Action):
        return ("invalid:action",)

    reasons = []
    if not isinstance(action.kind, str) or action.kind not in ("click", "type", "key", "scroll"):
        reasons.append("invalid:kind")
    purpose = action.purpose
    if (
        not isinstance(purpose, str)
        or not purpose.strip()
        or len(purpose) > 300
        or any(ord(char) < 32 or ord(char) == 127 for char in purpose)
    ):
        reasons.append("invalid:purpose")
    if not isinstance(action.risky, str) or action.risky not in ("yes", "no", "unsure"):
        reasons.append("invalid:risky")
    if not isinstance(action.keys, tuple) or not all(isinstance(key, str) for key in action.keys):
        reasons.append("invalid:keys")
    if not isinstance(action.submit, bool):
        reasons.append("invalid:submit")
    for field in ("app", "name", "role"):
        if not isinstance(getattr(action, field), str):
            reasons.append(f"invalid:{field}")
    return tuple(reasons)


def _key_name(key):
    return key.strip().casefold()


def decide(action) -> Verdict:
    invalid = _invalid_reasons(action)
    if invalid:
        return Verdict(REFUSE, reasons=invalid)

    group = ""
    reasons = []
    if action.kind == "click":
        group = label_group(action.name, action.role)
        if group:
            reasons.append(f"label:{group}")

    keys = {_key_name(key) for key in action.keys} if action.kind == "key" else set()
    presses_enter = bool(keys & {"enter", "return"}) or (action.kind == "type" and action.submit)
    if presses_enter and enter_asks(action.name, action.role):
        reasons.append("enter")
        group = label_group(action.name, action.role)
    if keys & {"delete", "del"}:
        reasons.append("key:delete")
        if group not in ("pay", "send"):
            group = "delete"

    if action.risky != "no":
        reasons.append(f"declared:{action.risky}")
    return Verdict(ASK if reasons else RUN, group, tuple(reasons))


def _clean_text(value):
    return "".join(char for char in value if category(char) not in _HIDDEN_CATEGORIES).replace('"', "'")


def _control_text(name):
    name = _clean_text(name).strip()
    if not name:
        return "an unlabeled control"
    if len(name) > 60:
        name = name[:59] + "…"
    return f'"{name}"'


def _app_text(app):
    app = "".join(
        char for char in _clean_text(app) if char.isalpha() or char.isdigit() or char in " .-_+()"
    )
    return app.strip()[:60].rstrip() or "an unknown app"


def _chord_text(keys, budget):
    chord = "+".join(_KEY_NAMES.get(_key_name(key), "a key") for key in keys) or "a key"
    if len(chord) > budget:
        end = chord.rfind("+", 0, budget - 1)
        return chord[:end] + "+…"
    return chord


def statement(action, verdict) -> str:
    try:
        if _invalid_reasons(action):
            return "An invalid desktop action"
        app = _app_text(action.app)
        if action.kind == "scroll":
            return f"Scroll in {app}"
        control = _control_text(action.name)
        if action.kind == "click":
            return f"Click {control} in {app}"
        if action.kind == "type":
            verb = "Type and press Enter" if action.submit else "Type"
            return f"{verb} in {control} in {app}"

        target = f" in {control} in {app}"
        chord = _chord_text(action.keys, 200 - len("Press ") - len(target))
        return f"Press {chord}{target}"
    except Exception:
        # A malformed Action subclass can raise even when reading fields.
        return "An invalid desktop action"


def voice_may_approve(verdict) -> bool:
    return isinstance(verdict, Verdict) and verdict.decision == ASK and verdict.group != "pay"
