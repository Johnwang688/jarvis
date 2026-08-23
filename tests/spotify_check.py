"""Synthetic checks for the Spotify integration. Free — no network, no API.

What must hold:
  1. auth: a missing bundle is a clear error, refresh tokens exchange and
     cache, and a **rotated** refresh token is persisted (PKCE hands back a
     new one and retires the old — not writing it locks the owner out an hour
     later, somewhere else)
  2. tools: every call speaks the Web API correctly against a fake transport,
     the Authorization header carries the token, a 401 refreshes and retries
  3. resolution: "play my X" prefers the owner's own playlist over a global
     search, and a track/artist/album/playlist each become the right play body
  4. devices: no device launches the desktop app rather than erroring, and a
     Premium refusal says so instead of reading as a Jarvis bug
  5. groups: the tools cost nothing when unconnected, only the core two when
     connected, and load_tools expands a *running* agent — while an agent with
     an explicit toolset cannot widen itself
  6. secrets: spotify_token.json is unreadable through every layer and its
     values are scrubbed from tool output

Run:  .venv/bin/python tests/spotify_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, spotify_auth, tools, workflows
from jarvis.tools import secrets, spotify

REFRESH_TOKEN = "AQC-refresh-token-value-not-for-the-model-abc123"
ROTATED_TOKEN = "AQC-rotated-refresh-token-def456-also-secret"
ACCESS_TOKEN = "BQA-fake-access-token"
CLIENT_ID = "0123456789abcdef0123456789abcdef"


def _bundle(path: Path, refresh: str = REFRESH_TOKEN) -> None:
    path.write_text(
        json.dumps({"client_id": CLIENT_ID, "refresh_token": refresh, "scopes": spotify_auth.SCOPES}),
        encoding="utf-8",
    )


def _response(status: int, payload: dict | None = None, headers: dict | None = None):
    body = json.dumps(payload if payload is not None else {})
    return types.SimpleNamespace(
        status_code=status,
        json=lambda: (payload if payload is not None else {}),
        text=body,
        content=b"" if status == 204 else body.encode(),
        headers=headers or {},
    )


def _reset() -> None:
    spotify_auth._cached_token = None
    spotify_auth._expires_at = 0.0
    spotify._profile_cache = None


# --- 1. auth ---------------------------------------------------------------


def auth_checks(token_path: Path) -> None:
    _reset()
    config.SPOTIFY_TOKEN_PATH = token_path.parent / "nope" / "spotify_token.json"
    assert not spotify_auth.connected()
    try:
        spotify_auth.access_token()
        raise AssertionError("a missing bundle must raise")
    except spotify_auth.AuthError as exc:
        assert "jarvis auth spotify" in str(exc), exc

    config.SPOTIFY_TOKEN_PATH = token_path
    _bundle(token_path)
    assert spotify_auth.connected()

    posts: list[dict] = []
    real_post = httpx.post

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append(data)
        return _response(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})

    httpx.post = fake_post
    try:
        assert spotify_auth.access_token() == ACCESS_TOKEN
        assert spotify_auth.access_token() == ACCESS_TOKEN  # cached
        assert len(posts) == 1, "the second call must not re-exchange"
        assert posts[0]["grant_type"] == "refresh_token"
        assert posts[0]["refresh_token"] == REFRESH_TOKEN
        assert posts[0]["client_id"] == CLIENT_ID
        assert "client_secret" not in posts[0], "PKCE carries no client secret"
        print("ok  auth: refresh exchanged, cached, and secretless (PKCE)")

        # Rotation. Spotify hands back a *new* refresh token and retires the
        # old one; a bundle still holding the old one is an authorised account
        # that stops working at the next refresh, an hour later.
        _reset()
        httpx.post = lambda url, data=None, headers=None, timeout=None: _response(
            200,
            {
                "access_token": ACCESS_TOKEN,
                "expires_in": 3600,
                "refresh_token": ROTATED_TOKEN,
            },
        )
        spotify_auth.access_token()
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        assert saved["refresh_token"] == ROTATED_TOKEN, saved
        assert saved["client_id"] == CLIENT_ID
        assert token_path.stat().st_mode & 0o777 == 0o600, "the bundle must stay 600"
        print("ok  auth: a rotated refresh token is persisted at mode 600")

        _reset()
        _bundle(token_path)
        httpx.post = lambda url, data=None, headers=None, timeout=None: _response(
            400, {"error": "invalid_grant"}
        )
        try:
            spotify_auth.access_token()
            raise AssertionError("invalid_grant must raise")
        except spotify_auth.AuthError as exc:
            assert "re-run `jarvis auth spotify`" in str(exc), exc
        print("ok  auth: invalid_grant explains itself")
    finally:
        httpx.post = real_post


# --- 2/3/4. tools, resolution, devices -------------------------------------


class _Api:
    """A fake Spotify, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.playlists = [
            {"name": "Deep Focus", "uri": "spotify:playlist:own1", "tracks": {"total": 42}}
        ]
        self.devices = [
            {"id": "d1", "name": "DESKTOP-JW", "type": "Computer", "is_active": True,
             "volume_percent": 60}
        ]
        self.play_bodies: list[dict] = []

    def __call__(self, method, url, headers=None, timeout=None, params=None, json=None):
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}", headers
        self.calls.append((method, url, params or {}))
        tail = url.split("/v1", 1)[1]

        if tail == "/me":
            return _response(200, {"country": "US", "product": "premium"})
        if tail == "/me/player/devices":
            return _response(200, {"devices": self.devices})
        if tail == "/me/playlists":
            return _response(200, {"items": self.playlists, "next": None})
        if tail == "/me/player" and method == "GET":
            return _response(200, {
                "is_playing": True,
                "progress_ms": 61000,
                "shuffle_state": False,
                "repeat_state": "off",
                "context": {"uri": "spotify:playlist:own1"},
                "item": {
                    "type": "track", "name": "Digital Love", "uri": "spotify:track:t1",
                    "duration_ms": 301000, "artists": [{"name": "Daft Punk"}],
                    "album": {"name": "Discovery"},
                },
            })
        if tail == "/search":
            kinds = params["type"].split(",")
            out = {}
            if "track" in kinds:
                out["tracks"] = {"items": [{
                    "type": "track", "name": "One More Time", "uri": "spotify:track:t9",
                    "duration_ms": 320000, "artists": [{"name": "Daft Punk"}],
                    "album": {"name": "Discovery"}}]}
            if "artist" in kinds:
                out["artists"] = {"items": [{
                    "type": "artist", "name": "Daft Punk", "id": "a1",
                    "uri": "spotify:artist:a1"}]}
            if "playlist" in kinds:
                out["playlists"] = {"items": [{
                    "type": "playlist", "name": "Deep Focus", "uri": "spotify:playlist:someone",
                    "owner": {"display_name": "Someone Else"}, "tracks": {"total": 10}}]}
            return _response(200, out)
        if tail == "/me/player/play":
            self.play_bodies.append({"body": json or {}, "params": params or {}})
            return _response(204)
        if tail in ("/me/player/pause", "/me/player/next", "/me/player/previous",
                    "/me/player/volume", "/me/player/shuffle", "/me/player/repeat",
                    "/me/player/queue"):
            return _response(204)
        if tail == "/me/tracks":
            return _response(200, {"items": [{"track": {
                "type": "track", "name": "Veridis Quo", "uri": "spotify:track:t3",
                "duration_ms": 344000, "artists": [{"name": "Daft Punk"}],
                "album": {"name": "Discovery"}}}]})
        if tail == "/artists/a1/top-tracks":
            return _response(200, {"tracks": [{
                "type": "track", "name": "Around the World", "uri": "spotify:track:t4",
                "duration_ms": 429000, "artists": [{"name": "Daft Punk"}],
                "album": {"name": "Homework"}}]})
        raise AssertionError(f"unexpected call: {method} {url}")


