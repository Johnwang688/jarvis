"""The owner chat's narrow browser operations."""

import logging
import time
from functools import wraps
from hashlib import sha256
from typing import NamedTuple
from unicodedata import category
from uuid import uuid4
from urllib.parse import urlsplit

from jarvis import untrusted
from jarvis.v3.logins import LoginError, _validate_name, canonical_origin

from .link import LinkError, bridge_available
from .read import Ref, read_page
from .service import BrowserBusy, BrowserUnavailable
from .session import BrowserError, Location, _CLOSED, _http_origin, _read_origin, settle
from .signin import _DETAILS, find_login, sign_in


LOG = logging.getLogger(__name__)
_MOVED = "Refused: The browser moved. Read the current page again."
_NOT_LINK = "That item is not a link on the current page."
_READ_FIRST = "Refused: Read the current page before following a link."
_ACT_READ_FIRST = "Refused: Read the current page before clicking or typing."
_CHANGED = "That item changed. Read the page again."
_UNUSABLE = "That item cannot be used on the current page."
_SIGN_IN = "Use browser_sign_in for a sign-in form."
_SENSITIVE = "That field is sensitive or read-only. No tool can type there."
_TEXT = "Use 1–2,000 characters with no control characters except newline."
_NEWLINE = "Only a textarea accepts a newline."
_CREDENTIAL = "That text looks like it contains a credential."
_NO_SUBMIT = "That field is not in a form. Fill it, then press the button."
_FORM_DESTINATION = "That form has no HTTP or HTTPS destination."
_INVALID_TEXT = "That field cannot accept the exact text."
_FILLED = "The field was filled, but the form changed before it could be submitted."
_ACTION_FIELDS = {
    "kind": str, "form_has_password": bool, "payment": bool, "destination_origin": str,
    "method": str, "sensitive": bool, "textarea": bool, "can_submit": bool, "binding": int,
    "button_like": bool, "target_number": int,
}
_REFUSED = "Refused: The browser cannot open that address."
_FAILED = "Error: The browser could not load that page."
_ERROR = "Error: The browser could not complete the request."
_NEEDS_CODE = "Tell the owner the site wants a verification code and Jarvis cannot enter it yet."
_OTHER_SITE = "The site accepted the form and moved to another site. Jarvis cannot confirm the sign-in."
_UNCONFIRMED = "The site accepted the form. Jarvis cannot confirm the sign-in."


class _Read(NamedTuple):
    location: Location
    refs: dict[int, Ref]
    context_id: int | None
    key: str


class ResolvedLink(NamedTuple):
    address: str
    source_origin: str
    destination_origin: str


def _site(origin):
    # The requested last-two-label rule is rough. Treat every such match to
    # a saved origin as a saved site, even across subdomains, to err towards asking.
    return urlsplit(origin).hostname.rstrip(".").split(".")[-2:]


