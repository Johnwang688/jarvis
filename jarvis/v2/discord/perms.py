"""The bot's permissions, computed the way Discord does (decisions D5, B1).

`effective()` follows Discord's documented algorithm ("Permissions → Permission
Hierarchy"), so the setup command and the HUD's channel light can say *which*
permission is missing before a post fails with a bare 50013:

1. The guild owner has every permission.
2. Base: the `@everyone` role (its id is the guild's id), OR every role the
   member has. Administrator in the base means every permission, everywhere.
3. In a channel: the `@everyone` overwrite (deny, then allow), then every role
   overwrite the member has (all denies, then all allows), then the member's
   own overwrite (deny, then allow).
4. Implicit: without View Channel in a channel, nothing else there counts;
   without Send Messages, Embed Links, Attach Files, Mention Everyone and
   Send TTS do not either.

D5 is decided: **no Administrator.** `REQUIRED` is the eight-permission set
the owner switches on by hand; `excess()` names what the bot holds beyond the
job, and Administrator is the one setup warns about loudly — it ignores every
channel restriction, so "never delete a channel" would rest on Jarvis's code
alone, and a leaked token would be a server takeover.

Discord sends permission integers as decimal **strings** (they outgrew 53
bits); everything here accepts either and works in Python ints.
"""
from __future__ import annotations

KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
SEND_TTS_MESSAGES = 1 << 12
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38

ALL = (1 << 64) - 1

NAMES = {
    MANAGE_CHANNELS: "Manage Channels",
    VIEW_CHANNEL: "View Channel",
    SEND_MESSAGES: "Send Messages",
    EMBED_LINKS: "Embed Links",
    ATTACH_FILES: "Attach Files",
    READ_MESSAGE_HISTORY: "Read Message History",
    CREATE_PUBLIC_THREADS: "Create Public Threads",
    SEND_MESSAGES_IN_THREADS: "Send Messages in Threads",
    ADMINISTRATOR: "Administrator",
    MANAGE_ROLES: "Manage Roles",
    MANAGE_GUILD: "Manage Server",
    MANAGE_WEBHOOKS: "Manage Webhooks",
    BAN_MEMBERS: "Ban Members",
    KICK_MEMBERS: "Kick Members",
}

# The decided set (plan §2), in the order the owner meets them in Discord.
_REQUIRED_ORDER = (MANAGE_CHANNELS, VIEW_CHANNEL, SEND_MESSAGES, EMBED_LINKS, ATTACH_FILES,
                   READ_MESSAGE_HISTORY, CREATE_PUBLIC_THREADS, SEND_MESSAGES_IN_THREADS)
REQUIRED = 0
for _bit in _REQUIRED_ORDER:
    REQUIRED |= _bit
assert REQUIRED == 309237763088, REQUIRED

# More than the job: flagged by setup and on the HUD, Administrator loudest.
_EXCESS_ORDER = (ADMINISTRATOR, MANAGE_ROLES, MANAGE_GUILD, MANAGE_WEBHOOKS, BAN_MEMBERS,
                 KICK_MEMBERS)
EXCESS = 0
for _bit in _EXCESS_ORDER:
    EXCESS |= _bit

# Lost with Send Messages (Discord's implicit rule).
_NEEDS_SEND = EMBED_LINKS | ATTACH_FILES | MENTION_EVERYONE | SEND_TTS_MESSAGES

ADMIN_WARNING = (
    "WARNING: the bot has ADMINISTRATOR. That ignores every channel restriction and "
    "adds roles, bans, kicks, webhooks and server settings: \"never delete a channel\" "
    "would rest on Jarvis's code alone, and a leaked bot token would be a full server "
    "takeover. Remove Administrator from the bot's role and grant the eight permissions "
    "above instead (decisions D5)."
)


def bits(value) -> int:
    """A permission integer from Discord's decimal string (or an int)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _role_map(roles) -> dict[str, int]:
    return {str(role.get("id")): bits(role.get("permissions"))
            for role in roles or () if isinstance(role, dict)}


def _member_id(member) -> str:
    user = (member or {}).get("user") or {}
    return str(user.get("id") or (member or {}).get("id") or "")


def granted(guild, roles, member) -> int:
    """The base permissions: `@everyone` OR every role the member has. Before
    the Administrator expansion, so `excess()` names what was really given."""
    guild_id = str((guild or {}).get("id") or "")
    table = _role_map(roles if roles is not None else (guild or {}).get("roles"))
    value = table.get(guild_id, 0)
    for role_id in (member or {}).get("roles") or ():
        value |= table.get(str(role_id), 0)
    return value


def effective(guild, roles, member, channel=None) -> int:
    """Discord's algorithm. `channel` None is the server-wide (base) answer."""
    guild = guild or {}
    if str(guild.get("owner_id") or "") and str(guild.get("owner_id")) == _member_id(member):
        return ALL
    base = granted(guild, roles, member)
    if base & ADMINISTRATOR:
        return ALL
    if channel is None:
        return base
    overwrites = {str(o.get("id")): o for o in channel.get("permission_overwrites") or ()
                  if isinstance(o, dict)}
    value = base
    everyone = overwrites.get(str(guild.get("id") or ""))
    if everyone:
        value &= ~bits(everyone.get("deny"))
        value |= bits(everyone.get("allow"))
    allow = deny = 0
    for role_id in (member or {}).get("roles") or ():
        overwrite = overwrites.get(str(role_id))
        if overwrite:
            allow |= bits(overwrite.get("allow"))
            deny |= bits(overwrite.get("deny"))
    value &= ~deny
    value |= allow
    own = overwrites.get(_member_id(member))
    if own:
        value &= ~bits(own.get("deny"))
        value |= bits(own.get("allow"))
    if not value & VIEW_CHANNEL:
        return 0
    if not value & SEND_MESSAGES:
        value &= ~_NEEDS_SEND
    return value


def missing(value) -> list[str]:
    """The required permissions `value` lacks, by name, in the decided order."""
    value = bits(value)
    return [NAMES[bit] for bit in _REQUIRED_ORDER if not value & bit]


def excess(value) -> list[str]:
    """What `value` holds beyond the job, by name, Administrator first."""
    value = bits(value)
    return [NAMES[bit] for bit in _EXCESS_ORDER if value & bit]


def invite_url(client_id, guild_id=None) -> str:
    """The bot invite with the decided permissions and both scopes; with a
    guild, Discord preselects it and hides the picker."""
    url = (f"https://discord.com/oauth2/authorize?client_id={client_id}"
           f"&scope=bot+applications.commands&permissions={REQUIRED}")
    if guild_id:
        url += f"&guild_id={guild_id}&disable_guild_select=true"
    return url
