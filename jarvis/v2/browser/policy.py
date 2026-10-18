"""The Browser's navigation policy (BR-1a, plan §4): the URL judge and the rule
the SOCKS gate applies to every connection. Pure: no I/O, no DNS, no clock.

Three layers keep the owner's daemon-owned Chrome off Jarvis's control plane
and, unless he says otherwise, off his home network. The second is the
boundary; the other two say why in words and catch what never touches the
network.

1. **The URL judge** (`judge_url`): every URL typed into the Browser, every
   "Open in Browser" and every stored URL, before Chrome is asked to load it.
   Only `http`, `https` and `about:blank`; never a user name or password in
   the address; never one of Jarvis's own ports in any loopback spelling;
   never a host two parsers would read differently (`config.
   ambiguous_authority`). It is the only layer that sees `file:`,
   `chrome:` and friends — a browser-initiated load of those never touches
   the network, so the gate cannot.
2. **The gate** (`gate.py`, with `judge_port` and `judge_address` here):
   every TCP connection Chrome makes — pages, redirects, frames, fetch, XHR,
   WebSockets, workers and Chrome's own background traffic — is a SOCKS5
   CONNECT the gate judges. Chrome sends every host as a name, IP literals
   included (measured, Chrome 155), so the gate parses literals itself,
   resolves real names exactly once and connects to the address it judged.
   This machine is closed like the LAN: a loopback port is reachable only
   once the owner opened it in a pane (`judge_address`), and a name keeps
   the address class it first resolved to for the host's lifetime
   (`gate.Gate`'s pins), so rebinding gets nowhere with the LAN on either.
   WebRTC sends no UDP at all (`host.launch_args`).
3. **The landing guard** (`host.py`): wherever a page's main frame lands,
   judged again by `judge_url(..., landed=True)`, and replaced with an
   explanation when it is refused.

`refused_ports` is the one port list: `config.control_ports()` (BR-0's single
Python definition: 8402, 8404, 8405, 8406, configured and default) plus the
HUD and API ports this daemon reports, plus the workshop's 8403 unless it is
this daemon's own workshop — `/status` → `refused_ports` reports it, and the
HUD's Direct judge reads that list.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from jarvis import config, untrusted

# The workshop's default port (`config.WORKSHOP_PORT` when unset). The gate
# admits the workshop only as the daemon's *reported* workshop (WP-E), so a
# test daemon's Browser can never reach the owner's live one on 8403.
DEFAULT_WORKSHOP_PORT = 8403

MAX_URL = 16 * 1024
SCHEMES = ("http", "https")
_LANDED_OK = ("chrome-error://chromewebdata",)

# Refusal classes: what the gate counts and logs (never a host, never a URL).
JARVIS_PORT = "jarvis_port"
OWN_PORT = "own_port"
PRIVATE_NETWORK = "private_network"
NAME_TO_LOOPBACK = "name_to_loopback"
NOT_A_HOST = "not_a_host"
UNRESOLVABLE = "unresolvable"
BAD_REQUEST = "bad_request"
UNSUPPORTED = "unsupported"
BUSY = "busy"
LOOPBACK_CLOSED = "loopback_closed"
REBINDING = "rebinding"
DNS_BUSY = "dns_busy"
PINS_FULL = "pins_full"

# The judge's sentences. Fixed text: a refusal is logged by the daemon's
# handler (`APIError`), and no URL may reach a log line (plan §4).
SCHEME_SENTENCE = "only http, https and about:blank addresses open in the Browser"
USERINFO_SENTENCE = "an address with a user name or password in it is refused"
JARVIS_PORT_SENTENCE = "that address is one of Jarvis's own ports"
MALFORMED_SENTENCE = "that address is malformed"
LONG_SENTENCE = "that address is too long"
CONTROL_SENTENCE = "that address contains invisible or control characters"


class URLRefused(ValueError):
    """A URL the Browser will not load: `reason` is the class, `str()` the
    owner's sentence. Neither ever contains the URL."""

    def __init__(self, reason: str, sentence: str):
        super().__init__(sentence)
        self.reason = reason


def refused_ports(*, hud_port: int | None = None, api_port: int | None = None,
                  workshop_port: int | None = None) -> frozenset[int]:
    """Every port the Browser may never reach on this machine: BR-0's control
    ports, the HUD and API ports this daemon reports, and the workshop's
    default port unless it is this daemon's workshop. Read at call time, so a
    test that repoints a port is covered."""
    ports = set(config.control_ports())
    ports |= {p for p in (hud_port, api_port) if p}
    ports |= {DEFAULT_WORKSHOP_PORT, config.WORKSHOP_PORT} - {workshop_port}
    ports.discard(0)
    return frozenset(ports)


# -- layer 1: the URL judge --------------------------------------------------------

def _refuse(reason: str, sentence: str):
    raise URLRefused(reason, sentence)


