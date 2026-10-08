"""Small synchronous REST adapter using v1 credentials and request conventions.

Two credentials pass through here and neither may leave it:

* the **bot token**, only ever in the `Authorization` header;
* an **interaction token** (S1), a 15-minute credential that can post as the
  application, only ever in a URL path — `/interactions/{id}/{token}/callback`
  and `/webhooks/{app}/{token}/…`.

So nothing here logs, and no error string carries a URL: a transport failure
is reported by exception class only, and an HTTP failure by status and
Discord's own message, with every secret the call used redacted from it.
Callers log the operation name, never the path.

**Rate limits (PR A).** A 429 is slept out here only while the total sleep
for one call stays within `MAX_SLEEP_S` (10 s), and at most `MAX_RETRIES`
times; anything more raises `DiscordHTTPError(status=429, retry_after=…)` so
the caller can defer the work rather than hold a worker hostage. The sleep
waits on the adapter's close event, so `close()` cuts it short. The
Reporter's circuit breaker counts those raises.

**Never a DELETE.** This adapter has no delete method of any kind, and a
test asserts no `DELETE` ever leaves it (decisions D3): channels and threads
are archived or left alone, never removed.
"""
from __future__ import annotations

import json
import math
import re
import threading

import httpx

from jarvis import config
from jarvis.tools import discord as v1

DiscordError = v1.DiscordError

EPHEMERAL = 64
# Message flag: the post appears, but no push or desktop notification fires,
# whatever the owner's per-channel notification setting is (decisions D1).
SUPPRESS_NOTIFICATIONS = 4096
MAX_SLEEP_S = 10.0
MAX_RETRIES = 3
# A thread with no activity for this many minutes auto-archives (7 days).
THREAD_ARCHIVE_MINUTES = 10080
# Discord JSON error codes the surfaces act on. The meaning of each is fixed by
# Discord; nothing here infers one from a message string.
UNKNOWN_CHANNEL = 10003
MISSING_ACCESS = 50001
MISSING_PERMISSIONS = 50013
THREAD_ARCHIVED = 50083
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,500}$")
_SNOWFLAKE = re.compile(r"^[0-9]{1,24}$")