def tool_checks(token_path: Path) -> None:
    _reset()
    config.SPOTIFY_TOKEN_PATH = token_path
    _bundle(token_path)

    real_post, real_request = httpx.post, httpx.request
    httpx.post = lambda *a, **k: _response(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})
    api = _Api()
    httpx.request = api
    try:
        out = tools.dispatch("spotify_status", "{}").text
        assert "Playing: Digital Love — Daft Punk" in out, out
        assert "1:01 / 5:01" in out, out
        assert "DESKTOP-JW [Computer] (active)" in out, out
        print("ok  status: now-playing, progress, and devices")

        out = tools.dispatch("spotify_search", json.dumps({"query": "daft punk"})).text
        assert "One More Time" in out and "spotify:track:t9" in out, out

        out = tools.dispatch("spotify_playlists", "{}").text
        assert "Deep Focus (42 tracks)" in out and "spotify:playlist:own1" in out, out

        # Resolution: the owner's own playlist beats the identically-named one
        # in the global catalogue. This is the whole reason _own_playlist runs
        # first — "play my deep focus" must not start a stranger's playlist.
        out = tools.dispatch("spotify_play", json.dumps({"query": "deep focus"})).text
        assert "spotify:playlist:own1" == api.play_bodies[-1]["body"]["context_uri"], api.play_bodies
        assert "Deep Focus" in out and "DESKTOP-JW" in out, out
        print("ok  resolution: the owner's own playlist wins over the catalogue")

        # A track plays as `uris`; an artist/album/playlist as `context_uri`.
        tools.dispatch("spotify_play", json.dumps({"query": "one more time"}))
        assert api.play_bodies[-1]["body"] == {"uris": ["spotify:track:t9"]}, api.play_bodies[-1]
        tools.dispatch("spotify_play", json.dumps({"query": "daft punk", "kind": "artist"}))
        assert api.play_bodies[-1]["body"] == {"context_uri": "spotify:artist:a1"}
        tools.dispatch("spotify_play", json.dumps({"uris": ["spotify:track:x", "spotify:track:y"]}))
        assert api.play_bodies[-1]["body"] == {"uris": ["spotify:track:x", "spotify:track:y"]}
        out = tools.dispatch("spotify_play", "{}").text
        assert api.play_bodies[-1]["body"] == {}, "a bare call is a resume, not a restart"
        assert out.startswith("Resumed"), out
        # The active device needs no transfer, so no device_id rides along.
        assert api.play_bodies[-1]["params"] == {}, api.play_bodies[-1]
        print("ok  play: track/artist/uris/resume each build the right body")

        assert "Paused" in tools.dispatch("spotify_pause", "{}").text
        assert "next" in tools.dispatch("spotify_skip", "{}").text
        assert "Error" in tools.dispatch("spotify_skip", json.dumps({"direction": "sideways"})).text

        out = tools.dispatch("spotify_settings", json.dumps(
            {"volume": 300, "shuffle": True, "repeat": "context"})).text
        assert "volume 100%" in out and "shuffle on" in out and "repeat context" in out, out
        volume_call = [c for c in api.calls if c[1].endswith("/volume")][-1]
        assert volume_call[2]["volume_percent"] == 100, "volume must clamp, not 400"
        assert "Error" in tools.dispatch("spotify_settings", json.dumps({"repeat": "sometimes"})).text
        assert "Nothing to change" in tools.dispatch("spotify_settings", "{}").text
        print("ok  transport: pause/skip/volume/shuffle/repeat, clamped and validated")

        out = tools.dispatch("spotify_queue", json.dumps({"query": "one more time"})).text
        assert "Queued" in out and "One More Time" in out, out
        queued = [c for c in api.calls if c[1].endswith("/player/queue")][-1]
        assert queued[2]["uri"] == "spotify:track:t9", queued

        out = tools.dispatch("spotify_library", json.dumps({"kind": "saved"})).text
        assert "Veridis Quo" in out and "spotify:track:t3" in out, out
        assert "Error" in tools.dispatch("spotify_library", json.dumps({"kind": "wat"})).text

        out = tools.dispatch("spotify_artist_tracks", json.dumps({"artist": "daft punk"})).text
        assert "Around the World" in out and "spotify:track:t4" in out, out
        # Catalogue lookups are scoped to the account's market, which is read
        # from /me rather than guessed.
        top = [c for c in api.calls if "top-tracks" in c[1]][-1]
        assert top[2]["market"] == "US", top
        print("ok  queue / library / artist tracks, scoped to the account market")

        # A 401 mid-conversation must self-heal, not surface to the owner.
        seen = {"n": 0}
        def flaky(method, url, headers=None, timeout=None, params=None, json=None):
            seen["n"] += 1
            if seen["n"] == 1:
                return _response(401, {"error": {"status": 401, "message": "expired"}})
            return api(method, url, headers=headers, timeout=timeout, params=params, json=json)
        httpx.request = flaky
        _reset()
        assert "Deep Focus" in tools.dispatch("spotify_playlists", "{}").text
        assert seen["n"] == 2, "a 401 must refresh and retry exactly once"
        print("ok  a 401 drops the cached token and retries once")
    finally:
        httpx.post, httpx.request = real_post, real_request


