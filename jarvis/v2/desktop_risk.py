"""Classify desktop actions and render their approval statements."""

from dataclasses import dataclass
from unicodedata import category, normalize


RUN, ASK, REFUSE = "run", "ask", "refuse"
GROUPS = ("pay", "send", "delete", "install", "confirm")

_PAY = (
    "pay", "buy", "purchase", "checkout", "check out", "subscribe", "donate",
    "place order", "order now", "complete order", "submit order", "buy now",
    "pay now", "complete purchase", "add funds", "send tip", "add tip", "bid",
    "transfer", "withdraw", "payment", "make a payment", "renew", "top up",
    "start free trial", "rent",
)
_SEND = (
    "send", "submit", "post", "publish", "reply", "reply all", "share", "tweet",
    "comment", "send message", "send email", "invite", "apply now",
    "submit application", "sign up", "register", "book now", "reserve", "rsvp",
    "upload", "resend", "merge", "call",
)
_DELETE = (
    "delete", "remove", "erase", "discard", "trash", "uninstall",
    "empty recycle bin", "clear all", "clear history", "clear data", "clear cache",
    "format drive", "format disk", "reset", "wipe", "unsubscribe",
    "cancel subscription", "cancel my subscription", "cancel membership",
    "cancel order", "cancel plan", "don t save", "overwrite", "replace the file",
    "replace the files", "deactivate", "close account",
)
_INSTALL = (
    "install", "update", "upgrade", "run anyway", "allow access", "grant",
    "authorize", "allow", "enable", "turn off", "disable", "restart",
    "shut down", "shutdown", "sign out", "signout", "log out", "logout",
    "log off", "add to chrome", "add extension", "trust",
)
_CONFIRM = (
    "confirm", "agree", "accept", "i agree", "save changes", "continue anyway",
    "proceed", "finish", "approve",
)
_LABEL_GROUPS = tuple(zip(GROUPS, (_PAY, _SEND, _DELETE, _INSTALL, _CONFIRM)))
_LOOKUP_WORDS = frozenset(("search", "find", "filter"))
_LOOKUP_NAMES = frozenset((
    "address", "address bar", "address and search bar", "url", "enter url", "enter a url",
))
_TEXT_ENTRY = frozenset(("editable", "edit", "textbox", "text", "richedit", "combobox", "document"))
_SEARCH = frozenset(("search", "searchbox"))
_PLAIN = frozenset((
    "button", "splitbutton", "link", "hyperlink", "menuitem", "menu", "listitem",
    "list", "tab", "tabitem", "checkbox", "radio", "radiobutton", "treeitem",
    "toolbar", "image", "cell", "row", "option", "switch",
))

_MATCH_HIDDEN = frozenset(("Cf", "Cs", "Mn", "Me", "Cc"))
_CARD_HIDDEN = _MATCH_HIDDEN | {"Zl", "Zp"}
_PURPOSE_INVALID = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))
_BLANK_CHARACTERS = frozenset("\u034f\u115f\u1160\u3164\uffa0\u2800")
_DOUBLE_QUOTES = frozenset('"\uff02\u02ba\u201c\u201d\u201e\u201f\u00ab\u00bb\u2033\u3003')
_ASCII_ALPHANUMERIC = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

KEYS = frozenset((
    "ctrl", "control", "alt", "altgr", "shift", "win", "windows", "meta",
    "super", "cmd", "command", "enter", "return", "delete", "del", "backspace",
    "tab", "space", "spacebar", "escape", "esc", "insert", "ins", "home", "end",
    "pageup", "pagedown", "pgup", "pgdn", "up", "down", "left", "right",
    "arrowup", "arrowdown", "arrowleft", "arrowright", "capslock", "numlock",
    "scrolllock", "printscreen", "pause", "break", "menu", "plus", "minus",
    *"abcdefghijklmnopqrstuvwxyz0123456789",
    *(f"f{number}" for number in range(1, 25)),
))
_KEY_NAMES = {key: key.title() for key in KEYS}
_SHORTCUT_MODIFIERS = frozenset((
    "ctrl", "control", "alt", "altgr", "win", "windows", "meta", "super", "cmd", "command",
))
_ALLOWED_SHORTCUTS = {
    "ctrl": frozenset((
        *"acvxzyfltnrsp0", "home", "end", "left", "right", "up", "down", "tab",
        "pageup", "pagedown", "plus", "minus", "backspace",
    )),
    "alt": frozenset(("left", "right", "tab", "home")),
}
_ENTER_KEYS = frozenset(("enter", "return"))
_SPACE_KEYS = frozenset(("space", "spacebar"))
_DELETE_KEYS = frozenset(("delete", "del"))
_VOICE_GROUPS = ("", *GROUPS[1:])
_CARD_LIMIT = 200
_INVALID_CARD = "An invalid desktop action"


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


