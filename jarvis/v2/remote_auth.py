"""Who the owner is off the PC: passkeys, pairing and phone sessions (phone
surface design PH-3..PH-6, §4, work package P-2).

The phone reaches the remote listener (`remote_listener.py`, 8408 on
loopback, `tailscale serve` in front). Being on the tailnet only gets a
request this far: **the owner off the PC is whoever holds a paired passkey
and verifies on it** (Face ID or the device passcode, both of which set the
WebAuthn UV flag; PH-3). Tailscale's identity headers are recorded with each
session as advisory text and never decide anything, because a local process
can send them to 8408 itself.

**Pairing starts only at the desk (PH-6).** The owner-only HUD route
`POST /phone/pair` (`hud_route`, `projects.owner_only` on the HUD listener)
opens a window: 5 minutes, one successful registration, closed early by the
owner (`POST /phone/pair/cancel`). It returns
`https://<host>/m/pair#<code>`, which the HUD shows as a QR code. The code is
256 random bits, lives only in this process's memory, is compared in
constant time, and travels in the URL's fragment, which a browser never
sends: it reaches neither the proxy's logs nor ours. "Single-use" means one
**successful** registration: a failed attempt (the phone's Face ID
cancelled, a refused response) may be retried with the same code while the
window is open; the first registration that lands closes it. The phone posts it with
`/m/api/pair/options` and again with `/m/api/pair/verify`. What is paired is
a **passkey**, not a device (§10.1: an iCloud Keychain passkey syncs to every
Apple device on the owner's Apple ID). At most `MAX_PASSKEYS` (3) are active;
a fourth is refused at the window, at the options and at the verify.
Registration asks for `residentKey: required` and `userVerification:
required`, and is verified with `require_user_verification=True` for
`rp_id` = the remote host and `origin` = `https://<host>`.

**Registration never signs in.** With `none` attestation (what iCloud sends)
a registration proves nothing about holding the private key, so a session is
only ever made by an assertion: the phone registers, then signs in.

**Login.** `/m/api/login/options` issues a server-side challenge (32 random
bytes, single-use, `CHALLENGE_S` = 60 s on the monotonic clock, pruned, and
capped per kind: login challenges at `MAX_LOGIN_CHALLENGES`, registration
challenges at `MAX_REGISTER_CHALLENGES` and only while a window is open; a
full pool refuses new ones (429) rather than evicting a pending one, so an
unauthenticated flood of `login/options` cannot evict anyone's ceremony).
`/m/api/login/verify` takes the challenge out of the store before anything
is verified (so a failed attempt spends it too), then requires a known,
unrevoked credential, the exact origin and rpId, `crossOrigin` not true
(and no `topOrigin`) in the client data, the UV flag, a valid signature, and
PH-4's counter rule: 0 is accepted while the stored counter is
0 (iCloud passkeys always report 0, §10.1); once a credential has reported a
nonzero counter, each new one must be greater. An unknown or used challenge
is 409; every other failure is 401, with no detail.

**Sessions (PH-5).** A random token (256 bits) in `__Host-jarvis_m`:
HttpOnly, Secure, SameSite=Strict, Path=/, no Domain, Max-Age 7 days. At rest
only its SHA-256 is kept. Each session records its passkey, the user agent
(cleaned, capped), created, last-seen and the advisory Tailscale headers.
`IDLE_S` 12 h idle and `ABSOLUTE_S` 7 days absolute, on the wall clock (they
outlive a restart). Logout ends the session; revoking a passkey (HUD) ends
every session it opened at once; ending one session (HUD) ends that one.

**A revoke holds in memory at once, and is durable only once written.** If
the passkeys file cannot be written the revoke answers 503, the passkey stays
refused in memory, the HUD shows it as "revoked, not saved" (`status()`'s
`state: "unsaved"`, `unsaved` list), and every later call (`status`,
`session_for`, login, pairing, a repeat Revoke) retries the write; a repeat
Revoke answers 200 once it lands. A restart before it lands loses it: the
file still says the passkey is active.

**The owner test (§4) is `is_remote_owner(handler)`**, and it is the only one
on the remote listener (`projects.is_owner` is false there by construction):
the request arrived on this daemon's remote listener, `Host` is exactly the
configured name, a valid session cookie names an active passkey, and any
method but GET/HEAD carries exactly `Origin: https://<host>`. Refusals are
400 (Host), 403 (Origin), 401 (session), with no body detail. Item 5, a fresh
assertion per Allow and Always, is P-4's: `answer_options` issues a
challenge bound to one approval, one answer ("allow" or "always"), the
session and its passkey (PH-4's sha256 of req_id, answer and a nonce;
single use, 60 s, at most 4 per session), and `answer_verify` checks it as
`login_verify` checks a sign-in, plus that binding and that the credential
is the session's own passkey; P-5's irreversible task verb (cancel) takes
the same assertion, bound to `task:<id>` and the verb (`TASK_DECISIONS`).
Past the session, `api_route` hands P-4's answer paths to `remote_answer`,
P-5's writes to `remote_write`, and every other path but logout and `me`
to `remote_api` (P-3's reads and event stream); `session_live` is the
stream's check, which does not touch last-seen. `recently_active` is
PH-10's "a phone session in the last 24 h"; `passkey_ready` is §4.1's
condition 4 without the assertion.

**Storage.** `<data root>/remote/` (0700): `passkeys.json` and
`sessions.json` (0600, atomic replacement through `stores._write_bytes`, read
without following a link). Both are protected state
(`tools/files._protected_state_paths`): no agent write tool, shell command or
grant changes them, and nor does a command on the `remote/` folder itself
(`protected_state`). A passkeys file that cannot be read is never
overwritten: pairing and login answer 503 until it reads again (each call
tries again; nothing sticks until a restart). A sessions file that cannot be
read signs everyone out.

`webauthn` is imported only when a passkey is registered or verified, so a
daemon whose environment lacks it still starts (503 on those routes), and
the HUD refuses to open a pairing window until it is installed.

SELF_PROTECTED: this module decides who counts as the owner off this
machine. Tests: tests/v2/remote_auth_check.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

LOG = logging.getLogger(__name__)

COOKIE = "__Host-jarvis_m"
MAX_PASSKEYS = 3
WINDOW_S = 300
CHALLENGE_S = 60
IDLE_S = 12 * 3600
ABSOLUTE_S = 7 * 24 * 3600
# The guards' strength, pinned by tests/v2/remote_auth_check.py.
CODE_BYTES = 32           # 256 bits of pairing code
TOKEN_BYTES = 32          # 256 bits of session token
CHALLENGE_BYTES = 32
# Outstanding challenges, per kind. A full pool refuses (429); nothing pending
# is evicted. Registration ones exist only while a window is open.
MAX_LOGIN_CHALLENGES = 32
MAX_REGISTER_CHALLENGES = 8
# Per-answer challenges (PH-4, P-4): their own pool, only for a signed-in
# phone, so neither sign-ins nor answers can crowd the other out; and capped
# per session, never across sessions (review of PR #313), so one session's
# abandoned prompts can never refuse another's. The same session asking again
# for the same card and answer replaces its earlier challenge.
MAX_ANSWER_CHALLENGES_PER_SESSION = 4
ANSWER_NONCE_BYTES = 32
# What a per-answer challenge binds: PH-4's sha256(req_id ‖ answer ‖ nonce),
# each part delimited (a req_id is `token_urlsafe`, never a NUL).
ANSWER_DOMAIN = b"jarvis-phone-answer-v1\x00"
ANSWER_DECISIONS = ("allow", "always")
# P-5: a task verb that cannot be undone takes the same per-answer assertion,
# bound to `task:<task id>` and the verb (a cancel ends the task for good:
# `CANCELLED` is terminal and only a blocked task resumes). An approval's
# req_id (`token_urlsafe`) never holds a colon, so the two never meet; and a
# verb's subject never takes an approval's answer, nor the reverse.
TASK_DECISIONS = ("cancel",)
_TASK_SUBJECT = re.compile(r"task:[0-9a-f]{8}")
# PH-10: a phone session seen this recently gives approvals the remote wait.
ACTIVE_S = 24 * 3600
MAX_SESSIONS = 32         # the least recently seen goes first
# The most answer challenges there can be at once (a bound, never a shared cap).
MAX_ANSWER_CHALLENGES = MAX_ANSWER_CHALLENGES_PER_SESSION * MAX_SESSIONS * 2
MAX_REVOKED = 16          # revoked passkey records kept, so a revoked id stays refused
LAST_SEEN_WRITE_S = 60    # last-seen is written at most this often per session
UA_CAP = 160
ADVISORY_CAP = 120
# Item 5 of design §4 aside, user verification is required everywhere a
# passkey is used: the library is told so and the result is checked again.
REQUIRE_UV = True

ADVISORY_HEADERS = (("login", "Tailscale-User-Login"), ("name", "Tailscale-User-Name"))
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_KEY = re.compile(r"k-[0-9a-f]{12}")
_SESSION_ID = re.compile(r"s-[0-9a-f]{16}")
_B64URL = re.compile(r"[A-Za-z0-9_-]{1,1400}")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")


class AuthError(Exception):
    """A refusal with its status and a fixed word: never the input, never why."""

    def __init__(self, status: int, word: str):
        super().__init__(word)
        self.status = status
        self.word = word


UNAUTHORIZED = "unauthorized"


def _refuse(why: str) -> AuthError:
    # The reason goes to the log at debug level only; the client sees 401.
    LOG.debug("phone auth refused: %s", why)
    return AuthError(401, UNAUTHORIZED)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    if not isinstance(text, str) or not _B64URL.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def clean_text(value, cap: int) -> str:
    """Phone-supplied text for a record: control and invisible characters
    out, whitespace collapsed, at most `cap` characters. Still untrusted:
    the HUD renders it as text only."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub(" ", value)
    text = " ".join(text.split())
    return text[:cap]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def counter_ok(stored: int, new: int) -> bool:
    """PH-4's counter rule: 0 is accepted while the stored counter is 0 (an
    iCloud passkey always reports 0); otherwise the new counter must be
    greater than the stored one, so 5 then 5 (or 0) is refused."""
    if stored == 0 and new == 0:
        return True
    return new > stored