def device_checks(token_path: Path) -> None:
    _reset()
    config.SPOTIFY_TOKEN_PATH = token_path
    _bundle(token_path)
    real_post, real_request = httpx.post, httpx.request
    httpx.post = lambda *a, **k: _response(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})
    api = _Api()
    try:
        # Premium is what playback control actually needs, and a bare "403
        # Forbidden" reads as a Jarvis bug and sends the owner to the wrong
        # place. It has to name the plan.
        def refuses(method, url, headers=None, timeout=None, params=None, json=None):
            if url.endswith("/me/player/devices"):
                return _response(200, {"devices": api.devices})
            if url.endswith("/me"):
                return _response(200, {"country": "US", "product": "free"})
            return _response(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED",
                                             "message": "Player command failed: Premium required"}})
        httpx.request = refuses
        out = tools.dispatch("spotify_play", json.dumps({"uri": "spotify:track:t1"})).text
        assert "Premium" in out and "still work" in out, out
        print("ok  a Premium refusal names the plan, not the HTTP code")

        # No device at all: launch the desktop app rather than erroring at the
        # owner. The launcher is stubbed — a test must not open Spotify.
        api.devices = []
        launched = {"n": 0}
        real_launch, real_wait = spotify._launch_app, spotify._wait_for_device
        spotify._launch_app = lambda: (launched.__setitem__("n", launched["n"] + 1), None)[1]
        spotify._wait_for_device = lambda deadline: [
            {"id": "d2", "name": "DESKTOP-JW", "type": "Computer", "is_active": False}
        ]
        httpx.request = api
        _reset()
        try:
            out = tools.dispatch("spotify_play", json.dumps({"query": "one more time"})).text
            assert launched["n"] == 1, "no device must launch the app"
            assert "DESKTOP-JW" in out, out
            # An inactive device has to be named explicitly or the play lands
            # nowhere — this is the transfer that makes a cold start work.
            assert api.play_bodies[-1]["params"] == {"device_id": "d2"}, api.play_bodies[-1]
            print("ok  no device: Spotify is launched and playback transferred to it")

            spotify._launch_app = lambda: "the configured Spotify executable is not there"
            spotify._wait_for_device = lambda deadline: []
            out = tools.dispatch("spotify_play", json.dumps({"query": "x"})).text
            assert "Error" in out and "not there" in out, out
            print("ok  an unlaunchable player degrades to a clear message")
        finally:
            spotify._launch_app, spotify._wait_for_device = real_launch, real_wait

        # The launcher takes no argument from the model — the executable is
        # config, which is what stops "play something" becoming "run something".
        import inspect
        assert not inspect.signature(spotify._launch_app).parameters, "launch takes no input"
        for name in [n for n in tools.REGISTRY if n.startswith("spotify_")]:
            props = tools.REGISTRY[name].schema["properties"]
            assert not any(k in props for k in ("path", "command", "exe")), name
        print("ok  no Spotify tool accepts a path or command (structural confinement)")
    finally:
        httpx.post, httpx.request = real_post, real_request