def _strip_hidden(value, categories):
    return "".join(
        char for char in value if char not in _BLANK_CHARACTERS and category(char) not in categories
    )


def _normalise(name):
    clean = _strip_hidden(normalize("NFKC", name), _MATCH_HIDDEN)
    folded = _strip_hidden(clean.casefold(), _MATCH_HIDDEN).replace("&", "")
    return " ".join("".join(char if char.isalnum() else " " for char in folded).split())


def _contains_term(normalised, terms):
    padded = f" {normalised} "
    return any(f" {term} " in padded for term in terms)


def label_group(name, role="") -> str:
    if type(name) is not str or type(role) is not str:
        return ""
    if role.strip().casefold() in _TEXT_ENTRY | _SEARCH:
        return ""
    if len(_strip_hidden(name, _MATCH_HIDDEN).split()) > 12:
        return ""
    plain = _normalise(name)
    for group, terms in _LABEL_GROUPS:
        if _contains_term(plain, terms):
            return group
    return ""


def _enter_asks(name, role, group):
    if role in _SEARCH:
        return False
    if role in _PLAIN:
        return bool(group)
    plain = _normalise(name)
    first_word = plain.partition(" ")[0]
    return not (
        first_word in _LOOKUP_WORDS or plain in _LOOKUP_NAMES
        or plain == "look up" or plain.startswith("look up ")
    )


def enter_asks(name, role) -> bool:
    if type(name) is not str or type(role) is not str:
        return True
    role = role.strip().casefold()
    return _enter_asks(name, role, label_group(name) if role in _PLAIN else "")


def _read_action(action):
    if type(action) is not Action:
        return None, ("invalid:action",)
    try:
        kind, app, name, role = action.kind, action.app, action.name, action.role
        keys, submit, purpose, risky = action.keys, action.submit, action.purpose, action.risky
    except Exception:
        return None, ("invalid:action",)

    reasons = []
    if type(kind) is not str or kind not in ("click", "type", "key", "scroll"):
        reasons.append("invalid:kind")
    if (
        type(purpose) is not str
        or len(purpose) > 300
        or not purpose.strip()
        or not any(char.isalpha() or char.isdigit() for char in purpose)
        or any(category(char) in _PURPOSE_INVALID for char in purpose)
    ):
        reasons.append("invalid:purpose")
    if type(risky) is not str or risky not in ("yes", "no", "unsure"):
        reasons.append("invalid:risky")
    if type(keys) is not tuple or any(type(key) is not str for key in keys):
        reasons.append("invalid:keys")
    elif type(kind) is str and kind == "key":
        keys = tuple(key.strip().casefold() for key in keys)
        if not keys or any(key not in KEYS for key in keys):
            reasons.append("invalid:keys")
    elif keys:
        reasons.append("invalid:keys")
    if type(submit) is not bool or (submit and (type(kind) is not str or kind != "type")):
        reasons.append("invalid:submit")
    for field, value in (("app", app), ("name", name), ("role", role)):
        if type(value) is not str:
            reasons.append(f"invalid:{field}")
    return (kind, app, name, role, keys, submit, risky), tuple(reasons)


