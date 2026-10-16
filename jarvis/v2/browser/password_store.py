"""Which password store a Browser profile's Chrome uses (B-LOGIN, M12).

Chrome on Linux encrypts saved passwords and cookies with a key from the
store named by `--password-store`. `basic` is a fixed, publicly known key:
anyone who copies the profile folder can read them. `gnome-libsecret` keeps
a random key in the owner's login keyring (gnome-keyring, reached over the
session D-Bus as the Secret Service), so a copied folder is useless without
the keyring password.

**What it protects, stated plainly.** The keyring protects the passwords
*at rest* — WSL shut down, the files read from Windows, a backup, a copied
profile. While it is unlocked, which Chrome needs, gnome-keyring has no
per-application rule: any program running as the owner can still ask the
Secret Service for Chrome's key. That makes reading them a distinct D-Bus
request (a gate rule for it is a follow-up), not a boundary.

**The rules** (the lead's decisions, 2026-10-15):

- Private keeps `basic` and never gets the bus: off-the-record, its folder
  dies with it.
- A named profile gets `gnome-libsecret`, with the daemon's validated session
  bus address, only when the default collection reads **unlocked** — checked
  before the launch and again after it, because Chrome falls back to `basic`
  silently when it cannot reach the key.
- **locked**, **no_keyring** (the Secret Service answers but has no default
  collection) or **error** (any answer this module does not recognise): the
  launch is refused with `UNLOCK_MESSAGE`. Jarvis never prompts, never
  unlocks, and never downgrades.
- **unavailable** (no Secret Service, no usable bus, or no keyring attached):
  `basic`, reported loudly as unprotected — unless the profile is sticky.
- **Sticky**: a profile once launched with the keyring is recorded in
  `profiles.json` and never launched with `basic` again (its `v11` data would
  be unreadable there, so it would look signed out and lose local passwords).
- The owner's `"password_store": "basic"` in `browser.json` asks for `basic`
  on purpose; it is honoured (loudly) except for a sticky profile.

**Only state is read.** The probe asks the Secret Service for one property,
`Locked`, of the default collection's alias. It never searches items, never
reads a secret, and never asks for an unlock (which would prompt).

**Tests never reach the real bus.** A `Keyring()` is *detached*: it reports
`unavailable`, runs nothing and hands Chrome no bus. daemon2's `main()`
attaches it to the daemon's own environment, as it attaches `browser.json`,
so no embedded or test daemon can touch the owner's keyring.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass

LOG = logging.getLogger("jarvis.v2.browser")

UNAVAILABLE, NO_KEYRING, LOCKED, UNLOCKED, ERROR = (
    "unavailable", "no_keyring", "locked", "unlocked", "error")
UNKNOWN = "unknown"   # /status only: not read yet
STATES = (UNAVAILABLE, NO_KEYRING, LOCKED, UNLOCKED, ERROR)

BASIC, KEYRING_STORE = "basic", "gnome-libsecret"
REQUEST_KEYRING, REQUEST_BASIC = "keyring", "basic"

# The owner's commands. Typed in a terminal Jarvis does not host: a HUD
# terminal is a Jarvis PTY, so its keystrokes pass through the daemon.
INSTALL_COMMAND = "sudo apt install --no-install-recommends gnome-keyring libsecret-1-0"
UNLOCK_COMMAND = ("read -rsp 'Keyring password: ' KP; echo; printf '%s' \"$KP\" | "
                  "gnome-keyring-daemon --unlock >/dev/null; unset KP")
UNLOCK_MESSAGE = ("Unlock the keyring in Windows Terminal (not a Jarvis terminal): "
                  + UNLOCK_COMMAND)
STICKY_UNAVAILABLE_MESSAGE = (
    "This profile's passwords are in the keyring, which is not running. Start it and "
    "unlock it in Windows Terminal (not a Jarvis terminal): " + UNLOCK_COMMAND)
STICKY_BASIC_MESSAGE = (
    "This profile's passwords are in the keyring; it never opens with the basic store. "
    "Remove \"password_store\": \"basic\" from browser.json to open it")
CHANGED_MESSAGE = ("The keyring locked while Chrome was starting, so it was closed. "
                   + UNLOCK_MESSAGE)
UNPROTECTED_HINT = ("Unprotected (basic store): saved passwords use a public key. Install "
                    "the keyring: " + INSTALL_COMMAND + " — then unlock it: " + UNLOCK_COMMAND)

GDBUS = Path("/usr/bin/gdbus")
PROBE_TIMEOUT_S = 3
STATUS_TTL_S = 5.0
_SECRETS = "org.freedesktop.secrets"
_DEFAULT = "/org/freedesktop/secrets/aliases/default"
_NO_SERVICE = frozenset({"org.freedesktop.DBus.Error.ServiceUnknown",
                         "org.freedesktop.DBus.Error.NameHasNoOwner"})
_NO_COLLECTION = frozenset({"org.freedesktop.Secret.Error.NoSuchObject",
                            "org.freedesktop.DBus.Error.UnknownObject",
                            "org.freedesktop.DBus.Error.UnknownMethod",
                            "org.freedesktop.DBus.Error.UnknownInterface"})
_ERROR_NAME = re.compile(r"GDBus\.Error:([A-Za-z0-9_.]+):")
_ADDRESS = re.compile(r"unix:path=(/[A-Za-z0-9_./-]+)")


def bus_address(environ=None, uid: int | None = None, *, runtime: Path | None = None) -> str | None:
    """The session bus address Chrome may be given, or None.

    Only `unix:path=<socket>` (no other keys, no abstract or TCP address),
    whose socket is directly in `/run/user/<uid>/`, is a socket and not a
    symlink, owned by us, in a folder owned by us and closed to others. With
    `DBUS_SESSION_BUS_ADDRESS` unset, the standard `/run/user/<uid>/bus`.
    `runtime` replaces `/run/user/<uid>` for tests only."""
    environ = os.environ if environ is None else environ
    uid = os.getuid() if uid is None else uid
    runtime = Path(f"/run/user/{uid}") if runtime is None else Path(runtime)
    raw = environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={runtime}/bus"
    match = _ADDRESS.fullmatch(raw)
    if match is None:
        return None
    path = Path(match.group(1))
    if os.path.normpath(str(path)) != str(path) or path.parent != runtime:
        return None
    try:
        folder = os.lstat(runtime)
        sock = os.lstat(path)
    except OSError:
        return None
    if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != uid
            or stat.S_IMODE(folder.st_mode) & 0o077):
        return None
    if not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != uid:
        return None
    return raw


def classify(returncode: int, out: str, err: str) -> str:
    """One `gdbus call … Properties.Get … Locked` result as a state."""
    if returncode == 0:
        text = out.strip()
        if text == "(<true>,)":
            return LOCKED
        if text == "(<false>,)":
            return UNLOCKED
        return ERROR
    found = _ERROR_NAME.search(err or "")
    name = found.group(1) if found else ""
    if name in _NO_SERVICE:
        return UNAVAILABLE
    if name in _NO_COLLECTION:
        return NO_KEYRING
    return ERROR


def probe(address: str | None, *, run=subprocess.run, gdbus: Path = GDBUS) -> str:
    """The default collection's state on the bus at `address`: one property
    read, nothing else (module note)."""
    if address is None or not gdbus.exists():
        return UNAVAILABLE
    env = {"DBUS_SESSION_BUS_ADDRESS": address, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8"}
    command = [str(gdbus), "call", "--session", "--timeout", str(PROBE_TIMEOUT_S),
               "--dest", _SECRETS, "--object-path", _DEFAULT,
               "--method", "org.freedesktop.DBus.Properties.Get",
               "org.freedesktop.Secret.Collection", "Locked"]
    try:
        done = run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                   text=True, timeout=PROBE_TIMEOUT_S + 2, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("keyring state not read (%s)", type(exc).__name__)
        return ERROR
    state = classify(done.returncode, done.stdout or "", done.stderr or "")
    if state == ERROR:
        LOG.warning("keyring state not recognised (exit %s)", done.returncode)
    return state


@dataclass(frozen=True)
class Choice:
    """What one launch uses: the store, the bus (None for basic), whether it
    is unprotected, and why it is refused (None when it is not)."""
    store: str
    bus: str | None
    unprotected: bool
    refused: str | None = None


def choose(*, private: bool, requested: str, sticky: bool, state: str,
           bus: str | None) -> Choice:
    """The store for one launch (module note's rules). Pure."""
    if private:
        return Choice(BASIC, None, True)
    if requested == REQUEST_BASIC:
        if sticky:
            return Choice(BASIC, None, True, STICKY_BASIC_MESSAGE)
        return Choice(BASIC, None, True)
    if state == UNLOCKED and bus is not None:
        return Choice(KEYRING_STORE, bus, False)
    if state == UNAVAILABLE or (state == UNLOCKED and bus is None):
        if sticky:
            return Choice(BASIC, None, True, STICKY_UNAVAILABLE_MESSAGE)
        return Choice(BASIC, None, True)
    return Choice(BASIC, None, True, UNLOCK_MESSAGE)


class Keyring:
    """The owner's keyring as the Browser sees it: detached (tests, embedded
    daemons) or attached to the daemon's own session bus by daemon2."""

    def __init__(self, *, run=subprocess.run, gdbus: Path = GDBUS, clock=time.monotonic):
        self._run = run
        self._gdbus = gdbus
        self._clock = clock
        self._lock = threading.Lock()
        self._address: str | None = None
        self._attached = False
        self._cached: tuple[float, str] | None = None
        self._refreshing = False

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, environ=None, *, runtime: Path | None = None) -> None:
        """Use the bus named by `environ` (default: the daemon's) from now on.
        `runtime` is for tests (`bus_address`)."""
        address = bus_address(environ, runtime=runtime)
        with self._lock:
            self._attached = True
            self._address = address
            self._cached = None
        if address is None:
            LOG.warning("no usable session bus for the keyring; the Browser uses the basic store")

    def bus(self) -> str | None:
        with self._lock:
            return self._address if self._attached else None

    def state(self) -> str:
        """A fresh read, for a launch."""
        address = self.bus()
        state = UNAVAILABLE if address is None else probe(address, run=self._run, gdbus=self._gdbus)
        with self._lock:
            self._cached = (self._clock(), state)
        return state

    def cached_state(self) -> str:
        """For `/status`, never blocking: the last read while it is younger
        than `STATUS_TTL_S`; else that value (`unknown` before the first)
        while one background read refreshes it."""
        if not self._attached:
            return UNAVAILABLE
        with self._lock:
            cached = self._cached
            stale = cached is None or self._clock() - cached[0] >= STATUS_TTL_S
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            def refresh():
                try:
                    self.state()
                finally:
                    with self._lock:
                        self._refreshing = False
            threading.Thread(target=refresh, name="jarvis-keyring-state", daemon=True).start()
        return cached[1] if cached is not None else UNKNOWN