# --- 5. tool groups --------------------------------------------------------


def group_checks(token_path: Path) -> None:
    from jarvis import agent as agent_mod

    group = tools.GROUPS["spotify"]
    assert set(group.all_names()) == {n for n in tools.REGISTRY if n.startswith("spotify_")}

    config.SPOTIFY_TOKEN_PATH = token_path.parent / "gone" / "spotify_token.json"
    names = tools.default_names()
    assert not [n for n in names if n.startswith("spotify_")], "unconnected must cost nothing"
    assert not tools.loadable(set(names)), "and must not even advertise itself"
    blank = agent_mod.Agent()
    assert "spotify" not in blank._groups_block()
    print("ok  groups: an unconnected service costs no schema and no pointer")

    config.SPOTIFY_TOKEN_PATH = token_path
    _bundle(token_path)
    names = tools.default_names()
    assert sorted(n for n in names if n.startswith("spotify_")) == [
        "spotify_play",
        "spotify_status",
    ], "connected costs the two core tools and nothing else"
    assert "spotify_search" not in names, "the deferred half must stay out"

    live = agent_mod.Agent()
    assert "load_tools with the name" in live._groups_block()
    before = {s["function"]["name"] for s in live.tool_specs}
    assert "spotify_play" in before and "spotify_search" not in before

    # The expansion path, as the agent actually runs it: bind the per-run
    # state, call the tool, sync. Failing here means load_tools reports
    # success and the tools never arrive.
    from jarvis import runtime
    runtime.bind(loaded_groups=live.loaded_groups, tool_names=before)
    out = tools.dispatch("load_tools", json.dumps({"group": "spotify"})).text
    assert "Loaded" in out and "spotify_search" in out, out
    live._sync_tools()
    after = {s["function"]["name"] for s in live.tool_specs}
    assert set(group.all_names()) <= after, sorted(after)
    assert runtime.parent_tools() >= set(group.all_names()), "children must inherit the expansion"
    assert not live._groups_block(), "a loaded group stops being advertised"
    assert "already loaded" in tools.dispatch("load_tools", json.dumps({"group": "spotify"})).text
    print("ok  groups: load_tools expands a running agent, once, and rebinds children")

    # An agent handed an explicit toolset must not be able to widen it — the
    # same rule that intersects a sub-agent's tools with its parent's.
    narrow = agent_mod.Agent(tool_names=["read_file", "load_tools"])
    assert not tools.loadable({"read_file", "load_tools"}), "no core tools = not loadable"
    assert not narrow._groups_block()
    assert "Error" in tools.dispatch("load_tools", json.dumps({"group": "nope"})).text

    # A deferred name is not an unknown name, and saying so is what stops the
    # model inventing a workaround for a tool that is right there. Note this is
    # a correctness guard, not a boundary: it fails *open* when no agent context
    # is bound, because a direct call from a script is not an agent reaching
    # past its toolset.
    runtime.bind(loaded_groups=set(), tool_names={"read_file"})
    hint = tools.dispatch("spotify_search", json.dumps({"query": "x"})).text
    assert "load_tools('spotify')" in hint, hint
    assert "not loaded" in hint, hint
    unknown = tools.dispatch("spotify_teleport", "{}").text
    assert "no tool named" in unknown and "spotify_search" not in unknown, unknown
    print("ok  groups: an explicit toolset cannot widen itself; deferred ≠ unknown")

    assert not [t for t in workflows.SAFE_TOOLS if t.startswith("spotify_")], \
        "an unattended workflow must not start music in the owner's room"
    assert "load_tools" not in tools.PARALLEL_SAFE
    print("ok  groups: absent from workflow tools; load_tools is not parallel-safe")


