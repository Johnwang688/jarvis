"""The fixed rules for an agent's action in a Browser pane (BR-9a). Pure.

Amendment 1 to the HUD Browser plan, §B.3 and §B.4
(`docs/plans/2026-10-11-hud-browser-plan-amendment-1.md`; its decisions file
wins over both). When an agent asks to click, type, press, scroll, select,
upload or navigate in one of the owner's Browser panes, `pre_judge(facts)`
answers first, before any model is asked:

  REFUSE    a hard refusal (H1–H12). No model can override it, and the
            classifier is **not even asked**. The reason is a fixed sentence
            per rule and never repeats page text.
  ASK       a fixed "always ask" floor: every upload; a submit of a login,
            two-factor or payment form; an address shaped like a login,
            OAuth, checkout, billing, security or account-deletion page; a
            link to an executable. The classifier can raise it to a deny, never
            lower it (`act_review.decide`).
  ALLOW     runs with no model call: scrolling, back and forward, the
            navigation keys, Backspace/Delete in an ordinary field, and up to
            64 characters typed into a search field.
  CLASSIFY  everything else: `act_review.judge` decides.

**Facts** are filled by the caller (BR-9b's gate, from the look and the
daemon's state). The state-dependent refusals — a card pending (H3), the owner
holding the pane (H4), the switches (H5), whether a window shows the tab (H6),
the URL judge (H7), another driver (H9), the budgets (H10), the strict profile
(H11), the desk (H12) — arrive as plain booleans and numbers, and this module
only reads them. Every one defaults to the value that refuses, so a `Facts`
nobody filled in is refused. H2 and H8 call into `secrets`,
`credential_patterns`, `protected_state`, `editor_policy` and the terminal
guard's value forms, never copying them: two copies of a rule drift, and the
looser one decides.

**Linear, always.** Labels and addresses are page- or agent-controlled and can
be any length. No regular expression here is built from page text; the fixed
ones never backtrack; a label is normalised with C-level passes (NFKC, a
translate table, fixed substitutions) and looked up with `in`; an address's
path segments go into sets. A one-megabyte label or a 100,000-character
address is judged in a few milliseconds (`tests/v2/browser_act_rules_check.py`
times both).

**Not a boundary.** A field whose label lies, a payment form drawn on a canvas,
a password field the page disguises as plain text, a homoglyph spelling of
"password": none of these is seen here. The card, desk-only driving, the
banner and the owner's takeover are the backstops (§B.11).

Not wired yet: BR-9b calls this from `hud_browser_act`'s gate.
"""
from __future__ import annotations

from dataclasses import dataclass
import functools
import os
from pathlib import Path
import re
import stat
import unicodedata
from urllib.parse import unquote, unquote_plus, urlsplit

from jarvis import config, credential_patterns, protected_state, untrusted
from jarvis.tools import secrets

# -- verdicts ------------------------------------------------------------------------------

REFUSE = "refuse"
ASK = "ask"
ALLOW = "allow"
CLASSIFY = "classify"

# The classifier's categories (§B.5) — one list, read by `act_review` too. Every
# one but `ordinary` is sensitive: a sensitive category is never allowed.
LOGIN = "login"
SIGNUP = "signup"
OAUTH = "oauth"
TWO_FACTOR = "two_factor"
PURCHASE = "purchase"
PAYMENT = "payment"
SEND_OR_POST = "send_or_post"
DELETE = "delete"
ACCOUNT_SECURITY = "account_security"
EXECUTABLE_DOWNLOAD = "executable_download"
UNRELATED = "unrelated_to_request"
INJECTION = "injection_suspected"
ORDINARY = "ordinary"
CATEGORIES = (LOGIN, SIGNUP, OAUTH, TWO_FACTOR, PURCHASE, PAYMENT, SEND_OR_POST, DELETE,
              ACCOUNT_SECURITY, EXECUTABLE_DOWNLOAD, UNRELATED, INJECTION, ORDINARY)
SENSITIVE = frozenset(CATEGORIES) - {ORDINARY}
# The upload floor's own category: an upload is asked about whatever it is.
UPLOAD = "upload"

ACTIONS = frozenset({"navigate", "back", "forward", "click", "type", "press", "scroll",
                     "select", "upload"})

# -- limits ----------------------------------------------------------------------------------

OWNER_QUIET_S = 5.0                 # H4: the owner's input this recently holds the pane
MAX_JUDGED_PER_TURN = 40            # H10: classifier-judged actions in one turn
MAX_ACTIONS_PER_TAB_HOUR = 200      # H10: actions on one tab in an hour
MAX_UPLOAD_BYTES = 50 * 1024 * 1024 # H8
SEARCH_TYPE_MAX = 64                # the ALLOW tier's short search
MAX_TYPE_CHARS = 10_000             # longer text to type is refused (bounds H2's scan)
MAX_FORM_FIELDS = 500               # a form with more controls is judged as holding every kind

# -- keys -----------------------------------------------------------------------------------

