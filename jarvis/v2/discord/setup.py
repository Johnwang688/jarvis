"""`jarvis auth discord-guild` — the one-time server setup (plan §2, B1).

Human-only, like every `jarvis auth`: a CLI subcommand, never a tool, so no
agent can choose the server Jarvis creates channels in. It needs `jarvis auth
discord` first (the bot token bundle), and then:

1. lists the servers the bot is in, and the owner picks one;
2. prints the invite pinned to that server — `scope=bot+applications.commands`,
   `permissions=309237763088`, `guild_id`, `disable_guild_select=true` — the
   shortcut for switching the decided permissions on (D5: the owner does it by
   hand, and never Administrator);
3. checks the bot's permissions there: a **missing** one blocks (fix it, press
   Enter, it checks again), an **excess** one is a warning, and Administrator
   is a loud one;
4. after a y/N (O6), creates the **Jarvis** and **Jarvis Archive** categories
   and **#ungrouped** under Jarvis — or reuses ones a previous run made;
5. writes `~/.config/jarvis/discord_guild.json` (mode 600) with the four ids.

The daemon reads that file whenever it needs it, so no restart is needed;
it then links the Inbox to #ungrouped. Nothing printed holds the bot token.
"""
from __future__ import annotations

from . import perms
from .guild import GuildConfig, path as guild_path, write as write_guild
from .rest import describe

CATEGORY, TEXT = 4, 0
JARVIS, ARCHIVE, UNGROUPED = "Jarvis", "Jarvis Archive", "ungrouped"
UNGROUPED_TOPIC = "Jarvis · Inbox chats and tasks (ungrouped work)"


def _safe(exc) -> str:
    facts = describe(exc)
    if facts["status"] is None:
        return facts["error"]
    return f"HTTP {facts['status']}, code {facts['code']}"


def _find(channels, name, kind, parent=None):
    for channel in channels:
        if (channel.get("type") == kind and str(channel.get("name") or "").lower() == name.lower()
                and (parent is None or str(channel.get("parent_id") or "") == str(parent))):
            return str(channel.get("id"))
    return None


def run(rest=None, *, ask=input, out=print) -> int:
    from jarvis.tools import discord as v1

    from . import commands
    from .rest import DiscordRest

    try:
        v1._load_bundle()
    except v1.DiscordError:
        out("Discord is not connected yet: run `jarvis auth discord` first.")
        return 1
    owned = rest is None
    rest = rest or DiscordRest()
    try:
        return _run(rest, ask, out, commands)
    finally:
        if owned:
            rest.close()


def _run(rest, ask, out, commands) -> int:
    try:
        app = commands.application_id(rest)
        bot = str(rest.me()["id"])
        guilds = rest.my_guilds()
    except Exception as exc:
        out(f"Could not read the bot from Discord ({_safe(exc)}).")
        return 1
    if not guilds:
        out("The bot is in no server yet. Invite it to yours first:\n"
            f"  {perms.invite_url(app)}\nthen run this again.")
        return 1

    # 1. Which server.
    out("Servers the bot is in:")
    for n, g in enumerate(guilds, 1):
        out(f"  {n}. {g.get('name') or '(unnamed)'}  ({g.get('id')})")
    choice = ask(f"Which server is Jarvis's? [1-{len(guilds)}, default 1]: ").strip() or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= len(guilds):
        out("No server chosen; nothing changed.")
        return 1
    picked = guilds[int(choice) - 1]
    guild_id = str(picked.get("id") or "")

    # 2. The invite, pinned to that server.
    out("\nInvite (the shortcut for switching the permissions on; harmless if the bot is "
        f"already there):\n  {perms.invite_url(app, guild_id)}\n")
    out("Required: " + ", ".join(perms.missing(0)) + ". Never Administrator.")

    # 3. Permissions: missing blocks, excess warns.
    while True:
        try:
            guild = rest.guild(guild_id)
            member = rest.guild_member(guild_id, bot)
        except Exception as exc:
            out(f"Could not read the server's permissions ({_safe(exc)}).")
            return 1
        roles = guild.get("roles") or []
        lacking = perms.missing(perms.effective(guild, roles, member))
        given = perms.granted(guild, roles, member)
        extra = perms.excess(given)
        if given & perms.ADMINISTRATOR:
            out("\n" + "!" * 72 + "\n" + perms.ADMIN_WARNING + "\n" + "!" * 72)
        elif extra:
            out("Warning: the bot has more than it needs: " + ", ".join(extra)
                + ". Consider removing them from its role.")
        if not lacking:
            out("Permissions: everything required is on.")
            break
        out("MISSING (blocking): " + ", ".join(lacking)
            + ".\nSwitch them on for the bot's role (Server Settings → Roles), or use the "
            "invite above.")
        again = ask("Press Enter to check again, or q to stop: ").strip().lower()
        if again in ("q", "quit", "n", "no"):
            out("Stopped; nothing changed.")
            return 1

    # 4. The categories and #ungrouped, after a y/N.
    try:
        channels = rest.guild_channels(guild_id)
    except Exception as exc:
        out(f"Could not list the server's channels ({_safe(exc)}).")
        return 1
    category = _find(channels, JARVIS, CATEGORY)
    archive = _find(channels, ARCHIVE, CATEGORY)
    ungrouped = _find(channels, UNGROUPED, TEXT, category) if category else None
    plan = []
    for label, found in ((f"category {JARVIS}", category), (f"category {ARCHIVE}", archive),
                         (f"#{UNGROUPED} under {JARVIS}", ungrouped)):
        plan.append(f"  {'use existing' if found else 'create'} {label}")
    out("\nThis will:\n" + "\n".join(plan)
        + f"\n  write {guild_path()} (mode 600)\nNothing is ever deleted.")
    if ask("Go ahead? [y/N]: ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return 1
    try:
        if not category:
            category = rest.create_channel(guild_id, JARVIS, kind=CATEGORY)
        if not archive:
            archive = rest.create_channel(guild_id, ARCHIVE, kind=CATEGORY)
        if not ungrouped:
            ungrouped = rest.create_channel(guild_id, UNGROUPED, kind=TEXT, parent_id=category,
                                            topic=UNGROUPED_TOPIC)
    except Exception as exc:
        out(f"Discord refused a create ({_safe(exc)}); re-run to finish — what exists is reused.")
        return 1

    # 5. The file.
    target = write_guild(GuildConfig(guild_id, str(category), str(archive), str(ungrouped)))
    out(f"Saved {target}. The daemon picks it up without a restart and links the Inbox "
        f"to #{UNGROUPED}; then use \"Create channels\" in the HUD's Discord panel.")
    return 0
