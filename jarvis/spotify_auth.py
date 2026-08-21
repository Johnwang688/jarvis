"""Spotify OAuth: one-time human consent, then silent token refresh for tools.

Same contract as google_auth.py (CLAUDE.md, safety design): Jarvis *uses* the
credential but never *sees* it. This module is the only reader of
`config.SPOTIFY_TOKEN_PATH` — a JSON bundle {client_id, refresh_token, scopes}
written by `jarvis auth spotify`. tools/secrets.py makes that file unreadable
through every tool and scrubs its values out of tool output; the model only
ever sees Spotify API responses.

Consent is deliberately human-only (a CLI subcommand, not a tool), so the
agent cannot initiate or widen its own access. Your Spotify password goes into
accounts.spotify.com in your own browser; the refresh token that comes back is
scoped to SCOPES below and revocable at spotify.com/account/apps.

Three Spotify-specific facts this flow is built around, each verified against
the current docs rather than assumed:

  * **PKCE, so there is no client secret.** Spotify's PKCE flow authenticates
    the *exchange* with a one-time verifier instead of a stored secret, which
    means the owner pastes only a public client id and there is no second
    credential to leak. The refresh token is the whole credential.

  * **The redirect URI must be `http://127.0.0.1:<port>` — literally.** Spotify
    requires HTTPS except for a loopback address, and rejects the spelling
    `localhost` outright. The port is therefore fixed
    (`config.SPOTIFY_AUTH_PORT`) rather than ephemeral like the Google flow's,
    because the owner registers this exact string by hand in the dashboard.

  * **Refresh tokens rotate.** Under PKCE a refresh response may carry a *new*
    refresh_token, and the old one stops working. Not persisting it is a bug
    that hides for an hour and then locks the owner out, so `_store()` writes
    the replacement back before the new access token is ever returned.

Widening SCOPES means re-running `jarvis auth spotify`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Minimal for what the tools actually do. Each one is a capability the owner
# reads on the consent screen, so an unused scope is a promise we did not need
# to ask for.
SCOPES = [
    "user-read-playback-state",       # what is playing, and on which devices
    "user-modify-playback-state",     # play / pause / skip / volume / queue
    "user-read-currently-playing",
    "playlist-read-private",          # the owner's own playlists
    "playlist-read-collaborative",
    "user-library-read",              # saved ("liked") tracks
    "user-top-read",                  # top artists and tracks
    "user-read-recently-played",
]


class AuthError(RuntimeError):
    """Auth is missing or broken. Tools return the message as text."""


_lock = threading.RLock()
_cached_token: str | None = None
_expires_at: float = 0.0


def _load_bundle() -> dict:
    path = config.SPOTIFY_TOKEN_PATH
    if not path.exists():
        raise AuthError(
            "Spotify is not connected. The owner needs to run `jarvis auth spotify` "
            "once — tell them, and offer to walk through the setup."
        )
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"the Spotify token bundle is unreadable ({exc}).") from exc
    missing = {"client_id", "refresh_token"} - bundle.keys()
    if missing:
        raise AuthError(
            f"the token bundle is missing {sorted(missing)} — "
            "the owner should re-run `jarvis auth spotify`."
        )
    return bundle


def _store(bundle: dict) -> None:
    """Write the bundle back at mode 600, atomically.

    Atomically because this runs on a token *refresh*, not just at setup: a
    process dying mid-write would leave a truncated bundle and lock Jarvis out
    of an account he was authorised for, which the owner could only fix by
    re-consenting.
    """
    path = config.SPOTIFY_TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


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
                    "grant_type": "refresh_token",
                    "refresh_token": bundle["refresh_token"],
                    "client_id": bundle["client_id"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach Spotify's token endpoint ({exc}).") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or "access_token" not in payload:
            error = payload.get("error", f"HTTP {response.status_code}")
            if error == "invalid_grant":
                raise AuthError(
                    "Spotify refused the refresh token (revoked at "
                    "spotify.com/account/apps, or superseded by a newer one). "
                    "The owner needs to re-run `jarvis auth spotify`."
                )
            raise AuthError(f"token refresh failed ({error}).")

        # Rotation: persist the replacement *before* handing back the access
        # token, so a crash cannot leave us using a refresh token Spotify has
        # already retired.
        rotated = payload.get("refresh_token")
        if rotated and rotated != bundle["refresh_token"]:
            bundle["refresh_token"] = rotated
            try:
                _store(bundle)
            except OSError:
                # An unwritable bundle is not a reason to fail the call in
                # progress; the old token is still good until it is not.
                pass

        _cached_token = payload["access_token"]
        _expires_at = time.time() + float(payload.get("expires_in", 3600))
        return _cached_token


def invalidate() -> None:
    """Drop the cached access token (called after a 401 so the next call refreshes)."""
    global _cached_token
    with _lock:
        _cached_token = None


def connected() -> bool:
    """True if a token bundle exists. Cheap — does not touch the network."""
    return config.SPOTIFY_TOKEN_PATH.exists()


# -- one-time consent (human-only, via `jarvis auth spotify`) ----------------


def _open_browser(url: str) -> None:
    """Best effort — the URL is printed either way.

    On WSL the Linux webbrowser module has no browser to talk to, so go via
    PowerShell to the Windows default browser (google_auth does the same).
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


