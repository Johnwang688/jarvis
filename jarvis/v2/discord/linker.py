"""Projects and their Discord channels (plan §2–§3, decisions D3, D4, O1, O7).

**Every channel links to a project whose folder exists.** A link needs
`Path(project.root).is_dir()` and a project that is not archived; the Inbox is
linked to **#ungrouped** by the daemon itself and nothing else can move it —
only re-running `jarvis auth discord-guild` does.

**Who may change a channel (D4).** The owner's own action counts as
permission: the owner-only HUD routes (`discord/routes.py`), a `PATCH
/projects` rename that passed `projects.owner_only` (`by: "owner"` on
`project_updated`), archiving, restoring or deleting a project in the HUD.
Anything Jarvis starts on its own — a rename after an `api` PATCH, a crowding
archive (D3, O7: 45 of 50 channels, projects idle 30 days), an overflow
"Jarvis Archive 2" — is asked through the approval broker as
`ApprovalRequest(tool="discord_channel", origin="Jarvis housekeeping",
allowlistable=False)`: Approve or Deny, never Always, and a deny or a timeout
does nothing. Those asks run on their own worker, one at a time, so a question
the owner has not answered never holds up the owner's own renames; they are
kept in `linker.json` and asked again after a restart.

**Only a sanctioned name is ever applied** (review fix 1). A channel name is
written only when the owner's own action or an approval chose it, and the
last such name is kept per project (`sanctioned`). A pending rename retries
*its own* name and is dropped the moment the project's name moves on — the
newer change then takes its own owner/api path. A restore that would apply a
name nobody sanctioned moves the channel back and asks about the name.

**Nothing an owner did is lost to a failure** (review fix 2). A rename or a
move that meets a 429, a 5xx or a transport failure is kept pending in
`linker.json` and retried when due; a restore moves first and renames
separately. On start, a reconcile moves an archived project's channel out of
Jarvis, and a live project's channel back out of the archive when it went
there with its project (never one moved for crowding — `archived_by`).

**Never a delete (D3).** A channel is created, renamed, moved between the
Jarvis categories, posted in, or left alone. `DiscordRest` has no delete
method, and a permanently deleted project's channel is kept with a note.
Moving never sends `lock_permissions` or `permission_overwrites`.

**Rate limits.** Discord allows two renames per channel per ten minutes; the
HUD shows "rename pending" and `GET /discord` counts pending renames and
moves. Creates (backfill, crowding moves) are paced at one a second.

Nothing here logs a URL, a token or a message body: failures are the
operation, the HTTP status and Discord's code (`rest.describe`).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import re
import threading
import time
import unicodedata
import uuid

from ..approvals import ApprovalRequest
from ..model import TERMINAL_STATES, to_json
from ..provider import Decision
from ..stores import StoreError, _write_bytes
from . import guild as guildmod
from . import perms
from .rest import (MISSING_ACCESS, MISSING_PERMISSIONS, UNKNOWN_CHANNEL, describe)

LOG = logging.getLogger(__name__)

KINDS = frozenset({"project_updated", "project_archived", "project_restored",
                   "project_deleted"})
SLUG_MAX = 90
CHANNELS_MAX = 50              # Discord's limit per category
CROWDED_AT = 45
IDLE_DAYS = 30
VIEW_TTL_S = 60.0
FACTS_TTL_S = 60.0
CHANNELS_TTL_S = 60.0
READ_TIMEOUT_S = 5.0
CREATE_INTERVAL = 1.0
TICK_S = 5.0
RETRY_S = 30.0                 # a transient failure without a retry_after
ASK_AGAIN_S = 24 * 3600.0      # after a denied or unanswered housekeeping ask
ORIGIN = "Jarvis housekeeping"
TEXT, CATEGORY = 0, 4
# Why a channel sits in an archive category: with its project, for room, or
# because the owner typed `/channel archive` (B2). Only BY_PROJECT is undone
# by a restart's reconcile; the other two stay until the owner moves them.
BY_PROJECT, BY_CROWDING, BY_OWNER = "project", "crowding", "owner"
ARCHIVE_REASONS = (BY_PROJECT, BY_CROWDING, BY_OWNER)
STATES = ("linked_ok", "unlinked", "not_found", "no_access", "wrong_guild",
          "missing_permissions", "folder_missing", "unconfigured", "unreachable")
_SNOWFLAKE = re.compile(r"^[0-9]{5,24}$")
CROWDING_NOTE = ("Moved to {where} to make room (you approved it). The project is still "
                 "active and this channel still works. It comes back to Jarvis with "
                 "`/channel restore` (coming soon); until then you can drag it back in "
                 "Discord — the link is kept either way.")


class LinkError(Exception):
    """A refusal the HUD shows in the owner's words; `status` is the HTTP one."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# -- names --------------------------------------------------------------------

def slug(name: str, project_id: str, taken=()) -> str:
    """Lowercase ASCII with hyphens, at most 90 characters. An empty result is
    `project-<id>`; a clash with `taken` gets `-<id[:4]>` appended."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    text = text[:SLUG_MAX].strip("-")
    if not text:
        text = f"project-{project_id}"[:SLUG_MAX]
    if text in set(taken or ()):
        suffix = f"-{project_id[:4]}"
        text = text[:SLUG_MAX - len(suffix)].strip("-") + suffix
    return text


def slugs(name: str, project_id: str) -> tuple[str, str]:
    """The two names a project's channel may carry: bare and clash-suffixed."""
    bare = slug(name, project_id)
    return bare, slug(name, project_id, {bare})


def topic(project) -> str:
    return f"Jarvis project · {project.name} · {project.id}"[:1024]


def _iso(value) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _transient(exc) -> bool:
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