def judge_url(url, *, refused: frozenset[int] = frozenset(), landed: bool = False) -> str:
    """The URL to load, or `URLRefused`.

    `landed=False` judges what the owner (or a stored pane) asks for: exactly
    `about:blank`, or an absolute `http`/`https` URL. `landed=True` judges
    where a page's main frame actually is, which may also be `about:blank`
    with a query or fragment, Chrome's own error page, or a `blob:` URL of an
    http(s) origin (a page's own content). `refused` adds the daemon's
    reported ports to `config.control_ports()`, which is always refused."""
    if not isinstance(url, str):
        _refuse("malformed", MALFORMED_SENTENCE)
    text = url.strip(" \t\r\n")
    if not text:
        _refuse("malformed", MALFORMED_SENTENCE)
    if len(text) > MAX_URL:
        _refuse("malformed", LONG_SENTENCE)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or untrusted.unseen(ch) for ch in text):
        _refuse("malformed", CONTROL_SENTENCE)
    lower = text.lower()
    if lower == "about:blank":
        return "about:blank"
    if landed:
        if lower.startswith(("about:blank#", "about:blank?")):
            return text
        if lower.startswith(_LANDED_OK):
            return text
        if lower.startswith("blob:"):
            judge_url(text[5:], refused=refused, landed=False)
            return text
    try:
        parts = urlsplit(text)
    except ValueError:
        _refuse("malformed", MALFORMED_SENTENCE)
    if parts.scheme.lower() not in SCHEMES:
        _refuse("scheme", SCHEME_SENTENCE)
    if config.ambiguous_authority(text):
        _refuse("malformed", MALFORMED_SENTENCE)
    if "@" in parts.netloc:
        _refuse("userinfo", USERINFO_SENTENCE)
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        _refuse("malformed", MALFORMED_SENTENCE)
    if not host:
        _refuse("malformed", MALFORMED_SENTENCE)
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    # BR-0's own test first (every loopback spelling, every control port),
    # then this daemon's reported ports under the same spellings.
    if config.is_face_origin(text) or (port in refused and config.claims_this_machine(host)):
        _refuse(JARVIS_PORT, JARVIS_PORT_SENTENCE)
    return text


# -- layer 2: the gate's rule ---------------------------------------------------------

_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_:%")


def host_kind(name: str):
    """How the gate reads a SOCKS host: `("literal", ip)` for an IP address in
    any spelling an IPv4 parser accepts (Chrome sends literals as names),
    `("loopback_name", None)` for `localhost` and `*.localhost` (which Chrome
    itself maps to this machine, never to DNS), `("name", None)` for a name
    to resolve, or `("bad", None)`."""
    if not isinstance(name, str) or not name or len(name) > 255:
        return "bad", None
    lowered = name.lower()
    if lowered.startswith("[") and lowered.endswith("]"):
        lowered = lowered[1:-1]
    if not lowered or any(ch not in _NAME_CHARS for ch in lowered):
        return "bad", None
    try:
        return "literal", ipaddress.ip_address(lowered)
    except ValueError:
        pass
    if ":" in lowered or "%" in lowered:
        return "bad", None
    bare = lowered[:-1] if lowered.endswith(".") else lowered
    if bare == "localhost" or bare.endswith(".localhost"):
        return "loopback_name", None
    if bare and all(ch in "0123456789abcdefx." for ch in bare):
        try:
            return "literal", ipaddress.ip_address(socket.inet_aton(bare))
        except (OSError, ValueError):
            pass
    if not bare or ".." in bare or bare.startswith("."):
        return "bad", None
    return "name", None


def address_class(ip) -> str:
    """`loopback` (this machine: 127/8, ::1, and the unspecified address,
    which Linux connects locally), `private` (every address that is not
    globally reachable: the LAN, link-local and cloud metadata, CGNAT,
    unique-local), `public`, or `not_a_host` (multicast, reserved,
    broadcast, the rest of 0/8)."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback or ip.is_unspecified:
        return "loopback"
    if ip.is_multicast or ip.is_reserved:
        return NOT_A_HOST
    if isinstance(ip, ipaddress.IPv4Address) and (
            ip in ipaddress.IPv4Network("0.0.0.0/8") or ip == ipaddress.IPv4Address("255.255.255.255")):
        return NOT_A_HOST
    if ip.is_global:
        return "public"
    return "private"


def judge_port(port: int, *, refused: frozenset[int], own: int | None = None) -> str | None:
    """The refusal class for a destination port alone, before any name is
    resolved, or None. Jarvis's ports are refused on every address: the
    daemon binds loopback only, and a public site on 8402–8406 is not worth
    a rule with an exception in it."""
    if not isinstance(port, int) or port <= 0 or port > 65535:
        return BAD_REQUEST
    if own is not None and port == own:
        return OWN_PORT
    if port in refused:
        return JARVIS_PORT
    return None


def judge_address(ip, *, via: str, lan: bool, loopback_open: bool = False) -> str | None:
    """The refusal class for one candidate address, or None to allow it.

    `via` is how the host was named (`host_kind`). **Loopback is closed like
    the LAN** (the lead's decision on PR #69, MED 2: "private network closed"
    includes this machine): behind the proxy Chrome's own Local Network
    Access protection no longer applies, so any public page could CSRF — and,
    with a permissive CORS answer, read — every local service. A loopback
    address is allowed only when it was asked for as a literal or as
    `localhost` **and** `loopback_open` (the owner opened that port in a
    Browser pane, `BrowserHost.loopback_open`); a *name* that resolves to
    loopback is DNS rebinding onto the owner's own services and is refused
    whatever else is true. A private address is allowed only with the LAN
    switch on (B-5)."""
    kind = address_class(ip)
    if kind == NOT_A_HOST:
        return NOT_A_HOST
    if kind == "loopback":
        if via not in ("literal", "loopback_name"):
            return NAME_TO_LOOPBACK
        return None if loopback_open else LOOPBACK_CLOSED
    if kind == "private":
        return None if lan else PRIVATE_NETWORK
    return None


def loopback_port(url: str) -> int | None:
    """The port of a URL whose host names this machine (any loopback
    spelling `config._names_this_machine` knows), or None. What an owner's
    navigation opens (`BrowserHost`); never a refused port's caller's
    business — the gate refuses Jarvis's ports first."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in SCHEMES or not config._names_this_machine(host):
        return None
    return port or (443 if parts.scheme.lower() == "https" else 80)
