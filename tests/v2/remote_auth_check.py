"""The phone's passkeys, pairing and sessions (phone surface design PH-3..PH-6,
§4, work package P-2): `jarvis/v2/remote_auth.py` and its routes on the
remote listener and the HUD.

Free and hermetic. Every daemon here listens on ephemeral loopback ports,
never 8402, 8403, 8405 or 8408; HOME, the data folder and the HUD build are
temporary. Real WebAuthn responses come from a software authenticator
(`Authenticator`: EC P-256, `none` attestation, the UV flag set or clear),
verified by the real `webauthn` library, so nothing here is a mock of the
check under test. Clocks are injected where expiry matters (relative to the
fake clock's own start, never to system uptime).

What it pins, the package's acceptance items:

  - pairing only within an open window opened at the desk; the code is
    256 bits, single-use, expires, and a wrong or absent one is refused;
  - a 4th passkey is refused while 3 are paired (window, options, verify);
  - UV clear is refused at registration and at login;
  - a wrong origin or rpId is refused at both;
  - a replayed, expired, unknown or wrong-kind challenge is refused (409);
  - PH-4's counter rule (0 then 0 ok; 5 then 5 refused; 5 then 6 ok);
  - the session cookie's attributes, exactly;
  - idle (12 h) and absolute (7 days) expiry;
  - revoke ends a passkey's sessions at once; End ends one; logout;
  - `projects.is_owner` stays false on the remote listener, session or not;
  - 400 bad Host, 403 bad Origin, 401 no session, with no detail;
  - the 64 KiB body cap (413) and that an unread body is drained;
  - 0600 files in a 0700 folder; only token hashes at rest;
  - the advisory Tailscale headers grant nothing;
  - `/status`'s `remote` fields; the files are protected state.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_auth_check.py
"""
from __future__ import annotations

import base64
from email.message import Message
import hashlib
import http.client
import http.cookies
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cbor2  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from jarvis import config  # noqa: E402
from jarvis.v2 import projects, remote_auth, remote_listener  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, SessionHandle, Usage  # noqa: E402
from jarvis.v2.remote_auth import RemoteAuth  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

