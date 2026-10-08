"""`/project new|link|unlink|channel` and `/channel archive|restore` (B2).

Plan §5 and decisions D2, D4, O3–O5, B11. Every command here was typed by the
owner — the interaction gate (`interactions.py`) has already checked that —
and what it may do follows from that:

* **`/project new`** makes a project, its folder and its channel. The folder
  rules are `jarvis/v2/folders.py`. An existing empty folder is used without
  asking (O5); a missing one, or one with something in it, is asked first as a
  broker request — `ApprovalRequest(tool="project_folder",
  args={action, path, name}, allowlistable=False)` — so the owner sees the
  **exact path** with Approve and Deny, in the channel the command was typed in
  and as a HUD card. One-shot, never Always, never by voice (a typed or tapped
  answer only), and a timeout denies. After the yes the check runs again; if
  the folder changed meanwhile the owner is asked again about what is there
  now. Then the folder (`make_project_folder`, called from here and nowhere
  else), the project (`projects.create_project`, PR #4's numbering) and the
  channel: a new one under Jarvis when asked from the DM, *this* channel when
  asked from an unlinked one. A linked channel refuses.
* **`/project link`** links the unlinked channel it is typed in (B1's
  `validate_link`), at once — the owner's own action (D4).
* **`/project unlink`** asks Approve/Deny first, then forgets the link. The
  channel is kept: nothing here can delete one (D3).
* **`/project channel`** makes a channel for a project that has none.
* **`/channel archive|restore`** moves this project's channel to Jarvis Archive
  and back, at once (D4). The project stays active.

Archiving or deleting a **project** is not here and never will be (B11): those
stay in the HUD.

*B2b, not here yet:* `project_propose`, the model path (decisions O3's Claude
Sonnet/Opus rule). It needs the peers plan's phase 0.
"""
# TODO(B2b): `project_propose(name, folder="")` — a Claude Sonnet/Opus chat
# raises the same `project_folder` confirmation through a daemon route that
# identifies the calling chat (peers phase 0: `runtime.caller()` and per-session
# jarvis-mcp tokens) and refuses the fast path, Codex, an unresolved model and a
# Claude model reached through OpenRouter. It reuses `ProjectCommands._make_folder`;
# the folder is still made here, by code, after the owner's yes.
from __future__ import annotations

import logging
import unicodedata

from ..approvals import ApprovalRequest
from ..provider import Decision
from .. import folders
from .linker import LinkError

LOG = logging.getLogger(__name__)

NAME_MAX = 100
MAX_ASKS = 3
ORIGIN_NEW = "Discord: new project"
ORIGIN_UNLINK = "Discord: /project unlink"
MODAL_ID = "jv:project:new"
NOT_SET_UP = "Discord is not set up: run `jarvis auth discord-guild` first."


def _clean_name(value) -> str:
    return " ".join(str(value or "").split())


def _bad_name(name: str) -> str | None:
    if not name:
        return "A project needs a name."
    if len(name) > NAME_MAX:
        return f"A project name is at most {NAME_MAX} characters."
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in name):
        return "A project name cannot contain control characters."
    return None


def new_project_modal() -> tuple[str, str, list]:
    """(custom id, title, components) for `/project new` with nothing filled in."""
    return MODAL_ID, "New project", [
        {"type": 1, "components": [{
            "type": 4, "custom_id": "name", "label": "Name", "style": 1,
            "min_length": 1, "max_length": NAME_MAX, "required": True}]},
        {"type": 1, "components": [{
            "type": 4, "custom_id": "folder", "label": "Folder (blank: ~/jarvis-work/<name>)",
            "style": 1, "max_length": 4000, "required": False}]},
    ]


