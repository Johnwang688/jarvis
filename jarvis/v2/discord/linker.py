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
the owner has not answered never holds up the owner's own renames.

**Never a delete (D3).** A channel is created, renamed, moved between the
Jarvis categories, posted in, or left alone. `DiscordRest` has no delete
method, and a permanently deleted project's channel is kept with a note.
Moving never sends `lock_permissions` or `permission_overwrites`.

**Rate limits.** Discord allows two renames per channel per ten minutes. A
rename that meets a long 429 is kept as *pending* and retried when Discord
said; the HUD shows "rename pending" and `GET /discord` counts them. Creates
(backfill, crowding moves) are paced at one a second.

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

from ..approvals import ApprovalRequest
from ..model import TERMINAL_STATES, to_json
from ..provider import Decision
from ..stores import StoreError, _write_bytes
from . import guild as guildmod
from . import perms
from .rest import (MISSING_ACCESS, MISSING_PERMISSIONS, UNKNOWN_CHANNEL, DiscordHTTPError,
                   describe)

LOG = logging.getLogger(__name__)

KINDS = frozenset({"project_updated", "project_archived", "project_restored",
                   "project_deleted"})
SLUG_MAX = 90
CHANNELS_MAX = 50              # Discord's limit per category
CROWDED_AT = 45
IDLE_DAYS = 30
VIEW_TTL_S = 60.0
FACTS_TTL_S = 60.0
READ_TIMEOUT_S = 5.0
CREATE_INTERVAL = 1.0
TICK_S = 5.0
ASK_AGAIN_S = 24 * 3600.0      # after a denied or unanswered housekeeping ask
ORIGIN = "Jarvis housekeeping"
TEXT, CATEGORY = 0, 4
STATES = ("linked_ok", "unlinked", "not_found", "no_access", "wrong_guild",
          "missing_permissions", "folder_missing", "unconfigured", "unreachable")
