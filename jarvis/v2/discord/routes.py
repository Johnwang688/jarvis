"""The HUD's channel routes (B1), mounted like PR #4's `projects.route`.

* `GET /projects/{id}/discord` → `{channel_id, origin, name, category, state,
  missing, checked_at, rename_pending}`. `state` is one of `linked_ok`,
  `unlinked`, `not_found`, `no_access`, `wrong_guild`, `missing_permissions`,
  `folder_missing`, `unconfigured`, `unreachable`. Cached 60 s by the linker;
  each Discord read is held to 5 s and never sleeps out a rate limit.
* `POST /projects/{id}/discord` `{action: create | link (channel_id) |
  unlink}` — **owner-only** (`projects.owner_only`: the HUD's own listener and
  Origin). An owner action is the owner's permission (D4), so it acts at once.
* `POST /discord/backfill` — owner-only: a channel for every unlinked, live
  project whose folder exists, one create a second. The CLI cannot pass the
  owner check, which is why this is a HUD button and not a command.

No tool reaches these routes (the forbidden `discord_` prefix, and nothing in
the tool registry calls the daemon's HTTP API for them); a shell command that
tried would go through the permission gate like any other.
"""
from __future__ import annotations

from .linker import LinkError

ACTIONS = ("create", "link", "unlink")


def _linker(daemon):
    surface = getattr(daemon, "discord", None)
    return getattr(surface, "linker", None) if surface is not None else None


def _offline_view(daemon, project_id) -> dict:
    """No Discord surface on this daemon: say whether that is setup or the
    surface, without a network call."""
    from . import guild
    project = daemon.require(daemon.stores.projects, project_id)
    configured = guild.load() is not None
    return {"channel_id": project.discord_channel_id,
            "origin": project.discord_channel_origin if project.discord_channel_id else None,
            "name": None, "category": None,
            "state": "unreachable" if configured else "unconfigured",
            "missing": [], "checked_at": None, "rename_pending": False}


def _call(fn, *args, **kwargs):
    from ..daemon import APIError
    try:
        return fn(*args, **kwargs)
    except LinkError as exc:
        raise APIError(exc.status, str(exc)) from None


def route(handler, daemon, parts, query):
    """The channel routes, or None for anything else."""
    from ..daemon import APIError, _object
    from ..projects import owner_only
    method = handler.command
    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "discord":
        project_id = parts[1]
        if method == "GET":
            _object(query, ("refresh",))
            linker = _linker(daemon)
            if linker is None:
                return 200, _offline_view(daemon, project_id)
            return 200, _call(linker.view, project_id, refresh=query.get("refresh") == "1")
        if method == "POST":
            owner_only(handler, daemon)
            _object(query, ())
            body = _object(handler._body(), ("action", "channel_id"), ("action",))
            action = body["action"]
            if action not in ACTIONS:
                raise APIError(400, "action must be create, link or unlink")
            if action == "link":
                if "channel_id" not in body:
                    raise APIError(400, "missing fields: channel_id")
            elif "channel_id" in body:
                raise APIError(400, f"{action} takes no channel_id")
            daemon.require(daemon.stores.projects, project_id)
            linker = _linker(daemon)
            if linker is None:
                raise APIError(409, "Discord is not running on this daemon")
            if action == "create":
                return 200, _call(linker.create, project_id)
            if action == "link":
                channel = body["channel_id"]
                return 200, _call(linker.link, project_id,
                                  channel.strip() if isinstance(channel, str) else channel)
            return 200, _call(linker.unlink, project_id)
    if parts == ["discord", "backfill"] and method == "POST":
        owner_only(handler, daemon)
        _object(query, ())
        _object(handler._body(), ())
        linker = _linker(daemon)
        if linker is None:
            raise APIError(409, "Discord is not running on this daemon")
        return 200, _call(linker.backfill)
    return None