# --- 6. secrets ------------------------------------------------------------


def secrets_checks(token_path: Path) -> None:
    config.SPOTIFY_TOKEN_PATH = token_path
    _bundle(token_path)
    assert secrets.is_protected("spotify_token.json")
    assert secrets.is_protected("/home/x/.config/jarvis/spotify_token.json")
    for command in ("cat spotify_token.json", "cat ~/.config/jarvis/spotify_token.*",
                    "sh -c 'cat spotify_token.json'"):
        assert secrets.protected_in_command(command), command

    out = tools.dispatch("read_file", json.dumps({"path": str(token_path)}))
    assert "protected" in out.text, out.text

    scrubbed = secrets.scrub(f"leaked {REFRESH_TOKEN} here")
    assert REFRESH_TOKEN not in scrubbed and secrets.REDACTED in scrubbed, scrubbed
    print("ok  secrets: the bundle is refused by name/glob/read_file, values scrubbed")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        token_path = Path(tmp) / "spotify_token.json"
        original = config.SPOTIFY_TOKEN_PATH
        try:
            auth_checks(token_path)
            tool_checks(token_path)
            device_checks(token_path)
            group_checks(token_path)
            secrets_checks(token_path)
        finally:
            config.SPOTIFY_TOKEN_PATH = original
            _reset()
    print("\nall spotify checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