# `press` takes only these names (case-insensitive; the aliases on the right).
# A printable character is refused, so `press` can never be used to type.
NAV_KEYS = frozenset({"Tab", "Shift+Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft",
                      "ArrowRight", "PageUp", "PageDown", "Home", "End"})
EDIT_KEYS = frozenset({"Backspace", "Delete"})
JUDGED_KEYS = frozenset({"Enter", "Space"})
KEYS = NAV_KEYS | EDIT_KEYS | JUDGED_KEYS
_KEY_ALIASES = {"esc": "Escape", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
                "right": "ArrowRight", "return": "Enter", "del": "Delete", "pgup": "PageUp",
                "pgdn": "PageDown", "shift-tab": "Shift+Tab", "backtab": "Shift+Tab"}
_KEY_NAMES = {name.lower(): name for name in KEYS} | _KEY_ALIASES
# What an editing key does to a field: Backspace and Delete remove a character,
# Space types one. All three are refused on a sensitive field (H1).
_EDITING = frozenset({"Backspace", "Delete", "Space"})

SCROLLS = frozenset({"", "up", "down", "top", "bottom"})

# -- tables ----------------------------------------------------------------------------------

# H1: a field in a frame from one of these hosts (or a subdomain) is a payment
# field, whatever its own attributes say — the card fields of hosted checkouts
# live in such frames (Stripe Elements, Braintree Hosted Fields, Adyen …).
PAYMENT_FRAME_HOSTS = frozenset({
    "stripe.com", "stripe.network", "braintreegateway.com", "braintree-api.com",
    "braintreepayments.com", "adyen.com", "adyenpayments.com", "paypal.com",
    "paypalobjects.com", "squareup.com", "squareupsandbox.com", "squarecdn.com",
    "checkout.com", "authorize.net", "worldpay.com", "klarna.com", "mollie.com",
    "recurly.com", "chargebee.com", "razorpay.com", "spreedly.com", "cybersource.com",
    "bluesnap.com", "2checkout.com", "paddle.com", "affirm.com", "afterpay.com",
    "payments-amazon.com", "pay.google.com", "shopifyinc.com", "shopifycs.com",
    "gocardless.com", "paysafe.com", "payu.com",
})

# H1's label heuristics. A label, placeholder, aria-label, accessible name,
# `name` or `id` is normalised to lower-case words (camelCase and letter–digit
# edges split, everything else a space: `otp_code`, `otpCode` and `otp1` all
# hold the word `otp`), then these are looked up as whole words or phrases.
OTP_LABEL_WORDS = frozenset({"otp", "totp", "mfa", "2fa", "passcode", "onetime", "twofactor"})
OTP_LABEL_PHRASES = ("one time", "2 fa", "two factor", "verification code", "security code",
                     "pass code", "auth code", "authentication code")
PASSWORD_LABEL_WORDS = frozenset({"password", "passwd", "passphrase", "pin"})
PAYMENT_LABEL_WORDS = frozenset({"cvv", "cvc", "csc", "cardnumber", "ccnum", "ccnumber"})
PAYMENT_LABEL_PHRASES = ("card number", "card num", "cc number", "cc num", "credit card",
                         "debit card", "card code")
PAYMENT_LABEL_PREFIXES = ("expir",)          # expiry, expiration, expires
# A numeric field of 4–8 digits labelled only "code" is a one-time code —
# unless the label says what else it is.
CODE_WORDS = frozenset({"code"})
NOT_A_SECRET_CODE = frozenset({"zip", "postal", "post", "postcode", "promo", "promotion",
                               "coupon", "discount", "gift", "voucher", "referral", "invite",
                               "invitation", "area", "country", "dial", "product", "tracking",
                               "reference", "order", "sort", "routing"})

_AUTOCOMPLETE_PASSWORD = frozenset({"current-password", "new-password", "webauthn"})
_AUTOCOMPLETE_OTP = frozenset({"one-time-code"})

