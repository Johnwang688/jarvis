"""Web Push to the owner's phone (phone surface design PH-8, §3, §5, §7;
work package P-6).

**One thing is ever sent: `{"count": n, "rank": r}`** (`payload`): how many
inbox rows of ranks 1 to 4 wait on the owner (an approval, a blocking
question, a proposal in its grace window, a blocked task) and the most
urgent rank among them (0 when none). No id, title, project, command or
any other text, ever: the push service (Apple's, Google's, Mozilla's)
sees nothing else, and the phone reads the inbox itself once opened. The
payload is encrypted to the subscription's keys (RFC 8291, aes128gcm) and
signed with this daemon's VAPID key (RFC 8292).

**When.** The sender listens to the bus's `inbox_changed` (`observe` only
sets an event; nothing runs on the publishing thread) and, on its own
thread, off the daemon's lock, reads the inbox projection
(`inbox.rows`). A push is due when a row new to ranks 1 to 4 appears (so
the count rises; a row that only moves within them is not new). At most one
push per `COALESCE_S` (60 s, monotonic): what arrives inside the window is
coalesced into one push sent when it ends, with the count as it is then
(none if nothing waits any more). **Quiet hours** (`quiet.py`, the owner's
answer of 2026-10-28): nothing is pushed while they hold, rank 1 included;
when they end, one push says what still waits at ranks 1 to 4, if anything
does. The desk's "Notifications" switch off: nothing is pushed.

**Who receives it.** Subscriptions are made by a signed-in phone
(`POST /m/api/push/subscribe`, remote listener only, past
`remote_auth.owner_status`), one per session: a session subscribing again
replaces its own, and an endpoint already stored under another session
moves to this one. A subscription lives as long as its passkey: logging
out, ending that session at the desk or revoking the passkey deletes it at
once (`ended`, from `RemoteAuth.on_end`), and a send goes only to
subscriptions whose passkey is still active. A session that merely expires
keeps it (the notification is what brings the owner back to sign in).
At most `MAX_SUBSCRIPTIONS`; the oldest goes first. A 404 or 410 from the
push service deletes the subscription; any other failure is logged with
its status (or the exception's class) only.

**The endpoint is a capability** (like Discord's interaction URL): it is
never logged, never in `/status`, never in the desk's view (which shows the
push service's host only). It must be `https`, on port 443, with no
userinfo, to a host on `PUSH_HOSTS` / `PUSH_SUFFIXES` (never an IP
literal), and every address the host resolves to must be global, checked
when the phone subscribes and again at each send; the send then connects
to exactly the address that was checked (`https_post`), with the
certificate verified for the host name, no proxy and no redirect followed.
So a forged subscription cannot make the daemon POST to the LAN or to this
machine.

**Never from a test or embedded daemon.** The sender's thread and its
network transport are attached by daemon2's `main()` only
(`attach_system`, as `inbox_prs` is); `Daemon.start()` starts the thread
only if it was attached, and the thread's first read seeds what is already
waiting without sending. Without `pywebpush` installed the daemon still
starts; the routes answer 503.

**Files** (`<data root>/remote/`, 0700; each 0600, read `O_NOFOLLOW |
O_NONBLOCK` relative to the folder's descriptor, written as a fresh `O_EXCL`
temp file, fsynced, replaced, folder fsynced): `jarvis_push_key.json` (the
VAPID private key as PKCS#8 PEM inside `{"version": 1, "pem"}`, made once on
first use), `jarvis_push_subscriptions.json` (endpoints and their keys) and
`push_settings.json` (the switch). **The folder is a credential folder**
(`config.remote_dir()` in `config.credential_dirs()`, review of PR #325
MED-1): v1's file and shell tools refuse every read in it, by any spelling
(`files.credential_folder_judge`, and none of its files is exempt as
"readable config"); v2's shell reads of it are always asked
(`credentials-path`) and v2's read tools are refused there
(`permissions._phone_folder_read`). All three files are protected state
(`tools/files._protected_state_paths`): no agent writes them. The private
key is never logged, returned or shown; only its public half is
(`GET /m/api/push`, the `applicationServerKey`).

**Each send is bounded** (review of PR #325, LOW-2): every subscription of
one push within `SEND_BUDGET_S` (15 s) in all, each name resolved within
`RESOLVE_TIMEOUT_S` on a thread of its own, each connection within what is
left, and the daemon's stop checked before every subscription and address.
**A push that reached no phone is tried again** (LOW-3) after 1, 5, then
every 15 minutes, with the count then, still coalesced and silent in quiet
hours. The payload is padded to `PAYLOAD_BYTES`.

Routes. Remote listener, signed-in phone only (`route`):

    GET    /m/api/push             {key, enabled, subscribed}
    POST   /m/api/push/subscribe   {subscription: {endpoint, keys: {p256dh, auth}}}
    DELETE /m/api/push/subscribe   this session's (or {endpoint}'s) subscription

HUD listener, owner-only (`hud_route`, from `remote_auth.hud_route`):

    GET  /phone/push               the switch, subscriptions (service host only), quiet hours
    POST /phone/push/settings      {enabled}
    POST /phone/push/test          one push now, with the current count

SELF_PROTECTED: it decides what leaves this machine for the phone, and
where to. Tests: tests/v2/push_check.py.
"""
from __future__ import annotations

