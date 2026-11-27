"""Owner-saved web logins in the default Secret Service collection.

Metadata never includes a password. Replacing a login requires a fresh
password, and only an exact origin match can retrieve a saved one.
The keyring backend never unlocks a collection or runs a prompt.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import wraps
import re
from typing import NamedTuple
from unicodedata import category
from urllib.parse import urlsplit


_APPLICATION = "jarvis-login"
_SCHEMA = "1"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IP_END = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)")
_UNAVAILABLE = "No login keyring is available."
_LOCKED = "The login keyring is locked. Unlock it and try again."
_PROMPT = "The login keyring requires interaction. Complete it outside Jarvis and try again."


@dataclass(frozen=True)
class Login:
    name: str
    origin: str
    username: str
    browser: str


class LoginError(Exception):
    """An error safe to show in the owner's terminal."""


class _LoginSecret(NamedTuple):
    login: Login
    password: str

    def __repr__(self):
        return f"_LoginSecret(login={self.login!r}, password=<redacted>)"

    __str__ = __repr__


def canonical_origin(url) -> str | None:
    if not isinstance(url, str):
        return None
    url = str.__str__(url)
    # ASCII only. A browser reports a page's address with the host already in
    # its ASCII (xn--) form, and that form is compared as it is. Mapping a
    # Unicode host here could disagree with the browser's own mapping and
    # hand one site's password to another.
    if not url.isascii() or any(char == "\\" or char.isspace() or category(char) == "Cc" for char in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or any(
            char in parts.netloc for char in "@[]"
        ) or parts.netloc.endswith(":"):
            return None
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    # Chromium interprets numeric final labels as IPv4, including legacy
    # spellings such as a single integer or hexadecimal octets.
    if _IP_END.fullmatch(labels[-1]):
        return None
    if port == 0:
        return None
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def _validate_name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise LoginError("Use a login name of 1–32 lowercase letters, digits or interior hyphens.")


def _login(name, site, username, browser) -> Login:
    _validate_name(name)
    origin = canonical_origin(site)
    if origin is None:
        raise LoginError("Use a valid HTTPS site address. Write a non-English host in its xn-- form.")
    site = str.__str__(site)
    if urlsplit(site).path not in ("", "/") or "?" in site or "#" in site:
        raise LoginError("Enter only the site origin, without a page path, query or fragment.")
    if not isinstance(username, str) or not 1 <= len(username) <= 254 or any(
        category(char) in ("Cc", "Cs") for char in username
    ):
        raise LoginError("Use a username of 1–254 characters without control characters.")
    if browser not in ("chrome", "edge"):
        raise LoginError("Choose chrome or edge for the browser.")
    return Login(name, origin, username, browser)


def _valid_password(password) -> bool:
    return (isinstance(password, str) and 1 <= len(password) <= 1024
            and not any(char in password for char in ("\n", "\r", "\x00")))


def _from_attributes(attributes) -> Login | None:
    if attributes.get("application") != _APPLICATION or attributes.get("schema") != _SCHEMA:
        return None
    try:
        login = _login(attributes["name"], attributes["origin"], attributes["username"], attributes["browser"])
    except (KeyError, LoginError):
        return None
    return login if login.origin == attributes["origin"] else None


class Store:
    def __init__(self, backend=None):
        self._backend = backend

    @classmethod
    def system(cls) -> "Store":
        return cls(_SecretService())

    def _attached_backend(self):
        if self._backend is None:
            raise LoginError("No login keyring is attached.")
        return self._backend

    def _items(self):
        return [(handle, attrs) for handle, attrs in self._attached_backend().items()
                if attrs.get("application") == _APPLICATION]

    def _check_add(self, name, site, username, *, browser, replace):
        self._attached_backend()
        login = _login(name, site, username, browser)
        items = self._items()
        matches = [handle for handle, attrs in items if attrs.get("name") == name]
        if matches and not replace:
            raise LoginError(f"A saved login named {name} already exists. Use --replace to replace it.")
        # Count names, not items: leftovers of a failed replace must not use up the limit.
        if len({attrs.get("name") for _, attrs in items} - {name}) >= 64:
            raise LoginError("The limit is 64 saved logins.")
        return login, matches

    def add(self, name, site, username, password, *, browser="chrome", replace=False) -> Login:
        backend = self._attached_backend()
        if not _valid_password(password):
            raise LoginError("Use a password of 1–1024 characters without line breaks or NUL.")
        login, matches = self._check_add(name, site, username, browser=browser, replace=replace)
        attributes = {
            "application": _APPLICATION, "schema": _SCHEMA, "name": login.name,
            "origin": login.origin, "username": login.username, "browser": login.browser,
        }
        # Write first. A failed write then leaves the old login as it was, and
        # a failed clean-up leaves two items, which secret_for refuses.
        backend.put(attributes, password, f"Jarvis login: {name}")
        for handle in matches:
            backend.delete(handle)
        return login

    def list(self) -> list[Login]:
        logins = [_from_attributes(attrs) for _, attrs in self._items()]
        return sorted((login for login in logins if login is not None), key=lambda login: login.name)

    def remove(self, name) -> bool:
        backend = self._attached_backend()
        _validate_name(name)
        matches = [handle for handle, attrs in self._items() if attrs.get("name") == name]
        for handle in matches:
            backend.delete(handle)
        return bool(matches)

    def secret_for(self, name, url) -> tuple[Login, str]:
        backend = self._attached_backend()
        _validate_name(name)
        matches = [(handle, attrs) for handle, attrs in self._items() if attrs.get("name") == name]
        if len(matches) != 1:
            raise LoginError(f"Expected one saved login named {name}.")
        handle, attributes = matches[0]
        login = _from_attributes(attributes)
        if login is None:
            raise LoginError(f"Saved login {name} has invalid attributes.")
        if canonical_origin(url) != login.origin:
            raise LoginError(f"The page is not the site for login {name}.")
        password = backend.secret(handle)
        if not _valid_password(password):
            raise LoginError(f"Saved login {name} has an invalid password.")
        return _LoginSecret(login, password)


def _fresh_error(method):
    """Re-raise a backend failure as a new error with no history.

    The library's own error (whose text is not ours to vouch for) and the
    frames that held a password are then not carried along as context or
    traceback.
    """
    @wraps(method)
    def call(self, *args):
        try:
            return method(self, *args)
        except LoginError as exc:
            message = str(exc)
        del args
        raise LoginError(message)
    return call


class _SecretService:
    @contextmanager
    def _collection(self):
        try:
            import secretstorage
        except ImportError:
            raise LoginError("The keyring library is not installed. Run: uv pip install -e .") from None
        except Exception:
            raise LoginError(_UNAVAILABLE) from None

        try:
            with closing(secretstorage.dbus_init()) as connection:
                # get_default_collection can create a collection and prompt.
                collection = secretstorage.Collection(connection)
                if collection.is_locked():
                    raise LoginError(_LOCKED)
                yield collection
            return
        except LoginError:
            raise
        except secretstorage.exceptions.LockedException:
            message = _LOCKED
        except Exception:
            message = _UNAVAILABLE
        raise LoginError(message)

    @_fresh_error
    def items(self):
        with self._collection() as collection:
            items = []
            for item in collection.search_items({"application": _APPLICATION}):
                attrs = item.get_attributes()
                items.append(((item.item_path, attrs.copy()), attrs))
            return items

    @_fresh_error
    def put(self, attributes, secret, label):
        with self._collection() as collection:
            from secretstorage.util import SS_PREFIX, format_secret, open_session

            session = open_session(collection.connection)
            properties = {
                SS_PREFIX + "Item.Label": ("s", label),
                SS_PREFIX + "Item.Attributes": ("a{ss}", attributes),
            }
            # create_item executes a prompt returned by the service.
            path, prompt = collection._collection.call(
                "CreateItem", "a{sv}(oayays)b", properties,
                format_secret(session, secret.encode("utf-8"), "text/plain"), False,
            )
            if prompt != "/":
                raise LoginError(_PROMPT)
            if path == "/":
                raise LoginError(_UNAVAILABLE)

    def _item(self, collection, handle):
        from secretstorage import Item

        path, attributes = handle
        item = Item(collection.connection, path)
        if item.get_attributes() != attributes:
            raise LoginError("The saved login changed. Try again.")
        return item

    @_fresh_error
    def secret(self, handle):
        with self._collection() as collection:
            item = self._item(collection, handle)
            password = item.get_secret().decode("utf-8")
            if item.get_attributes() != handle[1]:
                raise LoginError("The saved login changed. Try again.")
            return password

    @_fresh_error
    def delete(self, handle):
        with self._collection() as collection:
            item = self._item(collection, handle)
            if item.is_locked():
                raise LoginError(_LOCKED)
            # Item.delete executes a prompt returned by the service.
            prompt, = item._item.call("Delete", "")
            if prompt != "/":
                raise LoginError(_PROMPT)