# §B.4: an address shaped like these is an ASK floor. (host, path, category): a
# host matches itself and its subdomains, "" any host; a path matches when its
# segments appear in a row anywhere in the address's path (each segment's own
# extension and `;` parameters ignored: `login.php` is `login`), "" any path.
ASK_URL_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("", "/oauth", OAUTH), ("", "/oauth2", OAUTH), ("", "/authorize", OAUTH),
    ("", "/consent", OAUTH),
    ("", "/login", LOGIN), ("", "/log-in", LOGIN), ("", "/log_in", LOGIN), ("", "/signin", LOGIN),
    ("", "/sign-in", LOGIN), ("", "/sign_in", LOGIN), ("", "/logon", LOGIN),
    ("", "/signup", SIGNUP), ("", "/sign-up", SIGNUP), ("", "/sign_up", SIGNUP),
    ("", "/register", SIGNUP), ("", "/registration", SIGNUP),
    ("", "/checkout", PURCHASE), ("", "/purchase", PURCHASE),
    ("", "/pay", PAYMENT), ("", "/payment", PAYMENT), ("", "/payments", PAYMENT),
    ("", "/payment-methods", PAYMENT), ("", "/billing", PAYMENT),
    ("", "/2fa", TWO_FACTOR), ("", "/mfa", TWO_FACTOR), ("", "/two-factor", TWO_FACTOR),
    ("", "/two_factor", TWO_FACTOR),
    ("", "/settings/security", ACCOUNT_SECURITY), ("", "/account/security", ACCOUNT_SECURITY),
    ("", "/settings/password", ACCOUNT_SECURITY), ("", "/account/password", ACCOUNT_SECURITY),
    ("", "/change-password", ACCOUNT_SECURITY), ("", "/reset-password", ACCOUNT_SECURITY),
    ("", "/account/delete", DELETE), ("", "/delete-account", DELETE),
    ("", "/account/close", DELETE), ("", "/settings/delete", DELETE),
    ("accounts.google.com", "", LOGIN), ("login.microsoftonline.com", "", LOGIN),
    ("login.live.com", "", LOGIN), ("appleid.apple.com", "", LOGIN),
    ("idmsa.apple.com", "", LOGIN), ("myaccount.google.com", "/security", ACCOUNT_SECURITY),
    ("github.com", "/login", LOGIN), ("github.com", "/session", LOGIN),
    ("github.com", "/sessions", LOGIN),
    ("checkout.stripe.com", "", PAYMENT), ("pay.google.com", "", PAYMENT),
    ("paypal.com", "/checkout", PAYMENT), ("paypal.com", "/checkoutnow", PAYMENT),
    ("paypal.com", "/webapps", PAYMENT),
)
# Chrome's own pages (sign-in, sync, settings) are an account-security action:
# always a card, never an agent's on its own (amendment §C.2).
ASK_SCHEMES = {"chrome": ACCOUNT_SECURITY, "chrome-untrusted": ACCOUNT_SECURITY}

# §B.4: a link (or an address) whose path ends in one of these asks, as
# `executable_download`. Matched case-insensitively (`.AppImage`).
EXECUTABLE_EXTENSIONS = (".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".deb", ".rpm",
                         ".appimage", ".dmg", ".pkg", ".jar", ".apk", ".scr",
                         ".msix", ".appx", ".vbs")
# A download an agent action started whose MIME type is one of these is held
# (§B.4), as is one whose name ends in an extension above (`executable_download`).
EXECUTABLE_MIME_TYPES = frozenset({
    "application/x-msdownload", "application/x-msdos-program", "application/x-msi",
    "application/x-ms-installer", "application/vnd.microsoft.portable-executable",
    "application/x-dosexec", "application/x-executable", "application/x-elf",
    "application/x-sharedlib", "application/x-sh", "application/x-shellscript",
    "text/x-shellscript", "application/x-bat", "application/x-csh",
    "application/vnd.debian.binary-package", "application/x-debian-package",
    "application/x-rpm", "application/x-redhat-package-manager",
    "application/x-apple-diskimage", "application/x-newton-compatible-pkg",
    "application/java-archive", "application/x-java-archive",
    "application/vnd.android.package-archive", "application/x-appimage",
    "application/vnd.ms-appx", "application/msix",
})

# -- refusal sentences: fixed, one per rule, never page text ----------------------------------

R_DESK = ("agents drive the Browser only on a turn the owner typed at the HUD, and never "
          "from a sub-agent")
R_STRICT = "this thread runs under the strict profile, which never drives the Browser"
R_ACTION = "that is not a Browser action"
R_KEY = ("press takes only a named key: Enter, Space, Tab, Shift+Tab, Escape, the arrows, "
         "PageUp, PageDown, Home, End, Backspace or Delete")
R_SCROLL = "scroll takes up, down, top or bottom"
R_TOO_LONG = "the text to type is longer than 10,000 characters"
R_SWITCH_GLOBAL = "the owner has turned off 'Agents may drive'"
R_SWITCH_SEE = "the owner has turned off 'Jarvis can see' for this pane"
R_SWITCH_PRIVATE = "this is a Private pane, and the owner has not turned on its drive switch"
R_SWITCH_DRIVE = "the owner has not let Jarvis drive this pane"
R_NOT_DRAWN = "no HUD window is showing this pane, so the owner could not watch the action"
R_DIRECT = "this pane shows a local page directly; ask the owner to choose Open in Remote"
R_CARD = "an approval card is waiting for the owner; nothing acts until it is answered"
R_PAUSED = "the owner took over this pane; it stays paused until they press Resume Jarvis"
R_OWNER_BUSY = "the owner is using this pane right now"
R_OTHER_DRIVER = "another chat is driving this pane"
R_BUDGET_TURN = "this turn has used its 40 judged Browser actions"
R_BUDGET_HOUR = "this pane has used its 200 Browser actions for the hour"
R_URL = "the Browser's address judge refuses that address"
R_JARVIS_PORT = "that address is one of Jarvis's own ports"
R_SCHEME = "agents navigate only to http and https addresses"
R_MALFORMED = "that address is malformed"
R_SENSITIVE = ("Jarvis never types into a password, one-time-code or payment-card field; "
               "the owner fills those")
