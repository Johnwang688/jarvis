"""Google OAuth: one-time human consent, then silent token refresh for tools.

The rule (CLAUDE.md, safety design): Jarvis *uses* credentials but never
*sees* them. This module is the only reader of `config.GOOGLE_TOKEN_PATH` —
a JSON bundle {client_id, client_secret, refresh_token, scopes} written by
`jarvis auth google`. tools/secrets.py makes that file unreadable through
every tool and scrubs its values out of tool output; the model only ever
sees Google API responses.

The consent flow is deliberately human-only (a CLI subcommand, not a tool):
your Google password goes into accounts.google.com in your own browser,
never into anything Jarvis drives. The refresh token that comes back is
scoped to SCOPES below and revocable at myaccount.google.com — leaking it
is a bounded, fixable event, which is why we never touch the password.

Widening SCOPES means re-running `jarvis auth google`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
]


class AuthError(RuntimeError):
    """Auth is missing or broken. Tools return the message as text."""


_lock = threading.Lock()
_cached_token: str | None = None
_expires_at: float = 0.0


def _load_bundle() -> dict:
    path = config.GOOGLE_TOKEN_PATH
    if not path.exists():
        raise AuthError(
            "Google is not connected. The owner needs to run `jarvis auth google` "
            "once — tell them, and offer to explain the setup."
        )
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"the Google token bundle is unreadable ({exc}).") from exc
    missing = {"client_id", "client_secret", "refresh_token"} - bundle.keys()
    if missing:
        raise AuthError(
            f"the token bundle is missing {sorted(missing)} — "
            "the owner should re-run `jarvis auth google`."
        )
    return bundle


def access_token() -> str:
    """A live access token, cached until shortly before expiry.

    The return value is put in an Authorization header by tool code and is
    never part of a tool result.
    """
    global _cached_token, _expires_at
    with _lock:
        if _cached_token and time.time() < _expires_at - 60:
            return _cached_token

        bundle = _load_bundle()
        try:
            response = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": bundle["client_id"],
                    "client_secret": bundle["client_secret"],
                    "refresh_token": bundle["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach Google's token endpoint ({exc}).") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or "access_token" not in payload:
            error = payload.get("error", f"HTTP {response.status_code}")
            if error == "invalid_grant":
                raise AuthError(
                    "Google refused the refresh token (revoked, or expired because "
                    "the OAuth app is still in 'Testing' status). The owner needs "
                    "to re-run `jarvis auth google`."
                )
            raise AuthError(f"token refresh failed ({error}).")

        _cached_token = payload["access_token"]
        _expires_at = time.time() + float(payload.get("expires_in", 3600))
        return _cached_token


def invalidate() -> None:
    """Drop the cached access token (called after a 401 so the next call refreshes)."""
    global _cached_token
    with _lock:
        _cached_token = None


# -- one-time consent (human-only, via `jarvis auth google`) -----------------


def _open_browser(url: str) -> None:
    """Best effort — the URL is printed either way.

    On WSL the Linux webbrowser module has no browser to talk to (gio prints
    'Operation not supported'); go through PowerShell so the Windows default
    browser opens, same idea as the face's find_browser().
    """
    import shutil
    import subprocess

    powershell = shutil.which("powershell.exe")
    if powershell:
        try:
            subprocess.Popen(
                [powershell, "-NoProfile", "-Command", f'Start-Process "{url}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def connect(client_json: str | None) -> int:
    """Run the interactive consent flow, or report status with no argument."""
    token_path = config.GOOGLE_TOKEN_PATH

    if client_json is None:
        if token_path.exists():
            bundle = json.loads(token_path.read_text(encoding="utf-8"))
            print(f"connected — token bundle at {token_path}")
            for scope in bundle.get("scopes", []):
                print(f"  scope: {scope}")
            print("revoke any time at https://myaccount.google.com/permissions")
        else:
            print("not connected.")
            print("1. console.cloud.google.com → APIs & Services → enable the Gmail API")
            print("2. Google Auth Platform → Audience → Publish app. In 'Testing' status")
            print("   consent 403s (access_denied) unless you are a listed test user,")
            print("   and refresh tokens die after 7 days.")
            print("3. Credentials → Create credentials → OAuth client ID → Desktop app")
            print("4. Download the client JSON, then:  jarvis auth google <that file>")
        return 0

    source = Path(client_json).expanduser()
    if not source.exists():
        # An unexpanded glob (no match in that directory) arrives literally;
        # resolve it ourselves before giving up.
        import glob as globmod

        matches = sorted(globmod.glob(str(source)))
        if matches:
            source = Path(matches[0])
        else:
            print(f"no such file: {source}")
            print(
                "On WSL, browser downloads land on the Windows side — look in "
                "/mnt/c/Users/<you>/Downloads/"
            )
            return 1

    raw = json.loads(source.read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web") or raw
    client_id, client_secret = client["client_id"], client["client_secret"]

    # PKCE + state: standard protection for a loopback-redirect flow.
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)

    result: dict[str, str] = {}

    class _Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if not query:  # favicon and friends
                self.send_response(404)
                self.end_headers()
                return
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Jarvis is connected. You can close this tab.</h2>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Catch)
    port = server.server_address[1]
    redirect = f"http://localhost:{port}/"

    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",  # this is what yields a refresh token
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    print("Open this URL and approve access (your password goes to Google, not Jarvis):")
    print(f"\n  {url}\n")
    _open_browser(url)

    print(f"waiting up to 5 minutes for Google's redirect on {redirect} …")
    server.timeout = 10
    deadline = time.time() + 300
    while time.time() < deadline and "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if result.get("state") != state:
        print("state mismatch — aborting. Run the command again and use the fresh URL.")
        return 1
    if "code" not in result:
        print(f"no authorization code arrived ({result.get('error', 'timed out')}).")
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=30,
    )
    payload = response.json()
    if "refresh_token" not in payload:
        print(f"Google returned no refresh token ({payload.get('error', response.status_code)}).")
        print("If you have connected before, revoke the old grant at "
              "https://myaccount.google.com/permissions and retry.")
        return 1

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.parent.chmod(0o700)
    token_path.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": payload["refresh_token"],
                "scopes": SCOPES,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    token_path.chmod(0o600)
    invalidate()
    print(f"connected — token bundle written to {token_path} (mode 600).")
    print("Jarvis can now use Gmail; every send still needs your approval.")
    return 0
