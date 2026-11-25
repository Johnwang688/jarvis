"""PH1 passkey and session checks against the real RemoteAuth store."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from jarvis import config
from jarvis.v2 import remote_auth
from jarvis.v3 import phone_auth
from jarvis.v3.daemon import Daemon
from phone_listener_check import HOST, ORIGIN, request


_CREDENTIAL = b"fake-token"


def credential(options):
    # The clientDataJSON envelope follows the software authenticator in
    # tests/v2/remote_auth_check.py. Signature verification is faked here.
    data = json.dumps({"challenge": options["challenge"], "origin": ORIGIN,
                       "crossOrigin": False}).encode()
    cred_id = remote_auth.b64url(_CREDENTIAL)
    return {"id": cred_id, "rawId": cred_id, "response": {"clientDataJSON": remote_auth.b64url(data)}}


class FakeWebAuthn:
    @staticmethod
    def generate_authentication_options(*, challenge, rp_id, **_):
        return {"challenge": remote_auth.b64url(challenge), "rpId": rp_id}

    generate_registration_options = generate_authentication_options
    options_to_json = staticmethod(json.dumps)

    @staticmethod
    def verify_authentication_response(*, credential, expected_challenge, expected_rp_id,
                                       expected_origin, require_user_verification, **_):
        client = json.loads(remote_auth.unb64url(credential["response"]["clientDataJSON"]))
        if (client["challenge"] != remote_auth.b64url(expected_challenge)
                or client["origin"] != expected_origin or expected_rp_id != HOST
                or not require_user_verification):
            raise ValueError("invalid fake assertion")
        return SimpleNamespace(
            user_verified=True, new_sign_count=0, credential_id=_CREDENTIAL,
            credential_public_key=_CREDENTIAL, sign_count=0, credential_backed_up=True,
            credential_device_type="multi_device", aaguid="",
        )

    verify_registration_response = verify_authentication_response


def fake_webauthn():
    required = SimpleNamespace(REQUIRED="required")
    structs = SimpleNamespace(
        ResidentKeyRequirement=required, UserVerificationRequirement=required,
        AuthenticatorSelectionCriteria=lambda **kw: kw, PublicKeyCredentialDescriptor=lambda **kw: kw,
    )
    return FakeWebAuthn(), structs


_AGENTD_PROBE = """
import importlib.abc
from pathlib import Path
import runpy
import signal
import sys

class DenyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {"webauthn", "jarvis.agent", "jarvis.llm", "jarvis.models",
                        "jarvis.v2.providers", "jarvis.v2.remote_auth"}:
            raise AssertionError(fullname)

sys.meta_path.insert(0, DenyImports())
from jarvis import config
from jarvis.v3.daemon import Daemon

config.V3_DIR = Path(sys.argv[1]) / "data"
config.V3_API_PORT = 0
start = Daemon.start

def stop_after_start(self):
    start(self)
    assert self.port and self.phone_port is None
    signal.raise_signal(signal.SIGTERM)

