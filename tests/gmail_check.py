"""Synthetic checks for the Gmail integration. Free — no network, no API.

What must hold:
  1. auth: a missing bundle is a clear error, refresh tokens exchange and
     cache, invalid_grant explains itself
  2. tools: search/read/send speak the Gmail API correctly (fake transport),
     the Authorization header carries the token, a 401 refreshes and retries
  3. gating: gmail_send is dangerous and a denial never touches the network
  4. secrets: google_token.json is unreadable through every layer and its
     values are scrubbed from tool output

Run:  .venv/bin/python tests/gmail_check.py
"""

from __future__ import annotations

import base64
import email
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, google_auth, tools
from jarvis.tools import secrets

REFRESH_TOKEN = "1//refresh-token-value-abc123-not-for-the-model"
CLIENT_SECRET = "GOCSPX-fake-client-secret-xyz"
ACCESS_TOKEN = "ya29.fake-access-token"


def _bundle(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "client_id": "1234.apps.googleusercontent.com",
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "scopes": google_auth.SCOPES,
            }
        ),
        encoding="utf-8",
    )


def _response(status: int, payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        status_code=status, json=lambda: payload, text=json.dumps(payload)
    )


def _reset_cache() -> None:
    google_auth._cached_token = None
    google_auth._expires_at = 0.0


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def auth_checks(token_path: Path) -> None:
    _reset_cache()

    config.GOOGLE_TOKEN_PATH = token_path.parent / "nope" / "google_token.json"
    try:
        google_auth.access_token()
        raise AssertionError("missing bundle did not raise")
    except google_auth.AuthError as exc:
        assert "jarvis auth google" in str(exc)

    config.GOOGLE_TOKEN_PATH = token_path
    _bundle(token_path)

    posts: list[dict] = []

    def fake_post(url, data=None, timeout=None):
        posts.append(data)
        assert url == google_auth.TOKEN_URL
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == REFRESH_TOKEN
        return _response(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})

    real_post = httpx.post
    httpx.post = fake_post
    try:
        assert google_auth.access_token() == ACCESS_TOKEN
        assert google_auth.access_token() == ACCESS_TOKEN  # cached
        assert len(posts) == 1, "second call should not re-exchange"

        _reset_cache()
        httpx.post = lambda *a, **k: _response(400, {"error": "invalid_grant"})
        try:
            google_auth.access_token()
            raise AssertionError("invalid_grant did not raise")
        except google_auth.AuthError as exc:
            assert "re-run `jarvis auth google`" in str(exc)
    finally:
        httpx.post = real_post
    print("ok  auth: missing bundle explains itself, refresh caches, invalid_grant clear")


