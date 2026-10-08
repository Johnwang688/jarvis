"""Slash commands, autocomplete, buttons and modals (slash plan §3–§4, S1).

An interaction arrives over the same gateway as a message (INTERACTION_CREATE,
no intent needed) and is handled on its own worker thread. Every one passes
the same gate before anything is read:

1. **Our application.** `application_id` must equal the one READY reported.
   Anything else is dropped without an answer.
2. **The owner, every time — autocomplete included.** Autocomplete would
   otherwise hand project names, task briefs and approval commands to anyone
   who shares a server with the bot. A stranger's command gets a private
   "only the owner", their autocomplete an empty list, their button a refusal.
3. **A place Jarvis answers in.** `context` must be 0 (a guild) or 1 (the bot
   DM) — never 2, a private channel — and, once PR B1 configures a guild, the
   guild must be that one. Then the channel must be a Jarvis place for the
   command (`commands.Cmd.places`); Discord cannot hide a command per channel,
   so places are enforced here, on the server.

**The 3-second rule.** A refusal that needs only reads is answered at once
(type 4, ephemeral); autocomplete is answered at once (type 8). Everything else
sends type 5 (deferred) **before** its first store write or control call, then
edits `@original`; past 14 minutes, or once the token is dead (401/404), the
reply becomes an ordinary channel post.

**Never logged:** the interaction payload and its token. An exception is
logged by class only.
"""
from __future__ import annotations

import logging
import time

from .. import commands as registry
from ..commands import Context, SkillRefused, check_skill, completions, open_questions
from ..control import ControlError
from ..model import TERMINAL_STATES, ProviderName, TaskState
from .render import APPROVE_PREFIX, DENY_PREFIX
from .rest import DiscordError, DiscordHTTPError

LOG = logging.getLogger(__name__)

APPLICATION_COMMAND, COMPONENT, AUTOCOMPLETE, MODAL_SUBMIT = 2, 3, 4, 5
MESSAGE, DEFERRED, DEFERRED_UPDATE, UPDATE, CHOICES, MODAL = 4, 5, 6, 7, 8, 9
EPHEMERAL = 64
SUB_COMMAND = 1
TOKEN_WINDOW_S = 14 * 60            # Discord's is 15; leave a minute of margin
MODAL_PREFIX = "jv:task:"

OWNER_ONLY = "Only the owner can use Jarvis."
NOT_HERE = "This channel isn't a Jarvis place."
WRONG_CONTEXT = "Jarvis commands only work in your DM with Jarvis or in its server."
_TYPES = {"string": (3, str), "integer": (4, int), "boolean": (5, bool)}