class DiscordHTTPError(DiscordError):
    """A non-2xx answer. `status` lets a caller tell a dead interaction token
    (401/404) from anything else; `code` is Discord's own JSON error code
    (10003 unknown channel, 50013 missing permissions, 50083 thread archived)
    or None; `retry_after` is the wait Discord asked for on a 429 that was too
    long to sleep here. The message never holds a path or a token."""

    def __init__(self, message: str, status: int, code=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def describe(exc) -> dict:
    """The loggable facts of a failure: HTTP status and Discord code, or the
    transport's exception class. Never its message, which may hold a body."""
    return {"status": getattr(exc, "status", None), "code": getattr(exc, "code", None),
            "error": type(exc).__name__}


def _snowflake(value) -> str:
    value = str(value)
    if not _SNOWFLAKE.match(value):
        raise DiscordError("Error: Discord id is not a snowflake")
    return value


def _snowflake_or(value, default) -> str:
    return str(value) if value else default


def _token(value) -> str:
    value = str(value)
    if not _TOKEN.match(value):
        raise DiscordError("Error: interaction token has an unexpected shape")
    return value


class DiscordRest:
    """transport: httpx BaseTransport, or callable with httpx.request's signature.

    files are (filename, str-or-bytes body) pairs, retained in memory for retries.
    Long content is attached intact, including approval commands.
    """

    def __init__(self, transport=None):
        self._client = (httpx.Client(transport=transport)
                        if isinstance(transport, httpx.BaseTransport) else None)
        self._request = self._client.request if self._client else (transport or httpx.request)
        self._closed = threading.Event()

    def close(self):
        self._closed.set()          # wakes any 429 wait at once
        if self._client:
            self._client.close()

    def _sleep(self, delay: float) -> None:
        """A rate-limit wait that `close()` interrupts."""
        if self._closed.wait(delay):
            raise DiscordError("Error: Discord adapter closed during a rate-limit wait")

    def _api(self, method, path, *, secrets=(), timeout=30, patient=True, **kwargs):
        """`timeout` is the transport's; `patient=False` raises a 429 at once
        instead of sleeping it out (a status read with a 5 s budget, B1)."""
        token = v1._load_bundle()["bot_token"]
        hidden = [s for s in (token, *secrets) if s]

        def redact(text: str) -> str:
            for secret in hidden:
                text = text.replace(secret, "[REDACTED]")
            return text

        retries, slept = 0, 0.0
        while True:
            try:
                response = self._request(
                    method, f"{config.DISCORD_API}{path}",
                    headers={"Authorization": f"Bot {token}"}, timeout=timeout, **kwargs)
            except Exception as exc:
                # Transport exception messages can contain the URL, headers or
                # request data: the class is all that leaves.
                raise DiscordError(f"Error: Discord transport failed ({type(exc).__name__})") from None
            if response.status_code == 429:
                try:
                    delay = response.json().get("retry_after")
                    if delay is None:
                        delay = response.headers["Retry-After"]
                    delay = float(delay)
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError("invalid retry delay")
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise DiscordHTTPError(redact(v1._fail(response)), 429) from None
                if not patient or slept + delay > MAX_SLEEP_S or retries >= MAX_RETRIES:
                    # The caller defers: a worker parked for minutes on one
                    # bucket would hold every other post behind it.
                    raise DiscordHTTPError(redact(v1._fail(response)), 429,
                                           retry_after=delay) from None
                retries += 1
                slept += delay
                self._sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                code = None
                try:
                    code = response.json().get("code")
                except (ValueError, AttributeError):
                    pass
                raise DiscordHTTPError(redact(v1._fail(response)), response.status_code, code)
            return response

    def _id(self, method, path, **kwargs):
        response = self._api(method, path, **kwargs)
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError):
            raise DiscordError("Error: Discord API response has no id") from None

    def create_channel(self, guild_id, name, *, kind: int = 0, parent_id=None,
                       topic=None) -> str:
        """A text channel (0) or a category (4). Never a permission overwrite:
        a channel Jarvis makes inherits its category's permissions."""
        body = {"name": name, "type": int(kind)}
        if parent_id is not None:
            body["parent_id"] = str(parent_id)
        if topic is not None:
            body["topic"] = topic
        return self._id("POST", f"/guilds/{guild_id}/channels", json=body)

    def modify_channel(self, channel_id, *, name=None, parent_id=None, topic=None):
        """Rename, move between categories, or set the topic (B1, D3/D4).

        Only those three fields can ever be sent: never `lock_permissions` and
        never `permission_overwrites`, so moving a channel into Jarvis Archive
        does not re-sync or rewrite who can see it."""
        body = {}
        if name is not None:
            body["name"] = name
        if parent_id is not None:
            body["parent_id"] = str(parent_id)
        if topic is not None:
            body["topic"] = topic
        if not body:
            raise DiscordError("Error: nothing to change on the channel")
        self._api("PATCH", f"/channels/{channel_id}", json=body)

    # -- reads (B1: setup and the channel light) ----------------------------

    def _json(self, path, *, timeout=30, patient=True):
        try:
            return self._api("GET", path, timeout=timeout, patient=patient).json()
        except ValueError:
            raise DiscordError("Error: Discord response is not JSON") from None

    def get_channel(self, channel_id, *, timeout=30, patient=True) -> dict:
        body = self._json(f"/channels/{channel_id}", timeout=timeout, patient=patient)
        if not isinstance(body, dict):
            raise DiscordError("Error: Discord channel is not an object")
        return body

    def guild(self, guild_id, *, timeout=30, patient=True) -> dict:
        """The guild object; it carries `owner_id` and `roles`."""
        body = self._json(f"/guilds/{guild_id}", timeout=timeout, patient=patient)
        if not isinstance(body, dict):
            raise DiscordError("Error: Discord guild is not an object")
        return body

    def guild_member(self, guild_id, user_id, *, timeout=30, patient=True) -> dict:
        body = self._json(f"/guilds/{guild_id}/members/{user_id}", timeout=timeout,
                          patient=patient)
        if not isinstance(body, dict):
            raise DiscordError("Error: Discord member is not an object")
        return body

    def guild_channels(self, guild_id, *, timeout=30, patient=True) -> list:
        body = self._json(f"/guilds/{guild_id}/channels", timeout=timeout, patient=patient)
        if not isinstance(body, list):
            raise DiscordError("Error: Discord channel list is not a list")
        return [c for c in body if isinstance(c, dict)]

    def my_guilds(self) -> list:
        body = self._json("/users/@me/guilds")
        if not isinstance(body, list):
            raise DiscordError("Error: Discord guild list is not a list")
        return [g for g in body if isinstance(g, dict)]

    def me(self, *, timeout=30, patient=True) -> dict:
        body = self._json("/users/@me", timeout=timeout, patient=patient)
        if not isinstance(body, dict):
            raise DiscordError("Error: Discord user is not an object")
        return body

    def create_thread(self, channel_id, name, *,
                      auto_archive_duration: int = THREAD_ARCHIVE_MINUTES) -> str:
        """A public thread (type 11) that archives itself after a week idle."""
        return self._id("POST", f"/channels/{channel_id}/threads",
                        json={"name": name, "type": 11,
                              "auto_archive_duration": int(auto_archive_duration)})

    def add_owner(self, thread_id, owner_id=None):
        """Add the owner to a thread, so it shows in their thread list."""
        owner = _snowflake_or(owner_id, self._owner())
        self._api("PUT", f"/channels/{thread_id}/thread-members/{owner}")

    def ping_owner(self, channel_id, owner_id=None) -> str:
        """The D1 ping line: exactly `<@owner>`, mentioning the owner and no one
        else. The one post that is allowed to notify; never sent to a DM."""
        owner = _snowflake_or(owner_id, self._owner())
        return self._id("POST", f"/channels/{channel_id}/messages",
                        json={"content": f"<@{owner}>",
                              "allowed_mentions": {"parse": [], "users": [owner]}})

    @staticmethod
    def _owner() -> str:
        owner = v1._load_bundle().get("owner_id")
        if not owner:
            raise DiscordError("Error: no owner_id in the Discord token bundle")
        return str(owner)

    @staticmethod
    def _message(content, embed, components=None, flags=None):
        body = {"allowed_mentions": {"parse": []}}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        if components is not None:
            body["components"] = components
        if flags:
            body["flags"] = flags
        return body

    def _payload(self, content=None, embed=None, files=(), components=None, flags=None):
        """-> request kwargs: JSON, or multipart when anything is attached."""
        files = list(files)
        overflow = getattr(content, "overflow", None)
        if overflow is not None:
            files.append(("report.txt", overflow))
        if content is not None and len(content) > 2000:
            files.append(("message.txt", content))
            content = "Full message attached (unabridged)."
        body = self._message(content, embed, components, flags)
        if not files:
            return {"json": body}
        body["attachments"] = [{"id": i, "filename": name} for i, (name, _) in enumerate(files)]
        return {"data": {"payload_json": json.dumps(body, ensure_ascii=False)},
                "files": [(f"files[{i}]", (name, data.encode("utf-8") if isinstance(data, str)
                                          else data, "text/plain; charset=utf-8"))
                          for i, (name, data) in enumerate(files)]}

    def post(self, channel_id, content=None, embed=None, files=(), components=None,
             *, silent: bool = False) -> str:
        """`silent` sets SUPPRESS_NOTIFICATIONS: every guild post that is not
        the D1 ping line carries it, so nothing but the ping line buzzes."""
        return self._id("POST", f"/channels/{channel_id}/messages",
                        **self._payload(content, embed, files, components,
                                        SUPPRESS_NOTIFICATIONS if silent else None))

    def edit(self, channel_id, message_id, content=None, embed=None, components=None):
        """Edit first. Only when Discord says the thread is archived (50083) is
        it reopened and the edit tried once more — never pre-emptively, which
        cost a second request on every edit of a live thread."""
        body = self._message(content, embed, components)
        path = f"/channels/{channel_id}/messages/{message_id}"
        try:
            self._api("PATCH", path, json=body)
        except DiscordHTTPError as exc:
            if exc.code != THREAD_ARCHIVED:
                raise
            self.unarchive(channel_id)
            self._api("PATCH", path, json=body)

    def unarchive(self, thread_id):
        self._api("PATCH", f"/channels/{thread_id}", json={"archived": False})

    # -- interactions (S1) -------------------------------------------------
    # The interaction token is in every path below, so every call passes it as
    # a secret to redact, and none of these paths is ever logged.

    def callback(self, interaction_id, token, kind, data=None):
        """The initial response: 4 message, 5 deferred, 6 deferred update,
        7 update message, 8 autocomplete, 9 modal."""
        token = _token(token)
        body = {"type": int(kind)}
        if data is not None:
            body["data"] = data
        self._api("POST", f"/interactions/{_snowflake(interaction_id)}/{token}/callback",
                  secrets=(token,), json=body)

    def edit_original(self, app_id, token, *, content=None, embed=None, files=(),
                      components=None):
        token = _token(token)
        self._api("PATCH", f"/webhooks/{_snowflake(app_id)}/{token}/messages/@original",
                  secrets=(token,), **self._payload(content, embed, files, components))

    def followup(self, app_id, token, *, content=None, embed=None, files=(),
                 components=None, ephemeral=False) -> str:
        token = _token(token)
        return self._id("POST", f"/webhooks/{_snowflake(app_id)}/{token}",
                        secrets=(token,), **self._payload(content, embed, files, components,
                                                          EPHEMERAL if ephemeral else None))

    # -- command registration (S1) -----------------------------------------
    # The path is spelled only in discord/commands.py; this module just sends.

    def get_json(self, path):
        try:
            return self._api("GET", path).json()
        except ValueError:
            raise DiscordError("Error: Discord response is not JSON") from None

    def get_commands(self, app_id) -> list:
        from .commands import commands_path

        response = self._api("GET", commands_path(app_id))
        try:
            body = response.json()
        except ValueError:
            raise DiscordError("Error: Discord command list is not JSON") from None
        if not isinstance(body, list):
            raise DiscordError("Error: Discord command list is not a list")
        return body

    def put_commands(self, app_id, payload) -> list:
        from .commands import commands_path

        response = self._api("PUT", commands_path(app_id), json=list(payload))
        try:
            return list(response.json())
        except (ValueError, TypeError):
            return []
