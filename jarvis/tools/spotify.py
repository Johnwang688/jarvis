"""Spotify tools: search, playlists, playback control, library.

Narrow and typed like every other integration (CLAUDE.md safety design), but
none of these is `dangerous=True`, and that is a judgement rather than an
oversight. The gate exists for actions that are outward-facing or hard to
undo — `gmail_send` puts words in the owner's name in someone else's inbox.
Starting a song is neither: it is audible, immediately obvious, and undone by
saying "stop". Asking permission per track would teach the owner to approve
without reading, which is the failure mode the gate is meant to prevent.

They are also deliberately absent from `workflows.SAFE_TOOLS`. A workflow runs
where nobody is watching, and music starting from an unattended thread is a
surprise in the owner's room rather than in a log. Attended tasks and goals do
get them, because someone is by definition reachable there.

Credentials: spotify_auth.access_token(). The token goes into an Authorization
header here and is never part of a tool result — the model only sees Spotify's
API responses. On a 401 the cached token is dropped and the call retried once.

Two API facts worth knowing before changing anything here, both verified
against Spotify's current documentation rather than remembered:

  * **Playback control is Premium-only.** Every `/me/player/*` write returns
    403 on a free account. `_fail` names that specifically, because the generic
    "403 Forbidden" reads as a Jarvis bug and sends people to the wrong place.

  * **Spotify's recommendation endpoints are gone for apps registered after
    2024-11-27** — `/recommendations`, `/related-artists`, `/audio-features`
    and the editorial-playlist endpoints all 403 for a new client id, which
    ours is. So "play something like X" is served by `spotify_artist_tracks`
    (that artist's top tracks) blended with the owner's own top and saved
    tracks, and it is described that way in the tool text rather than being
    passed off as Spotify's recommender.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

import httpx

from .. import config, spotify_auth
from . import tool

API = config.SPOTIFY_API

# Spotify's own device-poll window after we launch the desktop client. It has
# to register with Spotify Connect over the network before the Web API can see
# it, which is a cold start on the far side of two hops.
_LAUNCH_WAIT = 25.0
_LAUNCH_POLL = 1.5

_profile_cache: dict | None = None


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """One authed API call, retried once on 401."""
    for attempt in (1, 2):
        response = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {spotify_auth.access_token()}"},
            timeout=30,
            **kwargs,
        )
        if response.status_code == 401 and attempt == 1:
            spotify_auth.invalidate()
            continue
        return response
    return response  # unreachable; keeps type-checkers calm


def _json(response: httpx.Response) -> dict:
    """Body as a dict. 204 (Spotify's 'nothing to report') is an empty one."""
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json() or {}
    except ValueError:
        return {}


def _fail(response: httpx.Response) -> str:
    """Turn an API error into something the model can act on.

    Spotify's status codes are unusually load-bearing here: the same "403" is
    both "your account cannot do this at all" and "that device went away", and
    telling them apart is the difference between the owner upgrading their plan
    and the owner opening an app.
    """
    payload = _json(response)
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message", "")
        reason = error.get("reason", "")
    else:
        message = str(error or "")
        reason = ""

    if response.status_code == 403:
        if "premium" in f"{message} {reason}".lower():
            return (
                "Error: Spotify only allows playback control on Premium accounts, "
                "and this one is not Premium. Search, playlists and library reads "
                "still work — tell the owner that rather than retrying."
            )
        return (
            f"Error: Spotify refused this ({message or 'forbidden'}). If it is a "
            "recommendation or audio-features call, those endpoints were retired "
            "for new apps — use spotify_artist_tracks instead."
        )
    if response.status_code == 404 and reason == "NO_ACTIVE_DEVICE":
        return (
            "Error: Spotify has no device to play on. Open Spotify on a device "
            "and try again — spotify_play will offer to launch it here."
        )
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "a few")
        return f"Error: Spotify rate-limited this. Wait {retry} seconds and retry."
    return f"Error: Spotify API returned {response.status_code}: {message or response.text[:200]}"