import base64
import errno
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)

FOLDER = "remote"
SUBSCRIPTIONS = "jarvis_push_subscriptions.json"
VAPID_KEY = "jarvis_push_key.json"
SETTINGS = "push_settings.json"
MAX_SUBSCRIPTIONS = 8
ENDPOINT_MAX = 1024
COALESCE_S = 60.0
SEND_TIMEOUT_S = 10.0       # one connection's socket timeout, at most
SEND_BUDGET_S = 15.0        # one whole send (every subscription), at most
RESOLVE_TIMEOUT_S = 5.0     # one name's resolution, at most
RETRY_S = (60.0, 300.0, 900.0)   # a failed push is tried again after these, the last repeated
MORNING_GRACE_S = 1800.0    # a start this soon after quiet hours end still gets the morning push
PAYLOAD_BYTES = 48          # every payload is padded to this length
TTL_S = 600                 # an approval waits at most this long for a phone
RECHECK_S = 300.0           # the sender looks again at least this often
ATTENTION_RANKS = frozenset({1, 2, 3, 4})
VAPID_EXP_S = 12 * 3600
UA_CAP = 160
_FILE_MAX = 256 * 1024

# The push services a subscription may name: exact hosts, and suffixes (a
# host ending in one, with at least one label before it).
PUSH_HOSTS = frozenset({"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"})
PUSH_SUFFIXES = (".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com")
OK_STATUSES = frozenset({200, 201, 202})
GONE_STATUSES = frozenset({404, 410})

INSTALL = "notifications need the pywebpush package: uv pip install -e . in the daemon's venv"

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_B64URL = re.compile(r"[A-Za-z0-9_-]{1,200}")
_ID = re.compile(r"p-[0-9a-f]{12}")
_SESSION_ID = re.compile(r"s-[0-9a-f]{16}")
_KEY = re.compile(r"k-[0-9a-f]{12}")
_PATH = re.compile(r"/[\x21-\x7e]*")

_READ = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
          | getattr(os, "O_CLOEXEC", 0))
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_SUBDIR = _DIR | getattr(os, "O_NOFOLLOW", 0)


class PushError(Exception):
    """A refusal with its status and a fixed sentence: never the input."""

    def __init__(self, status: int, word: str):
        super().__init__(word)
        self.status = status
        self.word = word


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text) -> bytes:
    if not isinstance(text, str) or not _B64URL.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def payload(count: int, rank: int) -> bytes:
    """The only thing a push carries (PH-8): `{"count": n, "rank": r}`, padded
    with spaces (which JSON ignores) to `PAYLOAD_BYTES`, so the ciphertext's
    length says nothing about the count. A count past 999 999 is sent as
    999 999."""
    if type(count) is not int or type(rank) is not int or count < 0 or not 0 <= rank <= 4:
        raise ValueError("a push carries a count and a rank only")
    text = json.dumps({"count": min(count, 999_999), "rank": rank}, separators=(",", ":"))
    return text.ljust(PAYLOAD_BYTES).encode("ascii")


# -- the endpoint --------------------------------------------------------------------


def push_host_allowed(host: str) -> bool:
    return host in PUSH_HOSTS or any(host.endswith(s) and len(host) > len(s) for s in PUSH_SUFFIXES)


