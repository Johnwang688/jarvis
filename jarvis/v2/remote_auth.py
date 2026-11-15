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
400 (Host), 403 (Origin), 401 (session), with no body detail.

**Item 5 is the unlock window (WS-5c, replacing PH-4's fresh assertion per
action; `docs/plans/2026-11-15-ws5-phone-working-set-plan.md` §5).** One
passkey assertion opens it: `unlock_options` issues a challenge bound to
`unlock:<session id>`, "unlock", the session, its passkey and the session's
`_unlock_gen` (single use, 60 s, one per session: asking again replaces
it), and `unlock_verify` checks it as `login_verify` checks a sign-in, plus
that binding, the generation and the session's own passkey, saves the
counter, and only then opens `_unlocks[token hash]` for `UNLOCK_S` (1 h) on
the monotonic clock, in memory only (a restart locks every phone). Every
gated action (Allow, Always, a deferral's Approve and retry, cancel, every
PR action, Approve selected, Done, Undo) calls `require_unlocked`, which
slides it, or raises `Locked`: exactly `locked_reply()`, 423
`{"error": "locked", "unlock": true}`, never 401, taken before `AuthError`
by every catch. A session-only action slides an open window
(`touch_unlock`) and never opens one; reads, the stream, `me`, stt and push
never touch it. Every ending (Lock now, logout, the desk's End, a revoke,
expiry or prune, a session found ended) runs `_end_unlock` under the lock:
the window goes, the session's unlock challenges are deleted and its
generation is bumped (Lock now), or retired with its ops (every other).