class ProjectCommands:
    """The handlers. `surface` is the DiscordRouter; its `linker` (B1) is set
    by `DiscordSurface.start` and is None in a surface without one."""

    def __init__(self, surface):
        self.surface = surface

    # -- shared ------------------------------------------------------------

    @property
    def linker(self):
        return getattr(self.surface, "linker", None)

    def _configured(self) -> bool:
        linker = self.linker
        return linker is not None and linker.config() is not None

    def _project(self, value):
        """A live project by id or name (autocomplete is a suggestion only)."""
        return self.surface.router.place(str(value or "").strip())

    def _ask(self, request) -> Decision:
        try:
            return Decision(self.surface.approvals.ask(request))
        except Exception as exc:                 # a broker that breaks never grants
            LOG.warning("Discord project approval failed (%s)", type(exc).__name__)
            return Decision.DENY

    # -- /project new ------------------------------------------------------

    def new(self, name, folder, reply, place) -> None:
        where, project, _task = place
        if where in ("project", "task"):
            reply.refuse(f"This channel is already linked to project {project.name}. Run "
                         "`/project new` in your DM with Jarvis or in an unlinked channel.")
            return
        name, folder = _clean_name(name), str(folder or "").strip()
        if not name and not folder:
            reply.modal(*new_project_modal())
            return
        if not name:
            try:
                name = folders.resolve_input(folder).name
            except folders.FolderRefused as exc:
                reply.refuse(f"I can't use that folder: {exc}. Nothing was made.")
                return
        refusal = _bad_name(name)
        if refusal:
            reply.refuse(refusal)
            return
        if where == "other" and not self._configured():
            reply.refuse(NOT_SET_UP)
            return
        # No folder: the *name* picks it, slugged, under ~/jarvis-work — even a
        # name with a `/` in it never becomes a path by accident.
        raw = folder or folders.folder_slug(name)
        if not raw:
            reply.refuse(f"`{_short(name)}` has no letters or digits to name a folder after; "
                         "give `folder:` too.")
            return
        try:
            check = folders.check_project_folder(raw, taken_roots=self._taken_roots())
        except folders.FolderRefused as exc:
            reply.refuse(f"I can't use that folder: {exc}. Nothing was made.")
            return
        reply.defer()
        channel_id = reply.channel_id
        if where == "other":
            try:
                self.linker.validate_channel(channel_id)
            except LinkError as exc:
                reply.send(f"I can't link this channel: {exc}. Nothing was made.")
                return
        path = self._make_folder(check, name, channel_id, reply)
        if path is None:
            return
        from ..daemon import APIError, DaemonError
        from ..projects import create_project
        from ..stores import StoreError
        try:
            created = create_project(self.surface.daemon, name, str(path))
        except (APIError, DaemonError, StoreError) as exc:
            reply.send(f"The folder `{path}` is ready, but the project was not made: {exc}")
            return
        reply.send(f"Made project **{created.name}** at `{path}`. "
                   f"{self._channel_for(created, where, channel_id)}")

    def _taken_roots(self) -> list[str]:
        from ..daemon import safe_list
        return [p.root for p in safe_list(self.surface.stores.projects)]

    def _make_folder(self, check, name, channel_id, reply):
        """Ask when needed, make the folder, and ask again if it changed after
        the yes. -> the path, or None once the owner has been told why not."""
        approval, forced = None, False
        for _ in range(MAX_ASKS):
            if check.needs_confirmation or forced:
                verb = "Create" if check.action == "create" else "Use"
                reply.send(f"{verb} `{check.path}` ({check.describe()}) for project "
                           f"**{name}**? Approve or Deny on the request below.")
                request = ApprovalRequest(
                    tool=folders.TOOL, args=check.approval_args(name),
                    reason=f"{verb} {check.path} as the folder of project {name}",
                    origin=ORIGIN_NEW, allowlistable=False, discord_channel_id=channel_id)
                decision = self._ask(request)
                if decision is not Decision.ALLOW:
                    reply.send(f"Not approved, so nothing was made (`{check.path}`).")
                    return None
                approval = folders.approved(request, decision)
            try:
                return folders.make_project_folder(check, approval)
            except folders.FolderChanged as exc:
                # Re-read the project roots too: what appeared may be another
                # project's folder, made while this one was being decided.
                try:
                    check = folders.check_project_folder(exc.check.raw,
                                                         taken_roots=self._taken_roots())
                except folders.FolderRefused as refused:
                    reply.send(f"`{exc.check.path}` changed while you were deciding, and "
                               f"now {refused}. Nothing was made.")
                    return None
                approval, forced = None, True
                reply.send(f"`{check.path}` changed while you were deciding: it is now "
                           f"{check.describe()}. Asking again.")
            except folders.FolderRefused as exc:
                reply.send(f"I can't use that folder any more: {exc}. Nothing was made.")
                return None
        reply.send(f"`{check.path}` kept changing, so nothing was made.")
        return None

    def _channel_for(self, project, where, channel_id) -> str:
        """Link this channel (from an unlinked one) or make one (from the DM).
        The project exists either way; a channel failure is said, not raised."""
        linker = self.linker
        if where == "other":
            try:
                linker.link(project.id, channel_id)
            except LinkError as exc:
                return (f"This channel was not linked ({exc}); run `/project link` here to "
                        "try again.")
            return "This channel is now its channel."
        if not self._configured():
            return "No channel yet: the Discord server is not set up."
        try:
            view = linker.create(project.id)
        except LinkError as exc:
            return f"No channel yet ({exc}); `/project channel` tries again."
        made = view.get("channel_id")
        return f"Its channel is <#{made}>." if made else "Its channel was made."

    # -- /project link, unlink, channel --------------------------------------

    def link(self, value, reply, place) -> None:
        where, here, _task = place
        if where in ("project", "task"):
            reply.refuse(f"This channel is already linked to project {here.name}. Run "
                         "`/project unlink` here first.")
            return
        if where == "dm":
            reply.refuse("Run `/project link` inside the server channel you want to link.")
            return
        target = self._project(value)
        if target is None:
            reply.refuse(f"I don't know a live project `{_short(value)}`.")
            return
        if not self._configured():
            reply.refuse(NOT_SET_UP)
            return
        reply.defer()
        try:
            self.linker.link(target.id, reply.channel_id)
        except LinkError as exc:
            reply.send(f"Not linked: {exc}.")
            return
        reply.send(f"Linked this channel to project **{target.name}**.")

    def unlink(self, reply, place) -> None:
        where, project, _task = place
        if where != "project":
            reply.refuse("Run `/project unlink` in the project's own channel.")
            return
        if project.inbox:
            reply.refuse("#ungrouped belongs to the Inbox; only re-running "
                         "`jarvis auth discord-guild` changes it.")
            return
        if self.linker is None:
            reply.refuse(NOT_SET_UP)
            return
        channel_id = reply.channel_id
        reply.defer()
        reply.send(f"Unlink this channel from project **{project.name}**? The channel is "
                   "kept. Approve or Deny on the request below.")
        request = ApprovalRequest(
            tool="discord_channel",
            args={"action": "unlink", "project": project.name, "channel": channel_id},
            reason=f"unlink this channel from project {project.name}",
            origin=ORIGIN_UNLINK, allowlistable=False, discord_channel_id=channel_id)
        if self._ask(request) is not Decision.ALLOW:
            reply.send("Not approved; the link is unchanged.")
            return
        fresh = self.surface.stores.projects.get(project.id)
        if fresh is None or fresh.discord_channel_id != channel_id:
            reply.send("The link changed while you were deciding; nothing was done.")
            return
        try:
            self.linker.unlink(project.id)
        except LinkError as exc:
            reply.send(f"Not unlinked: {exc}.")
            return
        reply.send(f"Unlinked from project **{project.name}**. This channel is kept.")

    def channel(self, value, reply, place) -> None:
        target = self._project(value)
        if target is None:
            reply.refuse(f"I don't know a live project `{_short(value)}`.")
            return
        if target.discord_channel_id:
            reply.refuse(f"Project {target.name} already has a channel "
                         f"(<#{target.discord_channel_id}>).")
            return
        if not self._configured():
            reply.refuse(NOT_SET_UP)
            return
        reply.defer()
        try:
            view = self.linker.create(target.id)
        except LinkError as exc:
            reply.send(f"No channel made: {exc}.")
            return
        reply.send(f"Made <#{view.get('channel_id')}> for project **{target.name}**.")

    # -- /channel archive|restore ----------------------------------------------

    def move(self, archive: bool, reply, place) -> None:
        _where, project, _task = place
        if project is None:
            reply.refuse("Run this in a project's channel.")
            return
        if project.inbox:
            reply.refuse("#ungrouped belongs to the Inbox and stays where it is.")
            return
        if not self._configured():
            reply.refuse(NOT_SET_UP)
            return
        reply.defer()
        try:
            result = self.linker.move_channel(project.id, archive=archive)
        except LinkError as exc:
            reply.send(f"Not moved: {exc}.")
            return
        where = result.get("category") or ("Jarvis Archive" if archive else "Jarvis")
        if not result.get("moved"):
            reply.send(f"This channel is already in {where}.")
        elif archive:
            reply.send(f"Moved this channel to {where}. Project **{project.name}** stays "
                       "active, and its threads keep working.")
        else:
            reply.send(f"Moved this channel back to {where}.")


def _short(value, limit: int = 40) -> str:
    text = " ".join(str(value or "").replace("`", "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