def tool_checks(token_path: Path) -> None:
    config.GOOGLE_TOKEN_PATH = token_path
    _reset_cache()

    real_post, real_request = httpx.post, httpx.request
    httpx.post = lambda *a, **k: _response(
        200, {"access_token": ACCESS_TOKEN, "expires_in": 3600}
    )

    calls: list[dict] = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "headers": headers, **kwargs})
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        if url.endswith("/messages") and method == "GET":
            return _response(200, {"messages": [{"id": "m1"}]})
        if url.endswith("/messages/m1"):
            return _response(
                200,
                {
                    "snippet": "see attached invoice",
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "headers": [
                            {"name": "From", "value": "bob@example.com"},
                            {"name": "To", "value": "john@example.com"},
                            {"name": "Subject", "value": "Invoice"},
                            {"name": "Date", "value": "Thu, 31 Jul 2026 09:00:00 -0500"},
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": _b64url("Hi John,\nInvoice attached.")},
                            },
                            {"mimeType": "text/html", "body": {"data": _b64url("<p>x</p>")}},
                        ],
                    },
                },
            )
        if url.endswith("/messages/send"):
            return _response(200, {"id": "sent-1"})
        raise AssertionError(f"unexpected call: {method} {url}")

    httpx.request = fake_request
    try:
        out = tools.dispatch("gmail_search", json.dumps({"query": "from:bob"}))
        assert "id m1" in out.text and "bob@example.com" in out.text, out.text
        assert "Invoice" in out.text

        out = tools.dispatch("gmail_read", json.dumps({"message_id": "m1"}))
        assert "Invoice attached." in out.text, out.text
        assert "From: bob@example.com" in out.text

        out = tools.dispatch(
            "gmail_send",
            json.dumps({"to": "bob@example.com", "subject": "Re: Invoice", "body": "Paid, thanks!"}),
            approve=lambda t, a: True,
        )
        assert "Sent to bob@example.com" in out.text and "sent-1" in out.text, out.text

        sent = next(c for c in calls if c["url"].endswith("/messages/send"))
        message = email.message_from_bytes(
            base64.urlsafe_b64decode(
                sent["json"]["raw"] + "=" * (-len(sent["json"]["raw"]) % 4)
            )
        )
        assert message["To"] == "bob@example.com"
        assert message["Subject"] == "Re: Invoice"
        assert message.get_payload().strip() == "Paid, thanks!"
        print("ok  tools: search/read/send speak the API, MIME round-trips")

        # 401 → invalidate → refresh → retry once.
        _reset_cache()
        seen = {"n": 0}

        def flaky(method, url, headers=None, timeout=None, **kwargs):
            seen["n"] += 1
            if seen["n"] == 1:
                return _response(401, {"error": {"message": "expired"}})
            return _response(200, {"messages": []})

        httpx.request = flaky
        out = tools.dispatch("gmail_search", json.dumps({"query": "x"}))
        assert "No messages match" in out.text, out.text
        assert seen["n"] == 2, "401 should retry exactly once"
        print("ok  tools: a 401 drops the cached token and retries once")
    finally:
        httpx.post, httpx.request = real_post, real_request


def gating_checks(token_path: Path) -> None:
    config.GOOGLE_TOKEN_PATH = token_path

    assert tools.REGISTRY["gmail_send"].dangerous is True
    assert tools.REGISTRY["gmail_search"].dangerous is False
    assert tools.REGISTRY["gmail_read"].dangerous is False

    real_request = httpx.request
    httpx.request = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a denied send touched the network")
    )
    try:
        out = tools.dispatch(
            "gmail_send",
            json.dumps({"to": "a@b.c", "subject": "s", "body": "b"}),
            approve=lambda t, a: False,
        )
        assert "declined" in out.text, out.text
    finally:
        httpx.request = real_request
    print("ok  gating: gmail_send is dangerous; a denial never touches the network")


def secrets_checks(token_path: Path) -> None:
    config.GOOGLE_TOKEN_PATH = token_path
    _bundle(token_path)

    assert secrets.is_protected(token_path)
    assert secrets.is_protected("google_token.json")
    for command in (
        f"cat {token_path}",
        "cat google_token.json",
        "cat ~/.config/jarvis/google_token.*",
        "less 'google_token.json'",
    ):
        assert secrets.protected_in_command(command), command
    for command in ("grep -R foo *", "cat tokens.md", "ls ~/.config/jarvis"):
        assert secrets.protected_in_command(command) is None, command

    out = tools.dispatch("read_file", json.dumps({"path": str(token_path)}))
    assert "protected" in out.text, out.text

    scrubbed = secrets.scrub(f"output happens to contain {REFRESH_TOKEN} and {CLIENT_SECRET}")
    assert REFRESH_TOKEN not in scrubbed and CLIENT_SECRET not in scrubbed, scrubbed
    assert secrets.REDACTED in scrubbed
    print("ok  secrets: token file refused by name/glob/read_file, values scrubbed")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        token_path = Path(tmp) / "google_token.json"
        auth_checks(token_path)
        tool_checks(token_path)
        gating_checks(token_path)
        secrets_checks(token_path)
    print("\nall gmail checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