**Op tokens (§5.3a)**: each gated action spends one server-issued token
(`issue_ops`: HMAC-SHA256 under `_op_key`, 32 random bytes made at start,
over `{sid, n, t}` with the server's issue time), checked after the window:
signature (410: forged or from before a restart) and session (400) outside
any lock, then in `Ops.admit`, as one step under its lock with one `now`, the
token's time (410), pruning, a duplicate (409, never run again), the rate
and the capacity (429, `Rate.take_if`), and the `running` record. Records
are kept until the token expires, never for a running op, never evicted;
`op_status` reads one back by this session's digest only.

Past the session, `api_route` hands P-7's `POST /m/api/stt` to
`remote_stt`, P-6's push paths to `push`, the morning review's
`/m/api/review...` to `review`, PM-D's PR action (`/m/api/inbox/pr`) to
`pr_merge`, P-4's answer paths to `remote_answer`, P-5's writes and WS-5c's
Done and Undo to `remote_write`, and every other path but logout, `me` and
the unlock and op routes to `remote_api` (P-3's reads and event stream); a logout,
the desk's End and a revoke tell `on_end` (P-6 deletes that session's or
passkey's push subscriptions); `session_live` is the
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
# The unlock window (WS-5c, `docs/plans/2026-11-15-ws5-phone-working-set-plan.md`
# §5): one passkey assertion opens it for `UNLOCK_S`, and every gated action
# (Allow, Always, a deferral's Approve and retry, cancel, every PR action,
# Approve selected, Done and Undo) slides it. In memory only, on the
# monotonic clock: a restart locks every phone. No hard stop but the session.
UNLOCK_S = 3600
# Unlock challenges: their own pool, only for a signed-in phone, at most one
# per session (asking again replaces the earlier one), so the bound across
# sessions is one per session.
MAX_UNLOCK_CHALLENGES_PER_SESSION = 1
ANSWER_NONCE_BYTES = 32
# What an unlock challenge binds: sha256(subject ‖ "unlock" ‖ nonce), each part
# delimited, under its own domain. The subject is `unlock:<session id>`.
UNLOCK_DOMAIN = b"jarvis-phone-unlock-v1\x00"
UNLOCK_DECISIONS = ("unlock",)
_UNLOCK_SUBJECT = re.compile(r"unlock:s-[0-9a-f]{16}")
# PH-10: a phone session seen this recently gives approvals the remote wait.
ACTIVE_S = 24 * 3600
MAX_SESSIONS = 32         # the least recently seen goes first
# The challenge families a signed-in phone can hold (WS-5c removed the
# per-action ones: approval, task, defer, review, pr), and the most there can
# be at once (a bound, never a shared cap).
ANSWER_FAMILIES = ("unlock",)
MAX_ANSWER_CHALLENGES = MAX_UNLOCK_CHALLENGES_PER_SESSION * MAX_SESSIONS * len(ANSWER_FAMILIES)
# Operation tokens (WS-5c §5.3a): replay protection without per-action
# challenges. Server-issued, signed with a key made at start and held only in
# memory (a restart makes every earlier token fail), good for `OP_TTL_S` from
# the server's own issue time, at most `OP_FUTURE_S` ahead of now.
OP_TTL_S = 600
OP_FUTURE_S = 1.0
OP_TOKENS = 4             # handed out per request
OP_KEY_BYTES = 32
OP_NONCE_BYTES = 16
OP_DOMAIN = b"jarvis-phone-op-v1\x00"
OP_ISSUE_RATE = 30        # token requests per `OP_ISSUE_S`, per session
OP_ISSUE_S = 60.0
# The `Ops` store's bounds: a full store refuses (429), never evicts.
OPS_PER_SESSION = 64
OPS_MAX = OPS_PER_SESSION * MAX_SESSIONS
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
    """A refusal with its status and a fixed word: never the input, never why.
    `extra` holds fixed, code-written fields only (an op's `duplicate` and
    `state`, an expired token's `expired`)."""

    def __init__(self, status: int, word: str, extra: dict | None = None):
        super().__init__(word)
        self.status = status
        self.word = word
        self.extra = dict(extra or {})


UNAUTHORIZED = "unauthorized"
LOCKED = "locked"


class Locked(AuthError):
    """WS-5c §5.3: a gated action while this session's unlock window is
    closed. Always exactly `locked_reply()`, never a 401 (the phone reads a
    401 as signed out), and every catch takes it before `AuthError`."""

    def __init__(self):
        super().__init__(423, LOCKED, {"unlock": True})


def locked_reply() -> tuple[int, dict, list]:
    """The one answer for a closed window: 423 {"error": "locked", "unlock": true}."""
    return 423, {"error": LOCKED, "unlock": True}, []


# The statuses whose own words a phone route passes on (code-written, fixed);
# anything else is the status's fixed word (`WORDS`).
OWN_WORDS = (409, 410, 429, 503)


def auth_reply(exc: AuthError) -> tuple[int, dict, list]:
    """An `AuthError` as a phone route answers it (`Locked` is taken first,
    by every catch): its own words for 409, 410, 429 and 503 plus its fixed
    extra fields; else the status's fixed word."""
    if exc.status in OWN_WORDS:
        return exc.status, {"error": exc.word, **exc.extra}, []
    return exc.status, {"error": WORDS.get(exc.status, UNAUTHORIZED)}, []


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


# -- operation tokens and the Ops store (WS-5c §5.3a) ------------------------------------

_OP_TOKEN = re.compile(r"o1\.([A-Za-z0-9_-]{1,400})\.([A-Za-z0-9_-]{43})")
_OP_NONCE = re.compile(r"[A-Za-z0-9_-]{22}")          # 16 bytes, base64url
FORGED_OP = ("this tap is from before Jarvis restarted, or is not genuine: nothing was done by "
             "this request")
EXPIRED_OP = "this tap expired: nothing was done by this request"
OP_DUPLICATE = "already sent"
OP_REUSED = "op token reused"
OPS_FULL = "too many actions are still recorded; wait a minute"
RATE_WORD = "too many writes from this phone; wait a minute"
OP_ISSUE_WORD = "too many requests for tap tokens; wait a minute"
OP_STATES = ("running", "completed", "refused", "uncertain")
# The fixed words a confirmed result is recorded as, per family (Sol r5-2,
# r6-2): never model text, never a response body. A PR result is
# `PrActions._outcome`'s phrase; "changed" is the 422 exception path
# (`pr_merge.py`), never a normal return.
PR_CODES = {"merged": "merged", "updated": "updated", "update requested": "update_requested",
            "not updated": "not_updated", "not read again": "not_read_again", "changed": "changed",
            "queued": "queued", "head moved": "head_moved", "closed": "closed",
            "not mergeable": "not_mergeable"}
OP_CODES = {"pr": frozenset(PR_CODES.values()),
            "approval": frozenset({"allowed", "always_written"}),
            "task": frozenset({"cancelled"}),
            "working": frozenset({"done", "undone"}),
            "review": frozenset({"approved"})}


def parse_op(token) -> dict:
    """Step 1 of §5.3: an op token's shape, or 400. `o1.` ‖ base64url of the
    payload `{sid, n, t}` ‖ `.` ‖ base64url of its HMAC-SHA256. Only shape:
    the signature and the session are `RemoteAuth.verify_op`'s (after the
    window), the time `Ops.admit`'s (inside its lock)."""
    if not isinstance(token, str):
        raise AuthError(400, "bad request")
    found = _OP_TOKEN.fullmatch(token)
    if found is None:
        raise AuthError(400, "bad request")
    try:
        payload, mac = unb64url(found.group(1)), unb64url(found.group(2))
        fields = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise AuthError(400, "bad request") from None
    if (not isinstance(fields, dict) or set(fields) != {"sid", "n", "t"} or len(mac) != 32
            or not isinstance(fields["sid"], str) or not _SESSION_ID.fullmatch(fields["sid"])
            or not isinstance(fields["n"], str) or not _OP_NONCE.fullmatch(fields["n"])
            or not _is_num(fields["t"])):
        raise AuthError(400, "bad request")
    return {"payload": payload, "mac": mac, "sid": fields["sid"], "n": fields["n"], "t": float(fields["t"])}


def op_digest(method: str, path: str, body: dict) -> str:
    """What one op token is spent on: sha256 of the method, the path and the
    canonical body without the token."""
    data = {k: v for k, v in body.items() if k != "op"} if isinstance(body, dict) else {}
    text = "\x00".join((method, path, json.dumps(data, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=True, default=str)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def op_outcome(family: str, status: int, payload, result) -> tuple[str, str]:
    """(outcome, code) of an op whose mutation returned: `ok` only when the
    route's own answer says so (a 2xx with `ok` true, or a 2xx of a family
    whose answer has no `ok`), `unknown` for "not read again" and the 409
    uncertain reply, else `failed`; the code from the family's fixed words,
    else `other`."""
    payload = payload if isinstance(payload, dict) else {}
    if family == "pr" and isinstance(result, str):
        result = PR_CODES.get(result)
    code = result if isinstance(result, str) and result in OP_CODES.get(family, ()) else "other"
    if code == "not_read_again" or payload.get("uncertain") is True:
        return "unknown", code
    if 200 <= status < 300:
        if "ok" in payload:
            return ("ok" if payload["ok"] is True else "failed"), code
        return "ok", code
    return "failed", code


def op_words(state: str, outcome, code) -> str:
    """What the phone shows for an op's recorded state (§5.3a): "Done" only
    for a completed op whose result was confirmed and is not an Update
    GitHub only accepted (Sol r6-1)."""
    if state == "running":
        return "Still running"
    if state == "refused" or (state == "completed" and outcome == "failed"):
        return "Not done"
    if state == "completed" and outcome == "ok":
        return "Update requested; check at the desk" if code == "update_requested" else "Done"
    if state == "completed":
        return "Finished; check at the desk"
    return "Unknown: check at the desk"


class Op:
    """One admitted operation: its key, its reservation, and how far the
    route got (`admitted`, then `mutating` immediately before the mutation
    call, then `returned` once it returned). `result` is the code-written
    result the route saw (`op_outcome`)."""

    def __init__(self, ops: "Ops", key: tuple[str, str], reservation: str):
        self.ops = ops
        self.key = key
        self.reservation = reservation
        self.phase = "admitted"
        self.result = None

    def mutating(self) -> None:
        self.phase = "mutating"

    def returned(self) -> None:
        self.phase = "returned"


class Ops:
    """The admitted operations, by (session digest, nonce): `{state, digest,
    reservation, expires, outcome, code}`, kept until the token's own expiry
    and never for a `running` one. One lock serialises admission, every state
    change and pruning; nothing is evicted (a full store is 429). The lock
    order is always this lock, then the session rate's (`Rate.take_if`)."""

    def __init__(self, mono):
        self.mono = mono
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], dict] = {}

    def _prune(self, now: float) -> None:
        # A running record is never pruned: its token is refused (410) by the
        # same snapshot's time check before this runs.
        self._records = {k: r for k, r in self._records.items()
                         if r["state"] == "running" or now < r["expires"]}

    def admit(self, session_digest: str, n: str, t: float, digest: str, rate) -> str:
        """Step 4 of §5.3, as one step under the lock with one `now`: the
        token's time (410), pruning, a duplicate (409), the rate and the
        capacity (429, through `rate(room)`, which records a write only when
        both pass), then the record, `running`. Returns its reservation."""
        with self._lock:
            now = self.mono()
            if not (t <= now + OP_FUTURE_S and now < t + OP_TTL_S):
                raise AuthError(410, EXPIRED_OP, {"expired": True})
            self._prune(now)
            key = (session_digest, n)
            found = self._records.get(key)
            if found is not None:
                if hmac.compare_digest(found["digest"], digest):
                    raise AuthError(409, OP_DUPLICATE, {"duplicate": True, "state": found["state"]})
                raise AuthError(409, OP_REUSED)

            def room() -> bool:
                # Called by `Rate.take_if` with this lock already held: it
                # reads the records and never takes the lock again.
                mine = sum(1 for k in self._records if k[0] == session_digest)
                return mine < OPS_PER_SESSION and len(self._records) < OPS_MAX
            verdict = rate(room)
            if verdict == "rate":
                raise AuthError(429, RATE_WORD)
            if verdict != "go":
                raise AuthError(429, OPS_FULL)
            reservation = secrets.token_hex(16)
            self._records[key] = {"state": "running", "digest": digest, "reservation": reservation,
                                  "expires": t + OP_TTL_S, "outcome": None, "code": None}
            return reservation

    def mark(self, key, reservation: str, state: str, outcome=None, code=None) -> bool:
        """A running record's terminal state, named by its reservation: one
        that does not match (pruned, retired, another record) changes
        nothing. No state ever releases the token."""
        if state not in OP_STATES or state == "running":
            return False
        with self._lock:
            record = self._records.get(key)
            if (record is None or record["state"] != "running"
                    or not hmac.compare_digest(record["reservation"], reservation)):
                return False
            record.update(state=state, outcome=outcome, code=code)
            return True

    def settle(self, op: Op, answer, family: str) -> None:
        """The state of `op` from how far its route got and what it
        answered: never mutated (an exception or a definite refusal)
        `refused`; an exception after `mutating` before the call returned
        `uncertain`; the call returned `completed`, with its outcome (an
        exception while the answer was built: `unknown`)."""
        if op.phase == "admitted":
            self.mark(op.key, op.reservation, "refused")
        elif op.phase == "mutating":
            self.mark(op.key, op.reservation, "uncertain", "unknown")
        elif answer is None:
            self.mark(op.key, op.reservation, "completed", "unknown",
                      op_outcome(family, 409, {"uncertain": True}, op.result)[1])
        else:
            outcome, code = op_outcome(family, answer[0], answer[1], op.result)
            self.mark(op.key, op.reservation, "completed", outcome, code)

    def status(self, session_digest: str, n: str) -> dict:
        """`{state, outcome, code, words}` of this session's op `n`; another
        session's, a pruned one or one never admitted is `unknown`."""
        with self._lock:
            record = self._records.get((session_digest, n))
            if record is None:
                return {"state": "unknown", "outcome": None, "code": None,
                        "words": op_words("unknown", None, None)}
            return {"state": record["state"], "outcome": record["outcome"], "code": record["code"],
                    "words": op_words(record["state"], record["outcome"], record["code"])}

    def retire(self, session_digest: str) -> None:
        """A session ended: every record of it goes (its tokens can never
        verify again: their `sid` no longer exists)."""
        with self._lock:
            self._records = {k: r for k, r in self._records.items() if k[0] != session_digest}

    def count(self, session_digest: str | None = None) -> int:
        with self._lock:
            if session_digest is None:
                return len(self._records)
            return sum(1 for k in self._records if k[0] == session_digest)


def gated(handler, session: dict, path: str, body: dict, op: dict, family: str, act):
    """Steps 2 to 6 of §5.3 for one gated action whose body (and op token,
    `parse_op`) already passed step 1: the window (423; nothing else runs),
    the token's signature and session (400, 410), admission (410, 409, 429),
    QH-3's presence, then `act(ticket)`, the route's act-time checks and its
    mutation, which answers `(status, payload, headers)` and calls
    `ticket.mutating()` immediately before the mutation and
    `ticket.returned()` once it returned. The op's state is settled however
    `act` ends."""
    from . import remote_write
    auth = handler.remote_auth
    rate = remote_write.rate_of(handler.server)
    digest = session.get("token_sha256") or ""
    ticket = auth.admit_op(session, handler.command, path, body, op,
                           lambda room: rate.take_if(digest, remote_write.clock(), room))
    seen = getattr(getattr(handler, "daemon", None), "owner_seen", None)
    if callable(seen):
        try:
            seen()                      # QH-3: an authorised gated action is presence
        except Exception as exc:  # noqa: BLE001 — never why an action fails
            LOG.warning("phone: presence not recorded (%s)", type(exc).__name__)
    answer = None
    try:
        answer = act(ticket)
        return answer
    finally:
        auth.ops.settle(ticket, answer, family)


INSTALL_WEBAUTHN ="passkeys need the webauthn package: uv pip install -e . in the daemon's venv"


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
        # Told when a session ends (logout, the desk's End) or a passkey is
        # revoked: `fn("session", id)` / `fn("passkey", key)`. P-6's push
        # deletes the subscriptions that belonged to it (push.Push.ended).
        self.on_end: list = []
        # WS-5c: each session's unlock window, by token hash: {passkey,
        # until (monotonic)}; and its generation, bumped by every ending, which
        # an unlock challenge must still carry when it is verified. Memory
        # only: a restart locks every phone.
        self._unlocks: dict[str, dict] = {}
        self._unlock_gen: dict[str, int] = {}
        # §5.3a: the op tokens' signing key, this process's own (a restart
        # makes every earlier token fail), the admitted ops, and each
        # session's recent token requests.
        self._op_key = secrets.token_bytes(OP_KEY_BYTES)
        self.ops = Ops(lambda: self.mono())
        self._issued: dict[str, list] = {}

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
        if kind == "unlock":
            # Per session (`unlock_options` replaces this session's earlier
            # one first), so one session's prompts never refuse another's.
            mine = [c for c in self._challenges.values()
                    if c["kind"] == "unlock" and c.get("session") == extra.get("session")]
            full = len(mine) >= MAX_UNLOCK_CHALLENGES_PER_SESSION
        else:
            cap = MAX_REGISTER_CHALLENGES if kind == "register" else MAX_LOGIN_CHALLENGES
            full = sum(1 for c in self._challenges.values() if c["kind"] == kind) >= cap
        if full:
            # Refused, never evicting a pending one (module note).
            raise AuthError(429, "too many sign-ins in progress; try again in a minute"
                            if kind != "unlock" else "too many unlocks in progress; try again in a minute")
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

    # -- the unlock window (WS-5c §5.1, §5.2) -------------------------------------------

    @staticmethod
    def unlock_challenge(subject: str, nonce: bytes) -> bytes:
        """An unlock's challenge: sha256(subject ‖ "unlock" ‖ nonce), each
        part delimited, under its own domain prefix."""
        return hashlib.sha256(UNLOCK_DOMAIN + subject.encode("utf-8") + b"\x00"
                              + b"unlock" + b"\x00" + nonce).digest()

    def _session_passkey(self, session: dict) -> dict:
        """The session's own passkey, still paired and unrevoked, of a
        session that still holds; else 401 (and a session found ended has
        its window, challenges and ops retired: §5.8)."""
        digest = session.get("token_sha256") if isinstance(session, dict) else None
        row = self._sessions.get(digest) if isinstance(digest, str) else None
        if row is None or self._expired(row, self.wall()):
            if isinstance(digest, str):
                self._end_unlock(digest, retire=True, why="the session ended")
            raise _refuse("the session ended")
        passkey = self._by_key(row["passkey"])
        if passkey is None or passkey.get("revoked") is not None:
            self._end_unlock(digest, retire=True, why="the passkey was revoked")
            raise _refuse("the session's passkey is revoked")
        return passkey

    def _end_unlock(self, digest: str, *, retire: bool = False, why: str = "") -> None:
        """Under `self._lock`: the window of the session with this token hash
        ends, every unlock challenge it holds is deleted (so an assertion
        signed before and delivered after is 409: Sol r2-1), and its
        generation is bumped. `retire` (the session itself ended): its
        generation entry, its ops and its token-request record go instead."""
        had = self._unlocks.pop(digest, None) is not None
        self._challenges = {k: c for k, c in self._challenges.items()
                            if not (c["kind"] == "unlock" and c.get("session") == digest)}
        if retire:
            self._unlock_gen.pop(digest, None)
            self._issued.pop(digest, None)
            self.ops.retire(digest)
        else:
            self._unlock_gen[digest] = self._unlock_gen.get(digest, 0) + 1
        if had:
            LOG.info("phone: an unlock window ended (%s)", why or "ended")

    def _open_unlock(self, digest: str, passkey: dict) -> dict | None:
        """The session's window when it is open and was opened by this
        passkey, else None (a lapsed one is dropped)."""
        held = self._unlocks.get(digest)
        if held is None:
            return None
        if held["passkey"] != passkey["key"] or not self.mono() < held["until"]:
            self._unlocks.pop(digest, None)
            LOG.info("phone: an unlock window lapsed")
            return None
        return held

    def unlock_options(self, session: dict) -> dict:
        """The request options for one unlock (§5.2): a challenge bound to
        `unlock:<session id>`, "unlock", this session, its passkey and its
        generation now; single use, `CHALLENGE_S` on the monotonic clock, at
        most one per session (asking again replaces the earlier one).
        `allowCredentials` names the session's own passkey only."""
        webauthn, structs = _webauthn()
        with self._lock:
            self._require_usable()
            passkey = self._session_passkey(session)
            digest = session["token_sha256"]
            subject = "unlock:" + self._sessions[digest]["id"]
            self._challenges = {k: c for k, c in self._challenges.items()
                                if not (c["kind"] == "unlock" and c.get("session") == digest)}
            nonce = secrets.token_bytes(ANSWER_NONCE_BYTES)
            challenge = self.unlock_challenge(subject, nonce)
            self._issue("unlock", challenge, subject=subject, decision="unlock", nonce=nonce,
                        session=digest, passkey=passkey["key"],
                        generation=self._unlock_gen.get(digest, 0))
            options = webauthn.generate_authentication_options(
                rp_id=self.host, challenge=challenge, timeout=CHALLENGE_S * 1000,
                allow_credentials=[structs.PublicKeyCredentialDescriptor(id=unb64url(passkey["id"]))],
                user_verification=structs.UserVerificationRequirement.REQUIRED)
            return json.loads(webauthn.options_to_json(options))

    def unlock_verify(self, session: dict, credential) -> dict:
        """Open this session's window for one verified assertion (§5.2), all
        under `self._lock`: the challenge is spent first (unknown, used,
        expired, replaced or deleted by an ending: 409); it must have been
        issued for this session's subject, "unlock", this session and the
        generation it still has (else 409), and its passkey; the credential is
        the session's own passkey, unrevoked; `crossOrigin` is not true;
        origin, rpId, the UV flag, the signature and the counter rule as at
        sign-in (each 401, no detail). The counter is saved (a failed save is
        503 and the window stays closed); only then does the window open."""
        webauthn, _structs = _webauthn()
        with self._lock:
            self._require_usable()
            key = _client_challenge(credential)
            challenge, entry = self._spend(key, "unlock")
            digest = session.get("token_sha256") if isinstance(session, dict) else None
            row = self._sessions.get(digest) if isinstance(digest, str) else None
            if (row is None or entry.get("session") != digest or entry.get("decision") != "unlock"
                    or entry.get("subject") != "unlock:" + row["id"]
                    or not _UNLOCK_SUBJECT.fullmatch(str(entry.get("subject")))
                    or not hmac.compare_digest(challenge, self.unlock_challenge(entry["subject"],
                                                                                entry["nonce"]))):
                raise _refuse("the assertion was issued for another unlock")
            if entry.get("generation") != self._unlock_gen.get(digest, 0):
                raise AuthError(409, "challenge expired")
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
                raise _refuse(f"unlock assertion: {type(exc).__name__}") from None
            if REQUIRE_UV and not verified.user_verified:
                raise _refuse("unlock assertion without user verification")
            if not counter_ok(passkey["sign_count"], int(verified.new_sign_count)):
                raise _refuse("signature counter did not increase")
            now = self.wall()
            updated = dict(passkey, sign_count=int(verified.new_sign_count), last_used=now)
            rows = [updated if p is passkey else p for p in self._passkeys]
            # The counter is saved before the window opens: a write that
            # fails refuses it (503), so a counter can never go back.
            self._write_passkeys(rows, self._user_handle)
            self._passkeys = rows
            self._unlocks[digest] = {"passkey": passkey["key"], "until": self.mono() + UNLOCK_S}
            LOG.info("phone: passkey %s unlocked session %s for %d s", passkey["key"], row["id"], UNLOCK_S)
            return {"unlocked": True, "unlock_s": UNLOCK_S}

    def require_unlocked(self, session: dict) -> None:
        """Every gated action's check (§5.3), in place of a per-action
        assertion: the phone state usable (503), the session live and its
        passkey unrevoked (401), and its window open, opened by that passkey
        (else `Locked`, 423). It slides the window: `UNLOCK_S` from now."""
        with self._lock:
            self._require_usable()
            passkey = self._session_passkey(session)
            held = self._open_unlock(session["token_sha256"], passkey)
            if held is None:
                raise Locked()
            held["until"] = self.mono() + UNLOCK_S

    def touch_unlock(self, session: dict) -> None:
        """A session-only action (Deny, Withdraw, Start, Resume, a message, an
        answer, Leave): an **open** window slides; a closed one stays closed.
        Never raises."""
        with self._lock:
            if self._broken or not isinstance(session, dict):
                return
            digest = session.get("token_sha256")
            row = self._sessions.get(digest) if isinstance(digest, str) else None
            if row is None or self._expired(row, self.wall()):
                return
            passkey = self._by_key(row["passkey"])
            if passkey is None or passkey.get("revoked") is not None:
                return
            held = self._open_unlock(digest, passkey)
            if held is not None:
                held["until"] = self.mono() + UNLOCK_S

    def unlock_left(self, session: dict) -> int:
        """`me`'s `unlock_s`: whole seconds left in this session's window (0
        when closed). A read: it never slides the window."""
        with self._lock:
            digest = session.get("token_sha256") if isinstance(session, dict) else None
            held = self._unlocks.get(digest) if isinstance(digest, str) else None
            row = self._sessions.get(digest) if isinstance(digest, str) else None
            if held is None or row is None or held["passkey"] != row["passkey"]:
                return 0
            return max(0, int(held["until"] - self.mono()))

    def lock(self, session: dict) -> dict:
        """Lock now (`POST /m/api/lock`): this session's window ends and its
        pending unlock challenges are deleted; the session stays."""
        with self._lock:
            digest = session.get("token_sha256") if isinstance(session, dict) else None
            if isinstance(digest, str):
                self._end_unlock(digest, why="Lock now")
            LOG.info("phone: session %s locked from the phone", session.get("id"))
            return {"unlocked": False}

    # -- operation tokens (WS-5c §5.3a) ---------------------------------------------------

    def _op_token(self, sid: str, now: float) -> str:
        payload = json.dumps({"sid": sid, "n": b64url(secrets.token_bytes(OP_NONCE_BYTES)), "t": now},
                             sort_keys=True, separators=(",", ":")).encode("ascii")
        mac = hmac.new(self._op_key, OP_DOMAIN + payload, hashlib.sha256).digest()
        return "o1." + b64url(payload) + "." + b64url(mac)

    def issue_ops(self, session: dict) -> dict:
        """`GET /m/api/ops/tokens`: `OP_TOKENS` fresh tokens for this
        session, issued now by the server's clock. Session only, a read: it
        never slides the window, and stores nothing but the request's time
        (`OP_ISSUE_RATE` per `OP_ISSUE_S`, 429 past it)."""
        with self._lock:
            self._require_usable()
            self._session_passkey(session)
            digest = session["token_sha256"]
            now = self.mono()
            for key in [k for k, seen in self._issued.items() if not seen or now - seen[-1] >= OP_ISSUE_S]:
                del self._issued[key]
            seen = [at for at in self._issued.get(digest, []) if now - at < OP_ISSUE_S]
            if len(seen) >= OP_ISSUE_RATE:
                raise AuthError(429, OP_ISSUE_WORD)
            seen.append(now)
            self._issued[digest] = seen
            sid = self._sessions[digest]["id"]
            return {"tokens": [self._op_token(sid, now) for _ in range(OP_TOKENS)], "ttl_s": OP_TTL_S}

    def verify_op(self, session: dict, op: dict) -> None:
        """Checks 2 and 3 of §5.3a (outside any lock): the HMAC under this
        process's key (else 410: forged, or from before a restart), and the
        token's session is this one (else 400)."""
        mac = hmac.new(self._op_key, OP_DOMAIN + op["payload"], hashlib.sha256).digest()
        if not hmac.compare_digest(mac, op["mac"]):
            raise AuthError(410, FORGED_OP)
        if not isinstance(session, dict) or op["sid"] != session.get("id"):
            raise AuthError(400, "bad request")

    def admit_op(self, session: dict, method: str, path: str, body: dict, op: dict, rate) -> Op:
        """Steps 2 to 4 of §5.3, in order: `require_unlocked` (423, and
        nothing else runs), `verify_op` (400, 410), then `Ops.admit` (410,
        409, 429). The admitted `Op`."""
        self.require_unlocked(session)
        self.verify_op(session, op)
        digest = session["token_sha256"]
        reservation = self.ops.admit(digest, op["n"], op["t"], op_digest(method, path, body), rate)
        return Op(self.ops, (digest, op["n"]), reservation)

    def op_status(self, session: dict, n: str) -> dict:
        """`GET /m/api/ops/<n>`: this session's op `n` only (`Ops.status`)."""
        return self.ops.status(session.get("token_sha256") or "", n)

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

    def _ended(self, kind: str, value: str) -> None:
        for fn in list(self.on_end):
            try:
                fn(kind, value)
            except Exception as exc:     # noqa: BLE001 — never why a logout or revoke fails
                LOG.warning("phone: an end listener failed (%s)", type(exc).__name__)

    def active_passkeys(self) -> set[str]:
        """The keys of the paired, unrevoked passkeys (none while the file
        cannot be read)."""
        with self._lock:
            self._load()
            if self._broken:
                return set()
            return {p["key"] for p in self._active()}

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
        before = set(self._sessions)
        self._sessions = {h: r for h, r in self._sessions.items()
                          if r["passkey"] in active and not self._expired(r, now)}
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda h: self._sessions[h]["last_seen"])
            del self._sessions[oldest]
        gone = before - set(self._sessions)
        for digest in gone:
            # Expiry, a revoked passkey, or `MAX_SESSIONS` eviction (§5.8).
            self._end_unlock(digest, retire=True, why="the session was pruned")
        return bool(gone)

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
                self._end_unlock(digest, retire=True, why="the session expired or its passkey was revoked")
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
            passkey = self._by_key(row["passkey"]) if row is not None else None
            if (row is None or passkey is None or passkey.get("revoked") is not None
                    or self._expired(row, self.wall())):
                # Found ended: its window, challenges and ops go (§5.8).
                self._end_unlock(digest, retire=True, why="the session ended")
                return False
            return True

    def _try_save_sessions(self) -> None:
        try:
            self._save_sessions()
        except AuthError:
            pass        # memory is authoritative; the refusal already holds

    def logout(self, session: dict) -> None:
        with self._lock:
            self._sessions.pop(session["token_sha256"], None)
            self._end_unlock(session["token_sha256"], retire=True, why="signed out")
            self._try_save_sessions()
            LOG.info("phone: session %s signed out", session["id"])
            self._ended("session", session["id"])

    def end_session(self, session_id: str) -> bool:
        with self._lock:
            self._load()
            for digest, row in list(self._sessions.items()):
                if row["id"] == session_id:
                    del self._sessions[digest]
                    self._end_unlock(digest, retire=True, why="ended from the HUD")
                    self._try_save_sessions()
                    LOG.info("phone: session %s ended from the HUD", session_id)
                    self._ended("session", session_id)
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
            # In memory, before the save that may fail (Sol r4): the passkey
            # is marked revoked, its sessions go, and each one's window,
            # challenges and ops are retired.
            ended = [h for h, r in self._sessions.items() if r["passkey"] == key]
            self._sessions = {h: r for h, r in self._sessions.items() if r["passkey"] != key}
            rows = [dict(p, revoked=self.wall(), unsaved=True) if p is passkey else p
                    for p in self._passkeys]
            revoked = sorted((p for p in rows if p.get("revoked") is not None), key=lambda p: p["revoked"])
            for old in revoked[:-MAX_REVOKED]:
                rows.remove(old)
            self._passkeys = rows
            self._unsaved = True
            for digest in ended:
                self._end_unlock(digest, retire=True, why="the passkey was revoked")
            self._try_save_sessions()
            LOG.info("phone: passkey %s revoked", key)
            # Before the write that may fail (503): the revoke holds in
            # memory, and so does this.
            self._ended("passkey", key)
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

    def _unlocked_until(self, digest: str, row: dict) -> float | None:
        """WS-5c §5.8: when this session's window ends as it stands, on the
        wall clock (the desk shows "unlocked until HH:MM"), or None when it is
        closed. A read: it never slides the window."""
        held = self._unlocks.get(digest)
        if held is None or held["passkey"] != row["passkey"]:
            return None
        left = held["until"] - self.mono()
        return self.wall() + left if left > 0 else None

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
            for digest, row in sorted(self._sessions.items(), key=lambda kv: -kv[1]["last_seen"]):
                sessions_of.setdefault(row["passkey"], []).append(
                    {"id": row["id"], "ua": row["ua"], "created": row["created"],
                     "last_seen": row["last_seen"], "advisory": dict(row["advisory"]),
                     "unlocked_until": self._unlocked_until(digest, row)})
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
         404: "not found", 409: "conflict", 410: "gone", 413: "request body too large",
         423: LOCKED, 429: "too many requests", 503: "passkeys unavailable"}