def check_endpoint(url) -> tuple[str, str]:
    """(host, path with query) of an acceptable endpoint, else ValueError
    with a fixed sentence (never the URL)."""
    if not isinstance(url, str) or not url or len(url) > ENDPOINT_MAX:
        raise ValueError("not a push endpoint")
    if any(ord(c) < 0x21 or ord(c) > 0x7e for c in url):
        raise ValueError("not a push endpoint")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("not a push endpoint") from None
    if parts.scheme != "https":
        raise ValueError("a push endpoint must be https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("not a push endpoint")
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError("a push endpoint may not name an address")
    if "[" in parts.netloc or not _HOST.fullmatch(host):
        raise ValueError("not a push endpoint")
    if port not in (None, 443) or parts.netloc not in (host, host + ":443"):
        raise ValueError("a push endpoint must be on port 443")
    if parts.fragment or not push_host_allowed(host):
        raise ValueError("this push service is not accepted")
    path = parts.path + ("?" + parts.query if parts.query else "")
    if not _PATH.fullmatch(path):
        raise ValueError("not a push endpoint")
    return host, path


def resolve(host: str) -> list[str]:
    """Every address `host` resolves to, for port 443 over TCP."""
    found = []
    for _family, _type, _proto, _name, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        ip = address[0]
        if ip not in found:
            found.append(ip)
    return found


def bounded(fn, timeout: float):
    """`fn()` on a thread of its own, waited for at most `timeout` seconds:
    its result, or TimeoutError (the thread is left to finish on its own;
    `getaddrinfo` cannot be interrupted)."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:     # noqa: BLE001 — handed to the caller
            box["error"] = exc

    worker = threading.Thread(target=run, name="jarvis-push-resolve", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise TimeoutError("timed out")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def public_addresses(host: str, resolver=resolve, timeout: float = RESOLVE_TIMEOUT_S) -> list[str]:
    """The host's addresses, each one global; ValueError if it resolves to
    none, or to any that is private, loopback, link-local, reserved or
    multicast (all refused, not just those: it fails closed), or if the
    resolution takes longer than `timeout`."""
    try:
        addresses = bounded(lambda: resolver(host), timeout)
    except (OSError, UnicodeError, TimeoutError):
        raise ValueError("the push service could not be resolved") from None
    if not addresses:
        raise ValueError("the push service could not be resolved")
    for text in addresses:
        try:
            ip = ipaddress.ip_address(str(text).split("%", 1)[0])
        except ValueError:
            raise ValueError("the push service resolves to a private address") from None
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("the push service resolves to a private address")
    return [str(a) for a in addresses]


def check_keys(keys) -> tuple[str, str]:
    """(p256dh, auth) of a subscription, checked: a P-256 point (65 bytes,
    uncompressed) and a 16-byte secret. ValueError otherwise."""
    if not isinstance(keys, dict):
        raise ValueError("the subscription's keys are missing")
    try:
        point, auth = unb64url(keys.get("p256dh")), unb64url(keys.get("auth"))
    except ValueError:
        raise ValueError("the subscription's keys are malformed") from None
    if len(point) != 65 or point[0] != 4 or len(auth) != 16:
        raise ValueError("the subscription's keys are malformed")
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ImportError:
        raise PushError(503, INSTALL) from None
    except ValueError:
        raise ValueError("the subscription's keys are malformed") from None
    return keys["p256dh"], keys["auth"]


# -- encryption, signing and the wire ---------------------------------------------------


def _libs():
    try:
        from pywebpush import WebPusher
        from py_vapid import Vapid02
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        raise PushError(503, INSTALL) from None
    return WebPusher, Vapid02, serialization, ec


def encrypt(sub: dict, plaintext: bytes) -> tuple[bytes, dict]:
    """RFC 8291's aes128gcm body for one subscription, and its header."""
    WebPusher, _v, _s, _e = _libs()
    encoded = WebPusher({"endpoint": sub["endpoint"],
                         "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}).encode(plaintext, "aes128gcm")
    return encoded["body"], {"Content-Encoding": "aes128gcm"}


def https_post(host: str, addresses: list[str], path: str, headers: dict, body: bytes,
               timeout: float, *, deadline: float | None = None, stop=None) -> int:
    """POST to exactly one of the checked `addresses`, with TLS verified for
    `host`: no proxy, no redirect followed. The status. Each address is tried
    only while `deadline` (monotonic) has not passed and `stop()` is not
    true, with its timeout cut to what is left."""
    context = ssl.create_default_context()
    last: Exception | None = None
    for address in addresses[:4]:
        left = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if left <= 0 or (stop is not None and stop()):
            raise TimeoutError("the send's time is up")
        try:
            raw = socket.create_connection((address, 443), timeout=left)
        except OSError as exc:
            last = exc
            continue
        try:
            tls = context.wrap_socket(raw, server_hostname=host)
        except BaseException:
            raw.close()
            raise
        conn = http.client.HTTPSConnection(host, 443, timeout=left, context=context)
        conn.sock = tls
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            response.read(8192)
            return response.status
        finally:
            conn.close()
    raise last or OSError("no address")


# -- the files -----------------------------------------------------------------------


def _valid_sub(row) -> bool:
    if not isinstance(row, dict) or set(row) != {"id", "endpoint", "p256dh", "auth", "session",
                                                 "passkey", "created", "ua"}:
        return False
    try:
        check_endpoint(row["endpoint"])
        unb64url(row["p256dh"])
        unb64url(row["auth"])
    except ValueError:
        return False
    return (isinstance(row["id"], str) and _ID.fullmatch(row["id"]) is not None
            and isinstance(row["session"], str) and _SESSION_ID.fullmatch(row["session"]) is not None
            and isinstance(row["passkey"], str) and _KEY.fullmatch(row["passkey"]) is not None
            and type(row["created"]) in (int, float) and isinstance(row["ua"], str)
            and len(row["ua"]) <= UA_CAP)


class _Folder:
    """`<root>/remote`, opened by descriptor, never through a link."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _open(self, create: bool) -> int | None:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            root_fd = os.open(self.root, _DIR)
        except FileNotFoundError:
            if create:
                raise
            return None
        try:
            if create:
                try:
                    os.mkdir(FOLDER, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            try:
                fd = os.open(FOLDER, _SUBDIR, dir_fd=root_fd)
            except FileNotFoundError:
                if create:
                    raise
                return None
        finally:
            os.close(root_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError("the remote folder is not a plain directory")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, name: str) -> bytes | None:
        """The file's bytes; None when absent. OSError when it is a link,
        not regular or too large."""
        dir_fd = self._open(create=False)
        if dir_fd is None:
            return None
        try:
            try:
                fd = os.open(name, _READ, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_MAX:
                    raise OSError(errno.EINVAL, "not one of ours")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    os.fchmod(fd, 0o600)
                chunks, left = [], _FILE_MAX + 1
                while left > 0 and (chunk := os.read(fd, min(65536, left))):
                    chunks.append(chunk)
                    left -= len(chunk)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        data = b"".join(chunks)
        if len(data) > _FILE_MAX:
            raise OSError(errno.EINVAL, "too large")
        return data

    def write(self, name: str, data: bytes) -> None:
        dir_fd = self._open(create=True)
        try:
            tmp = f".{name}.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, _WRITE, 0o600, dir_fd=dir_fd)
            try:
                try:
                    os.fchmod(fd, 0o600)
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except OSError:
                    pass
                raise
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        finally:
            os.close(dir_fd)


# -- the service -----------------------------------------------------------------------


class Push:
    """Subscriptions, the VAPID key, the switch and the sender for one daemon.
    `wall` and `mono` are the clocks (injectable): `created` on the wall
    clock, coalescing on the monotonic one. Quiet hours are the daemon's
    (`daemon.quiet`, with its own local clock). `resolver` and `transport`
    are injectable too; `transport` stays None until `attach_system`, and
    nothing is ever sent without one."""

    def __init__(self, daemon, root, host: str | None, *, wall=time.time, mono=time.monotonic,
                 resolver=resolve):
        self.daemon = daemon
        self.folder = _Folder(Path(root))
        self.host = host
        self.origin = "https://" + host if host else None
        self.wall = wall
        self.mono = mono
        self.resolver = resolver
        self.transport = None
        self.encrypt = encrypt
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._attached = False
        self._key = None
        # The sender's state.
        self._seen: set[str] | None = None
        self._pending = False
        self._last_sent: float | None = None
        self._was_quiet = False
        # LOW-3 (review of PR #325): a push that reached no phone is tried
        # again after RETRY_S, coalesced and quiet-hours-bound like any other.
        self._failures = 0
        self._retry_at: float | None = None

    # -- life cycle ------------------------------------------------------------------

    def attach_system(self) -> None:
        """daemon2's `main()` only: the real transport, and leave to run."""
        self.transport = https_post
        self._attached = True

    def start(self) -> None:
        """`Daemon.start()`: the sender's thread, only once attached. Never
        sends, never touches the network itself."""
        if not self._attached or self._thread is not None or self._stopping.is_set():
            return
        self._thread = threading.Thread(target=self._run, name="jarvis-push", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """No push starts after this. A send under way stops before its next
        subscription or address, and within `SEND_BUDGET_S` in any case; it is
        waited for that long (one resolution may outlive it on its own
        daemon thread, `bounded`, but sends nothing)."""
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(SEND_BUDGET_S + 1)

    def observe(self, record) -> None:
        """The bus's observer: `inbox_changed` only sets an event."""
        if isinstance(record, dict) and record.get("kind") == "inbox_changed" and self._thread is not None:
            self._wake.set()

    def _run(self) -> None:
        try:
            self.seed()
        except Exception as exc:     # noqa: BLE001
            LOG.warning("phone push: the first read failed (%s)", type(exc).__name__)
        while not self._stopping.is_set():
            try:
                wait = self.next_wait()
            except Exception as exc:     # noqa: BLE001 — a bad clock never stops the sender
                LOG.warning("phone push: the next wake could not be worked out (%s)", type(exc).__name__)
                wait = RECHECK_S
            self._wake.wait(wait)
            self._wake.clear()
            if self._stopping.is_set():
                return
            try:
                self.evaluate()
            except Exception as exc:     # noqa: BLE001 — the sender never dies of one read
                LOG.warning("phone push: a check failed (%s)", type(exc).__name__)

    # -- settings ----------------------------------------------------------------------

    def settings(self) -> dict:
        try:
            raw = self.folder.read(SETTINGS)
            if raw is None:
                return {"enabled": True}
            value = json.loads(raw)
            if isinstance(value, dict) and set(value) == {"version", "enabled"} \
                    and value["version"] == 1 and isinstance(value["enabled"], bool):
                return {"enabled": value["enabled"]}
        except (OSError, ValueError, UnicodeError):
            pass
        LOG.warning("phone push: the notifications switch cannot be read; notifications stay on")
        return {"enabled": True}

    def set_enabled(self, enabled) -> dict:
        if not isinstance(enabled, bool):
            raise PushError(400, "enabled is true or false")
        with self._lock:
            self.folder.write(SETTINGS, json.dumps({"version": 1, "enabled": enabled}).encode())
        LOG.info("phone push: notifications turned %s at the desk", "on" if enabled else "off")
        self._wake.set()
        return {"enabled": enabled}

    # -- the VAPID key -------------------------------------------------------------------

    def _private_key(self):
        _w, _v, serialization, ec = _libs()
        with self._lock:
            if self._key is not None:
                return self._key
            try:
                raw = self.folder.read(VAPID_KEY)
            except OSError:
                raise PushError(503, "the notification key cannot be read") from None
            if raw is None:
                key = ec.generate_private_key(ec.SECP256R1())
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
                try:
                    self.folder.write(VAPID_KEY, json.dumps({"version": 1, "pem": pem.decode("ascii")}).encode())
                except OSError:
                    raise PushError(503, "the notification key cannot be saved") from None
                LOG.info("phone push: a notification key was made")
            else:
                try:
                    stored = json.loads(raw)
                    if not isinstance(stored, dict) or stored.get("version") != 1 or not isinstance(stored.get("pem"), str):
                        raise ValueError("not one of ours")
                    key = serialization.load_pem_private_key(stored["pem"].encode("ascii"), password=None)
                except (ValueError, TypeError, UnicodeError):
                    raise PushError(503, "the notification key cannot be read") from None
                if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                    raise PushError(503, "the notification key cannot be read")
            self._key = key
            return key

    def public_key(self) -> str:
        """The `applicationServerKey`: the public half, uncompressed, base64url."""
        _w, _v, serialization, _e = _libs()
        key = self._private_key()
        return b64url(key.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint))

    def _vapid_headers(self, host: str) -> dict:
        _w, Vapid02, _s, _e = _libs()
        # The subject is this app's https origin, as P-0 sent it and Apple
        # accepted (201, §10.1). RFC 8292 allows a mailto: or https: URL;
        # Apple refuses a malformed one (BadJwtToken), and a `@localhost`
        # mailto is reported refused, so the verified origin stays. It tells
        # the push service this tailnet's name, nothing more.
        claims = {"sub": self.origin or "mailto:owner@localhost", "aud": "https://" + host,
                  "exp": int(self.wall()) + VAPID_EXP_S}
        signed = Vapid02(private_key=self._private_key()).sign(claims)
        return {"Authorization": signed["Authorization"]}

    # -- subscriptions -------------------------------------------------------------------

    def subscriptions(self) -> list[dict]:
        """The stored subscriptions; [] when the file is missing or not one of ours."""
        try:
            raw = self.folder.read(SUBSCRIPTIONS)
            if raw is None:
                return []
            value = json.loads(raw)
            rows = value.get("subscriptions") if isinstance(value, dict) else None
            if value.get("version") != 1 or not isinstance(rows, list) or not all(_valid_sub(r) for r in rows):
                raise ValueError("malformed")
            return [dict(r) for r in rows]
        except (OSError, ValueError, UnicodeError, AttributeError):
            LOG.warning("phone push: the subscriptions cannot be read; nothing is sent")
            return []

    def _save(self, rows: list[dict]) -> None:
        self.folder.write(SUBSCRIPTIONS, json.dumps({"version": 1, "subscriptions": rows},
                                                    sort_keys=True).encode())

    def subscribe(self, session: dict, value) -> dict:
        """A signed-in phone's subscription, checked and stored for its
        session (replacing that session's earlier one, and taking over the
        same endpoint from any other)."""
        sub = value.get("subscription") if isinstance(value, dict) else None
        if not isinstance(sub, dict):
            raise PushError(400, "bad request")
        try:
            host, _path = check_endpoint(sub.get("endpoint"))
            p256dh, auth = check_keys(sub.get("keys"))
            public_addresses(host, self.resolver)
        except ValueError as exc:
            raise PushError(400, str(exc)) from None
        from .remote_auth import clean_text
        row = {"id": "p-" + secrets.token_hex(6), "endpoint": sub["endpoint"], "p256dh": p256dh,
               "auth": auth, "session": session["id"], "passkey": session["passkey"],
               "created": self.wall(), "ua": clean_text(session.get("ua", ""), UA_CAP)}
        with self._lock:
            rows = [r for r in self.subscriptions()
                    if r["session"] != session["id"] and r["endpoint"] != sub["endpoint"]]
            rows.append(row)
            rows.sort(key=lambda r: r["created"])
            rows = rows[-MAX_SUBSCRIPTIONS:]
            try:
                self._save(rows)
            except OSError:
                raise PushError(503, "the subscription could not be saved") from None
        LOG.info("phone push: passkey %s turned notifications on (%s)", session["passkey"], host)
        return {"subscribed": True}

    def unsubscribe(self, session: dict, value) -> dict:
        endpoint = value.get("endpoint") if isinstance(value, dict) else None
        if endpoint is not None and not isinstance(endpoint, str):
            raise PushError(400, "bad request")
        with self._lock:
            rows = self.subscriptions()
            kept = [r for r in rows if r["session"] != session["id"] and r["endpoint"] != endpoint]
            if len(kept) != len(rows):
                try:
                    self._save(kept)
                except OSError:
                    raise PushError(503, "the subscription could not be removed") from None
                LOG.info("phone push: passkey %s turned notifications off", session["passkey"])
        return {"subscribed": False}

    def subscribed(self, session: dict) -> bool:
        return any(r["session"] == session["id"] for r in self.subscriptions())

    def ended(self, kind: str, value: str) -> None:
        """`RemoteAuth.on_end`: a session ended (logout, the desk's End) or a
        passkey was revoked; its subscriptions go now. Never raises."""
        field = {"session": "session", "passkey": "passkey"}.get(kind)
        if field is None:
            return
        try:
            with self._lock:
                rows = self.subscriptions()
                kept = [r for r in rows if r[field] != value]
                if len(kept) != len(rows):
                    self._save(kept)
                    LOG.info("phone push: %d subscription(s) removed with the %s", len(rows) - len(kept), kind)
        except Exception as exc:     # noqa: BLE001
            LOG.warning("phone push: a subscription could not be removed (%s)", type(exc).__name__)

    def _remove(self, ids: set[str]) -> None:
        if not ids:
            return
        with self._lock:
            rows = self.subscriptions()
            try:
                self._save([r for r in rows if r["id"] not in ids])
            except OSError:
                LOG.warning("phone push: a gone subscription could not be removed")

    # -- the sender ----------------------------------------------------------------------

    def attention(self) -> tuple[set[str], int, int]:
        """(ids, count, most urgent rank) of the inbox rows at ranks 1 to 4."""
        from .inbox import rows
        found = [r for r in rows(self.daemon, with_checks=False, marks="ignore")
                 if r.get("rank") in ATTENTION_RANKS]
        ids = {r["id"] for r in found}
        return ids, len(found), min((r["rank"] for r in found), default=0)

    def seed(self) -> None:
        """What already waits when the sender starts: seen, never pushed. But
        when the start falls in quiet hours, or within `MORNING_GRACE_S` after
        they end (a restart at 07:05), the morning push is still due: what
        waits then is pushed once quiet hours are over (a restart after this
        morning's push may send a second one)."""
        ids, _count, _rank = self.attention()
        quiet = getattr(self.daemon, "quiet", None)
        morning = False
        if quiet is not None:
            from datetime import timedelta
            now = quiet.clock()
            morning = quiet.is_quiet(now) or quiet.is_quiet(now - timedelta(seconds=MORNING_GRACE_S))
        with self._lock:
            if self._seen is None:
                self._seen = ids
                self._was_quiet = self._was_quiet or morning

    def _quiet(self):
        quiet = getattr(self.daemon, "quiet", None)
        if quiet is None:
            return None, False
        now = quiet.clock()
        return quiet, quiet.is_quiet(now)

    def evaluate(self) -> dict | None:
        """One look: the push this calls for, sent, or None. See the module
        note for the rule."""
        ids, count, rank = self.attention()
        _q, quiet = self._quiet()
        enabled = self.settings()["enabled"]
        with self._lock:
            new = ids - self._seen if self._seen is not None else set()
            self._seen = ids
            if not enabled:
                self._pending, self._was_quiet = False, False
                self._failures, self._retry_at = 0, None
                return None
            if quiet:
                self._pending, self._was_quiet = False, True
                self._failures, self._retry_at = 0, None
                return None
            if self._was_quiet:
                # Quiet hours just ended: one push for what still waits.
                self._was_quiet = False
                if count:
                    self._pending = True
            if new:
                self._pending = True
            if not count:
                self._pending = False
            if not self._pending:
                self._failures, self._retry_at = 0, None
                return None
            now = self.mono()
            if self._last_sent is not None and now - self._last_sent < COALESCE_S:
                return None
            if self._retry_at is not None and now < self._retry_at:
                return None
            self._pending = False
        result = self.send_all(count, rank)
        with self._lock:
            if result["sent"]:
                # Reached at least one phone: the window starts now.
                self._last_sent = now
                self._failures, self._retry_at = 0, None
            elif result["failed"]:
                # Reached none: tried again later, with what waits then.
                self._pending = True
                self._retry_at = self.mono() + RETRY_S[min(self._failures, len(RETRY_S) - 1)]
                self._failures += 1
        return result

    def next_wait(self) -> float:
        waits = [RECHECK_S]
        with self._lock:
            if self._pending and self._last_sent is not None:
                waits.append(max(0.0, self._last_sent + COALESCE_S - self.mono()) + 0.05)
            if self._pending and self._retry_at is not None:
                waits.append(max(0.0, self._retry_at - self.mono()) + 0.05)
        quiet = getattr(self.daemon, "quiet", None)
        if quiet is not None:
            now = quiet.clock()
            ends = quiet.ends_at(now)
            if ends is not None:
                waits.append(max(1.0, (ends - now).total_seconds() + 1.0))
        return min(waits)

    def send_all(self, count: int, rank: int) -> dict:
        """One push, `payload(count, rank)`, to every subscription whose
        passkey is still active. What happened, in counts."""
        plaintext = payload(count, rank)
        if self.transport is None:
            return {"sent": 0, "failed": 0, "removed": 0}
        auth = getattr(self.daemon, "remote_auth", None)
        active = auth.active_passkeys() if auth is not None else set()
        sent = failed = 0
        gone: set[str] = set()
        # LOW-2 (review of PR #325): the whole send, every subscription, ends
        # within SEND_BUDGET_S, and stops at once when the daemon stops.
        deadline = time.monotonic() + SEND_BUDGET_S
        for sub in self.subscriptions():
            if sub["passkey"] not in active:
                continue
            if self._stopping.is_set() or time.monotonic() >= deadline:
                failed += 1
                LOG.warning("phone push: not sent (%s)", "stopping" if self._stopping.is_set()
                            else "the send's time is up")
                continue
            try:
                status = self.send_one(sub, plaintext, deadline)
            except PushError as exc:
                failed += 1
                LOG.warning("phone push: not sent (%s)", exc.word)
                continue
            except ValueError as exc:
                failed += 1
                LOG.warning("phone push: not sent (%s)", type(exc).__name__)
                continue
            except Exception as exc:     # noqa: BLE001 — never the endpoint, never the text
                failed += 1
                LOG.warning("phone push: the send failed (%s)", type(exc).__name__)
                continue
            if status in OK_STATUSES:
                sent += 1
            elif status in GONE_STATUSES:
                gone.add(sub["id"])
                LOG.info("phone push: the push service says a subscription is gone (%d); removed", status)
            else:
                failed += 1
                LOG.warning("phone push: the push service answered %d; the subscription is kept", status)
        self._remove(gone)
        if sent or failed or gone:
            LOG.info("phone push: %d sent, %d failed, %d removed", sent, failed, len(gone))
        return {"sent": sent, "failed": failed, "removed": len(gone)}

    def send_one(self, sub: dict, plaintext: bytes, deadline: float | None = None) -> int:
        deadline = deadline if deadline is not None else time.monotonic() + SEND_BUDGET_S
        host, path = check_endpoint(sub["endpoint"])
        addresses = public_addresses(host, self.resolver,
                                     max(0.0, min(RESOLVE_TIMEOUT_S, deadline - time.monotonic())))
        body, headers = self.encrypt(sub, plaintext)
        headers = dict(headers, **self._vapid_headers(host))
        headers.update({"TTL": str(TTL_S), "Urgency": "high",
                        "Content-Type": "application/octet-stream"})
        return self.transport(host, addresses, path, headers, body, SEND_TIMEOUT_S,
                              deadline=deadline, stop=self._stopping.is_set)

    def send_test(self) -> dict:
        """The desk's "Send test notification": one push now, with the
        current count, past coalescing and quiet hours (the owner asked).
        Bounded like every send (`SEND_BUDGET_S`), so the HUD's request
        waits at most that long."""
        if self.transport is None:
            raise PushError(409, "notifications are not running on this daemon")
        if not self.settings()["enabled"]:
            raise PushError(409, "notifications are off")
        if not self.subscriptions():
            raise PushError(409, "no phone has turned notifications on")
        _ids, count, rank = self.attention()
        return self.send_all(count, rank)

    # -- the desk's view -------------------------------------------------------------------

    def view(self) -> dict:
        """What the HUD shows: never an endpoint, a key or a secret."""
        rows = [{"id": r["id"], "service": check_endpoint(r["endpoint"])[0], "created": r["created"],
                 "passkey": r["passkey"], "ua": r["ua"]} for r in self.subscriptions()]
        quiet = getattr(self.daemon, "quiet", None)
        return {"enabled": self.settings()["enabled"], "running": self.transport is not None,
                "subscriptions": rows, "max_subscriptions": MAX_SUBSCRIPTIONS,
                "quiet": quiet.view() if quiet is not None else None}


# -- the routes ----------------------------------------------------------------------

API = "/m/api/push"
SUBSCRIBE = "/m/api/push/subscribe"


def route(handler, path: str, session: dict, body: bytes):
    """A signed-in phone's push routes (after `remote_auth.owner_status`), as
    (status, payload, extra headers); None for any other path."""
    if path not in (API, SUBSCRIBE):
        return None
    daemon = getattr(handler, "daemon", None)
    push = getattr(daemon, "push", None)
    if not isinstance(push, Push):
        return 404, {"error": "not found"}, []
    method = handler.command
    allowed = ("GET",) if path == API else ("POST", "DELETE")
    if method not in allowed:
        return 405, {"error": "method not allowed"}, [("Allow", ", ".join(allowed))]
    from .remote_auth import AuthError, _json_body
    try:
        if path == API:
            return 200, {"key": push.public_key(), "enabled": push.settings()["enabled"],
                         "subscribed": push.subscribed(session)}, []
        value = _json_body(body)
        if method == "POST":
            return 200, push.subscribe(session, value), []
        return 200, push.unsubscribe(session, value), []
    except PushError as exc:
        return exc.status, {"error": exc.word}, []
    except AuthError as exc:
        return exc.status, {"error": "bad request" if exc.status == 400 else exc.word}, []


def hud_route(daemon, parts, method, body):
    """`/phone/push...` on the HUD listener, after `remote_auth.hud_route`'s
    `owner_only` (which read the POST's `body`); None for any other path."""
    if parts[:2] != ["phone", "push"]:
        return None
    from .daemon import APIError, _object
    push = getattr(daemon, "push", None)
    if not isinstance(push, Push):
        raise APIError(404, "route not found")
    try:
        if parts == ["phone", "push"] and method == "GET":
            return 200, push.view()
        if parts == ["phone", "push", "settings"] and method == "POST":
            value = _object(body, ("enabled",), ("enabled",))
            push.set_enabled(value["enabled"])
            return 200, push.view()
        if parts == ["phone", "push", "test"] and method == "POST":
            _object(body, ())
            return 200, push.send_test()
    except PushError as exc:
        raise APIError(exc.status, exc.word) from None
    except OSError:
        raise APIError(503, "the notification settings could not be saved") from None
    raise APIError(404, "route not found")