class ChannelLinker:
    """Owner actions are synchronous (the HUD routes call them); lifecycle
    events and housekeeping run on the linker's worker; approval asks on its
    asker. `start()` subscribes and starts both; `close()` stops them."""

    def __init__(self, daemon, rest, *, approvals=None, guild=None, clock=time.monotonic,
                 wall=time.time, bot_id=None, state_path=None):
        self.daemon = daemon
        self.stores = daemon.stores
        self.rest = rest
        self.approvals = approvals if approvals is not None else getattr(daemon, "approvals", None)
        self._guild = guild or guildmod.load
        self._clock, self._wall = clock, wall
        self._bot_id = bot_id
        self._state_path = Path(state_path) if state_path else \
            Path(self.stores.root) / "discord" / "linker.json"
        self._lock = threading.RLock()        # serialises every channel write
        self._state_lock = threading.RLock()  # linker.json, read and written by three threads
        self._views: dict[str, tuple[float, dict]] = {}
        self._facts: tuple[float, dict] | None = None
        self._channels_cache: tuple[float, list] | None = None
        self._perm_status: dict | None = None
        self._awaiting = 0
        self._last_create = None
        self._linked_cfg = None
        self._reconciled_cfg = None
        self._crowding_due = True
        # A reconcile or crowding pass whose channel listing failed is tried
        # again, but not before this (monotonic) time.
        self._listing_retry_at = None
        self._refreshed_at = None
        self.last_error: dict | None = None
        self._state = self._load_state()
        self._stop = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self.subscription = None
        self._worker = self._asker = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ChannelLinker":
        if self.subscription is not None:
            return self
        self.subscription = self.daemon.bus.subscribe(lambda r: r.get("kind") in KINDS)
        # Asks a previous process left unanswered are asked again (fix 2).
        with self._state_lock:
            for job in self._state["asks"]:
                self._awaiting += 1
                self._jobs.put(job)
        self._worker = threading.Thread(target=self._run, name="jarvis-discord-linker",
                                        daemon=True)
        self._asker = threading.Thread(target=self._ask_loop, name="jarvis-discord-linker-asks",
                                       daemon=True)
        self._worker.start()
        self._asker.start()
        return self

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self.subscription is not None:
            self.daemon.bus.unsubscribe(self.subscription)
            self.subscription.offer({"kind": "shutdown"})
        self._jobs.put(None)
        for worker in (self._worker, self._asker):
            if worker is not None:
                worker.join(timeout=2)

    # -- configuration and facts -------------------------------------------

    def config(self):
        try:
            return self._guild()
        except Exception:
            return None

    def _bot(self) -> str:
        if callable(self._bot_id):
            return str(self._bot_id())
        if self._bot_id:
            return str(self._bot_id)
        self._bot_id = str(self.rest.me(timeout=READ_TIMEOUT_S, patient=False)["id"])
        return self._bot_id

    def guild_facts(self, cfg, *, refresh=False) -> dict:
        """The guild, its roles and the bot's member record, cached 60 s."""
        now = self._clock()
        cached = self._facts
        if (not refresh and cached is not None and now - cached[0] < FACTS_TTL_S
                and cached[1]["guild"].get("id") == cfg.guild_id):
            return cached[1]
        kw = {"timeout": READ_TIMEOUT_S, "patient": False}
        guild = self.rest.guild(cfg.guild_id, **kw)
        guild.setdefault("id", cfg.guild_id)
        member = self.rest.guild_member(cfg.guild_id, self._bot(), **kw)
        facts = {"guild": guild, "roles": guild.get("roles") or [], "member": member}
        self._facts = (now, facts)
        return facts

    def channels(self, cfg, *, refresh=False) -> list | None:
        """The guild's channels, cached 60 s; None when Discord cannot say.
        Every channel write here drops the cache, so names stay current."""
        now = self._clock()
        cached = self._channels_cache
        if not refresh and cached is not None and now - cached[0] < CHANNELS_TTL_S:
            return cached[1]
        try:
            found = self.rest.guild_channels(cfg.guild_id, timeout=READ_TIMEOUT_S, patient=False)
        except Exception as exc:
            self._failed("list_channels", exc)
            return None
        self._channels_cache = (now, found)
        return found

    def _wrote(self, project_id=None) -> None:
        self._channels_cache = None
        self.invalidate(project_id)

    def refresh_permissions(self) -> dict | None:
        """The server-wide check `GET /discord` shows; None until configured."""
        cfg = self.config()
        if cfg is None:
            self._perm_status = None
            return None
        try:
            facts = self.guild_facts(cfg, refresh=True)
        except Exception as exc:
            self._failed("check_permissions", exc)
            return self._perm_status
        base = perms.effective(facts["guild"], facts["roles"], facts["member"])
        given = perms.granted(facts["guild"], facts["roles"], facts["member"])
        self._perm_status = {"missing": perms.missing(base), "excess": perms.excess(given),
                             "administrator": bool(given & perms.ADMINISTRATOR),
                             "checked_at": self._wall()}
        return self._perm_status

    def _failed(self, op, exc):
        facts = describe(exc)
        self.last_error = {"op": op, "status": facts["status"], "code": facts["code"],
                           "at": self._wall()}
        LOG.warning("Discord %s failed (HTTP %s, code %s, %s)", op, facts["status"],
                    facts["code"], facts["error"])

    # -- persisted state ------------------------------------------------------
    # archive_overflow: [category id]; pending_renames: {channel: {project_id,
    # name, due}}; pending_moves: {channel: {project_id, parent_id, reason,
    # due}}; sanctioned: {project id: the last name the owner or an approval
    # chose}; archived_by: {channel: "project" | "crowding"}; asked: {key:
    # when}; asks: [the housekeeping jobs not yet answered].

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        def rows(key):
            return {str(k): v for k, v in (data.get(key) or {}).items() if isinstance(v, dict)} \
                if isinstance(data.get(key), dict) else {}

        def strings(key):
            return {str(k): v for k, v in (data.get(key) or {}).items() if isinstance(v, str)} \
                if isinstance(data.get(key), dict) else {}

        asked = data.get("asked") if isinstance(data.get("asked"), dict) else {}
        return {
            "archive_overflow": [c for c in data.get("archive_overflow") or [] if isinstance(c, str)],
            "pending_renames": rows("pending_renames"),
            "pending_moves": rows("pending_moves"),
            "sanctioned": strings("sanctioned"),
            "archived_by": strings("archived_by"),
            "asked": {str(k): float(v) for k, v in asked.items() if isinstance(v, (int, float))},
            "asks": [j for j in data.get("asks") or [] if isinstance(j, dict) and j.get("action")],
        }

    def _save_state(self) -> None:
        with self._state_lock:
            try:
                _write_bytes(self._state_path, json.dumps(self._state, sort_keys=True).encode())
            except (StoreError, OSError, TypeError, ValueError) as exc:
                LOG.warning("Discord linker state not saved (%s)", type(exc).__name__)

    def _sanction(self, project_id, name) -> None:
        with self._state_lock:
            if self._state["sanctioned"].get(project_id) != name:
                self._state["sanctioned"][project_id] = name
                self._save_state()

    def sanctioned(self, project_id) -> str | None:
        return self._state["sanctioned"].get(project_id)

    def archive_target(self, cfg) -> str:
        """Where an archived channel goes: the newest overflow category, else
        Jarvis Archive."""
        overflow = self._state.get("archive_overflow") or []
        return overflow[-1] if overflow else cfg.archive_category_id

    def archive_categories(self, cfg) -> set[str]:
        return {cfg.archive_category_id, *(self._state.get("archive_overflow") or [])}

    def category_name(self, cfg, parent) -> str | None:
        parent = str(parent or "")
        if not parent:
            return None
        if parent == cfg.category_id:
            return "Jarvis"
        if parent == cfg.archive_category_id:
            return "Jarvis Archive"
        overflow = self._state.get("archive_overflow") or []
        if parent in overflow:
            return f"Jarvis Archive {overflow.index(parent) + 2}"
        return "another category"

    # -- status --------------------------------------------------------------

    def status(self) -> dict:
        """For `GET /discord`: states and counts, never a token or a URL."""
        cfg = self.config()
        renames = len(self._state.get("pending_renames") or {})
        moves = len(self._state.get("pending_moves") or {})
        if cfg is None:
            state, reason = "unconfigured", "run `jarvis auth discord-guild` to set up the server"
        elif renames or moves:
            parts = []
            if renames:
                parts.append(f"{renames} channel rename(s)")
            if moves:
                parts.append(f"{moves} channel move(s)")
            state, reason = "degraded", " and ".join(parts) + " pending (Discord rate limit or outage)"
        elif self.last_error and self._wall() - self.last_error.get("at", 0) < 600:
            error = self.last_error
            state, reason = "degraded", (f"last {error['op']} failed (HTTP {error['status']}, "
                                         f"code {error['code']})")
        else:
            state, reason = "ok", ""
        return {
            "guild": {"configured": cfg is not None, "id": cfg.guild_id if cfg else None},
            "linker": {"state": state, "reason": reason, "pending_renames": renames,
                       "pending_moves": moves, "awaiting_approval": self._awaiting},
            "permissions": dict(self._perm_status) if self._perm_status else None,
        }

    # -- the channel light for one project ------------------------------------

    def invalidate(self, project_id=None) -> None:
        if project_id is None:
            self._views.clear()
        else:
            self._views.pop(project_id, None)

    def view(self, project_id: str, *, refresh: bool = False) -> dict:
        """`GET /projects/{id}/discord`: cached 60 s, every read 5 s at most."""
        now = self._clock()
        cached = self._views.get(project_id)
        if not refresh and cached is not None and now - cached[0] < VIEW_TTL_S:
            return dict(cached[1])
        project = self._project(project_id)
        result = self._inspect(project)
        result["rename_pending"] = bool(project.discord_channel_id and project.discord_channel_id
                                        in (self._state.get("pending_renames") or {}))
        self._views[project_id] = (now, result)
        return dict(result)

    def _inspect(self, project) -> dict:
        cfg = self.config()
        out = {"channel_id": project.discord_channel_id,
               "origin": project.discord_channel_origin if project.discord_channel_id else None,
               "name": None, "category": None, "state": "unlinked", "missing": [],
               "checked_at": self._wall()}
        if cfg is None:
            out["state"] = "unconfigured"
            return out
        if not Path(project.root).is_dir():
            out["state"] = "folder_missing"
            return out
        if not project.discord_channel_id:
            return out
        try:
            channel = self.rest.get_channel(project.discord_channel_id, timeout=READ_TIMEOUT_S,
                                            patient=False)
        except Exception as exc:
            out["state"] = self._read_state(exc)
            return out
        out["name"] = channel.get("name") if isinstance(channel.get("name"), str) else None
        out["category"] = self.category_name(cfg, channel.get("parent_id"))
        if str(channel.get("guild_id") or "") != cfg.guild_id:
            out["state"] = "wrong_guild"
            return out
        try:
            facts = self.guild_facts(cfg)
        except Exception as exc:
            out["state"] = "unreachable" if _transient(exc) else "no_access"
            return out
        lacking = perms.missing(perms.effective(facts["guild"], facts["roles"],
                                                facts["member"], channel))
        out["missing"] = lacking
        out["state"] = "missing_permissions" if lacking else "linked_ok"
        return out

    @staticmethod
    def _read_state(exc) -> str:
        status, code = getattr(exc, "status", None), getattr(exc, "code", None)
        if code == UNKNOWN_CHANNEL or status == 404:
            return "not_found"
        if code == MISSING_ACCESS or status in (401, 403):
            return "no_access"
        return "unreachable"

    # -- owner actions (the HUD routes; later B2's slash handlers) ------------

    def _project(self, project_id):
        try:
            project = self.stores.projects.get(project_id)
        except StoreError:
            project = None
        if project is None:
            raise LinkError(404, f"no project {project_id}")
        return project

    def _get(self, project_id):
        try:
            return self.stores.projects.get(project_id)
        except StoreError:
            return None

    def _require_config(self):
        cfg = self.config()
        if cfg is None:
            raise LinkError(409, "Discord is not set up: run `jarvis auth discord-guild` first")
        return cfg

    @staticmethod
    def _refuse_inbox(project):
        if project.inbox:
            raise LinkError(409, "The Inbox's channel is #ungrouped; only re-running "
                                 "`jarvis auth discord-guild` changes it")

    @staticmethod
    def _refuse_unlinkable(project):
        if project.archived:
            raise LinkError(409, f"project {project.name} is archived; restore it first")
        if not Path(project.root).is_dir():
            raise LinkError(409, f"the folder {project.root} does not exist, so the project "
                                 "cannot have a channel")

    def _channel_name(self, cfg, channel_id) -> str | None:
        for channel in self.channels(cfg) or ():
            if str(channel.get("id")) == str(channel_id):
                name = channel.get("name")
                return name if isinstance(name, str) else None
        return None

    def _taken(self, project_id, own_channel=None) -> set[str]:
        """The names already in use, from the channels Discord really has
        (review fix 3) — never this project's own channel. If Discord cannot
        be read, the other linked projects' names stand in."""
        cfg = self.config()
        listed = self.channels(cfg) if cfg is not None else None
        names = {"ungrouped"}
        if listed is not None:
            for channel in listed:
                if channel.get("type") == CATEGORY or str(channel.get("id")) == str(own_channel):
                    continue
                if isinstance(channel.get("name"), str):
                    names.add(channel["name"])
            return names
        from ..daemon import safe_list
        for other in safe_list(self.stores.projects):
            if other.id != project_id and other.discord_channel_id and not other.inbox:
                names.update(slugs(other.name, other.id))
        return names

    def target_name(self, project, current=None) -> str:
        """The name this project's channel should carry. A current name that
        is already the bare or the suffixed slug is kept: whoever had a name
        first keeps it."""
        bare, suffixed = slugs(project.name, project.id)
        if current in (bare, suffixed):
            return current
        return bare if bare not in self._taken(project.id, project.discord_channel_id) else suffixed

    def names_match(self, project, name) -> bool:
        return name in slugs(project.name, project.id)

    def _pace(self):
        if self._last_create is not None:
            wait = self._last_create + CREATE_INTERVAL - self._clock()
            if wait > 0:
                self._stop.wait(wait)
        self._last_create = self._clock()

    def _set_channel(self, project_id, channel_id, origin, *, by="owner"):
        """Write the link (or clear it) and announce it. The record is re-read
        under the daemon lock, so a stale copy never wins."""
        with self.daemon._lock:
            project = self._project(project_id)
            if channel_id is not None:
                self._refuse_unlinkable(project)
            previous = project.discord_channel_id
            project.discord_channel_id = channel_id
            project.discord_channel_origin = origin if channel_id else None
            self.stores.projects.save(project)
        if previous and previous != channel_id:
            self._forget_channel(previous)
        self.invalidate(project_id)
        self.daemon.bus.publish({"kind": "project_updated", "project_id": project.id,
                                 "data": {"project_id": project.id,
                                          "changed": ["discord_channel_id"],
                                          "project": to_json(project), "by": by,
                                          "previous": {"discord_channel_id": previous}}})
        return project

    def _forget_channel(self, channel_id) -> None:
        """A channel is no longer linked: nothing pending for it may still run."""
        with self._state_lock:
            changed = False
            for key in ("pending_renames", "pending_moves", "archived_by"):
                changed |= self._state[key].pop(str(channel_id), None) is not None
            if changed:
                self._save_state()

    def _rest_refusal(self, op, exc) -> LinkError:
        self._failed(op, exc)
        return self._refusal(exc, op)

    @staticmethod
    def _refusal(exc, op="move_channel") -> LinkError:
        """The LinkError for a failure that is already logged."""
        facts = describe(exc)
        if facts["status"] is None:
            return LinkError(502, f"Discord could not be reached ({facts['error']})")
        if facts["status"] == 429:
            return LinkError(503, "Discord is rate-limiting this; try again in a minute")
        why = {MISSING_ACCESS: "the bot cannot see it",
               MISSING_PERMISSIONS: "the bot is missing a permission"}.get(facts["code"], "")
        return LinkError(502, f"Discord refused {op} (HTTP {facts['status']}, code "
                              f"{facts['code']}){': ' + why if why else ''}")

    def _orphan(self, cfg, project) -> dict | None:
        """A channel Jarvis already made for this project (its topic ends with
        `· <id>`) but never recorded — the create's answer was lost, or the
        link was refused after it (review fix 6). Adopting it makes a retry
        idempotent."""
        from ..daemon import safe_list
        listed = self.channels(cfg, refresh=True)
        if listed is None:
            return None
        linked = {p.discord_channel_id for p in safe_list(self.stores.projects)
                  if p.discord_channel_id}
        for channel in listed:
            text = channel.get("topic")
            if (channel.get("type") == TEXT and str(channel.get("parent_id") or "") == cfg.category_id
                    and isinstance(text, str) and text.endswith(f"· {project.id}")
                    and str(channel.get("id")) not in linked):
                return channel
        return None

    def create(self, project_id: str, *, by: str = "owner") -> dict:
        """A new channel in the Jarvis category, named by `slug`, topic set —
        or the one a lost create already made, adopted."""
        with self._lock:
            cfg = self._require_config()
            project = self._project(project_id)
            self._refuse_inbox(project)
            self._refuse_unlinkable(project)
            if project.discord_channel_id:
                raise LinkError(409, f"project {project.name} already has a channel; "
                                     "unlink it first")
            orphan = self._orphan(cfg, project)
            if orphan is not None:
                channel_id, name = str(orphan["id"]), orphan.get("name")
            else:
                self._pace()
                name = self.target_name(project)
                try:
                    channel_id = self.rest.create_channel(cfg.guild_id, name, kind=TEXT,
                                                          parent_id=cfg.category_id,
                                                          topic=topic(project))
                except Exception as exc:
                    raise self._rest_refusal("create_channel", exc) from None
                self._wrote()
            if isinstance(name, str):
                self._sanction(project.id, name)
            self._set_channel(project.id, channel_id, "created", by=by)
            self._crowding_due = True
            return self.view(project.id, refresh=True)

    def validate_link(self, project, channel_id) -> dict:
        """Every check a link must pass (shared with B2's `/project link`).
        -> the channel object."""
        self._require_config()
        if not isinstance(channel_id, str) or not _SNOWFLAKE.match(channel_id):
            raise LinkError(400, "channel_id must be a Discord channel id (digits only)")
        self._refuse_inbox(project)
        self._refuse_unlinkable(project)
        return self.validate_channel(channel_id, project_id=project.id)

    def validate_channel(self, channel_id, *, project_id=None) -> dict:
        """The channel half of `validate_link`: everything that does not depend
        on the project. B2's `/project new` runs it *before* it makes a folder
        or a project, so an unlinkable channel refuses with nothing made."""
        cfg = self._require_config()
        if not isinstance(channel_id, str) or not _SNOWFLAKE.match(channel_id):
            raise LinkError(400, "channel_id must be a Discord channel id (digits only)")
        if channel_id == cfg.ungrouped_channel_id:
            raise LinkError(400, "#ungrouped belongs to the Inbox and cannot be linked")
        if channel_id in (cfg.category_id, cfg.archive_category_id,
                          *(self._state.get("archive_overflow") or [])):
            raise LinkError(400, "that is a Jarvis category, not a channel")
        from ..daemon import safe_list
        for other in safe_list(self.stores.projects):
            if other.id != project_id and other.discord_channel_id == channel_id:
                raise LinkError(409, f"that channel is already linked to project {other.name}")
        try:
            channel = self.rest.get_channel(channel_id, timeout=READ_TIMEOUT_S, patient=False)
        except Exception as exc:
            state = self._read_state(exc)
            if state == "not_found":
                raise LinkError(400, "Discord has no channel with that id") from None
            if state == "no_access":
                raise LinkError(400, "the bot cannot see that channel") from None
            raise self._rest_refusal("get_channel", exc) from None
        if str(channel.get("guild_id") or "") != cfg.guild_id:
            raise LinkError(400, "that channel is in another server, not Jarvis's")
        if channel.get("type") != TEXT:
            raise LinkError(400, "that is not a text channel")
        try:
            facts = self.guild_facts(cfg)
        except Exception as exc:
            raise self._rest_refusal("check_permissions", exc) from None
        lacking = perms.missing(perms.effective(facts["guild"], facts["roles"],
                                                facts["member"], channel))
        if lacking:
            raise LinkError(400, "the bot is missing " + ", ".join(lacking) + " in that channel")
        return channel

    def link(self, project_id: str, channel_id, *, by: str = "owner") -> dict:
        with self._lock:
            project = self._project(project_id)
            if project.discord_channel_id and project.discord_channel_id == channel_id:
                return self.view(project.id, refresh=True)
            channel = self.validate_link(project, channel_id)
            # The owner chose this channel as it is named: that name stands.
            if isinstance(channel.get("name"), str):
                self._sanction(project.id, channel["name"])
            self._set_channel(project.id, channel_id, "linked", by=by)
            return self.view(project.id, refresh=True)

    def unlink(self, project_id: str, *, by: str = "owner") -> dict:
        """Forget the link. The channel itself is kept, always."""
        with self._lock:
            project = self._project(project_id)
            self._refuse_inbox(project)
            if not project.discord_channel_id:
                return self.view(project.id, refresh=True)
            self._set_channel(project.id, None, None, by=by)
            with self._state_lock:
                if self._state["sanctioned"].pop(project.id, None) is not None:
                    self._save_state()
            return self.view(project.id, refresh=True)

    def move_channel(self, project_id: str, *, archive: bool) -> dict:
        """`/channel archive|restore` (B2): move a live project's channel to
        the archive category or back to Jarvis. The project stays active and
        its threads keep working. The owner typed the command, which is the
        owner's permission (D4), so it acts at once. Never a delete (D3), and
        never `lock_permissions` or overwrites. -> {"moved", "category"}."""
        with self._lock:
            cfg = self._require_config()
            project = self._project(project_id)
            self._refuse_inbox(project)
            if project.archived:
                raise LinkError(409, f"project {project.name} is archived; its channel moves "
                                     "with the project, from the HUD")
            channel_id = project.discord_channel_id
            if not channel_id:
                raise LinkError(409, f"project {project.name} has no channel")
            try:
                current = self.rest.get_channel(channel_id, timeout=READ_TIMEOUT_S,
                                                patient=False)
            except Exception as exc:
                # Nothing was decided: an older pending move stays pending.
                raise self._rest_refusal("get_channel", exc) from None
            # The owner's command supersedes any older move still waiting to
            # retry (a 429'd restore, a crowding move): left pending, it would
            # undo this one as soon as it came due. Dropped only now that the
            # command will act — a failed lookup must not lose the older move.
            with self._state_lock:
                if self._state["pending_moves"].pop(str(channel_id), None) is not None:
                    self._save_state()
            parent = str(current.get("parent_id") or "")
            if (archive and parent in self.archive_categories(cfg)) \
                    or (not archive and parent == cfg.category_id):
                # Already there. Still record that the owner chose it, so a
                # restart's reconcile leaves it where the owner put it.
                with self._state_lock:
                    if archive:
                        self._state["archived_by"][str(channel_id)] = BY_OWNER
                    else:
                        self._state["archived_by"].pop(str(channel_id), None)
                    self._save_state()
                return {"moved": False, "category": self.category_name(cfg, parent)}
            target = self.archive_target(cfg) if archive else cfg.category_id
            try:
                # B1's bookkeeping: pending on a transient failure (the owner's
                # move is retried, not lost), `archived_by` set or cleared.
                self.move(project.id, channel_id, target,
                          BY_OWNER if archive else "restore", raise_errors=True)
            except Exception as exc:
                refusal = self._refusal(exc)
                if _transient(exc):
                    refusal = LinkError(refusal.status, f"{refusal}; the move is kept and "
                                                        "retried when Discord allows")
                raise refusal from None
            self._crowding_due = True
            return {"moved": True, "category": self.category_name(cfg, target)}

    def backfill(self) -> dict:
        """A channel for every unlinked, live project whose folder exists, at
        most one create a second. Owner-only (the route checks)."""
        from ..daemon import safe_list
        self._require_config()
        results, skipped = [], []
        for project in safe_list(self.stores.projects):
            if project.inbox or project.archived or project.discord_channel_id:
                continue
            if not Path(project.root).is_dir():
                skipped.append({"project_id": project.id, "name": project.name,
                                "reason": "folder missing"})
                continue
            if self._stop.is_set():
                break
            try:
                view = self.create(project.id)
                results.append({"project_id": project.id, "name": project.name,
                                "channel_id": view.get("channel_id"), "status": "created"})
            except LinkError as exc:
                results.append({"project_id": project.id, "name": project.name,
                                "channel_id": None, "status": "failed", "error": str(exc)})
        return {"created": sum(r["status"] == "created" for r in results),
                "results": results, "skipped": skipped}

    # -- the Inbox ---------------------------------------------------------------

    def ensure_inbox(self) -> bool:
        """Link the Inbox to #ungrouped once the guild is configured. True if
        it changed anything."""
        cfg = self.config()
        if cfg is None:
            return False
        if self._linked_cfg == cfg:
            return False
        try:
            inbox = self.stores.projects.inbox()
        except StoreError as exc:
            LOG.warning("Discord linker could not read the Inbox (%s)", type(exc).__name__)
            return False
        if inbox.discord_channel_id == cfg.ungrouped_channel_id \
                and inbox.discord_channel_origin == "created":
            self._linked_cfg = cfg
            return False
        with self._lock:
            self._set_channel(inbox.id, cfg.ungrouped_channel_id, "created", by="owner")
        # Marked done only once the link is written: a failure is retried on
        # the next tick instead of leaving the Inbox unlinked until restart.
        self._linked_cfg = cfg
        return True

    # -- the two channel writes, kept when they fail (review fix 2) --------------

    def _due(self, exc) -> float:
        delay = getattr(exc, "retry_after", None)
        try:
            delay = float(delay) if delay is not None else RETRY_S
        except (TypeError, ValueError):
            delay = RETRY_S
        return self._wall() + max(delay, 1.0)

    def rename(self, project_id, channel_id, name) -> bool:
        """Apply a **sanctioned** name — the owner's own action or an approved
        ask chose it; nothing else calls this. A 429, 5xx or transport
        failure keeps it pending, retried when due."""
        self._sanction(project_id, name)
        with self._lock:
            try:
                self.rest.modify_channel(channel_id, name=name)
            except Exception as exc:
                self._failed("rename_channel", exc)
                with self._state_lock:
                    if _transient(exc):
                        self._state["pending_renames"][str(channel_id)] = {
                            "project_id": project_id, "name": name, "due": self._due(exc)}
                    else:
                        self._state["pending_renames"].pop(str(channel_id), None)
                    self._save_state()
                self.invalidate(project_id)
                return False
            with self._state_lock:
                if self._state["pending_renames"].pop(str(channel_id), None) is not None:
                    self._save_state()
            self._wrote(project_id)
            return True

    def move(self, project_id, channel_id, parent_id, reason, *, raise_errors=False) -> bool:
        """Move a channel to a category by `parent_id` alone. `reason` is why
        it now sits where it does (`project`, `crowding`, `owner`, or
        `restore` for the way back), kept so a restart knows which moves to
        undo. Failures that may pass are kept pending; `raise_errors` also
        re-raises the failure, for an owner command that must answer."""
        with self._lock:
            try:
                self.rest.modify_channel(channel_id, parent_id=parent_id)
            except Exception as exc:
                self._failed("move_channel", exc)
                with self._state_lock:
                    if _transient(exc):
                        self._state["pending_moves"][str(channel_id)] = {
                            "project_id": project_id, "parent_id": str(parent_id),
                            "reason": reason, "due": self._due(exc)}
                    else:
                        self._state["pending_moves"].pop(str(channel_id), None)
                    self._save_state()
                if raise_errors:
                    raise
                return False
            with self._state_lock:
                self._state["pending_moves"].pop(str(channel_id), None)
                if reason in ARCHIVE_REASONS:
                    self._state["archived_by"][str(channel_id)] = reason
                else:
                    self._state["archived_by"].pop(str(channel_id), None)
                self._save_state()
            self._wrote(project_id)
            return True

    def _retry_pending(self) -> None:
        """Retry what is due. A rename retries **its own** name, and only
        while the project still carries it: once the name has moved on, the
        newer change took its own owner/api path (fix 1). A move retries only
        while it still fits the project's state."""
        now = self._wall()
        for channel, row in list((self._state.get("pending_renames") or {}).items()):
            if row.get("due", 0) > now:
                continue
            project = self._get(row.get("project_id") or "")
            name = row.get("name")
            if (project is None or project.discord_channel_id != channel
                    or not isinstance(name, str) or not self.names_match(project, name)):
                with self._state_lock:
                    self._state["pending_renames"].pop(channel, None)
                    self._save_state()
                continue
            self.rename(project.id, channel, name)
        for channel, row in list((self._state.get("pending_moves") or {}).items()):
            if row.get("due", 0) > now:
                continue
            project = self._get(row.get("project_id") or "")
            reason = row.get("reason")
            fits = project is not None and project.discord_channel_id == channel and (
                bool(project.archived) if reason == BY_PROJECT else not project.archived)
            if not fits:
                with self._state_lock:
                    self._state["pending_moves"].pop(channel, None)
                    self._save_state()
                continue
            self.move(project.id, channel, row.get("parent_id"), reason)

    _retry_renames = _retry_pending     # PR #8's name, kept for callers built on it

    # -- lifecycle events (worker) ----------------------------------------------

    def _handle(self, record) -> None:
        kind = record.get("kind")
        data = record.get("data") or {}
        project_id = record.get("project_id") or data.get("project_id")
        cfg = self.config()
        if cfg is None or not project_id:
            return
        self.invalidate(project_id)
        if kind == "project_updated":
            changed = data.get("changed") or []
            if "name" in changed:
                self._renamed(cfg, project_id, data)
        elif kind == "project_archived":
            self._archived(cfg, project_id)
        elif kind == "project_restored":
            self._restored(cfg, project_id, data)
        elif kind == "project_deleted":
            channel = data.get("discord_channel_id")
            if channel:
                self._forget_channel(channel)
                with self._state_lock:
                    if self._state["sanctioned"].pop(project_id, None) is not None:
                        self._save_state()
                name = str(data.get("name") or "")
                self._note(channel, f"Project {name} was permanently deleted in the HUD. "
                                    "This channel is kept; it is no longer linked to Jarvis.")

    def _note(self, channel, text) -> None:
        try:
            self.rest.post(channel, content=text[:2000], silent=True)
        except Exception as exc:
            self._failed("post_note", exc)

    def _ask_rename(self, project, channel, to, why) -> None:
        current = self._channel_name(self.config(), channel) if self.config() else None
        before = current or self.sanctioned(project.id) or ""
        self._enqueue({
            "action": "rename", "project_id": project.id, "channel": channel, "to": to,
            "args": {"action": "rename", "channel": channel, "from": before, "to": to,
                     "why": why}})

    def _renamed(self, cfg, project_id, data) -> None:
        project = self._get(project_id)
        if project is None or not project.discord_channel_id or project.inbox:
            return
        channel = project.discord_channel_id
        current = self._channel_name(cfg, channel)
        to = self.target_name(project, current)
        if current is not None and to == current:
            self._sanction(project.id, current)      # the channel already says it
            return
        if data.get("by") == "owner":
            self.rename(project.id, channel, to)
            return
        # Not the owner's own action: ask first (D4). A pending owner rename
        # for an older name is dropped by the retry, never carried along.
        self._ask_rename(project, channel, to, f"project {project.name} was renamed outside the HUD")

    def _archived(self, cfg, project_id) -> None:
        project = self._get(project_id)
        if project is None or not project.discord_channel_id or project.inbox:
            return
        target = self.archive_target(cfg)
        self.move(project.id, project.discord_channel_id, target, BY_PROJECT)
        self._note(project.discord_channel_id,
                   f"Project {project.name} was archived in the HUD. This channel moves to "
                   f"{self.category_name(cfg, target)} and is kept; restore the project in "
                   "the HUD to work here again.")
        self._crowding_due = True

    def _restored(self, cfg, project_id, data=None) -> None:
        """Move back first; the name is a separate step, applied only if it
        was sanctioned — the name the project had when the owner archived it,
        or PR #4's renumbering of that name on restore. Anything else asks."""
        data = data or {}
        project = self._get(project_id)
        if project is None or not project.discord_channel_id or project.inbox:
            return
        channel = project.discord_channel_id
        moved = self.move(project.id, channel, cfg.category_id, "restore")
        current = self._channel_name(cfg, channel)
        to = self.target_name(project, current)
        sanctioned = self.sanctioned(project.id)
        before = data.get("previous_name")
        renumbered = (isinstance(before, str) and before != project.name
                      and (self.names_match(SimpleProject(before, project.id), sanctioned or "")
                           or self.names_match(SimpleProject(before, project.id), current or "")))
        if current is not None and to == current:
            pass
        elif to == sanctioned or renumbered:
            self.rename(project.id, channel, to)
        else:
            self._ask_rename(project, channel, to,
                             f"project {project.name} was restored under a name nobody "
                             "approved for its channel; it was moved back with its old name")
        if moved:
            self._note(channel, "Restored.")
        elif str(channel) in (self._state.get("pending_moves") or {}):
            self._note(channel, (f"Project {project.name} was restored in the HUD. Moving this "
                                 "channel back to Jarvis did not work yet; it will be retried."))
        else:
            self._note(channel, (f"Project {project.name} was restored in the HUD, but Discord "
                                 "refused to move this channel back to Jarvis. Move it by hand, "
                                 "or use /channel restore."))
        self._crowding_due = True

    # -- start-up reconcile (review fix 2) ------------------------------------------

    def reconcile(self) -> int:
        """Bring channels in line with their projects after a restart or an
        outage: an archived project's channel still in Jarvis goes to the
        archive; a live project's channel that went to the archive *with its
        project* comes back. One moved for crowding stays. -> moves made, or
        None when the guild's channels could not be listed (nothing was
        checked, so the caller tries again)."""
        cfg = self.config()
        if cfg is None:
            return 0
        listed = self.channels(cfg, refresh=True)
        if listed is None:
            return None
        from ..daemon import safe_list
        parents = {str(c.get("id")): str(c.get("parent_id") or "") for c in listed}
        archives = self.archive_categories(cfg)
        moved = 0
        for project in safe_list(self.stores.projects):
            channel = project.discord_channel_id
            if not channel or project.inbox or channel not in parents:
                continue
            if channel in (self._state.get("pending_moves") or {}):
                continue                      # the retry owns it
            where = parents[channel]
            if project.archived and where == cfg.category_id:
                moved += self.move(project.id, channel, self.archive_target(cfg), BY_PROJECT)
            elif (not project.archived and where in archives
                  and self._state["archived_by"].get(channel) == BY_PROJECT):
                moved += self.move(project.id, channel, cfg.category_id, "restore")
        return moved

    # -- crowding (D3, O7) -----------------------------------------------------

    def _last_activity(self, project) -> float:
        from ..daemon import safe_list
        stamps = [_iso(project.created)]
        stamps += [_iso(t.updated) for t in safe_list(self.stores.threads, project_id=project.id)]
        for task in safe_list(self.stores.tasks, project_id=project.id):
            if task.state not in TERMINAL_STATES:
                return self._wall()               # work under way is not idle
            stamps += [_iso(task.updated), _iso(task.created)]
        return max([s for s in stamps if s is not None] or [0.0])

    def check_crowding(self) -> list[str]:
        """Count the Jarvis category and the archive target; ask (once) when
        either reaches 45 of 50. -> the asks raised (for tests and logs), or
        None when the channels could not be listed (nothing was counted)."""
        cfg = self.config()
        if cfg is None:
            return []
        channels = self.channels(cfg, refresh=True)
        if channels is None:
            return None
        counts: dict[str, int] = {}
        for channel in channels:
            parent = str(channel.get("parent_id") or "")
            if parent:
                counts[parent] = counts.get(parent, 0) + 1
        raised = []
        if counts.get(cfg.category_id, 0) >= CROWDED_AT and self._may_ask("crowded"):
            from ..daemon import safe_list
            here = {str(c.get("id")) for c in channels
                    if str(c.get("parent_id") or "") == cfg.category_id}
            cutoff = self._wall() - IDLE_DAYS * 86400
            idle = [p for p in safe_list(self.stores.projects)
                    if p.discord_channel_id in here and not p.inbox and not p.archived
                    and self._last_activity(p) < cutoff]
            if idle:
                names = {str(c.get("id")): c.get("name") for c in channels}
                listed = ", ".join(f"#{names.get(p.discord_channel_id) or slug(p.name, p.id)}"
                                   for p in idle)
                self._enqueue({
                    "action": "archive_idle", "key": "crowded",
                    "projects": [[p.id, p.discord_channel_id] for p in idle],
                    "args": {"action": "archive", "channel": listed, "from": "Jarvis",
                             "to": self.category_name(cfg, self.archive_target(cfg)),
                             "why": (f"Move these {len(idle)} to Jarvis Archive? The Jarvis "
                                     f"category holds {counts[cfg.category_id]} of "
                                     f"{CHANNELS_MAX} channels and these projects have been "
                                     f"idle {IDLE_DAYS}+ days. The projects stay active.")}})
                raised.append("crowded")
        target = self.archive_target(cfg)
        if counts.get(target, 0) >= CROWDED_AT and self._may_ask("archive_full"):
            number = len(self._state.get("archive_overflow") or []) + 2
            name = f"Jarvis Archive {number}"
            self._enqueue({
                "action": "create_category", "key": "archive_full", "name": name,
                "args": {"action": "create_category", "channel": name,
                         "from": self.category_name(cfg, target), "to": name,
                         "why": (f"{self.category_name(cfg, target)} holds {counts[target]} "
                                 f"of {CHANNELS_MAX} channels; archived channels go to a new "
                                 f"category, {name}, from now on.")}})
            raised.append("archive_full")
        return raised

    def _may_ask(self, key) -> bool:
        """Never while one is open (persisted), and not for a day after one
        was answered (persisted too, so a restart does not re-ask)."""
        with self._state_lock:
            if any(job.get("key") == key for job in self._state["asks"]):
                return False
            asked = self._state["asked"].get(key)
        return asked is None or self._wall() - asked >= ASK_AGAIN_S

    # -- housekeeping asks (asker worker) -----------------------------------------

    def _enqueue(self, job) -> None:
        job = dict(job, id=uuid.uuid4().hex[:12])
        with self._state_lock:
            self._state["asks"].append(job)
            self._save_state()
        self._awaiting += 1
        self._jobs.put(job)

    def _settle(self, job) -> None:
        with self._state_lock:
            self._state["asks"] = [j for j in self._state["asks"] if j.get("id") != job.get("id")]
            if job.get("key"):
                self._state["asked"][job["key"]] = self._wall()
            self._save_state()

    def _ask_loop(self) -> None:
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                return
            decision = None
            try:
                decision = self._ask(job)
            except Exception as exc:
                LOG.warning("Discord housekeeping ask failed (%s)", type(exc).__name__)
            finally:
                self._awaiting = max(0, self._awaiting - 1)
                # A shutdown denies every open ask; that is not an answer, so
                # the job stays on disk and is asked again next start. An
                # approval is a real answer whenever it came (a shutdown never
                # approves): its action already ran, and replaying it after a
                # restart would do it twice — a second archive category.
                if decision == Decision.ALLOW or not self._stop.is_set():
                    self._settle(job)

    def ask_now(self, job) -> Decision:
        """Run one housekeeping job on the calling thread (the tests' path)."""
        self._awaiting += 1
        try:
            return self._ask(job)
        finally:
            self._awaiting = max(0, self._awaiting - 1)
            self._settle(job)

    def _ask(self, job) -> Decision:
        if self.approvals is None:
            return Decision.DENY
        request = ApprovalRequest(tool="discord_channel", args=dict(job["args"]),
                                  reason=str(job["args"].get("why") or ""),
                                  origin=ORIGIN, allowlistable=False)
        decision = self.approvals.ask(request)
        if decision != Decision.ALLOW:
            return decision                    # a deny or a timeout does nothing
        cfg = self.config()
        if cfg is None:
            return decision
        action = job["action"]
        if action == "rename":
            project = self._get(job["project_id"])
            # Only what was asked: if the name or the link moved since, nothing.
            if (project is not None and project.discord_channel_id == job["channel"]
                    and self.names_match(project, job["to"])):
                self.rename(project.id, job["channel"], job["to"])
        elif action == "archive_idle":
            target = self.archive_target(cfg)
            for project_id, channel in job["projects"]:
                project = self._get(project_id)
                if project is None or project.discord_channel_id != channel or project.archived:
                    continue
                with self._lock:
                    self._pace()
                if self.move(project_id, channel, target, BY_CROWDING):
                    self._note(channel, CROWDING_NOTE.format(where=self.category_name(cfg, target)))
        elif action == "create_category":
            with self._lock:
                try:
                    created = self.rest.create_channel(cfg.guild_id, job["name"], kind=CATEGORY)
                except Exception as exc:
                    self._failed("create_category", exc)
                    return decision
                with self._state_lock:
                    self._state["archive_overflow"].append(str(created))
                    self._save_state()
                self._wrote()
        return decision

    # -- the worker ----------------------------------------------------------

    def _listing_due(self) -> bool:
        return self._listing_retry_at is None or self._clock() >= self._listing_retry_at

    def _housekeep(self) -> None:
        """One tick of the worker's housekeeping. Each pass is marked done
        only once it succeeded: a reconcile or a crowding count whose channel
        listing failed is tried again (after RETRY_S), not lost until the
        next restart."""
        try:
            self.ensure_inbox()
        except Exception as exc:
            # Retried next tick; the rest of the tick still runs.
            LOG.warning("Discord linker could not link the Inbox (%s)", type(exc).__name__)
        cfg = self.config()
        if cfg is not None and self._reconciled_cfg != cfg and self._listing_due():
            # Armed first, so a pass that raises waits RETRY_S too.
            self._listing_retry_at = self._clock() + RETRY_S
            if self.reconcile() is not None:
                self._reconciled_cfg = cfg
                self._listing_retry_at = None
        if self._refreshed_at is None or self._clock() - self._refreshed_at >= FACTS_TTL_S * 10:
            if cfg is not None:
                self.refresh_permissions()
                self._refreshed_at = self._clock()
        self._retry_pending()
        if self._crowding_due and cfg is not None and self._listing_due():
            # Cleared before the count, so a move made meanwhile (an owner's
            # /channel on the HTTP thread) re-arms it; put back if the
            # listing failed, so the count is not lost until the next move.
            self._crowding_due = False
            self._listing_retry_at = self._clock() + RETRY_S
            raised = None
            try:
                raised = self.check_crowding()
            finally:
                if raised is None:
                    self._crowding_due = True
                else:
                    self._listing_retry_at = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._housekeep()
            except Exception as exc:
                LOG.warning("Discord linker housekeeping failed (%s)", type(exc).__name__)
            try:
                record = self.subscription.get(timeout=TICK_S)
            except queue.Empty:
                continue
            try:
                if record.get("kind") == "shutdown":
                    return
                self._handle(record)
            except Exception as exc:
                LOG.warning("Discord linker could not handle an event (%s)", type(exc).__name__)
            finally:
                self.subscription.task_done()


class SimpleProject:
    """Just enough of a project for `names_match` on a name it no longer has."""

    def __init__(self, name, project_id):
        self.name, self.id = name, project_id