class InteractionReply:
    """A `Reply` over one interaction token. The token never leaves this
    object except into `DiscordRest`, which keeps it out of every message."""

    def __init__(self, rest, app_id, interaction, *, fallback, clock=time.monotonic,
                 window_s: float = TOKEN_WINDOW_S):
        self.rest = rest
        self.app_id = str(app_id)
        self.interaction_id = str(interaction.get("id") or "")
        self._token = str(interaction.get("token") or "")
        self.channel_id = str(interaction.get("channel_id")
                              or (interaction.get("channel") or {}).get("id") or "")
        self._clock = clock
        self.deadline = clock() + window_s
        self._fallback = fallback
        self.responded = False
        self.deferred = False
        self.ephemeral = False
        self.original_used = False
        self.said = False
        self.dead = False

    def __repr__(self) -> str:                  # never the token
        return f"InteractionReply(channel={self.channel_id!r})"

    def _callback(self, kind, data=None) -> bool:
        self.responded = True
        try:
            self.rest.callback(self.interaction_id, self._token, kind, data)
            return True
        except DiscordError as exc:
            # Past three seconds, or a token Discord no longer honours: the
            # rest of this reply goes to the channel instead.
            self.dead = True
            LOG.warning("Discord interaction callback failed (%s %s)", type(exc).__name__,
                        getattr(exc, "status", ""))
            return False

    def refuse(self, text: str) -> None:
        if not self.responded:
            self._callback(MESSAGE, {"content": text, "flags": EPHEMERAL,
                                     "allowed_mentions": {"parse": []}})
            return
        self.send(text, ephemeral=True)

    def defer(self, *, ephemeral: bool = False) -> None:
        if self.responded:
            return
        self.ephemeral = ephemeral
        self.deferred = self._callback(DEFERRED, {"flags": EPHEMERAL} if ephemeral else None)

    def acknowledge_update(self) -> None:
        """Type 6: a button press acknowledged, its message left as it is."""
        if not self.responded:
            self._callback(DEFERRED_UPDATE)

    def choices(self, rows) -> None:
        self._callback(CHOICES, {"choices": [{"name": n, "value": v} for n, v in rows][:25]})

    def modal(self, custom_id: str, title: str, components: list) -> None:
        self._callback(MODAL, {"custom_id": custom_id, "title": title,
                               "components": components})

    def send(self, content=None, *, embed=None, files=(), components=None, ephemeral=False):
        if not self.responded:
            self.defer(ephemeral=ephemeral)
        self.said = True
        if self.dead or self._clock() >= self.deadline:
            return self._channel(content, embed, files, components)
        try:
            if self.deferred and not self.original_used:
                self.original_used = True
                self.rest.edit_original(self.app_id, self._token, content=content, embed=embed,
                                        files=files, components=components)
                return "@original"
            return self.rest.followup(self.app_id, self._token, content=content, embed=embed,
                                      files=files, components=components,
                                      ephemeral=ephemeral or self.ephemeral)
        except DiscordHTTPError as exc:
            if exc.status in (401, 404):
                self.dead = True
                return self._channel(content, embed, files, components)
            LOG.warning("Discord interaction reply failed (HTTP %s)", exc.status)
        except DiscordError as exc:
            LOG.warning("Discord interaction reply failed (%s)", type(exc).__name__)
            self.dead = True
            return self._channel(content, embed, files, components)
        return None

    def finish(self) -> None:
        """A deferred reply that never said anything still has to stop
        "thinking…" on the owner's phone."""
        if self.deferred and not self.said and not self.dead \
                and self._clock() < self.deadline:
            self.send("Done.")

    def _channel(self, content, embed, files, components):
        if not self.channel_id:
            return None
        return self._fallback(self.channel_id, content, files=files, embed=embed,
                              components=components)