PAIR_OPTIONS = "/m/api/pair/options"
PAIR_VERIFY = "/m/api/pair/verify"
LOGIN_OPTIONS = "/m/api/login/options"
LOGIN_VERIFY = "/m/api/login/verify"
LOGOUT = "/m/api/logout"
ME = "/m/api/me"
# WS-5c: the unlock window, Lock now, and the op tokens with their status.
UNLOCK_OPTIONS = "/m/api/unlock/options"
UNLOCK = "/m/api/unlock"
LOCK = "/m/api/lock"
OPS_TOKENS = "/m/api/ops/tokens"
OPS_PREFIX = "/m/api/ops/"
UNLOCK_ROUTES = {UNLOCK_OPTIONS: "POST", UNLOCK: "POST", LOCK: "POST", OPS_TOKENS: "GET"}
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
            # §4.1's last result, to show (P-4): never what authorizes; and
            # the seconds left in the unlock window (WS-5c), a read that never
            # slides it.
            return 200, {"signed_in": True, "paired": True,
                         "session": {"created": session["created"],
                                     "ends": session["created"] + ABSOLUTE_S},
                         "secure": secure_view(getattr(handler, "daemon", None)),
                         "unlock_s": auth.unlock_left(session)}, []
        if path in (LOGOUT, ME):
            return 404, {"error": WORDS[404]}, []
        unlocked = _unlock_route(handler, auth, path, session, body)
        if unlocked is not None:
            return unlocked
        # P-7's push to talk (`remote_stt`): a clip in, its text out, never sent.
        from . import remote_stt
        heard = remote_stt.route(handler, path, session, body)
        if heard is not None:
            return heard
        # Notifications (P-6, push.py): subscribe and unsubscribe this phone.
        from . import push as _push
        answered = _push.route(handler, path, session, body)
        if answered is not None:
            return answered
        # P-4's answers and a card's detail (`remote_answer`), P-5's sends,
        # new chats and task verbs (`remote_write`), then P-3's reads and the
        # event stream (`remote_api`), only past the session check above.
        # None from the reads: the stream answered on the socket.
        # The morning review (MR, `review.py`): its report, Approve selected
        # (a passkey assertion bound to the whole set) and Leave selected.
        from . import review as _review
        answered = _review.route(handler, path, session, body)
        if answered is not None:
            return answered
        # PM-D: the owner's Merge and Update on a PR row (`pr_merge.py`), each
        # with its own assertion bound to the `pr:` subject.
        from . import pr_merge
        acted = pr_merge.phone_route(handler, path, session, body)
        if acted is not None:
            return acted
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
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)