_SNOWFLAKE = re.compile(r"^[0-9]{5,24}$")


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
        self._views: dict[str, tuple[float, dict]] = {}
        self._facts: tuple[float, dict] | None = None
        self._perm_status: dict | None = None
        self._awaiting = 0
        self._asked: dict[str, float] = {}    # housekeeping key -> wall time asked
        self._last_create = None
        self._linked_cfg = None
        self._crowding_due = True
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

    # -- persisted state (overflow categories, pending renames) -------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        overflow = [c for c in data.get("archive_overflow") or [] if isinstance(c, str)]
        renames = {k: v for k, v in (data.get("pending_renames") or {}).items()
                   if isinstance(v, dict)}
        return {"archive_overflow": overflow, "pending_renames": renames}

    def _save_state(self) -> None:
        try:
            _write_bytes(self._state_path, json.dumps(self._state, sort_keys=True).encode())
        except (StoreError, OSError) as exc:
            LOG.warning("Discord linker state not saved (%s)", type(exc).__name__)

    def archive_target(self, cfg) -> str:
        """Where an archived channel goes: the newest overflow category, else
        Jarvis Archive."""
        overflow = self._state.get("archive_overflow") or []
        return overflow[-1] if overflow else cfg.archive_category_id

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
        pending = len(self._state.get("pending_renames") or {})
        if cfg is None:
            state, reason = "unconfigured", "run `jarvis auth discord-guild` to set up the server"
        elif pending:
            state, reason = "degraded", f"{pending} channel rename(s) pending (Discord rate limit)"
        elif self.last_error and self._wall() - self.last_error.get("at", 0) < 600:
            error = self.last_error
            state, reason = "degraded", (f"last {error['op']} failed (HTTP {error['status']}, "
                                         f"code {error['code']})")
        else:
            state, reason = "ok", ""
        return {
            "guild": {"configured": cfg is not None, "id": cfg.guild_id if cfg else None},
            "linker": {"state": state, "reason": reason, "pending_renames": pending,
                       "awaiting_approval": self._awaiting},
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

    def _taken(self, project_id) -> set[str]:
        from ..daemon import safe_list
        names = {"ungrouped"}
        for other in safe_list(self.stores.projects):
            if other.id != project_id and other.discord_channel_id and not other.inbox:
                names.add(slug(other.name, other.id))
        return names

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
        self.invalidate(project_id)
        self.daemon.bus.publish({"kind": "project_updated", "project_id": project.id,
                                 "data": {"project_id": project.id,
                                          "changed": ["discord_channel_id"],
                                          "project": to_json(project), "by": by,
                                          "previous": {"discord_channel_id": previous}}})
        return project

    def _rest_refusal(self, op, exc) -> LinkError:
        self._failed(op, exc)
        facts = describe(exc)
        if facts["status"] is None:
            return LinkError(502, f"Discord could not be reached ({facts['error']})")
        if facts["status"] == 429:
            return LinkError(503, "Discord is rate-limiting this; try again in a minute")
        why = {MISSING_ACCESS: "the bot cannot see it",
               MISSING_PERMISSIONS: "the bot is missing a permission"}.get(facts["code"], "")
        return LinkError(502, f"Discord refused {op} (HTTP {facts['status']}, code "
                              f"{facts['code']}){': ' + why if why else ''}")

    def create(self, project_id: str, *, by: str = "owner") -> dict:
        """A new channel in the Jarvis category, named by `slug`, topic set."""
        with self._lock:
            cfg = self._require_config()
            project = self._project(project_id)
            self._refuse_inbox(project)
            self._refuse_unlinkable(project)
            if project.discord_channel_id:
                raise LinkError(409, f"project {project.name} already has a channel; "
                                     "unlink it first")
            self._pace()
            name = slug(project.name, project.id, self._taken(project.id))
            try:
                channel_id = self.rest.create_channel(cfg.guild_id, name, kind=TEXT,
                                                      parent_id=cfg.category_id,
                                                      topic=topic(project))
            except Exception as exc:
                raise self._rest_refusal("create_channel", exc) from None
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
            self.validate_link(project, channel_id)
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
                raise self._rest_refusal("get_channel", exc) from None
            parent = str(current.get("parent_id") or "")
            archives = {cfg.archive_category_id, *(self._state.get("archive_overflow") or [])}
            if (archive and parent in archives) or (not archive and parent == cfg.category_id):
                return {"moved": False, "category": self.category_name(cfg, parent)}
            target = self.archive_target(cfg) if archive else cfg.category_id
            try:
                self.rest.modify_channel(channel_id, parent_id=target)
            except Exception as exc:
                raise self._rest_refusal("archive_channel" if archive else "restore_channel",
                                         exc) from None
            self.invalidate(project.id)
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
        self._linked_cfg = cfg
        if inbox.discord_channel_id == cfg.ungrouped_channel_id \
                and inbox.discord_channel_origin == "created":
            return False
        with self._lock:
            self._set_channel(inbox.id, cfg.ungrouped_channel_id, "created", by="owner")
        return True

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
                self._renamed(project_id, data)
        elif kind == "project_archived":
            self._archived(cfg, project_id)
        elif kind == "project_restored":
            self._restored(cfg, project_id)
        elif kind == "project_deleted":
            channel = data.get("discord_channel_id")
            if channel:
                name = str(data.get("name") or "")
                self._note(channel, f"Project {name} was permanently deleted in the HUD. "
                                    "This channel is kept; it is no longer linked to Jarvis.")

    def _note(self, channel, text) -> None:
        try:
            self.rest.post(channel, content=text[:2000], silent=True)
        except Exception as exc:
            self._failed("post_note", exc)

    def _renamed(self, project_id, data) -> None:
        try:
            project = self.stores.projects.get(project_id)
        except StoreError:
            return
        if project is None or not project.discord_channel_id or project.inbox:
            return
        to = slug(project.name, project.id, self._taken(project.id))
        previous = (data.get("previous") or {}).get("name") or ""
        if data.get("by") == "owner":
            self.rename(project.id, project.discord_channel_id, to)
            return
        # Not the owner's own action: ask first (D4).
        before = slug(previous, project.id) if previous else ""
        self._enqueue({
            "action": "rename", "project_id": project.id,
            "channel": project.discord_channel_id, "to": to,
            "args": {"action": "rename", "channel": project.discord_channel_id,
                     "from": before, "to": to,
                     "why": f"project {project.name} was renamed outside the HUD"}})

    def rename(self, project_id, channel_id, name) -> bool:
        """Rename now; a long 429 keeps it pending, retried when Discord said."""
        with self._lock:
            try:
                self.rest.modify_channel(channel_id, name=name)
            except DiscordHTTPError as exc:
                if exc.status == 429:
                    delay = float(exc.retry_after or 60.0)
                    self._state["pending_renames"][str(channel_id)] = {
                        "project_id": project_id, "name": name, "due": self._wall() + delay}
                    self._save_state()
                    self.invalidate(project_id)
                    self._failed("rename_channel", exc)
                    return False
                self._failed("rename_channel", exc)
                return False
            except Exception as exc:
                self._failed("rename_channel", exc)
                return False
            if self._state["pending_renames"].pop(str(channel_id), None) is not None:
                self._save_state()
            self.invalidate(project_id)
            return True

    def _retry_renames(self) -> None:
        now = self._wall()
        for channel, row in list((self._state.get("pending_renames") or {}).items()):
            if row.get("due", 0) > now:
                continue
            project = None
            try:
                project = self.stores.projects.get(row.get("project_id") or "")
            except StoreError:
                pass
            if project is None or project.discord_channel_id != channel:
                self._state["pending_renames"].pop(channel, None)
                self._save_state()
                continue
            # The newest name wins: a rename typed while one was pending.
            self.rename(project.id, channel, slug(project.name, project.id,
                                                  self._taken(project.id)))

    def _archived(self, cfg, project_id) -> None:
        try:
            project = self.stores.projects.get(project_id)
        except StoreError:
            return
        if project is None or not project.discord_channel_id or project.inbox:
            return
        target = self.archive_target(cfg)
        with self._lock:
            try:
                self.rest.modify_channel(project.discord_channel_id, parent_id=target)
            except Exception as exc:
                self._failed("archive_channel", exc)
                return
        self._note(project.discord_channel_id,
                   f"Project {project.name} was archived in the HUD. This channel moved to "
                   f"{self.category_name(cfg, target)} and is kept; restore the project in "
                   "the HUD to work here again.")
        self._crowding_due = True

    def _restored(self, cfg, project_id) -> None:
        try:
            project = self.stores.projects.get(project_id)
        except StoreError:
            return
        if project is None or not project.discord_channel_id or project.inbox:
            return
        channel_id = project.discord_channel_id
        name = slug(project.name, project.id, self._taken(project.id))
        current = None
        try:
            current = self.rest.get_channel(channel_id, timeout=READ_TIMEOUT_S, patient=False)
        except Exception as exc:
            self._failed("get_channel", exc)
        with self._lock:
            try:
                if current is not None and current.get("name") == name:
                    self.rest.modify_channel(channel_id, parent_id=cfg.category_id)
                else:
                    # PR #4 may have renumbered the name on restore.
                    self.rest.modify_channel(channel_id, parent_id=cfg.category_id, name=name)
            except Exception as exc:
                self._failed("restore_channel", exc)
                return
        self._note(channel_id, "Restored.")
        self._crowding_due = True

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
        either reaches 45 of 50. -> the asks raised (for tests and logs)."""
        cfg = self.config()
        if cfg is None:
            return []
        try:
            channels = self.rest.guild_channels(cfg.guild_id, timeout=READ_TIMEOUT_S,
                                                patient=False)
        except Exception as exc:
            self._failed("list_channels", exc)
            return []
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
                    "projects": [(p.id, p.discord_channel_id) for p in idle],
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
        asked = self._asked.get(key)
        return asked is None or (asked >= 0 and self._wall() - asked >= ASK_AGAIN_S)

    # -- housekeeping asks (asker worker) -----------------------------------------

    def _enqueue(self, job) -> None:
        key = job.get("key")
        if key:
            self._asked[key] = -1.0           # open: never asked twice at once
        self._awaiting += 1
        self._jobs.put(job)

    def _ask_loop(self) -> None:
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._ask(job)
            except Exception as exc:
                LOG.warning("Discord housekeeping ask failed (%s)", type(exc).__name__)
            finally:
                self._awaiting = max(0, self._awaiting - 1)
                if job.get("key"):
                    self._asked[job["key"]] = self._wall()

    def ask_now(self, job) -> Decision:
        """Run one housekeeping job on the calling thread (the tests' path)."""
        self._awaiting += 1
        try:
            return self._ask(job)
        finally:
            self._awaiting = max(0, self._awaiting - 1)
            if job.get("key"):
                self._asked[job["key"]] = self._wall()

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
            project = self.stores.projects.get(job["project_id"])
            # Only what was asked: if the name or the link moved since, nothing.
            if (project is not None and project.discord_channel_id == job["channel"]
                    and slug(project.name, project.id, self._taken(project.id)) == job["to"]):
                self.rename(project.id, job["channel"], job["to"])
        elif action == "archive_idle":
            target = self.archive_target(cfg)
            for project_id, channel in job["projects"]:
                project = self.stores.projects.get(project_id)
                if project is None or project.discord_channel_id != channel:
                    continue
                with self._lock:
                    self._pace()
                    try:
                        self.rest.modify_channel(channel, parent_id=target)
                    except Exception as exc:
                        self._failed("archive_channel", exc)
                        continue
                self.invalidate(project_id)
                self._note(channel, f"Moved to {self.category_name(cfg, target)} to make room "
                                    "(you approved it). The project is still active.")
        elif action == "create_category":
            with self._lock:
                try:
                    created = self.rest.create_channel(cfg.guild_id, job["name"], kind=CATEGORY)
                except Exception as exc:
                    self._failed("create_category", exc)
                    return decision
                self._state["archive_overflow"].append(str(created))
                self._save_state()
        return decision

    # -- the worker ----------------------------------------------------------

    def _run(self) -> None:
        refreshed_at = None
        while not self._stop.is_set():
            try:
                self.ensure_inbox()
                if refreshed_at is None or self._clock() - refreshed_at >= FACTS_TTL_S * 10:
                    if self.config() is not None:
                        self.refresh_permissions()
                        refreshed_at = self._clock()
                self._retry_renames()
                if self._crowding_due and self.config() is not None:
                    self._crowding_due = False
                    self.check_crowding()
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