class InteractionRouter:
    """Gate, place, then one handler per command. Verbs live in the surface."""

    def __init__(self, surface, *, clock=time.monotonic):
        self.surface = surface
        self.clock = clock

    # -- entry -------------------------------------------------------------

    def handle(self, interaction) -> None:
        try:
            self._handle(interaction)
        except Exception as exc:
            LOG.warning("Discord interaction handling failed (%s)", type(exc).__name__)

    def _handle(self, interaction) -> None:
        if not isinstance(interaction, dict):
            return
        kind = interaction.get("type")
        if kind not in (APPLICATION_COMMAND, COMPONENT, AUTOCOMPLETE, MODAL_SUBMIT):
            return
        app = str(self.surface.application_id or "")
        if not app or str(interaction.get("application_id") or "") != app:
            LOG.info("Discord interaction for another application dropped")
            return
        reply = InteractionReply(self.surface.rest, app, interaction,
                                 fallback=self.surface._post, clock=self.clock)
        refusal = self._gate(interaction)
        if refusal is None:
            guild = interaction.get("guild_id")
            where, project, task = self.surface._locate(
                {"guild_id": str(guild)} if guild else {}, reply.channel_id)
            if where == "archived":
                refusal = self.surface.archived_text(project)
            elif where not in registry.JARVIS_PLACES:
                refusal = NOT_HERE
        if refusal is not None:
            if kind == AUTOCOMPLETE:
                reply.choices([])
            else:
                reply.refuse(refusal)
            return
        if where == "dm":
            self.surface._remember_dm(reply.channel_id)
        place = (where, project, task)
        if kind == AUTOCOMPLETE:
            self._autocomplete(interaction, reply, place)
            return
        try:
            if kind == APPLICATION_COMMAND:
                self._command(interaction, reply, place)
            elif kind == COMPONENT:
                self._component(interaction, reply)
            elif kind == MODAL_SUBMIT:
                self._modal(interaction, reply, place)
        except ControlError as exc:
            reply.send(str(exc))
        finally:
            reply.finish()

    def _gate(self, interaction) -> str | None:
        owner = str(self.surface.owner_id or "")
        member = interaction.get("member") or {}
        user = member.get("user") or interaction.get("user") or {}
        if not owner or str(user.get("id") or "") != owner or user.get("bot"):
            return OWNER_ONLY
        context = interaction.get("context")
        guild = interaction.get("guild_id")
        if context not in (0, 1) or (context == 0) != bool(guild):
            return WRONG_CONTEXT
        configured = getattr(self.surface, "guild_id", None)
        if guild and configured and str(guild) != str(configured):
            return WRONG_CONTEXT
        return None

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _spec(data):
        """-> (Cmd, raw options, display name) or (None, None, name)."""
        name = str(data.get("name") or "")
        options = data.get("options") or []
        cmd = registry.BY_NAME.get(name)
        if cmd is None:
            return None, None, name
        if not cmd.subcommands:
            return cmd, options, name
        if len(options) != 1 or options[0].get("type") != SUB_COMMAND:
            return None, None, name
        sub = registry.find(name, str(options[0].get("name") or ""))
        label = f"{name} {options[0].get('name')}"
        return (sub, options[0].get("options") or [], label) if sub else (None, None, label)

    @staticmethod
    def _values(spec, options):
        """-> (values, None) or (None, refusal). Autocomplete is never trusted
        to have shaped these: names, types, lengths and presence are checked."""
        known = {o.name: o for o in spec.options}
        values = {}
        for raw in options:
            opt = known.get(raw.get("name"))
            if opt is None:
                return None, (f"`/{spec.name}` has no option `{raw.get('name')}` — Discord's "
                              "command list may be out of date.")
            discord_type, python_type = _TYPES[opt.kind]
            value = raw.get("value")
            if raw.get("type") not in (None, discord_type) or not isinstance(value, python_type) \
                    or (python_type is int and isinstance(value, bool)):
                return None, f"`{opt.name}` has the wrong type."
            if isinstance(value, str):
                value = value.strip()
                if opt.max_length is not None and len(value) > opt.max_length:
                    return None, f"`{opt.name}` is longer than {opt.max_length} characters."
                if opt.choices and value not in {v for _, v in opt.choices}:
                    return None, f"`{opt.name}` must be one of " + \
                        ", ".join(v for _, v in opt.choices) + "."
            values[opt.name] = value
        for opt in spec.options:
            if opt.required and values.get(opt.name) in (None, ""):
                return None, f"`/{spec.name}` needs `{opt.name}`."
        return values, None

    def _task(self, values, place, *, key="task"):
        """-> (task, None) or (None, refusal). A typed id is re-checked: it must
        exist and its project must not be archived."""
        _where, _project, here = place
        wanted = str(values.get(key) or "").strip()
        if not wanted:
            if here is None:
                return None, f"Which task? Add `{key}:` — or use this in the task's thread."
            return here, None
        task = self.surface.stores.tasks.get(wanted) if _plain_id(wanted) else None
        if task is None:
            return None, f"I don't know a task `{_short(wanted)}`."
        project = self.surface.stores.projects.get(task.project_id)
        if project is None or project.archived:
            return None, f"Task {task.id} belongs to an archived project; nothing changed."
        return task, None

    # -- commands ----------------------------------------------------------

    def _command(self, interaction, reply, place) -> None:
        spec, options, label = self._spec(interaction.get("data") or {})
        if spec is None:
            reply.refuse(f"I don't know `/{_short(label)}` — Discord's command list may be "
                         "out of date. Restart Discord to refresh it.")
            return
        if place[0] not in spec.places:
            reply.refuse(NOT_HERE)
            return
        values, refusal = self._values(spec, options)
        if refusal is not None:
            reply.refuse(refusal)
            return
        handler = getattr(self, "_cmd_" + label.replace(" ", "_"))
        handler(values, reply, place, interaction)

    def _cmd_task(self, values, reply, place, interaction) -> None:
        where, project, _task = place
        target = project
        if values.get("project"):
            target = self.surface.router.place(values["project"])
            if target is None:
                reply.refuse(f"I don't know a live project `{_short(values['project'])}`.")
                return
        skill = values.get("skill") or ""
        if skill:
            try:
                skill = check_skill(skill)
            except SkillRefused as exc:
                reply.refuse(str(exc))
                return
        provider = values.get("provider") or ""
        brief = values.get("brief") or ""
        if not brief:
            # A phone's option box is one line; a long brief gets a real box.
            reply.modal(f"{MODAL_PREFIX}{target.id if target else ''}:{skill}:{provider}",
                        "New task", [{"type": 1, "components": [{
                            "type": 4, "custom_id": "brief", "label": "Brief", "style": 2,
                            "min_length": 1, "max_length": 4000, "required": True}]}])
            return
        self._open(brief, skill, provider, target, reply)

    def _open(self, brief, skill, provider, target, reply) -> None:
        if skill:
            brief = f'{brief}\n\nUse the "{skill}" skill.'
        self.surface._intake(brief, reply, target, provider=provider or None,
                             parse_named=False)

    def _cmd_status(self, values, reply, place, interaction) -> None:
        where, project, here = place
        if values.get("task"):
            task, refusal = self._task(values, place)
            if refusal:
                reply.refuse(refusal)
                return
            self.surface._status(reply, None, task)
            return
        self.surface._status(reply, project if where != "dm" else None, here)

    def _cmd_cancel(self, values, reply, place, interaction) -> None:
        task, refusal = self._task(values, place)
        if refusal:
            reply.refuse(refusal)
            return
        if task.state in TERMINAL_STATES:
            reply.refuse(f"Task {task.id} is already {task.state.value}.")
            return
        self.surface._cancel(task.id, reply)

    def _cmd_steer(self, values, reply, place, interaction) -> None:
        task, refusal = self._task(values, place)
        if refusal:
            reply.refuse(refusal)
            return
        self.surface._steer(task.id, values["text"], reply, False)

    def _cmd_answer(self, values, reply, place, interaction) -> None:
        task, refusal = self._task(values, place)
        if refusal:
            reply.refuse(refusal)
            return
        task = self.surface.stores.tasks.get(task.id) or task
        waiting = open_questions(task)
        if not waiting:
            reply.refuse(f"Task {task.id} has no open question to answer.")
            return
        index = waiting[0][0]
        if values.get("question") not in (None, ""):
            wanted = str(values["question"])
            indices = {str(i): i for i, _ in waiting}
            if wanted not in indices:
                reply.refuse(f"Task {task.id} has no open question `{_short(wanted)}`.")
                return
            index = indices[wanted]
        self.surface._answer(task, index, values["text"], reply)

    def _approve(self, verdict, values, reply) -> None:
        target, refusal = self.surface.approval_target(
            verdict, values.get("code") or "", reply.channel_id, bare_falls_through=False)
        if refusal is not None:
            reply.refuse(refusal)
            return
        self.surface.decide(target, verdict, reply)

    def _cmd_yes(self, values, reply, place, interaction) -> None:
        self._approve("allow", values, reply)

    def _cmd_no(self, values, reply, place, interaction) -> None:
        self._approve("deny", values, reply)

    def _cmd_always(self, values, reply, place, interaction) -> None:
        self._approve("always", values, reply)

    def _cmd_resume(self, values, reply, place, interaction) -> None:
        task, refusal = self._task(values, place)
        if refusal:
            reply.refuse(refusal)
            return
        if task.state != TaskState.BLOCKED:
            reply.refuse(f"Task {task.id} is {task.state.value}; only a blocked task resumes.")
            return
        provider = ProviderName(values["provider"]) if values.get("provider") else None
        self.surface._resume(task.id, provider, reply)

    def _cmd_skill(self, values, reply, place, interaction) -> None:
        where, project, task = place
        try:
            name = check_skill(values["name"])
        except SkillRefused as exc:
            reply.refuse(str(exc))
            return
        request = values.get("request") or ""
        if where == "task":
            # Orchestrators and implementers are Claude or Codex, which have the
            # skill installed: in a task thread, a skill is a steer.
            text = f'Use the "{name}" skill for: {request}' if request else \
                f'Use the "{name}" skill.'
            self.surface._steer(task.id, text, reply, False)
            return
        incoming = self.surface._incoming(request, reply.channel_id, project, task)
        self.surface._chat(reply, where, project, request, False, incoming, skill=name)

    def _cmd_project_list(self, values, reply, place, interaction) -> None:
        self.surface._list_projects(reply)

    # -- autocomplete --------------------------------------------------------

    def _autocomplete(self, interaction, reply, place) -> None:
        spec, options, _label = self._spec(interaction.get("data") or {})
        if spec is None or place[0] not in spec.places:
            reply.choices([])
            return
        focused = next((o for o in options if o.get("focused")), None)
        opt = next((o for o in spec.options if focused and o.name == focused.get("name")), None)
        if opt is None or not opt.complete:
            reply.choices([])
            return
        others = {o.get("name"): o.get("value") for o in options if not o.get("focused")}
        where, project, task = place
        pending = self.surface.pending_here(reply.channel_id)
        if opt.complete == "always_code":
            # Never suggest an `/always` that would be refused.
            pending = [r for r in pending if self.surface.allowlistable(r)[0]]
        ctx = Context(stores=self.surface.stores, project=project, task=task,
                      pending=pending, options=others)
        reply.choices(completions(opt.complete, str(focused.get("value") or ""), ctx))

    # -- buttons and modals --------------------------------------------------

    def _component(self, interaction, reply) -> None:
        data = interaction.get("data") or {}
        custom = str(data.get("custom_id") or "")
        if custom.startswith(APPROVE_PREFIX):
            verdict, code = "allow", custom[len(APPROVE_PREFIX):]
        elif custom.startswith(DENY_PREFIX):
            verdict, code = "deny", custom[len(DENY_PREFIX):]
        else:
            reply.refuse("That button isn't one I know. Nothing ran.")
            return
        message_id = str((interaction.get("message") or {}).get("id") or "")
        request, refusal = self.surface.button_target(message_id, reply.channel_id, code)
        if refusal is not None:
            reply.refuse(refusal)
            return
        reply.acknowledge_update()
        # The resolution strips the buttons (the surface's watcher); the
        # answer is said in the channel the approval was asked in.
        from .gateway import ChannelReply

        self.surface.decide(request, verdict, ChannelReply(self.surface, reply.channel_id))

    def _modal(self, interaction, reply, place) -> None:
        data = interaction.get("data") or {}
        custom = str(data.get("custom_id") or "")
        parts = custom[len(MODAL_PREFIX):].split(":") if custom.startswith(MODAL_PREFIX) else []
        if len(parts) != 3:
            reply.refuse("That form isn't one I know. Nothing opened.")
            return
        if place[0] not in registry.BY_NAME["task"].places:
            reply.refuse(NOT_HERE)
            return
        project_id, skill, provider = parts
        # The custom id round-tripped through Discord: every part is re-checked.
        target = None
        if project_id:
            target = self.surface.stores.projects.get(project_id) if _plain_id(project_id) \
                else None
            if target is None or target.archived:
                reply.refuse("That project is archived or gone, so I opened nothing.")
                return
        if skill:
            try:
                skill = check_skill(skill)
            except SkillRefused as exc:
                reply.refuse(str(exc))
                return
        if provider and provider not in ("claude", "codex"):
            reply.refuse("Provider must be claude or codex.")
            return
        brief = ""
        for row in data.get("components") or []:
            for field in row.get("components") or []:
                if field.get("custom_id") == "brief" and isinstance(field.get("value"), str):
                    brief = field["value"].strip()
        if not brief or len(brief) > 4000:
            reply.refuse("A task needs a brief of 1-4000 characters.")
            return
        self._open(brief, skill, provider, target, reply)


def _plain_id(value: str) -> bool:
    return value.isalnum() and len(value) <= 32


def _short(value, limit: int = 40) -> str:
    text = " ".join(str(value).replace("`", "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