HOST = "jarvis-box.tail-test.ts.net"
ORIGIN = "https://" + HOST
LIVE_PORTS = {8402, 8403, 8405, 8408}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """A platform authenticator in software: what an iPhone's passkey sends,
    in the serialization the P-0 spike's page produces (base64url fields,
    `clientExtensionResults`, `authenticatorAttachment`)."""

    def __init__(self, *, uv: bool = True, step: int = 0, count: int = 0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.uv = uv
        self.step = step          # how much the counter rises per assertion (iCloud: 0)
        self.count = count
        self.user_handle: str | None = None

    @property
    def id(self) -> str:
        return b64(self.cred_id)

    def _cose(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})

    def _flags(self, attested: bool, uv: bool) -> int:
        flags = 0x01 | 0x08 | 0x10           # UP, backup eligible, backed up
        if uv:
            flags |= 0x04
        if attested:
            flags |= 0x40
        return flags

    def create(self, options: dict, *, origin: str = ORIGIN, rp_id: str | None = None,
               uv: bool | None = None, challenge: str | None = None, client_extra: dict | None = None) -> dict:
        rp = rp_id or options["rp"]["id"]
        self.user_handle = options["user"]["id"]
        client = json.dumps({"type": "webauthn.create", "challenge": challenge or options["challenge"],
                             "origin": origin, "crossOrigin": False, **(client_extra or {})}).encode()
        auth_data = (hashlib.sha256(rp.encode()).digest()
                     + bytes([self._flags(True, self.uv if uv is None else uv)])
                     + self.count.to_bytes(4, "big") + bytes(16)
                     + len(self.cred_id).to_bytes(2, "big") + self.cred_id + self._cose())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": self.id, "rawId": self.id, "type": "public-key",
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation),
                             "transports": ["internal", "hybrid"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def get(self, options: dict, *, origin: str = ORIGIN, rp_id: str | None = None,
            uv: bool | None = None, count: int | None = None, challenge: str | None = None,
            client_extra: dict | None = None) -> dict:
        rp = rp_id or options["rpId"]
        if count is None:
            self.count += self.step
            count = self.count
        client = json.dumps({"type": "webauthn.get", "challenge": challenge or options["challenge"],
                             "origin": origin, "crossOrigin": False, **(client_extra or {})}).encode()
        auth_data = (hashlib.sha256(rp.encode()).digest()
                     + bytes([self._flags(False, self.uv if uv is None else uv)])
                     + count.to_bytes(4, "big"))
        signature = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        response = {"clientDataJSON": b64(client), "authenticatorData": b64(auth_data),
                    "signature": b64(signature)}
        if self.user_handle:
            response["userHandle"] = self.user_handle
        return {"id": self.id, "rawId": self.id, "type": "public-key", "response": response,
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}


class Fake:
    def __init__(self, name):
        self.name = name

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        return iter(())

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class Clock:
    """A settable clock, starting at an arbitrary value (expiry is relative)."""

    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


def raw(port, method, path, headers=(), body: bytes | None = None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        data = response.read() if method != "HEAD" else b""
        return response.status, response.headers, data
    finally:
        conn.close()


def send_bytes(port, data: bytes) -> bytes:
    """Send `data` as is and read to the end; a reset raises."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        out = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return out
            out += chunk


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_REMOTE_HOST"}
        env.update({"HOME": str(home), "XDG_DATA_HOME": str(self.root / "xdg"),
                    "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        guard = patch.dict(os.environ, env, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "mobile.html").write_text("<!doctype html><title>J.A.R.V.I.S.</title>PHONE")
        (dist / "index.html").write_text("<!doctype html><title>J.A.R.V.I.S.</title>HUD-PAGE")
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json",
                                DISCORD_GUILD_PATH=self.root / "guild.json",
                                HUD_DEV=True, HUD_DIST=dist).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)


class Remote(Base):
    """A real daemon with the remote listener bound, its auth on fake clocks."""

    def setUp(self):
        super().setUp()
        self.d = Daemon(Stores(), {name: Fake(name) for name in PN},
                        lambda *_: lambda *_: Decision.DENY, 0, remote_host=HOST)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.port = self.d.remote_port
        self.assertIsInstance(self.port, int)
        self.assertFalse({self.d.port, self.d.face_port, self.d.workshop_port, self.port} & LIVE_PORTS)
        self.auth = self.d.remote_auth
        self.wall = Clock(1_900_000_000.0)
        self.mono = Clock(5_000.0)
        self.auth.wall = self.wall
        self.auth.mono = self.mono

    # -- the phone ---------------------------------------------------------------

    def phone(self, method, path, value=None, *, cookie=None, host=HOST, origin=ORIGIN,
              extra=(), body: bytes | None = None):
        headers = [("Host", host)] if host is not None else []
        if origin is not None and method not in ("GET", "HEAD"):
            headers.append(("Origin", origin))
        if cookie is not None:
            headers.append(("Cookie", cookie))
        headers += list(extra)
        if value is not None:
            body = json.dumps(value).encode()
            headers.append(("Content-Type", "application/json"))
        status, response_headers, data = raw(self.port, method, path, headers, body)
        payload = json.loads(data) if data else None
        return status, response_headers, payload

    # -- the desk ----------------------------------------------------------------

    def hud(self, method, path, value=None, *, port=None, origin=True):
        port = port or self.d.face_port
        host = f"127.0.0.1:{port}"
        headers = [("Host", host)]
        if origin:
            headers.append(("Origin", f"http://{host}"))
        body = None
        if method == "POST":
            body = json.dumps(value or {}).encode()
            headers.append(("Content-Type", "application/json"))
        status, _, data = raw(port, method, path, headers, body)
        return status, (json.loads(data) if data else None)

    def open_window(self) -> str:
        status, body = self.hud("POST", "/phone/pair")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["url"].startswith(f"https://{HOST}/m/pair#"), body)
        return body["url"].split("#", 1)[1]

    def pair(self, authenticator: Authenticator | None = None, code: str | None = None) -> Authenticator:
        a = authenticator or Authenticator()
        code = code or self.open_window()
        status, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(status, 200, options)
        status, _, body = self.phone("POST", "/m/api/pair/verify",
                                     {"code": code, "credential": a.create(options)})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["paired"])
        return a

    def login(self, a: Authenticator, *, extra=(), **kw):
        status, _, options = self.phone("POST", "/m/api/login/options", {})
        self.assertEqual(status, 200, options)
        return self.phone("POST", "/m/api/login/verify", {"credential": a.get(options, **kw)}, extra=extra)

    def cookie_of(self, headers) -> str:
        value = headers["Set-Cookie"]
        self.assertIsNotNone(value)
        return value.split(";", 1)[0]

    def signed_in(self, a: Authenticator, **kw) -> str:
        status, headers, body = self.login(a, **kw)
        self.assertEqual(status, 200, body)
        return self.cookie_of(headers)

    def me(self, cookie):
        return self.phone("GET", "/m/api/me", cookie=cookie)[0]


# -- pairing (PH-6) --------------------------------------------------------------------


class Pairing(Remote):
    def test_only_within_an_open_window(self):
        a = Authenticator()
        status, _, body = self.phone("POST", "/m/api/pair/options", {"code": "x" * 43})
        self.assertEqual((status, body), (401, {"error": "unauthorized"}), "no window is open")
        code = self.open_window()
        self.assertEqual(len(unb64(code)), 32, "the code is 256 bits")
        self.assertGreaterEqual(remote_auth.CODE_BYTES * 8, 128)
        for bad in (None, "", code[:-1], code + "A", code.upper() if code.upper() != code else code.lower(),
                    " " + code, 7, ["x"], "x" * 500):
            value = {} if bad is None else {"code": bad}
            status, _, body = self.phone("POST", "/m/api/pair/options", value)
            self.assertEqual((status, body), (401, {"error": "unauthorized"}), repr(bad))
        status, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(status, 200)
        self.assertEqual(options["rp"]["id"], HOST)
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "required")
        # The right challenge but a wrong code at verify: refused, nothing stored.
        status, _, _ = self.phone("POST", "/m/api/pair/verify",
                                  {"code": code[:-1] + ("A" if code[-1] != "A" else "B"),
                                   "credential": a.create(options)})
        self.assertEqual(status, 401)
        self.assertEqual(self.auth.status()["passkeys"], [])
        self.pair(a, code)
        self.assertEqual(len(self.auth.status()["passkeys"]), 1)
        print("ok  pairing: only within a window opened at the desk; a wrong or absent code is 401")

    def test_code_is_single_use_and_the_window_closes(self):
        code = self.open_window()
        self.pair(Authenticator(), code)
        self.assertFalse(self.auth.status()["pairing"]["open"])
        status, _, _ = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(status, 401, "the window closed on the one registration")
        # Two phones in one window: only the first registration lands.
        code = self.open_window()
        a, b = Authenticator(), Authenticator()
        _, _, oa = self.phone("POST", "/m/api/pair/options", {"code": code})
        _, _, ob = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(self.phone("POST", "/m/api/pair/verify", {"code": code, "credential": a.create(oa)})[0], 200)
        self.assertEqual(self.phone("POST", "/m/api/pair/verify", {"code": code, "credential": b.create(ob)})[0], 401)
        self.assertEqual(len(self.auth.status()["passkeys"]), 2)
        print("ok  pairing: the code is single-use; one registration closes the window")

    def test_window_expires_is_replaced_and_cancelled(self):
        code = self.open_window()
        status, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(status, 200)
        self.mono.now += remote_auth.WINDOW_S - 1
        self.assertTrue(self.auth.status()["pairing"]["open"])
        self.mono.now += 1
        self.assertFalse(self.auth.status()["pairing"]["open"])
        status, _, _ = self.phone("POST", "/m/api/pair/verify",
                                  {"code": code, "credential": Authenticator().create(options)})
        self.assertEqual(status, 401, "an expired window")
        # A new window replaces the old: the old code and its challenges die.
        old = self.open_window()
        _, _, options = self.phone("POST", "/m/api/pair/options", {"code": old})
        new = self.open_window()
        self.assertNotEqual(old, new)
        self.assertEqual(self.phone("POST", "/m/api/pair/options", {"code": old})[0], 401)
        status, _, _ = self.phone("POST", "/m/api/pair/verify",
                                  {"code": new, "credential": Authenticator().create(options)})
        self.assertEqual(status, 409, "a challenge of the replaced window")
        # Cancel.
        self.assertEqual(self.hud("POST", "/phone/pair/cancel")[0], 200)
        self.assertEqual(self.phone("POST", "/m/api/pair/options", {"code": new})[0], 401)
        self.assertEqual(self.auth.status()["passkeys"], [])
        print("ok  pairing: the window expires at 5 minutes, a new one replaces it, Cancel closes it")

    def test_window_opens_only_from_the_hud(self):
        status, _ = self.hud("POST", "/phone/pair", port=self.d.port)
        self.assertEqual(status, 403, "the API listener every tool's client uses")
        status, _ = self.hud("POST", "/phone/pair", origin=False)
        self.assertEqual(status, 403, "no Origin")
        for path in ("/phone", "/phone/pair", "/phone/pair/cancel", "/phone/passkeys/k-000000000000/revoke"):
            status, _ = self.hud("POST" if path != "/phone" else "GET", path, port=self.d.port)
            self.assertEqual(status, 403, path)
        # The remote listener has no /phone route at all.
        self.assertEqual(self.phone("POST", "/phone/pair", {})[0], 404)
        self.assertEqual(self.phone("POST", "/m/phone/pair", {})[0], 404)
        self.assertFalse(self.auth.status()["pairing"]["open"])
        # The HUD's own reads never carry the code.
        code = self.open_window()
        status, view = self.hud("GET", "/phone")
        self.assertEqual(status, 200)
        self.assertNotIn(code, json.dumps(view))
        self.assertTrue(view["pairing"]["open"])
        self.assertEqual(view["pairing"]["expires_in"], remote_auth.WINDOW_S)
        print("ok  pairing: only the owner's HUD opens a window; its reads never show the code")

    def test_window_needs_a_host_and_a_bound_listener(self):
        d = Daemon(Stores(self.root / "other"), {name: Fake(name) for name in PN},
                   lambda *_: lambda *_: Decision.DENY, 0, remote_host=None)
        d.start()
        self.addCleanup(d.stop)
        host = f"127.0.0.1:{d.face_port}"
        status, _, body = raw(d.face_port, "POST", "/phone/pair",
                              [("Host", host), ("Origin", f"http://{host}")], b"{}")
        self.assertEqual(status, 409, body)
        print("ok  pairing: no window without a configured tailnet name")

    def test_fourth_passkey_refused(self):
        for _ in range(3):
            self.pair()
        status, body = self.hud("POST", "/phone/pair")
        self.assertEqual(status, 409, body)
        self.assertIn("three passkeys", body["error"])
        # Raced past the window: refused at options and at verify too.
        auth = RemoteAuth(self.root / "unit", HOST, wall=self.wall, mono=self.mono)
        auth.server = object()
        authenticators = []
        for _ in range(2):
            code = auth.open_window()["url"].split("#")[1]
            a = Authenticator()
            auth.pair_verify(code, a.create(auth.pair_options(code)))
            authenticators.append(a)
        code = auth.open_window()["url"].split("#")[1]
        options = auth.pair_options(code)
        extra = dict(auth._passkeys[0], key="k-" + "a" * 12, id=b64(os.urandom(16)))
        auth._passkeys.append(extra)
        with self.assertRaises(remote_auth.AuthError) as caught:
            auth.pair_verify(code, Authenticator().create(options))
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(remote_auth.AuthError) as caught:
            auth.pair_options(code)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(remote_auth.MAX_PASSKEYS, 3)
        print("ok  a 4th passkey is refused while 3 are paired: at the window, options and verify")

    def test_uv_clear_refused_at_registration(self):
        code = self.open_window()
        _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        status, headers, body = self.phone("POST", "/m/api/pair/verify",
                                           {"code": code, "credential": Authenticator(uv=False).create(options)})
        self.assertEqual((status, body), (401, {"error": "unauthorized"}))
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.auth.status()["passkeys"], [])
        print("ok  registration without user verification (UV clear) is refused")

    def test_wrong_origin_or_rp_refused_at_registration(self):
        for kw in ({"origin": "https://evil.ts.net"}, {"origin": "http://" + HOST},
                   {"origin": ORIGIN + ":443"}, {"rp_id": "evil.ts.net"}, {"rp_id": "tail-test.ts.net"}):
            code = self.open_window()
            _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
            status, _, body = self.phone("POST", "/m/api/pair/verify",
                                         {"code": code, "credential": Authenticator().create(options, **kw)})
            self.assertEqual((status, body), (401, {"error": "unauthorized"}), kw)
        self.assertEqual(self.auth.status()["passkeys"], [])
        print("ok  registration for another origin or rpId is refused")

    def test_uv_each_layer_on_its_own(self):
        """Both halves of the UV guard, each pinned alone: the library is
        always asked to require UV, and a result that says UV was not set is
        refused even if the library let it through."""
        import webauthn
        real_reg, real_auth = webauthn.verify_registration_response, webauthn.verify_authentication_response
        seen = []

        def spy(real):
            def call(**kw):
                seen.append(kw.get("require_user_verification"))
                return real(**kw)
            return call

        def lenient(real):
            def call(**kw):
                result = real(**dict(kw, require_user_verification=False))
                return type(result)(**{**result.__dict__, "user_verified": False})
            return call

        with patch.object(webauthn, "verify_registration_response", spy(real_reg)), \
                patch.object(webauthn, "verify_authentication_response", spy(real_auth)):
            a = self.pair()
            self.signed_in(a)
        self.assertEqual(seen, [True, True], "the library is told UV is required, both times")
        code = self.open_window()
        _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        with patch.object(webauthn, "verify_registration_response", lenient(real_reg)):
            status, _, _ = self.phone("POST", "/m/api/pair/verify",
                                      {"code": code, "credential": Authenticator().create(options)})
        self.assertEqual(status, 401, "the explicit check refuses a registration without UV")
        with patch.object(webauthn, "verify_authentication_response", lenient(real_auth)):
            status, headers, _ = self.login(a)
        self.assertEqual(status, 401, "the explicit check refuses an assertion without UV")
        self.assertNotIn("Set-Cookie", headers)
        print("ok  UV: the library is told to require it, and its answer is checked again")

    def test_registration_never_signs_in(self):
        code = self.open_window()
        _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        status, headers, _ = self.phone("POST", "/m/api/pair/verify",
                                        {"code": code, "credential": Authenticator().create(options)})
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.auth._sessions, {})
        print("ok  a registration makes no session: only an assertion does")


# -- login (PH-4's login half) ---------------------------------------------------------


class Login(Remote):
    def test_login_makes_a_session(self):
        a = self.pair()
        cookie = self.signed_in(a)
        self.assertEqual(self.me(cookie), 200)
        status, _, body = self.phone("GET", "/m/api/me", cookie=cookie)
        self.assertEqual(body["signed_in"], True)
        print("ok  login: a verified assertion makes a session")

    def test_login_options_name_no_credential(self):
        self.pair()
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        self.assertEqual(options.get("allowCredentials", []), [])
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual(options["rpId"], HOST)
        self.assertEqual(len(unb64(options["challenge"])), 32)
        print("ok  login options: UV required, no credential ids handed out")

    def test_uv_clear_refused_at_login(self):
        a = self.pair()
        status, headers, body = self.login(a, uv=False)
        self.assertEqual((status, body), (401, {"error": "unauthorized"}))
        self.assertNotIn("Set-Cookie", headers)
        print("ok  login without user verification (UV clear) is refused")

    def test_wrong_origin_or_rp_refused_at_login(self):
        a = self.pair()
        for kw in ({"origin": "https://evil.ts.net"}, {"origin": "http://" + HOST},
                   {"rp_id": "evil.ts.net"}, {"rp_id": "localhost"}):
            status, headers, body = self.login(a, **kw)
            self.assertEqual((status, body), (401, {"error": "unauthorized"}), kw)
            self.assertNotIn("Set-Cookie", headers)
        print("ok  login: an assertion for another origin or rpId is refused")

    def test_replayed_expired_unknown_challenge_refused(self):
        a = self.pair()
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        assertion = a.get(options)
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": assertion})[0], 200)
        status, headers, body = self.phone("POST", "/m/api/login/verify", {"credential": assertion})
        self.assertEqual((status, body), (409, {"error": "challenge expired"}), "a replay")
        self.assertNotIn("Set-Cookie", headers)
        # Expired: 60 s on the monotonic clock.
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        self.mono.now += remote_auth.CHALLENGE_S
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": a.get(options)})[0], 409)
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        self.mono.now += remote_auth.CHALLENGE_S - 1
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": a.get(options)})[0], 200)
        # Unknown: one the server never issued.
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        forged = a.get(options, challenge=b64(os.urandom(32)))
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": forged})[0], 409)
        # A failed attempt spends its challenge too.
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": a.get(options, uv=False)})[0], 401)
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": a.get(options)})[0], 409)
        # A registration challenge cannot sign in.
        code = self.open_window()
        _, _, reg = self.phone("POST", "/m/api/pair/options", {"code": code})
        wrong_kind = a.get({"rpId": HOST, "challenge": reg["challenge"]})
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": wrong_kind})[0], 409)
        print("ok  login: a replayed, expired, unknown, spent or registration challenge is refused (409)")

    def test_challenges_are_pruned_and_capped_per_kind(self):
        a = self.pair()
        # A pending sign-in and a pending registration, then a flood of
        # unauthenticated login/options: the flood is refused, nothing
        # pending is evicted.
        _, _, pending_login = self.phone("POST", "/m/api/login/options", {})
        code = self.open_window()
        newcomer = Authenticator()
        _, _, pending_reg = self.phone("POST", "/m/api/pair/options", {"code": code})
        statuses = [self.phone("POST", "/m/api/login/options", {})[0]
                    for _ in range(remote_auth.MAX_LOGIN_CHALLENGES + 10)]
        self.assertEqual(statuses.count(200), remote_auth.MAX_LOGIN_CHALLENGES - 1)
        self.assertTrue(all(s == 429 for s in statuses[remote_auth.MAX_LOGIN_CHALLENGES - 1:]), statuses)
        status, _, body = self.phone("POST", "/m/api/login/options", {})
        self.assertEqual((status, body), (429, {"error": "too many sign-ins in progress; try again in a minute"}))
        self.assertEqual(self.phone("POST", "/m/api/pair/verify",
                                    {"code": code, "credential": newcomer.create(pending_reg)})[0], 200,
                         "the pending registration survived the flood")
        self.assertEqual(self.phone("POST", "/m/api/login/verify",
                                    {"credential": a.get(pending_login)})[0], 200,
                         "the pending sign-in survived the flood")
        # Registration challenges: only within a window, capped on their own.
        auth = RemoteAuth(self.root / "unit", HOST, wall=self.wall, mono=self.mono)
        with self.assertRaises(remote_auth.AuthError) as caught:
            auth._issue("register", generation=0)
        self.assertEqual(caught.exception.status, 401)
        code = auth.open_window()["url"].split("#")[1]
        for _ in range(remote_auth.MAX_REGISTER_CHALLENGES):
            auth.pair_options(code)
        with self.assertRaises(remote_auth.AuthError) as caught:
            auth.pair_options(code)
        self.assertEqual(caught.exception.status, 429)
        auth.login_options()                       # the login pool is its own
        # Expiry prunes.
        self.mono.now += remote_auth.CHALLENGE_S + 1
        self.assertEqual(self.phone("POST", "/m/api/login/options", {})[0], 200)
        self.assertEqual(sum(1 for c in self.auth._challenges.values() if c["kind"] == "login"), 1,
                         "expired ones are pruned")
        self.assertEqual((remote_auth.MAX_LOGIN_CHALLENGES, remote_auth.MAX_REGISTER_CHALLENGES), (32, 8))
        print("ok  challenges: capped per kind, a flood refused (429) and nothing pending evicted; pruned")

    def test_cross_origin_ceremonies_refused(self):
        a = self.pair()
        for extra in ({"crossOrigin": True}, {"topOrigin": "https://evil.example"}, {"crossOrigin": "yes"}):
            status, headers, _ = self.login(a, client_extra=extra)
            self.assertEqual(status, 401, extra)
            self.assertNotIn("Set-Cookie", headers)
            code = self.open_window()
            _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
            status, _, _ = self.phone("POST", "/m/api/pair/verify",
                                      {"code": code, "credential": Authenticator().create(options, client_extra=extra)})
            self.assertEqual(status, 401, extra)
        self.assertEqual(len(self.auth.status()["passkeys"]), 1)
        self.assertEqual(self.login(a, client_extra={"crossOrigin": False})[0], 200)
        print("ok  crossOrigin true (or a topOrigin) is refused at registration and at login")

    def test_counter_rule(self):
        self.assertTrue(remote_auth.counter_ok(0, 0))
        self.assertTrue(remote_auth.counter_ok(0, 1))
        self.assertTrue(remote_auth.counter_ok(5, 6))
        self.assertFalse(remote_auth.counter_ok(5, 5))
        self.assertFalse(remote_auth.counter_ok(5, 0))
        self.assertFalse(remote_auth.counter_ok(5, 4))
        # iCloud: always 0, accepted again and again.
        icloud = self.pair(Authenticator(step=0))
        for _ in range(3):
            self.signed_in(icloud)
        # A counting authenticator: 5 then 5 refused, then 6 accepted, then 0 refused.
        counting = self.pair(Authenticator(count=5))
        self.assertEqual(self.login(counting, count=5)[0], 401, "5 then 5")
        self.assertEqual(self.login(counting, count=6)[0], 200, "5 then 6")
        self.assertEqual(self.login(counting, count=0)[0], 401, "6 then 0")
        self.assertEqual(self.login(counting, count=6)[0], 401, "6 then 6")
        self.assertEqual(self.login(counting, count=9)[0], 200)
        print("ok  counter (PH-4): 0 then 0 ok; 5 then 5 refused; 5 then 6 ok; never back to 0")

    def test_unknown_and_revoked_credentials_refused(self):
        self.pair()
        stranger = Authenticator()
        stranger.user_handle = self.auth._user_handle
        self.assertEqual(self.login(stranger)[0], 401)
        a = self.pair()
        key = self.auth.status()["passkeys"][-1]["key"]
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{key}/revoke")[0], 200)
        self.assertEqual(self.login(a)[0], 401, "revoked")
        # Re-pairing the revoked credential itself is refused too.
        code = self.open_window()
        _, _, options = self.phone("POST", "/m/api/pair/options", {"code": code})
        self.assertEqual(self.phone("POST", "/m/api/pair/verify",
                                    {"code": code, "credential": a.create(options)})[0], 401)
        print("ok  login: unknown and revoked credentials are refused")

    def test_signature_and_user_handle_checked(self):
        a = self.pair()
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        assertion = a.get(options)
        sig = bytearray(unb64(assertion["response"]["signature"]))
        sig[-1] ^= 1
        assertion["response"]["signature"] = b64(bytes(sig))
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": assertion})[0], 401)
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        assertion = a.get(options)
        assertion["response"]["userHandle"] = b64(b"someone-else")
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": assertion})[0], 401)
        _, _, options = self.phone("POST", "/m/api/login/options", {})
        assertion = a.get(options)
        assertion["id"] = b64(os.urandom(16))
        self.assertEqual(self.phone("POST", "/m/api/login/verify", {"credential": assertion})[0], 401)
        for bad in ({}, {"credential": "x"}, {"credential": {"response": {}}}, [1]):
            status, _, _ = self.phone("POST", "/m/api/login/verify", bad)
            self.assertEqual(status, 400, bad)
        print("ok  login: a bad signature, another user's handle or mismatched ids are refused")


# -- sessions (PH-5) -------------------------------------------------------------------


class Sessions(Remote):
    def test_cookie_attributes_exact(self):
        a = self.pair()
        status, headers, _ = self.login(a)
        self.assertEqual(status, 200)
        values = headers.get_all("Set-Cookie")
        self.assertEqual(len(values), 1)
        name, _, rest = values[0].partition("=")
        token, _, attributes = rest.partition("; ")
        self.assertEqual(name, "__Host-jarvis_m")
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(unb64(token)), 32)
        self.assertEqual(attributes, "Path=/; Max-Age=604800; Secure; HttpOnly; SameSite=Strict")
        parsed = http.cookies.SimpleCookie()
        parsed.load(values[0])
        morsel = parsed["__Host-jarvis_m"]
        self.assertEqual((morsel["path"], morsel["secure"], morsel["httponly"], morsel["samesite"],
                          morsel["domain"], morsel["max-age"]), ("/", True, True, "Strict", "", "604800"))
        print("ok  the session cookie: __Host-jarvis_m, Path=/, Secure, HttpOnly, SameSite=Strict, no Domain")

    def test_idle_and_absolute_expiry(self):
        a = self.pair()
        cookie = self.signed_in(a)
        self.wall.now += remote_auth.IDLE_S - 1
        self.assertEqual(self.me(cookie), 200, "just inside 12 h idle")
        self.wall.now += remote_auth.IDLE_S
        self.assertEqual(self.me(cookie), 401, "12 h idle")
        self.assertEqual(self.auth.status()["passkeys"][0]["sessions"], [])
        # Absolute: kept busy, still over at 7 days.
        cookie = self.signed_in(a)
        start = self.wall.now
        while self.wall.now + remote_auth.IDLE_S - 60 < start + remote_auth.ABSOLUTE_S:
            self.wall.now += remote_auth.IDLE_S - 60
            self.assertEqual(self.me(cookie), 200, self.wall.now - start)
        self.wall.now = start + remote_auth.ABSOLUTE_S - 1
        self.assertEqual(self.me(cookie), 200)
        self.wall.now = start + remote_auth.ABSOLUTE_S
        self.assertEqual(self.me(cookie), 401, "7 days absolute")
        self.assertEqual((remote_auth.IDLE_S, remote_auth.ABSOLUTE_S), (12 * 3600, 7 * 86400))
        print("ok  sessions: 12 h idle, 7 days absolute")

    def test_revoke_ends_sessions_at_once(self):
        a, b = self.pair(), self.pair()
        a1, a2, b1 = self.signed_in(a), self.signed_in(a), self.signed_in(b)
        keys = [p["key"] for p in self.auth.status()["passkeys"]]
        self.assertEqual(len(self.auth.status()["passkeys"][0]["sessions"]), 2)
        status, body = self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")
        self.assertEqual(status, 200, body)
        # Gone from memory at once, before any request could prune them.
        self.assertEqual({r["passkey"] for r in self.auth._sessions.values()}, {keys[1]})
        self.assertEqual((self.me(a1), self.me(a2), self.me(b1)), (401, 401, 200))
        self.assertEqual(self.login(a)[0], 401)
        # And on disk, with no help: a restart sees it.
        again = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertEqual([p["key"] for p in again.status()["passkeys"]], [keys[1]])
        self.assertIsNone(again.session_for([a1]))
        on_disk = json.loads((self.auth.dir / "sessions.json").read_text())["sessions"]
        self.assertEqual({r["passkey"] for r in on_disk}, {keys[1]})
        self.assertNotIn("unsaved", (self.auth.dir / "passkeys.json").read_text())
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")[0], 200, "idempotent")
        self.assertEqual(self.hud("POST", "/phone/passkeys/k-ffffffffffff/revoke")[0], 404)
        self.assertEqual(self.hud("POST", "/phone/passkeys/nonsense/revoke")[0], 404)
        print("ok  revoke: every session of the passkey ends at once, in memory and on disk")

    def test_revoke_whose_write_fails(self):
        a, b = self.pair(), self.pair()
        a1, b1 = self.signed_in(a), self.signed_in(b)
        keys = [p["key"] for p in self.auth.status()["passkeys"]]
        failing = patch.object(self.auth, "_write", side_effect=remote_auth.AuthError(503, "phone state unavailable"))
        failing.start()
        status, body = self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")
        self.assertEqual(status, 503, body)
        # Held in memory: its session is gone, its credential refused.
        self.assertEqual((self.me(a1), self.me(b1)), (401, 200))
        self.assertEqual(self.login(a)[0], 401, "an unsaved revoked credential is refused")
        # The HUD says so, and a repeat revoke retries (503 while it still fails).
        status, view = self.hud("GET", "/phone")
        self.assertEqual(view["state"], "unsaved")
        self.assertEqual([u["key"] for u in view["unsaved"]], [keys[0]])
        self.assertEqual([p["key"] for p in view["passkeys"]], [keys[1]])
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")[0], 503)
        # A restart now would lose it: the file still has the passkey active.
        lost = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertEqual(len(lost.status()["passkeys"]), 2, "not yet on disk")
        # The disk recovers: the repeat revoke lands it and answers 200.
        failing.stop()
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")[0], 200)
        self.assertEqual(self.hud("GET", "/phone")[1]["state"], "ok")
        self.assertEqual(self.hud("POST", f"/phone/passkeys/{keys[0]}/revoke")[0], 200,
                         "Save now on an already-saved revocation is 200")
        restarted = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertEqual([p["key"] for p in restarted.status()["passkeys"]], [keys[1]])
        self.assertIsNone(restarted.session_for([a1]), "and its session stays dead after a restart")
        self.assertIsNotNone(restarted.session_for([b1]))
        print("ok  revoke with a failing write: 503, refused in memory, 'unsaved' in the HUD, retried, durable once it lands")

    def test_daemon_stop_saves_or_names_an_unsaved_revocation(self):
        a, b = self.pair(), self.pair()
        key = self.auth.status()["passkeys"][0]["key"]
        with patch.object(self.auth, "_write", side_effect=remote_auth.AuthError(503, "x")):
            self.assertEqual(self.hud("POST", f"/phone/passkeys/{key}/revoke")[0], 503)
            # Still failing at stop: an ERROR line names the passkey.
            with self.assertLogs("jarvis.v2.remote_auth", "ERROR") as logs:
                self.assertEqual(self.auth.final_save(), [key])
            self.assertIn(key, "\n".join(logs.output))
        # The disk is back by the time the daemon stops: the last try lands it.
        self.d.stop()
        restarted = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertNotIn(key, [p["key"] for p in restarted.status()["passkeys"]])
        self.assertEqual(self.auth.final_save(), [])
        print("ok  Daemon.stop makes one last save of an unsaved revocation, and names it in an ERROR if it fails")

    def test_unsaved_revoke_lands_on_any_later_call(self):
        for call in ("status", "session", "login"):
            with self.subTest(call=call):
                a, b = self.pair(), self.pair()
                b1 = self.signed_in(b)
                key = self.auth.status()["passkeys"][-2]["key"]
                with patch.object(self.auth, "_write", side_effect=remote_auth.AuthError(503, "x")):
                    self.assertEqual(self.hud("POST", f"/phone/passkeys/{key}/revoke")[0], 503)
                self.assertTrue(self.auth._unsaved)
                if call == "status":
                    self.auth.status()
                elif call == "session":
                    self.assertEqual(self.me(b1), 200)
                else:
                    self.signed_in(b)
                self.assertFalse(self.auth._unsaved, call)
                restarted = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
                self.assertNotIn(key, [p["key"] for p in restarted.status()["passkeys"]], call)
                for p in list(self.auth.status()["passkeys"]):
                    self.auth.revoke(p["key"])
        print("ok  an unsaved revocation is written by the next status, session check or sign-in")

    def test_session_clock_skew_and_prune_order(self):
        a = self.pair()
        cookie = self.signed_in(a)
        start = self.wall.now
        self.wall.now = start - 299
        self.assertEqual(self.me(cookie), 200, "a clock 299 s behind the session's creation")
        self.wall.now = start - 301
        self.assertEqual(self.me(cookie), 401, "a session from more than 5 minutes in the future")
        self.wall.now = start
        # MAX_SESSIONS: the least recently seen goes first.
        cookies = []
        for _ in range(remote_auth.MAX_SESSIONS):
            self.wall.now += 1
            cookies.append(self.signed_in(a))
        self.wall.now += 1
        self.assertEqual(self.me(cookies[0]), 200)        # now the most recently seen
        self.wall.now += 1
        newest = self.signed_in(a)
        self.assertEqual(len(self.auth._sessions), remote_auth.MAX_SESSIONS)
        self.assertEqual((self.me(cookies[0]), self.me(cookies[1]), self.me(newest)), (200, 401, 200))
        print("ok  sessions: a future-dated one is refused; past MAX_SESSIONS the least recently seen goes")

    def test_revoked_records_are_kept_and_pruned(self):
        auth = RemoteAuth(self.root / "unit", HOST, wall=self.wall, mono=self.mono)
        auth.server = object()
        ids = []
        for _ in range(remote_auth.MAX_REVOKED + 3):
            self.wall.now += 10
            code = auth.open_window()["url"].split("#")[1]
            x = Authenticator()
            key = auth.pair_verify(code, x.create(auth.pair_options(code)))["passkey"]
            ids.append(x.id)
            self.assertTrue(auth.revoke(key))
        kept = [p["id"] for p in json.loads(auth.passkeys_path.read_text())["passkeys"]]
        self.assertEqual(len(kept), remote_auth.MAX_REVOKED)
        self.assertEqual(kept, ids[3:], "the oldest revocations go first")
        self.assertEqual(remote_auth.MAX_REVOKED, 16)
        print("ok  revoked passkeys: the newest 16 records are kept, the oldest pruned")

    def test_a_transient_read_error_does_not_stick(self):
        a = self.pair()
        cookie = self.signed_in(a)
        fresh = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        os.chmod(fresh.passkeys_path, 0)
        try:
            self.assertEqual(fresh.status()["state"], "unreadable")
            self.assertIsNone(fresh.session_for([cookie]))
        finally:
            os.chmod(fresh.passkeys_path, 0o600)
        self.assertEqual(fresh.status()["state"], "ok", "read again on the next call, no restart")
        self.assertIsNotNone(fresh.session_for([cookie]))
        print("ok  a passkeys file that could not be read is read again on the next call")

    def test_end_one_session(self):
        a = self.pair()
        one, two = self.signed_in(a), self.signed_in(a)
        sessions = self.auth.status()["passkeys"][0]["sessions"]
        target = next(s["id"] for s in sessions
                      if self.auth._sessions[remote_auth.token_hash(one.split("=", 1)[1])]["id"] == s["id"])
        self.assertEqual(self.hud("POST", f"/phone/sessions/{target}/end")[0], 200)
        self.assertEqual((self.me(one), self.me(two)), (401, 200))
        self.assertEqual(self.hud("POST", f"/phone/sessions/{target}/end")[0], 404)
        print("ok  End: ends that one session only")

    def test_logout(self):
        a = self.pair()
        cookie = self.signed_in(a)
        status, headers, _ = self.phone("POST", "/m/api/logout", {}, cookie=cookie, origin=None)
        self.assertEqual(status, 403, "a write without Origin")
        self.assertEqual(self.me(cookie), 200)
        status, headers, body = self.phone("POST", "/m/api/logout", {}, cookie=cookie)
        self.assertEqual((status, body), (200, {"signed_in": False}))
        self.assertEqual(headers["Set-Cookie"],
                         "__Host-jarvis_m=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
        self.assertEqual(self.me(cookie), 401)
        self.assertEqual(self.phone("POST", "/m/api/logout", {}, cookie=cookie)[0], 401)
        print("ok  logout ends the session and clears the cookie")

    def test_only_token_hashes_at_rest(self):
        a = self.pair()
        status, headers, _ = self.login(a)
        token = self.cookie_of(headers).split("=", 1)[1]
        code = self.open_window()
        files = list(self.auth.dir.iterdir())
        self.assertEqual({f.name for f in files}, {"passkeys.json", "sessions.json"})
        for f in files:
            text = f.read_text()
            self.assertNotIn(token, text, f.name)
            self.assertNotIn(code, text, f.name)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(),
                      (self.auth.dir / "sessions.json").read_text())
        status, view = self.hud("GET", "/phone")
        text = json.dumps(view)
        self.assertNotIn(token, text)
        self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), text)
        self.assertNotIn(a.id, text)
        self.assertNotIn(self.auth._passkeys[0]["public_key"], text)
        print("ok  at rest and in the HUD: token hashes only, never a token, code or credential id")

    def test_files_and_folder_modes(self):
        a = self.pair()
        self.signed_in(a)
        self.assertEqual(stat.S_IMODE(os.lstat(self.auth.dir).st_mode), 0o700)
        for name in ("passkeys.json", "sessions.json"):
            self.assertEqual(stat.S_IMODE(os.lstat(self.auth.dir / name).st_mode), 0o600, name)
        self.assertEqual(self.auth.dir, Path(config.V2_DATA_DIR) / "remote")
        # A loosened folder is tightened on the next write.
        os.chmod(self.auth.dir, 0o755)
        self.signed_in(a)
        self.assertEqual(stat.S_IMODE(os.lstat(self.auth.dir).st_mode), 0o700)
        print("ok  files 0600 in a 0700 folder under the data root")

    def test_sessions_survive_a_restart_and_corruption_fails_closed(self):
        a = self.pair()
        cookie = self.signed_in(a)
        again = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        token = cookie.split("=", 1)[1]
        self.assertIsNotNone(again.session_for([cookie]))
        (self.auth.dir / "sessions.json").write_text("{not json")
        fresh = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertIsNone(fresh.session_for([cookie]), "an unreadable sessions file signs out")
        (self.auth.dir / "passkeys.json").write_text('{"version": 1, "passkeys": [{"key": 1}]}')
        broken = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertIsNone(broken.session_for([cookie]))
        for call in (broken.open_window, broken.login_options, lambda: broken.pair_options("x")):
            with self.assertRaises(remote_auth.AuthError) as caught:
                call()
            self.assertEqual(caught.exception.status, 503)
        self.assertEqual(broken.status()["state"], "unreadable")
        self.assertIn('"key": 1', (self.auth.dir / "passkeys.json").read_text(), "never overwritten")
        # A link in place of a file is never followed.
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "user_handle": None, "passkeys": []}))
        (self.auth.dir / "passkeys.json").unlink()
        (self.auth.dir / "passkeys.json").symlink_to(target)
        linked = RemoteAuth(self.auth.dir, HOST, wall=self.wall, mono=self.mono)
        self.assertEqual(linked.status()["state"], "unreadable")
        del token
        print("ok  sessions persist; an unreadable sessions file signs out; bad passkeys fail closed")

    def test_ua_cleaned_and_advisory_headers_grant_nothing(self):
        a = self.pair()
        nasty = "Mozilla/5.0 (iPhone)\x1b[31m\u202eevil<b>" + "x" * 400
        advisory = [("Tailscale-User-Login", "owner@example.com"), ("Tailscale-User-Name", "Owner\x00Name")]
        # On the wire as UTF-8 bytes, which http.server reads as Latin-1.
        wire = nasty.encode("utf-8").decode("latin-1")
        status, headers, _ = self.login(a, extra=[("User-Agent", wire)] + advisory)
        self.assertEqual(status, 200)
        session = self.auth.status()["passkeys"][0]["sessions"][0]
        self.assertLessEqual(len(session["ua"]), remote_auth.UA_CAP)
        self.assertFalse(any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in session["ua"]), session["ua"])
        self.assertEqual(remote_auth.clean_text("a\u202eb\u200bc\x00d\n e", 50), "a b c d e")
        self.assertEqual(remote_auth.clean_text(None, 50), "")
        self.assertIn("<b>", session["ua"], "kept as text; the HUD renders it as text")
        self.assertEqual(session["advisory"], {"login": "owner@example.com", "name": "Owner Name"})
        # The headers on their own: nothing.
        for path, method in (("/m/api/me", "GET"), ("/m/api/logout", "POST"), ("/m/api/inbox", "GET")):
            status, _, body = self.phone(method, path, {} if method == "POST" else None, extra=advisory)
            self.assertEqual((status, body), (401, {"error": "unauthorized"}), path)
        # And with another session's cookie they change nothing either.
        cookie = self.cookie_of(headers)
        status, _, _ = self.phone("GET", "/m/api/me", cookie=cookie,
                                  extra=[("Tailscale-User-Login", "someone-else@example.com")])
        self.assertEqual(status, 200)
        print("ok  the user agent is cleaned and capped; Tailscale's headers are recorded and grant nothing")


# -- the owner test (§4) ------------------------------------------------------------------


class OwnerTest(Remote):
    def handler(self, server, method, headers):
        msg = Message()
        for name, value in headers:
            msg[name] = value
        return SimpleNamespace(server=server, command=method, headers=msg, remote_auth=self.auth)

    def test_status_codes_with_no_detail(self):
        a = self.pair()
        cookie = self.signed_in(a)
        cases = [
            ("GET", "/m/api/me", [("Host", "localhost")], 400, "untrusted host"),
            ("GET", "/m/api/me", [("Host", HOST + ":443"), ("Cookie", cookie)], 400, "untrusted host"),
            ("POST", "/m/api/logout", [("Host", HOST), ("Cookie", cookie)], 403, "cross-origin request refused"),
            ("POST", "/m/api/logout", [("Host", HOST), ("Origin", "https://evil.ts.net"), ("Cookie", cookie)],
             403, "cross-origin request refused"),
            ("GET", "/m/api/me", [("Host", HOST)], 401, "unauthorized"),
            ("GET", "/m/api/me", [("Host", HOST), ("Cookie", "__Host-jarvis_m=" + "A" * 43)], 401, "unauthorized"),
            ("GET", "/m/api/me", [("Host", HOST), ("Cookie", cookie + "; " + cookie)], 401, "unauthorized"),
            ("GET", "/m/api/me", [("Host", HOST), ("Cookie", "jarvis_m=" + cookie.split("=", 1)[1])], 401,
             "unauthorized"),
        ]
        for method, path, headers, want, word in cases:
            status, _, body = raw(self.port, method, path, headers, b"{}" if method == "POST" else None)
            self.assertEqual((status, json.loads(body)), (want, {"error": word}), (method, headers))
        # With a session, P-3's reads answer (tests/v2/remote_api_check.py)
        # and anything else under /m/api is 404; without one, all are 401.
        for path in ("/m/api/inbox", "/m/api/threads"):
            self.assertEqual(self.phone("GET", path, cookie=cookie)[0], 200, path)
            self.assertEqual(self.phone("GET", path)[0], 401, path)
        for path in ("/m/api/", "/m/api/pair/options", "/m/api/inbox/x", "/m/api/logout"):
            self.assertEqual(self.phone("GET", path, cookie=cookie)[0], 404, path)
            self.assertEqual(self.phone("GET", path)[0], 401, path)
        # P-4's answer route (remote_answer): a malformed row id is 400
        # signed in; without a session, 401 like everything else.
        self.assertEqual(self.phone("POST", "/m/api/inbox/x/answer", {}, cookie=cookie)[0], 400)
        self.assertEqual(self.phone("POST", "/m/api/inbox/x/answer", {})[0], 401)
        print("ok  §4: bad Host 400, bad Origin 403, no session 401, no detail; with one, "
              "P-3's reads and P-4's answers answer and the rest of /m/api is 404")

    def test_is_remote_owner_items(self):
        a = self.pair()
        cookie = self.signed_in(a)
        server = self.auth.server
        good = [("Host", HOST), ("Cookie", cookie)]
        self.assertTrue(remote_auth.is_remote_owner(self.handler(server, "GET", good)))
        self.assertEqual(remote_auth.owner_status(self.handler(server, "GET", good))[0], 200)
        self.assertEqual(remote_auth.owner_status(self.handler(server, "POST", good + [("Origin", ORIGIN)]))[0], 200)
        shapes = [
            (server, "GET", [("Host", "localhost"), ("Cookie", cookie)], 400),
            (server, "GET", [("Host", HOST), ("Host", HOST), ("Cookie", cookie)], 400),
            (server, "POST", good, 403),
            (server, "POST", good + [("Origin", ORIGIN), ("Origin", ORIGIN)], 403),
            (server, "DELETE", good + [("Origin", "http://" + HOST)], 403),
            (server, "GET", [("Host", HOST)], 401),
            (SimpleNamespace(server_address=("127.0.0.1", server.server_address[1]), remote_only=True),
             "GET", good, 401),
        ]
        for srv, method, headers, want in shapes:
            self.assertEqual(remote_auth.owner_status(self.handler(srv, method, headers))[0], want, headers)
            self.assertFalse(remote_auth.is_remote_owner(self.handler(srv, method, headers)))
        # The HUD's listener, with the phone's cookie: not the remote owner.
        face = next(s for s, _ in self.d._listeners if s.server_address[1] == self.d.face_port)
        self.assertEqual(remote_auth.owner_status(self.handler(face, "GET", good))[0], 401)
        # No remote auth on the handler at all.
        bare = SimpleNamespace(server=server, command="GET", headers=Message())
        self.assertFalse(remote_auth.is_remote_owner(bare))
        print("ok  is_remote_owner: §4 items 1 to 4, each refused on its own")

    def test_is_owner_still_false_on_the_remote_listener(self):
        a = self.pair()
        cookie = self.signed_in(a)
        server = self.auth.server
        for method, headers in (("GET", [("Host", HOST), ("Cookie", cookie)]),
                                ("POST", [("Host", HOST), ("Origin", ORIGIN), ("Cookie", cookie)]),
                                ("GET", [("Host", HOST), ("Cookie", cookie), ("Sec-Fetch-Site", "same-origin")])):
            self.assertTrue(remote_auth.is_remote_owner(self.handler(server, method, headers)))
            self.assertFalse(projects.is_owner(self.handler(server, method, headers), self.d))
        # The phone's cookie on the HUD's own routes opens nothing.
        host = f"127.0.0.1:{self.d.face_port}"
        status, _, _ = raw(self.d.face_port, "GET", "/phone", [("Host", host), ("Cookie", cookie)])
        self.assertEqual(status, 403)
        status, _, _ = raw(self.d.face_port, "POST", "/phone/pair",
                           [("Host", HOST), ("Origin", ORIGIN), ("Cookie", cookie)], b"{}")
        self.assertEqual(status, 403)
        print("ok  is_owner stays false on the remote listener, signed in or not")

    def test_without_auth_every_api_is_401(self):
        handler = remote_listener.handler_class(HOST)
        self.assertIsNone(handler.remote_auth)
        server = remote_listener.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.remote_only = True
        import threading
        worker = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        worker.start()
        try:
            port = server.server_address[1]
            for method, path in (("POST", "/m/api/login/options"), ("POST", "/m/api/pair/options"),
                                 ("GET", "/m/api/me")):
                headers = [("Host", HOST), ("Origin", ORIGIN)]
                status, _, body = raw(port, method, path, headers, b"{}" if method == "POST" else None)
                self.assertEqual((status, json.loads(body)), (401, {"error": "unauthorized"}))
        finally:
            server.shutdown()
            server.server_close()
        print("ok  a remote listener with no auth attached answers every /m/api with 401")


# -- bodies ---------------------------------------------------------------------------------


class Bodies(Remote):
    def counted(self):
        """Wrap each request's `rfile` to count the bytes the handler takes
        off the wire (request line and headers included)."""
        cls = self.auth.server.RequestHandlerClass
        real_setup = cls.setup
        readers = []

        class Counted:
            def __init__(self, inner):
                self.inner, self.n = inner, 0

            def _take(self, data):
                self.n += len(data)
                return data

            def read(self, *a):
                return self._take(self.inner.read(*a))

            def read1(self, *a):
                return self._take(self.inner.read1(*a))

            def readline(self, *a):
                return self._take(self.inner.readline(*a))

            def peek(self, *a):
                return self.inner.peek(*a)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def setup(handler):
            real_setup(handler)
            handler.rfile = Counted(handler.rfile)
            readers.append(handler.rfile)

        guard = patch.object(cls, "setup", setup)
        guard.start()
        self.addCleanup(guard.stop)
        return readers

    def exchange(self, readers, data: bytes) -> tuple[bytes, int]:
        before = len(readers)
        try:
            answer = send_bytes(self.port, data)
        except ConnectionResetError:
            answer = b""
        for _ in range(100):
            if len(readers) > before:
                break
            time.sleep(0.01)
        time.sleep(0.05)
        return answer, readers[-1].n

    def test_every_wire_byte_is_read(self):
        readers = self.counted()
        head = f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
        piece = b"x" * 8192
        trailers = b"X-One: a\r\nX-Two: b\r\n"
        cases = [
            ("over-cap Content-Length", 413, (head + f"Content-Length: {remote_listener.BODY_CAP + 4096}\r\n\r\n")
             .encode() + b"x" * (remote_listener.BODY_CAP + 4096)),
            ("chunked over the cap", 413, (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
             + b"".join(b"%x\r\n%s\r\n" % (len(piece), piece) for _ in range(10)) + b"0\r\n\r\n"),
            ("one chunk over the cap", 413, (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
             + b"%x;ext=1\r\n" % 70000 + b"y" * 70000 + b"\r\n0\r\n\r\n"),
            ("chunked with trailers", 200, (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
             + b"2\r\n{}\r\n0\r\n" + trailers + b"\r\n"),
            ("chunked over the cap with trailers", 413, (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
             + b"".join(b"%x\r\n%s\r\n" % (len(piece), piece) for _ in range(9)) + b"0\r\n" + trailers + b"\r\n"),
            ("a refusal (bad Host) with a body", 400,
             f"POST /m/api/me HTTP/1.1\r\nHost: evil\r\nContent-Length: 5000\r\n\r\n".encode() + b"z" * 5000),
        ]
        for name, want, data in cases:
            answer, taken = self.exchange(readers, data)
            self.assertTrue(answer.startswith(f"HTTP/1.0 {want}".encode()), (name, answer[:60]))
            self.assertEqual(taken, len(data), name)
        print("ok  bodies: every byte on the wire is read before the answer (413s, trailers, a refusal)")

    def test_wire_budget_and_trailer_cap(self):
        readers = self.counted()
        with patch.object(remote_listener, "DRAIN_CAP", 128 * 1024):
            head = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                    f"Content-Length: {256 * 1024}\r\n\r\n").encode()
            answer, taken = self.exchange(readers, head + b"x" * (256 * 1024))
            self.assertEqual(taken, len(head) + 128 * 1024, "drained to the budget, no further")
            head = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n").encode()
            body = b"".join(b"1\r\nx\r\n" for _ in range(40000)) + b"0\r\n\r\n"
            answer, taken = self.exchange(readers, head + body)
            self.assertLessEqual(taken - len(head), 128 * 1024 + 1100,
                                 "chunk framing counts against the budget too")
        head = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                "Transfer-Encoding: chunked\r\n\r\n").encode()
        many = b"".join(b"X-%d: %s\r\n" % (i, b"t" * 100) for i in range(100))
        answer, _ = self.exchange(readers, head + b"2\r\n{}\r\n0\r\n" + many + b"\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.0 400"), answer[:60])
        self.assertEqual(remote_listener.TRAILER_CAP, 8 * 1024)
        print("ok  bodies: the wire budget bounds Content-Length and chunked drains; trailers capped at 8 KiB")

    def test_whole_body_deadline(self):
        self.assertEqual(remote_listener.BODY_DEADLINE_S, 10.0)
        with patch.object(remote_listener, "BODY_DEADLINE_S", 1.0):
            head = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                    "Content-Length: 6\r\n\r\n").encode()
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
                sock.sendall(head)
                for byte in b'{ }   ':
                    time.sleep(0.3)               # each read well inside the socket timeout
                    try:
                        sock.sendall(bytes([byte]))
                    except OSError:
                        break
                answer = b""
                while True:
                    try:
                        chunk = sock.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    answer += chunk
            self.assertTrue(answer.startswith(b"HTTP/1.0 408"), answer[:60])
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
                sock.sendall(head)
                for byte in b'{}    ':
                    time.sleep(0.05)
                    sock.sendall(bytes([byte]))
                answer = sock.recv(65536)
            self.assertTrue(answer.startswith(b"HTTP/1.0 200"), answer[:60])
        print("ok  bodies: a body dripped past the whole-body deadline is 408; a quick one is read")

    def test_body_cap_413(self):
        self.assertEqual(remote_listener.BODY_CAP, 64 * 1024)
        big = b'{"code": "' + b"x" * remote_listener.BODY_CAP + b'"}'
        status, _, body = self.phone("POST", "/m/api/pair/options", body=big)
        self.assertEqual((status, body), (413, {"error": "request body too large"}))
        ok = json.dumps({"pad": "x" * (remote_listener.BODY_CAP - 100)}).encode()
        self.assertLessEqual(len(ok), remote_listener.BODY_CAP)
        self.assertEqual(self.phone("POST", "/m/api/login/options", body=ok)[0], 200)
        # Bad lengths.
        for length in ("-1", "abc", "1e3", "10, 10"):
            data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                    f"Content-Length: {length}\r\n\r\n{{}}").encode()
            self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 400"), length)
        print("ok  bodies: 64 KiB accepted, more is 413; a malformed length is 400")

    def test_unread_body_is_drained(self):
        # Answers that never look at the body: a bad Host, a bad Origin, no
        # session, a 404, a 413. Each must read it first, so the socket
        # closes cleanly (FIN) rather than with a reset that a proxy turns
        # into a 502. The whole body is in flight before any answer.
        body = b"x" * (48 * 1024)
        cases = [(f"POST /m/api/me HTTP/1.1\r\nHost: evil\r\n", 400),
                 (f"POST /m/api/me HTTP/1.1\r\nHost: {HOST}\r\n", 403),
                 (f"POST /m/api/inbox HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n", 401),
                 (f"POST /m/nothing HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n", 404),
                 (f"GET /m/api/me HTTP/1.1\r\nHost: {HOST}\r\n", 401)]
        for head, want in cases:
            for _ in range(5):
                data = (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body
                answer = send_bytes(self.port, data)
                self.assertTrue(answer.startswith(f"HTTP/1.0 {want}".encode()), (head, answer[:60]))
                self.assertTrue(answer.endswith(b"}"), "the whole answer arrived")
        over = b"x" * (remote_listener.BODY_CAP + 4096)
        data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                f"Content-Length: {len(over)}\r\n\r\n").encode() + over
        for _ in range(5):
            self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 413"))
        # Proof the reads happen: the handler's own reader is what consumed it.
        seen = []
        real = remote_listener._read_body

        def spy(handler):
            out = real(handler)
            seen.append(len(out))
            return out
        with patch.object(remote_listener, "_read_body", spy):
            send_bytes(self.port, (cases[0][0] + f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        self.assertEqual(seen, [len(body)], "a 400 for a bad Host still read the body")
        print("ok  bodies: every answer, a refusal's included, reads the body first (no reset)")

    def test_chunked_bodies(self):
        payload = b"{}"
        chunks = b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload)
        data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                "Transfer-Encoding: chunked\r\n\r\n").encode() + chunks
        self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 200"))
        piece = b"x" * 8192
        many = b"".join(b"%x\r\n%s\r\n" % (len(piece), piece) for _ in range(10)) + b"0\r\n\r\n"
        data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                "Transfer-Encoding: chunked\r\n\r\n").encode() + many
        self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 413"))
        for bad in (b"zz\r\n{}\r\n0\r\n\r\n", b"2\r\n{}XX0\r\n\r\n"):
            data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n").encode() + bad
            self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 400"), bad)
        data = (f"POST /m/api/login/options HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n").encode() + chunks
        self.assertTrue(send_bytes(self.port, data).startswith(b"HTTP/1.0 400"), "both framings")
        print("ok  bodies: chunked decoded under the same cap; malformed or doubly framed is 400")


# -- status, storage, source ------------------------------------------------------------


class StatusAndState(Remote):
    def test_status_fields(self):
        host = f"127.0.0.1:{self.d.port}"
        status, _, body = raw(self.d.port, "GET", "/status", [("Host", host)])
        value = json.loads(body)
        secure = value["remote"].pop("secure")
        self.assertEqual(value["remote"], {"port": self.port, "bound": True, "host_configured": True})
        # P-4: §4.1's last result, in fixed words; a test daemon reads no
        # Windows command, so it is limited.
        self.assertEqual(set(secure), {"ok", "failed", "reason", "label", "at"})
        self.assertFalse(secure["ok"])
        self.assertNotIn(HOST, body.decode())
        other = Daemon(Stores(self.root / "other"), {name: Fake(name) for name in PN},
                       lambda *_: lambda *_: Decision.DENY, 0, remote_host=None)
        other.start()
        self.addCleanup(other.stop)
        host = f"127.0.0.1:{other.port}"
        value = json.loads(raw(other.port, "GET", "/status", [("Host", host)])[2])
        # No listener: never checked, and shown as such.
        self.assertEqual(value["remote"].pop("secure")["failed"], "pending")
        self.assertEqual(value["remote"], {"port": 0, "bound": False, "host_configured": False})
        status, view = self.hud("GET", "/phone")
        self.assertEqual(set(view["remote"].pop("secure")), {"ok", "failed", "reason", "label", "at"})
        self.assertEqual(view["remote"], {"port": self.port, "bound": True, "host_configured": True})
        print("ok  /status: remote {port, bound, host_configured, secure}, no name, no secret")

    def test_protected_paths(self):
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2
        remote = Path(config.V2_DATA_DIR) / "remote"
        for name in ("passkeys.json", "sessions.json"):
            target = remote / name
            self.assertIn(target.resolve(), files._protected_state(), name)
            self.assertIn(target.resolve(), v2.protected_paths(), name)
            self.assertTrue(v2.denied_file(str(target)), name)
            self.assertTrue(v2.denied_command(f"cp /tmp/x {target}"), name)
            self.assertTrue(v2._command_writes_protected_state(f"cp /tmp/x {target}"), name)
            from jarvis import runtime, tools
            with runtime.unconfined():
                out = tools.dispatch("write_file", json.dumps({"path": str(target), "content": "{}"}))
            self.assertFalse(target.exists(), out.text)
        self.assertNotIn((remote / "other.json").resolve(), files._protected_state())
        # The folder itself (attacker review of PR #306, LOW-1): replacing or
        # removing it is a certain touch, refused at v2's layer 1 and never
        # auto-approved by v1; reading it is not; nothing else changed.
        from jarvis import permissions as v1, protected_state
        from jarvis import rules as v1_rules
        remote.mkdir(parents=True, exist_ok=True)
        for command in (f"rm -rf {remote}", f"rm -r {remote}/", f"mv {remote} /tmp/elsewhere",
                        f"mv /tmp/evil {remote}", f"cp -r /tmp/evil {remote}",
                        f"cd {remote.parent} && rm -rf remote"):
            touch = protected_state.command_touch(command)
            self.assertTrue(touch is not None and touch.certain, command)
            self.assertTrue(v2.denied_command(command), command)
            self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
        # Inline source that spells the folder in full beside a call that
        # removes or replaces it (second review of PR #306).
        for command in (f"python3 -c \"import shutil; shutil.rmtree('{remote}')\"",
                        f"python3 -c \"import os; os.rename('{remote}', '/tmp/x')\"",
                        f"node -e \"require('fs').rmSync('{remote}', {{recursive: true}})\""):
            self.assertTrue(v2.denied_command(command), command)
            self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
        for command in (f"ls {remote}", f"cat {remote}/sessions.json", f"cp {remote}/passkeys.json /tmp/b",
                        f"python3 -c \"import os; print(os.listdir('{remote}'))\"",
                        f"python3 -c \"import shutil; shutil.rmtree('{remote}2')\""):
            self.assertFalse(v2.denied_command(command), command)
        # The data root above it is asked, as before, not refused.
        touch = protected_state.command_touch(f"rm -rf {remote.parent}")
        self.assertTrue(touch is not None and not touch.certain)
        self.assertFalse(v2.denied_command(f"rm -rf {remote.parent}"))
        print("ok  passkeys.json, sessions.json and their remote/ folder are protected, refused to agent writes")

    def test_guard_strength_pinned(self):
        self.assertEqual((remote_auth.CODE_BYTES, remote_auth.TOKEN_BYTES, remote_auth.CHALLENGE_BYTES),
                         (32, 32, 32))
        self.assertEqual((remote_auth.WINDOW_S, remote_auth.CHALLENGE_S), (300, 60))
        self.assertTrue(remote_auth.REQUIRE_UV)
        self.assertEqual(remote_auth.COOKIE, "__Host-jarvis_m")
        print("ok  guard strengths pinned: 256-bit code, token and challenge; 5 min window, 60 s challenge")

    def test_the_pr_subject_family(self):
        """PM-D: the owner's PR actions are their own family: `pr:` and 56
        hex only, their own decisions, their own per-session budget; no
        other family's subject takes a PR decision, nor the reverse."""
        self.assertEqual(remote_auth.PR_DECISIONS, ("merge", "merge_as_is", "update", "fix"))
        self.assertEqual(remote_auth.ANSWER_FAMILIES, ("approval", "task", "defer", "review", "pr"))
        self.assertEqual(remote_auth.MAX_ANSWER_CHALLENGES,
                         remote_auth.MAX_ANSWER_CHALLENGES_PER_SESSION * remote_auth.MAX_SESSIONS * 5)
        good = "pr:" + "0123456789abcdef" * 3 + "01234567"
        self.assertRegex(good, remote_auth._PR_SUBJECT)
        for bad in ("pr:" + "a" * 55, "pr:" + "a" * 57, "pr:" + "A" * 56, "PR:" + "a" * 56, "pr:" + "g" * 56):
            self.assertIsNone(remote_auth._PR_SUBJECT.fullmatch(bad), bad)
        a = self.pair()
        self.signed_in(a)
        session = next(iter(self.auth._sessions.values()))
        s = {"token_sha256": session["token_sha256"]}
        for req_id, decision in ((good, "allow"), (good, "always"), (good, "cancel"), (good, "rebase"),
                                 ("abc", "merge"), ("task:0123abcd", "merge"), ("defer:abc", "update"),
                                 ("defers:" + "a" * 56, "merge"), ("pr:" + "a" * 55, "merge")):
            with self.assertRaises(remote_auth.AuthError) as caught:
                self.auth.answer_options(s, req_id, decision)
            self.assertEqual(caught.exception.status, 400, (req_id, decision))
        for decision in remote_auth.PR_DECISIONS:
            self.auth.answer_options(s, good, decision)
        families = {c["family"] for c in self.auth._challenges.values() if c.get("req_id") == good}
        self.assertEqual(families, {"pr"})
        print("ok  the pr: family: its own subject shape, decisions and budget; never another family's")

    def test_webauthn_missing_is_503_and_the_daemon_starts(self):
        text = (ROOT / "jarvis" / "v2" / "remote_auth.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^(import webauthn|from webauthn)", "imported only when used")
        with patch.object(remote_auth, "_webauthn",
                          side_effect=remote_auth.AuthError(503, "passkeys unavailable")):
            status, _, body = self.phone("POST", "/m/api/login/options", {})
            self.assertEqual((status, body), (503, {"error": "passkeys unavailable"}))
            # The desk refuses to open a window that would only 503 after the scan.
            status, body = self.hud("POST", "/phone/pair")
            self.assertEqual(status, 503, body)
            self.assertIn("uv pip install -e .", body["error"])
            self.assertFalse(self.auth.status()["pairing"]["open"])
        print("ok  without webauthn: 503 on the passkey routes; nothing imports it at start-up")

    def test_source(self):
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/remote_auth.py", SELF_PROTECTED)
        self.assertIn("jarvis/v2/remote_listener.py", SELF_PROTECTED)
        print("ok  remote_auth.py is SELF_PROTECTED")


if __name__ == "__main__":
    unittest.main(verbosity=1)