R_CREDENTIAL = "the text or address would carry a credential"
R_USERINFO = "an address with a user name or password in it is refused"
R_UPLOAD_NONE = "an upload needs a file inside this chat's project folder"
R_UPLOAD_OUTSIDE = "the file to upload must be inside this chat's project folder"
R_UPLOAD_PROTECTED = ("the file to upload is credential or protected state, which never leaves "
                      "this machine through the Browser")
R_UPLOAD_NOT_FILE = "the file to upload does not exist or is not a regular file"
R_UPLOAD_SIZE = "the file to upload is larger than 50 MB"


@dataclass(frozen=True)
class Element:
    """One element as the look saw it: attributes and computed facts, **never
    a value** (there is no field for one). Every string is page-controlled."""
    tag: str = ""                  # lower-case tag name
    type: str = ""                 # the `type` attribute
    role: str = ""                 # ARIA role, explicit or implicit
    name: str = ""                 # accessible name, as the look computed it
    label: str = ""                # text of its <label>
    placeholder: str = ""
    aria_label: str = ""
    attr_name: str = ""            # the `name` attribute
    attr_id: str = ""              # the `id` attribute
    autocomplete: str = ""
    inputmode: str = ""
    maxlength: int | None = None
    editable: bool = False         # contenteditable
    href: str = ""                 # a link's resolved address
    submits: bool = False          # activating it submits its form
    heading: str = ""              # the nearest heading (the classifier's context)
    single_char_siblings: int = 0  # maxlength=1 inputs beside it, itself included
    frame_origin: str = ""         # origin of the frame holding it; "" is the top page


@dataclass(frozen=True)
class Form:
    """The target's form: how it submits and its controls' attributes (never
    their values)."""
    method: str = ""                       # as the DOM reports it: "get", "post", "dialog"
    action: str = ""                       # resolved action address
    fields: tuple[Element, ...] = ()
    frame_origins: tuple[str, ...] = ()    # origins of frames inside it (a Stripe card frame)


@dataclass(frozen=True)
class Facts:
    """One action and everything the rules read about it. Filled by the gate.

    `text` is the text to type, the option label to select or the scroll
    direction; `nav_url` is where `navigate` goes; `url` is the page's own
    address. The state flags default to the value that refuses."""
    action: str
    key: str = ""
    text: str = ""
    nav_url: str = ""
    url: str = ""
    title: str = ""
    target: Element | None = None
    form: Form | None = None
    context: str = ""              # visible text around the target (the classifier's)
    context_flagged: bool = False  # the look's credential guard flagged that text
    upload_path: str = ""          # `upload`'s file, as the agent named it
    project_dir: str = ""          # the chat's project folder
    profile: str = ""              # the Browser profile the tab is in
    # H3–H12, as the daemon knows them
    card_pending: bool = False             # H3: any approval card is pending
    paused: bool = False                   # H4: the owner took over the tab
    owner_input_age_s: float | None = None # H4: seconds since the owner's last input, if any
    agents_may_drive: bool = False         # H5: the global switch
    pane_can_drive: bool = False           # H5: the pane's "Jarvis can drive"
    pane_can_see: bool = False             # H5: the pane's "Jarvis can see"
    private: bool = False                  # H5: a Private pane (driving off unless switched on)
    tab_drawn: bool = False                # H6: a HUD window streams the tab
    direct_mode: bool = False              # H6: the pane shows a local page directly
    url_refused: bool = False              # H7: the URL judge or the gate refuses
    other_driver: bool = False             # H9: another chat drives this tab
    judged_this_turn: int = 0              # H10: classifier-judged actions so far this turn
    actions_this_hour: int = 0             # H10: actions on this tab in the last hour
    strict_profile: bool = False           # H11
    at_desk: bool = False                  # H12: `runtime.at_desk()`
    depth: int = 0                         # H12: `runtime.depth()`
    sub_agent: bool = False                # H12: a Claude sub-agent (`agent_id`)


@dataclass(frozen=True)
class PreVerdict:
    """`kind` is REFUSE, ASK, ALLOW or CLASSIFY; `rule` names what decided it
    (for logs and tests); `category` is an ASK's; `reason` a REFUSE's fixed
    sentence; `path` an upload's resolved file — the one to upload."""
    kind: str
    rule: str = ""
    category: str = ""
    reason: str = ""
    path: str = ""


def _refuse(rule: str, reason: str) -> PreVerdict:
    return PreVerdict(REFUSE, rule, reason=reason)


# -- normalising page text, linearly ------------------------------------------------------------

