"""The guild file `jarvis auth discord-guild` writes (B1).

`~/.config/jarvis/discord_guild.json` (`config.DISCORD_GUILD_PATH`, env
`JARVIS_DISCORD_GUILD`) holds four snowflakes: the server, the **Jarvis**
category, the **Jarvis Archive** category and **#ungrouped**. It is read here,
by the daemon, *whenever it is needed* — so running setup while the daemon is
up takes effect on the next read, with no restart. The read is cached on the
file's (mtime, size) so a status poll costs one `stat`.

Only setup writes it (mode 600), and no agent can (`permissions.protected_paths`).
An unreadable or malformed file reads as "not configured": the surfaces then
behave exactly as before B1 (DM safety net, any guild), which is the safe side.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading

from jarvis import config

_SNOWFLAKE = re.compile(r"^[0-9]{5,24}$")
FIELDS = ("guild_id", "category_id", "archive_category_id", "ungrouped_channel_id")
_cache: dict = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class GuildConfig:
    guild_id: str
    category_id: str
    archive_category_id: str
    ungrouped_channel_id: str

    def to_json(self) -> dict:
        return {name: getattr(self, name) for name in FIELDS}


def is_snowflake(value) -> bool:
    return isinstance(value, str) and bool(_SNOWFLAKE.match(value))


def path() -> Path:
    return Path(config.DISCORD_GUILD_PATH).expanduser()


def parse(data) -> GuildConfig | None:
    if not isinstance(data, dict):
        return None
    values = {name: data.get(name) for name in FIELDS}
    if not all(is_snowflake(v) for v in values.values()):
        return None
    return GuildConfig(**values)


def load() -> GuildConfig | None:
    """The configured guild, or None. Never raises."""
    target = path()
    try:
        stat = target.stat()
    except OSError:
        return None
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache.get("key") == key:
            return _cache.get("value")
    try:
        value = parse(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError):
        value = None
    with _lock:
        _cache.update(key=key, value=value)
    return value


def write(cfg: GuildConfig, target: Path | None = None) -> Path:
    """Atomically, mode 600 from the first byte. Setup's alone."""
    target = Path(target) if target is not None else path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(cfg.to_json(), indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)
    return target