Daemon.start = stop_after_start
sys.argv = ["jarvis", "agentd"]
runpy.run_module("jarvis", run_name="__main__")
"""


class PhoneAuthCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "JARVIS_REMOTE_HOST": ""}))
        self.enterContext(patch.object(config, "remote_dir", return_value=self.root / "remote"))
        self.enterContext(patch.object(remote_auth, "_webauthn", side_effect=fake_webauthn))
        self.clock = SimpleNamespace(now=100_000)

    def start(self):
        # Use the daemon's normal creation and stop paths, with only the
        # authenticator library and the monotonic clock faked.
        daemon = Daemon(port=0, phone_host=HOST, data_dir=self.root / "data",
                        hud_dir=self.root / "hud", jobs=())
        daemon.start()
        self.addCleanup(daemon.stop)
        self.daemon = daemon
        self.auth = daemon._phone_server.auth
        self.auth.mono = lambda: self.clock.now

    def call(self, route, *, method="POST", body=None, cookie=None):
        headers = {"Cookie": cookie} if cookie is not None else None
        status, headers, payload = request(
            self.daemon.phone_port, method, "/m/api/" + route, origin=ORIGIN,
            headers=headers, body=body,
        )
        return status, headers, json.loads(payload)

    def ok(self, route, **kwargs):
        status, headers, payload = self.call(route, **kwargs)
        self.assertEqual(status, 200, payload)
        return headers, payload

    def pair(self):
        code = urlsplit(self.auth.open_window()["url"]).fragment
        _, options = self.ok("pair/options", body={"code": code})
        headers, payload = self.ok("pair/verify", body={"code": code, "credential": credential(options)})
        self.assertIsNone(headers["Set-Cookie"])
        self.assertTrue(payload["paired"])
        return payload["passkey"]

    def sign_in(self):
        _, options = self.ok("login/options")
        headers, payload = self.ok("login/verify", body={"credential": credential(options)})
        self.assertEqual(payload, {"signed_in": True})
        self.assertIsNotNone(headers["Set-Cookie"])
        return headers["Set-Cookie"]

    def unlock(self, cookie):
        _, options = self.ok("unlock/options", cookie=cookie)
        _, payload = self.ok("unlock", cookie=cookie, body={"assertion": credential(options["publicKey"])})
        self.assertEqual(payload, {"unlocked": True, "unlock_s": 3600})

    def me(self, cookie):
        return self.ok("me", method="GET", cookie=cookie)[1]

    def test_01_open_routes_and_session_gate(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        for route, method in (
            ("me", "GET"), ("logout", "POST"), ("lock", "POST"), ("unlock/options", "POST"),
            ("unlock", "POST"), ("missing", "GET"), ("login/options", "GET"),
        ):
            with self.subTest(route=route, method=method):
                status, _, payload = self.call(route, method=method)
                self.assertEqual((status, payload), (401, {"error": "unauthorized"}))
        self.assertEqual(self.call("missing", method="GET", cookie=cookie)[0], 404)
        data = Mock(return_value=None)
        with patch.dict(sys.modules, {"jarvis.v3.phone_routes": SimpleNamespace(data_route=data)}):
            self.assertEqual(self.call("missing", method="GET")[0], 401)
            data.assert_not_called()
            self.me(cookie)
            data.assert_not_called()
            self.assertEqual(self.call("missing", method="GET", cookie=cookie)[0], 404)
            handler, method, path, body = data.call_args.args
            self.assertEqual((method, path, body), ("GET", "/m/api/missing", b""))
            self.assertIs(handler.server.auth, self.auth)
            self.assertEqual(handler.server.services.status()["service"], "jarvis-agentd")
            data.return_value = 200, {"ok": True}, []
            self.assertEqual(self.ok("sample", body={"value": "fake-token"}, cookie=cookie)[1], {"ok": True})
            self.assertEqual(json.loads(data.call_args.args[-1]), {"value": "fake-token"})

    def test_02_login_sets_secure_host_cookie(self):
        self.start()
        self.pair()
        raw = self.sign_in()
        name, attrs = raw.split("; ", 1)
        self.assertRegex(name, r"^__Host-jarvis_m=[A-Za-z0-9_-]{43}$")
        self.assertEqual(set(attrs.split("; ")), {"Path=/", "Max-Age=604800", "Secure",
                                                 "HttpOnly", "SameSite=Strict"})
        self.assertTrue(self.me(name)["signed_in"])

    def test_03_recognizes_v2_session_file(self):
        writer = remote_auth.RemoteAuth(self.root / "remote", HOST)
        code = urlsplit(writer.open_window()["url"]).fragment
        writer.pair_verify(code, credential(writer.pair_options(code)))
        token, old = writer.login_verify(credential(writer.login_options()))
        self.assertTrue((self.root / "remote/sessions.json").is_file())
        self.start()
        self.assertEqual(self.me(f"__Host-jarvis_m={token}"), {
            "signed_in": True, "session": {"created": old["created"], "ends": old["created"] + 604800},
            "unlock_s": 0,
        })

    def test_04_me_does_not_slide_unlock(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        self.unlock(cookie)
        self.clock.now += 123
        self.assertEqual(self.me(cookie)["unlock_s"], 3477)
        self.clock.now += 100
        self.assertEqual(self.me(cookie)["unlock_s"], 3377)

    def test_05_unlock_assertion_belongs_to_its_session(self):
        self.start()
        self.pair()
        first = self.sign_in().split(";", 1)[0]
        second = self.sign_in().split(";", 1)[0]
        _, options = self.ok("unlock/options", cookie=first)
        status, _, payload = self.call("unlock", cookie=second,
                                       body={"assertion": credential(options["publicKey"])})
        self.assertEqual((status, payload), (401, {"error": "unauthorized"}))
        self.assertEqual([self.me(cookie)["unlock_s"] for cookie in (first, second)], [0, 0])

    def test_06_unlock_expires_after_one_hour(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        self.unlock(cookie)
        self.clock.now += 3599
        self.assertEqual(self.me(cookie)["unlock_s"], 1)
        self.clock.now += 2
        self.assertEqual(self.me(cookie)["unlock_s"], 0)
        live = self.auth.session_for([cookie])
        handler = SimpleNamespace(remote_auth=self.auth)
        self.assertEqual(phone_auth.require_unlocked(handler, live),
                         (423, {"error": "locked", "unlock": True}, []))
        phone_auth.touch_unlock(handler, live)
        self.assertEqual(self.me(cookie)["unlock_s"], 0)
        self.unlock(cookie)
        for action in (phone_auth.touch_unlock, phone_auth.require_unlocked):
            self.clock.now += 100
            self.assertIsNone(action(handler, live))
            self.assertEqual(self.me(cookie)["unlock_s"], 3600)

    def test_07_lock_closes_the_window(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        self.unlock(cookie)
        self.assertEqual(self.ok("lock", cookie=cookie)[1], {"unlocked": False})
        self.assertEqual(self.me(cookie)["unlock_s"], 0)

    def test_08_logout_clears_cookie_and_ends_session(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        headers, payload = self.ok("logout", cookie=cookie)
        self.assertEqual(payload, {"signed_in": False})
        self.assertEqual(headers["Set-Cookie"],
                         "__Host-jarvis_m=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
        self.assertEqual(self.call("me", method="GET", cookie=cookie)[0], 401)

    def test_09_revoked_passkey_cookie_is_refused(self):
        self.start()
        key = self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        self.assertTrue(self.auth.revoke(key))
        self.assertEqual(self.call("me", method="GET", cookie=cookie)[0], 401)

    def test_10_restart_keeps_session_but_locks(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        self.unlock(cookie)
        old = self.auth
        self.daemon.stop()
        self.daemon.start()
        self.auth = self.daemon._phone_server.auth
        self.assertEqual(self.me(cookie)["unlock_s"], 0)
        self.assertIsNot(self.auth, old)

    def test_11_missing_webauthn_routes_and_agentd(self):
        self.start()
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        # Remove the fixture patch so RemoteAuth uses its real import path.
        with patch.object(remote_auth, "_webauthn", _real_webauthn), patch.dict(sys.modules, {"webauthn": None}):
            for route, body, session in (
                ("login/options", {}, None), ("login/verify", {"credential": {}}, None),
                ("pair/options", {}, None), ("pair/verify", {}, None),
                ("unlock/options", {}, cookie), ("unlock", {"assertion": {}}, cookie),
            ):
                with self.subTest(route=route):
                    status, _, payload = self.call(route, body=body, cookie=session)
                    self.assertEqual((status, payload), (503, {"error": "passkeys unavailable"}))
            self.assertTrue(self.me(cookie)["signed_in"])
        probe = self.root / "agentd_probe.py"
        probe.write_text(_AGENTD_PROBE)
        result = subprocess.run([sys.executable, str(probe), str(self.root)], capture_output=True,
                                text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


_real_webauthn = remote_auth._webauthn


if __name__ == "__main__":
    unittest.main()