def _client_challenge(credential) -> str:
    """The challenge the phone signed, from its clientDataJSON: the key the
    server-side store is looked up by (and spent at). A ceremony run inside
    another origin's frame (`crossOrigin: true`, or a `topOrigin`) is
    refused here, before anything is spent."""
    if not isinstance(credential, dict):
        raise AuthError(400, "bad request")
    response = credential.get("response")
    if not isinstance(response, dict):
        raise AuthError(400, "bad request")
    try:
        client = json.loads(unb64url(response.get("clientDataJSON")))
        challenge = client["challenge"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise AuthError(400, "bad request") from None
    if not isinstance(challenge, str) or not _B64URL.fullmatch(challenge):
        raise AuthError(400, "bad request")
    if client.get("crossOrigin") not in (None, False) or "topOrigin" in client:
        raise _refuse("a cross-origin ceremony")
    return challenge


INSTALL_WEBAUTHN = "passkeys need the webauthn package: uv pip install -e . in the daemon's venv"


def _webauthn():
    try:
        import webauthn
        from webauthn.helpers import structs
    except ImportError:
        raise AuthError(503, "passkeys unavailable") from None
    return webauthn, structs


# -- files -----------------------------------------------------------------------------


def _read_private(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist. A link, a non-file
    or an unreadable file raises OSError: never followed, never guessed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise OSError("not a regular file of a sane size")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_int(value) -> bool:
    return type(value) is int


def _is_num(value) -> bool:
    return type(value) in (int, float) and value == value and abs(value) < 1e12


def _valid_passkey(row) -> bool:
    if not isinstance(row, dict):
        return False
    return (isinstance(row.get("key"), str) and _KEY.fullmatch(row["key"]) is not None
            and isinstance(row.get("id"), str) and _B64URL.fullmatch(row["id"]) is not None
            and isinstance(row.get("public_key"), str) and _B64URL.fullmatch(row["public_key"]) is not None
            and _is_int(row.get("sign_count")) and 0 <= row["sign_count"] < 2 ** 32
            and _is_num(row.get("created"))
            and (row.get("last_used") is None or _is_num(row["last_used"]))
            and (row.get("revoked") is None or _is_num(row["revoked"]))
            and isinstance(row.get("backed_up"), bool))


def _valid_session(row) -> bool:
    if not isinstance(row, dict):
        return False
    advisory = row.get("advisory")
    return (isinstance(row.get("id"), str) and _SESSION_ID.fullmatch(row["id"]) is not None
            and isinstance(row.get("token_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", row["token_sha256"]) is not None
            and isinstance(row.get("passkey"), str) and _KEY.fullmatch(row["passkey"]) is not None
            and isinstance(row.get("ua"), str) and len(row["ua"]) <= UA_CAP
            and _is_num(row.get("created")) and _is_num(row.get("last_seen"))
            and isinstance(advisory, dict)
            and all(isinstance(advisory.get(k), str) and len(advisory[k]) <= ADVISORY_CAP
                    for k, _ in ADVISORY_HEADERS))


# -- the state -------------------------------------------------------------------------


class RemoteAuth:
    """Passkeys, the pairing window, challenges and sessions for one remote
    host. `root` is the folder the two files live in (`<data root>/remote`);
    `wall` and `mono` are the clocks (injectable for the tests): sessions on
    the wall clock, the window and challenges on the monotonic one."""

    def __init__(self, root: Path, host: str | None, *, wall=time.time, mono=time.monotonic):
        self.dir = Path(root)
        self.host = host
        self.origin = "https://" + host if host else None
        self.wall = wall
        self.mono = mono
        # The bound remote listener (set by the daemon after binding it):
        # `is_remote_owner` requires the request to have arrived on it.
        self.server = None
        self._lock = threading.RLock()
        self._window: dict | None = None
        self._generation = 0
        self._challenges: dict[str, dict] = {}
        self._loaded = False
        self._broken: str | None = None
        self._user_handle: str | None = None
        self._passkeys: list[dict] = []
        self._sessions: dict[str, dict] = {}     # token hash -> record
        self._written: dict[str, float] = {}     # session id -> last-seen written
        # A revocation held in memory that the passkeys file does not have
        # yet (its write failed): retried on every later call (`_flush`).
        self._unsaved = False
        # PH-10: when a phone session was last seen (wall clock), kept past
        # a logout; from the sessions file at load.
        self._activity: float | None = None

    # -- files -----------------------------------------------------------------------

    @property
    def passkeys_path(self) -> Path:
        return self.dir / "passkeys.json"

    @property
    def sessions_path(self) -> Path:
        return self.dir / "sessions.json"

    def _ensure_dir(self) -> None:
        if self.dir.is_symlink():
            raise AuthError(503, "phone state unavailable")
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.dir)
        if not stat.S_ISDIR(info.st_mode):
            raise AuthError(503, "phone state unavailable")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(self.dir, 0o700)

    def _load(self) -> None:
        """Read both files, once they read. A failed read is not remembered
        as loaded: the next call tries again, so a transient error does not
        last until a restart."""
        if self._loaded:
            return
        self._broken = None
        self._passkeys, self._user_handle, self._sessions = [], None, {}
        if self.dir.is_symlink():
            self._broken = "the phone state folder is a link"
            return
        try:
            raw = _read_private(self.passkeys_path)
            if raw is not None:
                data = json.loads(raw)
                rows = data.get("passkeys") if isinstance(data, dict) else None
                handle = data.get("user_handle") if isinstance(data, dict) else None
                if (data.get("version") != 1 or not isinstance(rows, list)
                        or not all(_valid_passkey(r) for r in rows)
                        or not (handle is None or (isinstance(handle, str) and _B64URL.fullmatch(handle)))):
                    raise ValueError("malformed")
                self._passkeys = [dict(r) for r in rows]
                self._user_handle = handle
        except (OSError, ValueError, AttributeError):
            self._passkeys = []
            self._broken = "passkeys.json cannot be read"
            LOG.warning("phone: %s; pairing and sign-in are off until it is fixed", self._broken)
            return
        try:
            raw = _read_private(self.sessions_path)
            if raw is not None:
                data = json.loads(raw)
                rows = data.get("sessions") if isinstance(data, dict) else None
                if data.get("version") != 1 or not isinstance(rows, list):
                    raise ValueError("malformed")
                active = {p["key"] for p in self._passkeys if p.get("revoked") is None}
                for row in rows:
                    if _valid_session(row) and row["passkey"] in active:
                        self._sessions[row["token_sha256"]] = dict(row)
                        self._written[row["id"]] = row["last_seen"]
                        self._seen(row["last_seen"])
        except (OSError, ValueError, AttributeError):
            self._sessions = {}
            LOG.warning("phone: sessions.json cannot be read; every phone session is signed out")
        self._loaded = True
        self._prune_sessions()

    def _write(self, path: Path, value: dict) -> None:
        from .stores import StoreError, _write_bytes
        self._ensure_dir()
        try:
            _write_bytes(path, (json.dumps(value, indent=1, sort_keys=True) + "\n").encode())
        except StoreError:
            LOG.warning("phone: %s could not be written", path.name)
            raise AuthError(503, "phone state unavailable") from None
        os.chmod(path, 0o600)

    def _passkeys_value(self, passkeys: list[dict], handle: str | None) -> dict:
        rows = [{k: v for k, v in p.items() if k != "unsaved"} for p in passkeys]
        return {"version": 1, "user_handle": handle, "passkeys": rows}

    def _write_passkeys(self, rows: list[dict], handle: str | None) -> None:
        """Write the passkeys file. Every caller writes the in-memory rows
        (plus what it adds), so a write that lands also lands any revocation
        still unsaved."""
        self._write(self.passkeys_path, self._passkeys_value(rows, handle))
        self._unsaved = False

    def _save_passkeys(self) -> None:
        self._write_passkeys(self._passkeys, self._user_handle)

    def _flush(self) -> None:
        """Retry an unsaved revocation's write; a failure leaves the flag."""
        if self._unsaved and not self._broken:
            try:
                self._save_passkeys()
                LOG.info("phone: an unsaved revocation is now saved")
            except AuthError:
                pass

    def _save_sessions(self) -> None:
        rows = sorted(self._sessions.values(), key=lambda r: r["created"])
        self._write(self.sessions_path, {"version": 1, "sessions": rows})
        for row in rows:
            self._written[row["id"]] = row["last_seen"]

    def _require_usable(self) -> None:
        self._load()
        self._flush()
        if self._broken:
            raise AuthError(503, "phone state unavailable")
        if not self.host:
            raise AuthError(503, "phone state unavailable")

    # -- passkeys ----------------------------------------------------------------------

    def _active(self) -> list[dict]:
        return [p for p in self._passkeys if p.get("revoked") is None]

    def _by_key(self, key: str) -> dict | None:
        return next((p for p in self._passkeys if p["key"] == key), None)

    # -- the pairing window (PH-6) ---------------------------------------------------

    def _window_open(self) -> dict | None:
        window = self._window
        if window is not None and self.mono() >= window["expires"]:
            self._window = None
            window = None
        return window

    def open_window(self) -> dict:
        """Open (or replace) the pairing window: the URL with its one-time
        code in the fragment, and the seconds it stays open. The code is
        returned here and nowhere else."""
        try:
            _webauthn()
        except AuthError:
            raise AuthError(503, INSTALL_WEBAUTHN) from None
        with self._lock:
            self._require_usable()
            if len(self._active()) >= MAX_PASSKEYS:
                raise AuthError(409, "three passkeys are paired; revoke one first")
            self._generation += 1
            code = b64url(secrets.token_bytes(CODE_BYTES))
            self._window = {"code": code, "expires": self.mono() + WINDOW_S,
                            "generation": self._generation}
            # A replaced window's registrations die with it.
            self._challenges = {k: c for k, c in self._challenges.items() if c["kind"] != "register"}
            LOG.info("phone: pairing window opened for %d s", WINDOW_S)
            return {"url": f"https://{self.host}/m/pair#{code}", "expires_in": WINDOW_S}

    def cancel_window(self) -> None:
        with self._lock:
            if self._window is not None:
                LOG.info("phone: pairing window closed by the owner")
            self._close_window()

    def _close_window(self) -> None:
        self._window = None
        self._challenges = {k: c for k, c in self._challenges.items() if c["kind"] != "register"}

    def _check_code(self, code) -> dict:
        """The open window `code` opens, or a 401 with no detail: no window,
        an expired one, no code, or the wrong one all look the same."""
        window = self._window_open()
        if not isinstance(code, str) or not code or len(code) > 200:
            raise _refuse("no pairing code")
        if window is None:
            raise _refuse("no pairing window is open")
        if not hmac.compare_digest(code.encode("utf-8", "replace"), window["code"].encode()):
            raise _refuse("wrong pairing code")
        return window

    # -- challenges ----------------------------------------------------------------------

    def _issue(self, kind: str, challenge: bytes | None = None, **extra) -> bytes:
        now = self.mono()
        self._challenges = {k: c for k, c in self._challenges.items() if c["expires"] > now}
        if kind == "register" and self._window_open() is None:
            raise _refuse("no pairing window is open")
        if kind == "answer":
            # Per session (`answer_options`), and per family (P-5): a task
            # verb's prompts and an approval's are counted apart, so abandoned
            # cancel prompts can never refuse an Allow, nor the reverse.
            # Bounded by MAX_SESSIONS times that, twice.
            mine = [c for c in self._challenges.values()
                    if c["kind"] == "answer" and c.get("session") == extra.get("session")
                    and c.get("task", False) == extra.get("task", False)]
            full = len(mine) >= MAX_ANSWER_CHALLENGES_PER_SESSION
        else:
            cap = MAX_REGISTER_CHALLENGES if kind == "register" else MAX_LOGIN_CHALLENGES
            full = sum(1 for c in self._challenges.values() if c["kind"] == kind) >= cap
        if full:
            # Refused, never evicting a pending one (module note).
            raise AuthError(429, "too many sign-ins in progress; try again in a minute"
                            if kind != "answer" else "too many answers in progress; try again in a minute")
        if challenge is None:
            challenge = secrets.token_bytes(CHALLENGE_BYTES)
        self._challenges[b64url(challenge)] = {"kind": kind, "expires": now + CHALLENGE_S, **extra}
        return challenge

    def _spend(self, key: str, kind: str) -> tuple[bytes, dict]:
        """Take a challenge out of the store (single use: a failed attempt
        spends it too). Unknown, used, expired or of another kind: 409."""
        entry = self._challenges.pop(key, None)
        if entry is None or entry["kind"] != kind or self.mono() >= entry["expires"]:
            raise AuthError(409, "challenge expired")
        return unb64url(key), entry

    # -- registration (pairing) -------------------------------------------------------

    def pair_options(self, code) -> dict:
        webauthn, structs = _webauthn()
        with self._lock:
            self._require_usable()
            window = self._check_code(code)
            active = self._active()
            if len(active) >= MAX_PASSKEYS:
                raise AuthError(409, "three passkeys are paired; revoke one first")
            if self._user_handle is None:
                handle = b64url(secrets.token_bytes(16))
                # Saved before it is handed out: every passkey names one user.
                self._write_passkeys(self._passkeys, handle)
                self._user_handle = handle
            challenge = self._issue("register", generation=window["generation"])
            options = webauthn.generate_registration_options(
                rp_id=self.host, rp_name="Jarvis", user_id=unb64url(self._user_handle),
                user_name="owner", user_display_name="Jarvis owner", challenge=challenge,
                timeout=CHALLENGE_S * 1000,
                authenticator_selection=structs.AuthenticatorSelectionCriteria(
                    resident_key=structs.ResidentKeyRequirement.REQUIRED,
                    user_verification=structs.UserVerificationRequirement.REQUIRED),
                exclude_credentials=[structs.PublicKeyCredentialDescriptor(id=unb64url(p["id"]))
                                     for p in active])
            return json.loads(webauthn.options_to_json(options))

    def pair_verify(self, code, credential) -> dict:
        webauthn, _structs = _webauthn()
        with self._lock:
            self._require_usable()
            window = self._check_code(code)
            key = _client_challenge(credential)
            challenge, entry = self._spend(key, "register")
            if entry.get("generation") != window["generation"]:
                raise AuthError(409, "challenge expired")
            if len(self._active()) >= MAX_PASSKEYS:
                raise AuthError(409, "three passkeys are paired; revoke one first")
            try:
                verified = webauthn.verify_registration_response(
                    credential=credential, expected_challenge=challenge,
                    expected_rp_id=self.host, expected_origin=self.origin,
                    require_user_verification=REQUIRE_UV)
            except Exception as exc:     # noqa: BLE001 — every failure is the same 401
                raise _refuse(f"registration: {type(exc).__name__}") from None
            if REQUIRE_UV and not verified.user_verified:
                raise _refuse("registration without user verification")
            cred_id = b64url(verified.credential_id)
            if any(p["id"] == cred_id for p in self._passkeys):
                raise _refuse("that credential is (or was) already paired")
            now = self.wall()
            row = {"key": "k-" + secrets.token_hex(6), "id": cred_id,
                   "public_key": b64url(verified.credential_public_key),
                   "sign_count": int(verified.sign_count), "created": now, "last_used": None,
                   "revoked": None, "backed_up": bool(verified.credential_backed_up),
                   "device_type": str(getattr(verified.credential_device_type, "value",
                                              verified.credential_device_type))[:32],
                   "aaguid": str(verified.aaguid)[:64]}
            self._write_passkeys(self._passkeys + [row], self._user_handle)
            self._passkeys.append(row)
            # One registration per window: it closes now.
            self._close_window()
            LOG.info("phone: passkey %s paired (%s)", row["key"],
                     "synced" if row["backed_up"] else "single device")
            return {"paired": True, "passkey": row["key"]}

    # -- login (PH-4's login half) ----------------------------------------------------

    def login_options(self) -> dict:
        webauthn, structs = _webauthn()
        with self._lock:
            self._require_usable()
            challenge = self._issue("login")
            # Discoverable credentials (residentKey was required): no
            # credential ids are handed to whoever asks.
            options = webauthn.generate_authentication_options(
                rp_id=self.host, challenge=challenge, timeout=CHALLENGE_S * 1000,
                allow_credentials=[],
                user_verification=structs.UserVerificationRequirement.REQUIRED)
            return json.loads(webauthn.options_to_json(options))

    def login_verify(self, credential, *, ua: str = "", advisory: dict | None = None) -> tuple[str, dict]:
        """A session for a verified assertion: (the token for the cookie,
        the session's record). The token is returned here and kept nowhere."""
        webauthn, _structs = _webauthn()
        with self._lock:
            self._require_usable()
            key = _client_challenge(credential)
            challenge, _entry = self._spend(key, "login")
            cred_id = credential.get("rawId") if isinstance(credential.get("rawId"), str) else credential.get("id")
            if not isinstance(cred_id, str) or credential.get("id") != cred_id:
                raise _refuse("credential id and rawId differ")
            stored = next((p for p in self._passkeys if p["id"] == cred_id), None)
            if stored is None:
                raise _refuse("unknown credential")
            if stored.get("revoked") is not None:
                raise _refuse("revoked credential")
            handle = (credential.get("response") or {}).get("userHandle")
            if handle not in (None, "") and handle != self._user_handle:
                raise _refuse("another user's credential")
            try:
                verified = webauthn.verify_authentication_response(
                    credential=credential, expected_challenge=challenge,
                    expected_rp_id=self.host, expected_origin=self.origin,
                    credential_public_key=unb64url(stored["public_key"]),
                    credential_current_sign_count=stored["sign_count"],
                    require_user_verification=REQUIRE_UV)
            except Exception as exc:     # noqa: BLE001
                raise _refuse(f"assertion: {type(exc).__name__}") from None
            if REQUIRE_UV and not verified.user_verified:
                raise _refuse("assertion without user verification")
            if not counter_ok(stored["sign_count"], int(verified.new_sign_count)):
                raise _refuse("signature counter did not increase")
            now = self.wall()
            updated = dict(stored, sign_count=int(verified.new_sign_count), last_used=now)
            rows = [updated if p is stored else p for p in self._passkeys]
            self._write_passkeys(rows, self._user_handle)
            self._passkeys = rows
            token = b64url(secrets.token_bytes(TOKEN_BYTES))
            advisory = advisory or {}
            session = {"id": "s-" + secrets.token_hex(8), "token_sha256": token_hash(token),
                       "passkey": stored["key"], "ua": clean_text(ua, UA_CAP),
                       "created": now, "last_seen": now,
                       "advisory": {k: clean_text(advisory.get(k, ""), ADVISORY_CAP)
                                    for k, _ in ADVISORY_HEADERS}}
            self._sessions[session["token_sha256"]] = session
            self._seen(now)
            self._prune_sessions()
            self._save_sessions()
            # Advisory, logged and never trusted (PH-3).
            LOG.info("phone: passkey %s signed in (Tailscale says %r, advisory)",
                     stored["key"], session["advisory"]["login"] or "nothing")
            return token, dict(session)

    # -- per-answer assertions (PH-4, P-4) -------------------------------------------------

    @staticmethod
    def answer_challenge(req_id: str, decision: str, nonce: bytes) -> bytes:
        """PH-4's challenge: sha256(req_id ‖ answer ‖ nonce), each part
        delimited, under a fixed domain prefix."""
        return hashlib.sha256(ANSWER_DOMAIN + req_id.encode("utf-8") + b"\x00"
                              + decision.encode("ascii") + b"\x00" + nonce).digest()

    def _session_passkey(self, session: dict) -> dict:
        """The session's own passkey, still paired and unrevoked, of a
        session that still holds; else 401."""
        row = self._sessions.get(session.get("token_sha256")) if isinstance(session, dict) else None
        if row is None or self._expired(row, self.wall()):
            raise _refuse("the session ended")
        passkey = self._by_key(row["passkey"])
        if passkey is None or passkey.get("revoked") is not None:
            raise _refuse("the session's passkey is revoked")
        return passkey

    def answer_options(self, session: dict, req_id: str, decision: str) -> dict:
        """The request options for one answer (PH-4): a challenge bound to
        this approval, this exact answer ("allow" or "always"; or, P-5, the
        task `task:<id>` and "cancel", `TASK_DECISIONS`), this session
        and its passkey; single use, `CHALLENGE_S` on the monotonic clock,
        at most `MAX_ANSWER_CHALLENGES_PER_SESSION` per session (asking again
        for the same card and answer replaces the earlier one). `allowCredentials`
        names the session's own passkey only."""
        webauthn, structs = _webauthn()
        if not isinstance(req_id, str) or not req_id or len(req_id) > 64:
            raise AuthError(400, "bad request")
        task = _TASK_SUBJECT.fullmatch(req_id) is not None
        if decision not in (TASK_DECISIONS if task else ANSWER_DECISIONS) or (not task and ":" in req_id):
            raise AuthError(400, "bad request")
        with self._lock:
            self._require_usable()
            passkey = self._session_passkey(session)
            # This session asking again for this card and this answer: the
            # earlier challenge goes (its prompt was abandoned or retried).
            self._challenges = {k: c for k, c in self._challenges.items()
                                if not (c["kind"] == "answer" and c.get("session") == session["token_sha256"]
                                        and c.get("req_id") == req_id and c.get("decision") == decision)}
            nonce = secrets.token_bytes(ANSWER_NONCE_BYTES)
            challenge = self.answer_challenge(req_id, decision, nonce)
            self._issue("answer", challenge, req_id=req_id, decision=decision, nonce=nonce,
                        session=session["token_sha256"], passkey=passkey["key"], task=task)
            options = webauthn.generate_authentication_options(
                rp_id=self.host, challenge=challenge, timeout=CHALLENGE_S * 1000,
                allow_credentials=[structs.PublicKeyCredentialDescriptor(id=unb64url(passkey["id"]))],
                user_verification=structs.UserVerificationRequirement.REQUIRED)
            return json.loads(webauthn.options_to_json(options))

    def answer_verify(self, session: dict, credential, req_id: str, decision: str) -> None:
        """Verify one answer's assertion (PH-4), or raise: its challenge is
        spent first (a failed attempt spends it too; unknown, used or
        expired is 409); it must have been issued for exactly this approval,
        this answer, this session and its passkey (an Allow's assertion is
        never an Always, nor any other card's); the credential is the
        session's own passkey, unrevoked; `crossOrigin` is not true;
        origin, rpId, the UV flag, the signature and the counter rule as at
        sign-in. Every failure but the challenge's is 401, with no detail."""
        webauthn, _structs = _webauthn()
        with self._lock:
            self._require_usable()
            key = _client_challenge(credential)
            challenge, entry = self._spend(key, "answer")
            if (entry.get("req_id") != req_id or entry.get("decision") != decision
                    or entry.get("session") != session.get("token_sha256")
                    or not hmac.compare_digest(challenge, self.answer_challenge(
                        entry["req_id"], entry["decision"], entry["nonce"]))):
                raise _refuse("the assertion was issued for another answer")
            passkey = self._session_passkey(session)
            if entry.get("passkey") != passkey["key"]:
                raise _refuse("the assertion was issued for another passkey")
            cred_id = credential.get("rawId") if isinstance(credential.get("rawId"), str) else credential.get("id")
            if not isinstance(cred_id, str) or credential.get("id") != cred_id or cred_id != passkey["id"]:
                raise _refuse("not the session's passkey")
            handle = (credential.get("response") or {}).get("userHandle")
            if handle not in (None, "") and handle != self._user_handle:
                raise _refuse("another user's credential")
            try:
                verified = webauthn.verify_authentication_response(
                    credential=credential, expected_challenge=challenge,
                    expected_rp_id=self.host, expected_origin=self.origin,
                    credential_public_key=unb64url(passkey["public_key"]),
                    credential_current_sign_count=passkey["sign_count"],
                    require_user_verification=REQUIRE_UV)
            except Exception as exc:     # noqa: BLE001
                raise _refuse(f"answer assertion: {type(exc).__name__}") from None
            if REQUIRE_UV and not verified.user_verified:
                raise _refuse("answer assertion without user verification")
            if not counter_ok(passkey["sign_count"], int(verified.new_sign_count)):
                raise _refuse("signature counter did not increase")
            now = self.wall()
            updated = dict(passkey, sign_count=int(verified.new_sign_count), last_used=now)
            rows = [updated if p is passkey else p for p in self._passkeys]
            # The counter is saved before the answer counts: a write that
            # fails refuses it (503), so a counter can never go back.
            self._write_passkeys(rows, self._user_handle)
            self._passkeys = rows
            LOG.info("phone: passkey %s verified an answer (%s)", passkey["key"], decision)

    def passkey_ready(self, session: dict | None = None) -> tuple[bool, str]:
        """§4.1 condition 4, without the assertion: the passkeys read, and a
        paired, unrevoked passkey requiring user verification exists; with a
        session, it is the session's own. (ok, a fixed reason)."""
        with self._lock:
            self._load()
            if self._broken:
                return False, "the passkeys file cannot be read"
            if not self.host:
                return False, "no tailnet name is configured"
            if not REQUIRE_UV:
                return False, "user verification is not required"
            if session is not None:
                try:
                    self._session_passkey(session)
                except AuthError:
                    return False, "the session's passkey is not paired"
                return True, ""
            return (True, "") if self._active() else (False, "no passkey is paired")

    def _seen(self, at: float) -> None:
        if self._activity is None or at > self._activity:
            self._activity = at

    def recently_active(self) -> bool:
        """PH-10: a phone is paired (an active, unrevoked passkey) and a
        phone session was seen within `ACTIVE_S` (24 h)."""
        with self._lock:
            self._load()
            if self._broken or not self._active():
                return False
            now = self.wall()
            return self._activity is not None and now - self._activity < ACTIVE_S

    # -- sessions (PH-5) ------------------------------------------------------------------

    def _expired(self, row: dict, now: float) -> bool:
        return (now - row["created"] >= ABSOLUTE_S or now - row["last_seen"] >= IDLE_S
                or row["created"] - now > 300 or row["last_seen"] - now > 300)

    def _prune_sessions(self) -> bool:
        now = self.wall()
        active = {p["key"] for p in self._active()}
        before = len(self._sessions)
        self._sessions = {h: r for h, r in self._sessions.items()
                          if r["passkey"] in active and not self._expired(r, now)}
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda h: self._sessions[h]["last_seen"])
            del self._sessions[oldest]
        return len(self._sessions) != before

    def session_for(self, cookie_values: list[str] | None) -> dict | None:
        """The live session the request's `Cookie` header(s) name, or None:
        exactly one `__Host-jarvis_m` of the right shape, whose hash is a
        session that is neither idle nor past its absolute end, of an
        unrevoked passkey. Touches its last-seen."""
        token = _cookie_token(cookie_values)
        if token is None:
            return None
        with self._lock:
            self._load()
            self._flush()
            if self._broken:
                return None
            digest = token_hash(token)
            row = self._sessions.get(digest)
            if row is None:
                return None
            now = self.wall()
            passkey = self._by_key(row["passkey"])
            if passkey is None or passkey.get("revoked") is not None or self._expired(row, now):
                del self._sessions[digest]
                self._try_save_sessions()
                return None
            row["last_seen"] = max(row["last_seen"], now)
            self._seen(row["last_seen"])
            if now - self._written.get(row["id"], 0) >= LAST_SEEN_WRITE_S:
                self._try_save_sessions()
            return dict(row)

    def session_live(self, digest: str) -> bool:
        """Whether the session with this token hash still holds (not logged
        out, its passkey not revoked, not idle, not past its end), without
        touching its last-seen: the phone's event stream asks this before
        every write (`remote_api.events`), and an open stream is not
        activity."""
        with self._lock:
            if self._broken or not isinstance(digest, str):
                return False
            row = self._sessions.get(digest)
            if row is None:
                return False
            passkey = self._by_key(row["passkey"])
            if passkey is None or passkey.get("revoked") is not None:
                return False
            return not self._expired(row, self.wall())

    def _try_save_sessions(self) -> None:
        try:
            self._save_sessions()
        except AuthError:
            pass        # memory is authoritative; the refusal already holds

    def logout(self, session: dict) -> None:
        with self._lock:
            self._sessions.pop(session["token_sha256"], None)
            self._try_save_sessions()
            LOG.info("phone: session %s signed out", session["id"])

    def end_session(self, session_id: str) -> bool:
        with self._lock:
            self._load()
            for digest, row in list(self._sessions.items()):
                if row["id"] == session_id:
                    del self._sessions[digest]
                    self._try_save_sessions()
                    LOG.info("phone: session %s ended from the HUD", session_id)
                    return True
            return False

    def revoke(self, key: str) -> bool:
        """Revoke a passkey: every session it opened ends now, in memory,
        and it is refused from now on. True once the passkeys file says so:
        a repeat revoke is idempotent (an already-saved one answers True; one
        whose write failed retries it and answers True when it lands). False
        only for a key it does not know. 503 while the file cannot be written
        (the revoke still holds in memory and is retried by every later call,
        and once more when the daemon stops: `final_save`)."""
        with self._lock:
            self._load()
            if self._broken:
                raise AuthError(503, "phone state unavailable")
            passkey = self._by_key(key)
            if passkey is None:
                return False
            if passkey.get("revoked") is not None:
                if not passkey.get("unsaved"):
                    return True                # already revoked and saved
                if self._unsaved:
                    self._save_passkeys()      # 503 again if it still fails
                self._mark_saved()
                LOG.info("phone: passkey %s revocation saved on retry", key)
                return True
            self._sessions = {h: r for h, r in self._sessions.items() if r["passkey"] != key}
            rows = [dict(p, revoked=self.wall(), unsaved=True) if p is passkey else p
                    for p in self._passkeys]
            revoked = sorted((p for p in rows if p.get("revoked") is not None), key=lambda p: p["revoked"])
            for old in revoked[:-MAX_REVOKED]:
                rows.remove(old)
            self._passkeys = rows
            self._unsaved = True
            self._try_save_sessions()
            LOG.info("phone: passkey %s revoked", key)
            self._save_passkeys()
            self._mark_saved()
            return True

    def final_save(self) -> list[str]:
        """The daemon is stopping: one last try at an unsaved revocation's
        write. The keys still unsaved after it, each named in an ERROR line
        (a restart would bring them back), or []."""
        with self._lock:
            if not self._unsaved:
                return []
            self._flush()
            if not self._unsaved:
                return []
            keys = [p["key"] for p in self._passkeys if p.get("unsaved")]
            LOG.error("phone: the revocation of passkey %s is still not saved at shutdown; after "
                      "a restart it is active again: revoke it again in Settings > Phone",
                      ", ".join(keys) or "(unknown)")
            return keys

    def _mark_saved(self) -> None:
        """The file now has every revocation: drop the in-memory marks."""
        if not self._unsaved:
            self._passkeys = [{k: v for k, v in p.items() if k != "unsaved"} for p in self._passkeys]

    # -- the HUD's view -------------------------------------------------------------------

    def status(self) -> dict:
        """What the HUD's Phone section shows: never a code, a token or its
        hash, a public key or a credential id."""
        with self._lock:
            self._load()
            self._flush()
            self._mark_saved()
            if not self._broken and self._prune_sessions():
                self._try_save_sessions()
            window = self._window_open()
            sessions_of: dict[str, list] = {}
            for row in sorted(self._sessions.values(), key=lambda r: -r["last_seen"]):
                sessions_of.setdefault(row["passkey"], []).append(
                    {"id": row["id"], "ua": row["ua"], "created": row["created"],
                     "last_seen": row["last_seen"], "advisory": dict(row["advisory"])})
            passkeys = [{"key": p["key"], "created": p["created"], "last_used": p["last_used"],
                         "backed_up": p["backed_up"], "sessions": sessions_of.get(p["key"], [])}
                        for p in self._active()]
            unsaved = [{"key": p["key"], "revoked": p["revoked"]}
                       for p in self._passkeys if self._unsaved and p.get("unsaved")]
            state = "unreadable" if self._broken else ("unsaved" if self._unsaved else "ok")
            return {"state": state,
                    "unsaved": unsaved,
                    "host_configured": bool(self.host),
                    "max_passkeys": MAX_PASSKEYS,
                    "pairing": {"open": window is not None,
                                "expires_in": max(0, int(window["expires"] - self.mono())) if window else 0},
                    "passkeys": passkeys}


def _cookie_token(values: list[str] | None) -> str | None:
    """The session token from the request's Cookie header(s): exactly one
    `__Host-jarvis_m`, of the token's shape, else None."""
    if not values:
        return None
    found = []
    for value in values:
        if not isinstance(value, str):
            return None
        for part in value.split(";"):
            name, sep, item = part.strip().partition("=")
            if sep and name == COOKIE:
                found.append(item.strip())
    if len(found) != 1 or not _TOKEN.fullmatch(found[0]):
        return None
    return found[0]


def session_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={ABSOLUTE_S}; Secure; HttpOnly; SameSite=Strict"


def cleared_cookie() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"


# -- the owner test on the remote listener (design §4) -----------------------------


def _headers(handler, name: str) -> list[str] | None:
    get_all = getattr(getattr(handler, "headers", None), "get_all", None)
    if not callable(get_all):
        return None
    return get_all(name)


def owner_status(handler) -> tuple[int, dict | None]:
    """(200, the session) when the request is the owner's on the remote
    listener (§4 items 1 to 4), else (400, None) for a bad Host, (403, None)
    for a write without the exact Origin, or (401, None): not this daemon's
    remote listener, or no valid session."""
    auth = getattr(handler, "remote_auth", None)
    server = getattr(handler, "server", None)
    # Item 1: this daemon's remote listener, and nothing else.
    if (not isinstance(auth, RemoteAuth) or auth.server is None or server is not auth.server
            or not getattr(server, "remote_only", False) or not auth.host
            or server.server_address[1] != auth.server.server_address[1]):
        return 401, None
    # Item 2: exactly the configured name, once.
    if _headers(handler, "Host") != [auth.host]:
        return 400, None
    # Item 4: any method but GET and HEAD carries exactly the phone's Origin.
    if getattr(handler, "command", None) not in ("GET", "HEAD") and _headers(handler, "Origin") != [auth.origin]:
        return 403, None
    # Item 3: a live session of an active passkey.
    session = auth.session_for(_headers(handler, "Cookie"))
    if session is None:
        return 401, None
    return 200, session


def is_remote_owner(handler) -> bool:
    """Design §4 items 1 to 4: the only owner test on the remote listener."""
    return owner_status(handler)[0] == 200


# -- the phone's routes (on the remote listener only) ------------------------------

WORDS = {400: "bad request", 401: UNAUTHORIZED, 403: "cross-origin request refused",
         404: "not found", 409: "conflict", 413: "request body too large",
         503: "passkeys unavailable"}

PAIR_OPTIONS = "/m/api/pair/options"
PAIR_VERIFY = "/m/api/pair/verify"
LOGIN_OPTIONS = "/m/api/login/options"
LOGIN_VERIFY = "/m/api/login/verify"
LOGOUT = "/m/api/logout"
ME = "/m/api/me"
# The only routes a request without a session reaches, POST each.
OPEN_ROUTES = frozenset({PAIR_OPTIONS, PAIR_VERIFY, LOGIN_OPTIONS, LOGIN_VERIFY})


def _json_body(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        raise AuthError(400, "bad request") from None
    if not isinstance(value, dict):
        raise AuthError(400, "bad request")
    return value


def _advisory(handler) -> dict:
    out = {}
    for key, header in ADVISORY_HEADERS:
        values = _headers(handler, header) or []
        out[key] = clean_text(values[0], ADVISORY_CAP) if len(values) == 1 else ""
    return out


def api_route(handler, path: str, body: bytes) -> tuple[int, dict, list[tuple[str, str]]]:
    """Every `/m/api` request on the remote listener, after its own Host and
    Origin checks and with its body already read (`remote_listener`): the
    status, the JSON payload and any extra headers. The four pairing and
    login routes need no session; everything else is 401 without one. With
    one, logout and `me` are here and every other path is P-3's read API
    (`remote_api.route`: its reads, its event stream, 404 for anything
    else); the status is None when the stream has already answered."""
    auth = getattr(handler, "remote_auth", None)
    method = handler.command
    try:
        if not isinstance(auth, RemoteAuth):
            return 401, {"error": UNAUTHORIZED}, []
        if method == "POST" and path in OPEN_ROUTES:
            # The listener checked these; checked again, and item 1 too.
            server = getattr(handler, "server", None)
            if auth.server is None or server is not auth.server:
                return 401, {"error": UNAUTHORIZED}, []
            if _headers(handler, "Host") != [auth.host]:
                return 400, {"error": WORDS[400]}, []
            if _headers(handler, "Origin") != [auth.origin]:
                return 403, {"error": WORDS[403]}, []
            value = _json_body(body)
            if path == PAIR_OPTIONS:
                return 200, auth.pair_options(value.get("code")), []
            if path == PAIR_VERIFY:
                return 200, auth.pair_verify(value.get("code"), value.get("credential")), []
            if path == LOGIN_OPTIONS:
                return 200, auth.login_options(), []
            credential = value.get("credential")
            if not isinstance(credential, dict):
                raise AuthError(400, "bad request")
            ua = (_headers(handler, "User-Agent") or [""])[0]
            token, session = auth.login_verify(credential, ua=ua, advisory=_advisory(handler))
            return 200, {"signed_in": True}, [("Set-Cookie", session_cookie(token))]
        status, session = owner_status(handler)
        if status != 200:
            return status, {"error": WORDS.get(status, UNAUTHORIZED)}, []
        if path == LOGOUT and method == "POST":
            auth.logout(session)
            return 200, {"signed_in": False}, [("Set-Cookie", cleared_cookie())]
        if path == ME and method in ("GET", "HEAD"):
            # §4.1's last result, to show (P-4): never what authorizes.
            return 200, {"signed_in": True, "paired": True,
                         "session": {"created": session["created"],
                                     "ends": session["created"] + ABSOLUTE_S},
                         "secure": secure_view(getattr(handler, "daemon", None))}, []
        if path in (LOGOUT, ME):
            return 404, {"error": WORDS[404]}, []
        # P-4's answers and a card's detail (`remote_answer`), P-5's sends,
        # new chats and task verbs (`remote_write`), then P-3's reads and the
        # event stream (`remote_api`), only past the session check above.
        # None from the reads: the stream answered on the socket.
        from . import remote_answer
        answered = remote_answer.route(handler, path, session, body)
        if answered is not None:
            return answered
        from . import remote_write
        written = remote_write.route(handler, path, session, body)
        if written is not None:
            return written
        from . import remote_api
        return remote_api.route(handler, path, session)
    except AuthError as exc:
        word = exc.word if exc.status in (409, 429, 503) else WORDS.get(exc.status, UNAUTHORIZED)
        return exc.status, {"error": word}, []


# -- the HUD's owner-only routes (the HUD listener) --------------------------------


def secure_view(daemon) -> dict:
    """§4.1's last result (`remote_secure.SecureMonitor.view`), or "not
    checked yet" for a daemon without one."""
    monitor = getattr(daemon, "secure", None)
    if monitor is None:
        from .remote_secure import LABELS
        return {"ok": False, "failed": "pending", "reason": LABELS["pending"],
                "label": LABELS["pending"], "at": None}
    return monitor.view()


def remote_status(daemon) -> dict:
    """`/status`'s `remote`: the port, whether the listener bound, whether a
    host is configured, and §4.1's last result (`secure`, P-4: which check
    failed, in this module's fixed words). No name, no secret."""
    bound = getattr(daemon, "remote_port", None)
    want = getattr(daemon, "_remote_want", None)
    return {"port": bound if bound is not None else want, "bound": bound is not None,
            "host_configured": bool(getattr(daemon, "remote_host", None)),
            "secure": secure_view(daemon)}


def hud_route(handler, daemon, parts, query):
    """`/phone/...` on the HUD listener; None for any other path. Owner-only
    (`projects.owner_only`) before anything else on every `/phone` path:

      GET  /phone                         the window, the passkeys and their sessions
      POST /phone/pair                    open (or replace) the window: {url, expires_in}
      POST /phone/pair/cancel             close it
      POST /phone/passkeys/<key>/revoke   revoke a passkey and end its sessions
      POST /phone/sessions/<id>/end       end one session
    """
    if not parts or parts[0] != "phone":
        return None
    from .daemon import APIError, _object
    from .projects import owner_only
    owner_only(handler, daemon)
    _object(query, ())
    auth = getattr(daemon, "remote_auth", None)
    if not isinstance(auth, RemoteAuth):
        raise APIError(404, "route not found")
    method = handler.command
    if method == "POST":
        handler._body()
    try:
        if parts == ["phone"] and method == "GET":
            return 200, dict(auth.status(), remote=remote_status(daemon))
        if parts == ["phone", "pair"] and method == "POST":
            if not auth.host:
                raise APIError(409, "no tailnet name is configured (JARVIS_REMOTE_HOST)")
            if getattr(daemon, "remote_port", None) is None:
                raise APIError(409, "the remote listener is not running")
            return 200, auth.open_window()
        if parts == ["phone", "pair", "cancel"] and method == "POST":
            auth.cancel_window()
            return 200, {"open": False}
        if (len(parts) == 4 and parts[1] == "passkeys" and parts[3] == "revoke" and method == "POST"
                and _KEY.fullmatch(parts[2])):
            if not auth.revoke(parts[2]):
                raise APIError(404, "no such passkey")
            return 200, {"revoked": True}
        if (len(parts) == 4 and parts[1] == "sessions" and parts[3] == "end" and method == "POST"
                and _SESSION_ID.fullmatch(parts[2])):
            if not auth.end_session(parts[2]):
                raise APIError(404, "no such session")
            return 200, {"ended": True}
    except AuthError as exc:
        raise APIError(exc.status, exc.word) from None
    raise APIError(404, "route not found")