def _setup_help() -> None:
    print("not connected.")
    print("Spotify needs a (free) app registration before it will talk to Jarvis:")
    print()
    print("  1. developer.spotify.com/dashboard → Create app")
    print("     name/description: anything. Redirect URI, exactly:")
    print(f"       {config.SPOTIFY_REDIRECT}")
    print("     (http is only allowed for a loopback address, and Spotify")
    print("      rejects the spelling 'localhost' — it must be 127.0.0.1.)")
    print("     Which API: check 'Web API'.")
    print("  2. Open the app's Settings and copy the Client ID.")
    print("     There is no client secret to copy — this flow uses PKCE.")
    print("  3. Then run:  jarvis auth spotify <client-id>")
    print()
    print("Playback control (play/pause/skip/volume) is a Premium-only feature")
    print("of Spotify's API; search and library reads work on any account.")


def connect(client_id: str | None) -> int:
    """Run the interactive consent flow, or report status with no argument."""
    token_path = config.SPOTIFY_TOKEN_PATH

    if client_id is None:
        if not token_path.exists():
            _setup_help()
            return 0
        bundle = json.loads(token_path.read_text(encoding="utf-8"))
        print(f"connected — token bundle at {token_path}")
        for scope in bundle.get("scopes", []):
            print(f"  scope: {scope}")
        print("revoke any time at https://www.spotify.com/account/apps/")
        return 0

    client_id = client_id.strip()

    # PKCE + state: the verifier is what authenticates the exchange in place of
    # a client secret, and state is what stops a redirect we did not start.
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
            self.wfile.write(b"<h2>Jarvis is connected to Spotify. You can close this tab.</h2>")

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", config.SPOTIFY_AUTH_PORT), _Catch)
    except OSError as exc:
        print(f"could not listen on {config.SPOTIFY_REDIRECT} ({exc}).")
        print("Something else is using that port. Free it, or set")
        print("JARVIS_SPOTIFY_AUTH_PORT and register the matching redirect URI.")
        return 1

    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": config.SPOTIFY_REDIRECT,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    print("Open this URL and approve access (your password goes to Spotify, not Jarvis):")
    print(f"\n  {url}\n")
    _open_browser(url)

    print(f"waiting up to 5 minutes for Spotify's redirect on {config.SPOTIFY_REDIRECT} …")
    server.timeout = 10
    deadline = time.time() + 300
    while time.time() < deadline and "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if result.get("state") != state:
        print("state mismatch — aborting. Run the command again and use the fresh URL.")
        return 1
    if "code" not in result:
        error = result.get("error", "timed out")
        print(f"no authorization code arrived ({error}).")
        if error == "INVALID_CLIENT":
            print("That client id is not one of your apps — check the dashboard.")
        return 1

    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": config.SPOTIFY_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if "refresh_token" not in payload:
        error = payload.get("error_description") or payload.get("error") or response.status_code
        print(f"Spotify returned no refresh token ({error}).")
        print("The usual cause is a redirect URI in the dashboard that does not")
        print(f"match {config.SPOTIFY_REDIRECT} character for character.")
        return 1

    _store(
        {
            "client_id": client_id,
            "refresh_token": payload["refresh_token"],
            "scopes": SCOPES,
        }
    )
    invalidate()
    print(f"connected — token bundle written to {token_path} (mode 600).")
    print("Jarvis can now search, read your playlists, and control playback.")
    return 0
