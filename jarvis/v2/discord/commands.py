"""The registry as Discord's slash-command payload, and the one sync (plan §3).

This is the **only** module that may name Discord's `/applications/` API: a
test greps the tree to hold it to that. Two callers reach `sync`: the daemon,
once, after the first READY (which supplies the application id), and the
human CLI `jarvis discord commands`. No tool and no MCP tool can, so no agent
can register, alter or remove a command.

Why the daemon may sync at all: the payload is a pure function of
`jarvis/v2/commands.py` — no skill, project or task enters it — and the call
needs the bot token, which no agent can read. A bad registry fails closed:
commands vanish, approvals time out and deny, the HUD still works.

Registration is global with `contexts [GUILD, BOT_DM]`, because guild commands
never appear in a DM, and `default_member_permissions "0"`, which hides them
in the guild from everyone but administrators. It does not apply in DMs, so the
owner check in `interactions.py` is the real gate.

The sync is GET, diff, and **one bulk PUT only if the lists differ**. It never
sends a DELETE: a bulk overwrite is the only way a command ever leaves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

from ..commands import REGISTRY, Cmd, Opt

SUB_COMMAND, STRING, INTEGER, BOOLEAN = 1, 3, 4, 5
CHAT_INPUT = 1
CONTEXTS = [0, 1]                   # GUILD, BOT_DM — never 2 (private channels)
INTEGRATION_TYPES = [0]             # GUILD_INSTALL
MEMBER_PERMISSIONS = "0"            # hidden in the guild from non-administrators
_KINDS = {"string": STRING, "integer": INTEGER, "boolean": BOOLEAN}
_NAME = re.compile(r"^[-_a-z0-9]{1,32}$")
_SNOWFLAKE = re.compile(r"^[0-9]{1,24}$")


APPLICATION_ME = "/oauth2/applications/@me"     # who we are, for the human CLI


def commands_path(app_id: str) -> str:
    """The one place the path is spelled. `app_id` must be a snowflake."""
    app_id = str(app_id)
    if not _SNOWFLAKE.match(app_id):
        raise ValueError("application id must be a snowflake")
    return f"/applications/{app_id}/commands"


def _option(opt: Opt) -> dict:
    body = {"type": _KINDS[opt.kind], "name": opt.name, "description": opt.description}
    if opt.required:
        body["required"] = True
    if opt.complete:
        body["autocomplete"] = True
    if opt.choices:
        body["choices"] = [{"name": n, "value": v} for n, v in opt.choices]
    if opt.max_length is not None:
        body["max_length"] = opt.max_length
    return body


def _command(cmd: Cmd) -> dict:
    if cmd.subcommands:
        options = [{"type": SUB_COMMAND, "name": sub.name, "description": sub.description,
                    "options": [_option(o) for o in sub.options]}
                   for sub in cmd.subcommands]
    else:
        options = [_option(o) for o in cmd.options]
    return {"name": cmd.name, "type": CHAT_INPUT, "description": cmd.description,
            "options": options, "contexts": list(CONTEXTS),
            "integration_types": list(INTEGRATION_TYPES),
            "default_member_permissions": MEMBER_PERMISSIONS}


def to_discord(registry=REGISTRY) -> list[dict]:
    """Every Discord-surface command, in registry order. Static: same input,
    same bytes, whatever is on disk."""
    return [_command(cmd) for cmd in registry if "discord" in cmd.surfaces]


# -- normalization: what Discord echoes back vs what we send -----------------

_OPTION_KEYS = ("type", "name", "description", "required", "autocomplete", "choices",
                "options", "min_length", "max_length", "min_value", "max_value",
                "channel_types")
_COMMAND_KEYS = ("name", "type", "description", "options", "contexts",
                 "integration_types", "default_member_permissions", "nsfw")


def _norm_option(raw: dict) -> dict:
    out = {}
    for key in _OPTION_KEYS:
        value = raw.get(key)
        if value in (None, False, []):
            continue
        if key == "options":
            value = [_norm_option(o) for o in value]
        elif key == "choices":
            value = [{"name": c.get("name"), "value": c.get("value")} for c in value]
        elif key == "channel_types":
            value = sorted(value)
        out[key] = value
    return out


def _norm_command(raw: dict) -> dict:
    out = {}
    for key in _COMMAND_KEYS:
        value = raw.get(key)
        if key == "type":
            value = value or CHAT_INPUT
        if key in ("contexts", "integration_types") and value is not None:
            value = sorted(value)
        if key == "default_member_permissions" and value is not None:
            value = str(value)
        if value in (None, False, []):
            continue
        if key == "options":
            value = [_norm_option(o) for o in value]
        out[key] = value
    return out


def normalize(commands) -> list[dict]:
    """Drop what Discord adds (id, version, application_id, localizations,
    defaults) so a remote list and a local payload compare by meaning."""
    return sorted((_norm_command(c) for c in commands or []), key=lambda c: c.get("name", ""))


# -- validation ------------------------------------------------------------


def _length(obj) -> int:
    """Discord's 4000-character budget per command: names, descriptions,
    choice names and values, recursively."""
    total = len(str(obj.get("name", ""))) + len(str(obj.get("description", "")))
    for choice in obj.get("choices") or []:
        total += len(str(choice["name"])) + len(str(choice["value"]))
    for option in obj.get("options") or []:
        total += _length(option)
    return total


def _check_options(where: str, options, problems: list[str], depth: int = 0) -> None:
    if len(options) > 25:
        problems.append(f"{where}: more than 25 options")
    seen_optional = False
    names = set()
    for option in options:
        name = option.get("name", "")
        here = f"{where} {name}"
        if not _NAME.match(name):
            problems.append(f"{here}: bad option name")
        if name in names:
            problems.append(f"{here}: duplicate option name")
        names.add(name)
        if not 1 <= len(option.get("description", "")) <= 100:
            problems.append(f"{here}: description must be 1-100 characters")
        if option.get("type") == SUB_COMMAND:
            if depth:
                problems.append(f"{here}: subcommand nested too deep")
            _check_options(here, option.get("options") or [], problems, depth + 1)
            continue
        if option.get("type") not in (STRING, INTEGER, BOOLEAN):
            problems.append(f"{here}: unknown option type")
        if option.get("required"):
            if seen_optional:
                problems.append(f"{here}: a required option after an optional one")
        else:
            seen_optional = True
        choices = option.get("choices") or []
        if len(choices) > 25:
            problems.append(f"{here}: more than 25 choices")
        if choices and option.get("autocomplete"):
            problems.append(f"{here}: choices and autocomplete are exclusive")
        for choice in choices:
            if not 1 <= len(str(choice.get("name", ""))) <= 100:
                problems.append(f"{here}: choice name must be 1-100 characters")
        if option.get("max_length") is not None and not 1 <= option["max_length"] <= 6000:
            problems.append(f"{here}: max_length out of range")


def validate(payload: list[dict]) -> None:
    """Raise ValueError naming every limit the payload breaks."""
    problems = []
    if len(payload) > 100:
        problems.append("more than 100 commands")
    names = set()
    for command in payload:
        name = command.get("name", "")
        if not _NAME.match(name):
            problems.append(f"/{name}: bad command name")
        if name in names:
            problems.append(f"/{name}: duplicate command")
        names.add(name)
        if not 1 <= len(command.get("description", "")) <= 100:
            problems.append(f"/{name}: description must be 1-100 characters")
        if command.get("contexts") != CONTEXTS:
            problems.append(f"/{name}: contexts must be {CONTEXTS}")
        if command.get("integration_types") != INTEGRATION_TYPES:
            problems.append(f"/{name}: integration_types must be {INTEGRATION_TYPES}")
        if command.get("default_member_permissions") != MEMBER_PERMISSIONS:
            problems.append(f"/{name}: default_member_permissions must be \"0\"")
        _check_options(f"/{name}", command.get("options") or [], problems)
        if _length(command) > 4000:
            problems.append(f"/{name}: more than 4000 characters")
    if problems:
        raise ValueError("; ".join(problems))


# -- sync --------------------------------------------------------------------


@dataclass
class SyncResult:
    state: str = "pending"           # pending | ok | stale (check only) | failed
    count: int = 0
    changed: int = 0
    put: bool = False
    synced_at: float | None = None
    error: str | None = None
    detail: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"state": self.state, "count": self.count, "changed": self.changed,
                "synced_at": self.synced_at, "error": self.error}

    def line(self) -> str:
        if self.state == "failed":
            return f"discord commands: failed ({self.error})"
        if self.put:
            return f"discord commands: synced ({self.count}; {self.changed} changed)"
        return f"discord commands: up to date ({self.count})"


def diff(local: list[dict], remote) -> list[str]:
    """The command names that differ (added, removed or changed)."""
    mine = {c["name"]: c for c in normalize(local)}
    theirs = {c.get("name"): c for c in normalize(remote)}
    return sorted(name for name in mine.keys() | theirs.keys()
                  if mine.get(name) != theirs.get(name))


def _safe_error(exc: Exception) -> str:
    # DiscordRest already redacts the bot token; never add a path or a body.
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


def sync(rest, app_id: str, registry=REGISTRY, *, check_only: bool = False,
         clock=time.time) -> SyncResult:
    """GET, diff, then at most one PUT. Failure is a state, never a retry loop."""
    payload = to_discord(registry)
    result = SyncResult(count=len(payload))
    try:
        validate(payload)
        commands_path(app_id)
        remote = rest.get_commands(app_id)
        changed = diff(payload, remote)
        result.changed, result.detail = len(changed), changed
        if changed and not check_only:
            rest.put_commands(app_id, payload)
            result.put = True
        result.state = "ok" if (not changed or result.put) else "stale"
        result.synced_at = clock()
    except Exception as exc:
        result.state, result.error = "failed", _safe_error(exc)
    return result


def application_id(rest) -> str:
    body = rest.get_json(APPLICATION_ME)
    app = str((body or {}).get("id") or "")
    if not _SNOWFLAKE.match(app):
        raise ValueError("Discord did not return an application id")
    return app


def main(action: str = "check", rest=None, out=print) -> int:
    """`jarvis discord commands [--check | --sync]` — human-only.

    `--check` (the default) compares the registered list with the code and
    changes nothing; `--sync` sends the one bulk PUT when they differ. Nothing
    printed holds the bot token."""
    from jarvis.tools.discord import invite_url

    from .rest import DiscordRest

    owned = rest is None
    rest = rest or DiscordRest()
    try:
        try:
            app = application_id(rest)
        except Exception as exc:
            out(f"discord commands: cannot read the application ({_safe_error(exc)}).")
            return 1
        out("Invite (adds the applications.commands scope; harmless if the bot is "
            f"already in your server):\n  {invite_url(app)}")
        out("Developer Portal → General Information: the Interactions Endpoint URL "
            "must be empty, or commands arrive over HTTP instead of the gateway.")
        result = sync(rest, app, check_only=(action != "sync"))
        if result.state == "stale":
            out(f"discord commands: {result.changed} differ from the code "
                f"({', '.join('/' + n for n in result.detail)}). Run with --sync to register.")
            return 1
        out(result.line())
        return 0 if result.state == "ok" else 1
    finally:
        if owned:
            rest.close()