@functools.cache
def _unseen_table() -> dict[int, str | None]:
    """`untrusted.unseen`'s characters as a translate table: format characters
    (zero-width, bidi, tags) deleted — `pass\\u200bword` is `password` — and
    controls and line separators made spaces. Planes 0–2 and 14 hold every
    one; built once (~30 ms)."""
    table: dict[int, str | None] = {}
    for cp in [*range(0, 0x30000), *range(0xE0000, 0xE1000)]:
        ch = chr(cp)
        if untrusted.unseen(ch):
            table[cp] = None if unicodedata.category(ch) == "Cf" else " "
    return table


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DIGIT_EDGE = re.compile(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")
# After lower-casing: ASCII letters and digits stay, every other byte (and the
# `?` an unencodable character became) is a space.
_ASCII_WORD = bytes(c if (48 <= c <= 57 or 97 <= c <= 122) else 32 for c in range(256))
# An identifier (`name`, `id`) longer than this is not judged word by word:
# the field counts as sensitive. No real one is anywhere near it.
IDENT_CAP = 1024


def words(text: str, *, identifier: bool = False) -> str:
    """`text` as ` word word … ` (padded, single spaces): NFKC and invisible
    characters out (non-ASCII only), lower-case, everything but ASCII letters
    and digits a space. For an `identifier`, camelCase and letter–digit edges
    are split first (`otpCode`, `otp1` and `cardNumber` hold `otp`, `otp` and
    `card number`). Linear: C-level passes only, no regex from page text."""
    if not text:
        return " "
    if not text.isascii():
        text = unicodedata.normalize("NFKC", text).translate(_unseen_table())
    if identifier:
        text = _DIGIT_EDGE.sub(" ", _CAMEL.sub(" ", text).lower())
    data = text.lower().encode("ascii", "replace").translate(_ASCII_WORD)
    return " " + " ".join(data.decode("ascii").split()) + " "


def _has_word(padded: str, vocabulary) -> bool:
    return any(f" {word} " in padded for word in vocabulary)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return ""


def _host_in(host: str, hosts) -> bool:
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


# -- H1: sensitive fields ------------------------------------------------------------------------

PASSWORD, OTP, CARD = "password", "otp", "payment"


def sensitive_kind(el: Element | None) -> str | None:
    """`password`, `otp` or `payment` if Jarvis must never fill this field
    (H1), else None. Read from its type, its `autocomplete` tokens, its
    labels, split one-character boxes and the frame it sits in."""
    if el is None:
        return None
    if _host_in(_host(el.frame_origin), PAYMENT_FRAME_HOSTS):
        return CARD
    if el.type.strip().lower() == "password":
        return PASSWORD
    # `autocomplete` is a list of tokens (`section-x billing cc-number`): every
    # `cc-*` token is a card's, wherever it stands.
    tokens = " " + " ".join(el.autocomplete.lower().split()) + " "
    if " cc-" in tokens:
        return CARD
    if any(f" {token} " in tokens for token in _AUTOCOMPLETE_OTP):
        return OTP
    if any(f" {token} " in tokens for token in _AUTOCOMPLETE_PASSWORD):
        return PASSWORD
    if el.single_char_siblings >= 4:
        return OTP
    if len(el.attr_name) > IDENT_CAP or len(el.attr_id) > IDENT_CAP:
        return PASSWORD  # cannot be judged word by word: fail closed
    padded = words("\n".join((el.name, el.label, el.placeholder, el.aria_label))) + \
        words(el.attr_name + "\n" + el.attr_id, identifier=True)
    if _has_word(padded, PAYMENT_LABEL_WORDS) or any(f" {p} " in padded for p in PAYMENT_LABEL_PHRASES) \
            or any(f" {p}" in padded for p in PAYMENT_LABEL_PREFIXES):
        return CARD
    if _has_word(padded, OTP_LABEL_WORDS) or any(f" {p} " in padded for p in OTP_LABEL_PHRASES):
        return OTP
    if _has_word(padded, PASSWORD_LABEL_WORDS):
        return PASSWORD
    numeric = el.inputmode.strip().lower() == "numeric" or el.type.strip().lower() in ("tel", "number")
    if (numeric and el.maxlength is not None and 4 <= el.maxlength <= 8
            and _has_word(padded, CODE_WORDS) and not _has_word(padded, NOT_A_SECRET_CODE)):
        return OTP
    return None


class _Memo:
    """One judgement's `sensitive_kind` answers, so an element (a megabyte
    label, say) is read once however many rules ask about it."""

    def __init__(self) -> None:
        self._seen: dict[int, tuple[Element, str | None]] = {}

    def kind(self, el: Element | None) -> str | None:
        if el is None:
            return None
        hit = self._seen.get(id(el))
        if hit is None or hit[0] is not el:
            hit = self._seen[id(el)] = (el, sensitive_kind(el))
        return hit[1]


def form_kinds(form: Form | None, target: Element | None = None, *,
               memo: _Memo | None = None) -> frozenset[str]:
    """The sensitive kinds a form holds — its fields, frames inside it, split
    one-character boxes — plus the target's own (a password field with no
    <form> around it, as single-page apps draw them). A form with more than
    `MAX_FORM_FIELDS` controls or frames is judged as holding every kind."""
    memo = memo or _Memo()
    kinds: set[str] = set()
    kind = memo.kind(target)
    if kind:
        kinds.add(kind)
    if form is None:
        return frozenset(kinds)
    if len(form.fields) > MAX_FORM_FIELDS or len(form.frame_origins) > MAX_FORM_FIELDS:
        return frozenset({PASSWORD, OTP, CARD})
    singles = 0
    for field in form.fields:
        kind = memo.kind(field)
        if kind:
            kinds.add(kind)
        if field.maxlength == 1:
            singles += 1
    if singles >= 4:
        kinds.add(OTP)
    if any(_host_in(_host(origin), PAYMENT_FRAME_HOSTS) for origin in form.frame_origins):
        kinds.add(CARD)
    return frozenset(kinds)


def _form_category(kinds: frozenset[str], form: Form | None) -> str:
    if CARD in kinds:
        return PAYMENT
    if OTP in kinds:
        return TWO_FACTOR
    signup = form is not None and any("new-password" in f.autocomplete.lower().split()
                                      for f in form.fields[:MAX_FORM_FIELDS])
    return SIGNUP if signup else LOGIN


_TEXT_TYPES = frozenset({"", "text", "search", "email", "url", "tel", "number", "password"})


def _text_entry(el: Element | None) -> bool:
    """A field that takes typed text (clicking one only focuses it)."""
    if el is None:
        return False
    tag = el.tag.lower()
    if tag == "textarea" or el.editable:
        return True
    if tag == "input":
        return el.type.strip().lower() in _TEXT_TYPES
    return el.role.lower() in ("textbox", "searchbox", "combobox")


# -- H2: credentials in what the agent sends -------------------------------------------------------

def _carries_credential(texts, values) -> bool:
    from jarvis.v2 import terminal_guard  # the value forms (raw, URL-encoded, base64): not a copy

    forms = terminal_guard.value_forms(values)
    for text in texts:
        if not text:
            continue
        variants = {text}
        if not text.isascii():
            variants.add(text.translate(_unseen_table()))
        for variant in list(variants):
            if "%" in variant or "+" in variant:
                variants.add(unquote(variant))
                variants.add(unquote_plus(variant))
        for variant in variants:
            if terminal_guard.holds_value(variant, forms) or credential_patterns.holds_credential(variant):
                return True
    return False


# -- H7's belt and the address floors ---------------------------------------------------------------

def _address_refusal(url: str) -> str | None:
    """Our own belt under the URL judge (H7), for an address an agent sends
    the Browser to: Jarvis's ports, a host two parsers read differently, a
    scheme other than http(s) (Chrome's own pages ask instead), a user name
    or password in it."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return R_MALFORMED
    scheme = parts.scheme.lower()
    if scheme in ASK_SCHEMES:
        return None
    if scheme not in ("http", "https"):
        return R_SCHEME
    if config.ambiguous_authority(url) or not parts.hostname:
        return R_MALFORMED
    try:
        parts.port  # noqa: B018 — raises on a malformed port
    except ValueError:
        return R_MALFORMED
    if config.is_face_origin(url):
        return R_JARVIS_PORT
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return R_USERINFO
    return None


def _segments(path: str) -> list[str]:
    """A path's segments, decoded once, lower-case, each cut at its first `.`
    or `;` (`login.php` is `login`), empties dropped."""
    out = []
    for seg in unquote(path).lower().split("/"):
        seg = seg.split(";", 1)[0].split(".", 1)[0]
        if seg:
            out.append(seg)
    return out


def _shapes() -> tuple[list[tuple[str, tuple[str, ...], str]], set[int]]:
    # A host's own shapes are tried before the shapes for any host, so
    # `paypal.com/checkout` is a payment, not a purchase.
    shapes = sorted(((host, tuple(s for s in path.lower().split("/") if s), category)
                     for host, path, category in ASK_URL_SHAPES), key=lambda shape: not shape[0])
    return shapes, {len(segs) for _, segs, _ in shapes if segs}


_SHAPES, _SHAPE_LENGTHS = _shapes()


def url_shape(url: str) -> str | None:
    """The ASK floor category an address falls under, or None: Chrome's own
    pages, then `ASK_URL_SHAPES`. Linear in the address: its segments (and
    runs of two) go into sets; each shape is one lookup."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ASK_SCHEMES:
        return ASK_SCHEMES[scheme]
    segs = _segments(parts.path)
    runs: dict[int, set[tuple[str, ...]]] = {}
    for n in _SHAPE_LENGTHS:
        runs[n] = set(zip(*(segs[i:] for i in range(n)))) if n > 1 else {(s,) for s in segs}
    for shape_host, shape_segs, category in _SHAPES:
        if shape_host and not _host_in(host, (shape_host,)):
            continue
        if not shape_segs or shape_segs in runs.get(len(shape_segs), ()):
            return category
    return None


def is_executable_path(url: str) -> bool:
    """Does this address's path end in an executable's extension?"""
    try:
        path = unquote(urlsplit(url).path).lower().rstrip("/")
    except ValueError:
        return False
    return path.endswith(EXECUTABLE_EXTENSIONS)


def executable_download(name: str, mime: str = "") -> bool:
    """For BR-9b's download hold (§B.4): is a download an agent action
    started executable, by its file name or its MIME type?"""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().rstrip(". ").lower()
    kind = (mime or "").split(";", 1)[0].strip().lower()
    return base.endswith(EXECUTABLE_EXTENSIONS) or kind in EXECUTABLE_MIME_TYPES


# -- H8: the upload's file ----------------------------------------------------------------------------

def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def upload_refusal(upload_path: str, project_dir: str) -> tuple[str | None, str]:
    """(fixed sentence, "") if this file may not be uploaded, else (None,
    resolved path). Only a regular file inside the chat's project folder (no
    `..` out, no symlink out), at most 50 MB, that is no credential file
    (`secrets.is_credential`: the bundles and the owner's `.env*` rule,
    symlinks followed), not in a credential folder, a login file or a v2
    session carrier (`editor_policy`'s withheld rule, which reads
    `config.V2_CREDENTIAL_DIRS`), not the gate's state
    (`protected_state.protected_paths`), not in Jarvis's own config and data
    folders, and not inside a `.git` folder (a remote URL there can carry a
    token)."""
    from jarvis.v2 import editor_policy  # one classifier, called: not copied

    if not upload_path or not project_dir:
        return R_UPLOAD_NONE, ""
    try:
        root = Path(os.path.abspath(os.path.expanduser(project_dir)))
        root_real = Path(os.path.realpath(root))
        raw = Path(os.path.abspath(os.path.join(root, os.path.expanduser(upload_path))))
        resolved = Path(os.path.realpath(raw))
    except (OSError, ValueError):
        return R_UPLOAD_OUTSIDE, ""
    if raw == root or resolved == root_real or not _under(raw, root) or not _under(resolved, root_real):
        return R_UPLOAD_OUTSIDE, ""
    try:
        policy = editor_policy.Policy()
        protected = (secrets.is_credential(raw) or secrets.is_credential(resolved)
                     or policy.withheld_anywhere(raw, resolved)
                     or resolved in protected_state.protected_paths()
                     or resolved in protected_state.gate_paths()
                     or any(_under(p, d) for d in editor_policy.jarvis_dirs() for p in (raw, resolved))
                     or ".git" in raw.parts or ".git" in resolved.parts)
    except Exception:  # a guard that cannot tell refuses
        protected = True
    if protected:
        return R_UPLOAD_PROTECTED, ""
    try:
        info = os.stat(resolved)
    except OSError:
        return R_UPLOAD_NOT_FILE, ""
    if not stat.S_ISREG(info.st_mode):
        return R_UPLOAD_NOT_FILE, ""
    if info.st_size > MAX_UPLOAD_BYTES:
        return R_UPLOAD_SIZE, ""
    return None, str(resolved)


# -- the judgement -----------------------------------------------------------------------------------

def _key_name(key: str) -> str | None:
    if not isinstance(key, str) or len(key) > 16:
        return None
    return _KEY_NAMES.get(key.strip().lower())


def _state_refusal(f: Facts) -> PreVerdict | None:
    """H12, H11, H5, H6, H3, H4, H9 and the hourly half of H10: the daemon's
    facts, only read here."""
    if not f.at_desk or f.depth != 0 or f.sub_agent:
        return _refuse("H12", R_DESK)
    if f.strict_profile:
        return _refuse("H11", R_STRICT)
    if not f.agents_may_drive:
        return _refuse("H5", R_SWITCH_GLOBAL)
    if not f.pane_can_see:
        return _refuse("H5", R_SWITCH_SEE)
    if f.private and not f.pane_can_drive:
        return _refuse("H5", R_SWITCH_PRIVATE)
    if not f.pane_can_drive:
        return _refuse("H5", R_SWITCH_DRIVE)
    if f.direct_mode:
        return _refuse("H6", R_DIRECT)
    if not f.tab_drawn:
        return _refuse("H6", R_NOT_DRAWN)
    if f.card_pending:
        return _refuse("H3", R_CARD)
    if f.paused:
        return _refuse("H4", R_PAUSED)
    # Numbers are compared so that one that is not a number refuses (NaN).
    if f.owner_input_age_s is not None and not f.owner_input_age_s >= OWNER_QUIET_S:
        return _refuse("H4", R_OWNER_BUSY)
    if f.other_driver:
        return _refuse("H9", R_OTHER_DRIVER)
    if not f.actions_this_hour < MAX_ACTIONS_PER_TAB_HOUR:
        return _refuse("H10", R_BUDGET_HOUR)
    return None


def _destinations(f: Facts, key: str | None) -> list[str]:
    """Where this action may take the page: `navigate`'s address, a clicked
    link's, a submitted form's action."""
    out = []
    if f.action == "navigate":
        out.append(f.nav_url)
    target = f.target
    if f.action == "click" and target is not None and target.href:
        out.append(target.href)
    submits = (f.action == "click" and target is not None and target.submits) or \
              (f.action == "press" and key == "Enter")
    if submits and f.form is not None and f.form.action:
        out.append(f.form.action)
    return out


def pre_judge(f: Facts, *, extra_values=()) -> PreVerdict:
    """The fixed rules, in order: H1–H12, then the ASK floors, then the ALLOW
    tier; everything else is CLASSIFY. `extra_values` are credential values
    the caller knows beyond `secrets.secret_values()` (the project folder's
    env files, say); they only ever add."""
    refusal = _state_refusal(f)
    if refusal:
        return refusal
    action = f.action if isinstance(f.action, str) else ""
    if action not in ACTIONS:
        return _refuse("action", R_ACTION)
    key = _key_name(f.key) if action == "press" else None
    if action == "press" and key is None:
        return _refuse("action", R_KEY)
    if action == "scroll" and f.text.strip().lower() not in SCROLLS:
        return _refuse("action", R_SCROLL)
    if action == "type" and len(f.text) > MAX_TYPE_CHARS:
        return _refuse("action", R_TOO_LONG)

    # H7: the URL judge (a fact) and our belt under it, for an address the
    # agent sends the page to.
    if f.url_refused:
        return _refuse("H7", R_URL)
    if action == "navigate":
        belt = _address_refusal(f.nav_url)
        if belt:
            return _refuse("H7", belt)
    if action == "click" and f.target is not None and f.target.href:
        if config.ambiguous_authority(f.target.href):
            return _refuse("H7", R_MALFORMED)
        if config.is_face_origin(f.target.href):
            return _refuse("H7", R_JARVIS_PORT)

    # H1: filling a sensitive field (clicking into one is allowed).
    target = f.target
    memo = _Memo()
    fills = action in ("type", "select") or (action == "press" and key in _EDITING)
    if fills and memo.kind(target):
        return _refuse("H1", R_SENSITIVE)

    # H2: what the agent would type or send.
    sent = []
    if action in ("type", "select"):
        sent.append(f.text)
    sent.extend(d for d in _destinations(f, key) if d)
    if sent:
        values = list(secrets.secret_values()) + [v for v in extra_values if isinstance(v, str)]
        if _carries_credential(sent, values):
            return _refuse("H2", R_CREDENTIAL)

    # H8: the upload's file.
    resolved = ""
    if action == "upload":
        why, resolved = upload_refusal(f.upload_path, f.project_dir)
        if why:
            return _refuse("H8", why)

    # The ASK floors (§B.4).
    # The floors and CLASSIFY are judged (the reviewer is asked, §B.5), so
    # they count against the turn's budget; the ALLOW tier does not.
    spent = not f.judged_this_turn < MAX_JUDGED_PER_TURN
    floor = _floor(f, action, key, target, resolved, memo)
    if floor is not None:
        return _refuse("H10", R_BUDGET_TURN) if spent else floor

    # The ALLOW tier.
    if action in ("scroll", "back", "forward"):
        return PreVerdict(ALLOW, "allow:" + action)
    if action == "press" and key in NAV_KEYS:
        return PreVerdict(ALLOW, "allow:nav-key")
    if action == "press" and key in EDIT_KEYS and target is not None:
        return PreVerdict(ALLOW, "allow:edit-key")  # a sensitive field was refused above
    if action == "type" and _short_search(f, memo):
        return PreVerdict(ALLOW, "allow:search")

    if spent:
        return _refuse("H10", R_BUDGET_TURN)
    return PreVerdict(CLASSIFY, "classify")


def _floor(f: Facts, action: str, key: str | None, target: Element | None,
           resolved: str, memo: _Memo) -> PreVerdict | None:
    if action == "upload":
        return PreVerdict(ASK, "floor:upload", UPLOAD, path=resolved)
    # A submit of a login, two-factor or payment form: a click on anything in
    # it but a text field (clicking into a field only focuses it), Enter
    # anywhere in it, Space on a button in it.
    submitting = (action == "click" and target is not None and not _text_entry(target)) or \
                 (action == "press" and key == "Enter") or \
                 (action == "press" and key == "Space" and not _text_entry(target))
    if submitting and (f.form is not None or target is not None):
        kinds = form_kinds(f.form, target, memo=memo)
        if kinds:
            return PreVerdict(ASK, "floor:form", _form_category(kinds, f.form))
    for url in _destinations(f, key):
        category = url_shape(url)
        if category:
            return PreVerdict(ASK, "floor:url", category)
        if action in ("click", "navigate") and is_executable_path(url):
            return PreVerdict(ASK, "floor:executable", EXECUTABLE_DOWNLOAD)
    return None


def _short_search(f: Facts, memo: _Memo) -> bool:
    """At most 64 characters, no line break, into a search field: `type=search`,
    `role=searchbox`, or a text field of a GET form — and never one whose form
    holds a password, one-time-code or payment field."""
    target = f.target
    if target is None or not _text_entry(target) or len(f.text) > SEARCH_TYPE_MAX:
        return False
    if "\n" in f.text or "\r" in f.text:
        return False
    if form_kinds(f.form, target, memo=memo):
        return False
    if target.type.strip().lower() == "search" or target.role.lower() == "searchbox":
        return True
    return f.form is not None and f.form.method.strip().lower() == "get"