def _profile() -> dict:
    """The owner's account, cached for the process.

    Two things come from it and nothing else does: `country`, which is the
    market every catalogue lookup is scoped to, and `product`, which is how
    spotify_status can say plainly whether playback control will work at all
    instead of letting the owner discover it as a 403.
    """
    global _profile_cache
    if _profile_cache is None:
        response = _request("GET", f"{API}/me")
        _profile_cache = _json(response) if response.status_code == 200 else {}
    return _profile_cache


def _market() -> str:
    return _profile().get("country") or "US"


def _ms(milliseconds: int | None) -> str:
    if not milliseconds:
        return "0:00"
    seconds = int(milliseconds) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _artists(item: dict) -> str:
    return ", ".join(a.get("name", "?") for a in item.get("artists", [])) or "?"


def _describe(item: dict) -> str:
    """One line for a track, album, artist or playlist search result."""
    kind = item.get("type", "?")
    name = item.get("name", "?")
    if kind == "track":
        return f"{name} — {_artists(item)} ({item.get('album', {}).get('name', '?')}) [{_ms(item.get('duration_ms'))}]"
    if kind == "album":
        return f"{name} — {_artists(item)} ({item.get('total_tracks', '?')} tracks)"
    if kind == "artist":
        return name
    if kind == "playlist":
        owner = item.get("owner", {}).get("display_name", "?")
        total = (item.get("tracks") or {}).get("total", "?")
        return f"{name} — by {owner} ({total} tracks)"
    return name


# -- devices ----------------------------------------------------------------


def _devices() -> tuple[list[dict], str | None]:
    """(devices, error). Never raises for an API failure."""
    response = _request("GET", f"{API}/me/player/devices")
    if response.status_code != 200:
        return [], _fail(response)
    return _json(response).get("devices", []), None