_OPS_PATH = re.compile(r"/m/api/ops/([A-Za-z0-9_-]{1,64})")


def _query_of(handler) -> str:
    from urllib.parse import urlsplit
    return urlsplit(getattr(handler, "path", "") or "").query


def _unlock_route(handler, auth: RemoteAuth, path: str, session: dict, body: bytes):
    """WS-5c's own routes past the session check, or None for any other path:

      POST /m/api/unlock/options {}            the unlock challenge (§5.2)
      POST /m/api/unlock         {assertion}   open the window
      POST /m/api/lock           {}            Lock now: end it
      GET  /m/api/ops/tokens                   fresh op tokens (§5.3a), a read
      GET  /m/api/ops/<n>                      one op's recorded state, a read

    A query string is 400; another method 405. Neither read slides the
    window, and each re-checks the session before it answers (401)."""
    want = UNLOCK_ROUTES.get(path)
    status_read = want is None and path.startswith(OPS_PREFIX)
    if want is None and not status_read:
        return None
    method = handler.command
    if method != (want or "GET"):
        return 405, {"error": "method not allowed"}, [("Allow", want or "GET")]
    if _query_of(handler):
        return 400, {"error": WORDS[400]}, []
    digest = session.get("token_sha256")
    if status_read:
        found = _OPS_PATH.fullmatch(path)
        if found is None or not _OP_NONCE.fullmatch(found.group(1)):
            return 400, {"error": WORDS[400]}, []
        payload = auth.op_status(session, found.group(1))
        if not auth.session_live(digest):
            return 401, {"error": UNAUTHORIZED}, []
        return 200, payload, []
    if path == OPS_TOKENS:
        payload = auth.issue_ops(session)
        if not auth.session_live(digest):
            return 401, {"error": UNAUTHORIZED}, []
        return 200, payload, []
    value = _json_body(body)
    if path == UNLOCK:
        credential = value.get("assertion")
        if set(value) != {"assertion"} or not isinstance(credential, dict):
            raise AuthError(400, "bad request")
        return 200, auth.unlock_verify(session, credential), []
    if value:
        raise AuthError(400, "bad request")
    if path == UNLOCK_OPTIONS:
        return 200, {"publicKey": auth.unlock_options(session)}, []
    return 200, auth.lock(session), []


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
    body = handler._body() if method == "POST" else {}
    # Notifications (P-6, push.py): the switch, the test and the list.
    from . import push as _push
    mounted = _push.hud_route(daemon, parts, method, body)
    if mounted is not None:
        return mounted
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