def _read_verdict(verdict):
    if type(verdict) is not Verdict:
        return None
    try:
        decision, group, reasons = verdict.decision, verdict.group, verdict.reasons
    except Exception:
        return None
    if (
        type(decision) is str and decision in (RUN, ASK, REFUSE)
        and type(group) is str and group in ("", *GROUPS)
        and type(reasons) is tuple and all(type(reason) is str for reason in reasons)
    ):
        return decision, group
    return None


def _shortcut_asks(keys, key_set):
    if len(keys) <= 1 or not key_set & _SHORTCUT_MODIFIERS:
        return False
    if keys.count("shift") > 1:
        return True
    chord = tuple(key for key in keys if key != "shift")
    if len(chord) == 2:
        for modifier, key in (chord, chord[::-1]):
            if modifier == "control":
                modifier = "ctrl"
            if key in _ALLOWED_SHORTCUTS.get(modifier, ()):
                return False
    return True


def decide(action) -> Verdict:
    fields, invalid = _read_action(action)
    if invalid:
        return Verdict(REFUSE, reasons=invalid)
    kind, _, name, role, keys, submit, risky = fields
    role = role.strip().casefold()
    reasons = []
    groups = set()
    if kind == "click":
        group = label_group(name, role)
        if group:
            reasons.append(f"label:{group}")
            groups.add(group)

    key_set = set(keys)
    shortcut = _shortcut_asks(keys, key_set)
    if shortcut:
        reasons.append("shortcut")
    presses_enter = submit or bool(key_set & _ENTER_KEYS)
    presses_space = role in _PLAIN and bool(key_set & _SPACE_KEYS)
    if presses_enter or presses_space:
        group = label_group(name)
        if presses_enter and (shortcut or _enter_asks(name, role, group)):
            reasons.append("enter")
            groups.add(group)
        if presses_space and group:
            reasons.append("press")
            groups.add(group)
    if key_set & _DELETE_KEYS and role not in _TEXT_ENTRY | _SEARCH:
        reasons.append("key:delete")
        groups.add("delete")

    if risky != "no":
        reasons.append(f"declared:{risky}")
    group = next((group for group in GROUPS if group in groups), "")
    return Verdict(ASK if reasons else RUN, group, tuple(reasons))


def _control_name(name):
    clean = _strip_hidden(name, _CARD_HIDDEN)
    return "".join(
        "'" if char in _DOUBLE_QUOTES or category(char) in ("Pi", "Pf") else char
        for char in clean
    ).strip()


def _ellipsise(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _control_text(name, limit=60):
    return f'"{_ellipsise(name, limit)}"' if name else "an unlabeled control"


def _app_text(app):
    clean = _strip_hidden(app, _CARD_HIDDEN)
    allowed = "".join(char for char in clean if char.isalpha() or char.isdigit() or char in " .-_+()")
    app = allowed.strip()[:60].rstrip()
    return app if _ASCII_ALPHANUMERIC.intersection(app) else "an unknown app"


def statement(action, verdict) -> str:
    fields, invalid = _read_action(action)
    if invalid:
        return _INVALID_CARD
    kind, app, name, _, keys, submit, _ = fields
    app = _app_text(app)
    result = _read_verdict(verdict)
    suffix = f" ({result[1]})" if result and result[1] else ""
    if kind == "scroll":
        return f"Scroll in {app}{suffix}"

    name = _control_name(name)
    target = f" in {app}{suffix}"
    control = _control_text(name)
    if kind == "click":
        return f"Click {control}{target}"
    if kind == "type":
        verb = "Type and press Enter" if submit else "Type"
        return f"{verb} in {control}{target}"

    chord = "+".join(_KEY_NAMES[key] for key in keys)
    card = f"Press {chord} in {control}{target}"
    if len(card) > _CARD_LIMIT:
        budget = _CARD_LIMIT - len(f"Press  in {control}{target}")
        end = chord.rfind("+", 0, budget - 1)
        chord = chord[:end] + "+…" if end > 0 else _ellipsise(chord, budget)
        card = f"Press {chord} in {control}{target}"
    return card


def voice_may_approve(verdict) -> bool:
    result = _read_verdict(verdict)
    return result is not None and result[0] == ASK and result[1] in _VOICE_GROUPS