def _launch_app() -> str | None:
    """Start the Windows Spotify client. Returns an error string, or None.

    Confined the way DESKTOP_APPS is: the executable is `config.SPOTIFY_EXE`
    and nothing else. No tool here takes a path or a command line, so the model
    cannot turn "play something" into "run something" — which is the only
    reason a music tool is allowed to start a process at all.
    """
    exe = (config.SPOTIFY_EXE or "").strip()
    if not exe:
        return "no Spotify executable is configured (set JARVIS_SPOTIFY_EXE)"
    if not Path(exe).exists() and not shutil.which(exe):
        return f"the configured Spotify executable is not there ({exe})"
    try:
        subprocess.Popen(
            [exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"could not start Spotify ({exc})"
    return None


def _wait_for_device(deadline: float) -> list[dict]:
    while time.time() < deadline:
        devices, error = _devices()
        if devices or error:
            return devices
        time.sleep(_LAUNCH_POLL)
    return []


def _pick_device(name: str = "") -> tuple[dict | None, str]:
    """Choose where to play. Returns (device, note-for-the-owner).

    The note is not decoration: "playing on the desktop app because your phone
    was the only other option" is exactly the kind of thing that otherwise
    reads as Jarvis picking at random.
    """
    devices, error = _devices()
    if error:
        return None, error

    if not devices:
        launch_error = _launch_app()
        if launch_error:
            return None, (
                f"no Spotify device is available and {launch_error}. "
                "Open Spotify on any device and ask again."
            )
        devices = _wait_for_device(time.time() + _LAUNCH_WAIT)
        if not devices:
            return None, (
                "started Spotify here, but it has not registered with Spotify "
                "Connect yet. Give it a few seconds and ask again."
            )

    if name:
        wanted = name.strip().lower()
        for device in devices:
            if device.get("name", "").lower() == wanted:
                return device, ""
        for device in devices:
            if wanted in device.get("name", "").lower():
                return device, ""
        available = ", ".join(d.get("name", "?") for d in devices)
        return None, f"no device called {name!r}. Available: {available}."

    for device in devices:
        if device.get("is_active"):
            return device, ""
    for device in devices:
        if device.get("type") == "Computer":
            return device, f"nothing was playing, so I used {device.get('name', '?')}."
    return devices[0], f"nothing was playing, so I used {devices[0].get('name', '?')}."


# -- resolving what to play -------------------------------------------------


def _own_playlist(name: str) -> dict | None:
    """The owner's own playlist matching `name`, if there is one.

    Checked before the catalogue search on purpose: "play my running mix" means
    *the owner's* playlist, and a global search for those words returns someone
    else's playlist of the same name nearly every time.
    """
    wanted = name.strip().lower()
    if not wanted:
        return None
    url = f"{API}/me/playlists"
    for _ in range(4):  # up to 200 playlists; past that, name it exactly
        response = _request("GET", url, params={"limit": 50})
        if response.status_code != 200:
            return None
        payload = _json(response)
        items = [p for p in payload.get("items", []) if p]
        for playlist in items:
            if playlist.get("name", "").lower() == wanted:
                return playlist
        for playlist in items:
            if wanted in playlist.get("name", "").lower():
                return playlist
        url = payload.get("next")
        if not url:
            break
    return None


_KINDS = {"track", "album", "artist", "playlist"}


def _resolve(query: str, kind: str) -> tuple[dict | None, str]:
    """Turn free text into something playable. Returns (item, error)."""
    kind = (kind or "auto").strip().lower()
    if kind not in _KINDS | {"auto"}:
        return None, f"Error: kind must be one of auto, {', '.join(sorted(_KINDS))}."

    if kind in ("auto", "playlist"):
        playlist = _own_playlist(query)
        if playlist:
            return playlist, ""
        if kind == "playlist":
            # Fall through to the catalogue: a public playlist is still a
            # reasonable answer when the owner has none by that name.
            pass

    types = kind if kind != "auto" else "track,album,artist,playlist"
    response = _request(
        "GET",
        f"{API}/search",
        params={"q": query, "type": types, "limit": 5, "market": _market()},
    )
    if response.status_code != 200:
        return None, _fail(response)
    payload = _json(response)

    # Preference order for a bare "play X": a named track is the common ask,
    # then the artist (which plays their top tracks), then an album, then a
    # playlist. Only consulted for kind="auto" — an explicit kind wins.
    order = [f"{kind}s"] if kind != "auto" else ["tracks", "artists", "albums", "playlists"]
    for bucket in order:
        items = [i for i in (payload.get(bucket) or {}).get("items", []) or [] if i]
        if items:
            return items[0], ""
    return None, f"Error: nothing on Spotify matched {query!r}."


def _play_body(item: dict, uris: list[str] | None) -> dict:
    if uris:
        return {"uris": uris}
    if item.get("type") == "track":
        return {"uris": [item["uri"]]}
    return {"context_uri": item["uri"]}  # album, artist, playlist


# -- tools ------------------------------------------------------------------


@tool
def spotify_status() -> str:
    """What Spotify is playing right now, and which devices are available.

    Call this before assuming anything about playback state — including
    whether the account can be controlled at all.
    """
    try:
        profile = _profile()
        lines: list[str] = []
        if profile.get("product") and profile["product"] != "premium":
            lines.append(
                f"account: {profile['product']} — playback control needs Premium; "
                "search and library reads still work."
            )

        response = _request("GET", f"{API}/me/player")
        if response.status_code not in (200, 204):
            return _fail(response)
        state = _json(response)
        item = state.get("item") or {}
        if not state or not item:
            lines.append("Nothing is playing.")
        else:
            lines.append(
                f"{'Playing' if state.get('is_playing') else 'Paused'}: {_describe(item)}"
            )
            lines.append(
                f"  {_ms(state.get('progress_ms'))} / {_ms(item.get('duration_ms'))}"
                f" · shuffle {'on' if state.get('shuffle_state') else 'off'}"
                f" · repeat {state.get('repeat_state', 'off')}"
            )
            context = state.get("context") or {}
            if context.get("uri"):
                lines.append(f"  from: {context['uri']}")
            lines.append(f"  uri: {item.get('uri', '?')}")

        devices, error = _devices()
        if error:
            lines.append(error)
        elif not devices:
            lines.append("No devices are open. spotify_play can launch Spotify here.")
        else:
            lines.append("Devices:")
            for device in devices:
                mark = " (active)" if device.get("is_active") else ""
                volume = device.get("volume_percent")
                volume_text = f" · volume {volume}%" if volume is not None else ""
                lines.append(
                    f"  - {device.get('name', '?')} [{device.get('type', '?')}]{mark}{volume_text}"
                )
        return "\n".join(lines)
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_search(
    query: Annotated[str, "Search text, e.g. 'daft punk one more time', 'lofi beats'"],
    kind: Annotated[str, "track, album, artist, or playlist"] = "track",
    limit: Annotated[int, "How many results, 1-20"] = 8,
) -> str:
    """Search Spotify's catalogue. Returns names plus the URI to play.

    For the owner's *own* playlists use spotify_playlists — this searches all
    of Spotify, where someone else's playlist of the same name usually wins.
    """
    kind = (kind or "track").strip().lower().rstrip("s")
    if kind not in _KINDS:
        return f"Error: kind must be one of {', '.join(sorted(_KINDS))}."
    limit = max(1, min(int(limit), 20))
    try:
        response = _request(
            "GET",
            f"{API}/search",
            params={"q": query, "type": kind, "limit": limit, "market": _market()},
        )
        if response.status_code != 200:
            return _fail(response)
        items = [i for i in (_json(response).get(f"{kind}s") or {}).get("items", []) or [] if i]
        if not items:
            return f"Nothing matched {query!r}."
        return "\n".join(f"- {_describe(i)}\n  {i.get('uri', '?')}" for i in items)
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_playlists(
    limit: Annotated[int, "How many playlists, 1-50"] = 50,
) -> str:
    """List the owner's own playlists (saved and created), with URIs to play."""
    limit = max(1, min(int(limit), 50))
    try:
        response = _request("GET", f"{API}/me/playlists", params={"limit": limit})
        if response.status_code != 200:
            return _fail(response)
        items = [p for p in _json(response).get("items", []) or [] if p]
        if not items:
            return "No playlists on this account."
        return "\n".join(
            f"- {p.get('name', '?')} ({(p.get('tracks') or {}).get('total', '?')} tracks)"
            f"\n  {p.get('uri', '?')}"
            for p in items
        )
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_play(
    query: Annotated[
        str,
        "What to play in plain words, e.g. 'my running playlist', 'Bohemian Rhapsody'. "
        "Omit to resume whatever is paused.",
    ] = "",
    uri: Annotated[str, "Exact spotify: URI from a search. Beats query when given."] = "",
    uris: Annotated[list[str], "Several exact track URIs to play in order."] = [],
    kind: Annotated[str, "Narrow a query: track, album, artist, playlist, or auto"] = "auto",
    device: Annotated[str, "Device name to play on; omit for the active one"] = "",
) -> str:
    """Start playback: a playlist, album, artist, track, or a resume.

    A bare `query` is resolved for you — the owner's own playlists are checked
    before the global catalogue, so "play my focus mix" finds theirs. The reply
    states exactly what was chosen, so a wrong guess is visible immediately.

    If no Spotify device is open, this launches the desktop app and waits for
    it. Needs Spotify Premium, like every playback control.
    """
    try:
        target, note = _pick_device(device)
        if target is None:
            return f"Error: {note}"

        item: dict = {}
        picked = ""
        chosen_uris = [u for u in (uris or []) if u]
        if uri:
            item = {"uri": uri, "type": uri.split(":")[1] if ":" in uri else "track"}
            picked = uri
        elif chosen_uris:
            picked = f"{len(chosen_uris)} track(s)"
        elif query:
            item, error = _resolve(query, kind)
            if item is None:
                return error
            picked = _describe(item)
        # else: no target at all — a bare resume.

        params = {}
        if not target.get("is_active"):
            params["device_id"] = target.get("id")

        body = _play_body(item, chosen_uris) if (item or chosen_uris) else {}
        response = _request("PUT", f"{API}/me/player/play", params=params, json=body)
        if response.status_code not in (200, 202, 204):
            return _fail(response)

        where = f" on {target.get('name', '?')}"
        if not picked:
            return f"Resumed{where}." + (f" {note}" if note else "")
        return f"Playing {picked}{where}." + (f" {note}" if note else "")
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_pause() -> str:
    """Pause playback. spotify_play with no arguments resumes it."""
    try:
        response = _request("PUT", f"{API}/me/player/pause")
        if response.status_code not in (200, 202, 204):
            return _fail(response)
        return "Paused."
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_skip(
    direction: Annotated[str, "'next' or 'previous'"] = "next",
) -> str:
    """Skip to the next or previous track."""
    direction = (direction or "next").strip().lower()
    if direction not in ("next", "previous"):
        return "Error: direction must be 'next' or 'previous'."
    try:
        response = _request("POST", f"{API}/me/player/{direction}")
        if response.status_code not in (200, 202, 204):
            return _fail(response)
        # Spotify applies the skip asynchronously; a read straight back is
        # racy, so report the action and let spotify_status answer "what now".
        return f"Skipped to the {direction} track."
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_queue(
    query: Annotated[str, "A track to queue in plain words. Omit to show the queue."] = "",
    uri: Annotated[str, "Exact spotify:track: URI to queue. Beats query."] = "",
) -> str:
    """Add a track to the queue, or show what is queued up next."""
    try:
        if not query and not uri:
            response = _request("GET", f"{API}/me/player/queue")
            if response.status_code != 200:
                return _fail(response)
            payload = _json(response)
            queue = [i for i in payload.get("queue", []) or [] if i][:15]
            if not queue:
                return "The queue is empty."
            current = payload.get("currently_playing") or {}
            lines = [f"Now: {_describe(current)}"] if current else []
            lines += [f"{n}. {_describe(i)}" for n, i in enumerate(queue, 1)]
            return "\n".join(lines)

        picked = uri
        if not uri:
            item, error = _resolve(query, "track")
            if item is None:
                return error
            picked = item["uri"]
            label = _describe(item)
        else:
            label = uri

        response = _request("POST", f"{API}/me/player/queue", params={"uri": picked})
        if response.status_code not in (200, 202, 204):
            return _fail(response)
        return f"Queued {label}."
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_settings(
    volume: Annotated[int, "Volume 0-100"] | None = None,
    shuffle: Annotated[bool, "Shuffle on or off"] | None = None,
    repeat: Annotated[str, "'off', 'track' (this song), or 'context' (this playlist)"] = "",
) -> str:
    """Adjust how playback behaves: volume, shuffle, repeat. Set any or all."""
    done: list[str] = []
    try:
        if volume is not None:
            level = max(0, min(int(volume), 100))
            response = _request(
                "PUT", f"{API}/me/player/volume", params={"volume_percent": level}
            )
            if response.status_code not in (200, 202, 204):
                return _fail(response)
            done.append(f"volume {level}%")

        if shuffle is not None:
            response = _request(
                "PUT", f"{API}/me/player/shuffle", params={"state": bool(shuffle)}
            )
            if response.status_code not in (200, 202, 204):
                return _fail(response)
            done.append(f"shuffle {'on' if shuffle else 'off'}")

        if repeat:
            mode = repeat.strip().lower()
            if mode not in ("off", "track", "context"):
                return "Error: repeat must be 'off', 'track', or 'context'."
            response = _request("PUT", f"{API}/me/player/repeat", params={"state": mode})
            if response.status_code not in (200, 202, 204):
                return _fail(response)
            done.append(f"repeat {mode}")

        if not done:
            return "Nothing to change — pass volume, shuffle, or repeat."
        return "Set " + ", ".join(done) + "."
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_library(
    kind: Annotated[str, "'saved', 'top_tracks', 'top_artists', or 'recent'"] = "saved",
    limit: Annotated[int, "How many, 1-50"] = 20,
) -> str:
    """Read the owner's listening history and saved music, with URIs.

    'saved' is their liked songs, 'top_tracks'/'top_artists' what they play
    most, 'recent' what they played last.
    """
    kind = (kind or "saved").strip().lower()
    limit = max(1, min(int(limit), 50))
    endpoints = {
        "saved": (f"{API}/me/tracks", "track"),
        "top_tracks": (f"{API}/me/top/tracks", None),
        "top_artists": (f"{API}/me/top/artists", None),
        "recent": (f"{API}/me/player/recently-played", "track"),
    }
    if kind not in endpoints:
        return f"Error: kind must be one of {', '.join(endpoints)}."
    url, unwrap = endpoints[kind]
    try:
        response = _request("GET", url, params={"limit": limit})
        if response.status_code != 200:
            return _fail(response)
        rows = [i for i in _json(response).get("items", []) or [] if i]
        items = [row.get(unwrap) or {} for row in rows] if unwrap else rows
        items = [i for i in items if i]
        if not items:
            return f"Nothing under {kind}."
        return "\n".join(f"- {_describe(i)}\n  {i.get('uri', '?')}" for i in items)
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def spotify_artist_tracks(
    artist: Annotated[str, "Artist name, e.g. 'Daft Punk'"],
    limit: Annotated[int, "How many tracks, 1-10"] = 10,
) -> str:
    """An artist's most-played tracks, with URIs — the "more like this" tool.

    Spotify retired its recommendation endpoints for apps registered after
    November 2024, and ours is one, so there is no recommender to call. For
    "play something like X", combine these with spotify_library('top_tracks')
    and pass the URIs to spotify_play — and describe it to the owner as what it
    is, a mix built from that artist plus their own favourites.
    """
    limit = max(1, min(int(limit), 10))
    try:
        found, error = _resolve(artist, "artist")
        if found is None:
            return error
        response = _request(
            "GET",
            f"{API}/artists/{found['id']}/top-tracks",
            params={"market": _market()},
        )
        if response.status_code != 200:
            return _fail(response)
        tracks = [t for t in _json(response).get("tracks", []) or [] if t][:limit]
        if not tracks:
            return f"No top tracks listed for {found.get('name', artist)}."
        head = f"Top tracks for {found.get('name', artist)}:"
        return head + "\n" + "\n".join(f"- {_describe(t)}\n  {t['uri']}" for t in tracks)
    except (spotify_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


# The deferred half of this module (see tools.ToolGroup).
#
# `spotify_play` and `spotify_status` are core because they answer the common
# ask outright — "play my running playlist" should not cost a round trip spent
# expanding a toolset, least of all on the voice surface. The other eight are
# for a conversation that has actually turned into one about music, and cost
# nothing until then.
#
# Availability is "has the owner connected an account", so on a machine that
# never ran `jarvis auth spotify` this whole module is invisible: no schemas,
# and no pointer in the working context advertising a service that would only
# answer with a setup error.
from . import register_group  # noqa: E402

register_group(
    name="spotify",
    summary="Spotify: search, playlists, library, queue, volume, shuffle/repeat",
    core=("spotify_play", "spotify_status"),
    extra=(
        "spotify_search",
        "spotify_playlists",
        "spotify_pause",
        "spotify_skip",
        "spotify_queue",
        "spotify_settings",
        "spotify_library",
        "spotify_artist_tracks",
    ),
    available=spotify_auth.connected,
)