def _fixed_errors(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except BrowserBusy:
            return "Error: The browser is in use."
        except BrowserUnavailable:
            return "Error: The browser is unavailable."
        except BrowserError as exc:
            return {
                "closed": f"Error: {_CLOSED}",
                "moved": _MOVED,
                "refused": _REFUSED,
                "not_a_site": "Refused: The browser is not showing a public site.",
            }.get(exc.reason, _ERROR)
        except LinkError:
            return "Error: The browser connection could not complete the request."
        except LoginError:
            return "Error: The saved login could not be used."
        except Exception:
            LOG.warning("Unexpected browser desk error.")
            return _ERROR

    return call


class BrowserDesk:
    def __init__(self, service, *, store_factory, feed_post, clock=time.time):
        self._service = service
        self._store_factory = store_factory
        self._feed_post = feed_post
        self._clock = clock
        self._last_read: _Read | None = None

    def available(self) -> bool:
        return bridge_available()

    def _read(self, session) -> str:
        self._last_read = None
        page = read_page(session, with_context=True)
        origin = _read_origin(page.location)
        lines = [page.title, *(f"[{ref.number}] {ref.kind}: {ref.label}" for ref in page.refs),
                 "", page.body]
        status = "Text was cut." if page.truncated else "Text was not cut."
        result = f"Site: {origin}. {status}\n" + untrusted.fence("\n".join(lines), origin)
        self._last_read = _Read(page.location, {ref.number: ref for ref in page.refs},
                                page.context_id, uuid4().hex)
        return result

    def _open(self, session, url) -> str:
        self._last_read = None
        outcome = session.open(url).outcome
        if outcome == "loaded":
            return self._read(session)
        return _REFUSED if outcome == "refused" else _FAILED

    @_fixed_errors
    def open(self, url) -> str:
        with self._service.use("chrome") as session:
            return self._open(session, url)

    @_fixed_errors
    def read(self) -> str:
        with self._service.use("chrome") as session:
            return self._read(session)

    def _resolve_follow(self, session, number) -> ResolvedLink | str:
        last = self._last_read
        if last is None:
            return _READ_FIRST
        if last.context_id is None or not session.is_current(last.location):
            self._last_read = None
            return _MOVED
        if type(number) is not int or number not in last.refs or last.refs[number].kind != "link":
            return _NOT_LINK
        href = session.follow_call(last.context_id, number)
        if href == "moved":
            self._last_read = None
            return _MOVED
        if type(href) is not str or not href.startswith(("https://", "http://")):
            return _NOT_LINK
        try:
            destination = _http_origin(href)
        except (BrowserError, ValueError):
            return _NOT_LINK
        return ResolvedLink(href, _read_origin(last.location), destination)

    @_fixed_errors
    def resolve_follow(self, number) -> ResolvedLink | str:
        """Resolve the current numbered link without navigating or reading a secret."""
        if self._last_read is None:
            # Nothing was read: do not start a browser just to say so.
            return _READ_FIRST
        with self._service.use("chrome") as session:
            return self._resolve_follow(session, number)

    def crosses_into_saved_site(self, link: ResolvedLink) -> bool:
        destination = _site(link.destination_origin)
        if _site(link.source_origin) == destination:
            return False
        try:
            return any(_site(login.origin) == destination for login in self._store_factory().list())
        except Exception:
            return True

    def is_read_native_link(self, number):
        last = self._last_read
        ref = last.refs.get(number) if last is not None and type(number) is int else None
        return ref is not None and ref.kind == "link" and ref.native_link

    @_fixed_errors
    def follow(self, number, *, expected: ResolvedLink | None = None) -> str:
        if self._last_read is None:
            return _READ_FIRST
        with self._service.use("chrome") as session:
            link = self._resolve_follow(session, number)
            if isinstance(link, str):
                return link
            if expected is not None and link != expected:
                return _NOT_LINK
            return self._open(session, link.address)

    def _describe(self, session, number):
        from . import act

        last = self._last_read
        if last is None:
            return _ACT_READ_FIRST
        if last.context_id is None or not session.is_current(last.location):
            self._last_read = None
            return _MOVED
        if type(number) is not int or number not in last.refs:
            return {"kind": "unusable"}
        try:
            data = session.act_call(last.context_id, act.DESCRIBE, [number])
        except BrowserError as exc:
            if exc.reason == "moved":
                self._last_read = None
            raise
        if data == "moved":
            self._last_read = None
            return _MOVED
        if data == {"kind": "unusable"}:
            return data
        if (type(data) is not dict or data.keys() != _ACTION_FIELDS.keys() or
                any(type(data[key]) is not kind for key, kind in _ACTION_FIELDS.items()) or
                data["kind"] not in ("link", "submit", "toggle", "control", "field")):
            return _ERROR
        target = last.refs.get(data["target_number"])
        if target is None:
            return _ERROR
        return {**data, "number": number, "label": target.label,
                "origin": _read_origin(last.location), "document": last.key}

    @_fixed_errors
    def describe(self, number):
        if self._last_read is None:
            return _ACT_READ_FIRST
        with self._service.use("chrome") as session:
            return self._describe(session, number)

    @staticmethod
    def action_refusal(described, *, text=None, submit=False):
        kind = described["kind"]
        if text is None:
            if kind == "field":
                return "That item is a field. Use browser_fill to enter text."
            if kind == "unusable":
                return _UNUSABLE
            submit = kind == "submit"
        else:
            if kind != "field":
                return _UNUSABLE if kind == "unusable" else "That item is not a text field."
            if described["sensitive"]:
                return _SENSITIVE
            if (type(text) is not str or type(submit) is not bool or not 1 <= len(text) <= 2000 or
                    any(char != "\n" and category(char) in ("Cc", "Cs") for char in text)):
                return _TEXT
            if "\n" in text and not described["textarea"]:
                return _NEWLINE
            from jarvis.v2 import redact
            from jarvis.v3.approval_card import card_text

            if (redact.for_display(text) != text or
                    redact.CardPass().hide(text, command=False) != text):
                return _CREDENTIAL
            if card_text(text, limit=None) != text:
                return "That text cannot be shown exactly on the approval card."
            if submit and not described["can_submit"]:
                return _NO_SUBMIT
        if submit and described["form_has_password"]:
            return _SIGN_IN
        if submit and described["method"] and not described["destination_origin"] and described["method"] != "dialog":
            return _FORM_DESTINATION
        return None

    def _act(self, number, text, submit, expected, *, link=None, crosses_saved=None):
        from . import act

        if self._last_read is None:
            return _ACT_READ_FIRST if expected is None else _CHANGED
        with self._service.use("chrome") as session:
            try:
                described = self._describe(session, number)
            except BrowserError as exc:
                if expected is not None and exc.reason == "moved":
                    return _CHANGED
                raise
            if expected is not None and described != expected:
                return _CHANGED
            if isinstance(described, str):
                return described
            if refusal := self.action_refusal(described, text=text, submit=submit):
                return refusal
            if described["kind"] == "link":
                # The chat uses follow's own preview and grant for links.
                try:
                    resolved = self._resolve_follow(session, described["target_number"])
                except BrowserError as exc:
                    if exc.reason != "moved":
                        raise
                    self._last_read = None
                    return _MOVED if expected is None else _CHANGED
                if expected is not None and (resolved != link or
                        (crosses_saved is False and self.crosses_into_saved_site(resolved))):
                    return _CHANGED
                return resolved if isinstance(resolved, str) else self._open(session, resolved.address)
            last, self._last_read = self._last_read, None
            script = act.CLICK if text is None else act.TYPE
            args = [number, described["binding"]]
            if text is not None:
                args.extend((text, submit))
            try:
                outcome = session.act_call(last.context_id, script, args)
            except BrowserError as exc:
                if expected is not None and exc.reason == "moved":
                    return _CHANGED
                raise
            if outcome not in ("done", "filled"):
                return {"moved": _MOVED if expected is None else _CHANGED,
                        "changed": _CHANGED, "invalid_text": _INVALID_TEXT}.get(outcome, _ERROR)
            settle(session, time.monotonic, 20.0)
            page = self._read(session)
            return f"{_FILLED}\n{page}" if outcome == "filled" else page

    @_fixed_errors
    def click(self, number, *, expected=None, link=None, crosses_saved=None):
        return self._act(number, None, False, expected, link=link, crosses_saved=crosses_saved)

    @_fixed_errors
    def type(self, number, text, submit=False, *, expected=None):
        return self._act(number, text, submit, expected)

    @_fixed_errors
    def sign_in(self, name) -> str:
        try:
            _validate_name(name)
        except LoginError:
            return "Refused: Use a saved login name of 1–32 lowercase letters, digits or interior hyphens."
        try:
            store = self._store_factory()
            login = find_login(store, name)
            with self._service.use(login.browser) as session:
                try:
                    use_current = canonical_origin(session.location().url) == login.origin
                except BrowserError:
                    use_current = False
                outcome = sign_in(session, store, name, expected_login=login, use_current=use_current)
                signed_in_detail = _DETAILS["signed_in"]
                if outcome.result == "signed_in":
                    try:
                        if canonical_origin(session.location().url) != login.origin:
                            signed_in_detail = _OTHER_SITE
                    except BrowserError:
                        signed_in_detail = _UNCONFIRMED
        finally:
            self._last_read = None
        if outcome.result == "needs_code":
            return _NEEDS_CODE
        if outcome.result == "signed_in":
            # Imported here: the daemon builds a desk at start, and start-up
            # must not load the feed and its index.
            from jarvis.v3.feed import FeedError, SIGN_IN_NOTICE

            host = urlsplit(login.origin).hostname
            minute = int(self._clock() // 60)
            key = sha256(f"browser-sign-in:{name}:{minute}".encode()).hexdigest()
            try:
                self._feed_post(actor="jarvis", kind="news", rank=4,
                                ask=f"{SIGN_IN_NOTICE}{host}.", idempotency_key=key)
            except (FeedError, OSError):
                return "Error: The site accepted the form, but Jarvis could not write the feed notice."
            return signed_in_detail
        return _DETAILS.get(outcome.result, _DETAILS["failed"])
